/* Token Observatory · standalone classic Widgets module. No external assets. */
(function () {
  'use strict';
  var API = '/api/extensions/token-usage';
  // Every fetch is bounded. The module bridge has no default deadline, so a stalled
  // /data or /export would otherwise leave the status pinned at "Updating selection…"
  // until the reader disposed the card. A timeout is reported as its own retryable
  // error, never as the silent AbortError that disposal uses.
  var REQUEST_TIMEOUT_MS = 45000;
  var ROOT_ID = 'token-observatory';
  var prior = document.getElementById(ROOT_ID);
  if (prior && typeof prior.__dispose === 'function') prior.__dispose();
  var hostRoot = document.getElementById('root') || document.body || document.documentElement;
  var root = document.createElement('section');
  root.id = ROOT_ID;
  root.setAttribute('aria-label', 'Token Observatory');
  var state = {
    disposed: false, disposing: false, disposePromise: null, epoch: 0, controller: null, prefController: null, prefPromise: null, controllers: new Set(),
    timer: null, prefTimer: null, timers: new Set(), prefPending: null, prefSaving: false, data: null, signature: '', prefError: '',
    query: {period: 'all', model: '', route: '', project: '', task: '', scope: 'all', custom_start: '', custom_end: '', view: 'auto'},
    preferencesTouched: false, page: 1, expanded: new Set(), urls: new Set(), pending: false
  };
  var nf = new Intl.NumberFormat('en-US');
  var compact = new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 2});
  var money = new Intl.NumberFormat('en-US', {style: 'currency', currency: 'USD', maximumFractionDigits: 4});
  var zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'local time';
  function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function number(value, short) { return value == null ? 'Unknown' : !Number.isFinite(value) ? 'Very large' : (short ? compact : nf).format(value); }
  function exact(value, digits) { return typeof digits === 'string' && /^\d+$/.test(digits) ? digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',') : number(value); }
  function rowNumber(row, key) {
    if (row.kind === 'subscription_session' && row.session_token_exact) return exact(null, row.session_token_exact[key]);
    return exact(row[key], row.token_exact && row.token_exact[key]);
  }
  function dollars(value) { return value == null ? 'Unknown' : money.format(value); }
  function metric(summary, key) { return summary && summary.metrics && summary.metrics[key] || {value: null, known: 0, missing: 0}; }
  function report(summary) { return summary && summary.reported_tokens != null ? summary.reported_tokens : null; }
  function metricNote(m) { return m.missing ? number(m.known || 0) + ' reported · ' + number(m.missing) + ' unknown' : number(m.known || 0) + ' reported'; }
  function sumStatus(summary) { return summary && (metric(summary, 'prompt_tokens').missing || metric(summary, 'completion_tokens').missing) ? 'Partial reported sum' : 'Reported input + output'; }
  function info(text) { return '<button type="button" class="to-info" data-action="explain" data-explanation="' + esc(text) + '" aria-label="Explain this metric" title="' + esc(text) + '">i</button>'; }
  root.innerHTML = '<style>\n' +
    /* One palette, and it is the host's: docs/DESIGN.md names the roles, web/ui.css
   holds the values. A sandboxed module cannot load that stylesheet, so it mirrors
   the same values rather than inventing a second brand. */
    ':root{color-scheme:dark;--bg:#0d0b0f;--glass-fill:rgba(255,255,255,.05);--glass-edge:rgba(255,255,255,.10);--glass-edge-top:rgba(255,255,255,.16);--glass-blur:saturate(150%) blur(18px);--bg-inset:rgba(0,0,0,.30);--surface:var(--glass-fill);--surface-raised:rgba(255,255,255,.075);--line:rgba(255,255,255,.10);--muted:rgba(255,255,255,.68);--dim:rgba(255,255,255,.54);--text:#e2e8f0;--accent:#c93545;--accent-soft:#f07a86;--accent-dim:rgba(201,53,69,.15);--secondary:#7fb0f5;--secondary-core:#3b82f6;--amber:#fcd34d;--bad:#fca5a5;--row-h:32px;--radius-sm:8px;--radius-md:10px;--radius-lg:14px;--radius-pill:9999px}' +
    /* The frame paints its own ground. Every rule used to be scoped to the section,
       which left the iframe document at its default white and showed through as pale
       corners; and glass has nothing to blur over a bare canvas, so two faint accent
       pools give the blurred surfaces something to sit on. */
    'html{background:var(--bg)}body{margin:0;padding:0;color:var(--text);font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased;background:radial-gradient(900px 420px at 10% -10%,rgba(201,53,69,.16),transparent 60%),radial-gradient(700px 380px at 94% 2%,rgba(120,60,160,.12),transparent 62%),var(--bg);background-attachment:fixed}' +
    /* The declared frame height is fixed and the document scrolls inside it. Nothing
       here is measured in vh: sizing the page to the frame while the frame sizes to
       the page is the loop that chases itself two pixels at a time. */
    '#token-observatory{box-sizing:border-box;width:100%;min-width:0;display:block;isolation:isolate;font-size:14px}' +
    '#token-observatory *{box-sizing:border-box}#token-observatory button,#token-observatory select,#token-observatory input{font-family:inherit;font-size:13px;line-height:1.4;color:inherit}#token-observatory button{cursor:pointer}#token-observatory button:disabled{opacity:.4;cursor:default}#token-observatory :focus-visible{outline:2px solid var(--accent-soft);outline-offset:3px}#token-observatory button,#token-observatory select,#token-observatory input{min-height:var(--row-h);border:1px solid var(--glass-edge);border-radius:var(--radius-sm);background:var(--glass-fill)}#token-observatory button{padding:5px 11px}#token-observatory button:hover:enabled{background:rgba(255,255,255,.10);color:var(--text)}#token-observatory button[aria-pressed=true]{background:var(--accent-dim);border-color:rgba(201,53,69,.45);color:var(--accent-soft)}#token-observatory input,#token-observatory select{padding:6px 9px;max-width:100%;width:100%}#token-observatory option{background:#1a1520;color:#e2e8f0}' +
    /* The card already carries the widget's name, so the page inside it does not
   repeat it: the old 24px title, its 37px mark and a header with its own fill
   and border read as a second frame around a frame. What is left is one sticky
   control row — the shape the sibling widgets use — and the 24px is spent once,
   on the single number this dashboard is about. */
    '#token-observatory .to-header{position:sticky;top:0;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 14px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(13,11,15,.88),rgba(13,11,15,.66));backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur)}#token-observatory .to-brand{display:flex;gap:9px;align-items:center;min-width:0}#token-observatory .to-mark{width:20px;height:20px;flex:none;color:var(--accent-soft)}#token-observatory h1{position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip-path:inset(50%);white-space:nowrap}#token-observatory .to-eyebrow{color:var(--muted);font-size:12px;letter-spacing:0}#token-observatory .to-brand .to-eyebrow{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    /* Both actions close the row as one capsule: two lone circles at the edge read
       as leftovers, one capsule reads as a control. */
    '#token-observatory .to-actions{display:inline-flex;align-items:center;gap:2px;height:var(--row-h);flex:none;background:var(--bg-inset);padding:3px;border-radius:var(--radius-pill);border:1px solid var(--glass-edge);border-top-color:var(--glass-edge-top);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur)}#token-observatory .to-actions button{height:100%;min-height:0;padding:0 11px;border:1px solid transparent;border-radius:var(--radius-pill);background:transparent;color:var(--muted);font-size:12px}#token-observatory .to-actions button:hover:enabled{background:var(--glass-fill);color:var(--text)}#token-observatory .to-refresh{width:34px;padding:0;font-size:14px}#token-observatory .to-scroll{padding:10px 14px 16px}' +
    '#token-observatory .to-periods{display:flex;gap:5px;flex-wrap:wrap;align-items:center}#token-observatory .to-periods button{font-size:12px;min-width:42px}#token-observatory .to-periods .to-period-label{color:var(--muted);margin-right:8px;font-size:12px}#token-observatory .to-filters{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}#token-observatory .to-filter-actions{display:flex;justify-content:flex-end;margin-top:8px}#token-observatory .to-filter-actions button{font-size:12px;min-height:32px}#token-observatory label{font-size:12px;color:var(--muted);display:block}#token-observatory label select,#token-observatory label input{display:block;margin-top:5px;color:var(--text);font-size:13px}#token-observatory .to-calendar{display:flex;align-items:end;gap:10px;flex-wrap:wrap;padding-top:12px}#token-observatory .to-calendar label{flex:1;min-width:125px}#token-observatory .to-calendar-note{width:100%;font-size:12px;color:var(--muted)}#token-observatory .to-scope{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--muted)}#token-observatory .to-scope button{font-size:12px;min-height:32px;padding:5px 9px}#token-observatory .to-window{margin-top:10px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}' +
    '#token-observatory .to-message{margin-top:12px;border-radius:var(--radius-md);border:1px solid rgba(245,158,11,.30);background:rgba(245,158,11,.15);color:var(--amber);padding:10px 12px;font-size:12px}#token-observatory .to-calendar-note.error{color:var(--bad)}#token-observatory .to-message.error{border-color:rgba(239,68,68,.30);background:rgba(239,68,68,.15);color:var(--bad)}#token-observatory .to-message.neutral{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.12);color:var(--muted)}#token-observatory .to-message button{font-size:12px;min-height:30px;margin-left:10px}#token-observatory .to-message p{margin:0}#token-observatory .to-message p+p{margin-top:5px}#token-observatory [hidden]{display:none!important}#token-observatory .to-status{min-height:28px;padding:10px 0 0;display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:12px;color:var(--muted)}#token-observatory .to-status-mark{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent-soft);margin-right:6px}#token-observatory .to-status.loading .to-status-mark{background:var(--amber);animation:to-pulse 1s ease-in-out infinite alternate}#token-observatory .to-status.error .to-status-mark{background:var(--bad)}@keyframes to-pulse{from{opacity:.3}to{opacity:1}}' +
    '#token-observatory .to-topline{display:flex;justify-content:space-between;gap:14px;align-items:end;margin:16px 0 10px}#token-observatory h2{font-size:16px;font-weight:600;margin:0;letter-spacing:-.1px}#token-observatory .to-total{font-size:24px;font-weight:660;letter-spacing:-1.2px;line-height:1.15;font-variant-numeric:tabular-nums}#token-observatory .to-total small{font-size:12px;color:var(--muted);font-weight:400;letter-spacing:0;margin-left:8px}#token-observatory .to-coverage-pill{font-size:12px;border:1px solid var(--line);padding:4px 8px;border-radius:20px;color:var(--muted);white-space:nowrap}#token-observatory .to-coverage-pill.partial{color:var(--amber);border-color:rgba(245,158,11,.30);background:rgba(245,158,11,.12)}#token-observatory .to-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}#token-observatory .to-card,#token-observatory .to-panel{border:1px solid var(--glass-edge);border-top-color:var(--glass-edge-top);border-radius:var(--radius-lg);background:var(--glass-fill);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);min-width:0}#token-observatory .to-card{padding:10px 14px;position:relative;overflow:hidden}#token-observatory .to-card.input{background:linear-gradient(120deg,rgba(201,53,69,.20),rgba(255,255,255,.04))}#token-observatory .to-card.output{background:linear-gradient(120deg,rgba(59,130,246,.18),rgba(255,255,255,.04))}#token-observatory .to-card-title{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--muted)}#token-observatory .to-dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none}#token-observatory .input .to-dot{background:var(--accent-soft)}#token-observatory .output .to-dot{background:var(--secondary)}#token-observatory .to-value{font-size:16px;font-weight:600;line-height:1.3;letter-spacing:-.2px;margin:6px 0 3px;font-variant-numeric:tabular-nums}#token-observatory .input .to-value,#token-observatory .output .to-value{color:var(--text)}#token-observatory .to-caption{color:var(--muted);font-size:12px}#token-observatory .to-info{border:1px solid var(--glass-edge);background:var(--glass-fill);color:var(--dim);display:inline-flex;align-items:center;justify-content:center;border-radius:50%;font-size:12px;min-height:18px;width:18px;padding:0;margin-left:auto;flex:none}#token-observatory .to-cache{margin-top:10px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}#token-observatory .to-cache .to-card{padding:11px 13px}#token-observatory .to-cache .to-value{font-size:14px;margin:5px 0 2px;letter-spacing:0}#token-observatory .to-money-lines{font-size:12px;color:var(--muted);line-height:1.6}#token-observatory .to-money-lines b{font-weight:500;color:var(--text)}#token-observatory .to-note{font-size:12px;color:var(--muted);margin:9px 0 0;line-height:1.6}' +
    '#token-observatory .to-panel{padding:15px 17px;margin-top:12px}#token-observatory .to-panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:11px}#token-observatory .to-legend{display:flex;gap:11px;color:var(--muted);font-size:12px;flex-wrap:wrap}#token-observatory .to-legend span{display:flex;align-items:center;gap:5px}#token-observatory .to-legend i{width:13px;height:2px;display:inline-block;background:var(--accent-soft)}#token-observatory .to-legend span+span i{background:var(--secondary)}#token-observatory .to-chart{width:100%;height:auto;min-height:120px;max-height:150px;aspect-ratio:16/3;display:block;overflow:visible}#token-observatory .to-chart text{fill:var(--muted);font:12px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}#token-observatory .to-trend-ticks{display:flex;justify-content:space-between;color:var(--muted);font-size:12px;gap:10px;margin-top:2px}#token-observatory .to-rank-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}#token-observatory .to-rank{padding:0;border:0;background:none;text-align:left;width:100%;display:block;margin-top:11px;min-height:40px}#token-observatory .to-rank:hover:enabled{background:var(--glass-fill);border-radius:var(--radius-sm)}#token-observatory .to-rank-label{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:14px;margin-bottom:5px}#token-observatory .to-rank-label span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}#token-observatory .to-rank-label span:last-child{font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap}#token-observatory .to-track{height:4px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden;display:block}#token-observatory .to-fill{height:100%;display:block;border-radius:3px;background:linear-gradient(90deg,var(--accent),var(--accent-soft))}#token-observatory .to-route .to-fill{background:linear-gradient(90deg,var(--secondary-core),var(--secondary))}#token-observatory .to-small-link{font-size:12px;min-height:30px;padding:3px 7px;color:var(--muted)}#token-observatory .to-breakdown{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}#token-observatory .to-empty{padding:24px 16px;text-align:center;color:var(--muted);font-size:14px}#token-observatory .to-empty strong{display:block;color:var(--text);font-size:16px;margin-bottom:6px}#token-observatory .to-skeleton{height:100px;border-radius:var(--radius-lg);background:linear-gradient(100deg,rgba(255,255,255,.04),rgba(255,255,255,.075),rgba(255,255,255,.04));margin-top:12px}' +
    '#token-observatory .to-details summary{cursor:pointer;min-height:34px;font-weight:550;font-size:14px}#token-observatory table{width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed}#token-observatory th{text-align:right;font-size:12px;color:var(--muted);font-weight:500;padding:10px 5px;border-bottom:1px solid var(--line)}#token-observatory th:first-child{text-align:left;width:31%}#token-observatory td{text-align:right;vertical-align:top;padding:10px 5px;border-bottom:1px solid rgba(255,255,255,.07);font-variant-numeric:tabular-nums;overflow-wrap:anywhere}#token-observatory td:first-child{text-align:left}#token-observatory .to-table-label{font-weight:500;color:var(--text)}#token-observatory .to-table-meta{font-size:12px;color:var(--muted);margin-top:3px}#token-observatory .to-row-details{margin-top:4px;font-size:12px;color:var(--muted)}#token-observatory .to-row-details summary{font-size:12px;min-height:24px;font-weight:400}#token-observatory .to-row-details p{margin:4px 0;overflow-wrap:anywhere}#token-observatory .to-pagination{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:12px;font-size:12px;color:var(--muted)}#token-observatory .to-pagination div{display:flex;gap:6px}#token-observatory .to-pagination button{font-size:12px;min-height:32px}#token-observatory .to-footnote{margin:16px 1px 0;color:var(--muted);font-size:12px;line-height:1.7}#token-observatory .to-dim{color:var(--muted)}' +
    '@media(max-width:620px){#token-observatory .to-header{padding:9px 12px}#token-observatory .to-scroll{padding:10px 12px 16px}#token-observatory .to-eyebrow{font-size:12px;letter-spacing:0}#token-observatory .to-filters{grid-template-columns:repeat(2,minmax(0,1fr))}#token-observatory .to-total{font-size:24px}#token-observatory .to-total small{display:block;margin:4px 0 0}#token-observatory .to-card{padding:13px}#token-observatory .to-cache{grid-template-columns:repeat(2,minmax(0,1fr))}#token-observatory .to-cache .to-card:last-child{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:0 12px}#token-observatory .to-cache .to-card:last-child .to-card-title{grid-column:1/-1}#token-observatory .to-value{font-size:16px}#token-observatory .to-panel{padding:13px}#token-observatory .to-rank-grid,#token-observatory .to-breakdown{grid-template-columns:1fr}#token-observatory .to-panel-head{align-items:start}#token-observatory .to-export-label{display:none}#token-observatory .to-cache-column{display:none}#token-observatory th:first-child{width:48%}#token-observatory .to-status{align-items:start}#token-observatory .to-status time{text-align:right}}' +
    '#token-observatory .to-view{display:flex;align-items:center;gap:6px;margin-top:8px;flex-wrap:wrap}#token-observatory .to-view button{min-height:30px;font-size:12px;padding:4px 9px}#token-observatory .to-view .to-caption{margin-left:auto}#token-observatory .to-coverage details{padding:6px 10px;font-size:12px;margin-top:7px}#token-observatory .to-coverage summary{cursor:pointer;min-height:24px;display:list-item}#token-observatory .to-coverage details p{font-size:12px;margin:5px 0}#token-observatory .to-topline .to-caption{margin-top:3px}#token-observatory .to-flow{margin-top:10px;padding:12px 14px}#token-observatory .to-flow .to-panel-head{margin-bottom:7px}#token-observatory .to-flow .to-chart{min-height:110px;max-height:136px}#token-observatory .to-normalized{margin-top:8px}#token-observatory .to-history-boundary{color:var(--muted);font-size:12px;overflow-wrap:anywhere}#token-observatory .to-view button:focus-visible{outline-offset:1px}@media(max-width:620px){#token-observatory .to-view .to-caption{width:100%;margin:0}#token-observatory .to-header{padding:12px}#token-observatory .to-scroll{padding:10px 12px 18px}}' +
/* The one remaining piece of foreign chrome was the native select: four OS chevrons
   in default field trim, the only control on the card that did not belong to the
   palette. The arrow is an inline data: image, which the frame's img-src allows. */
    '#token-observatory select{appearance:none;-webkit-appearance:none;padding-right:26px;background-image:url("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27%20viewBox%3D%270%200%2010%206%27%3E%3Cpath%20d%3D%27M1%201l4%204%204-4%27%20fill%3D%27none%27%20stroke%3D%27rgba(255%2C255%2C255%2C.68)%27%20stroke-width%3D%271.4%27%20stroke-linecap%3D%27round%27%2F%3E%3C%2Fsvg%3E");background-repeat:no-repeat;background-position:right 10px center;background-size:10px 6px}' +
    '@media(prefers-reduced-motion:reduce){#token-observatory *{animation:none!important;scroll-behavior:auto!important}}' +
    '</style>' +
    '<header class="to-header"><div class="to-brand"><svg class="to-mark" viewBox="0 0 40 40" fill="none" aria-hidden="true"><circle cx="20" cy="20" r="15" stroke="currentColor" stroke-opacity=".2" stroke-width="1.5"/><path d="M32.5 11.8A15 15 0 1 1 19 5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="m29 5 6 6-8 1" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="20" cy="20" r="5" fill="currentColor" fill-opacity=".12"/><circle cx="20" cy="20" r="2" fill="currentColor"/></svg><div><div class="to-eyebrow">Ouroboros · recorded usage</div><h1>Token Observatory</h1></div></div><div class="to-actions"><button type="button" data-action="export" title="Download a frozen numeric snapshot" aria-label="Download a frozen numeric snapshot">↓ <span class="to-export-label">Snapshot</span></button><button type="button" class="to-refresh" data-action="refresh" title="Refresh data" aria-label="Refresh data">↻</button></div></header>' +
    '<main class="to-scroll" tabindex="0" aria-label="Token usage dashboard. Scroll for charts and details."><div class="to-periods" role="group" aria-label="History period"><span class="to-period-label">History</span>' +
    ['1h', '24h', '7d', '30d', 'all', 'custom'].map(function (p) { return '<button type="button" data-period="' + p + '" aria-pressed="' + (p === 'all') + '">' + ({all: 'All history', custom: 'Calendar'}[p] || p) + '</button>'; }).join('') +
    '</div><div class="to-calendar" hidden><label>From · local date<input type="date" data-pref="custom_start" aria-label="Calendar start date, local time"></label><label>Through · local date<input type="date" data-pref="custom_end" aria-label="Calendar end date, inclusive local date"></label><button type="button" data-action="apply-calendar">Apply dates</button><div class="to-calendar-note" aria-live="polite">Calendar uses ' + esc(zone) + '. Start is inclusive; the following midnight is the exclusive end in UTC.</div></div>' +
    '<div class="to-filters">' + [['model', 'Recorded model'], ['route', 'Recorded harness / route'], ['project', 'Project'], ['task', 'Root / selected task']].map(function (x) { return '<label>' + x[1] + '<select data-pref="' + x[0] + '" aria-label="' + x[1] + '"><option value="">All ' + ({model: 'models', route: 'routes', project: 'projects', task: 'tasks'}[x[0]]) + '</option></select></label>'; }).join('') + '</div><div class="to-filter-actions"><button type="button" data-action="clear" disabled>Clear filters</button></div>' +
    '<div class="to-scope" hidden><span>Selected task</span><button type="button" data-scope="own" aria-pressed="false">Own usage</button><button type="button" data-scope="descendants" aria-pressed="false">Sum of descendants</button><button type="button" data-scope="all" aria-pressed="true">Own + descendants</button>' + info('Own usage includes records assigned directly to the selected task. Sum of descendants includes child usage attributed to that exact root identity, excluding its own records. Session aggregates are kept separate from physical calls. Attribution gaps remain explicit.') + '</div>' +
    '<div class="to-window"></div><div class="to-message neutral to-explanation" role="status" hidden></div><div class="to-message error to-error" role="alert" hidden></div><div class="to-message to-pref-error" role="alert" hidden></div><div class="to-status loading" role="status" aria-live="polite"><span><i class="to-status-mark"></i><span class="to-status-text">Loading retained history…</span></span><time></time></div><div class="to-coverage"></div><div class="to-content" aria-busy="true"><div class="to-skeleton"></div><div class="to-skeleton"></div></div></main>';
  hostRoot.appendChild(root);
  var $ = function (s) { return root.querySelector(s); };
  var content = $('.to-content');
  function setStatus(text, kind) { $('.to-status').className = 'to-status ' + (kind || ''); $('.to-status-text').textContent = text; }
  function error(message, withRetry) { var el = $('.to-error'); el.hidden = false; el.innerHTML = '<p>' + esc(message) + (withRetry === false ? '' : '<button type="button" data-action="refresh">Retry</button>') + '</p>'; setStatus('Update failed · last successful data stays visible', 'error'); }
  function calendarError(message) { var note = $('.to-calendar-note'); note.className = 'to-calendar-note error'; note.textContent = message; var el = $('.to-error'); el.hidden = true; el.innerHTML = '<p>' + esc(message) + '</p>'; setStatus('Choose valid calendar dates', 'error'); }
  function clearCalendarError() { var note = $('.to-calendar-note'); note.className = 'to-calendar-note'; note.textContent = 'Calendar uses ' + zone + '. Start is inclusive; the following midnight is the exclusive end in UTC.'; }
  function validateCalendar() { try { var start = dateUTC(state.query.custom_start, false), end = dateUTC(state.query.custom_end, true); if (start >= end) throw new Error('The end date must be on or after the start date.'); clearCalendarError(); return true; } catch (e) { calendarError(e.message); return false; } }
  function prefError(message) { state.prefError = message; $('.to-pref-error').hidden = !message; $('.to-pref-error').textContent = message; }
  function dateUTC(value, advance) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) throw new Error('Choose both calendar dates.');
    var parts = value.split('-').map(Number);
    var date = new Date(parts[0], parts[1] - 1, parts[2]);
    if (date.getFullYear() !== parts[0] || date.getMonth() !== parts[1] - 1 || date.getDate() !== parts[2]) throw new Error('Choose a valid calendar date.');
    if (advance) date.setDate(date.getDate() + 1);
    return date.toISOString();
  }
  function queryParams() {
    var params = new URLSearchParams();
    ['period', 'model', 'route', 'project', 'task', 'scope'].forEach(function (key) { if (state.query[key]) params.set(key, state.query[key]); });
    if (state.query.period === 'custom') {
      var start = dateUTC(state.query.custom_start, false), end = dateUTC(state.query.custom_end, true);
      if (start >= end) throw new Error('The end date must be on or after the start date.');
      params.set('start', start); params.set('end', end);
    }
    params.set('page', String(state.page)); params.set('page_size', '15');
    return params;
  }
  function syncControls() {
    root.querySelectorAll('[data-period]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.period === state.query.period)); });
    $('.to-calendar').hidden = state.query.period !== 'custom';
    if (state.query.period !== 'custom') clearCalendarError();
    root.querySelectorAll('[data-pref]').forEach(function (el) { var value = state.query[el.dataset.pref] || ''; if (el.value !== value) el.value = value; });
    $('.to-scope').hidden = !state.query.task;
    root.querySelectorAll('[data-scope]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.scope === state.query.scope)); });
    var clear = $('[data-action="clear"]');
    if (clear) clear.disabled = !state.query.model && !state.query.route && !state.query.project && !state.query.task && state.query.scope === 'all';
    var message = state.query.period === 'all' ? 'All retained history · undated records included and disclosed' : 'Rolling window · UTC start inclusive, end exclusive';
    if (state.query.period === 'custom') {
      try { message = dateUTC(state.query.custom_start, false) + ' ≤ timestamp < ' + dateUTC(state.query.custom_end, true) + ' · from local calendar'; } catch (e) { message = 'Choose calendar dates, then apply.'; }
    }
    $('.to-window').textContent = message;
  }
  function updateFacets(facets) {
    [['model', 'models'], ['route', 'routes'], ['project', 'projects'], ['task', 'tasks']].forEach(function (pair) {
      var select = $('[data-pref="' + pair[0] + '"]');
      var items = facets && facets[pair[1]] || [];
      var values = items.map(function (item) { return {id: String(item.id == null ? '' : item.id), label: String(item.label == null ? item.id : item.label)}; });
      var chosen = state.query[pair[0]] || '';
      if (chosen && !values.some(function (v) { return v.id === chosen; })) values.push({id: chosen, label: chosen + ' · selected'});
      var signature = JSON.stringify(values);
      if (select.__options === signature) return;
      select.__options = signature;
      select.innerHTML = '<option value="">All ' + pair[1] + '</option>' + values.map(function (item) { return '<option value="' + esc(item.id) + '">' + esc(item.label) + '</option>'; }).join('');
      select.value = chosen;
    });
  }
  async function request(path, options, controller) {
    state.controllers.add(controller);
    var timedOut = false;
    // No deadline while disposing: the host already bounds its Stop handshake, and a
    // deadline armed for the drained preference PUT would outlive the widget — exactly
    // the outstanding timer the widget-contract harness asserts against.
    var timer = state.disposing ? null : setTimeout(function () { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    if (timer) state.timers.add(timer);
    try {
      var response = await fetch(API + path, Object.assign({credentials: 'same-origin', signal: controller.signal, cache: 'no-store', timeoutMs: REQUEST_TIMEOUT_MS}, options || {}));
      var text = await response.text(), body;
      try { body = text ? JSON.parse(text) : {}; } catch (e) { throw new Error('The widget endpoint returned an unreadable response (' + response.status + ').'); }
      if (!response.ok || body.error || body.status >= 400) throw new Error(typeof body.error === 'string' ? body.error : body.detail || 'Request failed (' + response.status + ').');
      return body;
    } catch (e) {
      // A timeout must not read as disposal: callers stay silent on AbortError.
      if (timedOut) throw new Error('The widget endpoint did not answer within ' + Math.round(REQUEST_TIMEOUT_MS / 1000) + ' seconds.');
      throw e;
    } finally { if (timer) { clearTimeout(timer); state.timers.delete(timer); } state.controllers.delete(controller); }
  }
  function saveNextPreference() {
    if (state.disposed || state.prefSaving || state.prefPending === null) return state.prefPromise || Promise.resolve();
    var body = state.prefPending; state.prefPending = null; state.prefSaving = true;
    var controller = new AbortController(); state.prefController = controller;
    var operation = (async function () {
      try {
        await request('/preferences', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: body}, controller);
        if (!state.disposed && !state.disposing) prefError('');
      } catch (e) {
        if (!state.disposed && !state.disposing && e.name !== 'AbortError') prefError('Filters are active, but preferences could not be saved: ' + e.message);
      }
    })();
    state.prefPromise = operation;
    operation.then(function () {
      state.prefSaving = false; state.prefController = null;
      if (!state.disposed && state.prefPending !== null) {
        if (state.prefPromise === operation) state.prefPromise = null;
        saveNextPreference();
      } else if (state.prefPromise === operation) state.prefPromise = null;
    }, function () {
      // The operation handles request failures; keep the disposer promise safe even if a host fetch shim rejects unusually.
      state.prefSaving = false; state.prefController = null;
      if (!state.disposed && state.prefPending !== null) {
        if (state.prefPromise === operation) state.prefPromise = null;
        saveNextPreference();
      } else if (state.prefPromise === operation) state.prefPromise = null;
    });
    return operation;
  }
  function remember() {
    clearTimeout(state.prefTimer);
    state.prefTimer = setTimeout(function () {
      if (state.disposed) return;
      // Serial writes matter: aborting a request does not cancel a server write.
      state.prefPending = JSON.stringify(state.query);
      saveNextPreference();
    }, 250);
  }
  function schedule(delay) {
    clearTimeout(state.timer);
    if (!state.disposed) state.timer = setTimeout(function () { if (!document.hidden) refresh(false); else schedule(30000); }, delay || 30000);
  }
  async function refresh(userInitiated) {
    if (state.disposed) return;
    clearTimeout(state.timer);
    var params;
    try { params = queryParams(); } catch (e) { if (state.query.period === 'custom') calendarError(e.message); else error(e.message); return; }
    if (state.controller) state.controller.abort();
    var controller = new AbortController(); state.controller = controller;
    var epoch = ++state.epoch;
    state.pending = true; content.setAttribute('aria-busy', 'true');
    if (userInitiated || !state.data) setStatus(state.data ? 'Updating selection…' : 'Reading retained history…', 'loading');
    try {
      var data = await request('/data?' + params.toString(), null, controller);
      if (state.disposed || epoch !== state.epoch) return;
      if (!data || !data.summary || !data.details || !data.coverage) throw new Error('The widget received an incomplete dashboard response.');
      if (data.coverage.status === 'error') throw new Error((data.coverage.issues || ['The retained source is unavailable.']).map(function (issue) { return typeof issue === 'string' ? issue : issue.message || issue.code; }).join(' '));
      $('.to-error').hidden = true;
      var loadingSource = ['loading', 'indexing', 'catching_up'].indexOf(data.coverage.status) !== -1;
      if (loadingSource) {
        setStatus('Catching up with retained history…', 'loading');
        $('.to-coverage').innerHTML = '<div class="to-message neutral">Initial catch-up is running. Totals will appear when the retained numeric history is indexed.</div>';
        if (!state.data) content.innerHTML = '<div class="to-skeleton"></div><div class="to-skeleton"></div>';
        schedule(3000); return;
      }
      state.data = data;
      updateFacets(data.facets);
      var displayCoverage = {status: data.coverage.status, issues: data.coverage.issues, project_gaps: data.coverage.project_gaps, project_conflicts: data.coverage.project_conflicts};
      var signature = JSON.stringify([data.snapshot_id, params.toString(), displayCoverage, data.summary, data.trend, data.session_trend, data.rankings, data.session_rankings, data.details]);
      if (signature !== state.signature) { render(data); state.signature = signature; }
      var catchingUp = ['loading', 'indexing', 'catching_up'].indexOf(data.coverage.status) !== -1;
      setStatus(catchingUp ? 'Catching up with retained history…' : 'Snapshot ' + String(data.snapshot_id || 'unidentified').slice(0, 12) + ' · automatic refresh', catchingUp ? 'loading' : '');
      var timestamp = new Date(data.generated_at);
      if (!isNaN(timestamp.getTime())) { $('.to-status time').textContent = timestamp.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); $('.to-status time').dateTime = timestamp.toISOString(); }
      schedule(catchingUp ? 3000 : 30000);
    } catch (e) {
      if (state.disposed || epoch !== state.epoch || e.name === 'AbortError') return;
      error(e.message);
      if (!state.data) content.innerHTML = '<div class="to-empty"><strong>History could not be loaded</strong>The source may be unavailable or unreadable. Retry to check again.</div>';
      schedule(30000);
    } finally {
      if (!state.disposed && epoch === state.epoch) { state.pending = !!loadingSource; content.setAttribute('aria-busy', String(!!loadingSource)); }
    }
  }
  function card(title, key, summary, cls, description) {
    var m = metric(summary, key);
    return '<div class="to-card ' + cls + '"><div class="to-card-title"><span class="to-dot"></span>' + title + info(description) + '</div><div class="to-value" title="' + esc(exact(m.value, m.exact)) + '">' + esc(number(m.value, true)) + '</div><div class="to-caption">' + esc(metricNote(m)) + '</div></div>';
  }
  function trendChart(trend) {
    if (!trend || !trend.length) return '<div class="to-empty">No dated records in this selection.</div>';
    var keys = ['prompt_tokens', 'completion_tokens'];
    var max = 0, anyKnown = false;
    trend.forEach(function (point) { keys.forEach(function (key) { var value = metric(point.summary, key).value; if (value != null) { anyKnown = true; max = Math.max(max, value); } }); });
    if (!anyKnown) return '<div class="to-empty">Token values were not reported for these time buckets. Unknown values are not plotted as zero.</div>';
    if (!Number.isFinite(max)) return '<div class="to-empty">These counters exceed chart precision. Read the exact values in the table below.</div>';
    var width = 800, height = 150, left = 44, right = 9, top = 8, bottom = 20, plot = height - top - bottom;
    var x = function (i) { return left + (trend.length === 1 ? .5 : i / (trend.length - 1)) * (width - left - right); };
    var y = function (v) { return top + plot - (max ? v / max : 0) * plot; };
    var svg = '<svg class="to-chart" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Recorded input and generated output over time. Gaps mean unreported values. Exact values are in the trend table below.">';
    [0, .5, 1].forEach(function (fraction) { var yy = top + plot * (1 - fraction); svg += '<line x1="' + left + '" x2="' + (width - right) + '" y1="' + yy + '" y2="' + yy + '" stroke="rgba(255,255,255,.10)" stroke-dasharray="3 5"/><text x="0" y="' + (yy + 3) + '">' + esc(number(max * fraction, true)) + '</text>'; });
    keys.forEach(function (key, index) {
      var color = index ? '#7fb0f5' : '#f07a86', segments = [], current = [];
      trend.forEach(function (point, i) {
        var value = metric(point.summary, key).value;
        if (value == null) { if (current.length) segments.push(current); current = []; return; }
        current.push([x(i), y(value), point, value]);
      });
      if (current.length) segments.push(current);
      segments.forEach(function (segment) {
        var path = segment.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(2) + ',' + p[1].toFixed(2); }).join(' ');
        if (segment.length > 1) {
          svg += '<path d="' + path + ' L' + segment[segment.length - 1][0] + ',' + (top + plot) + ' L' + segment[0][0] + ',' + (top + plot) + ' Z" fill="' + color + '" fill-opacity=".045"/>';
          svg += '<path d="' + path + '" fill="none" stroke="' + color + '" stroke-width="2.3" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>';
        }
        segment.forEach(function (p) { svg += '<circle cx="' + p[0] + '" cy="' + p[1] + '" r="' + (trend.length < 12 ? '3' : '1.7') + '" fill="' + color + '"><title>' + esc((p[2].label || p[2].start) + ' · ' + (index ? 'Generated output' : 'Recorded input') + ': ' + exact(p[3], metric(p[2].summary, key).exact)) + '</title></circle>'; });
      });
    });
    svg += '</svg><div class="to-trend-ticks"><span>' + esc(trend[0].label || trend[0].start) + '</span><span>' + esc(trend[trend.length - 1].label || trend[trend.length - 1].start) + '</span></div>';
    return svg;
  }
  function ranking(items, dimension, title, cls) {
    items = items || [];
    var max = items.reduce(function (m, item) { return Math.max(m, report(item.summary) || 0); }, 0);
    return '<section class="to-panel ' + (cls || '') + '"><div class="to-panel-head"><h2>' + title + '</h2><span class="to-caption">Reported tokens</span></div>' +
      (items.length ? items.slice(0, 5).map(function (item) { var value = report(item.summary), partial = sumStatus(item.summary) === 'Partial reported sum'; return '<button type="button" class="to-rank" data-dimension="' + dimension + '" data-value="' + esc(item.id) + '" title="Filter by ' + esc((item.label || item.id) + ' · ' + exact(value, item.summary && item.summary.reported_tokens_exact) + ' reported tokens') + '"><span class="to-rank-label"><span>' + esc(item.label || item.id || 'Unknown') + '</span><span>' + esc(number(value, true)) + (partial ? ' · partial' : '') + '</span></span><span class="to-track"' + (!Number.isFinite(max) ? ' hidden' : '') + '><span class="to-fill" style="width:' + (value == null || !max ? 0 : !Number.isFinite(max) ? (Number.isFinite(value) ? 0 : 100) : Math.max(.5, value / max * 100)).toFixed(2) + '%"></span></span></button>'; }).join('') : '<div class="to-empty">No records in this selection.</div>') +
      (items.length > 5 ? '<p class="to-note">Top 5 of ' + number(items.length) + '. Choose any item in the filter above.</p>' : '') + '</section>';
  }
  function readableTrend(trend) {
    return '<details class="to-details" data-preserve="trend-table"><summary>Read the chart as a table</summary><table><caption class="to-note">Exact reported values · UTC half-open buckets</caption><thead><tr><th scope="col">UTC period</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Unknown fields<br>input / output</th></tr></thead><tbody>' + (trend || []).map(function (p) { var input = metric(p.summary, 'prompt_tokens'), output = metric(p.summary, 'completion_tokens'); return '<tr><td><span title="' + esc(p.start + ' ≤ timestamp < ' + p.end) + '">' + esc(p.label || p.start) + '</span></td><td>' + esc(exact(input.value, input.exact)) + '</td><td>' + esc(exact(output.value, output.exact)) + '</td><td>' + number(input.missing || 0) + ' / ' + number(output.missing || 0) + '</td></tr>'; }).join('') + '</tbody></table></details>';
  }
  function rowDetails(row) {
    var fields = [['Attempt', row.attempt_id], ['Kind / state', [row.kind, row.state].filter(Boolean).join(' / ')], ['UTC timestamp', row.ts || 'Undated'], ['Recorded harness / route', row.subscription_route || 'Unknown'], ['Task', row.task_id || 'Unassigned'], ['Root task', row.root_task_id || 'Unassigned'], ['Project', row.project_name || row.project_id || 'Unassigned'], ['Project attribution', row.project_assignment || 'Not separately reported'], ['Cache read · non-additive', rowNumber(row, 'cached_tokens')], ['Cache write · non-additive', rowNumber(row, 'cache_write_tokens')], ['Recorded cost · ' + (row.cost_final === true ? 'confirmed' : row.cost_final === false ? 'estimated' : 'finality unknown'), dollars(row.cost_usd)], ['Reservation upper bound · not spending', dollars(row.reservation_upper_bound_usd)], ['Recorded provider', row.provider || 'Unknown'], ['Source', row.source || 'Unknown']];
    if (row.kind === 'subscription_session') {
      var normalized = row.input_token_usage_exact;
      fields.push(['Input semantics', normalized ? 'Normalized input object; caches are non-additive' : 'Legacy fields; normalized object absent or invalid']);
      ['total_tokens', 'cache_read_tokens', 'cache_write_tokens'].forEach(function (key) { fields.push(['Normalized ' + key.replace(/_/g, ' '), exact(null, normalized && normalized[key])]); });
      fields.push(['Legacy input · comparison only', exact(row.prompt_tokens, row.token_exact && row.token_exact.prompt_tokens)]);
      fields.push(['Legacy cache read · comparison only', exact(row.cached_tokens, row.token_exact && row.token_exact.cached_tokens)]);
      fields.push(['Legacy cache write · comparison only', exact(row.cache_write_tokens, row.token_exact && row.token_exact.cache_write_tokens)]);
    }
    return '<details class="to-row-details" data-preserve="record-' + esc(row.attempt_id || '') + '"><summary>Record details</summary>' + fields.map(function (f) { return '<p><strong>' + esc(f[0]) + ':</strong> ' + esc(f[1]) + '</p>'; }).join('') + '</details>';
  }
  function detailTable(details) {
    var rows = details.rows || [], page = details.page || 1, size = details.page_size || 15, total = details.total || 0;
    return '<section class="to-panel"><details class="to-details" data-preserve="records"><summary>Explore all retained record types <span class="to-dim">· ' + number(total) + ' in this selection</span></summary><p class="to-note">This table includes both accounting views and excluded record types. Each row is the latest retained state of one record. Session aggregates are labelled separately. Filters apply to the complete selection before pagination.</p><table><thead><tr><th scope="col">Recorded model / task</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col" class="to-cache-column">Cache read*</th><th scope="col" class="to-cache-column">Cache write*</th></tr></thead><tbody>' + rows.map(function (row) { return '<tr><td><div class="to-table-label">' + esc(row.model || 'Unknown model') + '</div><div class="to-table-meta">' + esc(row.task_id ? String(row.task_id).slice(0, 14) : 'Unassigned task') + ' · ' + esc(row.kind === 'subscription_session' ? 'Session aggregate' : row.kind || 'Unknown kind') + '</div>' + rowDetails(row) + '</td><td>' + esc(rowNumber(row, 'prompt_tokens')) + '</td><td>' + esc(rowNumber(row, 'completion_tokens')) + '</td><td class="to-cache-column">' + esc(rowNumber(row, 'cached_tokens')) + '</td><td class="to-cache-column">' + esc(rowNumber(row, 'cache_write_tokens')) + '</td></tr>'; }).join('') + '</tbody></table>' + (!rows.length ? '<div class="to-empty">No records match these filters.</div>' : '') + '<div class="to-pagination"><span>' + (total ? number((page - 1) * size + 1) + '–' + number(Math.min(page * size, total)) + ' of ' + number(total) : '0 records') + '</span><div><button type="button" data-action="previous" data-focus="previous" ' + (page <= 1 ? 'disabled' : '') + '>Previous</button><button type="button" data-action="next" data-focus="next" ' + (page * size >= total ? 'disabled' : '') + '>Next</button></div></div><p class="to-note">* Cache counts are non-additive. On narrow cards, open Record details to read them.</p></details></section>';
  }
  function selectedView(data) {
    if (state.query.view !== 'auto') return state.query.view;
    return data.summary.subscription_sessions && (!data.summary.physical_calls || report(data.summary) == null) ? 'sessions' : 'physical';
  }
  function metricsTable(items, caption) {
    return '<table><caption class="to-note">' + esc(caption) + '</caption><thead><tr><th scope="col">Field</th><th scope="col">Exact reported value</th><th scope="col">Known / unknown</th></tr></thead><tbody>' + items.map(function (pair) {
      var m = pair[1] || {value: null, known: 0, missing: 0};
      return '<tr><td>' + esc(pair[0]) + '</td><td>' + esc(exact(m.value, m.exact)) + '</td><td>' + number(m.known) + ' / ' + number(m.missing) + '</td></tr>';
    }).join('') + '</tbody></table>';
  }
  function render(data) {
    var active = document.activeElement, focus = active && active.dataset && active.dataset.focus, focusPref = active && active.dataset && active.dataset.pref;
    content.querySelectorAll('details[data-preserve]').forEach(function (el) { if (el.open) state.expanded.add(el.dataset.preserve); else state.expanded.delete(el.dataset.preserve); });
    var physical = data.summary, coverage = data.coverage, issues = coverage.issues || [];
    var view = selectedView(data), sessionView = view === 'sessions';
    var summary = sessionView ? physical.subscription_summary || {rows: 0} : physical;
    var series = sessionView ? data.session_trend : data.trend;
    var rankings = (sessionView ? data.session_rankings : data.rankings) || {};
    if (coverage.project_gaps) issues = issues.concat(['Across retained history: ' + number(coverage.project_gaps) + ' records have gaps in project attribution.']);
    if (coverage.project_conflicts) issues = issues.concat(['Across retained history: ' + number(coverage.project_conflicts) + ' records have conflicting project attribution.']);
    issues = issues.map(function (issue) { return typeof issue === 'string' ? issue : issue.message || issue.code || JSON.stringify(issue); }).filter(function (item, i, list) { return item && list.indexOf(item) === i; });
    var oldCoverage = $('.to-coverage').querySelector('details');
    var coverageOpen = oldCoverage && oldCoverage.open;
    $('.to-coverage').innerHTML = issues.length ? '<details class="to-message"' + (coverageOpen ? ' open' : '') + '><summary><strong>Coverage notes</strong> · ' + number(issues.length) + ' · ' + (coverage.history_complete === false ? 'history incomplete' : 'attribution or field gaps') + '</summary>' + issues.map(function (issue) { return '<p>' + esc(issue) + '</p>'; }).join('') + '</details>' : '';
    var isPartial = coverage.status !== 'complete' && coverage.status !== 'ready' || sumStatus(summary) === 'Partial reported sum';
    var viewLabel = sessionView ? 'Harness sessions' : 'Physical calls';
    var html = '<div class="to-view" role="group" aria-label="Separate accounting views"><button type="button" data-view="physical" data-focus="view-physical" aria-pressed="' + !sessionView + '">Physical calls · ' + number(physical.physical_calls || 0) + '</button><button type="button" data-view="sessions" data-focus="view-sessions" aria-pressed="' + sessionView + '">Harness sessions · ' + number(physical.subscription_sessions || 0) + '</button><span class="to-caption">Separate totals · never added together</span></div>';
    html += '<div class="to-topline"><div><div class="to-eyebrow">' + viewLabel + ' · ' + esc(sumStatus(summary)) + '</div><div class="to-total" title="' + esc(exact(report(summary), summary.reported_tokens_exact)) + '">' + esc(number(report(summary), true)) + '<small>tokens reported</small></div></div><span class="to-coverage-pill ' + (isPartial ? 'partial' : '') + '">' + (isPartial ? 'Partial coverage' : 'Retained source indexed') + '</span></div>';
    html += '<div class="to-cards">' + card('Recorded input', 'prompt_tokens', summary, 'input', sessionView ? 'Uses normalized total input when the complete normalized object is valid, including recorded cache observations without adding them. A null normalized field stays unknown. Legacy input is used only for sessions without a valid normalized object.' : 'The recorded prompt_tokens field. Historical sources may report input differently; cache is non-additive. This is not a count of unique documents or uncached reads.') + card('Generated output', 'completion_tokens', summary, 'output', 'The recorded completion_tokens field. Reasoning is not separately reported. Missing counts remain unknown.') + '</div>';
    if (sessionView) {
      var presence = summary.normalized_input_usage || {known: 0, missing: summary.rows || 0};
      html += '<p class="to-note">Normalized input: ' + number(presence.known) + ' valid objects · ' + number(presence.missing) + ' absent or invalid. ' + number(summary.legacy_input_sessions || 0) + ' sessions use legacy input semantics.</p>';
    }
    if (!data.details.total) html += '<div class="to-empty"><strong>No records in this selection</strong>Try another period or clear a filter.</div>';
    else if (!(sessionView ? physical.subscription_sessions : physical.physical_calls)) html += '<p class="to-note">No ' + (sessionView ? 'harness sessions' : 'physical calls') + ' in this selection. Choose the other accounting view or explore retained records below.</p>';
    html += '<section class="to-panel to-flow"><div class="to-panel-head"><div><h2>' + viewLabel + ' · token flow</h2><div class="to-caption">Recorded fields over time · gaps remain unknown</div></div><div class="to-legend"><span><i></i>Input</span><span><i></i>Output</span></div></div>' + trendChart(series) + '<div style="margin-top:8px">' + readableTrend(series) + '</div></section>';
    html += '<div class="to-cache">' + card('Cache read', 'cached_tokens', summary, '', 'Non-additive recorded cache read. Sessions prefer a valid normalized object; null stays unknown. Never add to input.') + card('Cache write', 'cache_write_tokens', summary, '', 'Non-additive recorded cache write. Never add to input or cache read.') + '<div class="to-card"><div class="to-card-title">Recorded cost · physical calls' + info('Costs cover the physical-call selection in either view. Confirmed, estimated and held amounts are separate; held is not spending. Session prices and savings are not inferred.') + '</div><div><div class="to-value">' + esc(dollars(metric(physical, 'cost_confirmed_usd').value)) + '</div><div class="to-caption">Confirmed · ' + esc(metricNote(metric(physical, 'cost_confirmed_usd'))) + '</div></div><div class="to-money-lines">Estimated <b>' + esc(dollars(metric(physical, 'cost_estimated_usd').value)) + '</b><br>Held · active calls <b>' + esc(dollars(metric(physical, 'cost_held_usd').value)) + '</b><br>' + number(physical.unknown_cost_rows || 0) + ' unknown cost records</div></div></div>';
    if (physical.reservations) html += '<p class="to-note">Reserved, not dispatched: ' + number(physical.reservations) + ' records · ' + esc(dollars(physical.reserved_hold_usd && physical.reserved_hold_usd.value)) + ' held. Separate from calls and spending.</p>';
    html += '<div class="to-rank-grid">' + ranking(rankings.models, 'model', viewLabel + ' · recorded models', '') + ranking(rankings.routes, 'route', 'Recorded harness / route', 'to-route') + '</div>';
    html += '<div class="to-breakdown">' + ranking(rankings.projects, 'project', viewLabel + ' · projects', '') + ranking(rankings.tasks, 'task', viewLabel + ' · root tasks', 'to-route') + '</div>';
    if (physical.subscription_sessions) {
      var sessions = physical.subscription_summary || {}, normalized = sessions.normalized_metrics || {}, legacy = sessions.legacy_metrics || {};
      html += '<section class="to-panel"><details class="to-details" data-preserve="sessions"><summary>Session field availability <span class="to-dim">· ' + number(physical.subscription_sessions) + ' aggregates · ' + number(physical.unknown_subscription_sessions || 0) + ' with unknown tokens</span></summary><p class="to-note">Session counters may overlap physical calls. Normalized total input replaces legacy input for a valid object; its cache fields are included observations, never additions. Null normalized fields stay unknown.</p>';
      html += metricsTable([['Effective input', metric(sessions, 'prompt_tokens')], ['Generated output', metric(sessions, 'completion_tokens')], ['Effective cache read · non-additive', metric(sessions, 'cached_tokens')], ['Effective cache write · non-additive', metric(sessions, 'cache_write_tokens')]], 'Session values used in the chart');
      html += metricsTable([['Normalized total input', normalized.total_tokens], ['Normalized cache read', normalized.cache_read_tokens], ['Normalized cache write', normalized.cache_write_tokens]], 'Normalized observations only · missing objects and null fields stay unknown');
      html += metricsTable([['Legacy input', legacy.prompt_tokens], ['Legacy output', legacy.completion_tokens], ['Legacy cache read', legacy.cached_tokens], ['Legacy cache write', legacy.cache_write_tokens]], 'Original legacy fields · comparison only, never added to normalized totals');
      html += '</details></section>';
    }
    html += detailTable(data.details);
    if ((data.notices || []).length) html += '<section class="to-panel"><details class="to-details" data-preserve="interpretation"><summary>Reading these numbers</summary>' + data.notices.map(function (notice) { return '<p class="to-note">' + esc(notice) + '</p>'; }).join('') + '</details></section>';
    html += '<p class="to-footnote">Ouroboros and its launched agents only. Recorded harness / route, provider and model are distinct labels; none proves the observed executor. Reasoning: not separately reported. Cache is non-additive.<br>' + number(coverage.retained_undated_rows == null ? physical.undated_rows || 0 : coverage.retained_undated_rows) + ' undated retained records; included in All history, excluded from finite windows and charts.</p>';
    content.innerHTML = html;
    content.querySelectorAll('details[data-preserve]').forEach(function (el) { el.open = state.expanded.has(el.dataset.preserve); });
    if (focus || focusPref) { var target = focus ? content.querySelector('[data-focus="' + focus + '"]') : root.querySelector('[data-pref="' + focusPref + '"]'); if (target && !target.disabled) target.focus({preventScroll: true}); }
    var boundary = coverage.retained_start ? 'Retained: ' + coverage.retained_start + ' — ' + coverage.retained_end : 'Retained dates: unknown or empty';
    syncControls();
    $('.to-window').textContent += ' · ' + boundary + (coverage.history_complete === false ? ' · linked history incomplete' : '');
  }
  function changed() { state.preferencesTouched = true; state.page = 1; syncControls(); remember(); refresh(true); }
  async function download() {
    var button = $('[data-action="export"]'); button.disabled = true;
    var controller = new AbortController();
    // The export deliberately bypasses request(): it must hand the ORIGINAL response bytes
    // to the Blob, because parsing and reserializing would round integers beyond 2^53. So it
    // arms the same deadline itself, and keeps it armed across the blob read — a stalled
    // export otherwise leaves the Snapshot button disabled until disposal.
    var exportTimedOut = false;
    var exportTimer = state.disposing ? null : setTimeout(function () { exportTimedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    if (exportTimer) state.timers.add(exportTimer);
    try {
      var params = queryParams(); params.delete('page'); params.delete('page_size');
      state.controllers.add(controller);
      var response = await fetch(API + '/export?' + params.toString(), {credentials: 'same-origin', signal: controller.signal, cache: 'no-store', timeoutMs: REQUEST_TIMEOUT_MS});
      if (!response.ok) throw new Error('Export request failed (' + response.status + ').');
      // Preserve original JSON bytes: parsing/reserializing would round integers beyond 2^53.
      var blob = await response.blob();
      // Validate the envelope only; download the untouched Blob, never JSON.stringify(parsed).
      var envelope;
      try { envelope = JSON.parse(await blob.text()); } catch (e) { throw new Error('The snapshot response was not valid JSON.'); }
      if (envelope.error || envelope.status >= 400) throw new Error(typeof envelope.error === 'string' ? envelope.error : 'The snapshot endpoint reported an error.');
      if (!envelope.snapshot_id || !Array.isArray(envelope.rows)) throw new Error('The snapshot response is incomplete.');
      if (state.disposed) return;
      var url = URL.createObjectURL(blob); state.urls.add(url);
      var link = document.createElement('a'); link.href = url; link.download = 'token-observatory-' + String(envelope.snapshot_id).replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 48) + '.json'; link.style.display = 'none'; root.appendChild(link); link.click(); link.remove();
      setStatus('Frozen numeric snapshot downloaded');
      // Revoke on the next export or disposal: no detached timeout survives Stop.
      state.urls.forEach(function (existing) { if (existing !== url) { URL.revokeObjectURL(existing); state.urls.delete(existing); } });
    } catch (e) {
      // A deadline abort must not be swallowed as disposal, or the failure is invisible.
      if (exportTimedOut) { if (!state.disposed) error('Snapshot export failed: the endpoint did not answer within ' + Math.round(REQUEST_TIMEOUT_MS / 1000) + ' seconds.'); }
      else if (!state.disposed && e.name !== 'AbortError') error('Snapshot export failed: ' + e.message);
    }
    finally { if (exportTimer) { clearTimeout(exportTimer); state.timers.delete(exportTimer); } state.controllers.delete(controller); if (!state.disposed) button.disabled = false; }
  }
  function onClick(event) {
    var button = event.target.closest('button'); if (!button || !root.contains(button) || button.disabled) return;
    if (button.dataset.period) {
      state.preferencesTouched = true; state.query.period = button.dataset.period;
      if (state.query.period === 'custom' && (!state.query.custom_start || !state.query.custom_end)) {
        var today = new Date(), local = today.getFullYear() + '-' + String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');
        state.query.custom_start = local; state.query.custom_end = local;
        syncControls(); $('[data-pref="custom_start"]').focus(); return;
      }
      changed(); return;
    }
    if (button.dataset.view) { state.query.view = button.dataset.view; state.preferencesTouched = true; remember(); if (state.data) render(state.data); return; }
    if (button.dataset.scope) { state.query.scope = button.dataset.scope; changed(); return; }
    if (button.dataset.dimension) { state.query[button.dataset.dimension] = button.dataset.value || ''; if (button.dataset.dimension === 'task') state.query.scope = 'all'; updateFacets(state.data && state.data.facets); changed(); $('[data-pref="' + button.dataset.dimension + '"]').focus({preventScroll: true}); return; }
    switch (button.dataset.action) {
      case 'refresh': refresh(true); break;
      case 'apply-calendar': if (validateCalendar()) changed(); break;
      case 'clear': ['model', 'route', 'project', 'task'].forEach(function (key) { state.query[key] = ''; }); state.query.scope = 'all'; changed(); break;
      case 'previous': state.page = Math.max(1, state.page - 1); refresh(true); break;
      case 'next': state.page += 1; refresh(true); break;
      case 'export': download(); break;
      case 'explain': $('.to-explanation').hidden = false; $('.to-explanation').innerHTML = '<p>' + esc(button.dataset.explanation) + '<button type="button" data-action="close-explanation">Close explanation</button></p>'; $('.to-explanation').scrollIntoView({block: 'nearest', behavior: 'auto'}); break;
      case 'close-explanation': $('.to-explanation').hidden = true; break;
    }
  }
  function onChange(event) {
    var key = event.target.dataset.pref; if (!key) return;
    state.preferencesTouched = true;
    state.query[key] = event.target.value;
    if (key === 'custom_start' || key === 'custom_end') return;
    if (key === 'task') state.query.scope = 'all';
    changed();
  }
  function onVisibility() { if (!document.hidden && !state.disposed) refresh(false); }
  function dispose() {
    if (state.disposePromise) return state.disposePromise;
    state.disposePromise = (async function () {
      if (state.disposed) return;
      state.disposing = true; state.epoch += 1;
      clearTimeout(state.timer); state.timer = null;
      clearTimeout(state.prefTimer); state.prefTimer = null;
      // A fetch deadline is a timer too, so Stop must leave none of them outstanding.
      // The preference PUT kept alive for the drain below clears its own in its finally.
      state.timers.forEach(function (t) { clearTimeout(t); }); state.timers.clear();
      // Capture the latest controls even when the debounce timer has not fired yet.
      if (state.preferencesTouched) state.prefPending = JSON.stringify(state.query);
      // Stop rendering and cancel ordinary work synchronously. A preference PUT, if any,
      // is deliberately left alive for the awaited drain below.
      state.controllers.forEach(function (controller) { if (controller !== state.prefController) controller.abort(); });
      state.urls.forEach(function (url) { URL.revokeObjectURL(url); }); state.urls.clear();
      root.removeEventListener('click', onClick); root.removeEventListener('change', onChange);
      document.removeEventListener('visibilitychange', onVisibility);
      root.remove();
      // Keep the preference controller alive while the serial PUT queue drains. The host
      // awaits this hook during its one-second Stop handshake.
      while (state.prefSaving || state.prefPending !== null) {
        var operation = saveNextPreference();
        if (operation && typeof operation.then === 'function') await operation;
        else break;
      }
      state.disposed = true; state.disposing = false;
      state.controllers.forEach(function (controller) { controller.abort(); }); state.controllers.clear();
    })().catch(function () {
      // Cleanup must never reject the host's disposal handshake or create an unhandled rejection.
      state.disposed = true; state.disposing = false;
      state.controllers.forEach(function (controller) { controller.abort(); }); state.controllers.clear();
      state.urls.forEach(function (url) { URL.revokeObjectURL(url); }); state.urls.clear();
      root.removeEventListener('click', onClick); root.removeEventListener('change', onChange);
      document.removeEventListener('visibilitychange', onVisibility);
      root.remove();
    });
    return state.disposePromise;
  }
  root.__dispose = dispose;
  root.addEventListener('click', onClick); root.addEventListener('change', onChange);
  document.addEventListener('visibilitychange', onVisibility);
  if (typeof window.__ouroWidgetOnDispose === 'function') window.__ouroWidgetOnDispose(dispose);
  async function initialize() {
    var controller = new AbortController();
    try {
      var response = await request('/preferences', null, controller);
      if (state.disposed) return;
      var prefs = state.preferencesTouched ? {} : response.preferences || response;
      Object.keys(state.query).forEach(function (key) { if (typeof prefs[key] === 'string') state.query[key] = prefs[key]; });
      if (['1h', '24h', '7d', '30d', 'all', 'custom'].indexOf(state.query.period) < 0) throw new Error('The saved period is invalid.');
      if (['auto', 'physical', 'sessions'].indexOf(state.query.view) < 0) throw new Error('The saved accounting view is invalid.');
      if (['all', 'own', 'descendants'].indexOf(state.query.scope) < 0) throw new Error('The saved task scope is invalid.');
    } catch (e) {
      if (state.disposed || e.name === 'AbortError') return;
      if (!state.preferencesTouched) { state.query.period = 'all'; state.query.scope = 'all'; state.query.view = 'auto'; }
      prefError('Saved filters could not be restored: ' + e.message);
    }
    if (state.disposed) return;
    syncControls(); refresh(true);
  }
  initialize();
})();
