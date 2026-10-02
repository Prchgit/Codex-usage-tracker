"""Core independent of MCP. SQLite stores local request snapshots and reports."""
import copy
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from .config import DEFAULT_PROVIDER_TIMEOUT, DEFAULT_PREVIEW_TTL_SECONDS, TOKENS_PER_RATE_UNIT, positive_number

SCOPE = "Only requests executed by this server; host model calls excluded."


def cost(rate, input_tokens, output_tokens, cached_tokens=0):
    if any(type(value) is not int for value in (input_tokens, output_tokens, cached_tokens)) or not 0 <= cached_tokens <= input_tokens or output_tokens < 0:
        raise ValueError("Invalid usage counts")
    value = ((input_tokens - cached_tokens) * Decimal(rate['input'])
             + cached_tokens * Decimal(rate['cached_input'])
             + output_tokens * Decimal(rate['output'])) / Decimal(TOKENS_PER_RATE_UNIT)
    return str(value.quantize(Decimal('0.00000001')))


class DemoProvider:
    demo = True

    def count(self, model, messages):
        return max(1, math.ceil(len(json.dumps(messages, ensure_ascii=False).encode('utf-8')) / 3)), 'heuristic_estimate'

    def generate(self, model, messages, max_output):
        n, _ = self.count(model, messages)
        return {'answer': 'DEMO ONLY: simulated response. No model was called.',
                'model': model, 'status': 'completed', 'usage': {
                    'input_tokens': n, 'output_tokens': min(20, max_output),
                    'total_tokens': n + min(20, max_output),
                    'input_tokens_details': {'cached_tokens': 0},
                    'output_tokens_details': {'reasoning_tokens': 0}}}


class OpenAIProvider:
    demo = False

    def __init__(self, client=None, timeout=DEFAULT_PROVIDER_TIMEOUT):
        positive_number(timeout, 'timeout')
        from openai import OpenAI
        # No automatic retries: ambiguous failures must not incur duplicate spend.
        self.client = client if client is not None else OpenAI(max_retries=0, timeout=timeout)

    def count(self, model, messages):
        result = self.client.responses.input_tokens.count(model=model, input=messages)
        return result.input_tokens, 'provider_count'

    def generate(self, model, messages, max_output):
        result = self.client.responses.create(model=model, input=messages,
            max_output_tokens=max_output, store=False, service_tier='default')
        return {'answer': result.output_text, 'model': result.model,
                'provider_response_id': result.id, 'status': result.status,
                'usage': result.usage.model_dump() if result.usage else None}


class BudgetService:
    def __init__(self, database, provider, catalog=None, preview_ttl_seconds=DEFAULT_PREVIEW_TTL_SECONDS):
        self.preview_ttl_seconds = positive_number(preview_ttl_seconds, 'preview_ttl_seconds')
        self.provider = provider
        self.catalog = json.loads(Path(catalog or Path(__file__).with_name('catalog.json')).read_text())
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.lock = threading.RLock()
        self.db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.commit()

    def close(self):
        self.db.close()

    def put(self, identifier, value):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO records VALUES (?,?)', (identifier, json.dumps(value)))
            self.db.commit()

    def get(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT body FROM records WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Unknown request or preview ID')
        return json.loads(row[0])

    def preview(self, messages, selected_model='gpt-4.1', task_type='general', output_min=128, output_max=512):
        if task_type not in ('general', 'extraction', 'classification', 'rewrite', 'complex_reasoning'):
            raise ValueError('Unsupported task type')
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ('system','developer','user','assistant')
                               or not isinstance(m['content'], str) for m in messages):
            raise ValueError('Supply nonempty text messages with only role and content')
        if type(output_min) is not int or type(output_max) is not int or not 1 <= output_min <= output_max:
            raise ValueError('Require 1 <= output_min <= output_max')
        if selected_model not in self.catalog['models']:
            raise ValueError('Model is not in the configured catalog')
        # Snapshot before counting; callers cannot mutate the reviewed payload.
        messages = copy.deepcopy(messages)
        candidates = {}
        for model, rate in self.catalog['models'].items():
            if output_max > rate['max_output']:
                continue
            n, method = self.provider.count(model, messages)
            if n + output_max > rate['context']:
                continue
            candidates[model] = {'input_tokens': n, 'input_basis': method,
                'output_tokens_range': [output_min, output_max],
                'cost_range_usd': [cost(rate,n,output_min), cost(rate,n,output_max)],
                'pricing': rate}
        if selected_model not in candidates:
            raise ValueError('Selected model cannot accommodate this request')
        chosen = selected_model
        reason = 'Retain selected model; no validated downgrade rule for this task.'
        if task_type in ('extraction','classification','rewrite'):
            chosen = min(candidates, key=lambda m: Decimal(candidates[m]['cost_range_usd'][1]))
            reason = 'Lowest estimated cost among catalog models for the declared simple task; quality must be validated.'
        current = Decimal(candidates[selected_model]['cost_range_usd'][1])
        proposed = Decimal(candidates[chosen]['cost_range_usd'][1])
        identifier = 'p_' + uuid.uuid4().hex
        record = {'preview_id': identifier, 'created_at': time.time(), 'status': 'previewed',
            'demo': self.provider.demo, 'scope': SCOPE, 'selected_model': selected_model,
            'messages': messages, 'task_type': task_type, 'output_max': output_max,
            'payload_sha256': hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest(),
            'pricing_version': self.catalog['version'], 'candidates': candidates,
            'recommendation': {'model': chosen, 'reason': reason, 'quality_validated': False,
                'estimated_saving_percent': float((current-proposed)/current*100) if current else 0},
            'assumptions': ['Output range is user supplied, not a prediction.',
                'Preview assumes no cached input; pricing is standard USD, excludes taxes and negotiated rates.',
                'No images, tools, agents or automatic context retrieval in v1.'], 'request_id': None}
        self.put(identifier, record)
        return self.public_preview(record)

    @staticmethod
    def public_preview(record):
        result = {k:v for k,v in record.items() if k != 'messages'}
        selected = record['candidates'][record['selected_model']]
        recommendation = record['candidates'][record['recommendation']['model']]
        result['view'] = (f"{'DEMO — ' if record['demo'] else ''}Request preview\n"
            f"Preview: {record['preview_id']}\nModel: {record['selected_model']}\n"
            f"Input: {selected['input_tokens']} ({selected['input_basis']})\n"
            f"Output: {selected['output_tokens_range']} (assumed allowance)\n"
            f"Estimated USD: {'–'.join(selected['cost_range_usd'])}\n"
            f"Recommendation: {record['recommendation']['model']}\n"
            f"Recommended estimate USD: {'–'.join(recommendation['cost_range_usd'])}\n"
            f"Estimated saving: {record['recommendation']['estimated_saving_percent']:.1f}%\n"
            f"{record['recommendation']['reason']}\n{SCOPE}")
        return result

    def execute(self, preview_id, model, confirmed=False):
        if not confirmed:
            raise ValueError('Review preview and obtain user selection before setting confirmed=true')
        with self.lock:
            # Transactional claim prevents duplicate submissions across processes.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                record = self.get(preview_id)
                if record['request_id']:
                    self.db.rollback()
                    return self.get(record['request_id'])
                if time.time() - record['created_at'] > self.preview_ttl_seconds:
                    raise ValueError('Preview expired; create a fresh preview')
                if record['demo'] != self.provider.demo:
                    raise ValueError('Preview execution mode changed; create a fresh preview')
                if model not in record['candidates']:
                    raise ValueError('Model was not included in this preview')
                identifier = 'r_' + uuid.uuid4().hex
                record['request_id'] = identifier
                report = {'request_id': identifier, 'preview_id': preview_id,
                    'status': 'running', 'model': model, 'demo': record['demo'], 'scope': SCOPE}
                self.db.execute('UPDATE records SET body=? WHERE id=?', (json.dumps(record), preview_id))
                self.db.execute('INSERT INTO records VALUES (?,?)', (identifier,json.dumps(report)))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        try:
            response = self.provider.generate(model, record['messages'], record['output_max'])
            usage = response.get('usage')
            amount = None
            if usage and usage.get('input_tokens') is not None and usage.get('output_tokens') is not None:
                cached = (usage.get('input_tokens_details') or {}).get('cached_tokens',0) or 0
                amount = cost(record['candidates'][model]['pricing'],usage['input_tokens'],usage['output_tokens'],cached)
            low,high = map(Decimal,record['candidates'][model]['cost_range_usd'])
            comparison = ('unavailable' if amount is None else
                'below' if Decimal(amount)<low else 'above' if Decimal(amount)>high else 'within')
            report.update(response)
            report.update({'calculated_cost_usd': amount, 'estimate_comparison': comparison,
                'usage_basis': 'simulated' if record['demo'] else 'provider_reported' if usage else 'unavailable',
                'pricing_version': record['pricing_version'], 'pricing': record['candidates'][model]['pricing']})
        except Exception:
            # Avoid exposing provider exception text, credentials or request contents.
            report.update({'status': 'failed_or_unknown', 'usage': None, 'calculated_cost_usd': None,
                'error': 'Provider call failed; billing may be unknown. Automatic retry disabled.'})
        usage = report.get('usage') or {}
        cached = (usage.get('input_tokens_details') or {}).get('cached_tokens', 'unavailable')
        reasoning = (usage.get('output_tokens_details') or {}).get('reasoning_tokens', 'unavailable')
        report['view'] = (f"{'DEMO — ' if report['demo'] else ''}Usage report\nRequest: {identifier}\n"
            f"Status: {report['status']}\nModel: {report['model']}\n"
            f"Input / output / total tokens: {usage.get('input_tokens', 'unavailable')} / "
            f"{usage.get('output_tokens', 'unavailable')} / {usage.get('total_tokens', 'unavailable')}\n"
            f"Cached input: {cached}; reasoning within output: {reasoning}\n"
            f"Usage basis: {report.get('usage_basis', 'unavailable')}\n"
            f"Calculated USD: {report['calculated_cost_usd']}\n"
            f"Estimate comparison: {report.get('estimate_comparison', 'unavailable')}\n{SCOPE}")
        self.put(identifier, report)
        return report
