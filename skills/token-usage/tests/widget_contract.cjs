/* Dependency-free logic/lifecycle harness. This does not replace browser/Widgets acceptance. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'widget.js'), 'utf8');
process.env.TZ = 'America/New_York';

function harness() {
  let root, disposal, timerId = 0;
  const requests = [], timers = new Map(), blobs = [], listeners = new Map();
  const document = {hidden: false, currentScript: {dataset: {}}, activeElement: null};
  class Element {
    constructor(tag = 'div', dataset = {}) {
      this.tagName = tag.toUpperCase(); this.dataset = dataset; this.attributes = {}; this.style = {};
      this.value = ''; this.hidden = false; this.disabled = false; this.textContent = '';
      this.listeners = new Map(); this.children = new Map(); this.writes = 0; this._html = '';
    }
    set innerHTML(value) {
      this._html = value; this.writes++;
      this.details = [...value.matchAll(/<details[^>]*data-preserve="([^"]+)"[^>]*>/g)].map(match => {
        const el = new Element('details', {preserve: match[1]}); el.open = false; return el;
      });
    }
    get innerHTML() { return this._html; }
    setAttribute(name, value) { this.attributes[name] = value; }
    querySelector(selector) {
      let element = this.children.get(selector);
      if (!element) {
        const match = selector.match(/^\[data-(pref|action|focus)="(.+)"\]$/);
        element = new Element(match && match[1] === 'pref' ? 'select' : 'div', match ? {[match[1]]: match[2]} : {});
        this.children.set(selector, element);
      }
      return element;
    }
    querySelectorAll(selector) {
      if (selector === 'details[data-preserve]') return this.details || [];
      const values = {'[data-period]': ['1h', '24h', '7d', '30d', 'all', 'custom'], '[data-pref]': ['model', 'route', 'project', 'task', 'custom_start', 'custom_end'], '[data-scope]': ['own', 'descendants', 'all']}[selector];
      if (!values) return [];
      const key = selector.slice(6, -1);
      return values.map(value => { const element = this.querySelector('[data-' + key + '="' + value + '"]'); element.dataset[key] = value; return element; });
    }
    appendChild(child) { if (child.id === 'token-observatory') root = child; return child; }
    addEventListener(name, callback) { this.listeners.set(name, callback); }
    removeEventListener(name, callback) { if (this.listeners.get(name) === callback) this.listeners.delete(name); }
    contains() { return true; }
    closest() { return this; }
    focus() { document.activeElement = this; }
    scrollIntoView() { this.scrolled = true; }
    click() { this.clicked = true; }
    remove() { this.removed = true; }
  }
  document.createElement = tag => new Element(tag);
  document.body = new Element('body'); document.documentElement = document.body;
  document.getElementById = () => null;
  document.addEventListener = (name, fn) => listeners.set(name, fn);
  document.removeEventListener = (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); };
  const window = {__ouroWidgetOnDispose: fn => { disposal = fn; }}; window.parent = window;
  const fetch = (url, options = {}) => {
    assert.match(url, /^\/api\/extensions\/token-usage\/(preferences$|data\?|export\?)/, 'every request uses the exact host-owned skill prefix');
    return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
  };
  const context = {window, document, fetch, Intl, Date, URLSearchParams, AbortController, Blob, Set, console,
    URL: {createObjectURL(blob) { blobs.push(blob); return 'blob:' + blobs.length; }, revokeObjectURL() {}},
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout(id) { timers.delete(id); }};
  vm.runInNewContext(source, context, {filename: 'widget.js'});
  function respond(index, payload, status = 200) {
    const text = typeof payload === 'string' ? payload : JSON.stringify(payload);
    requests[index].resolve({ok: status >= 200 && status < 300, status, text: async () => text, blob: async () => new Blob([text], {type: 'application/json'})});
  }
  function click(dataset) { const el = new Element('button', dataset); root.listeners.get('click')({target: el}); }
  function change(key, value) { const el = root.querySelector('[data-pref="' + key + '"]'); el.value = value; root.listeners.get('change')({target: el}); }
  function tick(delay) { [...timers].filter(([, v]) => v.delay === delay).forEach(([id, v]) => { timers.delete(id); v.fn(); }); }
  return {get root() { return root; }, requests, timers, blobs, listeners, document, respond, click, change, tick, dispose: () => disposal()};
}
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
const metric = (value, known = 1, missing = 0) => ({value, exact: value == null ? null : String(value), known, missing});
function data(id = 'stable', output = 20) {
  const summary = {rows: 1, physical_calls: 1, subscription_sessions: 0, unknown_subscription_sessions: 0, fully_reported_rows: 1,
    metrics: {prompt_tokens: metric(9007199254740992), completion_tokens: metric(output), cached_tokens: metric(null, 0, 1), cache_write_tokens: metric(0), cost_confirmed_usd: metric(null, 0, 1), cost_estimated_usd: metric(null, 0, 0), cost_held_usd: metric(null, 0, 0)},
    reported_tokens: 9007199254740992 + output, reported_tokens_exact: '9007199254741013', unknown_cost_rows: 1, undated_rows: 0};
  summary.metrics.prompt_tokens.exact = '9007199254740993';
  return {snapshot_id: id, generated_at: '2026-09-10T08:00:00Z', query: {period: 'all'}, coverage: {status: 'complete', issues: []}, summary,
    trend: [{start: '2026-09-10T00:00:00Z', end: '2026-09-11T00:00:00Z', label: '09-10 UTC', summary}],
    rankings: {models: [{id: 'recorded-model', label: 'Recorded <model>', summary}], routes: [], projects: [], tasks: []},
    facets: {models: [{id: 'recorded-model', label: 'Recorded model'}], routes: [], projects: [{id: 'p', label: 'Project'}], tasks: [{id: 'root', label: 'Root'}]},
    details: {rows: [{attempt_id: 'a', kind: 'attempt', state: 'settled', model: 'recorded-model', task_id: 'root', prompt_tokens: 9007199254740992, token_exact: {prompt_tokens: '9007199254740993'}, completion_tokens: output, cached_tokens: null, cache_write_tokens: 0}], total: 1, page: 1, page_size: 15}, notices: ['Long interpretation notice.']};
}

(async () => {
  const h = harness();
  assert.match(h.requests[0].url, /\/api\/extensions\/token-usage\/preferences$/);
  h.respond(0, {preferences: {period: 'all', project: 'p'}}); await flush();
  assert.equal(new URL(h.requests[1].url, 'http://test').searchParams.get('project'), 'p');
  const firstSnapshot = data(); firstSnapshot.coverage.source_bytes_read = 73000000;
  h.respond(1, firstSnapshot); await flush();
  const content = h.root.querySelector('.to-content');
  assert.match(content.innerHTML, /9,007,199,254,740,993/, 'exact token strings survive the JS integer boundary');
  assert.match(content.innerHTML, /Unknown/, 'null metrics remain unknown');
  assert.match(content.innerHTML, /Recorded &lt;model&gt;/, 'labels are escaped');
  assert.doesNotMatch(h.root.querySelector('.to-coverage').innerHTML, /Long interpretation/);
  assert.match(content.innerHTML, /Reading these numbers/);
  const model = h.root.querySelector('[data-pref="model"]'); model.focus();
  const writes = content.writes;
  h.click({action: 'refresh'}); const unchanged = data(); unchanged.coverage.source_bytes_read = 0; h.respond(2, unchanged); await flush();
  assert.equal(content.writes, writes, 'unchanged refresh with a different I/O byte counter must not replace dashboard content or its text nodes');
  assert.equal(h.document.activeElement, model, 'unchanged refresh preserves focused control');
  assert.equal(h.root.querySelector('[data-pref="project"]').value, 'p');

  h.click({period: '1h'}); const older = h.requests.length - 1;
  h.click({period: '24h'}); const newer = h.requests.length - 1;
  assert.equal(h.requests[older].options.signal.aborted, true, 'superseded request aborts');
  h.respond(newer, data('newer', 222)); await flush(); const newestHTML = content.innerHTML;
  h.respond(older, data('older', 111)); await flush();
  assert.equal(content.innerHTML, newestHTML, 'late response cannot overwrite the current selection');

  h.click({period: 'custom'});
  h.change('custom_start', '2026-03-08'); h.change('custom_end', '2026-03-08');
  h.click({action: 'apply-calendar'});
  const calendar = h.requests.length - 1, q = new URL(h.requests[calendar].url, 'http://test').searchParams;
  assert.equal(q.get('start'), '2026-03-08T05:00:00.000Z');
  assert.equal(q.get('end'), '2026-03-09T04:00:00.000Z', 'inclusive local end becomes the next local midnight across DST');
  h.respond(calendar, data('calendar')); await flush();
  h.tick(250); const pref = h.requests.length - 1;
  assert.equal(h.requests[pref].options.method, 'PUT');
  assert.equal(JSON.parse(h.requests[pref].options.body).custom_start, '2026-03-08');
  h.respond(pref, {ok: true}); await flush();

  h.click({action: 'export'}); const exported = h.requests.length - 1;
  const originalJSON = '{"snapshot_id":"frozen", "rows":[{"prompt_tokens":9007199254740993}]}\n';
  h.respond(exported, originalJSON); await flush();
  assert.equal(await h.blobs[0].text(), originalJSON, 'snapshot export must preserve original JSON bytes exactly');

  h.click({action: 'export'}); h.respond(h.requests.length - 1, {error: 'Snapshot source unavailable', status: 500}); await flush();
  assert.match(h.root.querySelector('.to-error').innerHTML, /Snapshot source unavailable/, 'HTTP 200 error envelopes are not downloaded as successful snapshots');

  const aggregate = data('sessions');
  aggregate.summary.physical_calls = 0; aggregate.summary.rows = 0;
  aggregate.summary.subscription_sessions = 2; aggregate.summary.unknown_subscription_sessions = 2;
  aggregate.summary.subscription_summary = {rows: 2, metrics: {prompt_tokens: metric(null, 0, 2), completion_tokens: metric(null, 0, 2)}, normalized_input_usage: {known: 1, missing: 1}, legacy_input_sessions: 1};
  h.click({action: 'refresh'}); h.respond(h.requests.length - 1, aggregate); await flush();
  assert.match(content.innerHTML, /Harness sessions · token flow/, 'session-only selections automatically show their separate chart');
  assert.match(content.innerHTML, /2 aggregates · 2 with unknown tokens/);
  assert.doesNotMatch(content.innerHTML, /<strong>No records in this selection/);

  const sessionSnapshot = data('normalized-sessions');
  const sessionSummary = {rows: 1, metrics: {prompt_tokens: metric(9007199254740992), completion_tokens: metric(7), cached_tokens: metric(0), cache_write_tokens: metric(null, 0, 1)}, reported_tokens: 9007199254741000, reported_tokens_exact: '9007199254741000', normalized_input_usage: {known: 1, missing: 0}, legacy_input_sessions: 0,
    normalized_metrics: {total_tokens: metric(9007199254740992), cache_read_tokens: metric(0), cache_write_tokens: metric(null, 0, 1)},
    legacy_metrics: {prompt_tokens: metric(999), completion_tokens: metric(7), cached_tokens: metric(777), cache_write_tokens: metric(555)}};
  sessionSummary.metrics.prompt_tokens.exact = '9007199254740993';
  sessionSummary.normalized_metrics.total_tokens.exact = '9007199254740993';
  sessionSnapshot.summary.subscription_sessions = 1;
  sessionSnapshot.summary.subscription_summary = sessionSummary;
  sessionSnapshot.session_trend = [{start: '2026-09-10T00:00:00Z', end: '2026-09-11T00:00:00Z', label: '09-10 UTC', summary: sessionSummary}];
  sessionSnapshot.session_rankings = {models: [], routes: [{id: 'codex-route', label: 'Recorded Codex route', summary: sessionSummary}], projects: [], tasks: []};
  sessionSnapshot.coverage = {status: 'partial', history_complete: false, issues: ['Older linked archive is missing.', 'Metadata collection missing.'], retained_start: '2025-01-01T00:00:00Z', retained_end: '2026-09-10T00:00:00Z', retained_undated_rows: 3};
  sessionSnapshot.details.rows.push({attempt_id: 'session-one', kind: 'subscription_session', subscription_route: 'codex-route', provider: 'provider-label', prompt_tokens: 999, cached_tokens: 777, cache_write_tokens: 555,
    input_token_usage_exact: {total_tokens: '9007199254740993', cache_read_tokens: '0', cache_write_tokens: null}, session_token_exact: {prompt_tokens: '9007199254740993', completion_tokens: '7', cached_tokens: '0', cache_write_tokens: null}});
  h.click({action: 'refresh'}); h.respond(h.requests.length - 1, sessionSnapshot); await flush();
  const requestsBeforeView = h.requests.length;
  h.click({view: 'sessions'});
  assert.equal(h.requests.length, requestsBeforeView, 'switching the cohort uses the same frozen selection without another query');
  assert.match(content.innerHTML, /Harness sessions · token flow/);
  assert.match(content.innerHTML, /Recorded Codex route/, 'session routes rank session tokens, separately from physical calls');
  assert.match(content.innerHTML, /Normalized input: 1 valid objects · 0 absent or invalid/);
  assert.match(content.innerHTML, /9,007,199,254,740,993/, 'normalized session exact strings survive rendering');
  assert.match(content.innerHTML, /Legacy input · comparison only/);
  assert.match(content.innerHTML, /Recorded provider/);
  assert.match(content.innerHTML, /provider-label/);
  assert.match(content.innerHTML, /Recorded cost · physical calls/, 'cost is not misattributed to session totals');
  assert.match(h.root.querySelector('.to-coverage').innerHTML, /^<details[^>]*><summary>/, 'coverage notes are collapsed before the data');
  assert.match(h.root.querySelector('.to-window').textContent, /2025-01-01.*linked history incomplete/, 'the retained-history bounds remain visible');
  assert.ok(content.innerHTML.indexOf('token flow') < content.innerHTML.indexOf('to-cache'), 'chart precedes secondary cache and monetary panels');
  h.tick(250); const savedView = h.requests.length - 1;
  assert.equal(JSON.parse(h.requests[savedView].options.body).view, 'sessions');
  h.respond(savedView, {}); await flush();
  h.click({view: 'physical'});
  assert.match(content.innerHTML, /Physical calls · token flow/);
  assert.doesNotMatch(content.innerHTML, /Recorded Codex route<\/span>/, 'session route ranking is absent from physical charts');

  h.click({action: 'refresh'}); const pending = h.requests.length - 1;
  const beforeDispose = content.writes; h.dispose();
  assert.equal(h.requests[pending].options.signal.aborted, true);
  assert.equal(h.timers.size, 0, 'Stop clears every polling/preferences timer');
  assert.equal(h.listeners.size, 0, 'Stop removes document listeners');
  assert.equal(h.root.listeners.size, 0, 'Stop removes widget listeners');
  assert.equal(h.root.removed, true);
  h.respond(pending, data('too-late')); await flush();
  assert.equal(content.writes, beforeDispose, 'Stop fences late asynchronous work');

  const loading = harness(); loading.respond(0, {}); await flush();
  const initial = data('loading'); initial.coverage = {status: 'loading', issues: ['Catching up']}; initial.details.total = 0;
  loading.respond(1, initial); await flush();
  assert.match(loading.root.querySelector('.to-content').innerHTML, /to-skeleton/);
  assert.equal(loading.root.querySelector('.to-content').attributes['aria-busy'], 'true', 'initial source catch-up remains busy for assistive technology');
  assert.doesNotMatch(loading.root.querySelector('.to-content').innerHTML, /No records/);
  loading.click({action: 'refresh'});
  const failed = data('error'); failed.coverage = {status: 'error', issues: ['Source cannot be read']};
  loading.respond(2, failed); await flush();
  assert.match(loading.root.querySelector('.to-error').innerHTML, /Source cannot be read/);
  assert.match(loading.root.querySelector('.to-error').innerHTML, /Retry/);
  assert.equal(loading.root.querySelector('.to-status').className, 'to-status error');
  loading.dispose();

  const serialized = harness(); serialized.respond(0, {}); await flush();
  serialized.respond(1, data()); await flush();
  serialized.change('model', 'first-model'); serialized.tick(250);
  const firstWrite = serialized.requests.findIndex(request => request.options.method === 'PUT');
  serialized.change('route', 'latest-route'); serialized.tick(250);
  serialized.change('project', 'latest-project'); serialized.tick(250);
  assert.equal(serialized.requests.filter(request => request.options.method === 'PUT').length, 1, 'preference writes are serialized, not aborted/raced');
  assert.equal(serialized.requests[firstWrite].options.signal.aborted, false);
  serialized.respond(firstWrite, {ok: true}); await flush();
  const prefWrites = serialized.requests.filter(request => request.options.method === 'PUT');
  assert.equal(prefWrites.length, 2, 'completion starts the single latest queued save');
  const saved = JSON.parse(prefWrites[1].options.body);
  assert.equal(saved.model, 'first-model');
  assert.equal(saved.route, 'latest-route');
  assert.equal(saved.project, 'latest-project', 'latest complete preference snapshot wins after a burst');
  serialized.dispose();

  const selections = harness(); selections.respond(0, {view: 'sessions', period: '30d', task: 'root', scope: 'descendants'}); await flush();
  selections.respond(1, sessionSnapshot); await flush();
  assert.match(selections.root.querySelector('.to-content').innerHTML, /Harness sessions · token flow/, 'accounting view restores from preferences');
  for (const period of ['1h', '24h', '7d', '30d', 'all']) {
    selections.click({period});
    const selected = new URL(selections.requests.at(-1).url, 'http://test').searchParams;
    assert.equal(selected.get('period'), period);
    assert.equal(selected.has('view'), false, 'display view is not a backend selection parameter');
    selections.respond(selections.requests.length - 1, sessionSnapshot); await flush();
  }
  for (const [key, value] of [['model', 'm'], ['route', 'r'], ['project', 'p'], ['task', 'root']]) selections.change(key, value);
  let selection = new URL(selections.requests.at(-1).url, 'http://test').searchParams;
  for (const [key, value] of [['model', 'm'], ['route', 'r'], ['project', 'p'], ['task', 'root']]) assert.equal(selection.get(key), value);
  assert.equal(selection.get('scope'), 'all');
  for (const scope of ['descendants', 'all', 'own']) {
    selections.click({scope});
    assert.equal(new URL(selections.requests.at(-1).url, 'http://test').searchParams.get('scope'), scope);
  }
  selections.click({period: 'custom'}); selections.change('custom_start', '2026-11-01'); selections.change('custom_end', '2026-11-01'); selections.click({action: 'apply-calendar'});
  selection = new URL(selections.requests.at(-1).url, 'http://test').searchParams;
  assert.equal(selection.get('start'), '2026-11-01T04:00:00.000Z');
  assert.equal(selection.get('end'), '2026-11-02T05:00:00.000Z', 'fall-back calendar day spans 25 hours');
  const beforeInvalid = selections.requests.length;
  selections.change('custom_start', '2026-02-30'); selections.click({action: 'apply-calendar'});
  assert.equal(selections.requests.length, beforeInvalid, 'invalid calendar dates do not fetch');
  assert.match(selections.root.querySelector('.to-error').innerHTML, /valid calendar date/);
  selections.change('custom_start', '2026-11-02'); selections.change('custom_end', '2026-11-01'); selections.click({action: 'apply-calendar'});
  assert.equal(selections.requests.length, beforeInvalid, 'reversed local calendar does not fetch');
  selections.dispose();

  const overflow = harness(); overflow.respond(0, {}); await flush();
  const overflowSnapshot = data('beyond-chart-precision');
  const hugeDigits = '1' + '0'.repeat(400);
  overflowSnapshot.summary.metrics.prompt_tokens = {value: 'overflow-counter', exact: hugeDigits, known: 1, missing: 0};
  overflowSnapshot.summary.reported_tokens = 'overflow-counter';
  overflowSnapshot.summary.reported_tokens_exact = hugeDigits;
  overflow.respond(1, JSON.stringify(overflowSnapshot).replaceAll('"overflow-counter"', hugeDigits)); await flush();
  const overflowHTML = overflow.root.querySelector('.to-content').innerHTML;
  assert.match(overflowHTML, /exceed chart precision/);
  assert.doesNotMatch(overflowHTML, /NaN|Infinity/, 'extreme exact counters never produce invalid SVG or bar geometry');
  assert.ok(overflowHTML.includes('<td>' + new Intl.NumberFormat('en-US').format(BigInt(hugeDigits)) + '</td>'), 'exact table counters remain available beyond browser Number range');
  overflow.dispose();

  const latePreferences = harness();
  latePreferences.click({period: '7d'}); latePreferences.click({scope: 'descendants'}); latePreferences.click({view: 'sessions'});
  latePreferences.respond(0, {error: 'Preferences unreadable'}, 500); await flush();
  const afterPreferenceFailure = new URL(latePreferences.requests.at(-1).url, 'http://test').searchParams;
  assert.equal(afterPreferenceFailure.get('period'), '7d', 'failed startup restore must not overwrite an intervening user selection');
  assert.equal(afterPreferenceFailure.get('scope'), 'descendants');
  latePreferences.respond(latePreferences.requests.length - 1, sessionSnapshot); await flush();
  assert.match(latePreferences.root.querySelector('.to-content').innerHTML, /Harness sessions · token flow/);
  latePreferences.dispose();

  const invalid = harness(); invalid.respond(0, {error: 'Preferences unreadable', status: 500}); await flush();
  assert.match(invalid.root.querySelector('.to-pref-error').textContent, /Preferences unreadable/);
  invalid.dispose();
  console.log('Widget contract checks passed (mock DOM/fetch; no browser or Widgets acceptance claim).');
})().catch(error => { console.error(error); process.exitCode = 1; });
