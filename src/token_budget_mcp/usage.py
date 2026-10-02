"""Pure usage aggregation and chat presentation; no file or network access."""
import json
from datetime import datetime, timezone
from decimal import Decimal
from .core import cost
from .config import DEFAULT_INACTIVITY_SECONDS, positive_number

FIELDS = ('input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','reasoning_output_tokens','total_tokens')


def normalize_usage(usage):
    """Keep valid counts; invalid optional fields become unknown, never guessed."""
    if not isinstance(usage, dict) or not all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens', 'output_tokens')
    ):
        raise ValueError('Usage requires nonnegative integer input and output counts')
    clean = {}
    invalid = False
    for field in FIELDS:
        value = usage.get(field)
        valid = value is None or (type(value) is int and value >= 0)
        if field == 'cached_input_tokens' and value is not None:
            valid = valid and value <= usage['input_tokens']
        if field == 'reasoning_output_tokens' and value is not None:
            valid = valid and value <= usage['output_tokens']
        clean[field] = value if valid else None
        invalid |= not valid
    return clean, invalid

def session_display_name(title, origin, source):
    """Use explicit chat titles, then internal task metadata without reading prompts."""
    if isinstance(title,str) and title.strip(): return title
    try:
        metadata=json.loads(source) if isinstance(source,str) else source
    except (ValueError,TypeError): metadata=None
    agent=metadata.get('subagent') if isinstance(metadata,dict) else None
    spawn=agent.get('thread_spawn') if isinstance(agent,dict) else None
    if isinstance(spawn,dict):
        task=spawn.get('agent_path')
        task=task.rstrip('/').split('/')[-1] if isinstance(task,str) else ''
        words=task.replace('_',' ').replace('-',' ').lower()
        role=spawn.get('agent_role')
        description=words+' '+(role.lower() if isinstance(role,str) else '')
        if 'review' in description:
            return 'Security review' if 'security' in description else 'Code review'
        if words and words not in ('root','agent','subagent') and not all(c in '0123456789abcdef ' for c in words):
            return words.capitalize()
        nickname=spawn.get('agent_nickname')
        if isinstance(nickname,str) and nickname.strip(): return 'Agent · '+nickname
    if origin=='guardian_review': return 'Approval review'
    if origin=='subagent' or agent is not None: return 'Agent session'
    return None

def floating_sessions(turns, now=None, inactivity_seconds=DEFAULT_INACTIVITY_SECONDS):
    """One latest turn per chat within the activity window; latest chat fallback."""
    positive_number(inactivity_seconds, "inactivity_seconds")
    now = now or datetime.now(timezone.utc)
    latest = []
    seen = set()
    for row in turns:
        if row.get('model') == 'codex-auto-review' or row.get('is_internal'): continue
        key = row.get('thread_id') or row['turn_id']
        if key in seen: continue
        seen.add(key)
        latest.append(row)
    recent = []
    for row in latest:
        try:
            stamp = datetime.fromisoformat((row.get('last_activity_at') or row.get('started') or '').replace('Z', '+00:00'))
            age = (now - stamp).total_seconds()
            if 0 <= age < inactivity_seconds: recent.append(row)
        except (ValueError, TypeError): pass
    return recent or latest[:1]

def aggregate_chats(turns):
    """Aggregate all persisted turn records, independently of history pagination."""
    groups = {}
    for turn in turns:
        key = turn.get('thread_id') or turn['turn_id']
        if key not in groups:
            groups[key] = dict(turn, recorded_turns=0, missing_usage_turns=0, unsupported_usage_turns=0, unknown_credit_calls=0,
                model_calls=0, credits_pending=False, credit_priced_calls=0, settled_turns=0, pending_turns=0, usage={k:0 for k in (*FIELDS,'new_input_tokens')},
                estimated_credits=None, known_estimated_credits=Decimal(0), consumption_scope='Live tokens across recorded turns; credits across completed/interrupted turns only')
        chat = groups[key]
        chat['recorded_turns'] += 1
        chat['unsupported_usage_turns'] += int(not turn.get('usage_coverage_complete', True))
        chat['model_calls'] += turn['model_calls']
        if turn.get('status') in ('completed','interrupted'):
            chat['settled_turns'] += 1
            chat['missing_usage_turns'] += int(turn.get('usage') is None)
            chat['unknown_credit_calls'] += turn.get('unknown_credit_calls',0)
            chat['credit_priced_calls'] += turn['model_calls']-turn.get('unknown_credit_calls',0)
            chat['known_estimated_credits'] += Decimal(turn.get('known_estimated_credits') or '0')
        else:
            chat['pending_turns'] += 1
        for field in chat['usage']:
            value = (turn.get('usage') or {}).get(field)
            if turn.get('usage') is not None:
                if value is None: chat['usage'][field] = None
                elif chat['usage'][field] is not None: chat['usage'][field] += value
        stamps = [v for v in (chat.get('last_activity_at'),turn.get('last_activity_at')) if v]
        chat['last_activity_at'] = max(stamps, default=None)
        chat['first_recorded_at'] = min(filter(None, [chat.get('first_recorded_at'),turn.get('started')]), default=None)
    for chat in groups.values():
        chat['credit_coverage_complete'] = not (chat['missing_usage_turns'] or chat['unknown_credit_calls'] or chat['unsupported_usage_turns'])
        chat['known_estimated_credits'] = str(chat['known_estimated_credits'])
        if chat['credit_coverage_complete']: chat['estimated_credits'] = chat['known_estimated_credits']
        if not chat['model_calls']: chat['usage'] = None
    return list(groups.values())

def summarize_calls(calls, credit_rates, prices):
    """Sum call usage and exact model prices, preserving unknown/partial counts."""
    totals = {field: 0 for field in FIELDS}
    missing = set()
    credits, amounts = [], []
    valid_calls = 0
    malformed_calls = 0
    for model, raw in calls:
        try:
            usage, invalid = normalize_usage(json.loads(raw))
        except (ValueError, TypeError):
            malformed_calls += 1
            continue
        valid_calls += 1
        malformed_calls += int(invalid)
        for field in FIELDS:
            value = usage.get(field)
            if value is None: missing.add(field)
            else: totals[field] += value
        cached = usage.get('cached_input_tokens')
        valid_cache = type(cached) is int and 0 <= cached <= usage['input_tokens']
        credit_rate = credit_rates['models'].get(model)
        if credit_rate and valid_cache:
            credits.append(cost(credit_rate, usage['input_tokens'], usage['output_tokens'], cached))
        rate = prices['models'].get(model)
        if rate and valid_cache and not usage.get('cache_write_input_tokens'):
            amounts.append(cost(rate, usage['input_tokens'], usage['output_tokens'], cached))
    for field in missing: totals[field] = None
    cached = totals['cached_input_tokens']
    totals['new_input_tokens'] = (totals['input_tokens'] - cached
        if type(cached) is int and totals['input_tokens'] is not None and 0 <= cached <= totals['input_tokens'] else None)
    known_credits = str(sum(map(Decimal, credits)))
    return {
        'usage': totals if valid_calls else None,
        'usage_coverage_complete': malformed_calls == 0,
        'malformed_calls': malformed_calls,
        'known_estimated_credits': known_credits,
        'unknown_credit_calls': len(calls) - len(credits),
        'estimated_credits': known_credits if calls and not malformed_calls and len(credits) == len(calls) else None,
        'api_equivalent_cost_usd': str(sum(map(Decimal, amounts))) if calls and not malformed_calls and len(amounts) == len(calls) else None,
    }
