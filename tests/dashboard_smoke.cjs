/* Dashboard behavior with a small DOM fixture; no browser or network dependency. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor() { this.children = []; this.value = ''; this.checked = false; }
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  set innerHTML(value) { throw new Error('Unsafe HTML sink'); }
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Node());
    return elements.get(id);
  },
  createElement() { return new Node(); },
};
const unsafeName = '<img src=x onerror=alert(1)>';
const chat = {
  thread_id: 'chat & one', chat_name: unsafeName, recorded_turns: 1, model_calls: 1,
  missing_usage_turns: 0, unknown_credit_calls: 0, known_estimated_credits: '0.01',
  estimated_credits: '0.01', credit_coverage_complete: true,
  usage: {new_input_tokens: 10, cached_input_tokens: 5, output_tokens: 2},
};
const report = {chats: [chat], turns: [chat], diagnostics: 0, coverage: 'Local', updated_at: '2026-10-02T00:00:00Z'};
const requests = [];
const polls = [];
let fail = false;
const context = vm.createContext({
  location: {search:'?embed=1&thread_id=chat%20%26%20one'}, URLSearchParams,
  document, Node, Option: class extends Node { constructor(label, value) { super(); this.textContent = label; this.value = value; } },
  AbortController, setTimeout, clearTimeout,
  setInterval(callback, interval) { polls.push(interval); },
  async fetch(url) { requests.push(url); return {ok: !fail, async json() { return report; }}; },
});
const html = fs.readFileSync(path.join(__dirname, '../src/token_budget_mcp/dashboard.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__POLL_INTERVAL_MS__', '750');

(async () => {
  vm.runInContext(script, context);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(polls, [750]);
  assert.equal(requests[0], '/api/turns?thread_id=chat%20%26%20one');
  assert.equal(document.getElementById('chat').value, 'chat & one');
  assert.equal(document.getElementById('chats').textContent, 1);
  context.renderAccountUsage({limits: [{limit_id: 'codex', used_percent: 29}], updated_at: 'now'});
  assert.equal(document.getElementById('account-usage').textContent, '· 29% used');
  context.renderAccountUsage({limits: [{limit_id: 'codex', used_percent: 29}], stale: true});
  assert.equal(document.getElementById('account-usage').textContent, '· 29% used (stale)');
  context.renderAccountUsage({limits: [{limit_id: 'codex', label: '5h', window_duration_mins: 300, used_percent: 40}, {limit_id: 'codex', label: 'Weekly', window_duration_mins: 10080, used_percent: 80}]});
  assert.equal(document.getElementById('account-usage').textContent, '· 5h 40% · Weekly 80% used');
  assert.equal(vm.runInContext("usageText({usage: {output_tokens: 10}, unsupported_usage_turns: 1}, 'output_tokens')", context), '10 (partial)');
  context.renderAccountUsage(null);
  assert.equal(document.getElementById('account-usage').textContent, 'Usage unavailable');
  const button = document.getElementById('summary').children[0].children[0].children[0];
  assert.equal(button.textContent, unsafeName); // Text remains literal, not executable HTML.
  document.getElementById('chat').value = 'chat & one';
  await context.update();
  assert.equal(requests.at(-1), '/api/turns?thread_id=chat%20%26%20one');
  fail = true;
  await context.update();
  assert.match(document.getElementById('error').textContent, /Collector unavailable/);
  fail = false;
  await context.update();
  assert.equal(document.getElementById('error').textContent, '');
  console.log('PASS: dashboard polling, safe text, filter encoding, HTTP failure and recovery');
})().catch(error => { console.error(error); process.exitCode = 1; });
