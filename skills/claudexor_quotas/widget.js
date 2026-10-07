/* Claudexor Quotas widget — v0.7.0
 *
 * Runs as a reviewed module widget: a classic inline script inside an
 * opaque-origin sandboxed iframe whose window.fetch is a parent-mediated
 * bridge restricted to this skill's own extension route prefix.
 *
 * Display law: a value that was not read is labeled as not read.
 * Never 0, never "unlimited", never an empty cell standing in for a refused facet.
 */
(function () {
    'use strict';

    var ROUTE = '/api/extensions/claudexor_quotas/quotas';
    var REFRESH_ROUTE = '/api/extensions/claudexor_quotas/refresh';
    var REFRESH_MS = 30000;
    // How long one request may take before the widget stops waiting for it.
    // Each bound sits above the skill's own bound on that route — its status
    // read gives up after 25 s, its live refresh after 180 s — with room for
    // the history read and the bridge, so the skill's own answer (a failure
    // included) normally arrives first. The bridge enforces the bound itself
    // (init.timeoutMs); BACKSTOP_MS later the widget stops waiting on its own,
    // for a host whose bridge does not.
    var GET_TIMEOUT_MS = 60000;
    var REFRESH_TIMEOUT_MS = 210000;
    var PREFS_TIMEOUT_MS = 30000;
    var BACKSTOP_MS = 5000;
    var FACET_ORDER = ['catalog', 'accounts', 'quota'];
    // The words follow the dot's new question: "heavy use" described a long
    // bar, while yellow now also means a model is out.
    var TONE_WORD = { ok: 'ready', warn: 'needs a look', bad: 'alert', muted: 'no live reading' };

    // How much of a row to unfold. The choice belongs to whoever is looking at
    // this screen, and the skill keeps it for them — see applyPrefs below for
    // why it cannot be kept here. One table and no list of keys beside it: two
    // sources for one set drift apart the day a fourth mode is added to only
    // one of them.
    var DENSITY_OPTIONS = [
        { key: 'compact', name: 'compact', note: 'bars only' },
        { key: 'normal', name: 'normal', note: 'what is spent, plus one line for the rest' },
        { key: 'detailed', name: 'detailed', note: 'a line per window, nothing folded' }
    ];
    // What a row in the account list says about model-scoped windows. The
    // choice is per family, because families differ in whether the engine
    // marks models at all — and it applies to the list only: the card of the
    // account you have opened always shows everything it was told.
    // The words for these are not written here: they are built from the data,
    // so a family whose model windows all name Fable offers "only Fable"
    // rather than a category nobody has seen on the screen.
    var MODEL_VIEWS = ['all', 'models', 'shared'];
    var PREFS_ROUTE = '/api/extensions/claudexor_quotas/prefs';
    // How far the reserve chart looks back and ahead. Not a saved choice: it
    // opens on a day every time.
    var HORIZONS = ['24h', '7d'];


    // Tabs, not one long column: what goes in here will keep growing, and a
    // panel that answers by scrolling makes every setting harder to find than
    // the last.
    var SETTINGS_TABS = [
        { key: 'detail', name: 'Row detail', icon: 'rows' },
        { key: 'models', name: 'Models', icon: 'filter' },
        { key: 'accounts', name: 'Accounts', icon: 'fold' },
        { key: 'state', name: 'System state', icon: 'signal' }
    ];

    // Why an account can be folded out of the list. The skill keeps a yes or
    // no for each of them, and the order here is the order they stand in.
    var FOLD_REASONS = ['failed', 'disabled', 'signed_out'];
    // Folded until the skill says otherwise, written once. A fresh map every
    // time: one shared object here and the reader's first switch would edit
    // the defaults themselves.
    function foldDefaults() {
        var out = {};
        FOLD_REASONS.forEach(function (reason) { out[reason] = true; });
        return out;
    }
    // Sentence case, and the same words the row's tail uses one line lower, so
    // a reader is not told the state twice in two vocabularies.
    var FOLD_TITLE = {
        failed: 'Verification failed',
        disabled: 'Disabled',
        signed_out: 'Not signed in'
    };
    // What the reader is actually switching, in the words of the thing that
    // caused it. A title alone leaves "Disabled" and "Not signed in" looking
    // like the same trouble twice.
    var FOLD_NOTE = {
        failed: 'Signed in, but the check failed. Their dot stays red.',
        disabled: 'Switched off in Claudexor, so nothing runs on them.',
        signed_out: 'No login on this machine.'
    };

    var root = document.getElementById('root');
    var generation = 0;
    var dataTimer = null;
    // The open account list, held rather than looked up: the tree is thrown away
    // and rebuilt on every redraw, and this is the one node whose scroll has to
    // survive that.
    var accountPop = null;
    var lastGood = null;
    var lastGoodAt = 0;
    var stopped = false;
    var disposed = false;
    var themeOff = null;
    var inFlight = false;
    var settingsOpen = false;
    // The open tab is not remembered — the panel always opens on the one a
    // reader came for most often.
    var settingsTab = 'detail';
    // The reader's display choices. They start as the defaults and are replaced
    // by whatever the skill has kept, which arrives with the first reading.
    var density = 'normal';
    var modelChoices = {};
    // A reader who has never opened the settings sees the short list, not
    // every dead account.
    var foldChoices = foldDefaults();
    // While a save is in the air the screen is ahead of the skill; a reading
    // that lands in that window would drag the choice back to what was stored
    // a moment ago.
    var prefsInFlight = 0;
    // Set when the skill answered but did not keep the choice. Without it the
    // screen paints the wish, holds it for thirty seconds and then quietly
    // reverts — the very thing the route was added to stop.
    var saveError = '';
    // Every save still in the air, as one promise the dispose hook can wait on;
    // cleared when the count above comes back to zero.
    var prefsSaving = null;
    // Saves are numbered in the order the reader made them, under a name for
    // this load of the frame. Two saves can cross on the way to the skill and
    // back, and the later answer is not always the later choice: only the
    // newest save's answer speaks for the screen, and the skill does not write
    // a save that a newer one from this frame has already overtaken.
    var PREFS_FRAME = Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
    var prefsSeq = 0;
    // The newest save that has answered, success or failure.
    var prefsAnswered = 0;
    // The screen shows one account at a time: which family, and which account
    // inside it. Both survive the 30-second redraw, and both fall back on their
    // own when what they point at stops existing.
    var selectedHarness = '';
    var selectedAccountKey = '';
    var accountsOpen = false;
    // Folded shut every time the list opens; the one exception is decided in
    // the handler that opens it, where the accounts are in hand.
    var foldOpen = false;
    var focusAccountBtn = false;
    var currentView = null;
    var staleMessage = '';
    var actionMessage = '';
    // The reserve chart: its horizon, the limit picked per family, the last
    // chart request asked on the reader's behalf (so a mismatch is asked about
    // once, not in a loop), and whether a live Refresh landed after the
    // overview was computed — the overview is not recomputed by a refresh.
    var horizon = '24h';
    var chartKeys = {};
    var chartAsked = '';
    var refreshedSinceSummary = false;
    // The chart's data table is the reader's accessible copy of the chart:
    // the 30-second rebuild must not fold it away under them.
    var chartTableOpen = false;
    // The chart is folded until the reader asks for it, every time the
    // widget opens; open, it stays open across the 30-second redraw. So do
    // a limit's unfolded details, the "how to read" note, and the chart's
    // cursor while the pointer or the keyboard is on it.
    var chartOpen = false;
    var openRows = {};
    var aboutOpen = false;
    var chartCursor = null;
    // The account below the overview opens folded to its state and problems
    // on every mount, and the whole card
    // (every tile, last-known readings) opens on request. Like the chart it
    // is a choice for this visit only: kept across the 30-second redraw and
    // a theme change, never saved.
    var accountOpen = false;
    // Set while render() throws the old tree away. Chromium fires blur on a
    // focused node as it is removed; that blur is the redraw's, not the
    // reader's, and must not take the chart's keyboard cursor with it.
    var rebuilding = false;
    // The control the keyboard was on when a redraw drew it disabled — Refresh
    // while its own request is in the air. A disabled button cannot hold focus,
    // so the browser drops it to the body; the key waits here for the redraw
    // that enables the control again, unless the reader has moved on.
    var focusParked = '';
    // The estimate's refill scenario is drawn only while the reader asks for
    // it; a reported reset time is not evidence of a refill.
    var refillShown = false;
    // Set when the last answer could not be drawn: the screen before it stays
    // up, with this said above it and a Retry.
    var drawFault = '';

    var STYLE_ID = 'claudexor-quotas-style';

    // One neutral surface and one type scale, shared by the summary and disclosures.
    var STYLE = [
        ":root{",
        "color-scheme:dark;",
        "--bg-canvas:#171719;--surface:#202023;--inset:#29292d;--popup:#242427;",
        "--text-primary:#e4e4e7;--text-meta:#b0b0b8;--text-secondary:#a0a0aa;--text-disabled:#787881;",
        "--edge:#38383e;--edge-strong:#62626c;--neutral-rgb:228,228,231;",
        "--status-ok:#82bd9c;--status-ok-bg:#263b30;--status-warn:#dfb574;--status-warn-bg:#3b3224;",
        "--status-warn-border:#665238;--status-bad:#e99a9f;--status-bad-bg:#40292e;--status-bad-border:#72444b;",
        "--status-stale:#dfb574;--warn-text:var(--status-warn);--bad-text:var(--status-bad);--stale-text:var(--status-stale);",
        "--accent-core:#c93545;--focus-accent-border:#ec7782;--share-ink:#a6a6b3;--pace-ink:#9ab9fa;",
        "--type-meta:12px;--type-body:14px;--type-section:16px;--row-h:32px;",
        "--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;",
        "--radius-sm:6px;--radius-md:8px;--radius-lg:12px;",
        "--font-mono:ui-monospace,\"SF Mono\",Menlo,Monaco,Consolas,monospace;",
        "}",
        ":root[data-theme=light]{color-scheme:light;--bg-canvas:#fafafa;--surface:#fff;--inset:#f3f3f5;--popup:#fff;",
        "--text-primary:#26262b;--text-meta:#60606b;--text-secondary:#686873;--text-disabled:#90909a;",
        "--edge:#e2e2e7;--edge-strong:#91919d;--neutral-rgb:38,38,43;",
        "--status-ok:#38704f;--status-ok-bg:#edf6f0;--status-warn:#845713;--status-warn-bg:#fbf5e9;",
        "--status-warn-border:#e4c78d;--status-bad:#a73c49;--status-bad-bg:#fcf0f1;--status-bad-border:#e8b3ba;",
        "--status-stale:#845713;--focus-accent-border:#b62f42;--share-ink:#787884;--pace-ink:#365eac}",
        "*{box-sizing:border-box}",
        "body{margin:0;padding:var(--space-4);font:var(--type-body)/1.5 -apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;color:var(--text-primary);background:var(--bg-canvas);-webkit-font-smoothing:antialiased}",
        "#root{display:flex;flex-direction:column;min-width:0}",
        "button{font:inherit;color:var(--text-primary);cursor:pointer}",
        "button:disabled{color:var(--text-disabled);cursor:default}",
        "button:focus-visible,summary:focus-visible,.chart-plot:focus-visible{outline:2px solid var(--focus-accent-border);outline-offset:2px}",
        "button svg{flex:none}",
        ".sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}",
        ".control-bar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:var(--space-2);margin-bottom:var(--space-4)}",
        ".harness-seg,.action-seg{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1)}",
        ".harness-seg{flex:1 1 280px}",
        ".action-seg{margin-left:auto}",
        ".harness-btn,.action-btn,.pill-btn,.seg-opt,.models-opt,.settings-tab{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:var(--row-h);padding:4px 10px;border:1px solid transparent;border-radius:var(--radius-sm);background:transparent;color:var(--text-primary);font-size:var(--type-meta);line-height:1.35;position:relative}",
        ".harness-btn{padding-inline:6px;gap:5px}",
        ".harness-btn:hover,.action-btn:hover:not(:disabled),.pill-btn:hover,.seg-opt:hover,.models-opt:hover,.settings-tab:hover{background:var(--inset)}",
        ".harness-btn.active,.action-btn.is-open,.pill-btn.on,.seg-opt.active,.models-opt.active,.settings-tab.active{background:var(--inset);border-color:var(--edge);font-weight:600}",
        ".action-btn,.pill-btn{border-color:var(--edge);background:var(--surface)}",
        ".action-refresh{font-weight:600}",
        ".harness-btn.loading{color:var(--text-disabled)}",
        ".harness-initial{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;font-size:var(--type-meta);font-weight:600}",
        ".pip,.state-dot{display:inline-block;flex:none;width:5px;height:5px;border-radius:50%;background:var(--text-disabled)}",
        ".pip.ok,.state-dot.ok{background:var(--status-ok)}",
        ".pip.warn,.state-dot.warn{background:var(--status-warn)}",
        ".pip.bad,.state-dot.bad{background:var(--status-bad)}",
        ".pip.muted,.state-dot.muted{background:var(--text-disabled)}",
        ".action-btn.has-problem{color:var(--status-bad);border-color:var(--status-bad-border)}",
        ".dot-label{display:inline-flex;align-items:center;gap:6px;font-size:var(--type-meta);color:var(--text-meta)}",
        ".dot-label.strong{color:var(--text-primary)}",
        ".icon-spin{display:inline-flex}",
        "@keyframes spin{to{transform:rotate(360deg)}}",
        ".action-btn.is-refreshing .icon-spin{animation:spin 1s linear infinite}",
        "@media(prefers-reduced-motion:reduce){.action-btn.is-refreshing .icon-spin{animation:none}}",
        ".banner{display:flex;align-items:flex-start;gap:var(--space-2);padding:var(--space-3);margin-bottom:var(--space-3);border:1px solid var(--status-warn-border);border-radius:var(--radius-md);background:var(--status-warn-bg);color:var(--warn-text);font-size:var(--type-meta);overflow-wrap:anywhere}",
        ".banner.bad{background:var(--status-bad-bg);border-color:var(--status-bad-border);color:var(--bad-text)}",
        ".banner-icon{display:flex;flex:none;margin-top:2px}",
        ".reserve{min-width:0;margin-bottom:var(--space-4)}",
        ".reserve-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--space-2) var(--space-3);flex-wrap:wrap;margin-bottom:var(--space-1)}",
        ".reserve-title{margin:0;font-size:var(--type-section);font-weight:600}",
        ".reserve-family{font-weight:400;color:var(--text-meta)}",
        ".reserve-age,.reserve-note{font-size:var(--type-meta);color:var(--text-meta)}",
        ".reserve-age{font-variant-numeric:tabular-nums}",
        ".reserve-age.old{color:var(--warn-text)}",
        ".reserve-note{margin:var(--space-2) 0}",
        ".reserve-unit{margin:0 0 var(--space-3)}",
        ".reserve-table{border-top:1px solid var(--edge)}",
        ".reserve-cols{display:none}",
        ".reserve-row{width:100%;display:grid;grid-template-columns:minmax(0,1fr) auto 12px;grid-template-areas:\"name fig chev\" \"strip avg chev\" \"tail tail chev\";gap:6px var(--space-3);padding:var(--space-3) 0;border:0;border-bottom:1px solid var(--edge);border-radius:0;background:transparent;text-align:left}",
        ".reserve-row:hover,.reserve-row.open{background:var(--inset)}",
        ".reserve-row:focus-visible{outline-offset:-2px}",
        ".rs-name{grid-area:name;display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1) var(--space-2);min-width:0;font-size:var(--type-body)}",
        ".rs-name-text{min-width:0;overflow-wrap:anywhere}",
        ".rs-tight{font-size:var(--type-meta);color:var(--text-meta);white-space:nowrap}",
        ".rs-tight:before{content:\"\u00b7 \";color:var(--text-meta)}",
        ".rs-accts{grid-area:strip;display:flex;align-items:center;flex-wrap:wrap;gap:6px var(--space-2);min-width:0}",
        ".rs-meas{font-size:var(--type-meta);color:var(--text-meta);white-space:nowrap}",
        ".rs-cov,.rs-fig,.rs-avg{font-variant-numeric:tabular-nums}",
        ".rs-fig{grid-area:fig;font-size:var(--type-section);font-weight:600;white-space:nowrap;text-align:right}",
        ".rs-of,.rs-unit{font-size:var(--type-meta);font-weight:400;color:var(--text-meta)}",
        ".rs-avg{grid-area:avg;font-size:var(--type-meta);color:var(--text-meta);text-align:right;white-space:nowrap}",
        ".rs-tail{grid-area:tail;font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".rs-warn{color:var(--warn-text)}",
        ".rs-chev{grid-area:chev;display:flex;align-self:center;color:var(--text-meta);transform:rotate(-90deg)}",
        ".reserve-row.open .rs-chev{transform:none}",
        ".strip{display:flex;align-items:flex-end;gap:2px;height:24px;min-width:0}",
        ".cell{position:relative;display:inline-block;width:7px;height:24px;flex:none;box-shadow:0 1px 0 var(--edge-strong);vertical-align:middle}",
        ".cell-fill{position:absolute;left:0;right:0;bottom:0;background:var(--share-ink);border-radius:1px 1px 0 0}",
        ".meter{position:relative;display:block;height:6px;box-shadow:0 1px 0 var(--edge-strong)}",
        ".meter-fill{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink);border-radius:0 1px 1px 0}",
        ".meter.mini{display:inline-block;width:36px;height:4px;flex:none}",
        ".meter.avg{display:inline-block;width:96px;flex:none}",
        ".cell.restricted,.meter.restricted{box-shadow:0 1px 0 var(--status-warn)}",
        ".cell.restricted .cell-fill,.meter.restricted .meter-fill{background:var(--status-warn)}",
        ".cell.spent,.meter.spent{box-shadow:0 2px 0 var(--status-bad)}",
        ".meter.stale .meter-fill{background:rgba(var(--neutral-rgb),.24)}",
        ".cell.unread{box-shadow:none;font-size:var(--type-meta);line-height:24px;text-align:center;color:var(--text-meta)}",
        ".cell.unread.many{width:auto;padding:0 2px;white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".strip.avg{align-items:center;gap:6px}",
        ".strip-avg{font-size:var(--type-meta);color:var(--text-meta)}",
        ".reserve-detail{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px var(--space-4);margin:0;padding:var(--space-3);background:var(--inset);font-size:var(--type-meta);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".reserve-detail dt{color:var(--text-meta)}",
        ".reserve-detail dd{margin:0;min-width:0;overflow-wrap:anywhere}",
        ".reserve-foot{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-3);padding-top:var(--space-3)}",
        ".reserve-cover{flex:1 1 260px;font-size:var(--type-meta);color:var(--text-meta)}",
        ".foot-btns{display:flex;gap:var(--space-2);margin-left:auto}",
        ".pill-icon{display:flex}",
        ".reserve-about{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--space-3) var(--space-5);padding:var(--space-4);margin-top:var(--space-3);border-radius:var(--radius-md);background:var(--inset);font-size:var(--type-meta);color:var(--text-meta)}",
        ".about-title{font-size:var(--type-body);font-weight:500;color:var(--text-primary);margin-bottom:var(--space-1)}",
        ".reserve-about p{margin:0}",
        ".strip-legend{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-3);font-size:var(--type-meta);color:var(--text-meta);margin-top:var(--space-2)}",
        ".strip-legend .cell{height:16px}",
        ".strip-legend .cell.unread{line-height:16px}",
        ".account-area{min-width:0;border-top:1px solid var(--edge);padding-top:var(--space-3)}",
        ".account-toolbar{display:flex;align-items:flex-start;gap:var(--space-2);margin-bottom:var(--space-2)}",
        ".account-label{padding-top:7px;font-size:var(--type-meta);color:var(--text-meta);white-space:nowrap}",
        ".acct-wrap{position:relative;flex:1 1 auto;min-width:0}",
        ".acct-btn{width:100%;min-height:var(--row-h);display:flex;align-items:center;gap:var(--space-2);padding:5px var(--space-2);border:1px solid var(--edge);border-radius:var(--radius-sm);background:var(--surface);text-align:left;font-size:var(--type-meta)}",
        ".acct-btn:hover:not(:disabled){border-color:var(--edge-strong)}",
        ".acct-name{flex:1 1 auto;min-width:40px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:var(--type-body)}",
        ".acct-count,.acct-opt-tail{font-size:var(--type-meta);color:var(--text-meta);font-variant-numeric:tabular-nums;flex:none}",
        ".acct-alarm{font-size:var(--type-meta);color:var(--status-bad);white-space:nowrap;flex:none}",
        ".acct-caret{display:flex;flex:none;color:var(--text-meta)}",
        ".acct-pop{position:relative;margin-top:6px;padding:var(--space-1);max-height:320px;overflow-y:auto;border:1px solid var(--edge-strong);border-radius:var(--radius-md);background:var(--popup);box-shadow:0 8px 24px rgba(0,0,0,.12)}",
        ".acct-opt{width:100%;display:flex;align-items:flex-start;gap:var(--space-2);padding:var(--space-2);border:0;border-radius:var(--radius-sm);background:transparent;text-align:left;font-size:var(--type-body)}",
        ".acct-opt:hover,.acct-opt.active{background:var(--inset)}",
        ".acct-opt .state-dot{margin-top:7px}",
        ".acct-opt-body{flex:1 1 auto;min-width:0}",
        ".acct-opt-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acct-opt-tail{max-width:40%;overflow-wrap:anywhere;text-align:right}",
        ".acct-line2{font-size:var(--type-meta);color:var(--text-meta);margin-top:var(--space-1);overflow-wrap:anywhere}",
        ".acct-wins,.acct-grp{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-2);min-width:0;max-width:100%}",
        ".acct-wins{margin-top:var(--space-1)}",
        ".acct-win{display:inline-flex;align-items:center;gap:5px;font-size:var(--type-meta);color:var(--text-meta);min-width:0;max-width:100%;font-variant-numeric:tabular-nums}",
        ".acct-win-tag{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acct-win-pct{white-space:nowrap}",
        ".acct-sep{width:1px;height:12px;background:var(--edge)}",
        ".acct-cap,.tile-model,.model-chip,.models-name{font-size:var(--type-meta);color:var(--text-meta);background:var(--inset);padding:0 4px;border-radius:3px;max-width:100%;overflow-wrap:anywhere}",
        ".acct-cap.exhausted,.tile-model.spent,.model-chip.exhausted{color:var(--status-bad);background:var(--status-bad-bg)}",
        ".acct-cap.held,.tile-model.held,.model-chip.held{color:var(--warn-text);background:var(--status-warn-bg)}",
        ".acct-pool{max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}",
        ".acct-rls{margin-top:var(--space-1);font-size:var(--type-meta)}",
        ".acct-rl{display:flex;align-items:baseline;gap:6px;padding:2px 0;flex-wrap:wrap}",
        ".acct-rl-tag{max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acct-rl-tag.bad,.acct-rl-when{color:var(--status-bad)}",
        ".acct-rl-tag.warn,.acct-rl-when.warn{color:var(--warn-text)}",
        ".acct-rl-tag.ok,.acct-rl-when.free{color:var(--status-ok)}",
        ".acct-rl-txt{color:var(--text-meta)}",
        ".acct-rl-lead{flex:1 1 auto;min-width:12px;border-bottom:1px dotted var(--edge);align-self:center}",
        ".acct-rl-when,.acct-rl-in{font-variant-numeric:tabular-nums;white-space:nowrap}",
        ".acct-rl-in,.acct-rl-more{color:var(--text-meta)}",
        ".acct-fold{width:100%;display:flex;align-items:center;gap:var(--space-2);padding:var(--space-2);border:0;border-top:1px solid var(--edge);background:transparent;font-size:var(--type-meta);text-align:left}",
        ".acct-fold-caret{display:flex;transform:rotate(-90deg)}",
        ".acct-fold.open .acct-fold-caret{transform:none}",
        ".acct-fold-word{flex:1}",
        ".acct-fold-dots{display:flex;gap:4px}",
        ".acct-sec-head{display:flex;align-items:center;gap:var(--space-2);padding:var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".acct-sec-line{flex:1;height:1px;background:var(--edge)}",
        ".acct-sec.broken{background:var(--status-bad-bg);border-radius:var(--radius-sm)}",
        ".account-head,.account-header,.account-title-wrap,.account-head-right{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap}",
        ".account-head{justify-content:space-between}",
        ".account-head-left{min-width:0;flex:1}",
        ".account-header{margin-bottom:var(--space-1)}",
        ".acct-family{font-size:var(--type-body);font-weight:500}",
        ".plan-chip,.account-next-up{font-size:var(--type-meta);color:var(--text-meta)}",
        ".account-meta{display:flex;align-items:center;gap:var(--space-1) var(--space-2);flex-wrap:wrap;font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".account-meta span+span:before{content:\"\u00b7\";margin-right:var(--space-2)}",
        ".meta-bad{color:var(--status-bad)}",
        ".meta-muted{color:var(--text-meta)}",
        ".account-head-right{margin-left:auto}",
        ".quota-primary-row{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap;font-size:var(--type-meta)}",
        ".quota-primary-text{color:var(--text-meta)}",
        ".quota-primary-text.exhausted{color:var(--status-bad)}",
        ".quota-primary-text.cooling{color:var(--warn-text)}",
        ".quota-when{display:inline-flex;align-items:center;flex-wrap:wrap;gap:4px;font-size:var(--type-meta);color:var(--text-meta)}",
        ".quota-cooldown,.quota-exhaustion,.quota-unavailable,.acct-lastknown{display:flex;align-items:flex-start;gap:6px;margin-top:var(--space-2);font-size:var(--type-meta);color:var(--warn-text)}",
        ".quota-cooldown>svg,.quota-exhaustion>svg,.quota-unavailable>svg,.acct-lastknown>svg{flex:none;margin-top:3px}",
        ".quota-exhaustion.past{color:var(--text-meta)}",
        ".quota-cooldown-body{min-width:0;overflow-wrap:anywhere}",
        // An inline-flex part drops the space its own text starts with, and
        // "Fable · until" read "Fable· until": the gap is given back here.
        ".quota-cooldown-body>.quota-when{margin-left:.3em}",
        ".quota-action{color:var(--warn-text)}",
        ".quota-unavailable{flex-wrap:wrap}",
        ".acct-toggle{flex:none}",
        ".acct-toggle-caret{transform:rotate(-90deg)}",
        ".acct-toggle.on .acct-toggle-caret{transform:none}",
        ".quotas-container{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:var(--space-3);margin-top:var(--space-3)}",
        ".quota-tile{min-width:0;padding:var(--space-3);border-radius:var(--radius-md);background:var(--inset)}",
        ".quota-tile:only-child{grid-column:1/-1}",
        ".tile-head,.tile-meta,.tile-name,.tile-foot,.tile-when{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap}",
        ".tile-head,.tile-meta{justify-content:space-between;font-size:var(--type-meta);color:var(--text-meta)}",
        ".tile-head{color:var(--text-primary)}",
        ".tile-meta{margin-top:var(--space-2)}",
        ".tile-pct{white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".tile-pct.spent{color:var(--status-bad)}",
        ".tile-when-word{color:var(--text-meta)}",
        ".tile-when-word.warn{color:var(--warn-text)}",
        ".rel-time{color:var(--text-meta)}",
        ".quota-tile .meter{margin:var(--space-2) 0}",
        ".quota-last-known{margin-top:var(--space-3);padding:var(--space-3);border-left:2px solid var(--status-stale);background:var(--status-warn-bg)}",
        ".last-known-copy{font-size:var(--type-meta);color:var(--stale-text)}",
        ".quota-tile.stale .tile-pct,.quota-tile.stale .tile-when-word{color:var(--status-stale)!important}",
        ".ticker{font-variant-numeric:tabular-nums}",
        ".ticker.bad{color:var(--status-bad)}",
        ".ticker.warn{color:var(--warn-text)}",
        ".model-chips-wrap{display:flex;align-items:center;gap:4px;flex-wrap:wrap}",
        ".meta{display:flex;align-items:center;gap:6px;font-size:var(--type-meta);color:var(--text-meta)}",
        ".empty-card{padding:var(--space-5);background:var(--surface);border:1px solid var(--edge);border-radius:var(--radius-md);text-align:center;display:flex;align-items:center;flex-direction:column;gap:var(--space-2)}",
        ".empty-icon{color:var(--text-meta)}",
        ".empty-title{font-size:var(--type-section);font-weight:500;margin:0}",
        ".empty-desc{font-size:var(--type-meta);color:var(--text-meta);max-width:360px;margin:0}",
        ".empty-notes{display:flex;gap:var(--space-3);flex-wrap:wrap}",
        ".settings-panel{border:1px solid var(--edge);border-radius:var(--radius-md);background:var(--surface);min-width:0}",
        ".settings-tabs{display:flex;flex-wrap:wrap;gap:var(--space-1);padding:var(--space-2);border-bottom:1px solid var(--edge)}",
        ".settings-body{padding:var(--space-4)}",
        ".settings-note,.models-note,.fold-note,.density-note{font-size:var(--type-meta);color:var(--text-meta)}",
        ".settings-note{margin-bottom:var(--space-3)}",
        ".settings-opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:var(--space-2)}",
        ".density-opt{display:flex;flex-direction:column;gap:var(--space-2);padding:var(--space-3);text-align:left;border:1px solid var(--edge);border-radius:var(--radius-sm);background:transparent}",
        ".density-opt.active,.density-opt:hover{background:var(--inset)}",
        ".density-opt.active{border-color:var(--edge-strong)}",
        ".density-top{display:flex;align-items:center;justify-content:space-between;gap:var(--space-2)}",
        ".density-mark{width:12px;height:12px;border:1px solid var(--edge-strong);border-radius:50%}",
        ".density-mark.on{background:var(--text-meta)}",
        ".density-name,.density-note{display:block}",
        ".density-name{font-size:var(--type-body)}",
        ".dens-pv{flex:none}",
        ".dens-pv .pv-dot,.dens-pv .pv-name,.dens-pv .pv-line.strong{fill:var(--text-meta)}",
        ".dens-pv .pv-bar,.dens-pv .pv-line{fill:var(--edge-strong)}",
        ".dens-pv .pv-bar.spent{fill:var(--status-bad)}",
        ".models-row,.fold-row{display:flex;align-items:center;justify-content:space-between;gap:var(--space-3);padding:var(--space-3) 0;flex-wrap:wrap}",
        ".models-family{display:inline-flex;align-items:center;gap:var(--space-2);font-size:var(--type-body)}",
        ".models-seg,.seg{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1)}",
        ".models-opt,.seg-opt{border-color:var(--edge)}",
        ".models-note{margin-bottom:var(--space-2)}",
        ".fold-row{flex-wrap:nowrap;border-top:1px solid var(--edge)}",
        ".fold-words{min-width:0}",
        ".fold-name,.fold-note{display:block}",
        ".fold-name{font-size:var(--type-body)}",
        ".switch{position:relative;width:40px;height:var(--row-h);padding:0;flex:none;border:0;background:transparent}",
        ".switch:before{content:\"\";position:absolute;left:0;right:0;top:6px;height:20px;border:1px solid var(--edge-strong);border-radius:12px;background:var(--inset)}",
        ".switch.on:before{background:var(--text-meta)}",
        ".switch-knob{position:absolute;top:9px;left:3px;width:14px;height:14px;background:var(--text-meta);border-radius:50%}",
        ".switch.on .switch-knob{left:23px;background:var(--surface)}",
        ".settings-state{display:flex;flex-wrap:wrap;gap:var(--space-3) var(--space-4)}",
        ".chart-panel{margin-top:var(--space-4);padding:var(--space-3);background:var(--surface);border:1px solid var(--edge);border-radius:var(--radius-md);min-width:0}",
        ".chart-top{display:flex;align-items:center;justify-content:space-between;gap:var(--space-2);flex-wrap:wrap}",
        ".chart-title{font-size:var(--type-body)}",
        ".chart-sub,.chart-scope,.chart-facts,.chart-notes,.chart-legend{font-size:var(--type-meta);color:var(--text-meta)}",
        ".chart-sub{margin-top:var(--space-3)}",
        ".chart-scope{overflow-wrap:anywhere}",
        ".chart-plot{position:relative;margin-top:var(--space-2);border-radius:var(--radius-sm)}",
        ".chart-svg{display:block;max-width:100%;height:auto;overflow:visible;touch-action:none}",
        ".chart-svg .grid,.chart-svg .tick,.chart-svg .day-rule,.chart-svg .reset-line{stroke:var(--edge);stroke-width:1}",
        ".chart-svg .grid.base,.chart-svg .grid.cap{stroke:var(--edge-strong)}",
        ".chart-svg .axis-text,.chart-svg .reset-text,.chart-svg .region-text,.chart-svg .rail-text{fill:var(--text-meta);font-size:var(--type-meta);font-family:inherit;font-variant-numeric:tabular-nums}",
        ".chart-svg .axis-text.now,.chart-svg .axis-text.day{fill:var(--text-primary);font-weight:500}",
        ".chart-svg .future-bg{fill:var(--inset)}",
        ".chart-svg .gap-band,.chart-legend .gap-band{fill:rgba(var(--neutral-rgb),.12)}",
        ".chart-svg .now-line,.chart-svg .cursor-line{stroke:var(--text-meta);stroke-width:1}",
        ".chart-svg .hit{fill:transparent;cursor:crosshair}",
        ".line-observed{fill:none;stroke:var(--text-primary);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}",
        ".line-hold{fill:none;stroke:var(--text-meta);stroke-width:2;stroke-dasharray:6 4;stroke-linejoin:round}",
        ".line-pace{fill:none;stroke:var(--pace-ink);stroke-width:2;stroke-dasharray:2 3.5;stroke-linecap:round;stroke-linejoin:round}",
        ".point-seen{fill:var(--text-primary);stroke:var(--surface);stroke-width:1.5}",
        ".point-unsettled{fill:var(--surface);stroke:var(--text-primary);stroke-width:1.5;stroke-dasharray:2 1.6}",
        ".rail-line{stroke:var(--text-meta);stroke-width:1;stroke-dasharray:1 2.5}",
        ".chart-svg .cursor-dot{stroke:var(--surface);stroke-width:2;stroke-dasharray:none}",
        ".chart-svg .cursor-dot.line-observed{fill:var(--text-primary)}",
        ".chart-svg .cursor-dot.line-hold{fill:var(--text-meta)}",
        ".chart-svg .cursor-dot.line-pace{fill:var(--pace-ink)}",
        ".chart-tip{position:absolute;z-index:5;pointer-events:none;max-width:100%;padding:var(--space-2);border-radius:var(--radius-sm);background:var(--popup);border:1px solid var(--edge-strong);font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".tip-when,.tip-val{color:var(--text-primary);font-weight:500}",
        ".tip-row{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}",
        ".tip-row svg{flex:none}",
        ".tip-foot{margin-top:var(--space-1)}",
        ".chart-legend{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-3);margin-top:var(--space-2)}",
        ".legend-item{display:inline-flex;align-items:center;gap:6px}",
        ".legend-item svg{flex:none}",
        ".chart-facts{display:flex;flex-direction:column;gap:var(--space-2);margin-top:var(--space-3)}",
        ".chart-notes{margin-top:var(--space-2)}",
        ".chart-notes p{margin:0 0 var(--space-2)}",
        ".chart-table{margin-top:var(--space-3);overflow-x:auto}",
        ".chart-table summary{cursor:pointer;font-size:var(--type-meta);color:var(--text-primary);min-height:var(--row-h);padding:6px 0}",
        ".chart-table table{border-collapse:collapse;width:100%;font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".chart-table th,.chart-table td{text-align:left;padding:6px var(--space-2) 6px 0;border-top:1px solid var(--edge);color:var(--text-meta);vertical-align:top}",
        ".chart-table th{font-weight:500;color:var(--text-primary)}",
        // 0.7.0 — the approved v4 composition: one row per limit with a bar
        // per account, a hatched last-known value, an outlined "?" unknown.
        // The hatch is a 6px tile drawn once per theme: a mark of state
        // (last known), not decoration.
        ":root{--share-track:rgba(var(--neutral-rgb),.07);--area:rgba(var(--neutral-rgb),.10);--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2E2YTZiMycgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=);--hatch-warn:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2RmYjU3NCcgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=)}",
        ":root[data-theme=light]{--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzc4Nzg4NCcgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=);--hatch-warn:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzg0NTcxMycgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=)}",
        ".harness-count{color:var(--text-meta);font-weight:400;font-variant-numeric:tabular-nums}",
        ".reserve-age{display:inline-flex;align-items:baseline;gap:6px;text-align:right}",
        ".reserve-age .pip{position:relative;top:-1px}",
        ".reserve-age.status-warn{color:var(--warn-text)}",
        ".reserve-age.status-bad{color:var(--bad-text)}",
        ".banner-text{flex:1 1 auto;min-width:0}",
        ".banner .banner-retry{margin:-4px 0 -4px auto;min-height:26px;padding:2px 10px;color:inherit;border-color:currentColor;background:transparent;white-space:nowrap;flex:0 0 auto}",
        ".lrows{border-top:1px solid var(--edge)}",
        ".lrow{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto 28px;grid-template-areas:\"name fig more\" \"strip sub more\" \"tail tail more\";column-gap:var(--space-4);row-gap:6px;padding:var(--space-3) var(--space-2);border-bottom:1px solid var(--edge)}",
        ".lrow.sel,.lrow.open{background:var(--inset)}",
        ".l-name{grid-area:name;justify-self:start;align-self:start;display:inline-flex;align-items:baseline;flex-wrap:wrap;gap:2px 8px;min-height:0;padding:0;border:0;background:none;font-size:var(--type-body);font-weight:500;text-align:left;line-height:1.35}",
        ".l-name:hover .l-name-text{text-decoration:underline;text-decoration-color:var(--edge-strong);text-underline-offset:3px}",
        ".lrow.sel .l-name{font-weight:600}",
        ".l-name .on{color:var(--text-meta);font-size:var(--type-meta);font-weight:400}",
        ".l-fig{grid-area:fig;justify-self:end;white-space:nowrap;line-height:1.35}",
        ".l-fig b{font-size:var(--type-section);font-weight:600;font-variant-numeric:tabular-nums}",
        ".l-fig .of{color:var(--text-meta);font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".l-sub{grid-area:sub;align-self:end;justify-self:end;text-align:right;color:var(--text-meta);font-size:var(--type-meta);white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".l-sub.warn{color:var(--warn-text)}",
        ".l-sub .sw{display:inline-block;width:9px;height:9px;margin-right:5px;vertical-align:-1px;border-radius:1px;background:rgba(var(--neutral-rgb),.08) var(--hatch)}",
        ".l-tail{grid-area:tail;min-width:0;color:var(--text-meta);font-size:var(--type-meta);overflow-wrap:anywhere}",
        ".l-tail .rs-bad{color:var(--bad-text)}",
        ".l-more{grid-area:more;align-self:center;justify-self:end;display:flex;align-items:center;justify-content:center;width:28px;min-height:var(--row-h);padding:0;border:0;border-radius:var(--radius-sm);background:transparent;color:var(--text-meta)}",
        ".l-more:hover{background:var(--inset)}",
        ".l-more .rs-chev{transform:rotate(-90deg)}",
        ".l-more.on .rs-chev{transform:none}",
        ".bars{grid-area:strip;justify-self:start;display:flex;align-items:flex-end;height:46px;max-width:100%;border-bottom:1px solid var(--edge-strong)}",
        ".bar{position:relative;display:block;flex:1 1 0;min-width:4px;height:100%;min-height:0;padding:0;border:0;border-radius:3px 3px 0 0;background:var(--share-track);box-shadow:none}",
        ".bar>.fill{position:absolute;left:0;right:0;bottom:0;border-radius:2px 2px 0 0;background:var(--share-ink)}",
        ".bar:hover:not(:disabled)>.fill{filter:brightness(.82)}",
        ".bar.last>.fill{background:rgba(var(--neutral-rgb),.07) var(--hatch);box-shadow:inset 0 1.5px 0 var(--share-ink)}",
        ".bar.held>.fill{background:var(--status-warn)}",
        ".bar.held.last>.fill{background:transparent var(--hatch-warn);box-shadow:inset 0 1.5px 0 var(--status-warn)}",
        ".bar.spent{box-shadow:inset 0 -3px 0 var(--status-bad)}",
        ".bar.unknown{background:transparent;box-shadow:inset 0 0 0 1px rgba(var(--neutral-rgb),.32)}",
        ".bar.unknown:after{content:\"?\";position:absolute;left:0;right:0;bottom:4px;text-align:center;color:var(--text-meta);font-size:var(--type-meta);line-height:1}",
        ".bar.sel{outline:2px solid var(--text-primary);outline-offset:1px;z-index:1}",
        ".bar:disabled{cursor:default}",
        ".strip-legend .bar{display:inline-block;width:12px;height:16px;flex:none}",
        ".acct-lims{margin-top:var(--space-2)}",
        ".acct-lim{display:grid;grid-template-columns:minmax(88px,140px) 64px 44px minmax(0,1fr);align-items:center;column-gap:var(--space-3);margin-top:6px;font-size:var(--type-meta)}",
        ".acct-lim .k{color:var(--text-meta);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
        ".acct-lim .m{position:relative;height:5px;border-radius:3px;background:var(--share-track);overflow:hidden}",
        ".acct-lim .m>i{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink)}",
        ".acct-lim .m.last>i{background:rgba(var(--neutral-rgb),.07) var(--hatch)}",
        ".acct-lim .m.held>i{background:var(--status-warn)}",
        ".acct-lim .m.none{background:transparent}",
        ".acct-lim .p{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-primary)}",
        ".acct-lim .p.bad{color:var(--bad-text);font-weight:600}",
        ".acct-lim .p.meta{color:var(--text-meta)}",
        ".acct-lim .r{min-width:0;color:var(--text-meta);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}",
        ".acct-lim .r .warn{color:var(--warn-text)}",
        ".acct-lim .r .est{color:var(--pace-ink)}",
        ".chart-heading{display:flex;align-items:baseline;flex-wrap:wrap;gap:0 4px;min-width:0}",
        ".chart-title{font-weight:600}",
        ".chart-heading .chart-sub{margin:0}",
        ".chart-svg .area-observed,.chart-legend .area-observed{fill:var(--area)}",
        // A stretch with no record is hatched, so it never reads as a value.
        ".chart-svg .gap-band,.chart-legend .gap-band{fill:url(#cq-gap-hatch);stroke:rgba(var(--neutral-rgb),.14);stroke-width:1;stroke-dasharray:2 3}",
        ".gap-hatch-line{stroke:rgba(var(--neutral-rgb),.16);stroke-width:1}",
        ".chart-svg .future-bg{fill:rgba(var(--neutral-rgb),.025)}",
        ".line-observed.soft{stroke:rgba(var(--neutral-rgb),.45);stroke-width:1.5}",
        ".line-cohort{fill:none;stroke:var(--text-primary);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}",
        ".line-pace{stroke-dasharray:5 4}",
        ".line-refill{fill:none;stroke:var(--pace-ink);stroke-width:1.5;stroke-dasharray:2 4;opacity:.6;stroke-linecap:round}",
        ".chart-svg .cursor-dot.line-cohort{fill:var(--text-primary)}",
        ".chart-svg .cursor-dot.line-refill{fill:var(--pace-ink)}",
        ".legend-toggle{min-height:0;padding:1px 6px;margin:-1px 0;border:1px dashed var(--edge-strong);border-radius:var(--radius-sm);background:transparent;color:var(--text-meta);font-size:var(--type-meta)}",
        ".legend-toggle.on{border-style:solid;color:var(--text-primary);background:var(--inset)}",
        ".chart-facts>div:first-child{font-size:var(--type-body);color:var(--text-primary)}",
        ".fact-muted{color:var(--text-meta)}",
        "@media(max-width:520px){body{padding:var(--space-3)}.quotas-container{grid-template-columns:minmax(0,1fr)}.account-label{display:none}.acct-btn .meter,.acct-btn .acct-count{display:none}.acct-alarm-words{display:none}.reserve-detail{grid-template-columns:minmax(0,1fr);gap:2px}.reserve-detail dd{margin-bottom:var(--space-2)}.account-toolbar{flex-wrap:wrap}.acct-wrap{flex-basis:200px}.rs-unit{display:none}.rs-accts{gap:6px}.rs-fig{font-size:var(--type-body)}.rs-name{font-size:var(--type-body)}.reserve-row{column-gap:var(--space-2)}.lrow{grid-template-areas:\"name fig more\" \"sub sub more\" \"strip strip more\" \"tail tail more\";column-gap:var(--space-2);padding:10px 4px}.l-sub{justify-self:start;text-align:left;white-space:normal}.bars{justify-self:stretch;width:auto!important;height:40px}.acct-lim{grid-template-columns:72px 40px 40px minmax(0,1fr);column-gap:var(--space-2)}.reserve-head{display:block}.reserve-age{text-align:left;margin-top:2px}}",
        "@media(max-width:360px){.reserve-row{grid-template-areas:\"name name chev\" \"fig avg chev\" \"strip strip chev\" \"tail tail chev\"}.rs-fig{text-align:left}.foot-btns{margin-left:0}.acct-wrap{flex-basis:160px}}",
    ].join('');

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null && text !== '') node.textContent = String(text);
        return node;
    }



    // Icons are drawn, not typed: an emoji renders in the host OS font and looks
    // different on every machine, and the widget frame forbids loading an icon
    // font. These are stroke paths that inherit the surrounding text colour.
    var ICON_PATHS = {
        info: ['M12 21a9 9 0 100-18 9 9 0 000 18z', 'M12 11v5', 'M12 8h.01'],
        warn: ['M10.3 4.3L2.6 17.5A2 2 0 004.3 20.5h15.4a2 2 0 001.7-3L13.7 4.3a2 2 0 00-3.4 0z', 'M12 9v4', 'M12 17h.01'],
        error: ['M12 21a9 9 0 100-18 9 9 0 000 18z', 'M15 9l-6 6', 'M9 9l6 6'],
        refresh: ['M20.5 12a8.5 8.5 0 11-2.6-6.1', 'M20.5 4.5V10h-5.5'],
        signal: ['M5 19v-6', 'M12 19V5', 'M19 19v-9'],
        caret: ['M6 9l6 6 6-6'],
        // Sliders rather than a cog: the button changes how much of the list
        // is shown, not what the widget is allowed to do.
        density: ['M4 7h16', 'M4 12h16', 'M4 17h16',
                  'M9 5v4', 'M15 10v4', 'M7 15v4'],
        // A row with lines inside it: the tab decides how much of a row is
        // unfolded, and "signal" next to it is the state of the machine.
        rows: ['M4 5h16v14H4z', 'M8 10h8', 'M8 14h5'],
        // A funnel: the tab decides what passes through into a row, and lets
        // the rest by. Nothing is thrown away, only kept out of the list.
        filter: ['M4 5h16l-6.2 7.4v5.3l-3.6 1.8v-7.1z'],
        // Rows folding into a chevron: this tab decides which rows leave the
        // list and gather under one of them.
        fold: ['M4 6h16', 'M4 10h16', 'M8 14l4 4 4-4'],
        // A line over time: the button that unfolds the reserve chart.
        chart: ['M4 19h16', 'M5 15l4-5 4 3 6-7']
    };

    // Family marks are the vendors' own: a widget that names an account's CLI
    // and then draws a shape of its own invention makes the reader guess. These
    // are filled marks, not stroked outlines, so they take their own renderer.
    // Vendor marks, monochrome, each in the grid its owner drew it on. Codex,
    // Claude, Cursor and OpenCode are copied byte-for-byte from the host's own
    // list of harness marks (web/modules/harness_presentation.js), which in turn
    // carries them from Claudexor's HarnessLogoData.swift: Claude, Cursor and
    // OpenCode from Simple Icons, Codex from SVGL. Antigravity is the silhouette
    // of the SVGL mark, the same source Codex came from.
    //
    // The paths are untouched; only the frame around each one is computed, from
    // the shape itself rather than from the grid it was published on, because
    // not every mark sits in the middle of the grid it was published on —
    // Antigravity rides high in its 16-by-15 — and at this size that is most of
    // a pixel, which is the crookedness the row was pulled up on. The frames
    // account for the arcs, not only the points the path names: the Codex mark
    // bulges past its own listed points at the top, and a frame drawn to those
    // points clipped its crown. Product names and marks remain the property of
    // their owners.
    var BRAND_MARKS = Object.assign(Object.create(null), {
        codex: { viewBox: '-1.76 0 259.52 259.52', path: 'M239.184 106.203a64.716 64.716 0 0 0-5.576-53.103C219.452 28.459 191 15.784 163.213 21.74A65.586 65.586 0 0 0 52.096 45.22a64.716 64.716 0 0 0-43.23 31.36c-14.31 24.602-11.061 55.634 8.033 76.74a64.665 64.665 0 0 0 5.525 53.102c14.174 24.65 42.644 37.324 70.446 31.36a64.72 64.72 0 0 0 48.754 21.744c28.481.025 53.714-18.361 62.414-45.481a64.767 64.767 0 0 0 43.229-31.36c14.137-24.558 10.875-55.423-8.083-76.483Zm-97.56 136.338a48.397 48.397 0 0 1-31.105-11.255l1.535-.87 51.67-29.825a8.595 8.595 0 0 0 4.247-7.367v-72.85l21.845 12.636c.218.111.37.32.409.563v60.367c-.056 26.818-21.783 48.545-48.601 48.601Zm-104.466-44.61a48.345 48.345 0 0 1-5.781-32.589l1.534.921 51.722 29.826a8.339 8.339 0 0 0 8.441 0l63.181-36.425v25.221a.87.87 0 0 1-.358.665l-52.335 30.184c-23.257 13.398-52.97 5.431-66.404-17.803ZM23.549 85.38a48.499 48.499 0 0 1 25.58-21.333v61.39a8.288 8.288 0 0 0 4.195 7.316l62.874 36.272-21.845 12.636a.819.819 0 0 1-.767 0L41.353 151.53c-23.211-13.454-31.171-43.144-17.804-66.405v.256Zm179.466 41.695-63.08-36.63L161.73 77.86a.819.819 0 0 1 .768 0l52.233 30.184a48.6 48.6 0 0 1-7.316 87.635v-61.391a8.544 8.544 0 0 0-4.4-7.213Zm21.742-32.69-1.535-.922-51.619-30.081a8.39 8.39 0 0 0-8.492 0L99.98 99.808V74.587a.716.716 0 0 1 .307-.665l52.233-30.133a48.652 48.652 0 0 1 72.236 50.391v.205ZM88.061 139.097l-21.845-12.585a.87.87 0 0 1-.41-.614V65.685a48.652 48.652 0 0 1 79.757-37.346l-1.535.87-51.67 29.825a8.595 8.595 0 0 0-4.246 7.367l-.051 72.697Zm11.868-25.58 28.138-16.217 28.188 16.218v32.434l-28.086 16.218-28.188-16.218-.052-32.434Z' },
        claude: { viewBox: '0 0 24 24', path: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z' },
        cursor: { viewBox: '0 0 24 24', path: 'M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23' },
        opencode: { viewBox: '0 0 24 24', path: 'M22 24H2V0h20zM17 4.8H7v14.4h10z' },
        agy: { viewBox: '0 -0.61 15.53 15.53', path: 'M14.0777 13.984C14.945 14.6345 16.2458 14.2008 15.0533 13.0084C11.476 9.53949 12.2349 0 7.79033 0C3.34579 0 4.10461 9.53949 0.527295 13.0084C-0.773543 14.3092 0.635692 14.6345 1.50293 13.984C4.86344 11.7076 4.64663 7.69664 7.79033 7.69664C10.934 7.69664 10.7172 11.7076 14.0777 13.984Z' }
    });

    var SVG_NS = 'http://www.w3.org/2000/svg';

    // Every drawing here stands beside words that already say the same thing,
    // so all of them are set up the same way and all of them are kept out of
    // the reading order.
    function svgCanvas(viewBox, width, height) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', viewBox);
        svg.setAttribute('width', String(width));
        svg.setAttribute('height', String(height));
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        return svg;
    }

    function svgIcon(paths, size, filled, viewBox) {
        var svg = svgCanvas(viewBox || '0 0 24 24', size, size);
        if (filled) {
            svg.setAttribute('fill', 'currentColor');
        } else {
            svg.setAttribute('fill', 'none');
            svg.setAttribute('stroke', 'currentColor');
            svg.setAttribute('stroke-width', '1.8');
            svg.setAttribute('stroke-linecap', 'round');
            svg.setAttribute('stroke-linejoin', 'round');
        }
        paths.forEach(function (d) {
            var path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function icon(name, size) {
        return svgIcon(ICON_PATHS[name] || [], size || 14, false);
    }

    // Vendor marks are filled shapes where icon() strokes: they take a fill and
    // no stroke. Two names rather than one function with a flag — a bare "true"
    // at the call site says nothing about what it switches. Each mark also
    // brings its own grid, so nothing is squeezed into a square it was not
    // drawn for.
    function brandIcon(name, size) {
        var mark = BRAND_MARKS[name];
        return svgIcon([mark.path], size, true, mark.viewBox);
    }
    // What the three row-detail choices differ in is one thing: how many lines
    // of explanation open under the bars. The preview draws that and nothing
    // else — a state dot, a stub of a name, the window bars, and beneath them
    // as many lines as the choice unfolds. No labels: the words are already
    // under the choice's name, and no real figures either, since a row's true
    // length depends on the account, and promising a shape here would be a
    // claim the settings panel cannot keep.
    var DENSITY_PREVIEW = {
        compact: [],
        normal: [[8, 10, 20, 1], [8, 14.5, 14, 0]],
        detailed: [[8, 10, 20, 1], [8, 14.5, 24, 1], [8, 19, 16, 0], [8, 23.5, 21, 0]]
    };

    function densityPreview(key) {
        var svg = svgCanvas('0 0 44 28', 44, 28);
        svg.setAttribute('class', 'dens-pv');
        function box(x, y, w, h, cls, r) {
            var node = document.createElementNS(SVG_NS, 'rect');
            node.setAttribute('x', String(x));
            node.setAttribute('y', String(y));
            node.setAttribute('width', String(w));
            node.setAttribute('height', String(h));
            node.setAttribute('rx', String(r));
            node.setAttribute('class', cls);
            svg.appendChild(node);
        }
        var dot = document.createElementNS(SVG_NS, 'circle');
        dot.setAttribute('cx', '4');
        dot.setAttribute('cy', '5.2');
        dot.setAttribute('r', '2');
        dot.setAttribute('class', 'pv-dot');
        svg.appendChild(dot);
        box(8, 4, 4, 2.5, 'pv-name', 1);
        box(14, 4, 9, 2.5, 'pv-bar spent', 1.25);
        box(25, 4, 7, 2.5, 'pv-bar', 1.25);
        box(34, 4, 6, 2.5, 'pv-bar', 1.25);
        (DENSITY_PREVIEW[key] || []).forEach(function (line) {
            box(line[0], line[1], line[2], 1.8, 'pv-line' + (line[3] ? ' strong' : ''), 0.9);
        });
        return svg;
    }

    function withIcon(node, name, size) {
        node.insertBefore(icon(name, size), node.firstChild);
        return node;
    }

    // Colour alone says nothing to a screen reader, so a visible dot is either
    // paired with a word or explicitly hidden from the reading order by whoever
    // puts the state into a label of its own.
    function stateDot(tone, spoken) {
        var dot = el('span', 'state-dot ' + tone);
        if (!spoken) dot.setAttribute('aria-hidden', 'true');
        return dot;
    }

    // The whole widget speaks one language for state: a dot and a quiet word
    // beside it, never a coloured capsule.
    function dotLabel(text, tone) {
        var wrap = el('span', 'dot-label');
        wrap.appendChild(stateDot(tone || 'muted', true));
        wrap.appendChild(document.createTextNode(text));
        return wrap;
    }

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

    function pad2(n) {
        return (n < 10 ? '0' : '') + n;
    }

    // Owner's call: show WHEN the window resets rather than how long is left.
    // The per-second countdown that used to live here is gone with its timer —
    // a date needs no ticking, and a timer that finds nothing is worse than none.
    function formatResetAt(iso) {
        if (!iso) return '';
        var at = new Date(String(iso));
        if (isNaN(at.getTime())) return '';
        return at.getDate() + ' ' + MONTHS[at.getMonth()] + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    // relTime speaks ISO because that is what the engine sends. The two places
    // that ask about a moment of the widget's own were each turning a number
    // into ISO and straight back; that conversion lives here now — and refuses
    // a number no calendar accepts rather than throwing on it.
    function relSince(ms) {
        var at = new Date(ms);
        return isFinite(at.getTime()) ? relTime(at.toISOString()) : '';
    }

    function relTime(iso) {
        if (!iso) return '';
        var at = Date.parse(String(iso));
        if (!isFinite(at)) return '';
        // Which side of now it falls on is read before the rounding, not
        // after: half a minute into the past rounds to zero minutes, and zero
        // minutes used to count as the future — "in a moment" for something
        // that already happened.
        var delta = at - Date.now();
        var mins = Math.round(Math.abs(delta) / 60000);
        var future = delta >= 0;
        var body;
        if (mins <= 1) body = 'a moment';
        else if (mins < 60) body = mins + 'm';
        else if (mins < 2880) body = Math.round(mins / 60) + 'h';
        else body = Math.round(mins / 1440) + 'd';
        return future ? ('in ' + body) : (body + ' ago');
    }

    function facetTone(state) {
        if (state === 'ok') return 'ok';
        if (state === 'failed') return 'bad';
        if (state === 'not_read') return 'warn';
        return 'muted';
    }

    function facetWord(state) {
        if (state === 'ok') return 'read';
        if (state === 'not_read') return 'not read';
        if (state === 'failed') return 'failed';
        return 'indeterminate';
    }

    function quotaClass(state) {
        if (state === 'exhausted') return 'quota-primary-text exhausted';
        if (state === 'cooling') return 'quota-primary-text cooling';
        if (state === 'ok') return 'quota-primary-text okstate';
        return 'quota-primary-text unknown';
    }

    function hasPct(value) {
        return value !== null && value !== undefined;
    }

    // Whether a window is at its limit is the skill's verdict on the
    // unrounded share (at_limit), never a rounded percent: 99.6% prints as
    // "99.6%", and is not spent. An answer without the verdict (an older
    // skill) falls back to the percent it sent.
    function atLimit(view) {
        if (!view) return false;
        if (typeof view.at_limit === 'boolean') return view.at_limit;
        return hasPct(view.used_pct) && view.used_pct >= 100;
    }

    // The share used, in the skill's own words for it ("58", "99.6", "<100"):
    // rounding is for the eye, and done once, by the skill, which knows
    // whether the share is full.
    function pctText(view) {
        if (!view || !hasPct(view.used_pct)) return '';
        if (typeof view.used_text === 'string' && view.used_text) return view.used_text;
        return String(view.used_pct);
    }

    // One bar language for the whole widget, the reserve's: a bar is as long
    // as the share LEFT, on one 0–100% scale, in the same neutral ink, over a
    // hairline base that spans the whole scale — no coloured track and no
    // usage thresholds. Amber is a share held back now (a cooldown, a spent
    // limit elsewhere on the account, an account that cannot run), a share at
    // its limit has no fill but a red base, and a last-known reading is muted
    // and claims neither. No number, no bar: the words beside it say why, and
    // an empty bar would read as "0% left". The size is the only difference —
    // full width in a tile, short on the button and in a row, "avg" in a
    // crowded reserve row.
    function meter(leftPct, how) {
        if (typeof leftPct !== 'number' || !isFinite(leftPct)) return null;
        how = how || {};
        var bar = el('span', 'meter' + (how.size ? ' ' + how.size : '')
            + (how.stale ? ' stale' : (how.spent ? ' spent' : (how.held ? ' restricted' : ''))));
        bar.setAttribute('aria-hidden', 'true');
        var left = Math.max(0, Math.min(100, leftPct));
        var spent = !how.stale && !!how.spent;
        // An average says what it is on its own strip; "N% left" here would
        // read as one account's share.
        if (!how.untitled) {
            bar.title = exactPct(left, spent || left <= 0) + '% left' + (spent ? ' — at the limit' : '')
                + (how.stale ? ' — last known' : (!spent && how.held ? ' — held back now' : ''));
        }
        // The length as given, not rounded to a tenth that would turn 0.04%
        // into nothing; a share at its limit has no fill at all.
        if (left > 0 && !spent) {
            var fill = el('span', 'meter-fill');
            fill.style.width = +left.toFixed(4) + '%';
            bar.appendChild(fill);
        }
        return bar;
    }

    // Held back as a whole, in the words the reserve's "restricted" uses: the
    // skill's verdict is a cooldown or a spent shared limit, or the account
    // cannot run at all (no login, switched off, a failed check).
    function accountHeld(account) {
        if (!account) return false;
        var state = (account.quota || {}).state;
        return state === 'cooling' || state === 'exhausted' || !!foldReason(account);
    }

    // Held back by a hold the skill carried apart, by the reserve's own rules
    // for its "cooling" and "model_exhausted" restrictions: a cooldown on the
    // whole account holds every window; a cooldown on some models, or a model
    // limit the engine reports out until a reset still ahead, holds only the
    // windows of exactly those models (the whole scope, by scope_key). The
    // number drawn may come from a newer source than the hold, so the
    // window's own cooldown_until is not enough.
    function heldByScope(view, quota) {
        if (!view) return false;
        return cooldownsOf(quota, 'account').length > 0 || modelScopeHeld(view, quota);
    }

    // Only the model holds, for one model window.
    function modelScopeHeld(view, quota) {
        return !!scopeHoldOf(view, quota);
    }

    // One window of an account: held with the account, or by a hold that
    // covers this window.
    function windowHeld(account, view) {
        return accountHeld(account) || heldByScope(view, (account || {}).quota);
    }

    // The model-scoped exhaustions the skill carried (quota.model_exhaustions),
    // each as the engine reported it; `live` only while its reset is ahead.
    function exhaustionsOf(quota) {
        return ((quota || {}).model_exhaustions || []).filter(function (e) { return !!e; });
    }

    function liveExhaustions(quota) {
        return exhaustionsOf(quota).filter(function (e) { return e.live === true; });
    }

    // A hold names a model scope only with the skill's own identity of one:
    // '-' is "no model named" and '' an answer that carried none. Such a hold
    // can match no window, and the reserve counts it against none.
    function isModelScope(key) {
        return !!key && key !== '-';
    }

    // Every model scope held now, apart from the windows, each keeping its
    // own kind: a cooldown on some models, or a model limit reported out
    // until a reset still ahead. A cooldown is not a limit reached. A live
    // exhaustion that names no model holds nothing — the reserve does not
    // count it — and is only disclosed (renderExhaustions), never a hold.
    function modelHolds(quota) {
        var out = cooldownsOf(quota, 'models').map(function (c) {
            return { kind: 'cooldown', key: c.scope_key || '', models: c.models || [],
                     omitted: c.models_omitted || 0, until: c.until || '', note: c.until_note || '',
                     stale: !!c.freshness && c.freshness !== 'fresh' };
        });
        liveExhaustions(quota).forEach(function (e) {
            out.push({ kind: 'limit', key: e.scope_key || '', models: e.models || [],
                       omitted: e.models_omitted || 0, until: e.resets_at || '', note: e.reset_note || '',
                       stale: !!e.freshness && e.freshness !== 'fresh' });
        });
        return out.filter(function (h) { return isModelScope(h.key); });
    }

    // The hold a model window stands under, apart from its own cooldown:
    // the first one on exactly its scope — a cooldown before a limit, since
    // a cooldown's end is the nearer fact. Null for a shared window, or one
    // no hold names.
    function scopeHoldOf(view, quota) {
        var scope = view && view.scope_key;
        if (!isModelScope(scope) || !isModelWindow(view)) return null;
        var mine = modelHolds(quota).filter(function (h) { return h.key === scope; });
        return mine.filter(function (h) { return h.kind === 'cooldown'; })[0] || mine[0] || null;
    }

    // One window's share left. At its limit is the skill's verdict; a
    // window's own cooldown holds it back like the account's; a last-known
    // reading claims neither.
    function windowMeter(view, how) {
        if (!view || !hasPct(view.used_pct)) return null;
        how = how || {};
        var stale = !!how.stale;
        var spent = !stale && atLimit(view);
        return meter(100 - view.used_pct, {
            size: how.size,
            stale: stale,
            spent: spent,
            held: !stale && !spent && (!!how.held || isCooling(view) || unreadableCooldown(view))
        });
    }

    // A failed check arrives as tone 'warn' — 'bad' is never emitted by
    // verification_view, so testing for it alone matched nothing.
    function verificationFailed(account) {
        var tone = (account.verification || {}).tone;
        return tone === 'warn' || tone === 'bad';
    }

    // Three questions, deliberately different answers, and all three are asked
    // about the same row of the selector — so the differences are written here
    // rather than being inferred from the call sites.
    //
    //   isAlertAccount — "calls for a look": spent quota, cooldown, a failed
    //   check, a switched-off account. It feeds the dot and the "N need
    //   attention" badge. An account nobody has logged into is NOT in it:
    //   there is nothing to attend to, only something to set up.
    //
    //   isTroubled — "something to fix": not signed in, switched off, or the
    //   check failed. It decides whether the engine's own explanation is worth
    //   a line. A stale reading is in neither: nobody has to do anything.
    //
    //   foldReason — "why it is hidden": the same three states as isTroubled,
    //   but named one at a time, because the fold puts each under its own
    //   heading and the reader switches them on and off separately.

    // The key for one account, or '' when nothing is wrong. The order is the
    // order a person would explain it in: with no login there is nothing to say
    // about a check, and a switched-off account is the reader's own doing
    // before it is anything else. troubleReason turns the key into words.
    function foldReason(account) {
        if (!account.signed_in) return 'signed_out';
        if (account.enabled === false) return 'disabled';
        if (verificationFailed(account)) return 'failed';
        return '';
    }

    // The same trouble in the engine's own words — or '' when there is none.
    // The yes/no question and the word for the screen both come from the key
    // above, so the two can never answer differently.
    function troubleReason(account) {
        var reason = foldReason(account);
        if (reason === 'signed_out') return 'not signed in';
        if (reason === 'disabled') return 'disabled';
        if (reason === 'failed') {
            // The engine always sends a label with the tone — but if it ever
            // sends the tone alone, an empty string here would make a failed
            // check look like a healthy account, which is the one thing this
            // widget must never do.
            return (account.verification || {}).label || 'verification failed';
        }
        return '';
    }

    // The list in two: what works, in the order the engine sent it, and what
    // does not, gathered by reason. A reason the reader switched off is not a
    // reason at all here — those accounts stay upstairs where they were.
    // Whether folding works at all right now. While the accounts facet is
    // unread, every state on this screen is last known — and the engine answers
    // a failed check with a muted tone then, so that one reason would quietly
    // stop folding while the other two carried on. Nothing folds until the
    // facet answers again. The list asks this, and so does the Accounts tab,
    // which has to say why its switches change nothing meanwhile.
    function foldPaused(facets) {
        return !!(facets && facets.accounts && facets.accounts !== 'ok');
    }

    function splitAccounts(accounts, facets) {
        var live = [];
        var byReason = {};
        var folded = [];
        // The banner above already says why the list looks different.
        var unread = foldPaused(facets);
        (accounts || []).forEach(function (candidate) {
            var reason = unread ? '' : foldReason(candidate);
            if (!reason || !foldChoices[reason]) {
                live.push(candidate);
                return;
            }
            if (!byReason[reason]) byReason[reason] = [];
            byReason[reason].push(candidate);
        });
        FOLD_REASONS.forEach(function (reason) {
            if (byReason[reason]) {
                folded.push({
                    reason: reason,
                    title: FOLD_TITLE[reason],
                    accounts: byReason[reason]
                });
            }
        });
        return { live: live, folded: folded };
    }

    function foldedCount(folded) {
        var total = 0;
        folded.forEach(function (section) { total += section.accounts.length; });
        return total;
    }

    function holdsSelected(folded) {
        return folded.some(function (section) {
            return section.accounts.some(function (candidate) {
                return candidate.key === selectedAccountKey;
            });
        });
    }

    function isTroubled(account) {
        return !!troubleReason(account);
    }

    function isAlertAccount(account) {
        var hasCooldown = false;
        liveWindows(account).forEach(function (c) {
            // Honesty rule 4: a per-model cap never marks the whole account.
            // The cooldown branch used to ignore that and did mark it.
            if (isModelWindow(c)) return;
            if (isCooling(c)) hasCooldown = true;
        });
        // "cooling": a cooldown holds the whole account, whichever reading
        // reported it (a source whose number is not the one drawn, a stale
        // one) — the skill decides that, and says it in the state.
        return (account.quota && (account.quota.state === 'exhausted'
                                  || account.quota.state === 'cooling')) ||
               verificationFailed(account) ||
               hasCooldown ||
               account.enabled === false;
    }

    function liveWindows(account) {
        return ((account.quota || {}).constraints || []);
    }

    // The widget runs in an opaque-origin sandbox: every browser store throws
    // there, so a choice kept in one is silently forgotten. The skill keeps it
    // instead, in its own state directory, and hands it back with the reading.
    function applyPrefs(prefs) {
        if (prefsInFlight) return;
        keepPrefs(prefs);
    }

    // What the skill says it keeps, drawn as the reader's choice. Readings go
    // through applyPrefs above; the newest save's own answer comes here
    // directly, since the older saves still in the air no longer speak for it.
    function keepPrefs(prefs) {
        if (!prefs || typeof prefs !== 'object') return;
        density = DENSITY_OPTIONS.some(function (o) { return o.key === prefs.density; })
            ? prefs.density : 'normal';
        modelChoices = (prefs.models && typeof prefs.models === 'object'
            && !Array.isArray(prefs.models)) ? prefs.models : {};
        var fold = (prefs.fold && typeof prefs.fold === 'object'
            && !Array.isArray(prefs.fold)) ? prefs.fold : {};
        foldChoices = foldDefaults();
        FOLD_REASONS.forEach(function (reason) {
            // Anything that is not a plain yes or no leaves the default
            // standing: a missing answer is not the reader answering "no".
            if (typeof fold[reason] === 'boolean') foldChoices[reason] = fold[reason];
        });
    }

    // The screen changes at once and the skill catches up: waiting for a round
    // trip before redrawing would make every choice feel like it stuck.
    function savePrefs() {
        prefsInFlight++;
        var seq = ++prefsSeq;
        saveError = '';
        // All three choices travel together: the skill keeps them in one file
        // and writes what it is given, so a save that left one out would wipe
        // it rather than leave it alone. The order travels with them and is
        // never stored.
        var body = JSON.stringify({
            density: density, models: modelChoices, fold: foldChoices,
            frame: PREFS_FRAME, seq: seq
        });
        var request = Promise.resolve().then(function () {
            return window.fetch(PREFS_ROUTE, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body,
                timeoutMs: PREFS_TIMEOUT_MS
            });
        }).then(function (response) {
            return response.text();
        }).then(function (text) {
            var answer = null;
            try { answer = JSON.parse(text); } catch (err) { answer = null; }
            prefsInFlight = Math.max(0, prefsInFlight - 1);
            if (!prefsInFlight) prefsSaving = null;
            settleSave(seq, (answer && answer.error) ? String(answer.error)
                : (answer ? '' : 'the skill answered with something unreadable'),
                answer && answer.prefs);
            rerender();
        })['catch'](function (err) {
            prefsInFlight = Math.max(0, prefsInFlight - 1);
            if (!prefsInFlight) prefsSaving = null;
            settleSave(seq, (err && err.message) ? err.message : 'the skill could not be reached', null);
            rerender();
        });
        prefsSaving = prefsSaving ? Promise.all([prefsSaving, request]) : request;
    }

    // One save's answer. The newest save's decides the warning and what is
    // drawn: what the skill kept, not what we sent — if it refused a value,
    // the screen must show the refusal rather than the wish.
    // An older save that answers after it would put back a choice the reader
    // has already changed, so its choices are never drawn; its failure is
    // still said while the newest has not answered, and once the newest has,
    // that answer (a full copy of every choice) is what stands.
    function settleSave(seq, error, kept) {
        if (seq === prefsSeq) {
            prefsAnswered = seq;
            saveError = error;
            if (kept) keepPrefs(kept);
        } else if (error && prefsAnswered < prefsSeq) {
            saveError = error;
        }
    }

    // The frame is disposable: the host removes it on Stop, on leaving the page
    // and when the skill's revision changes, and gives its dispose hooks up to a
    // second first. Every choice is saved the moment it is made, so what can
    // still be lost is a save in the air — the hook waits for that, and never
    // past the host's second.
    function flushPrefs() {
        if (!prefsSaving) return undefined;
        return Promise.race([prefsSaving, new Promise(function (resolve) {
            window.setTimeout(resolve, 900);
        })]);
    }

    function modelView(harnessId) {
        var chosen = modelChoices[harnessId];
        var known = MODEL_VIEWS.some(function (key) { return key === chosen; });
        return known ? chosen : 'all';
    }

    function isModelWindow(view) {
        return !!(view && view.scoped_models && view.scoped_models.length);
    }

    // The list only. Which windows a row shows is a matter of taste; which
    // windows exist is not, and everything that answers "how is this account"
    // — the dot, the worst figure, the card — keeps reading all of them.
    function windowsForRow(account, harnessId) {
        var windows = liveWindows(account);
        var chosen = modelView(harnessId);
        if (chosen === 'all') return windows;
        var wantModels = chosen === 'models';
        return windows.filter(function (c) { return isModelWindow(c) === wantModels; });
    }

    function familyHasModelWindows(group) {
        return ((group || {}).accounts || []).some(function (account) {
            return liveWindows(account).some(isModelWindow);
        });
    }

    // The one model this family's windows are tied to, when there is exactly
    // one. Two of them and the general word is the honest one: "only Fable and
    // Opus" is a promise about a set that changes with the next reading, while
    // "only models" stays true whatever comes back.
    function familyModelName(group) {
        var seen = {};
        ((group || {}).accounts || []).forEach(function (account) {
            liveWindows(account).forEach(function (view) {
                if (!isModelWindow(view)) return;
                var label = modelLabel(view.scoped_models);
                if (label) seen[label] = 1;
            });
        });
        var names = Object.keys(seen);
        return names.length === 1 ? names[0] : '';
    }

    // Two pieces: what the button says, and what a screen reader hears. They
    // differ because the button has room for one word and a chip, and the
    // reader needs the whole sentence.
    function modelViewWords(key, modelName) {
        var thing = modelName || 'models';
        if (key === 'models') {
            return { name: modelName, plain: modelName ? 'only' : 'only models',
                     spoken: 'only the windows tied to ' + thing };
        }
        if (key === 'shared') {
            return { name: modelName, plain: modelName ? 'without' : 'without models',
                     spoken: 'every window except those tied to ' + thing };
        }
        return { name: '', plain: 'all', spoken: 'every window the account reported' };
    }

    // Window names run to 27 characters ("GPT-5.3-Codex-Spark primary") and
    // three of those do not fit a row, so a window is named by how long it
    // lasts. This is the table both directions of that question share.
    var WINDOW_UNITS = [
        [604800, 'week'], [86400, 'day'], [3600, 'hour'], [60, 'minute']
    ];

    // Same table as windowLength, asked the other way round — and asked about
    // the value, not the shape. A name of "3 day" beside window_seconds of a
    // week is two read values that disagree; dropping the name because it
    // merely looks like a length would hide the disagreement and print the
    // engine's own week as if nothing else had been read.
    function sameLength(text, seconds) {
        var words = WINDOW_UNITS.map(function (u) { return u[1]; }).join('|');
        var found = new RegExp('^(\\d+)\\s+(' + words + ')s?$', 'i')
            .exec(String(text || '').trim());
        if (!found) return false;
        var unit = found[2].toLowerCase();
        for (var i = 0; i < WINDOW_UNITS.length; i++) {
            if (WINDOW_UNITS[i][1] === unit) {
                return WINDOW_UNITS[i][0] * Number(found[1]) === seconds;
            }
        }
        return false;
    }

    // "7d" is how a log writes it, not how a person reads it. The unit has to
    // divide the length exactly — rounding once turned 36 hours into "2d" — and
    // a single one drops the number: "week", not "1 week".
    function windowLength(seconds) {
        if (typeof seconds !== 'number' || seconds <= 0) return '';
        for (var i = 0; i < WINDOW_UNITS.length; i++) {
            var size = WINDOW_UNITS[i][0];
            var word = WINDOW_UNITS[i][1];
            if (seconds % size) continue;
            var n = seconds / size;
            return n === 1 ? word : (n + ' ' + word + 's');
        }
        return '';
    }

    var ROLE_WORDS = ['primary', 'secondary', 'tertiary'];

    // Window names come in four shapes across the two engines:
    // "GPT-5.3-Codex-Spark primary" — model then role;
    // "7 day" — the length again, which the tile already prints on its own;
    // "7 day (Fable)" — the length plus the model it is scoped to;
    // "1 reset credit available" — a whole phrase and no length at all.
    // Taking "the last word" as the role read "5 hour" as model 5, role hour.
    // `pool` says whether the model is the name of a pool the window belongs
    // to: a name with a role ("codex primary") or a bracket ("7 day (Fable)").
    // A name with neither — a stand-in, a whole phrase, a length said another
    // way ("Weekly") — is a window in its own right, not a pool.
    function splitWindowName(label, seconds) {
        var length = windowLength(seconds);
        var text = String(label || '').trim();
        if (!text) return { model: length ? '' : 'Window Limit', role: '', pool: false };
        var parts = text.split(/\s+/);
        var role = ROLE_WORDS.indexOf(parts[parts.length - 1].toLowerCase()) !== -1
            ? parts.pop() : '';
        var rest = parts.join(' ');
        var bracket = /\(([^()]+)\)/.exec(rest);
        if (bracket) return { model: bracket[1], role: role, pool: true };
        // A name that only repeats the length is not worth a chip of its own —
        // but only when it repeats it exactly.
        if (length && sameLength(rest, seconds)) return { model: '', role: role, pool: false };
        // "primary" on its own leaves nothing behind once the role is taken;
        // with no length either, the tile would carry no name at all.
        if (!rest && !length) return { model: 'Window Limit', role: role, pool: false };
        return { model: rest, role: role, pool: !!role };
    }

    // The pool a window belongs to. Codex keeps two — "codex" and
    // "GPT-5.3-Codex-Spark", each with windows of its own — and the engine
    // names them only inside each window's label. A window scoped to a model
    // belongs to that model's pool. A window that names no pool is general:
    // the plain 5-hour and 7-day windows of Claude.
    function poolOf(view) {
        if (isModelWindow(view)) return modelLabel(view.scoped_models);
        var name = splitWindowName(view.label, view.window_seconds);
        return name.pool ? name.model : '';
    }

    function roleRank(view) {
        var role = splitWindowName(view.label, view.window_seconds).role;
        var rank = ROLE_WORDS.indexOf(role.toLowerCase());
        return rank === -1 ? ROLE_WORDS.length : rank;
    }

    // The window's length in seconds, or Infinity when none was reported —
    // the same guard windowLength keeps, so a window with no length sorts
    // last: it has no place on the clock.
    function knownLength(view) {
        var seconds = view.window_seconds;
        return typeof seconds === 'number' && seconds > 0 ? seconds : Infinity;
    }

    function compareText(a, b) {
        return a < b ? -1 : (a > b ? 1 : 0);
    }

    // Shorter first, then primary before secondary, then by name.
    function windowOrder(a, b) {
        var la = knownLength(a);
        var lb = knownLength(b);
        if (la !== lb) return la < lb ? -1 : 1;
        var ra = roleRank(a);
        var rb = roleRank(b);
        if (ra !== rb) return ra - rb;
        return compareText(String(a.label || ''), String(b.label || ''));
    }

    // The general pool — the one with no name — stands first; named pools
    // follow by name, case set aside, and two spellings of one name stand in
    // a fixed order of their own. So the order never depends on what the
    // engine sent first.
    function poolOrder(a, b) {
        var named = (a.pool ? 1 : 0) - (b.pool ? 1 : 0);
        if (named) return named;
        return compareText(a.pool.toLowerCase(), b.pool.toLowerCase())
            || compareText(a.pool, b.pool)
            || compareText(a.key || '', b.key || '');
    }

    // The engine hands the windows over in an order that changes from one
    // reading to the next, and it differs between accounts. Nothing here
    // keeps that order: windows are grouped by pool — the general pool first,
    // then pools by name — and stand by length inside each. Every drawing of
    // windows (the tiles, the row, the lines under it) reads these groups,
    // so they cannot disagree. A pool is grouped by its exact name: merging
    // spellings would hand the chip whichever spelling the engine sent first.
    // A model's pool is grouped by the skill's identity of its whole scope
    // (scope_key), never by its printed name: two scopes can print the same
    // "Fable" or "M00 +24", and one pool drawn for both would lend one
    // scope's hold to the other.
    function poolKey(view) {
        if (isModelWindow(view) && isModelScope(view.scope_key)) return 'scope:' + view.scope_key;
        return 'pool:' + poolOf(view);
    }

    function poolGroups(constraints) {
        var groups = [];
        var byPool = Object.create(null);
        (constraints || []).forEach(function (view) {
            var key = poolKey(view);
            if (!byPool[key]) {
                byPool[key] = { pool: poolOf(view), key: key, windows: [] };
                groups.push(byPool[key]);
            }
            byPool[key].windows.push(view);
        });
        groups.sort(poolOrder);
        groups.forEach(function (group) { group.windows.sort(windowOrder); });
        return groups;
    }

    function orderedWindows(constraints) {
        var out = [];
        poolGroups(constraints).forEach(function (group) {
            out.push.apply(out, group.windows);
        });
        return out;
    }

    // `scopeHeld`: a hold the skill carried apart (a model cooldown, a model
    // limit the engine reports out) covers exactly this window's models.
    function renderConstraint(view, stale, held, scopeHeld) {
        var card = el('div', 'quota-tile' + (stale ? ' stale' : ''));
        var pct = view.used_pct;
        var length = windowLength(view.window_seconds);
        var name = splitWindowName(view.label, view.window_seconds);
        // The engine's own name for this window. When the name is only the
        // length again the header drops it, and without this the string would
        // exist nowhere on the card at all.
        if (view.label) card.title = view.label;

        var header = el('div', 'tile-head');
        var left = el('span', 'tile-name');
        if (length) left.appendChild(el('span', 'tile-len', length));
        // The version is the point of the chip — a reader needs "GPT-5.3", not a
        // family word — so the name is never shortened here; it ellipsises when
        // the column is narrow and keeps the whole of itself in the title.
        var scoped = isModelWindow(view);
        // Only a model-scoped window carries a coloured name chip: red when its
        // measured share is at the limit — "this model is out", the one thing
        // a per-model cap says and a plain window does not — and amber when a
        // cooldown or a reported model limit holds it without that share being
        // spent. A scoped window at 10 %, or one with no ratio at all, is
        // neither. A stale reading claims nothing, and the account dot and the
        // reset lines read the same two words.
        var spent = !stale && scoped && isSpent(view);
        var cooling = !stale && scoped && !spent && (windowCooling(view) || !!scopeHeld);
        var chipTone = spent ? ' spent' : (cooling ? ' held' : '');
        if (name.model) {
            var chip = el('span', 'tile-model' + chipTone, name.model);
            chip.title = view.label || '';
            left.appendChild(chip);
        }

        // Red only at the limit, the skill's verdict — the bar below says the
        // same with its red base; a share merely high is not an alarm.
        var right = el('span', 'tile-pct' + (!stale && hasPct(pct) && atLimit(view) ? ' spent' : ''));
        // A ratio the skill refused (out of range, not a number) is said as
        // unreadable: it is not a share, and never a full one.
        right.textContent = hasPct(pct) ? (pctText(view) + '% used')
            : (view.ratio_problem ? 'Unreadable ratio' : 'Unmetered / No ratio');
        header.appendChild(left);
        header.appendChild(right);
        card.appendChild(header);

        var bar = windowMeter(view, { stale: stale, held: held });
        if (bar) card.appendChild(bar);

        var meta = el('div', 'tile-meta');
        var metaLeft = el('div', 'tile-foot');
        if (name.role) metaLeft.appendChild(el('span', 'tile-role', name.role));
        if (scoped) {
            // The header names the model when the engine put it in brackets
            // ("7 day (Fable)"), and printing it again 20px below said "Fable
            // Fable". Only the models the header did not name are listed here;
            // red on either of them means spent, never merely model-scoped.
            var shown = String(name.model || '').toLowerCase();
            var rest = view.scoped_models.filter(function (m) {
                return String(m).toLowerCase() !== shown;
            });
            if (rest.length) {
                var mWrap = el('span', 'model-chips-wrap');
                rest.slice(0, 2).forEach(function (m) {
                    var chipCls = 'model-chip' + (spent ? ' exhausted' : (cooling ? ' held' : ''));
                    mWrap.appendChild(el('span', chipCls, m));
                });
                if (rest.length > 2) {
                    mWrap.appendChild(el('span', 'model-chip', '+' + (rest.length - 2)));
                }
                mWrap.title = view.scoped_models.join(', ');
                metaLeft.appendChild(mWrap);
            }
        }
        // An empty foot or an empty when-line would still take a row's margin,
        // so neither is appended until it has something in it.
        if (metaLeft.firstChild) meta.appendChild(metaLeft);
        var when = renderWhen(view, stale);
        if (when.firstChild) meta.appendChild(when);
        if (meta.firstChild) card.appendChild(meta);
        // Read straight through, a tile says "week codex 100% used primary 27
        // Aug in 5d". As one label it names its window first, the way the
        // account rows in the selector already do.
        // textContent of a flex row runs its spans together — "27 Aug, 07:20in
        // 5d" — because the gap that separates them is layout, not text.
        var whenSaid = [];
        for (var w = 0; w < when.childNodes.length; w++) {
            var said = String(when.childNodes[w].textContent || '').trim();
            if (said) whenSaid.push(said);
        }
        card.setAttribute('aria-label', (view.label || name.model || 'Window limit')
            + ' — ' + right.textContent
            + (whenSaid.length ? ' — ' + whenSaid.join(' ') : ''));
        return card;
    }

    // A moment plus how far off it is. Returns false when the timestamp does
    // not parse, so callers can tell "nothing to say" from "said it". The
    // tone is 'bad' (a reset a spent share waits for; `true` says the same),
    // 'warn' (the end of a hold) or nothing.
    function appendStamp(wrap, iso, tone, withRel) {
        var cls = 'ticker' + (tone === true || tone === 'bad' ? ' bad' : (tone === 'warn' ? ' warn' : ''));
        // A value that was read but cannot be parsed is not a missing value.
        // formatResetAt returns '' for both, so the two used to look identical:
        // an unreadable timestamp printed nothing at all, which is the display
        // law of this widget read backwards.
        if (iso && !formatResetAt(iso)) {
            wrap.appendChild(el('span', cls, 'unreadable date'));
            return true;
        }
        var stamp = formatResetAt(iso);
        if (!stamp) return false;
        wrap.appendChild(el('span', cls, stamp));
        if (!withRel) return true;
        // Only for a moment still ahead. A reset date in the past means the
        // engine has not refreshed its snapshot, and "44h ago" beside a share
        // read as "the window should have reset and did not".
        if (Date.parse(iso) <= Date.now()) return true;
        var rel = relTime(iso);
        if (rel) wrap.appendChild(el('span', 'rel-time', rel));
        return true;
    }

    function isCooling(view) {
        return !!(view && view.cooldown_until
            && Date.parse(view.cooldown_until) > Date.now());
    }

    // The engine repeats a cooldown's own end as the resets_at of a bare
    // cooldown constraint (no window, no share). That instant is when the
    // cooldown lifts, not a reset: nothing refills then, so it is said once,
    // as the cooldown's end. A quota window's reset is always its own.
    function ownReset(view) {
        var reset = (view && view.resets_at) || '';
        if (reset && view.cooldown_until && !view.window_seconds && !hasPct(view.used_pct)
            && Date.parse(reset) === Date.parse(view.cooldown_until)) return '';
        return reset;
    }

    // A cooldown the engine sent with a date the widget cannot read. isCooling
    // says no to it; on its own that "no" read as "open".
    function unreadableCooldown(view) {
        return !!(view && view.cooldown_until && !formatResetAt(view.cooldown_until));
    }

    // A window's own cooldown, live or ending at a date nobody can read. Out
    // for now, in amber: a cooldown holds a share back, it does not spend it.
    function windowCooling(view) {
        return isCooling(view) || unreadableCooldown(view);
    }

    // Spent is the measured share at its limit — the skill's verdict — and
    // the only red. The account's own verdict keeps the two apart the same
    // way (plugin.py: "Limit reached" or "Cooling down"). A model window
    // cooling until a date nobody can read was once red here, as if spent.
    function isSpent(view) {
        return atLimit(view);
    }

    // Out for now, either way: at its limit (red) or cooling (amber). The
    // tile, the family mark, the reset lines and the pool's chip all ask this
    // one question, and then isSpent for the colour. When each asked its own
    // way, a model window with an unreadable cooldown was marked on the card
    // and neutral in the row.
    function isOut(view) {
        return isSpent(view) || windowCooling(view);
    }

    // Two halves of one answer: the moment, and how long that is from now.
    // Both are drawn once per redraw — nothing counts down, because a ticking
    // second-hand was already taken out of this widget once.
    //
    // A cooldown and a reset are two different facts, and the card used to
    // print both. Showing only the nearer one dropped the other: a window can
    // be cooling until tonight and still not reset until Sunday.
    function renderWhen(view, stale) {
        var wrap = el('div', 'tile-when');
        // A cooldown that cannot be parsed is still a cooldown the engine sent:
        // isCooling says no to it, and without this branch the row would drop
        // it in silence while plugin.py counts the same account as spent.
        var cooling = windowCooling(view);
        if (cooling) {
            wrap.appendChild(el('span', 'tile-when-word' + (stale ? '' : ' warn'), 'cooldown'));
            appendStamp(wrap, view.cooldown_until, stale ? '' : 'warn', true);
        }
        // With a cooldown already spelled out, the reset needs its date, not a
        // second relative reading beside the first. The word goes in only once
        // a date is known to follow it: the engine reports a cooldown with no
        // reset often enough, and "· resets" alone ends the line on a promise.
        var reset = el('span');
        if (appendStamp(reset, ownReset(view), false, !cooling)) {
            if (cooling) wrap.appendChild(el('span', 'rel-time', '· resets'));
            while (reset.firstChild) wrap.appendChild(reset.firstChild);
        }
        return wrap;
    }

    // The account's overall verdict — "Limit reached", and when it lifts. It
    // used to run as a full-width band under the caption, which cost a line and
    // put the answer below the question; it now stands opposite the name.
    function renderQuotaVerdict(quota) {
        var p = el('div', 'quota-primary-row');
        var textSpan = el('span', quotaClass(quota.state));
        textSpan.textContent = quota.label || 'No quota data reported';
        p.appendChild(textSpan);

        if (quota.state === 'cooling') {
            // A cooldown ends; nothing resets or refills then. With any end
            // unknown the skill sends none, and the cooldown lines say why.
            var untilWrap = el('span', 'quota-when', 'until ');
            if (appendStamp(untilWrap, quota.cooling_until, 'warn', true)) p.appendChild(untilWrap);
            return p;
        }
        var resetWrap = el('span', 'quota-when', 'Resets ');
        if (appendStamp(resetWrap, quota.resets_at, quota.state === 'exhausted', true)) {
            p.appendChild(resetWrap);
        }
        return p;
    }

    function quotaObservedAt(account) {
        return String(((account || {}).quota || {}).observed_at || '');
    }

    function retryAction(retryAt) {
        var at = Date.parse(String(retryAt || ''));
        if (!isFinite(at) || at <= Date.now()) return '';
        return 'Retry after ' + Math.max(1, Math.ceil((at - Date.now()) / 60000)) + 'm';
    }

    function absenceAction(account, absence) {
        if (!absence) return '';
        if (absence.action_kind === 'sign_in_if_unverified') {
            return account && account.verified_live ? '' : 'Sign-in required';
        }
        if (absence.action_kind === 'source_missing') return 'No live quota source';
        if (absence.action_kind === 'retry') return retryAction(absence.retry_at);
        return '';
    }

    function renderAbsence(parent, account, absence) {
        if (!absence) return;
        var row = el('div', 'quota-unavailable');
        row.appendChild(icon('warn', 13));
        row.appendChild(el('span', null, absence.message || 'Quota temporarily unavailable'));
        var action = absenceAction(account, absence);
        if (action) row.appendChild(el('span', 'quota-action', action));
        parent.appendChild(row);
    }

    // The cooldowns the skill carried as facts of their own (quota.cooldowns):
    // what each holds — the whole account or some models — until when, or
    // why that is not known, and whether a stale reading reported it. Never
    // "Limit reached" and never "Resets": a cooldown is not a share, and
    // nothing refills when it ends. An older answer has no list.
    function cooldownsOf(quota, scope) {
        return ((quota || {}).cooldowns || []).filter(function (c) {
            return !!c && (!scope || c.scope === scope);
        });
    }

    var COOLDOWN_END = {
        unreadable: 'end time unreadable',
        not_reported: 'no end time reported',
        passed: 'its reported end has passed'
    };

    function renderCooldowns(parent, quota, inBrief) {
        cooldownsOf(quota).forEach(function (c) {
            var row = el('div', 'quota-cooldown' + (inBrief ? ' in-brief' : ''));
            row.appendChild(icon('warn', 12));
            // One run of text, so a narrow frame wraps it like a sentence
            // rather than stacking its parts.
            var body = el('span', 'quota-cooldown-body');
            row.appendChild(body);
            var what = c.scope === 'models'
                ? (modelLabel(c.models, c.models_omitted) || 'some models') : 'whole account';
            body.appendChild(el('span', null, 'Cooling down · ' + what));
            var said = ['Cooling down', what];
            // Each part carries its own "·": the gap between spans is layout,
            // and the row's text would otherwise run "Fableuntil".
            var until = el('span', 'quota-when', ' · until ');
            if (c.until && appendStamp(until, c.until, 'warn', true)) {
                body.appendChild(until);
                said.push('until ' + formatResetAt(c.until) + ' ' + relTime(c.until));
            } else {
                var end = COOLDOWN_END[c.until_note] || COOLDOWN_END.not_reported;
                body.appendChild(el('span', 'quota-when', ' · ' + end));
                said.push(end);
            }
            if (c.freshness && c.freshness !== 'fresh') {
                var seen = relTime(c.observed_at);
                var from = 'reported by a stale reading' + (seen ? ' observed ' + seen : '');
                body.appendChild(el('span', 'quota-when', ' · ' + from));
                said.push(from);
            }
            if (c.scope === 'models') {
                row.title = scopeList(c.models, c.models_omitted);
                said.push(row.title);
            }
            row.setAttribute('aria-label', said.join(', '));
            parent.appendChild(row);
        });
    }

    // A scope's names in full, as far as the skill sent them, and how many
    // it left out: past 24 names only the first 24 travel.
    function scopeList(models, omitted) {
        var names = (models || []).join(', ') || 'no model named';
        return omitted ? names + ' +' + omitted + ' more not listed' : names;
    }

    var EXHAUSTION_RESET = {
        not_reported: 'no reset time reported',
        unreadable: 'reset time unreadable',
        passed: 'its reported reset has passed'
    };

    // The model-scoped exhaustions the engine reports, each as a fact of its
    // own: a model's limit, never the account, and never a cooldown. A live
    // one holds those models until its reported reset. One with no reset, an
    // unreadable one or one whose reset has passed is only what was reported
    // — said as such, in the muted voice, and nothing about now is claimed
    // for it. The brief keeps what may hold now and leaves the history (a
    // passed reset) and a scope whose window it already names at the limit
    // (`named`: scope keys) to the details.
    //
    // One the engine reports with a reset still ahead but naming no model
    // holds nothing: no window is its scope, and the reserve counts it
    // against none. It is disclosed in the details in the muted voice, with
    // the reset as reported, and never marks the brief, a window or the dot.
    function renderExhaustions(parent, quota, inBrief, named) {
        exhaustionsOf(quota).forEach(function (e) {
            var unnamed = e.live === true && !isModelScope(e.scope_key);
            if (inBrief && (unnamed || e.reset_note === 'passed' || (named && named[e.scope_key]))) return;
            var live = e.live === true && !unnamed;
            var row = el('div', 'quota-exhaustion' + (live ? '' : ' past') + (inBrief ? ' in-brief' : ''));
            row.appendChild(icon(live ? 'warn' : 'info', 12));
            var body = el('span', 'quota-cooldown-body');
            row.appendChild(body);
            var what = modelLabel(e.models, e.models_omitted) || 'models not named';
            var lead = live ? 'Model limit reached' : 'Reported model limit reached';
            body.appendChild(el('span', null, lead + ' · ' + what));
            var said = [lead, what];
            var until = el('span', 'quota-when', unnamed ? ' · reported until ' : ' · until ');
            if (unnamed) {
                if (e.resets_at && appendStamp(until, e.resets_at, false, true)) {
                    body.appendChild(until);
                    said.push('reported until ' + formatResetAt(e.resets_at) + ' ' + relTime(e.resets_at));
                }
                body.appendChild(el('span', 'quota-when', ' · holds no window'));
                said.push('holds no window');
            } else if (live && e.resets_at && appendStamp(until, e.resets_at, 'warn', true)) {
                body.appendChild(until);
                said.push('until ' + formatResetAt(e.resets_at) + ' ' + relTime(e.resets_at));
            } else {
                var end = EXHAUSTION_RESET[e.reset_note] || EXHAUSTION_RESET.not_reported;
                if (e.reset_note === 'passed' && formatResetAt(e.resets_at)) {
                    end += ' (' + formatResetAt(e.resets_at) + ')';
                }
                body.appendChild(el('span', 'quota-when', ' · ' + end));
                said.push(end);
            }
            if (e.freshness && e.freshness !== 'fresh') {
                var seen = relTime(e.observed_at);
                var from = 'reported by a stale reading' + (seen ? ' observed ' + seen : '');
                body.appendChild(el('span', 'quota-when', ' · ' + from));
                said.push(from);
            }
            row.title = (e.constraint_id ? e.constraint_id + ': ' : '') + scopeList(e.models, e.models_omitted);
            said.push(row.title);
            row.setAttribute('aria-label', said.join(', '));
            parent.appendChild(row);
        });
    }

    function renderQuota(parent, quota, account) {
        if (quota.note) {
            var noteP = withIcon(el('div', 'meta', quota.note), 'info');
            noteP.style.margin = '4px 0 0 2px';
            parent.appendChild(noteP);
        }

        renderAbsence(parent, account, quota.absence);
        renderCooldowns(parent, quota, false);
        renderExhaustions(parent, quota, false);

        if (quota.constraints && quota.constraints.length) {
            var constraintsWrap = el('div', 'quotas-container');
            // The tiles stand in the widget's own order, not the engine's: the
            // engine's changes from one reading to the next.
            orderedWindows(quota.constraints).forEach(function (view) {
                constraintsWrap.appendChild(renderConstraint(view, false, windowHeld(account, view),
                                                             modelScopeHeld(view, quota)));
            });
            parent.appendChild(constraintsWrap);
        }

        if (quota.stale && quota.stale.length) {
            quota.stale.forEach(function (snap) {
                // A reading with no window in it has nothing to show: an empty
                // amber panel only repeated the age the head already gives.
                if (!(snap.constraints && snap.constraints.length)) return;
                var staleBlock = el('div', 'quota-last-known');
                // A fresh reading the reserve does not count says why; a stale
                // one is last known. Neither is drawn as a current bar.
                staleBlock.appendChild(withIcon(el(
                    'div',
                    'last-known-copy',
                    (snap.why ? 'Not current — ' + snap.why + ' · observed '
                        : 'Last known · observed ')
                        + (relTime(snap.observed_at) || 'at an unreported time')
                        + (snap.why ? ' · not counted' : ' · not used to grant routing')
                ), 'warn', 12));
                if (snap.constraints && snap.constraints.length) {
                    var staleConstraints = el('div', 'quotas-container');
                    orderedWindows(snap.constraints).forEach(function (view) {
                        staleConstraints.appendChild(renderConstraint(view, true, false));
                    });
                    staleBlock.appendChild(staleConstraints);
                }
                parent.appendChild(staleBlock);
            });
        }
    }

    // Said in two places now: beside the account, and on the card of a family
    // that holds no account — where it is the only thing that can explain why.
    function appendHarnessNotes(parent, group, facets) {
        if (group.harness_status && group.harness_status !== 'ok') {
            parent.appendChild(dotLabel('harness ' + group.harness_status, 'warn'));
        }
        if (group.harness_enabled === false) {
            parent.appendChild(dotLabel('harness disabled', 'warn'));
        }
        if (group.catalog_known === false) {
            parent.appendChild(dotLabel('catalog ' + facetWord(facets.catalog), facetTone(facets.catalog)));
        }
    }

    function isActive(account) {
        return !!account.signed_in && account.enabled !== false;
    }

    function familyName(group) {
        return group.family_label || group.harness_id;
    }

    // The engine skips per-model caps when it decides whether an account is
    // spent (plugin.py::quota_for, honesty rule 4), so everything that prints
    // one number for a whole account has to skip them too. Otherwise the dot,
    // the card and the list quote three different figures for one account.
    function worstWindow(account) {
        var worst = null;
        liveWindows(account).forEach(function (c) {
            if (isModelWindow(c)) return;
            if (typeof c.used_pct === 'number' && (!worst || c.used_pct > worst.used_pct)) worst = c;
        });
        return worst;
    }

    function worstUsedPct(account) {
        var worst = worstWindow(account);
        return worst ? worst.used_pct : null;
    }

    // A per-model cap never marks the whole account (honesty rule 4), but it is
    // still the difference between "everything runs" and "one model is out" —
    // and that difference is exactly what the dot is asked about.
    function hasSpentModelCap(account) {
        return liveWindows(account).some(function (c) {
            return isModelWindow(c) && isOut(c);
        });
    }

    // The dot answers one question: can I work on this account right now. It
    // used to answer another one — how full the worst bar is — and painted an
    // account red at 85% while it was still perfectly usable.
    // Green means "read, and fine". An account with no window to stand on —
    // never read, or read and refused — is grey: the display law the header
    // states, a value that was not read is never dressed as a good one. That
    // answer cannot be greener than the reading it stands on: with the accounts
    // facet unread, identity and quota are last-known values, and a live green
    // dot over them is the exact claim this widget exists to refuse.
    function accountTone(account, facets) {
        if (isAlertAccount(account)) return 'bad';
        if (facets && facets.accounts && facets.accounts !== 'ok') return 'muted';
        if (!isActive(account)) return 'muted';
        var worst = worstUsedPct(account);
        // Grey is for an account with no windows at all — which is what a
        // refused or never-taken reading leaves behind. It used to be for any
        // reading the engine would not call live, and that grey covered three
        // windows with figures in them.
        if (worst === null) return 'muted';
        // Red only when nothing runs: the general window itself is spent —
        // at its limit by the skill's verdict, not by a rounded percent.
        if (liveWindows(account).some(function (c) { return !isModelWindow(c) && atLimit(c); })) return 'bad';
        // Yellow means "look at this", and two different facts deserve it: a
        // model is already out, or the general window is close to its edge.
        // There is no third colour to tell them apart, and both call for the
        // same thing — opening the account.
        if (hasSpentModelCap(account) || modelHolds(account.quota).length) return 'warn';
        if (worst >= 85) return 'warn';
        return 'ok';
    }

    // `folding`: the overview above already answers "how much is left", so
    // the account opens as a brief and its card unfolds on request. Without
    // an overview (none sent, or it failed) the account is all there is on
    // screen and is shown whole, with nothing to fold it under.
    function renderAccount(parent, group, account, facets, folding) {
        var open = !folding || accountOpen;
        var tile = el('div', 'account-plane' + (open ? '' : ' collapsed'));
        var head = el('div', 'account-head');
        var headLeft = el('div', 'account-head-left');
        var header = el('div', 'account-header');

        var titleWrap = el('div', 'account-title-wrap');
        var tone = accountTone(account, facets);
        titleWrap.appendChild(stateDot(tone, true));
        // The dot is the account's state; the same state goes in as a word a
        // screen reader can read.
        titleWrap.appendChild(el('span', 'sr-only',
            TONE_WORD[tone] || tone));
        titleWrap.appendChild(el('span', 'acct-family', familyName(group)));
        if (account.caption) {
            // credential_kind is the same word for every account here
            // ("config_dir_login") and reads as noise beside the family name.
            // A title on a plain div is not read aloud, so the word also goes
            // in where only a screen reader picks it up.
            titleWrap.title = account.caption;
            titleWrap.appendChild(el('span', 'sr-only', account.caption));
        }
        appendHarnessNotes(titleWrap, group, facets);
        header.appendChild(titleWrap);

        // Everything the old capsules said is still said: verification as a
        // plain word in the caption line below, rotation as these two quiet
        // words. The plan is the exception — it earned a chip of its own.
        // The plan is what these accounts differ by; as one grey word among
        // five it read as noise, so it stands beside the family name instead.
        if (account.plan) {
            var chip = el('span', 'plan-chip', planWord(account.plan, group));
            chip.title = account.plan;
            titleWrap.appendChild(chip);
        }
        if (account.next_up) {
            header.appendChild(el('span', 'account-next-up', 'next up'));
        }
        // The door to the card stands on the account's own line, at its end.

        if (open) headLeft.appendChild(header);

        var meta = el('div', 'account-meta');
        // The selector above shows the account's display name, which is not its
        // address: a profile called "work" has no e-mail in it at all, and
        // without this line the numbers below would have no owner.
        if (open && account.email && account.email !== account.label) {
            meta.appendChild(el('span', null, account.email));
        }
        if (open && account.kind !== 'profile') {
            meta.appendChild(el('span', null, 'Vendor CLI login'));
        }
        var observed = relTime(quotaObservedAt(account));
        meta.appendChild(el('span', 'quota-observed', observed
            ? ('Quota observed ' + observed)
            : 'No quota observation time reported'));

        if (account.verification && account.verification.label && (open || verificationFailed(account))) {
            var vClass = verificationFailed(account) ? 'meta-bad' : null;
            meta.appendChild(el('span', vClass, account.verification.label));
        }
        if (!account.signed_in) {
            meta.appendChild(el('span', 'meta-muted', 'not signed in'));
        } else if (account.enabled === false) {
            meta.appendChild(el('span', 'meta-muted', 'disabled'));
        }
        // A kept screen says it once, in its banner and status line; the same
        // words on every card would only crowd it.
        if (facets.accounts && facets.accounts !== 'ok' && !(currentView && currentView.kept)) {
            meta.appendChild(el('span', null, 'Accounts facet: ' + facetWord(facets.accounts) + ' (last known)'));
        }
        headLeft.appendChild(meta);
        head.appendChild(headLeft);
        // When the engine's verdict IS the worst window — state 'ok' spells it
        // as "58% used" with that window's own reset time — the tiles below say
        // the same number and the same date, and the frame is short. The
        // verdict earns its place when it says something they cannot: a limit
        // reached, a facet unread, no window reported at all.
        var quota = account.quota || {};
        // The brief's window chips echo the verdict the same way the tiles
        // do, so the same rule decides for both.
        var echoesTiles = quota.state === 'ok'
            && !!(quota.constraints && quota.constraints.length);
        var right = el('div', 'account-head-right');
        // An account cooldown already has its scope, end and provenance below.
        // Keep an exhausted-window verdict independent of that restriction.
        if (!echoesTiles && !(quota.state === 'cooling' && cooldownsOf(quota, 'account').length)) {
            right.appendChild(renderQuotaVerdict(quota));
        }
        if (right.firstChild) head.appendChild(right);
        tile.appendChild(head);

        if (open) renderQuota(tile, quota, account);
        else renderBrief(tile, quota, account);
        parent.appendChild(tile);
    }

    function accountToggle(open) {
        var btn = el('button', 'pill-btn acct-toggle' + (open ? ' on' : ''), open ? 'Hide details' : 'Details');
        var caret = withIcon(el('span', 'pill-icon acct-toggle-caret'), 'caret', 12);
        btn.appendChild(caret);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.setAttribute('aria-label', open ? 'Hide the account details' : 'Show every window and last-known reading of this account');
        btn.setAttribute('data-focus', 'account-details');
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            accountOpen = !accountOpen;
            rerender();
        });
        return btn;
    }

    // The folded account keeps problems and the presence of excluded readings.
    // Window tiles, identity detail and normal verification open with Details.
    function renderBrief(parent, quota, account) {
        renderAbsence(parent, account, quota.absence);
        renderCooldowns(parent, quota, true);
        var modelProblems = (quota.constraints || []).filter(function (c) {
            return isModelWindow(c) && isOut(c);
        });
        // A model window already named here at its limit says that limit; its
        // reported exhaustion waits in the details with its own time.
        var named = {};
        modelProblems.forEach(function (c) { if (isSpent(c) && c.scope_key) named[c.scope_key] = true; });
        renderExhaustions(parent, quota, true, named);
        if (modelProblems.length) {
            var problems = el('div', 'acct-lastknown');
            problems.appendChild(icon('warn', 12));
            problems.appendChild(el('span', null, modelProblems.map(function (c) {
                return (modelLabel(c.scoped_models) || c.label || 'Model') + ': '
                    + (isSpent(c) ? 'limit reached' : 'cooling down');
            }).join(' · ') + ' · in the details'));
            parent.appendChild(problems);
        }
        var withWindows = (quota.stale || []).filter(function (snap) {
            return !!(snap.constraints && snap.constraints.length);
        });
        var aside = withWindows.filter(function (snap) { return !!snap.why; });
        var stale = withWindows.filter(function (snap) { return !snap.why; });
        if (aside.length) {
            var whys = [];
            aside.forEach(function (snap) { if (whys.indexOf(snap.why) < 0) whys.push(snap.why); });
            parent.appendChild(withIcon(el('div', 'acct-lastknown',
                (aside.length === 1 ? 'A fresh reading' : aside.length + ' fresh readings')
                    + ' not counted now (' + whys.join('; ') + ') · in the details'), 'warn', 12));
        }
        if (stale.length) {
            var newest = '';
            stale.forEach(function (snap) {
                if (snap.observed_at && (!newest || snap.observed_at > newest)) newest = snap.observed_at;
            });
            parent.appendChild(withIcon(el('div', 'acct-lastknown',
                (stale.length === 1 ? 'A last-known reading' : stale.length + ' last-known readings')
                    + ' · observed ' + (relTime(newest) || 'at an unreported time')
                    + ' · not used to grant routing · in the details'), 'warn', 12));
        }
    }

    // "claude_max" beside "Claude Code" says Claude twice and keeps a wire
    // underscore in the middle of a word. The vendor prefix is dropped only
    // when the family name already carries it, and the whole value stays in
    // the title, so the abbreviated display keeps the full value one hover
    // away.
    function planWord(plan, group) {
        var text = String(plan || '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
        var family = String(familyName(group) || '').trim().split(/\s+/)[0];
        if (family && text.toLowerCase().indexOf(family.toLowerCase() + ' ') === 0) {
            text = text.slice(family.length + 1).trim();
        }
        return text;
    }

    // A harness that is down or switched off is trouble even when it holds no
    // account at all — and a family with no accounts used to have nowhere to
    // say so, because it never reached the account header where this was told.
    function groupTrouble(group) {
        return !!(group.harness_status && group.harness_status !== 'ok')
            || group.harness_enabled === false;
    }

    // Grey outranks green on purpose: a reading that was refused is not a good
    // one, and the mark must not say "go ahead" on its behalf.
    var TONE_WEIGHT = { bad: 3, warn: 2, muted: 1, ok: 0 };

    // One size for a family's glyph wherever it appears, and the ring with an
    // initial is cut to it in the stylesheet.
    var FAMILY_MARK_PX = 14;

    // The family mark answers the same question its accounts do, so it answers
    // it with the same word: the worst tone anything under it carries. Writing
    // a second scale here would be a second opinion about one fact.
    // The worst state a set of accounts is in, by the weights above. The mark
    // of a family asks it about the family; the fold row asks it about one
    // section — and a section's dot has to say what the worst row in it would
    // say, or the two disagree about the same account.
    function worstTone(accounts, facets) {
        var worst = 'ok';
        (accounts || []).forEach(function (account) {
            var tone = accountTone(account, facets);
            if (TONE_WEIGHT[tone] > TONE_WEIGHT[worst]) worst = tone;
        });
        return worst;
    }

    function groupTone(group, facets) {
        if (groupTrouble(group)) return 'bad';
        return worstTone(group.accounts, facets);
    }

    // What the reader picked by hand outlives the 30-second redraw, but never
    // outlives the thing it points at: a profile deleted in the app must not
    // leave the screen blank while the selection insists it is still there.
    function syncSelection(groups) {
        var group = null;
        var i;
        for (i = 0; i < groups.length; i++) {
            if (groups[i].harness_id === selectedHarness) { group = groups[i]; break; }
        }
        if (!group) {
            for (i = 0; i < groups.length; i++) {
                if ((groups[i].accounts || []).length) { group = groups[i]; break; }
            }
        }
        if (!group) group = groups[0] || null;
        if (!group) {
            selectedHarness = '';
            selectedAccountKey = '';
            accountsOpen = false;
            return { group: null, account: null };
        }
        selectedHarness = group.harness_id;

        var accounts = group.accounts || [];
        var account = null;
        for (i = 0; i < accounts.length; i++) {
            if (accounts[i].key === selectedAccountKey) { account = accounts[i]; break; }
        }
        if (!account) {
            for (i = 0; i < accounts.length; i++) {
                if (isActive(accounts[i])) { account = accounts[i]; break; }
            }
        }
        if (!account) account = accounts[0] || null;
        selectedAccountKey = account ? account.key : '';
        // An open list over an empty family would come back by itself with the
        // next answer, without anybody having clicked: the flag outlived what
        // it was opened over.
        if (!accounts.length) accountsOpen = false;
        return { group: group, account: account };
    }

    // Unknown families keep a neutral initial beside their full name.
    // A family looks the same wherever it is named: its own mark when the widget
    // carries one, the ring with its initial when it does not. Written once —
    // the choice used to be spelled out at each call site, and one of them had
    // forgotten the ring, so a family with no vendor mark, of which a reader
    // can easily have two, was named with nothing in front of it. The size is
    // not a parameter: the ring is sized in the stylesheet, and a mark that
    // could be asked for at any size while the ring could not would be a
    // promise only half of this function keeps.
    function familyMark(group) {
        return BRAND_MARKS[group.harness_id]
            ? brandIcon(group.harness_id, FAMILY_MARK_PX)
            : harnessInitial(group);
    }

    function harnessInitial(group) {
        var name = familyName(group) || '?';
        var badge = el('span', 'harness-initial', (name.charAt(0) || '?').toUpperCase());
        return badge;
    }

    function renderHarnessSeg(parent, groups, selected, hasAnswer, facets) {
        var seg = el('div', 'harness-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Agent family');

        // Before the first answer the row would otherwise hold an empty 8px
        // capsule where the marks belong, and the widget looks broken while it
        // is merely reading. Three of the marks it carries, shown faint and
        // dead: a shape waiting to be filled, not a claim that the reader has
        // these three.
        if (!groups.length && !hasAnswer) {
            ['codex', 'claude', 'cursor'].forEach(function (name) {
                var ghost = el('button', 'harness-btn loading');
                ghost.disabled = true;
                ghost.appendChild(brandIcon(name, 14));
                ghost.appendChild(el('span', null, name.charAt(0).toUpperCase() + name.slice(1)));
                seg.appendChild(ghost);
            });
            seg.setAttribute('aria-label', 'Reading agent families');
            parent.appendChild(seg);
            return;
        }

        groups.forEach(function (group) {
            var isOn = selected && group.harness_id === selected.harness_id;
            var count = (group.accounts || []).length;
            var tone = groupTone(group, facets);
            var btn = el('button', 'harness-btn' + (isOn ? ' active' : '') + (count ? '' : ' empty'));
            // A family the widget has no mark for gets its own initial, never
            // somebody else's logo: a wrong mark is a wrong claim about whose
            // account this is.
            btn.appendChild(familyMark(group));
            var name = familyName(group);
            btn.appendChild(el('span', 'harness-name', name.replace(/ (?:CLI|Code)$/, '')));
            // The number is the accounts switched on: the ones that can carry work.
            var active = (group.accounts || []).filter(function (a) { return a.enabled !== false; }).length;
            if (count) btn.appendChild(el('span', 'harness-count', String(active)));
            // Colour is not a word: whatever the pip says in red, amber or green
            // has to be readable out loud as well — and in the words the rest of
            // the widget already uses for the same four tones.
            var say = name + ' — ' + (count
                ? (count + (count === 1 ? ' account' : ' accounts') + ', ' + TONE_WORD[tone])
                : 'no accounts');
            btn.setAttribute('aria-label', say);
            btn.setAttribute('aria-pressed', isOn ? 'true' : 'false');
            btn.setAttribute('data-focus', 'harness:' + group.harness_id);
            btn.title = say;
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var same = selectedHarness === group.harness_id;
                selectedHarness = group.harness_id;
                if (!same) selectedAccountKey = '';
                // Clicking the mark you are already on is still a click outside
                // the list, and every other click outside closes it.
                accountsOpen = false;
                settingsOpen = false;
                rerender();
            });
            // The pip answers "can I work here"; with no account under the mark
            // there is no such question, so nothing is said rather than said grey.
            if (count || groupTrouble(group)) {
                btn.appendChild(el('span', 'pip ' + tone + ' seg-pip'));
            }
            seg.appendChild(btn);
        });
        parent.appendChild(seg);
    }

    // The tail of a row carries the shortest true sentence about the account
    // that is not a number: what is broken, or — for an account nobody has to
    // fix — why there is no figure. An account with figures says nothing here;
    // they stand on the line below, and repeating them would be a third copy.
    function tailWord(account) {
        var reason = troubleReason(account);
        if (reason) return reason;
        if (worstUsedPct(account) !== null) return '';
        // Read, and the window carries no ratio — not the same thing as nothing
        // having been read, so not the same words.
        if (liveWindows(account).length) return 'no ratio';
        return TONE_WORD.muted;
    }

    // What a window is called in the row: its length. Codex holds two
    // seven-day windows, and "week" twice would name neither — but they belong
    // to different pools, and the pool's chip stands in front of its own
    // windows, so the length is usually all a tag has to say. The windows
    // here are one pool's; two of one length inside it take the role word
    // from their names as well — "week primary", "week secondary" — and only
    // then, so the everyday row keeps its short words.
    function windowTags(constraints) {
        var marks = constraints.map(function (c) {
            return {
                c: c,
                scoped: isModelWindow(c),
                pool: poolOf(c),
                // No length reported: the window keeps its own name. With no
                // name either it takes the same stand-in the card uses, so a
                // read window is never silently dropped from the row.
                tag: windowLength(c.window_seconds) || String(c.label || '') || 'Window Limit'
            };
        });
        var seen = {};
        marks.forEach(function (m) { seen[m.tag] = (seen[m.tag] || 0) + 1; });
        marks.forEach(function (m) {
            if (seen[m.tag] < 2) return;
            var role = splitWindowName(m.c.label, m.c.window_seconds).role;
            // Same length and no role to tell them apart: the window's own
            // name is the last honest difference.
            m.tag = role ? m.tag + ' ' + role : (String(m.c.label || '') || m.tag);
        });
        return marks;
    }

    // The pool's chip: the same chip the tile wears, in front of the pool's
    // windows. A long name gives way from the front — "…Codex-Spark" — because
    // the tail is the part that tells one pool from another; the whole of it
    // stays in the title. Coloured only when the pool is a model's own: red
    // when its window's measured share is spent — the one thing a per-model
    // cap says and a plain window does not — amber when a cooldown or a
    // reported model limit holds it. `tone` is 'bad', 'warn' or nothing.
    // `view`: a window of the pool. A model's pool says its whole scope on
    // hover, since two scopes may print the same short name.
    function poolChip(pool, tone, view) {
        var chip = el('span', 'acct-cap acct-pool'
            + (tone === 'bad' ? ' exhausted' : (tone === 'warn' ? ' held' : '')));
        chip.appendChild(el('bdi', null, pool));
        chip.title = isModelWindow(view) ? pool + ' (' + scopeList(view.scoped_models) + ')' : pool;
        return chip;
    }

    // One sentence per fact: the card, the row and the button all say
    // "83% used" — the verb is the unit, and a bar beside it is the share
    // left. usedText is the figure alone; the missing ratio takes the
    // half of the card's own wording that is true — the engine reported no
    // number. "unmetered" alone would read as "unlimited", which is the one
    // word the display law at the top of this file forbids.
    function usedText(view) {
        return hasPct((view || {}).used_pct) ? (pctText(view) + '%') : 'no ratio';
    }

    // A window scoped to a model lists every name that model answers to:
    // ['fable', 'claude-fable-5', 'best'] is one model under three names — the
    // short one, the versioned one, and the role alias. Printing them as three
    // models would be a lie, so the names collapse to the shortest real stem.
    var MODEL_ALIASES = { best: 1, latest: 1, fastest: 1, 'default': 1 };

    // `omitted`: names of the scope the skill did not send (past 24). Each
    // counts in the "+N" — it may be one more model, and a count that left
    // it out would say the scope is smaller than it is.
    function modelLabel(models, omitted) {
        var list = (models || []).map(function (m) { return String(m || '').trim(); })
            .filter(function (m) { return !!m; });
        if (!list.length) return '';
        var real = list.filter(function (m) {
            return !MODEL_ALIASES[m.toLowerCase()];
        });
        if (!real.length) real = list;
        // Same stem, different spelling: keep the shortest way to say it.
        var stems = {};
        real.forEach(function (m) {
            // A version can be several segments deep — "claude-opus-4-5" is one
            // model, and trimming a single "-5" would leave "opus-4" standing
            // apart from plain "opus".
            var stem = m.toLowerCase().replace(/^claude-/, '').replace(/([-_][\d.]+)+$/, '');
            stems[stem] = 1;
        });
        // The stem itself is the model's plain name — "claude-sonnet-4-5" and
        // "sonnet" are the same model, and the row has room only for the short
        // way of saying it. Every spelling stays in the chip's tooltip.
        var names = Object.keys(stems).filter(function (k) { return !!k; });
        // Every name was a version suffix and nothing else ("claude-", "-5"):
        // no model name is left to print, and an empty chip reads as a
        // rendering fault rather than as an absent one.
        if (!names.length) return '';
        names.sort(function (a, b) { return a.length - b.length; });
        var head = names[0];
        head = head.charAt(0).toUpperCase() + head.slice(1);
        // What is left after the collapse really is several models, and then
        // the counter is honest again.
        var more = names.length - 1 + (omitted > 0 ? omitted : 0);
        return head + (more > 0 ? ' +' + more : '');
    }

    // The row says a window is spent and stays silent about when it comes back,
    // while the date is already in the data. These lines carry that date — and
    // only that date: a window still running has nothing to wait for, so its
    // right-hand column says "available" instead of borrowing a red timestamp.
    var RESET_LINES_MAX = 3;

    // Why one window of a row is out for now, in the words and colour of its
    // own scope, or null. Spent (red) is its measured share at the limit and
    // waits for its reset. Cooling (amber) is its own cooldown or one on
    // exactly its models, and waits for that cooldown's end. A model limit
    // the engine reports out while the share read here is below it (amber)
    // waits for that limit's reported reset. A hold on another scope — a
    // model's, for a shared window — is never this window's.
    function windowOut(m, quota) {
        if (isSpent(m.c)) {
            return { kind: 'spent', word: 'spent', stamp: m.c.resets_at || '', missing: 'no reset time' };
        }
        if (windowCooling(m.c)) {
            return { kind: 'cooling', word: 'cooling down', stamp: m.c.cooldown_until || '',
                     missing: m.c.cooldown_until ? 'end time unreadable' : 'no end time' };
        }
        var hold = scopeHoldOf(m.c, quota);
        if (!hold) return null;
        if (hold.kind === 'cooldown') {
            return { kind: 'cooling', word: 'cooling down', stamp: hold.until,
                     missing: hold.note === 'unreadable' ? 'end time unreadable' : 'no end time',
                     stale: hold.stale };
        }
        return { kind: 'limit', word: 'limit reported', stamp: hold.until, missing: 'no reset time',
                 stale: hold.stale };
    }

    function appendResetLines(parent, marks, account) {
        // Compact is the list as it was before these lines existed: bars only.
        if (density === 'compact') return '';
        var quota = (account || {}).quota;
        // Out for now: spent (red, back at its reset), cooling or a model
        // limit reported out (amber, back when that ends).
        var outOf = marks.map(function (m) { return windowOut(m, quota); });
        var outs = marks.filter(function (m, i) { return !!outOf[i]; });
        // Nothing is owed: the bars above have already said everything — unless
        // the reader asked for detail, in which case an empty answer is not the
        // detail they asked for.
        if (!outs.length && density !== 'detailed') return '';
        var scopedOut = outs.some(function (m) { return m.scoped; });
        var alive = marks.filter(function (m) { return outs.indexOf(m) < 0; });
        // Detailed asks for every window by name. Normal keeps a single line
        // for "the rest still runs", and it is the window closest to its own
        // edge: "others 0% used" above "others 64% used" is two lines saying
        // the same thing, and neither says which is which.
        var rest = density === 'detailed' ? alive : (alive.length ? [alive.reduce(function (worst, m) {
            var a = hasPct(m.c.used_pct) ? m.c.used_pct : -1;
            var b = hasPct(worst.c.used_pct) ? worst.c.used_pct : -1;
            return a > b ? m : worst;
        })] : []);
        var rows = outs.concat(rest);
        var shown = density === 'detailed' ? rows : rows.slice(0, RESET_LINES_MAX);
        var spoken = [];
        var box = el('div', 'acct-rls');

        shown.forEach(function (m) {
            var out = outOf[marks.indexOf(m)];
            var isOutRow = !!out;
            // Two words and two colours, as on the tile: a share at its limit
            // is "spent" in red and waits for its reset; a cooldown — even one
            // whose end nobody can read — is "cooling down" in amber and waits
            // for its end. At the limit and cooling, the share is spent.
            var isSpentRow = isOutRow && out.kind === 'spent';
            var cooling = isOutRow && !isSpentRow;
            var tone = isSpentRow ? 'bad' : (cooling ? 'warn' : '');
            // The tag is the window's length; the pool it belongs to stands
            // beside it, as the same chip the bars wear. A general window next
            // to a spent per-model one in the folded view is "everything
            // else": naming it by length would repeat the bar above it.
            var name = (scopedOut && !isOutRow && density !== 'detailed' && !m.scoped)
                ? 'others' : m.tag;
            var line = el('div', 'acct-rl');
            var tag = el('span', 'acct-rl-tag ' + (tone || 'ok'), name);
            tag.title = m.c.label || m.tag;
            line.appendChild(tag);
            // A pool chip is coloured only for a model's own pool, and only
            // by what holds that pool.
            if (m.pool) line.appendChild(poolChip(m.pool, m.scoped ? tone : '', m.c));
            var said = name + (m.pool ? ' ' + m.pool : '');

            var word = isOutRow ? out.word : usedText(m.c) + ' used';
            line.appendChild(el('span', 'acct-rl-txt', word));
            line.appendChild(el('span', 'acct-rl-lead'));
            var spokenWord = out && out.kind === 'limit' ? 'model limit reported reached' : word;
            var from = out && out.stale ? ', reported by a stale reading' : '';

            var stampIso = isOutRow ? out.stamp : '';
            var when = isOutRow ? formatResetAt(stampIso) : '';
            var whenCls = 'acct-rl-when' + (cooling ? ' warn' : '');
            if (isOutRow && when) {
                line.appendChild(el('span', whenCls, when));
                // relTime already answers as a phrase — "in 6d", "2h ago" — so
                // the line prints it as it is. It used to put an "in" of its
                // own in front, and a spent window read "in in 6d".
                line.appendChild(el('span', 'acct-rl-in', relTime(stampIso)));
                spoken.push(said + ' ' + spokenWord + (isSpentRow ? ', back ' : ' until ') + when + from);
            } else if (isOutRow) {
                // No date to give: say which one is missing rather than leave
                // a gap that reads as "available". A cooldown ends, it does
                // not reset.
                line.appendChild(el('span', whenCls, out.missing));
                line.appendChild(el('span', 'acct-rl-in'));
                spoken.push(said + ' ' + spokenWord + ', ' + out.missing
                    + (out.kind === 'cooling' ? '' : ' reported') + from);
            } else {
                line.appendChild(el('span', 'acct-rl-when free', 'available'));
                line.appendChild(el('span', 'acct-rl-in'));
                spoken.push(said + ' available, ' + word);
            }
            box.appendChild(line);
        });

        var hidden = rows.length - shown.length;
        if (hidden > 0) {
            box.appendChild(el('div', 'acct-rl-more',
                '+' + hidden + ' more window' + (hidden === 1 ? '' : 's')));
            spoken.push(hidden + ' more window' + (hidden === 1 ? '' : 's'));
        }
        parent.appendChild(box);
        return spoken.join(' · ');
    }

    // What a row says of the holds the skill carried apart from its windows,
    // each chip `{text, full}`: the short words, and the whole sentence for
    // the title and a screen reader. A cooldown on the whole account is one
    // chip. Model holds are told apart by their whole scope: one is named
    // with its model and its kind — a cooldown, or a limit the engine reports
    // reached — and several are counted in one chip whose sentence names
    // every one; "model cooldown" for two different scopes named neither. A
    // hold a window of the row already shows (its own cooldown, its model's
    // share at the limit) is not said twice.
    function rowHolds(quota, marks) {
        var out = [];
        var whole = cooldownsOf(quota, 'account');
        if (whole.length && !marks.some(function (m) { return !m.scoped && windowCooling(m.c); })) {
            var ends = whole.map(function (c) { return c.until; });
            var last = ends.every(function (u) { return !!formatResetAt(u); })
                ? ends.reduce(function (a, b) { return Date.parse(a) >= Date.parse(b) ? a : b; }) : '';
            out.push({ text: 'account cooldown',
                       full: 'account cooldown' + (last ? ' until ' + formatResetAt(last) : ', end not known') });
        }
        var models = modelHolds(quota).filter(function (h) {
            return !marks.some(function (m) {
                if (!m.scoped || !h.key || m.c.scope_key !== h.key) return false;
                return h.kind === 'cooldown' ? windowCooling(m.c) : isSpent(m.c);
            });
        });
        function kindWords(h) {
            return h.kind === 'cooldown' ? 'model cooldown' : 'model limit reached';
        }
        function sentence(h) {
            var name = modelLabel(h.models, h.omitted) || 'models not named';
            var when = formatResetAt(h.until);
            var end = when ? ' until ' + when
                : (h.kind === 'cooldown' ? ', ' + (COOLDOWN_END[h.note] || COOLDOWN_END.not_reported) : '');
            return kindWords(h) + ': ' + name + end + (h.stale ? ', reported by a stale reading' : '')
                + ' (' + scopeList(h.models, h.omitted) + ')';
        }
        if (models.length === 1) {
            out.push({ text: kindWords(models[0]) + ': '
                + (modelLabel(models[0].models, models[0].omitted) || 'models not named'),
                       full: sentence(models[0]) });
        } else if (models.length > 1) {
            var kinds = {};
            models.forEach(function (h) { kinds[h.kind] = true; });
            var noun = kinds.cooldown && kinds.limit ? 'model holds'
                : (kinds.cooldown ? 'model cooldowns' : 'model limits reached');
            out.push({ text: models.length + ' ' + noun,
                       full: models.length + ' ' + noun + ': ' + models.map(sentence).join('; ') });
        }
        return out;
    }

    // The second floor of a row is one of four things, in this order, and each
    // has its own condition: nothing falls through to it by default, because a
    // line drawn "because there was room" is a claim nobody checked. The
    // fourth is the newest — the reader's own filter left this row nothing to
    // show, which is not the same as the account reporting nothing.
    function appendSecondFloor(parent, account, harnessId) {
        var known = liveWindows(account);
        var windows = windowsForRow(account, harnessId);
        var spoken = [];
        // The filter took everything this account had. An empty row here is
        // indistinguishable from "nothing was reported", and those are two
        // different facts — so the row says which one it is.
        if (!windows.length && known.length) {
            var kind = modelView(harnessId) === 'models' ? 'model' : 'shared';
            var said = 'no ' + kind + ' window on this account';
            parent.appendChild(el('div', 'acct-line2', said));
            return said;
        }
        if (windows.length) {
            var wins = el('div', 'acct-wins');
            // The row draws the same groups the tiles stand in: a pool's chip,
            // then its bars. A group moves to the next line whole; only when
            // it alone is wider than the list does it fold inside itself
            // (the rule on .acct-grp). The lines under the bars read the same
            // marks in the same order.
            var marks = [];
            var groups = poolGroups(windows).map(function (group) {
                var groupMarks = windowTags(group.windows);
                marks.push.apply(marks, groupMarks);
                return { pool: group.pool, marks: groupMarks };
            });
            groups.forEach(function (group, index) {
                if (index > 0) wins.appendChild(el('span', 'acct-sep'));
                var grp = el('span', 'acct-grp');
                if (group.pool) {
                    // Red is spent, and only spent; amber is held — the card
                    // next to it paints its chip by the same rule, and only
                    // for a model's own pool.
                    var poolTone = group.marks.some(function (m) {
                        return m.scoped && isSpent(m.c);
                    }) ? 'bad' : (group.marks.some(function (m) {
                        return m.scoped && (windowCooling(m.c) || modelScopeHeld(m.c, account.quota));
                    }) ? 'warn' : '');
                    grp.appendChild(poolChip(group.pool, poolTone, group.marks[0] && group.marks[0].c));
                }
                group.marks.forEach(function (m) {
                    var win = el('span', 'acct-win');
                    var bar = windowMeter(m.c, { size: 'mini', held: windowHeld(account, m.c) });
                    if (bar) win.appendChild(bar);
                    // The bar is the share left and the figure the share used,
                    // so the figure says its unit: "week 25%" beside a bar
                    // three-quarters full read as either.
                    var pct = usedText(m.c) + (hasPct(m.c.used_pct) ? ' used' : '');
                    // A tag can be a whole phrase — "1 reset credit available" is
                    // a real window name — and "name no ratio" then runs together
                    // as one sentence. With no ratio there is no bar either, so
                    // the absent figure is already visible: the name stands alone
                    // and the full wording stays in the tooltip.
                    win.appendChild(el('span', 'acct-win-tag', m.tag));
                    if (hasPct(m.c.used_pct)) {
                        win.appendChild(el('span', 'acct-win-pct', pct));
                    }
                    var cooling = windowCooling(m.c);
                    if (cooling) win.appendChild(el('span', 'acct-cap held', 'cooldown'));
                    win.title = (m.c.label || m.tag) + ' — ' + pct
                        + (ownReset(m.c) ? ', resets ' + formatResetAt(ownReset(m.c)) : '')
                        // The tile above says "cooldown" with the same date; the
                        // row used to drop it, leaving a coloured mark with no reason.
                        + (cooling ? ', cooldown ' + (isCooling(m.c)
                            ? 'until ' + formatResetAt(m.c.cooldown_until) : 'end time unreadable') : '');
                    // Without the pool, "week 58% used" and "week 100% used" are the same
                    // sentence twice — and the chip that tells them apart is
                    // exactly what a screen reader cannot see.
                    spoken.push((m.pool ? m.pool + ' ' : '') + m.tag + ' ' + pct
                        + (cooling ? ' cooldown' : ''));
                    grp.appendChild(win);
                });
                wins.appendChild(grp);
            });
            // Holds the skill carried apart from these windows (another
            // source's, a stale reading's, the engine's model exhaustions)
            // still mark the row, each named by what it holds.
            rowHolds(account.quota, marks).forEach(function (h) {
                var chip = el('span', 'acct-cap held', h.text);
                chip.title = h.full;
                wins.appendChild(chip);
                spoken.push(h.full);
            });
            parent.appendChild(wins);
            var owed = appendResetLines(parent, marks, account);
            return spoken.join(', ') + (owed ? ' · ' + owed : '');
        }
        // Raw engine detail may contain local paths or vendor bodies. The row
        // uses only the independently typed account state; quota actions are
        // mapped separately from typed quota absences.
        if (isTroubled(account)) {
            var reason = troubleReason(account);
            parent.appendChild(el('div', 'acct-line2', reason));
            return reason;
        }
        var holds = rowHolds(account.quota, []);
        if (holds.length) {
            parent.appendChild(el('div', 'acct-line2', holds.map(function (h) { return h.text; }).join(' · ')));
            return holds.map(function (h) { return h.full; }).join(', ');
        }
        var bits = [];
        if (account.plan) bits.push(account.plan);
        var observed = relTime(quotaObservedAt(account));
        if (observed) bits.push('quota observed ' + observed);
        if (!bits.length) return '';
        parent.appendChild(el('div', 'acct-line2', bits.join(' · ')));
        return bits.join(', ');
    }

    function renderAccountSelect(parent, group, account, hasAnswer, facets) {
        var accounts = group ? (group.accounts || []) : [];
        var open = accountsOpen && accounts.length > 0;
        // Trouble on the account already on screen is visible on screen. This
        // number is the reason to open the list at all, so it counts the others.
        // Counted by the same rule that paints the dots in the list below.
        // isAlertAccount alone said "2" while three rows showed red, because a
        // window past 85% used to turn the dot without being an alert. Since
        // the dot went by availability that threshold is 100%, and red here
        // means the same thing it means in the list: nothing runs.
        var elsewhere = accounts.filter(function (a) {
            return a.key !== selectedAccountKey && accountTone(a, facets) === 'bad';
        }).length;
        var wrap = el('div', 'acct-wrap');
        var btn = el('button', 'acct-btn');
        btn.setAttribute('data-focus', 'account-btn');
        btn.disabled = !accounts.length;

        // The button carries the whole account in one aria-label, so the dot
        // stays out of the reading order rather than repeating a word of it.
        if (account) btn.appendChild(stateDot(accountTone(account, facets)));
        // "no accounts" is a verdict, and it may only be said when a family
        // actually answered with none. Before the first answer, and when the
        // endpoint answered with no family at all, the button says neither.
        var idle = !hasAnswer ? 'reading…' : (group ? 'no accounts' : 'not read');
        btn.appendChild(el('span', 'acct-name', account ? account.label : idle));

        var worst = account ? worstWindow(account) : null;
        var word = account ? tailWord(account) : '';
        if (worst) {
            var worstBar = windowMeter(worst, { size: 'mini', held: windowHeld(account, worst) });
            if (worstBar) btn.appendChild(worstBar);
            btn.appendChild(el('span', 'acct-count', usedText(worst) + ' used'));
        }
        // A figure and a fault are not alternatives: an account can be read,
        // counted and still have a check that failed. Showing only the number
        // left the fault to be discovered by opening the list.
        if (word) btn.appendChild(el('span', 'acct-count', word));
        if (elsewhere) {
            // The number and the words are two pieces, as in the list's fold
            // row: a narrow frame keeps the number (the rule on .acct-btn).
            var alarm = el('span', 'acct-alarm');
            alarm.appendChild(el('span', null, elsewhere));
            alarm.appendChild(el('span', 'acct-alarm-words', (elsewhere === 1 ? ' needs' : ' need') + ' attention'));
            btn.appendChild(alarm);
        }
        btn.appendChild(withIcon(el('span', 'acct-caret'), 'caret', 12));

        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.setAttribute('aria-label', accounts.length
            ? ('Account: ' + (account ? account.label : '')
               + (worst ? ' — ' + usedText(worst) + ' used' : '')
               + (word ? ' — ' + word : '')
               + (elsewhere ? ' — ' + elsewhere + ' other '
                  + (elsewhere === 1 ? 'account in this family needs'
                     : 'accounts in this family need') + ' attention' : ''))
            : (!hasAnswer ? 'Reading accounts'
                : (group ? 'No accounts in this family' : 'No family was reported')));
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (!accounts.length) return;
            accountsOpen = !accountsOpen;
            if (accountsOpen) {
                // Shut, unless the account on screen is inside it: a fold that
                // hid the selected row would answer a question nobody asked.
                foldOpen = holdsSelected(splitAccounts(accounts, facets).folded);
            }
            // Opening one panel closes the others: the click that opens this
            // one never reaches the document handler, so it cannot close them.
            settingsOpen = false;
            rerender();
        });
        wrap.appendChild(btn);

        if (open) {
            // Buttons in a labelled group, not a listbox: role="listbox"
            // promises arrow-key navigation, and promising a contract that is
            // not implemented is worse for a screen reader than plain buttons.
            var pop = el('div', 'acct-pop');
            accountPop = pop;
            pop.setAttribute('role', 'group');
            pop.setAttribute('aria-label', 'Accounts in ' + familyName(group));
            var split = splitAccounts(accounts, facets);
            var option = function (candidate) {
                var isOn = candidate.key === selectedAccountKey;
                var opt = el('button', 'acct-opt' + (isOn ? ' active' : ''));
                opt.setAttribute('data-focus', 'opt:' + candidate.key);
                var rowTone = accountTone(candidate, facets);
                opt.appendChild(stateDot(rowTone));

                var body = el('span', 'acct-opt-body');
                body.appendChild(el('span', 'acct-opt-name', candidate.label));
                // Whatever the second floor ended up saying is said aloud too:
                // the windows are the reason this row is two storeys tall, and
                // a label that stopped at the name would hide them entirely.
                var floorSpeech = appendSecondFloor(body, candidate, group.harness_id);
                opt.appendChild(body);

                var word = tailWord(candidate);
                if (word) opt.appendChild(el('span', 'acct-opt-tail', word));

                // The dot's colour and the words often say the same thing; said
                // twice in a row a screen reader reads it twice.
                // The tail already names the state in the words closest to the
                // cause ("not signed in"); the tone word after it would be the
                // same fact one step further away ("no live reading").
                var spokenState = word || TONE_WORD[rowTone] || rowTone;
                opt.setAttribute('aria-label', candidate.label
                    + ' — ' + spokenState
                    + (floorSpeech ? ' · ' + floorSpeech : '')
                    + (isOn ? ' · shown' : ''));
                opt.addEventListener('click', function (e) {
                    e.stopPropagation();
                    selectedAccountKey = candidate.key;
                    accountsOpen = false;
                    settingsOpen = false;
                    focusAccountBtn = true;
                    rerender();
                });
                return opt;
            };

            split.live.forEach(function (candidate) {
                pop.appendChild(option(candidate));
            });

            if (split.folded.length) {
                var showFolded = foldOpen;
                if (!split.live.length) {
                    // Nothing above to fold under. The sections stand on their
                    // own and say why the family looks empty; a row reading
                    // "Hidden" over an empty list would hide the answer behind
                    // one more click.
                    showFolded = true;
                } else {
                    pop.appendChild(foldRow(split.folded, facets));
                }
                if (showFolded) {
                    split.folded.forEach(function (section) {
                        pop.appendChild(foldSection(section, option));
                    });
                }
            }
            wrap.appendChild(pop);
        }
        parent.appendChild(wrap);
    }

    // One row under the live accounts, one line tall at every density: it names
    // what is hidden and does not grow with it.
    function foldRow(folded, facets) {
        var row = el('button', 'acct-fold' + (foldOpen ? ' open' : ''));
        row.setAttribute('data-focus', 'fold');
        row.setAttribute('aria-expanded', foldOpen ? 'true' : 'false');
        row.appendChild(withIcon(el('span', 'acct-fold-caret'), 'caret', 12));
        row.appendChild(el('span', 'acct-fold-word', 'Hidden'));
        var dots = el('span', 'acct-fold-dots');
        var spoken = [];
        folded.forEach(function (section) {
            // The dots are the reason this row can stay shut: a check that
            // failed downstairs still shows its colour from the outside.
            dots.appendChild(stateDot(worstTone(section.accounts, facets)));
            spoken.push(section.accounts.length + ' ' + section.title.toLowerCase());
        });
        // The button above counts every account in trouble except the one on
        // screen, whose trouble is on screen already. This row counts those same
        // accounts among the ones it hides, by the same rule, so the two differ
        // only by red rows standing upstairs in plain sight. The number goes in
        // the red pill the button wears — folding an account the reader switched
        // off on purpose should not force the fold open every time.
        var attention = 0;
        folded.forEach(function (section) {
            section.accounts.forEach(function (candidate) {
                if (candidate.key !== selectedAccountKey
                    && accountTone(candidate, facets) === 'bad') attention++;
            });
        });
        var attentionTail = (attention === 1 ? ' needs' : ' need') + ' attention';
        var attentionWords = attention + attentionTail;
        if (attention) {
            // The number and the words are two pieces, so that a list too
            // narrow for the phrase keeps the number alone (the rule on .acct-pop).
            var pill = el('span', 'acct-alarm');
            pill.appendChild(el('span', null, attention));
            pill.appendChild(el('span', 'acct-alarm-words', attentionTail));
            row.appendChild(pill);
        }
        row.appendChild(dots);
        row.appendChild(el('span', 'acct-fold-count', foldedCount(folded)));
        // Open or shut is aria-expanded's to say: a word for it here is read
        // twice, and "shown" in this list already names the account on screen.
        row.setAttribute('aria-label', foldedCount(folded) + ' hidden — '
            + spoken.join(', ')
            + (attention ? ' · ' + attentionWords : ''));
        // Said to the mouse as well. The dots keep quiet on purpose — the
        // label says the same thing in words.
        row.title = row.getAttribute('aria-label');
        row.addEventListener('click', function (e) {
            e.stopPropagation();
            foldOpen = !foldOpen;
            rerender();
        });
        return row;
    }

    function foldSection(section, option) {
        // Only one section is not the reader's own doing — a check that failed
        // — and it wears a tint rather than a second colour on its rows.
        var box = el('div', 'acct-sec' + (section.reason === 'failed' ? ' broken' : ''));
        var head = el('div', 'acct-sec-head');
        head.appendChild(el('span', 'acct-sec-title', section.title));
        head.appendChild(el('span', 'acct-sec-count', section.accounts.length));
        head.appendChild(el('span', 'acct-sec-line'));
        box.appendChild(head);
        section.accounts.forEach(function (candidate) {
            box.appendChild(option(candidate));
        });
        return box;
    }

    /* ------------------------------------------------------------------
       Reserve overview and chart.

       Every number here is the skill's (quota_summary.py): the same
       calculation the model's quota_summary tool reads. The widget formats;
       it never adds, averages or projects anything of its own.

       On first sight the overview answers three questions and no more: how
       much of each limit is left, which limit is the tightest, and how old
       the readings are. Everything else unfolds when asked for — a limit's
       details under its own row, the chart under its own button — so the
       first screen is a short table, not a wall of figures and caveats.
       ------------------------------------------------------------------ */

    // One cell per account reads well up to about this many accounts in the
    // room a row has; past it the row draws one bar of the average instead.
    var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    function reserveUrl(reuse) {
        var parts = [];
        if (selectedHarness) parts.push('harness=' + encodeURIComponent(selectedHarness));
        if (chartOpen) {
            var key = selectedHarness
                ? chartKeyFor(selectedHarness, reserveGroups(currentView, selectedHarness)) : '';
            if (key) parts.push('group=' + encodeURIComponent(key));
            parts.push('horizon=' + encodeURIComponent(horizon));
        } else {
            // The chart is folded: the skill need not draw it, nor read the
            // longer stretch of history it would need.
            parts.push('chart=0');
        }
        if (reuse) parts.push('reuse=1');
        return ROUTE + '?' + parts.join('&');
    }

    function reserveGroups(view, harnessId) {
        var summary = view && view.reserve && view.reserve.summary;
        if (!summary || !summary.groups) return [];
        return summary.groups.filter(function (g) { return g.harness === harnessId; });
    }

    // The limit the chart shows for a family: the one the reader picked, as
    // long as it still exists, else the family's tightest one — the same rule
    // the skill applies when it is asked for no limit in particular. A short
    // limit that happens to be nearly full is not where a chart should open.
    //
    // The limit it opens on is kept from then on, exactly as a pick is: an
    // answer during an outage can name another limit "lowest left" from
    // last-known values, and the chart must not swap limits under the reader
    // for that. Only a click, or the limit's absence from a whole answer (it
    // is really gone), chooses again; an incomplete answer that lacks it
    // shows the fallback without forgetting the choice.
    function chartKeyFor(harnessId, groups) {
        var picked = chartKeys[harnessId];
        var i;
        for (i = 0; i < groups.length; i++) {
            if (groups[i].key === picked) return picked;
        }
        var key = '';
        for (i = 0; i < groups.length && !key; i++) {
            if (groups[i].tightest) key = groups[i].key;
        }
        for (i = 0; i < groups.length && !key; i++) {
            if (groups[i].measured && groups[i].measured.accounts) key = groups[i].key;
        }
        if (!key && groups.length) key = groups[0].key;
        var whole = !!(currentView && (currentView.complete === undefined
            ? currentView.ok : currentView.complete === true));
        if (key && (picked === undefined || whole)) chartKeys[harnessId] = key;
        return key;
    }

    function chartMissing(view) {
        if (!chartOpen) return false;
        var groups = reserveGroups(view, selectedHarness);
        if (!groups.length) return false;
        var chart = view.reserve.chart;
        return !chart || chart.group_key !== chartKeyFor(selectedHarness, groups)
            || chart.horizon !== horizon;
    }

    // After any drawing: when the chart is open and the one on hand is for
    // another limit or horizon, ask once for the right one. The same question
    // is never asked twice in a row, so an answer that cannot match does not
    // turn into a loop.
    function askForChart() {
        if (stopped || inFlight || !currentView || !chartMissing(currentView)) return;
        var url = reserveUrl(true);
        if (url === chartAsked) return;
        chartAsked = url;
        Promise.resolve().then(function () { load(true); });
    }

    function num(value, digits) {
        return (typeof value === 'number' && isFinite(value)) ? value.toFixed(digits) : '';
    }

    function plural(n, word) {
        return n + ' ' + word + (n === 1 ? '' : 's');
    }

    // A share left, as a whole percent — except that rounding never makes a
    // share that is there read as none, or one that is not full read as full.
    function leftPct(value) {
        if (typeof value !== 'number' || !isFinite(value)) return '';
        var shown = Math.round(value);
        if (shown <= 0 && value > 0) return '<1';
        if (shown >= 100 && value < 100) return '>99';
        return String(shown);
    }

    // The same share to the hundredth of a percent, for one bar under the
    // pointer: 0.04% stays 0.04%, and a share not at the limit never reads 0.
    function exactPct(value, spent) {
        var shown = +value.toFixed(2);
        if (shown <= 0 && !spent) return '<0.01';
        if (shown >= 100 && value < 100) return '>99.99';
        return String(shown);
    }

    // A share cell at the limit: the skill's verdict on the unrounded share.
    function shareSpent(s) {
        return typeof s.at_limit === 'boolean' ? s.at_limit : s.left <= 0.0001;
    }

    // The account-windows left, to two decimals — and "<0.01" when accounts
    // not at the limit add up to less than that, not a "0.00" that reads as
    // every one of them spent.
    function windowsLeft(m) {
        var text = num(m.windows, 2);
        return text === '0.00' && (m.at_limit || 0) < (m.accounts || 0) ? '<0.01' : text;
    }

    function tzWords() {
        var offset = -new Date().getTimezoneOffset();
        var sign = offset >= 0 ? '+' : '−';
        var abs = Math.abs(offset);
        var words = 'UTC' + sign + Math.floor(abs / 60) + (abs % 60 ? ':' + pad2(abs % 60) : '');
        try {
            var name = Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (name) words = name + ' (' + words + ')';
        } catch (err) {
            // No zone name in this frame: the offset alone is still true.
        }
        return words;
    }

    function clockAt(iso) {
        var at = new Date(String(iso || ''));
        if (isNaN(at.getTime())) return '';
        return pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    function minutesWords(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
        var mins = Math.round(seconds / 60);
        return mins < 90 ? (mins + ' min') : (Math.round(mins / 6) / 10 + ' h');
    }

    function localDay(at) {
        return new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime();
    }

    // A moment ahead, the way a person says it: "today 22:59", "tomorrow
    // 14:10", "Thu 09:00" within the week, else the date. Days are counted
    // by the calendar, not by 24-hour blocks, so 00:30 tomorrow is tomorrow.
    function whenWords(iso) {
        var at = new Date(String(iso || ''));
        if (isNaN(at.getTime())) return '';
        var days = Math.round((localDay(at) - localDay(new Date())) / 86400000);
        var clock = pad2(at.getHours()) + ':' + pad2(at.getMinutes());
        if (days === 0) return 'today ' + clock;
        if (days === 1) return 'tomorrow ' + clock;
        if (days > 1 && days < 7) return WEEKDAYS[at.getDay()] + ' ' + clock;
        return formatResetAt(iso);
    }

    // The whole moment, for a tooltip or a table: "Sat 27 Sep, 14:05".
    function momentWords(seconds) {
        var at = new Date(seconds * 1000);
        if (isNaN(at.getTime())) return '';
        return WEEKDAYS[at.getDay()] + ' ' + at.getDate() + ' ' + MONTHS[at.getMonth()]
            + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    function ageWords(iso) {
        var at = Date.parse(String(iso || ''));
        if (!isFinite(at)) return '';
        var mins = Math.round(Math.max(0, Date.now() - at) / 60000);
        if (mins < 2) return 'just now';
        if (mins < 90) return mins + ' min';
        if (mins < 2880) return Math.round(mins / 60) + ' h';
        return Math.round(mins / 1440) + ' d';
    }

    // How old the readings behind the family's figures are: the provider's
    // own observation times, never the moment this page asked for them. The
    // oldest counts, because every measured account is in the figures.
    var OLD_READING_MS = 30 * 60000;

    function readingAge(summary, groups) {
        var newest = '';
        var oldest = '';
        groups.forEach(function (g) {
            var o = g.observed || {};
            if (o.newest_at && (!newest || o.newest_at > newest)) newest = o.newest_at;
            if (o.oldest_at && (!oldest || o.oldest_at < oldest)) oldest = o.oldest_at;
        });
        if (!newest) return { text: 'No fresh reading', old: true, detail: 'no fresh reading' };
        var a = ageWords(oldest);
        var b = ageWords(newest);
        var text;
        if (a === b) {
            text = a === 'just now' ? 'Observed just now' : 'Observed ' + a + ' ago';
        } else if (b === 'just now') {
            text = 'Observed up to ' + a + ' ago';
        } else {
            var unit = / (min|h|d)$/.exec(a);
            text = 'Observed ' + (unit && b.slice(-unit[0].length) === unit[0]
                ? b.slice(0, -unit[0].length) : b) + '–' + a + ' ago';
        }
        var detail = 'provider readings observed ' + clockAt(oldest)
            + (clockAt(oldest) !== clockAt(newest) ? '–' + clockAt(newest) : '')
            + (summary.status_read_at ? ' · status read ' + clockAt(summary.status_read_at) : '')
            + ' · times in ' + tzWords();
        return { text: text, old: Date.now() - Date.parse(oldest) > OLD_READING_MS, detail: detail };
    }

    // Words used in a limit's details, where there is room for them.
    var FLAG_WORDS = {
        disabled: 'disabled',
        signed_out: 'signed out',
        auth_failed: 'check failed',
        cooling: 'cooldown reported',
        model_exhausted: 'with this model limit reported reached',
        other_limit_spent: 'with another limit spent',
        account_state_unknown: 'account state not read'
    };
    // And the shorter ones a row's tail uses.
    var FLAG_SHORT = {
        disabled: 'disabled',
        signed_out: 'signed out',
        auth_failed: 'check failed',
        cooling: 'cooling down',
        model_exhausted: 'model limit reported reached',
        other_limit_spent: 'blocked by another limit',
        account_state_unknown: 'state not read'
    };

    var LENGTH_NAMES = { week: 'Weekly', day: 'Daily', hour: 'Hourly', minute: 'Per-minute' };

    // "5 hours" → "5-hour", "week" → "Weekly": a limit's length said as the
    // kind of limit it is.
    function lengthName(seconds) {
        var words = windowLength(seconds);
        if (!words) return 'No stated length';
        if (LENGTH_NAMES[words]) return LENGTH_NAMES[words];
        var found = /^(\d+) (\w+?)s$/.exec(words);
        return found ? found[1] + '-' + found[2] : words;
    }

    // What tells a limit apart inside its family besides its length: the
    // model its scope names (the engine's own bracket, "7 day (Fable)", else
    // the models it lists), or the pool its label names ("codex primary").
    function limitTag(g) {
        var name = splitWindowName(g.label, g.window_seconds);
        var models = g.models || [];
        if (models.length) {
            return (name.pool && name.model) ? name.model
                : (modelLabel(models, g.models_omitted) || models[0]);
        }
        // Tied to models whose names the history did not keep: still scoped,
        // never the family's shared limit of that length.
        if (g.model_scope === 'names_unknown') return 'model-scoped';
        return name.pool ? name.model : '';
    }

    // The short names of one family's limits, told apart from each other: the
    // length and the model or pool; the role ("primary") only when two would
    // otherwise read the same; then the engine's own model list; and a number
    // as the last resort. Two limits never share a name on screen, and the
    // whole scope is always in the row's spoken label and its details.
    function limitNames(groups) {
        var names = {};
        function collide() {
            var seen = {};
            groups.forEach(function (g) { seen[names[g.key]] = (seen[names[g.key]] || 0) + 1; });
            return groups.filter(function (g) { return seen[names[g.key]] > 1; });
        }
        groups.forEach(function (g) {
            var tag = limitTag(g);
            // A pool named after the family itself ("codex" in Codex) says
            // nothing the family tab does not; another pool keeps its name.
            if (tag && String(tag).toLowerCase() === String(g.harness || '').toLowerCase()) tag = '';
            names[g.key] = lengthName(g.window_seconds) + (tag ? ' · ' + tag : '');
        });
        collide().forEach(function (g) {
            var role = splitWindowName(g.label, g.window_seconds).role;
            if (role) names[g.key] += ' · ' + role;
        });
        collide().forEach(function (g) {
            var models = g.models || [];
            var more = models.length - 1 + (g.models_omitted || 0);
            names[g.key] = models.length
                ? lengthName(g.window_seconds) + ' · ' + models[0] + (more ? ' +' + more : '')
                : names[g.key] + ' · ' + String(g.label || g.meaning || '');
        });
        var counter = {};
        collide().forEach(function (g) {
            counter[names[g.key]] = (counter[names[g.key]] || 0) + 1;
            names[g.key] += ' (' + counter[names[g.key]] + ')';
        });
        return names;
    }

    // The whole scope, in words: what the short name abbreviates. A very long
    // list is shown in part, and says how much of it is not shown.
    function scopeWords(g) {
        var models = g.models || [];
        var omitted = g.models_omitted || 0;
        if (!models.length && g.model_scope === 'names_unknown') {
            return 'model-scoped: model names unavailable from the history';
        }
        return models.length ? (models.length + omitted === 1 ? 'model: ' : 'models: ') + models.join(', ')
            + (omitted ? ' +' + omitted + ' more' : '') : '';
    }

    function restrictionWords(g) {
        var out = [];
        var r = g.restrictions || {};
        Object.keys(r).forEach(function (flag) {
            var slot = r[flag] || {};
            out.push(slot.accounts + ' ' + (FLAG_WORDS[flag] || flag)
                + ' (' + num(slot.windows, 2) + ')');
        });
        return out;
    }

    // Accounts that report this limit but add nothing to the figure: stale,
    // unreadable, disagreeing sources, a reset since the reading.
    function unreadCount(g) {
        var c = g.coverage || {};
        return (c.stale_only || 0) + (c.invalid || 0) + (c.conflicting || 0) + (c.reset_passed || 0);
    }

    function coverageWords(g) {
        var c = g.coverage || {};
        var out = [];
        if (c.stale_only) out.push(c.stale_only + ' stale');
        if (c.invalid) out.push(c.invalid + ' unreadable');
        if (c.conflicting) out.push(c.conflicting + ' sources disagree');
        if (c.reset_passed) out.push(c.reset_passed + ' reset since reading');
        var lk = (g.last_known || {}).accounts;
        if (lk) out.push(lk + ' shown as last known, not current');
        // Accounts of the family with no reading of this limit at all — in
        // this answer or in the history: not zero, not counted, and perhaps
        // without this limit altogether.
        var other = typeof g.applicability_unknown === 'number'
            ? g.applicability_unknown : (c.other_family_accounts || 0);
        if (other) {
            out.push(other + ' other account' + (other === 1 ? '' : 's')
                + ': no reading, limit may not apply');
        }
        return out;
    }

    // The accounts of the view by the key the skill puts on each bar, for
    // their names. A bar whose account is not in the list keeps a neutral
    // word: the bar still counts, it is only not named.
    function accountIndex(view) {
        var out = {};
        ((view && view.groups) || []).forEach(function (group) {
            (group.accounts || []).forEach(function (a) { if (a && a.key) out[a.key] = a; });
        });
        return out;
    }

    function accountName(index, key) {
        var a = key ? index[key] : null;
        return a ? String(a.label || a.email || key) : 'an account';
    }

    // The bars of one limit as the skill sends them; an older answer without
    // them falls back on its unnamed current shares.
    function barsOf(g) {
        if (Array.isArray(g.bars)) return g.bars;
        var out = (g.shares || []).map(function (s) {
            return { state: 'current', left: s.left, at_limit: s.at_limit, restricted: s.restricted };
        });
        for (var i = 0; i < unreadCount(g); i++) out.push({ state: 'unknown', left: null, why: 'not_read' });
        return out;
    }

    // How many accounts the limit is known to apply to: the scale of its row
    // and of its chart. It does not move when a reading goes stale.
    function slotCount(g) {
        return typeof g.slots === 'number' ? g.slots : barsOf(g).length;
    }

    var UNKNOWN_WORDS = {
        not_read: 'no usable reading',
        unreadable: 'its reading could not be read',
        sources_disagree: 'its sources disagree',
        reset_passed: 'its window reset after the last reading',
        too_old: 'its last reading is older than the window',
        no_reading_in_answer: 'no reading in this answer'
    };

    function originWords(origin) {
        if (origin === 'history') return 'from the local history';
        if (origin === 'screen') return 'kept from the last answer shown here';
        if (origin === 'cached') return 'from the last answer that read quota';
        return 'reported stale';
    }

    // What one bar says under the pointer and to a screen reader.
    function barWords(bar, index, rowName) {
        var name = accountName(index, bar.account);
        if (bar.state === 'unknown') {
            var last = bar.last_reading;
            return name + ' · ' + rowName + ': unknown — ' + (UNKNOWN_WORDS[bar.why] || 'no usable reading')
                + (last && typeof last.left === 'number' ? '; last read '
                    + exactPct(last.left * 100, last.left <= 0) + '% left ' + (relTime(last.observed_at) || '') : '')
                + ' — unknown, not counted, never a zero';
        }
        var left = Math.max(0, Math.min(1, bar.left));
        var spent = !!bar.at_limit;
        if (bar.state === 'last_known') {
            // What it had when it was read, dated: never a verdict on now.
            var then = name + ': ' + exactPct(left * 100, spent) + '% left when last read'
                + (spent ? ' (at the limit then)' : '') + (bar.restricted ? ' — restricted now' : '');
            if (bar.resets_at) then += ' · reported reset ' + whenWords(bar.resets_at);
            return then + ' · last known, read ' + (relTime(bar.observed_at) || 'at an unreported time')
                + ' (' + originWords(bar.origin) + '), not current';
        }
        var words = name + ': ' + exactPct(left * 100, spent) + '% left'
            + (spent ? ' — at the limit' : '') + (bar.restricted ? ' — restricted now' : '');
        if (bar.resets_at) words += ' · ' + (spent ? 'until ' : 'resets ') + whenWords(bar.resets_at);
        if (bar.state === 'last_known') {
            words += ' · last known, read ' + (relTime(bar.observed_at) || 'at an unreported time')
                + ' (' + originWords(bar.origin) + '), not current';
        } else if (bar.observed_at) {
            words += ' · read ' + relTime(bar.observed_at);
        }
        return words;
    }

    // Restrictions that hold an account back now. "Account state not read"
    // is not one of them: it says what is unknown (said once, above the
    // rows), and painting every bar amber for it would hide the real holds.
    function heldFlags(bar) {
        return (bar.flags || []).filter(function (f) { return f !== 'account_state_unknown'; });
    }

    function heldNow(bar) {
        return heldFlags(bar).length > 0 || (!bar.flags && !!bar.restricted);
    }

    // Bars are wide enough to point at and stay one width per slot count, so
    // a row does not change its width when a reading goes stale.
    function barWidth(n) {
        if (n <= 6) return 36;
        if (n <= 12) return 28;
        if (n <= 24) return 24;
        return Math.max(6, Math.floor(600 / n) - 2);
    }

    // One bar per account the limit applies to, on one 0–100% scale, as tall
    // as the share it has left; fullest first, in the skill's own order. A
    // last-known value is hatched and dated, a share held back by a
    // restriction is amber, an account at the limit is a red base, and an
    // account with no usable value is an outlined "?" after the rest — never
    // a zero. A bar is a button: it selects its account.
    function barStrip(g, rowName, index) {
        var bars = barsOf(g);
        var n = Math.max(bars.length, 1);
        var gap = n > 24 ? 2 : 3;
        var strip = el('div', 'bars');
        strip.setAttribute('role', 'group');
        strip.setAttribute('aria-label', rowName + ': each account’s share left, fullest first');
        strip.style.gap = gap + 'px';
        strip.style.width = (n * barWidth(n) + (n - 1) * gap) + 'px';
        bars.forEach(function (bar, i) {
            var known = bar.state !== 'unknown' && typeof bar.left === 'number' && isFinite(bar.left);
            var cls = 'bar';
            if (!known) cls += ' unknown';
            if (bar.state === 'last_known') cls += ' last';
            // Red is a current reading at the limit; a last-known zero stays
            // hatched and dated, never current exhaustion.
            if (known && bar.at_limit && bar.state !== 'last_known') cls += ' spent';
            if (known && bar.restricted) cls += ' held restricted';
            var isSel = !!bar.account && bar.account === selectedAccountKey;
            if (isSel) cls += ' sel';
            if (known && bar.restricted && !heldNow(bar)) cls = cls.replace(' held restricted', ' restricted');
            var b = el('button', cls);
            b.setAttribute('type', 'button');
            if (!known) b.appendChild(el('span', 'sr-only', '?'));
            b.setAttribute('data-focus', 'bar:' + g.key + ':' + (bar.account || i));
            b.setAttribute('data-state', bar.state || 'current');
            if (known) {
                var left = Math.max(0, Math.min(1, bar.left));
                if (left > 0) {
                    var f = el('span', 'fill');
                    f.style.height = +(left * 100).toFixed(4) + '%';
                    b.appendChild(f);
                }
            }
            var words = barWords(bar, index, rowName);
            b.title = words;
            b.setAttribute('aria-label', words);
            b.setAttribute('aria-pressed', isSel ? 'true' : 'false');
            if (bar.account) {
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    selectedAccountKey = bar.account;
                    accountsOpen = false;
                    rerender();
                    restoreFocus('bar:' + g.key + ':' + bar.account);
                });
            } else {
                b.disabled = true;
            }
            strip.appendChild(b);
        });
        return strip;
    }

    // The figure of a row: what the accounts read now have left, in
    // account-windows, of the accounts the limit applies to — current
    // readings only. A last-known value is never in it: it stays its hatched
    // bar and the dated "Last known" line below. With no current reading the
    // figure is "—", never 0; a measured 0 (every current account at the
    // limit) is a real 0.
    function rowFigure(g) {
        var m = g.measured || {};
        var lk = g.last_known || {};
        return {
            any: !!m.accounts,
            text: m.accounts ? windowsLeft(m) : '—',
            of: slotCount(g),
            lastKnown: lk.accounts ? lk : null
        };
    }

    function limitTail(g, index) {
        var out = [];
        var bars = barsOf(g);
        var spent = bars.filter(function (b) { return b.state === 'current' && b.at_limit; });
        // One account is named when its name is known; otherwise, and for
        // more than one, the count is said.
        var named = function (b) { return !!(b.account && index[b.account]); };
        if (spent.length === 1 && named(spent[0])) {
            out.push({ text: accountName(index, spent[0].account) + ' at the limit'
                + (spent[0].resets_at ? ' until ' + whenWords(spent[0].resets_at) : ''), tone: 'bad' });
        } else if (spent.length) {
            out.push({ text: spent.length + ' at the limit', tone: 'bad' });
        }
        var held = bars.filter(function (b) { return b.state !== 'unknown' && heldNow(b) && !b.at_limit; });
        if (held.length) {
            var flags = {};
            held.forEach(function (b) { heldFlags(b).forEach(function (f) { flags[f] = true; }); });
            var keys = Object.keys(flags);
            var word = keys.length === 1 ? (FLAG_SHORT[keys[0]] || keys[0]) : 'restricted';
            out.push({ text: (held.length === 1 && named(held[0]) ? accountName(index, held[0].account)
                : held.length) + ' ' + word, tone: 'warn' });
        }
        // At the limit when last read: dated and muted, not exhaustion now.
        var wasSpent = bars.filter(function (b) { return b.state === 'last_known' && b.at_limit; });
        if (wasSpent.length) {
            out.push({ text: (wasSpent.length === 1 && named(wasSpent[0]) ? accountName(index, wasSpent[0].account)
                : wasSpent.length) + ' at the limit when last read'
                + (wasSpent.length === 1 ? ' (' + (relTime(wasSpent[0].observed_at) || 'at an unreported time') + ')' : ''),
                tone: '' });
        }
        var unknown = bars.filter(function (b) { return b.state === 'unknown'; }).length;
        if (unknown) out.push({ text: unknown + ' unknown — not counted', tone: '' });
        if (g.next_reset && g.next_reset.at) {
            out.push({ text: 'next reset ' + whenWords(g.next_reset.at), tone: '' });
        } else if ((g.measured || {}).accounts && g.reset_unknown === (g.measured || {}).accounts) {
            out.push({ text: 'no reset time reported', tone: '' });
        }
        return out;
    }

    function limitRow(g, name, chartKey, open, index, markTightest) {
        var inChart = chartOpen && g.key === chartKey;
        var row = el('div', 'lrow' + (inChart ? ' sel' : '') + (open ? ' open' : ''));
        var fig = rowFigure(g);
        var nameBtn = el('button', 'l-name');
        nameBtn.setAttribute('type', 'button');
        nameBtn.setAttribute('data-focus', 'limit:' + g.key);
        nameBtn.setAttribute('aria-pressed', inChart ? 'true' : 'false');
        nameBtn.appendChild(el('span', 'l-name-text', name));
        if (markTightest) {
            var badge = el('span', 'rs-tight', 'lowest left');
            badge.title = tightWords(g);
            nameBtn.appendChild(badge);
        }
        if (inChart) nameBtn.appendChild(el('span', 'on', 'in chart'));
        nameBtn.title = 'Show ' + name + ' in the chart';
        nameBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            chartKeys[g.harness] = g.key;
            if (!chartOpen) { chartOpen = true; chartAsked = ''; }
            rerender();
        });
        row.appendChild(nameBtn);

        var figure = el('div', 'l-fig');
        figure.appendChild(el('b', null, fig.text));
        figure.appendChild(el('span', 'of', ' of ' + fig.of));
        figure.title = (fig.any ? '' : 'No current reading — not zero. ')
            + 'Account-windows left now: the share each account read now has left of this limit, added up (a '
            + 'full account counts 1), of the ' + plural(fig.of, 'account') + ' it applies to. Last-known and '
            + 'unknown accounts are not counted, never as 0.';
        row.appendChild(figure);

        row.appendChild(barStrip(g, name, index));

        var m = g.measured || {};
        var sub = el('div', 'l-sub');
        if (fig.lastKnown) {
            sub.className = 'l-sub warn';
            sub.appendChild(el('span', 'sw'));
            sub.appendChild(document.createTextNode('Last known ' + num(fig.lastKnown.windows, 2) + ' · '
                + (ageWords(fig.lastKnown.oldest_observed_at) || 'age unknown')));
            sub.title = 'Last known, not current and not in the figure: ' + num(fig.lastKnown.windows, 2)
                + ' account-windows of ' + plural(fig.lastKnown.accounts, 'account') + ', read up to '
                + (relTime(fig.lastKnown.oldest_observed_at) || 'an unreported time') + '. The figure: '
                + (m.accounts ? windowsLeft(m) + ' of ' + plural(m.accounts, 'current account') : 'no current reading') + '.';
        } else if (m.accounts) {
            sub.appendChild(document.createTextNode(leftPct(m.average_remaining_pct) + '% avg left'));
        } else {
            sub.appendChild(document.createTextNode('no current reading'));
        }
        row.appendChild(sub);

        var tail = el('div', 'l-tail');
        var words = limitTail(g, index);
        words.forEach(function (w, i) {
            if (i) tail.appendChild(el('span', 'rs-dot', ' · '));
            tail.appendChild(el('span', w.tone ? 'rs-' + w.tone : null, w.text));
        });
        row.appendChild(tail);

        var more = el('button', 'l-more' + (open ? ' on' : ''));
        more.setAttribute('type', 'button');
        more.appendChild(withIcon(el('span', 'rs-chev'), 'caret', 12));
        more.setAttribute('data-focus', 'reserve:' + g.key);
        more.setAttribute('aria-expanded', open ? 'true' : 'false');
        var spoken = [name];
        if (scopeWords(g)) spoken.push(scopeWords(g));
        spoken.push(fig.any ? fig.text + ' of ' + fig.of + ' account-windows left now' : 'no current reading');
        if (fig.lastKnown) {
            spoken.push('last known ' + num(fig.lastKnown.windows, 2) + ' account-windows, read '
                + (relTime(fig.lastKnown.oldest_observed_at) || 'at an unreported time') + ', not counted');
        }
        spoken.push((m.accounts || 0) + ' current of ' + fig.of + ' accounts');
        words.forEach(function (w) { spoken.push(w.text); });
        restrictionWords(g).forEach(function (w) { spoken.push(w); });
        if (restrictionWords(g).length && num(g.unrestricted_windows, 2)) {
            spoken.push(num(g.unrestricted_windows, 2) + ' unrestricted');
        }
        coverageWords(g).forEach(function (w) { spoken.push(w); });
        if (markTightest) spoken.push('lowest average share left in this family, a ranking, not a verdict');
        more.setAttribute('aria-label', (open ? 'Hide' : 'Show') + ' details — ' + spoken.join(' — '));
        more.title = open ? 'Hide the details of this limit' : 'Details of this limit';
        more.addEventListener('click', function (e) {
            e.stopPropagation();
            openRows[g.key] = !open;
            rerender();
        });
        row.appendChild(more);
        return row;
    }

    // "lowest left" names a ranking, and its words say so: the smallest
    // average share left among the family's measured limits — not a verdict
    // on what can run, and a limit tied to models binds only those models.
    function tightWords(g) {
        return 'Lowest average share left among this family’s measured limits: a ranking, not a verdict'
            + (scopeWords(g) ? '. It binds only its own ' + scopeWords(g) + ', not the family’s other models' : '')
            + '.';
    }

    // Every account of the family this row can speak for.
    function familyTotal(g) {
        return slotCount(g) + (typeof g.applicability_unknown === 'number'
            ? g.applicability_unknown : ((g.coverage || {}).other_family_accounts || 0));
    }

    // What the collector's watch vouches for, in the words its fields mean:
    // where the current unbroken watch began, or — when it goes back further
    // than the skill looks on a request — only a lower bound, never a start.
    function watchWords(history) {
        var w = (history || {}).unbroken_watch;
        if (!w || !w.since) return '';
        if (w.exact) return 'watched without a break since ' + clockAt(w.since);
        // `since` is the earliest sweep the bounded look-back found, which
        // can be up to one sweep inside it: the honest bound is that moment,
        // not the look-back's length.
        return 'watched without a break at least since ' + clockAt(w.since);
    }

    // Where the record begins: the first collector sweep still kept.
    function recordWords(history) {
        var h = history || {};
        if (h.state === 'unavailable') {
            return 'Local history unavailable' + (h.error ? ' (' + h.error + ')' : '') + ': nothing observed can be drawn';
        }
        if (!h.collecting_since) return 'Nothing recorded yet';
        return 'Recorded since ' + formatResetAt(h.collecting_since) + ' (kept up to 14 days'
            + (h.capped_before ? '; older rows dropped by the size cap' : '') + ')';
    }

    // What the first screen leaves out, in one line: the accounts a row does
    // not count. When every row leaves out the same ones, they are named;
    // otherwise each row's own count and details say it.
    // What the bars cannot show, in one line: accounts of the family with no
    // reading of a limit at all (here or in the history). Stale, unread and
    // disputed accounts are bars of their own — hatched or "?" — and are not
    // repeated here.
    function applicabilityUnknown(g) {
        return typeof g.applicability_unknown === 'number'
            ? g.applicability_unknown : ((g.coverage || {}).other_family_accounts || 0);
    }

    function coverSentence(groups) {
        var counts = groups.map(applicabilityUnknown);
        var most = Math.max.apply(null, [0].concat(counts));
        if (!most) return '';
        if (counts.every(function (c) { return c === counts[0]; })) {
            return plural(most, 'account') + ' of this family report' + (most === 1 ? 's' : '')
                + ' no reading of ' + (groups.length > 1 ? 'these limits' : 'this limit')
                + ' — it may not apply to ' + (most === 1 ? 'it' : 'them') + '; not counted, never as zero.';
        }
        return 'Some accounts report no reading of some of these limits — they may not apply; '
            + 'not counted, never as zero. Each limit’s details say which.';
    }

    function paceWords(g, history) {
        var pace = g.recent_pace || {};
        var out = [];
        if (pace.state === 'ok' || pace.state === 'partial') {
            var span = minutesWords(pace.span_min_seconds);
            var spanMax = minutesWords(pace.span_max_seconds);
            var words = (pace.state === 'ok' ? 'Recent pace: ' : 'Recent pace (at least): ')
                + num(pace.windows_per_hour, 3) + ' account-windows/h from '
                + pace.accounts_known + ' of ' + pace.of + ' accounts, over '
                + (span === spanMax ? span : span + '–' + spanMax) + ' of readings';
            out.push(words + '.');
            if (pace.zero_growth) {
                out.push(pace.zero_growth === pace.accounts_known
                    ? 'No net change observed in that span — not proof of zero use, and not a promise that the reserve lasts.'
                    : plural(pace.zero_growth, 'account') + ' showed no net change in that span (not proof of zero use).');
            }
            if (pace.state === 'partial') {
                out.push('The rest have no comparable readings in the last hour yet.');
            }
        } else if (pace.state === 'warming_up') {
            var watched = watchWords(history);
            out.push('Recent pace: warming up — it needs at least 15 minutes of comparable watched readings within the trailing hour'
                + (watched ? ' (' + watched + ')' : '') + '.');
        } else if (pace.state === 'unavailable') {
            out.push('Recent pace unavailable: the local history could not be read.');
        } else if (pace.state === 'insufficient') {
            out.push('Recent pace: not enough comparable readings in the last hour (a reset, a gap or too few readings).');
        }
        if (pace.exhaust_before_reset) {
            out.push('At that pace ' + plural(pace.exhaust_before_reset, 'account')
                + ' would run out before the reported reset'
                + (pace.earliest_exhaustion_at ? ', the first ' + formatResetAt(pace.earliest_exhaustion_at) : '') + '.');
        }
        if (pace.reach_limit_no_reported_reset) {
            // Said apart: "before its reset" cannot be said of an account
            // that reports none.
            out.push('At that pace ' + plural(pace.reach_limit_no_reported_reset, 'account')
                + ' with no reported reset would reach the limit'
                + (pace.earliest_no_reset_reach_at ? ', the first ' + formatResetAt(pace.earliest_no_reset_reach_at) : '')
                + '.');
        }
        return out;
    }

    // A limit's details: every figure the row folds away, one line each.
    function reserveDetail(g, history) {
        var dl = el('dl', 'reserve-detail');
        function item(label, value) {
            if (!value) return;
            dl.appendChild(el('dt', null, label));
            var dd = el('dd');
            (Array.isArray(value) ? value : [value]).forEach(function (line) {
                dd.appendChild(el('div', null, line));
            });
            dl.appendChild(dd);
        }
        var m = g.measured || {};
        item('Scope', scopeWords(g));
        item('Left now', m.accounts
            ? windowsLeft(m) + ' account-windows of ' + plural(m.accounts, 'measured account')
                + ' · ' + leftPct(m.average_remaining_pct) + '% left on average'
            : 'no account with a fresh reading, so nothing is summed');
        if (m.at_limit) item('At the limit', plural(m.at_limit, 'account'));
        var limited = restrictionWords(g);
        if (limited.length) {
            item('Restricted now', limited.join(' · ')
                + (num(g.unrestricted_windows, 2) ? ' · ' + num(g.unrestricted_windows, 2) + ' unrestricted' : ''));
        }
        item('Not counted', coverageWords(g).join(' · '));
        if (g.plans && g.plans.mixed) {
            item('Plans', ['mixed plans'].concat((g.plans.breakdown || []).map(function (p) {
                return p.plan + ': ' + num(p.windows, 2) + ' account-windows across '
                    + plural(p.accounts, 'account');
            })));
        }
        if (g.possible_duplicates) {
            item('Shared sign-in', g.possible_duplicates + ' share a sign-in (they may draw on one pool)');
        }
        var resets = [];
        if (g.next_reset && g.next_reset.at) {
            resets.push('next ' + formatResetAt(g.next_reset.at)
                + (g.next_reset.accounts > 1 ? ' (' + g.next_reset.accounts + ' accounts)' : ''));
        }
        if (g.reset_unknown) {
            resets.push(g.reset_unknown + ' with no reported reset: when '
                + (g.reset_unknown === 1 ? 'it refills' : 'they refill') + ' is unknown');
        }
        item('Resets', resets.join(' · '));
        if (g.pace_to_reset) {
            item('Even use', 'to each account’s reported reset: '
                + num(g.pace_to_reset.windows_per_hour, 3) + ' account-windows/h across '
                + plural(g.pace_to_reset.accounts, 'account'));
        }
        item('Pace', paceWords(g, history));
        return dl;
    }

    function svgEl(tag, attrs) {
        var node = document.createElementNS(SVG_NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
        return node;
    }

    function legendSwatch(cls) {
        var svg = svgCanvas('0 0 22 8', 22, 8);
        if (cls === 'point-seen' || cls === 'point-unsettled') {
            svg.appendChild(svgEl('circle', { cx: 11, cy: 4, r: 3, 'class': cls }));
        } else if (cls === 'point-rail') {
            svg.appendChild(svgEl('line', { x1: 2, y1: 4, x2: 20, y2: 4, 'class': 'rail-line' }));
            svg.appendChild(svgEl('circle', { cx: 11, cy: 4, r: 3, 'class': 'point-unsettled' }));
        } else if (cls === 'gap-band' || cls === 'area-observed') {
            svg.appendChild(svgEl('rect', { x: 3, y: 0, width: 16, height: 8, rx: 1.5, 'class': cls }));
            if (cls === 'area-observed') {
                svg.appendChild(svgEl('line', { x1: 3, y1: 0.75, x2: 19, y2: 0.75, 'class': 'line-observed soft' }));
            }
        } else {
            svg.appendChild(svgEl('line', { x1: 2, y1: 4, x2: 20, y2: 4, 'class': cls }));
        }
        return svg;
    }

    function lineAt(points, t) {
        if (!points || !points.length) return null;
        var value = null;
        for (var i = 0; i + 1 < points.length; i++) {
            var a = points[i], b = points[i + 1];
            if (a[0] <= t && t <= b[0]) {
                value = b[0] === a[0] ? b[1] : a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0]);
                if (t < b[0]) break;
            }
        }
        return value;
    }

    // The observed total at t: each vertex holds until the next; null is a
    // gap; before the first vertex there is no record. The line keeps every
    // recorded change, so the vertex is found by halving, not by a walk.
    function observedAt(chart, t) {
        var past = chart.past || [];
        var lo = 0;
        var hi = past.length - 1;
        var found = -1;
        while (lo <= hi) {
            var mid = (lo + hi) >> 1;
            if (past[mid][0] <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
        }
        return found < 0 ? null : past[found][1];
    }

    // A point whose sources disagree has no value of its own. Where the line
    // runs, its mark sits on the line; where none does (a gap, or before the
    // record starts) it goes on the rail above the plot, never at a value.
    function onRail(chart, p) {
        if (!p || (p[1] !== null && p[1] !== undefined)) return false;
        var v = observedAt(chart, p[0]);
        return v === null || v === undefined;
    }

    // The single-sweep point nearest to t within `tol` seconds, or null.
    function pointNear(chart, t, tol) {
        var best = null;
        (chart.points || []).forEach(function (p) {
            var d = Math.abs(p[0] - t);
            if (d <= tol && (!best || d < Math.abs(best[0] - t))) best = p;
        });
        return best;
    }

    // What the chart says at one moment, as rows of [series, value]. The
    // tooltip, the spoken read-out and the dots on the lines all read this.
    // On a single-sweep point it is that point's total, which holds for no
    // time on either side of it.
    function valuesAt(chart, t, point) {
        if (point) return { future: false, point: true, rows: [['observed', point[1]]] };
        if (t <= chart.now) {
            var v = observedAt(chart, t);
            var rows = [['observed', v === null || v === undefined ? null : v]];
            if (chart.cohort_past) {
                var c = observedAt({ past: chart.cohort_past }, t);
                rows.push(['in estimate', c === null || c === undefined ? null : c]);
            }
            return { future: false, rows: rows };
        }
        var ahead = [];
        if (chart.recent_pace) ahead.push(['estimate', lineAt(chart.recent_pace, t)]);
        if (refillShown && chart.recent_pace_refill_scenario) {
            ahead.push(['refill scenario', lineAt(chart.recent_pace_refill_scenario, t)]);
        }
        if (!ahead.length) ahead.push(['estimate', null]);
        return { future: true, rows: ahead };
    }

    // Whose total a value of the observed line is. The record sums every
    // account with a record in the range (past_accounts), whatever its
    // reading now — not the accounts with a current reading. With no record
    // at all the line is the current figure alone, at now: the accounts read
    // now (accounts) are its sum.
    function observedCount(chart) {
        if (typeof chart.past_accounts !== 'number') return chart.accounts || 0;
        return chart.past_accounts || chart.accounts || 0;
    }

    function readoutAt(chart, t, name, point) {
        var at = valuesAt(chart, t, point);
        var recorded = observedCount(chart);
        var words = at.rows.map(function (r) {
            if (at.point) {
                return r[1] === null || r[1] === undefined ? 'sources disagree at this sweep'
                    + (onRail(chart, point) ? ', no value here (≠ rail)' : '')
                    : 'observed at this sweep only ' + num(r[1], 2) + ' of ' + plural(recorded, 'account');
            }
            var of = r[0] === 'observed' ? plural(recorded, 'account')
                : ((chart.recent_pace_scope || {}).accounts || 0) + ' in the estimate';
            return r[0] + ' ' + (r[1] === null || r[1] === undefined
                ? (at.future ? 'not drawn' : 'no record (gap)') : num(r[1], 2) + ' of ' + of);
        });
        return momentWords(t) + (at.future ? ' · if the pace continues' : '') + ' — ' + name + ': ' + words.join(', ');
    }

    var GAP_BAND_SEC = 600;

    var SERIES_CLASS = { 'observed': 'line-observed', 'in estimate': 'line-cohort',
                         'estimate': 'line-pace', 'refill scenario': 'line-refill',
                         'no new use': 'line-hold', 'recent pace': 'line-pace' };

    function niceStep(max) {
        var steps = [1, 2, 5, 10, 20, 50, 100, 200, 500];
        for (var i = 0; i < steps.length; i++) {
            if (max / steps[i] <= 4) return steps[i];
        }
        return steps[steps.length - 1];
    }

    // Local clock ticks: whole hours in a step that leaves room for a label,
    // days at midnight. Counted on the calendar, so a daylight-saving change
    // moves no tick off its hour.
    function timeTicks(t0, t1, usable) {
        var hours = (t1 - t0) / 3600;
        var steps = [1, 2, 3, 6, 12, 24, 48];
        var step = 48;
        for (var i = 0; i < steps.length; i++) {
            if (usable / (hours / steps[i]) >= 46) { step = steps[i]; break; }
        }
        var ticks = [];
        var d = new Date(t0 * 1000);
        d.setHours(0, 0, 0, 0);
        for (var guard = 0; guard < 400 && d.getTime() / 1000 <= t1; guard++) {
            var t = d.getTime() / 1000;
            if (t >= t0) ticks.push({ t: t, midnight: d.getHours() === 0, day: d.getDay(), date: d.getDate(), hour: d.getHours() });
            d.setHours(d.getHours() + step);
        }
        return { ticks: ticks, step: step };
    }

    function renderChartPanel(section, groups, names, chart, history, index) {
        var panel = el('div', 'chart-panel');
        panel.setAttribute('role', 'group');
        panel.setAttribute('aria-label', 'Reserve chart');
        var key = chartKeyFor(selectedHarness, groups);
        var g = null;
        groups.forEach(function (x) { if (x.key === key) g = x; });
        if (!g) { section.appendChild(panel); return; }
        var name = names[g.key];

        var top = el('div', 'chart-top');
        var heading = el('div', 'chart-heading');
        heading.appendChild(el('span', 'chart-title', name));
        heading.appendChild(el('span', 'chart-sub', ' · left, in full accounts'));
        top.appendChild(heading);
        var seg = el('div', 'seg horizon-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Chart horizon');
        HORIZONS.forEach(function (h) {
            var on = h === horizon;
            var b = el('button', 'seg-opt' + (on ? ' active' : ''), h === '24h' ? '24 h' : '7 days');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.setAttribute('aria-label', (h === '24h' ? '24 hours' : '7 days') + ' back and ahead');
            b.setAttribute('data-focus', 'horizon:' + h);
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                horizon = h;
                rerender();
            });
            seg.appendChild(b);
        });
        top.appendChild(seg);
        panel.appendChild(top);
        if (scopeWords(g)) panel.appendChild(el('div', 'chart-scope', scopeWords(g)));

        if (!chart || chart.group_key !== g.key || chart.horizon !== horizon) {
            panel.appendChild(el('div', 'reserve-note chart-wait', 'Loading the chart for this limit…'));
            section.appendChild(panel);
            return;
        }
        if (!chart.y_max) {
            panel.appendChild(el('div', 'reserve-note', 'No account known for this limit: nothing to draw.'));
            section.appendChild(panel);
            return;
        }
        drawChart(panel, name, chart);
        // Under the chart: one sentence for the estimate, one line for who is
        // not in it, one for where the line stops. The rest is in the notes.
        var said = estimateWords(g, chart, history, index);
        var facts = el('div', 'chart-facts');
        said.shown.forEach(function (f) {
            facts.appendChild(el('div', f[1] || null, f[0]));
        });
        panel.appendChild(facts);
        panel.appendChild(chartTable(chart, said.notes));
        section.appendChild(panel);
    }

    var PACE_REASON = {
        few_observations: 'too few readings yet',
        short_span: 'under 15 min of readings',
        gap: 'a gap in the watch',
        reset: 'its reported reset moved in the last hour',
        ratio_drop: 'use fell (a restore)',
        plan: 'plan changed',
        correction: 'reading corrected',
        history_unavailable: 'history unreadable'
    };

    function paceReasonWords(pace) {
        if (!pace) return 'no pace';
        return PACE_REASON[pace.reason] || (pace.state === 'warming_up' ? 'warming up' : 'no comparable readings');
    }

    // "33 min", "30–33 min", "1.2–2 h": one unit said once.
    function spanWords(lo, hi) {
        var a = minutesWords(lo);
        var b = minutesWords(hi);
        if (a === b) return a;
        var unit = / (min|h)$/.exec(a);
        if (unit && b.slice(-unit[0].length) === unit[0]) return a.slice(0, -unit[0].length) + '–' + b;
        return a + '–' + b;
    }

    function untilWords(fromIso, toIso) {
        var a = Date.parse(String(fromIso || '')), b = Date.parse(String(toIso || ''));
        if (!isFinite(a) || !isFinite(b) || b <= a) return '';
        var mins = Math.round((b - a) / 60000);
        if (mins < 90) return mins + ' min';
        if (mins < 2880) return Math.round(mins / 6) / 10 + ' h';
        return Math.round(mins / 144) / 10 + ' days';
    }

    // The estimate in words, from the same numbers the line is drawn from.
    // Conditional throughout: what the observed net change would give if it
    // continued and nothing refilled — never what will happen. A small
    // observed change is stated as the change it is.
    function estimateWords(g, chart, history, index) {
        var shown = [];
        var notes = [];
        var bars = barsOf(g);
        var pace = g.recent_pace || {};
        var scope = chart.recent_pace_scope || {};
        var cohort = bars.filter(function (b) { return b.state === 'current' && b.pace && b.pace.state === 'ok'; });
        var spanText = spanWords(pace.span_min_seconds, pace.span_max_seconds);
        if (!cohort.length || !chart.recent_pace) {
            if (pace.state === 'warming_up') {
                var watched = watchWords(history);
                shown.push(['No estimate yet: no account has 15 minutes of comparable readings within the last hour'
                    + (watched ? ' (' + watched + ')' : '') + '.', '']);
            } else if (pace.state === 'unavailable') {
                shown.push(['No estimate: the local history could not be read.', '']);
            } else if (pace.state === 'no_measured_accounts' || !(g.measured || {}).accounts) {
                shown.push(['No estimate: no account has a current reading of this limit.', '']);
            } else {
                shown.push(['No estimate: no account has comparable readings over the last hour '
                    + '(a reset, a gap or too few readings).', '']);
            }
        } else {
            // The skill's one predicate (reaches_limit): before a reported
            // reset, or — said apart — with no reset reported at all.
            var byTime = function (a, b) { return a.pace.reaches_limit_at < b.pace.reaches_limit_at ? -1 : 1; };
            var reach = cohort.filter(function (b) {
                return b.pace.reaches_limit_at && b.pace.reset_reported !== false;
            });
            var noReset = cohort.filter(function (b) {
                return b.pace.reaches_limit_at && b.pace.reset_reported === false;
            });
            reach.sort(byTime);
            noReset.sort(byTime);
            var lead = 'At the pace observed over the last ' + spanText + ', if it continued';
            var noResetLine = noReset.length
                ? ' With no reset reported, ' + (noReset.length === 1
                    ? accountName(index, noReset[0].account) + ' would reach it'
                    : plural(noReset.length, 'account') + ' would reach it, the first')
                  + ' about ' + whenWords(noReset[0].pace.reaches_limit_at) + '.'
                : '';
            if (reach.length) {
                var first = reach[0];
                var before = first.resets_at ? untilWords(first.pace.reaches_limit_at, first.resets_at) : '';
                var line = lead + ' and nothing refilled first, ' + accountName(index, first.account)
                    + ' would reach the limit about ' + whenWords(first.pace.reaches_limit_at)
                    + (before ? ' (' + before + ' before its reported reset)' : '') + '.';
                if (reach.length > 1) line += ' ' + (reach.length - 1) + ' more would too.';
                var change = first.pace.change;
                if (typeof change === 'number' && change <= 0.0101) {
                    line += ' That pace rests on an observed change of ' + leftPct(change * 100)
                        + ' percentage point' + (Math.round(change * 100) === 1 ? '' : 's') + '.';
                }
                shown.push([line + noResetLine, '']);
            } else {
                shown.push([lead + ', none of the ' + plural(cohort.length, 'account') + ' in the estimate '
                    + 'would reach the limit before a reported reset.' + noResetLine, '']);
            }
        }
        // Who is not in the line, by name when there are few.
        var out = [];
        bars.forEach(function (b) {
            if (b.state === 'current' && !(b.pace && b.pace.state === 'ok')) {
                out.push({ account: b.account, why: paceReasonWords(b.pace) });
            } else if (b.state === 'last_known') {
                out.push({ account: b.account, why: 'last known, not current' });
            } else if (b.state === 'unknown') {
                out.push({ account: b.account, why: 'unknown' });
            }
        });
        if (out.length && cohort.length) {
            var text;
            if (out.length <= 3) {
                text = out.map(function (o) { return accountName(index, o.account) + ' (' + o.why + ')'; }).join(', ');
            } else {
                var by = {};
                out.forEach(function (o) { by[o.why] = (by[o.why] || 0) + 1; });
                text = Object.keys(by).map(function (w) { return by[w] + ' ' + w; }).join(', ');
            }
            shown.push(['Not in the estimate: ' + text + '. The line sums the other '
                + plural(scope.accounts || cohort.length, 'account') + ' only.', 'fact-muted']);
        }
        var horizonWords = (chart.end - chart.now) >= 7 * 86400 ? 'the 7-day horizon' : 'the 24-hour horizon';
        if (chart.recent_pace) {
            shown.push(['An estimate, not a promise: no additional unreported resets are assumed, and '
                + horizonWords + ' is how far it is drawn, not how far it is reliable.'
                + (scope.stops_at_reset && scope.until
                    ? ' It stops at ' + whenWords(new Date(scope.until * 1000).toISOString())
                        + ', the first reported reset among these accounts: what a window does after its reset is not observed.'
                    : ''), 'fact-muted chart-caveat']);
        }
        if (pace.zero_growth) {
            notes.push(plural(pace.zero_growth, 'account') + ' showed no net change in that span — not proof '
                + 'of zero use.');
        }
        if (chart.recent_pace_refill_scenario) {
            notes.push('Refill scenario (off unless chosen in the legend): the same pace, with each account '
                + 'refilled to a full window at its next reported reset. A reported reset time is not evidence '
                + 'of a full refill.');
        }
        if (chart.past_clipped_before) {
            notes.push('Changes before ' + momentWords(chart.past_clipped_before) + ' are not drawn: '
                + 'more were recorded in this range than one chart holds.');
        }
        if (g.reset_unknown) {
            notes.push(g.reset_unknown + (g.reset_unknown === 1 ? ' account reports' : ' accounts report')
                + ' no reset time.');
        }
        var watchedNote = watchWords(history);
        notes.push(recordWords(history) + (watchedNote ? '; ' + watchedNote : '') + ' · times in ' + tzWords() + '.');
        notes.push('The observed line sums every account of this limit with a record in this range ('
            + (chart.past_accounts || 0) + ' of ' + (chart.y_max || 0)
            + (!chart.past_accounts && chart.accounts
                ? '; with none, it is only the current figure of ' + plural(chart.accounts, 'account') + ' at now' : '')
            + '), whatever its reading now, and stops '
            + 'wherever any of them was not vouched for — an outage, a sweep that did not see it, a reset, a '
            + 'stale reading — never drawing a zero there. A total seen at one sweep only (a reading seen once, the last one '
            + 'before a gap) is a dot of its own, never a stretch of line.'
            + ((chart.points || []).some(function (p) { return onRail(chart, p); })
                ? ' One whose sources disagree has no value: it sits on the line where the line runs, '
                    + 'and on the ≠ rail above the plot where it does not.' : '')
            + ' The scale is every account the limit applies to.');
        return { shown: shown, notes: notes };
    }

    // Steps of a held line ([t, v] holds v until the next t; null is a gap)
    // as separate runs of points, so a gap is never bridged.
    function stepRuns(line, x, y, t0) {
        var runs = [];
        var cur = null;
        var prev = null;
        (line || []).forEach(function (p) {
            var px = x(Math.max(p[0], t0));
            if (p[1] === null || p[1] === undefined) {
                if (cur && prev) { cur.push([px, y(prev[1])]); runs.push(cur); }
                cur = null;
                prev = null;
                return;
            }
            if (!cur) cur = [[px, y(p[1])]];
            else { cur.push([px, y(prev[1])]); cur.push([px, y(p[1])]); }
            prev = p;
        });
        if (cur) runs.push(cur);
        return runs;
    }

    function runPath(runs) {
        return runs.map(function (run) {
            return 'M' + run.map(function (pt) { return pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join('L');
        }).join('');
    }

    function runArea(runs, base) {
        return runs.filter(function (run) { return run.length > 1; }).map(function (run) {
            var first = run[0], last = run[run.length - 1];
            return 'M' + first[0].toFixed(1) + ' ' + base.toFixed(1) + 'L'
                + run.map(function (pt) { return pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join('L')
                + 'L' + last[0].toFixed(1) + ' ' + base.toFixed(1) + 'Z';
        }).join('');
    }

    function drawChart(panel, name, chart) {
        var avail = (root && root.clientWidth) || 580;
        var W = Math.max(260, Math.min(1000, Math.floor(avail - 24 - 30 - 26)));
        var H = W < 460 ? 188 : 218;
        // Two rows above the plot: what each side is (observed, estimate),
        // then the reported resets — so neither label lands on the other.
        var pl = 30, pr = 12, pt = 32, pb = 24;
        var now = chart.now;
        var past = chart.past || [];
        var points = chart.points || [];
        var railed = points.filter(function (p) { return onRail(chart, p); });
        var railY = null;
        if (railed.length) {
            railY = pt + 5;
            pt += 18;
            H += 18;
        }
        var first = null;
        past.forEach(function (p) {
            if (first === null && p[1] !== null && p[1] !== undefined) first = p[0];
        });
        points.forEach(function (p) {
            if (first === null || p[0] < first) first = p[0];
        });
        // The time axis is the chosen range, whole hours at both ends — never
        // where the record happens to begin. It moves once an hour, not with
        // every reading, and an empty stretch is drawn as empty.
        var t0 = Math.floor(chart.start / 3600) * 3600;
        var t1 = t0 + Math.ceil((chart.end - t0) / 3600) * 3600;
        // The scale is every account the limit applies to.
        var ymax = Math.max(1, chart.y_max || 1);
        function x(t) { return pl + (t - t0) / (t1 - t0) * (W - pl - pr); }
        function y(v) { return pt + (1 - v / ymax) * (H - pt - pb); }
        var bottom = H - pb;

        var plot = el('div', 'chart-plot');
        plot.setAttribute('tabindex', '0');
        plot.setAttribute('data-focus', 'chart-plot');
        plot.setAttribute('role', 'group');
        plot.setAttribute('aria-label', 'Chart of ' + name + ', account-windows left. '
            + 'Arrow keys move through time; the table below lists the same values.');
        var svg = svgCanvas('0 0 ' + W + ' ' + H, W, H);
        svg.setAttribute('class', 'chart-svg');
        var defs = svgEl('defs', {});
        var hatch = svgEl('pattern', { id: 'cq-gap-hatch', width: 6, height: 6,
            patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
        hatch.appendChild(svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 6, 'class': 'gap-hatch-line' }));
        defs.appendChild(hatch);
        svg.appendChild(defs);

        svg.appendChild(svgEl('rect', { x: x(now), y: pt, width: Math.max(0, x(t1) - x(now)), height: bottom - pt,
            'class': 'future-bg' }));

        // Gaps in the record after it began: a pale band, never a zero.
        var gaps = 0;
        for (var i = 0; i < past.length; i++) {
            var v = past[i][1];
            if (v !== null && v !== undefined) continue;
            if (first === null || past[i][0] < first) continue;
            var gs = Math.max(past[i][0], t0);
            var ge = Math.min(i + 1 < past.length ? past[i + 1][0] : now, now);
            // The line breaks at every gap, however short; a band marks only
            // a gap of ten minutes or more — the few minutes after one
            // account's reset, before it is read again, would otherwise
            // stripe the whole plot.
            if (ge - gs >= GAP_BAND_SEC && x(ge) - x(gs) >= 1.5) {
                svg.appendChild(svgEl('rect', { x: x(gs), y: pt, width: x(ge) - x(gs), height: bottom - pt,
                    'class': 'gap-band' }));
                gaps++;
            }
        }

        var step = niceStep(ymax);
        for (var v2 = 0; v2 < ymax - step * 0.35; v2 += step) {
            svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(v2), y2: y(v2), 'class': v2 ? 'grid' : 'grid base' }));
            var lab = svgEl('text', { x: pl - 6, y: y(v2) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
            lab.textContent = String(v2);
            svg.appendChild(lab);
        }
        svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(ymax), y2: y(ymax), 'class': 'grid cap' }));
        var capLab = svgEl('text', { x: pl - 6, y: y(ymax) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
        capLab.textContent = String(ymax);
        svg.appendChild(capLab);

        var ticks = timeTicks(t0, t1, W - pl - pr);
        ticks.ticks.forEach(function (tick) {
            var tx = x(tick.t);
            if (tick.midnight) {
                svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: pt, y2: bottom, 'class': 'day-rule' }));
            }
            svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: bottom, y2: bottom + 4, 'class': 'tick' }));
            if (Math.abs(tx - x(now)) < (chart.kept ? 44 : 30) || tx < pl + 12 || tx > W - pr - 12) return;
            var label = svgEl('text', { x: tx, y: H - 6, 'text-anchor': 'middle',
                'class': 'axis-text' + (tick.midnight ? ' day' : '') });
            label.textContent = tick.midnight ? WEEKDAYS[tick.day] + ' ' + tick.date : pad2(tick.hour) + ':00';
            svg.appendChild(label);
        });

        // Reported resets: a short mark on the base and a count above — the
        // engine said when, which is not evidence of a refill.
        var lastLabel = null;
        var resetTop = railY === null ? pt : railY - 6;
        (chart.resets || []).forEach(function (r) {
            svg.appendChild(svgEl('line', { x1: x(r.at), x2: x(r.at), y1: resetTop, y2: bottom, 'class': 'reset-line' }));
            if (lastLabel && x(r.at) - lastLabel.x < 16) {
                lastLabel.count += r.accounts;
                lastLabel.node.textContent = '↻' + lastLabel.count;
                return;
            }
            var label = svgEl('text', { x: x(r.at), y: resetTop - 6, 'text-anchor': 'middle', 'class': 'reset-text' });
            label.textContent = '↻' + (r.accounts > 1 ? r.accounts : '');
            svg.appendChild(label);
            lastLabel = { x: x(r.at), count: r.accounts, node: label };
        });

        svg.appendChild(svgEl('line', { x1: x(now), x2: x(now), y1: 3, y2: bottom + 4, 'class': 'now-line' }));
        var nowLab = svgEl('text', { x: x(now), y: H - 6, 'text-anchor': 'middle', 'class': 'axis-text now' });
        // A kept chart stays on the time axis of its own answer: its moment
        // is when that answer was read, not now.
        nowLab.textContent = chart.kept ? 'read ' + clockAt(new Date(now * 1000).toISOString()) : 'now';
        svg.appendChild(nowLab);
        var nowX = x(now);
        var fits = nowX >= 68;
        var seenLab = svgEl('text', fits
            ? { x: nowX - 7, y: 12, 'text-anchor': 'end', 'class': 'region-text' }
            : { x: 2, y: 12, 'text-anchor': 'start', 'class': 'region-text' });
        seenLab.textContent = nowX >= 84 ? '← observed' : 'observed';
        svg.appendChild(seenLab);
        var aheadLab = svgEl('text', { x: fits ? nowX + 7 : 74, y: 12,
            'text-anchor': 'start', 'class': 'region-text' });
        aheadLab.textContent = chart.recent_pace ? 'if the pace continues →' : (chart.kept ? 'not read since →' : 'ahead →');
        svg.appendChild(aheadLab);

        // Observed: every measured account, as a pale area; where the estimate
        // covers fewer accounts, the same accounts' own total as the line it
        // continues. A null is a gap: the line stops there.
        var cohort = chart.cohort_past || null;
        var runs = stepRuns(past, x, y, t0);
        var area = runArea(runs, bottom);
        if (area) svg.appendChild(svgEl('path', { d: area, 'class': 'area-observed' }));
        var d = runPath(runs);
        if (d) svg.appendChild(svgEl('path', { d: d, 'class': cohort ? 'line-observed soft' : 'line-observed' }));
        if (cohort) {
            var cd = runPath(stepRuns(cohort, x, y, t0));
            if (cd) svg.appendChild(svgEl('path', { d: cd, 'class': 'line-cohort' }));
        }
        if (railY !== null) {
            svg.appendChild(svgEl('line', { x1: pl, x2: x(now), y1: railY, y2: railY, 'class': 'rail-line' }));
            var railLab = svgEl('text', { x: pl - 6, y: railY + 4, 'text-anchor': 'end', 'class': 'rail-text' });
            railLab.textContent = '≠';
            svg.appendChild(railLab);
        }
        points.forEach(function (p) {
            if (onRail(chart, p)) {
                svg.appendChild(svgEl('circle', { cx: x(p[0]).toFixed(1), cy: railY, r: 3,
                    'class': 'point-mark point-unsettled point-rail' }));
                return;
            }
            var pv = p[1] === null || p[1] === undefined ? observedAt(chart, p[0]) : p[1];
            svg.appendChild(svgEl('circle', { cx: x(p[0]).toFixed(1), cy: y(pv).toFixed(1), r: 3,
                'class': p[1] === null || p[1] === undefined ? 'point-mark point-unsettled' : 'point-mark point-seen' }));
        });
        function poly(line, cls) {
            if (!line || !line.length) return;
            var path = line.map(function (p, j) {
                return (j ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1);
            }).join('');
            svg.appendChild(svgEl('path', { d: path, 'class': cls }));
        }
        if (refillShown) poly(chart.recent_pace_refill_scenario, 'line-refill');
        poly(chart.recent_pace, 'line-pace');

        var cursor = svgEl('line', { x1: 0, x2: 0, y1: railY === null ? pt : railY - 5, y2: bottom,
            'class': 'cursor-line', visibility: 'hidden' });
        svg.appendChild(cursor);
        var dotNodes = {};
        ['observed', 'in estimate', 'estimate', 'refill scenario'].forEach(function (series) {
            var dot = svgEl('circle', { cx: 0, cy: 0, r: 3.5, 'class': 'cursor-dot ' + SERIES_CLASS[series], visibility: 'hidden' });
            dotNodes[series] = dot;
            svg.appendChild(dot);
        });
        var hit = svgEl('rect', { x: pl, y: 0, width: W - pl - pr, height: H, 'class': 'hit' });
        svg.appendChild(hit);
        plot.appendChild(svg);
        var tip = el('div', 'chart-tip');
        tip.setAttribute('aria-hidden', 'true');
        tip.style.display = 'none';
        plot.appendChild(tip);
        var readout = el('div', 'sr-only chart-readout');
        readout.setAttribute('aria-live', 'polite');
        plot.appendChild(readout);
        panel.appendChild(plot);

        var cursorKey = chart.group_key + '|' + chart.horizon;
        var snap = 6 * (t1 - t0) / (W - pl - pr);
        function show(t, via) {
            t = Math.max(t0, Math.min(t1, t));
            var point = t <= now ? pointNear(chart, t, via === 'pointer' ? snap : 0) : null;
            if (point) t = point[0];
            chartCursor = { key: cursorKey, t: t, via: via };
            var cx = x(t);
            cursor.setAttribute('x1', cx);
            cursor.setAttribute('x2', cx);
            cursor.setAttribute('visibility', 'visible');
            var at = valuesAt(chart, t, point);
            Object.keys(dotNodes).forEach(function (series) { dotNodes[series].setAttribute('visibility', 'hidden'); });
            tip.textContent = '';
            tip.appendChild(el('div', 'tip-when', momentWords(t)));
            at.rows.forEach(function (r) {
                var line = el('div', 'tip-row');
                var value = r[1] === null || r[1] === undefined;
                var railMark = at.point && onRail(chart, point);
                line.appendChild(legendSwatch(at.point ? (value ? (railMark ? 'point-rail' : 'point-unsettled')
                    : 'point-seen') : SERIES_CLASS[r[0]]));
                line.appendChild(el('span', 'tip-val', value
                    ? (at.point ? 'not settled' : (at.future ? 'not drawn' : 'no record')) : num(r[1], 2)));
                line.appendChild(el('span', 'tip-name', at.point
                    ? (value ? 'sources disagree at this sweep' : 'observed at this sweep only') : r[0]));
                tip.appendChild(line);
                if (railMark) tip.appendChild(el('div', 'tip-foot', 'no value here · on the ≠ rail'));
                if (!value && dotNodes[r[0]]) {
                    var dot = dotNodes[r[0]];
                    dot.setAttribute('cx', cx);
                    dot.setAttribute('cy', y(r[1]));
                    dot.setAttribute('visibility', 'visible');
                }
            });
            tip.appendChild(el('div', 'tip-foot', (at.future ? 'if the pace continues · ' : '') + name
                + ' · account-windows left, scale ' + chart.y_max));
            tip.style.display = 'block';
            var rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
            var scale = rect && rect.width ? rect.width / W : 1;
            var width = tip.offsetWidth || 170;
            var left = cx * scale + 14;
            if (left + width > W * scale - 4) left = cx * scale - 14 - width;
            tip.style.left = Math.max(0, left) + 'px';
            tip.style.top = '4px';
            readout.textContent = readoutAt(chart, t, name, point);
        }
        function hide() {
            chartCursor = null;
            cursor.setAttribute('visibility', 'hidden');
            Object.keys(dotNodes).forEach(function (series) { dotNodes[series].setAttribute('visibility', 'hidden'); });
            tip.style.display = 'none';
        }
        function fromPointer(ev) {
            var rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
            if (!rect || !rect.width) return;
            var px = (ev.clientX - rect.left) * (W / rect.width);
            show(t0 + (px - pl) / (W - pl - pr) * (t1 - t0), 'pointer');
        }
        hit.addEventListener('pointermove', fromPointer);
        hit.addEventListener('pointerdown', fromPointer);
        hit.addEventListener('pointerleave', function () {
            if (!chartCursor || chartCursor.via === 'pointer') hide();
        });
        plot.addEventListener('keydown', function (ev) {
            var stepT = (t1 - t0) / 60;
            var at = chartCursor && chartCursor.key === cursorKey ? chartCursor.t : now;
            var next = null;
            var passed = function (lo, hi) {
                return points.filter(function (p) { return p[0] > lo && p[0] <= hi; });
            };
            if (ev.key === 'ArrowRight') {
                var ahead = passed(at, at + stepT);
                next = ahead.length ? ahead[0][0] : at + stepT;
            } else if (ev.key === 'ArrowLeft') {
                var behind = passed(at - stepT - 1e-6, at - 1e-6);
                next = behind.length ? behind[behind.length - 1][0] : at - stepT;
            } else if (ev.key === 'Home') {
                next = t0;
            } else if (ev.key === 'End') {
                next = t1;
            }
            if (next === null) return;
            if (ev.preventDefault) ev.preventDefault();
            show(next, 'key');
        });
        plot.addEventListener('focus', function () {
            show(chartCursor && chartCursor.key === cursorKey ? chartCursor.t : now, 'key');
        });
        plot.addEventListener('blur', function () {
            if (!rebuilding && chartCursor && chartCursor.via === 'key') hide();
        });
        if (chartCursor && chartCursor.key === cursorKey) {
            var keep = chartCursor;
            Promise.resolve().then(function () {
                if (plot.parentNode && chartCursor === keep) show(keep.t, keep.via);
            });
        }

        var scope = chart.recent_pace_scope || {};
        var legend = el('div', 'chart-legend');
        // Who the observed area sums: the accounts with a record in this
        // range, the same whatever is fresh now (past_basis says how many of
        // them have a current reading).
        var pastN = chart.past_accounts || 0;
        var pastOf = Math.max(pastN, chart.y_max || 0);
        var items = [['area-observed', chart.past_basis === 'last_known'
            ? 'recorded · ' + plural(pastN, 'account') + ' with no current reading now'
            : chart.past_basis === 'recorded'
                ? 'recorded · ' + plural(pastN, 'account') + ', ' + (chart.past_current_accounts || 0) + ' current now'
                : 'observed · ' + plural(pastN || chart.accounts || 0, 'current account')
                    + (pastN && pastN < pastOf ? ' (' + (pastOf - pastN) + ' with no record here)' : ''), true]];
        if (cohort) items.push(['line-cohort', 'observed · the ' + (scope.accounts || 0) + ' in the estimate', true]);
        items.push(['line-pace', chart.recent_pace
            ? 'estimate · ' + (scope.accounts || 0) + ' of ' + (scope.of || 0) + ' at their pace'
            : 'estimate — not drawn', !!chart.recent_pace]);
        items.forEach(function (item) {
            var li = el('span', 'legend-item' + (item[2] ? '' : ' off'));
            li.appendChild(legendSwatch(item[0]));
            li.appendChild(el('span', null, item[1]));
            legend.appendChild(li);
        });
        if ((chart.resets || []).length) legend.appendChild(el('span', 'legend-item', '↻ reported reset'));
        if (gaps) {
            var gl = el('span', 'legend-item');
            gl.appendChild(legendSwatch('gap-band'));
            gl.appendChild(el('span', null, 'not recorded'));
            legend.appendChild(gl);
        }
        [['point-seen', 'seen at one sweep only', function (p) { return p[1] !== null && p[1] !== undefined; }],
         ['point-unsettled', 'sources disagree at one sweep', function (p) {
             return (p[1] === null || p[1] === undefined) && !onRail(chart, p); }],
         ['point-rail', '≠ sources disagree at one sweep where no line is drawn — no value',
             function (p) { return onRail(chart, p); }]
        ].forEach(function (kind) {
            if (!points.some(kind[2])) return;
            var mark = el('span', 'legend-item');
            mark.appendChild(legendSwatch(kind[0]));
            mark.appendChild(el('span', null, kind[1]));
            legend.appendChild(mark);
        });
        if (chart.recent_pace_refill_scenario) {
            var refill = el('button', 'legend-item legend-toggle' + (refillShown ? ' on' : ''));
            refill.setAttribute('type', 'button');
            refill.setAttribute('aria-pressed', refillShown ? 'true' : 'false');
            refill.setAttribute('data-focus', 'refill-toggle');
            refill.appendChild(legendSwatch('line-refill'));
            refill.appendChild(el('span', null, 'refill scenario'));
            refill.title = 'Also draw the same pace with each account refilled to a full window at its next '
                + 'reported reset — an assumption, not an observation.';
            refill.addEventListener('click', function (e) {
                e.stopPropagation();
                refillShown = !refillShown;
                rerender();
            });
            legend.appendChild(refill);
        }
        panel.appendChild(legend);
    }

    // The notes the chart foot leaves out, then the data table. The table is
    // a sample of the observed line (every gap's beginning among it) and
    // says so: the chart and its cursor read every recorded change.
    function chartTable(chart, notes) {
        var details = el('details', 'chart-table');
        if (chartTableOpen) details.open = true;
        details.addEventListener('toggle', function () { chartTableOpen = !!details.open; });
        var summary = el('summary', null, 'Notes and data table');
        // A focus key, so the rebuild puts the keyboard back on it.
        summary.setAttribute('data-focus', 'chart-table');
        details.appendChild(summary);
        var box = el('div', 'chart-notes');
        (notes || []).forEach(function (line) { box.appendChild(el('p', null, line)); });
        var rows = (chart.table || []).filter(function (row) {
            return row.observed !== undefined && row.event !== 'now' && !row.sighting;
        });
        var sightings = (chart.table || []).filter(function (row) { return !!row.sighting; }).length;
        var pointsAll = (chart.points || []).length;
        var recorded = typeof chart.past_points === 'number' ? chart.past_points : Math.max(0, (chart.past || []).length - 1);
        var gapsAll = (chart.past || []).slice(0, -1).filter(function (p) { return p[1] === null; }).length;
        var gapsListed = rows.filter(function (row) { return row.observed === null; }).length;
        box.appendChild(el('p', 'table-note', 'The table lists ' + rows.length + ' of ' + recorded
            + ' recorded changes of the observed line, '
            + (gapsListed === gapsAll ? 'every gap’s beginning among them'
                : gapsListed + ' of ' + gapsAll + ' gaps’ beginnings among them')
            + (pointsAll ? ', and ' + (sightings === pointsAll ? 'every' : sightings + ' of ' + pointsAll)
                + ' total seen at one sweep only' : '')
            + '; the chart and its cursor use all of them. Scenario rows are at reported resets and a '
            + 'quarter, half and all of the horizon.'));
        details.appendChild(box);
        var table = el('table');
        var head = el('tr');
        ['Time', 'Observed', 'No new use', 'Recent pace', 'Event'].forEach(function (h) {
            var th = el('th', null, h);
            th.setAttribute('scope', 'col');
            head.appendChild(th);
        });
        var thead = el('thead');
        thead.appendChild(head);
        table.appendChild(thead);
        var body = el('tbody');
        (chart.table || []).forEach(function (row) {
            var tr = el('tr');
            var at = Date.parse(String(row.at || ''));
            [isFinite(at) ? momentWords(at / 1000) : row.at,
             row.observed === undefined ? '' : (row.observed === null ? (row.sighting ? 'not settled' : 'gap')
                 : num(row.observed, 2)),
             row.no_new_use === undefined || row.no_new_use === null ? '' : num(row.no_new_use, 2),
             row.recent_pace === undefined || row.recent_pace === null ? '' : num(row.recent_pace, 2),
             row.event || ''
            ].forEach(function (cell) { tr.appendChild(el('td', null, cell)); });
            body.appendChild(tr);
        });
        table.appendChild(body);
        details.appendChild(table);
        return details;
    }

    // "How to read": the unit, the cells, the count beside them, what "lowest
    // left" ranks, and how old the readings and the record are. Folded until
    // asked for; it is where every caveat the first screen leaves out lives.
    function aboutPanel(summary, age, groups) {
        var box = el('div', 'reserve-about');
        box.setAttribute('id', 'reserve-about');
        function item(title, text, extra) {
            var it = el('div', 'about-item');
            it.appendChild(el('div', 'about-title', title));
            it.appendChild(el('p', null, text));
            if (extra) it.appendChild(extra);
            box.appendChild(it);
        }
        var sample = groups.filter(function (g) { return (g.measured || {}).accounts; })[0] || groups[0];
        var scoped = groups.filter(function (g) { return (g.models || []).length; })[0];
        var names = limitNames(groups);
        item('Windows left', 'Each account counts 1 whatever its plan: its share left of a limit is added '
            + 'up, so accounts with 40% and 70% left make 1.10 account-windows. Not tokens or hours.');
        item('Bars', 'One per account the limit applies to, fullest first — each row sorted on its own, so a '
            + 'column does not follow one account from row to row; point at a bar for its account, click it to '
            + 'select that account. Every row has one scale: full height is 100% left, the base 0%. A hatched '
            + 'bar is a last-known value — dated, not current; a “?” has no usable value and is not counted.',
            stripLegend(groups, true));
        item('Figure', 'Account-windows left now, of the accounts the limit applies to: current readings only. '
            + 'Last-known values are never in it — they stay hatched bars and the dated “Last known” line under '
            + 'it. With no current reading the figure is “—”, never 0. Accounts with no reading of a limit at '
            + 'all are named below the rows, never counted as zero.');
        item('Lowest left', 'The limit whose measured accounts have the lowest average share left: a ranking, '
            + 'not a verdict on what can run. A limit scoped to models binds only the models it names, never '
            + 'the family’s other models' + (scoped ? ' — “' + names[scoped.key] + '” covers '
            + scopeWords(scoped).replace(/^models?: /, '') : '') + '.');
        item('Never added', 'Limits of different length or model are never added together: a 5-hour and a '
            + 'weekly limit bound the same work at once. Measured quota is not a dispatch guarantee: '
            + 'Claudexor decides routing.');
        var history = summary.history || {};
        var watched = watchWords(history);
        item('Readings', age.detail.charAt(0).toUpperCase() + age.detail.slice(1) + '. '
            + recordWords(history) + (watched ? '; ' + watched : '') + '.');
        return box;
    }

    function stripLegend(groups, all) {
        var kinds = { fill: !!all, last: !!all, restricted: !!all, spent: !!all, unread: !!all };
        groups.forEach(function (g) {
            barsOf(g).forEach(function (b) {
                if (b.state === 'unknown') { kinds.unread = true; return; }
                if (b.state === 'last_known') kinds.last = true;
                if (b.restricted) kinds.restricted = true;
                if (b.at_limit && b.state !== 'last_known') kinds.spent = true;
                else kinds.fill = true;
            });
        });
        var legend = el('div', 'strip-legend');
        legend.setAttribute('aria-hidden', 'true');
        [['fill', '', 'share left'],
         ['last', ' last', 'last known: dated, not current'],
         ['restricted', ' held', 'held back now (cooldown, model limit reported reached, disabled, another limit spent)'],
         ['spent', ' spent', 'at the limit: 0% left'],
         ['unread', ' unknown', 'no usable value — not counted']].forEach(function (k) {
            if (!kinds[k[0]] && k[0] !== 'fill') return;
            var item = el('span', 'legend-item');
            var cell = el('span', 'bar' + k[1], null);
            if (k[0] !== 'unread' && k[0] !== 'spent') {
                var f = el('span', 'fill');
                f.style.height = '62%';
                cell.appendChild(f);
            }
            item.appendChild(cell);
            item.appendChild(el('span', null, k[2]));
            legend.appendChild(item);
        });
        return legend;
    }

    // The one line beside the title: how much of the family is current, what
    // is shown as last known and how old, and whether Claudexor answered at
    // all. Counted over accounts, not limits: one account is one account.
    function reserveStatus(view, summary, groups) {
        var cached = (view && view.cached) || summary.cached || {};
        var cachedAt = cached.quota || cached.accounts || cached.catalog || '';
        var seen = {};
        groups.forEach(function (g) {
            barsOf(g).forEach(function (b, i) {
                var key = b.account || (g.key + '#' + i);
                var rank = b.state === 'current' ? 3 : (b.state === 'last_known' ? 2 : 1);
                if (!seen[key] || seen[key] < rank) seen[key] = rank;
            });
        });
        var keys = Object.keys(seen);
        var current = keys.filter(function (k) { return seen[k] === 3; }).length;
        var last = keys.filter(function (k) { return seen[k] === 2; }).length;
        var unknown = keys.length - current - last;
        var age = readingAge(summary, groups);
        var lkOldest = '';
        groups.forEach(function (g) {
            var o = (g.last_known || {}).oldest_observed_at;
            if (o && (!lkOldest || o < lkOldest)) lkOldest = o;
        });
        if (view && (view.transport_error || view.kept) && cachedAt) {
            return { tone: 'bad', text: 'Claudexor not read now · all as read ' + (relTime(cachedAt) || clockAt(cachedAt)),
                     detail: 'The status read failed. Every value is last known, with its age.' };
        }
        if (cached.quota) {
            return { tone: 'warn', text: 'Quota not read now · last known as read ' + (relTime(cached.quota) || clockAt(cached.quota)),
                     detail: 'The quota facet did not answer; its last answer is shown, dated, never as current.' };
        }
        if (!keys.length) return { tone: 'muted', text: age.text, detail: age.detail };
        if (!last && !unknown) {
            return { tone: age.old ? 'warn' : 'ok',
                     text: (current === 1 ? 'Read' : 'All ' + current + ' read') + ' · ' + age.text.replace(/^Observed /, 'observed '),
                     detail: age.detail };
        }
        var parts = [current + ' current'];
        if (last) parts.push(last + ' last known' + (lkOldest ? ' (' + ageWords(lkOldest) + ')' : ''));
        if (unknown) parts.push(unknown + ' unknown');
        return { tone: 'warn', text: parts.join(' · '), detail: age.detail };
    }

    // The family's accounts that stand in no row at all, by name when few:
    // switched off in Claudexor, or with no reading of these limits (here or
    // in the history) — never counted, never as zero. With the account list
    // not read now (`keptAt`: when it was) a switch is dated.
    function outsideWords(groups, family, index, keptAt) {
        if (!family || !(family.accounts || []).length) return '';
        var inBars = {};
        var named = false;
        groups.forEach(function (g) {
            barsOf(g).forEach(function (b) { if (b.account) { inBars[b.account] = true; named = true; } });
        });
        if (!named) return '';
        var missing = family.accounts.filter(function (a) { return !inBars[a.key]; });
        var off = missing.filter(function (a) { return a.enabled === false; });
        var other = missing.filter(function (a) { return a.enabled !== false; });
        function names(list) {
            return list.length <= 3
                ? list.map(function (a) { return accountName(index, a.key); }).join(', ')
                : plural(list.length, 'account');
        }
        var out = [];
        if (off.length && keptAt !== undefined && keptAt !== null) {
            var when = relTime(keptAt) || clockAt(keptAt);
            out.push(names(off) + (off.length === 1 ? ' was' : ' were')
                + ' switched off in Claudexor when last read' + (when ? ' (' + when + ')' : '') + ' — not counted.');
        } else if (off.length) {
            out.push(names(off) + (off.length === 1 ? ' is' : ' are')
                + ' switched off in Claudexor — not counted.');
        }
        if (other.length) {
            out.push(names(other) + ' — no reading of these limits, here or in the history: '
                + (other.length === 1 ? 'it' : 'they') + ' may not apply; not counted, never as zero.');
        }
        return out.join(' ');
    }

    function rosterWords(summary) {
        var cached = summary.cached || {};
        if (summary.roster === 'cached' && cached.accounts) {
            return 'Account list as read ' + (relTime(cached.accounts) || clockAt(cached.accounts))
                + ' — not read now, so no account’s current state is known.';
        }
        if (summary.roster === 'unknown') {
            return 'Account list not read — accounts with no reading in this answer are not listed.';
        }
        return '';
    }

    function renderReserve(parent, reserve, family, view) {
        if (!reserve || typeof reserve !== 'object') return;
        var section = el('section', 'reserve');
        section.setAttribute('aria-label', 'Reserve overview');
        var head = el('div', 'reserve-head');
        var title = el('h3', 'reserve-title', 'Reserve');
        if (family) title.appendChild(el('span', 'reserve-family', ' · ' + familyName(family)));
        head.appendChild(title);
        var summary = reserve.summary;
        if (!summary) {
            section.appendChild(head);
            section.appendChild(el('div', 'reserve-note', 'Reserve overview unavailable'
                + (reserve.error ? ' (' + reserve.error + ')' : '') + '. Account details below are unaffected.'));
            parent.appendChild(section);
            return;
        }
        var hid = family ? family.harness_id : '';
        var groups = reserveGroups({ reserve: reserve }, hid);
        var status = reserveStatus(view, summary, groups);
        var stamp = el('span', 'reserve-age status-' + status.tone);
        stamp.appendChild(el('span', 'pip ' + status.tone));
        stamp.appendChild(el('span', null, status.text));
        stamp.title = status.detail || '';
        head.appendChild(stamp);
        section.appendChild(head);
        section.appendChild(el('div', 'reserve-note reserve-unit',
            'Account-windows left · a full account counts 1 · limits are never added'));
        var reads = summary.reads || {};
        var quotaCached = !!(summary.cached || {}).quota;
        if (reads.quota && reads.quota !== 'ok' && !quotaCached && !groups.length) {
            section.appendChild(el('div', 'reserve-note', 'Quota was not read on this answer, and no earlier '
                + 'reading is kept in this session, so nothing is summed.'));
            parent.appendChild(section);
            return;
        }
        if (!groups.length) {
            section.appendChild(el('div', 'reserve-note', 'No quota limit reported for '
                + (family ? familyName(family) : 'this family') + '.'));
            parent.appendChild(section);
            return;
        }
        var names = limitNames(groups);
        var index = accountIndex(view);
        var chartKey = chartKeyFor(hid, groups);
        var measuredGroups = groups.filter(function (g) { return g.measured && g.measured.accounts; }).length;

        var table = el('div', 'lrows reserve-table');
        table.setAttribute('role', 'group');
        table.setAttribute('aria-label', 'Limits of ' + (family ? familyName(family) : 'this family'));
        groups.forEach(function (g) {
            var open = !!openRows[g.key];
            table.appendChild(limitRow(g, names[g.key], chartKey, open, index, g.tightest && measuredGroups > 1));
            if (open) table.appendChild(reserveDetail(g, summary.history));
        });
        section.appendChild(table);
        if (refreshedSinceSummary) {
            section.appendChild(el('div', 'reserve-note', 'Accounts were refreshed after this overview was '
                + 'computed; it catches up with the next reading.'));
        }

        // The foot: what the rows do not count, in one line, then the two
        // doors — the caveats and the chart.
        var foot = el('div', 'reserve-foot');
        // The account list as last read, when it was not read now.
        var keptAt = (view && view.kept) || summary.roster === 'cached'
            ? String((summary.cached || {}).accounts || '') : null;
        var cover = [outsideWords(groups, family, index, keptAt) || coverSentence(groups), rosterWords(summary)]
            .filter(Boolean).join(' ');
        if (cover) foot.appendChild(el('div', 'reserve-cover', cover));
        var buttons = el('div', 'foot-btns');
        var about = el('button', 'pill-btn' + (aboutOpen ? ' on' : ''), 'How to read');
        about.insertBefore(withIcon(el('span', 'pill-icon'), 'info', 12), about.firstChild);
        about.setAttribute('aria-expanded', aboutOpen ? 'true' : 'false');
        about.setAttribute('aria-controls', 'reserve-about');
        about.setAttribute('data-focus', 'reserve-about');
        about.addEventListener('click', function (e) {
            e.stopPropagation();
            aboutOpen = !aboutOpen;
            rerender();
        });
        buttons.appendChild(about);
        var toggle = el('button', 'pill-btn chart-toggle' + (chartOpen ? ' on' : ''),
            chartOpen ? 'Hide chart' : 'Show chart');
        toggle.insertBefore(withIcon(el('span', 'pill-icon'), 'chart', 12), toggle.firstChild);
        toggle.setAttribute('aria-expanded', chartOpen ? 'true' : 'false');
        toggle.setAttribute('data-focus', 'chart-toggle');
        toggle.addEventListener('click', function (e) {
            e.stopPropagation();
            chartOpen = !chartOpen;
            chartAsked = '';
            if (!chartOpen) chartCursor = null;
            rerender();
        });
        buttons.appendChild(toggle);
        foot.appendChild(buttons);
        section.appendChild(foot);
        if (aboutOpen) section.appendChild(aboutPanel(summary, readingAge(summary, groups), groups));
        if (chartOpen) renderChartPanel(section, groups, names, reserve.chart, summary.history, index);
        parent.appendChild(section);
    }

    // The selected account's own line in every limit of its family, from the
    // same bars the rows draw: its share left, its reset, and — only when its
    // own observed pace, continued, would reach the limit before that reset —
    // when. A last-known value says so, with its age; an unknown one says why.
    function accountLimits(parent, reserve, group, account) {
        var summary = reserve && reserve.summary;
        if (!summary || !group || !account) return;
        var groups = reserveGroups({ reserve: reserve }, group.harness_id);
        if (!groups.length) return;
        // Without account keys on the bars there is no telling which bar is
        // this account's: say nothing rather than "no reading".
        var keyed = groups.some(function (g) {
            return barsOf(g).some(function (b) { return !!b.account; });
        });
        if (!keyed) return;
        var names = limitNames(groups);
        var box = el('div', 'acct-lims');
        box.setAttribute('role', 'list');
        box.setAttribute('aria-label', account.label + ' in each limit');
        groups.forEach(function (g) {
            var bar = null;
            barsOf(g).forEach(function (b) { if (b.account === account.key) bar = b; });
            var line = el('div', 'acct-lim');
            line.setAttribute('role', 'listitem');
            line.appendChild(el('span', 'k', names[g.key]));
            if (!bar) {
                line.appendChild(el('span', 'm none'));
                line.appendChild(el('span', 'p meta', '—'));
                line.appendChild(el('span', 'r', 'no reading of this limit — it may not apply'));
                box.appendChild(line);
                return;
            }
            var known = bar.state !== 'unknown' && typeof bar.left === 'number';
            var meterNode = el('span', 'm' + (bar.state === 'last_known' ? ' last' : '')
                + (heldNow(bar) ? ' held' : ''));
            if (known && bar.left > 0) {
                var fill = el('i');
                fill.style.width = +(Math.max(0, Math.min(1, bar.left)) * 100).toFixed(2) + '%';
                meterNode.appendChild(fill);
            }
            line.appendChild(meterNode);
            line.appendChild(el('span', 'p' + (known && bar.at_limit && bar.state !== 'last_known' ? ' bad'
                : (known ? '' : ' meta')),
                known ? leftPct(bar.left * 100) + '%' : '?'));
            var rest = el('span', 'r');
            var parts = [];
            if (!known) {
                parts.push([UNKNOWN_WORDS[bar.why] || 'no usable reading', '']);
            } else {
                if (bar.resets_at) {
                    parts.push([(bar.state === 'last_known' ? 'reported reset '
                        : (bar.at_limit ? 'until ' : 'resets ')) + whenWords(bar.resets_at), '']);
                }
                if (bar.state === 'last_known') {
                    parts.push(['last known, read ' + (relTime(bar.observed_at) || 'at an unreported time'), 'warn']);
                } else if (bar.pace && bar.pace.reaches_limit_at) {
                    parts.push(['at its recent pace would reach the limit ~' + whenWords(bar.pace.reaches_limit_at), 'est']);
                }
                if (heldNow(bar) && !bar.at_limit) {
                    parts.push([heldFlags(bar).map(function (f) { return FLAG_SHORT[f] || f; }).join(', ') || 'restricted', 'warn']);
                }
            }
            parts.forEach(function (part, i) {
                if (i) rest.appendChild(document.createTextNode(' · '));
                rest.appendChild(el('span', part[1] || null, part[0]));
            });
            line.appendChild(rest);
            line.title = barWords(bar, accountIndex(currentView), names[g.key]);
            box.appendChild(line);
        });
        parent.appendChild(box);
    }

    function quotaIdentity(harness, subjectId) {
        return String(harness || '') + '\u0000' + String(subjectId || '');
    }

    function facetProblemNote(facets) {
        return FACET_ORDER.filter(function (name) {
            return (facets[name] || 'indeterminate') !== 'ok';
        }).map(function (name) {
            return name + ': ' + (facets[name] || 'indeterminate');
        }).join('; ');
    }

    // A foreground answer contains quota evidence only. Match it by the
    // engine-owned subject id, and preserve every non-quota account facet.
    function mergeQuotaFacet(view, response) {
        var updates = {};
        (response.quota_updates || []).forEach(function (update) {
            if (!update || !update.harness || !update.quota) return;
            updates[quotaIdentity(update.harness, update.subject_id)] = update.quota;
        });
        var merged = Object.assign({}, view || {});
        var facets = Object.assign({}, merged.facets || {});
        facets.quota = 'ok';
        merged.facets = facets;
        merged.facet_note = facetProblemNote(facets);
        merged.groups = (merged.groups || []).map(function (group) {
            var changed = false;
            var accounts = (group.accounts || []).map(function (account) {
                var quota = updates[quotaIdentity(group.harness_id, account.subject_id)];
                if (!quota) return account;
                changed = true;
                return Object.assign({}, account, { quota: quota });
            });
            return changed ? Object.assign({}, group, { accounts: accounts }) : group;
        });
        return merged;
    }

    // One request through the bridge, bounded, that never rejects: it settles
    // with {ok, status, body, error, timedOut, bodyLost} however the request
    // ends — an answer, an HTTP error, a bridge or abort error, a body that
    // never finishes or breaks off after a success status (bodyLost: an
    // answer came, its body could not be read), or no answer at all.
    // Whoever calls it decides what each means.
    function request(url, init, timeoutMs) {
        return new Promise(function (resolve) {
            var settled = false;
            var guard = null;
            var head = null;
            function done(result) {
                if (settled) return;
                settled = true;
                if (guard !== null && typeof window.clearTimeout === 'function') window.clearTimeout(guard);
                resolve(result);
            }
            guard = window.setTimeout(function () {
                done({ ok: false, status: 0, body: '', timedOut: true,
                       error: 'no answer within ' + Math.round(timeoutMs / 1000) + ' s' });
            }, timeoutMs + BACKSTOP_MS);
            var options = Object.assign({}, init || {}, { timeoutMs: timeoutMs });
            Promise.resolve().then(function () {
                return window.fetch(url, options);
            }).then(function (response) {
                if (!response || typeof response.text !== 'function') {
                    throw new Error('the bridge returned no response');
                }
                head = response;
                return Promise.resolve(response.text()).then(function (body) {
                    done({ ok: !!response.ok, status: response.status, body: String(body || ''),
                           error: '', timedOut: false, bodyLost: false });
                });
            })['catch'](function (err) {
                var message = String((err && err.message) || err || 'bridge error');
                done({ ok: false, status: head ? head.status : 0, body: '', error: message,
                       timedOut: /timed out/i.test(message), bodyLost: !!(head && head.ok) });
            });
        });
    }

    // render() rebuilds the whole tree. If drawing an answer throws half way,
    // the screen that was up before comes back (its nodes are only detached),
    // with the error said above it and a Retry — never a blank card, and never
    // an exception thrown out of a timer or a click into the host.
    function safeRender(view, staleText, actionText) {
        var kept = Array.prototype.slice.call(root.childNodes);
        var before = { view: currentView, stale: staleMessage, action: actionMessage };
        try {
            render(view, staleText, actionText);
            drawFault = '';
            return true;
        } catch (err) {
            drawFault = ((err && err.name) ? err.name + ': ' : '') + String((err && err.message) || err);
            try { console.error('claudexor quotas: the answer could not be drawn', err); } catch (e) { /* no console */ }
            currentView = before.view;
            staleMessage = before.stale;
            actionMessage = before.action;
            // The view before is drawn again with what holds now (a request
            // that has ended, a button that is free again) — kept, as every
            // fallback is (keptView): the clock has moved since it was read.
            // Only if that fails too are its old nodes put back as they were.
            // One banner at a time: the fault's own, below, says it now.
            var redrawn = false;
            if (before.view && before.view !== view) {
                try {
                    render(keptView(before.view), '', before.action);
                    redrawn = true;
                } catch (again) {
                    redrawn = false;
                }
            }
            if (!redrawn) {
                rebuilding = true;
                try { root.textContent = ''; } finally { rebuilding = false; }
                kept.forEach(function (node) { root.appendChild(node); });
            }
            var note = faultBanner(kept.length
                ? 'The latest answer could not be drawn (' + drawFault + '). The screen before it is kept.'
                : 'The answer could not be drawn (' + drawFault + ').');
            root.insertBefore(note, root.firstChild);
            // The banner above goes with the next redraw; the kept screen
            // keeps saying what it is until an answer is drawn.
            if (redrawn) staleMessage = before.stale || 'the latest answer could not be drawn';
            return false;
        }
    }

    function faultBanner(text) {
        var node = el('div', 'banner bad draw-fault');
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), 'warn'));
        node.appendChild(el('span', 'banner-text', text));
        node.appendChild(retryButton());
        return node;
    }

    // Retry is the ordinary read, asked for now: it never repeats a refresh.
    function retryButton() {
        var btn = el('button', 'pill-btn banner-retry', 'Retry');
        btn.setAttribute('data-focus', 'retry');
        btn.disabled = inFlight;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            load();
        });
        return btn;
    }

    function render(view, staleText, actionText) {
        currentView = view;
        staleMessage = staleText;
        actionMessage = actionText || '';

        // The whole tree is rebuilt every 30 seconds on its own. Without this
        // the keyboard focus would drop to the body mid-use — including focus
        // sitting on a row of an open list.
        var was = document.activeElement;
        var focusWas = (was && was.getAttribute) ? was.getAttribute('data-focus') : null;
        // A control an earlier redraw drew disabled under the keyboard is
        // taken back only while focus is still nowhere the reader chose.
        if (!focusWas && focusParked && focusLost(was)) focusWas = focusParked;
        focusParked = '';
        // And without this the reader is thrown back to the first row mid-scroll:
        // the new nodes come in at zero, however far down the old ones were.
        var scrollWas = readScroll();

        rebuilding = true;
        try {
            root.textContent = '';
        } finally {
            rebuilding = false;
        }
        accountPop = null;
        // The settings panel is a place, not a drawer: while it is open the
        // page becomes a column so the panel can stand on the frame's floor.
        root.classList.toggle('settings-open', settingsOpen);

        var facets = view.facets || {};
        var groups = view.groups || [];

        var selection = syncSelection(groups);

        // The family controls own the overview. Account selection sits below it.
        var daemon = view.daemon || {};
        var daemonDown = !!(daemon.state && daemon.state !== 'running');
        var facetProblem = FACET_ORDER.some(function (f) {
            return (facets[f] || 'indeterminate') !== 'ok';
        });
        // Before the first answer arrives nothing has been read and nothing has
        // failed: a red pip then would be a claim about data we do not have.
        var hasAnswer = !!(daemon.state || groups.length || Object.keys(facets).length
            || view.transport_error);
        var statusProblem = hasAnswer && (daemonDown || facetProblem || !!view.transport_error);

        var controlBar = el('div', 'control-bar');

        renderHarnessSeg(controlBar, groups, selection.group, hasAnswer, facets);

        // Explicit actions beside the labelled family choices.
        var actionSeg = el('div', 'action-seg');
        actionSeg.setAttribute('role', 'group');
        actionSeg.setAttribute('aria-label', 'Settings and refresh');
        var settingsBtn = el('button', 'action-btn action-settings' + (settingsOpen ? ' is-open' : '')
            + (statusProblem ? ' has-problem' : ''));
        settingsBtn.appendChild(icon('density', 14));
        settingsBtn.appendChild(el('span', null, 'Settings'));
        settingsBtn.setAttribute('aria-expanded', settingsOpen ? 'true' : 'false');
        // One button, two facts: what it opens, and whether something in there
        // needs looking at. The pip below carries the second one visually.
        settingsBtn.setAttribute('aria-label', 'Settings and system state'
            + (!hasAnswer ? ' — nothing read yet'
                : (statusProblem ? ' — the daemon or a facet did not answer' : ''))
            + ' · detail now ' + density);
        settingsBtn.title = settingsBtn.getAttribute('aria-label');
        settingsBtn.setAttribute('data-focus', 'settings');
        settingsBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            settingsOpen = !settingsOpen;
            accountsOpen = false;
            rerender();
        });
        settingsBtn.appendChild(el('span', 'pip seg-pip '
            + (!hasAnswer ? 'muted' : (statusProblem ? 'bad' : 'ok'))));
        actionSeg.appendChild(settingsBtn);

        var refreshBtn = el('button', 'action-btn action-refresh' + (inFlight ? ' is-refreshing' : ''));
        refreshBtn.appendChild(withIcon(el('span', 'icon-spin'), 'refresh', 14));
        refreshBtn.appendChild(el('span', null, 'Refresh'));
        refreshBtn.disabled = inFlight;
        // The schedule is a promise about attempts, not about data: an attempt
        // against a daemon that is down brings nothing.
        refreshBtn.setAttribute('aria-label', inFlight ? 'Refreshing…'
            : 'Refresh — the widget re-reads on its own every '
                + Math.round(REFRESH_MS / 1000) + ' seconds');
        refreshBtn.title = refreshBtn.getAttribute('aria-label');
        refreshBtn.setAttribute('data-focus', 'refresh');
        refreshBtn.addEventListener('click', function () {
            if (inFlight) return;
            refreshQuota();
        });
        actionSeg.appendChild(refreshBtn);
        controlBar.appendChild(actionSeg);
        root.appendChild(controlBar);

        /* 2. Banners. What the settings card folds away is only ever the good
           news: a daemon that is down and a facet that did not answer both
           still say so in the open, above the account — and they speak for
           every family, not only the one on screen. */
        if (daemonDown) {
            banner('warn', 'Claudexor daemon is ' + daemon.state
                + '. Readings below are last known, not live.', true);
        }
        var cachedFacets = view.cached || {};
        var cachedNames = FACET_ORDER.filter(function (f) { return !!cachedFacets[f]; });
        if (staleMessage) {
            banner('warn', 'Reading could not be refreshed (' + staleMessage + '). The last answer, received '
                + (relSince(lastGoodAt) || 'earlier') + ', is kept: nothing in it is current, and each value '
                + 'is dated by when it was observed.', true).appendChild(retryButton());
        }
        if (actionMessage) {
            banner('warn', actionMessage, true);
        }
        if (view.transport_error && !staleMessage) {
            var lastAt = cachedNames.length ? cachedFacets[cachedNames[0]] : '';
            var down = banner('error', lastAt
                ? 'Claudexor status could not be read. Everything below is last known from '
                    + (relTime(lastAt) || clockAt(lastAt)) + ' — nothing is current.'
                : 'Endpoint unreachable. No quota claims made.', true);
            // The raw error stays out of the screen: it can carry local paths
            // or a vendor's text.
            down.appendChild(retryButton());
        } else if (view.facet_note) {
            var unreadFacets = FACET_ORDER.filter(function (f) { return (facets[f] || 'indeterminate') !== 'ok'; });
            var kept = unreadFacets.filter(function (f) { return !!cachedFacets[f]; });
            banner('info', 'Not read now: ' + view.facet_note + '. '
                + (kept.length ? kept.join(', ') + ' shown as last known from '
                    + (relTime(cachedFacets[kept[0]]) || clockAt(cachedFacets[kept[0]])) + ', never as current or zero.'
                    : 'Values shown as unread/last known, not zero.'), false);
        }
        if (saveError) {
            banner('warn', 'Display choice was not saved (' + saveError
                + '). It holds until the next reading and then reverts.', true);
        }

        /* 3. The reserve overview of the chosen family, then settings take the
           whole plane, or the one selected account. */
        if (!settingsOpen && hasAnswer) renderReserve(root, view.reserve, selection.group, view);
        if (settingsOpen && selection.account) {
            var chooser = el('div', 'account-toolbar');
            chooser.appendChild(el('span', 'account-label', 'Account'));
            renderAccountSelect(chooser, selection.group, selection.account, hasAnswer, facets);
            root.appendChild(chooser);
        }
        if (settingsOpen) {
            var panel = el('div', 'settings-panel');
            panel.setAttribute('role', 'group');
            panel.setAttribute('aria-label', 'Settings');

            var tabs = el('div', 'settings-tabs');
            tabs.setAttribute('role', 'tablist');
            SETTINGS_TABS.forEach(function (tab) {
                var on = tab.key === settingsTab;
                var b = el('button', 'settings-tab' + (on ? ' active' : ''));
                b.appendChild(icon(tab.icon, 13));
                b.appendChild(el('span', null, tab.name));
                b.setAttribute('role', 'tab');
                b.setAttribute('aria-selected', on ? 'true' : 'false');
                b.setAttribute('tabindex', on ? '0' : '-1');
                // In a narrow frame a tab not open shows only its icon (the rule
                // on .settings-panel); its name stays under the pointer and in
                // what a screen reader announces.
                b.title = tab.name;
                // Every button that triggers a redraw carries this: the redraw
                // rebuilds the tree, and the keyboard is put back by key alone.
                b.setAttribute('data-focus', 'settings-tab:' + tab.key);
                // The state tab carries the same pip the button outside does:
                // a reader should not have to open a tab to learn it is the
                // one with the trouble in it.
                if (tab.key === 'state' && statusProblem) {
                    b.appendChild(el('span', 'pip bad tab-pip'));
                }
                b.addEventListener('click', function (ev) {
                    ev.stopPropagation();
                    settingsTab = tab.key;
                    rerender();
                });
                b.addEventListener('keydown', function (ev) {
                    var at = SETTINGS_TABS.indexOf(tab), next = at;
                    if (ev.key === 'ArrowRight') next = (at + 1) % SETTINGS_TABS.length;
                    else if (ev.key === 'ArrowLeft') next = (at + SETTINGS_TABS.length - 1) % SETTINGS_TABS.length;
                    else if (ev.key === 'Home') next = 0;
                    else if (ev.key === 'End') next = SETTINGS_TABS.length - 1;
                    else return;
                    ev.preventDefault();
                    settingsTab = SETTINGS_TABS[next].key;
                    rerender();
                    restoreFocus('settings-tab:' + settingsTab);
                });
                tabs.appendChild(b);
            });
            panel.appendChild(tabs);

            var body = el('div', 'settings-body');
            if (settingsTab === 'detail') {
                body.appendChild(el('div', 'settings-note',
                    'How much of each row in the account list is unfolded.'));
                var opts = el('div', 'settings-opts');
                DENSITY_OPTIONS.forEach(function (opt) {
                    var on = opt.key === density;
                    var b = el('button', 'density-opt' + (on ? ' active' : ''));
                    // Picture and the chosen mark share the top line, the words
                    // have the rest of the tile to themselves.
                    var top = el('span', 'density-top');
                    top.appendChild(densityPreview(opt.key));
                    top.appendChild(el('span', 'density-mark' + (on ? ' on' : '')));
                    b.appendChild(top);
                    var inner = el('span', 'density-body');
                    inner.appendChild(el('span', 'density-name', opt.name));
                    inner.appendChild(el('span', 'density-note', opt.note));
                    b.appendChild(inner);
                    b.setAttribute('aria-label', opt.name + ' — ' + opt.note + (on ? ' · chosen' : ''));
                    b.setAttribute('data-focus', 'density:' + opt.key);
                    b.addEventListener('click', function (ev) {
                        ev.stopPropagation();
                        density = opt.key;
                        savePrefs();
                        rerender();
                    });
                    opts.appendChild(b);
                });
                body.appendChild(opts);
            } else if (settingsTab === 'models') {
                body.appendChild(el('div', 'settings-note',
                    'Which windows a row in the account list shows. The card of '
                    + 'the account you open is not affected.'));
                if (!groups.length) {
                    body.appendChild(el('div', 'settings-note', 'No agent family has been read yet.'));
                }
                groups.forEach(function (group) {
                    var chosen = modelView(group.harness_id);
                    var row = el('div', 'models-row');
                    var head = el('span', 'models-family');
                    head.appendChild(familyMark(group));
                    head.appendChild(el('span', null, familyName(group)));
                    row.appendChild(head);

                    var modelName = familyModelName(group);
                    var seg = el('div', 'models-seg');
                    seg.setAttribute('role', 'group');
                    seg.setAttribute('aria-label', familyName(group) + ' — windows shown in the list');
                    MODEL_VIEWS.forEach(function (key) {
                        var on = key === chosen;
                        var words = modelViewWords(key, modelName);
                        var b = el('button', 'models-opt' + (on ? ' active' : ''));
                        b.appendChild(document.createTextNode(words.plain));
                        // The model's own name carries the same red it wears as
                        // a chip beside a window: one name, one colour, wherever
                        // it turns up.
                        if (words.name) b.appendChild(el('span', 'models-name', words.name));
                        b.setAttribute('aria-pressed', on ? 'true' : 'false');
                        b.setAttribute('aria-label', words.spoken);
                        b.title = words.spoken;
                        b.setAttribute('data-focus', 'models:' + group.harness_id + ':' + key);
                        b.addEventListener('click', function (ev) {
                            ev.stopPropagation();
                            modelChoices[group.harness_id] = key;
                            savePrefs();
                            rerender();
                        });
                        seg.appendChild(b);
                    });
                    row.appendChild(seg);
                    body.appendChild(row);
                    // Only Claude arrives with its windows tied to models. Saying
                    // so beats hiding the row: silence would read as "we forgot
                    // this family", and guessing a model out of a window's name
                    // is a claim the engine never made.
                    if (!familyHasModelWindows(group)) {
                        body.appendChild(el('div', 'models-note',
                            'No window here is tied to a named model, so this changes nothing yet.'));
                    }
                });
            } else if (settingsTab === 'accounts') {
                body.appendChild(el('div', 'settings-note',
                    'Accounts that cannot run anything gather by reason at the '
                    + 'bottom of the account list. Off puts them back in place.'));
                // The switches still keep the choice, but the list ignores them
                // until the accounts are read again. Said here, the way the
                // Models tab says when its choice changes nothing yet.
                if (foldPaused(facets)) {
                    body.appendChild(el('div', 'settings-note',
                        'Accounts were not read on the last answer, so this changes '
                        + 'nothing yet: nothing folds until they are.'));
                }
                FOLD_REASONS.forEach(function (reason) {
                    var on = foldChoices[reason] !== false;
                    var row = el('div', 'fold-row');
                    var words = el('span', 'fold-words');
                    words.appendChild(el('span', 'fold-name', FOLD_TITLE[reason]));
                    words.appendChild(el('span', 'fold-note', FOLD_NOTE[reason]));
                    row.appendChild(words);

                    // A switch, not a pill pair: this is one thing that is on
                    // or off, and the panel's other choices are one-of-three.
                    var sw = el('button', 'switch' + (on ? ' on' : ''));
                    sw.setAttribute('role', 'switch');
                    sw.setAttribute('aria-checked', on ? 'true' : 'false');
                    sw.setAttribute('aria-label', FOLD_TITLE[reason] + ' — '
                        + FOLD_NOTE[reason]
                        + (on ? ' · folded away' : ' · shown in the list'));
                    sw.title = FOLD_NOTE[reason];
                    sw.setAttribute('data-focus', 'fold-pref:' + reason);
                    sw.appendChild(el('span', 'switch-knob'));
                    sw.addEventListener('click', function (ev) {
                        ev.stopPropagation();
                        foldChoices[reason] = !on;
                        savePrefs();
                        rerender();
                    });
                    row.appendChild(sw);
                    body.appendChild(row);
                });
            } else {
                // Word for word from the strip this replaced: same wording,
                // same tones, same order of facets.
                body.appendChild(el('div', 'settings-note', view.kept
                    ? 'Nothing was read on the latest attempt: nothing below is current.'
                    : 'What the daemon answered on this read.'));
                var stateRow = el('div', 'settings-state');
                var lastRead = relTime(daemon.read_at) || clockAt(daemon.read_at);
                var dRow = daemon.state
                    ? dotLabel('daemon ' + daemon.state
                        + (daemon.engine_version ? ' · ' + daemon.engine_version : ''),
                        daemonDown ? 'bad' : 'ok')
                    : (daemon.last_state
                        ? dotLabel('daemon ' + daemon.last_state + ' when last read'
                            + (lastRead ? ' (' + lastRead + ')' : ''), 'muted')
                        : dotLabel('daemon not reported', 'muted'));
                dRow.className = 'dot-label strong';
                stateRow.appendChild(dRow);
                // "next up" is a fact about routing, and on a wire that
                // carries no verdict the widget shows no marker anywhere. That
                // looks exactly like "nobody is next", which is a claim it was
                // never told — so the absence is named here instead.
                var mute = groups.filter(function (g) { return g.routing_read === false; });
                if (mute.length) {
                    stateRow.appendChild(dotLabel('rotation not reported for '
                        + mute.map(familyName).join(', '), 'warn'));
                }
                FACET_ORDER.forEach(function (f) {
                    var state = facets[f] || 'indeterminate';
                    // A green dot already says "read". The word is spent only
                    // on the states where something actually went wrong.
                    stateRow.appendChild(dotLabel(state === 'ok' ? f : f + ' ' + facetWord(state), facetTone(state)));
                });
                body.appendChild(stateRow);
            }
            panel.appendChild(body);
            root.appendChild(panel);
        } else if (selection.account) {
            // The account the selector above has chosen, below the family's
            // overview: named as such, so the two are not read as one thing,
            // and folded to its brief while the overview stands above it.
            var overview = !!(hasAnswer && view.reserve && view.reserve.summary);
            var accountArea = el('section', 'account-area');
            accountArea.setAttribute('aria-label', 'Selected account');
            var accountToolbar = el('div', 'account-toolbar');
            accountToolbar.appendChild(el('span', 'account-label', 'Account'));
            renderAccountSelect(accountToolbar, selection.group, selection.account, hasAnswer, facets);
            if (overview) accountToolbar.appendChild(accountToggle(accountOpen));
            accountArea.appendChild(accountToolbar);
            renderAccount(accountArea, selection.group, selection.account, facets, overview);
            if (overview && !accountOpen) accountLimits(accountArea, view.reserve, selection.group, selection.account);
            root.appendChild(accountArea);
        } else if (!hasAnswer) {
            // Nothing has been read yet. An empty verdict here would be a claim
            // about data that has not arrived.
            emptyCard('refresh', 'Reading accounts…',
                'Asking the Claudexor daemon for catalog, accounts and quota.');
        } else if (!selection.group) {
            // No family came back at all. That is not "you have no accounts" —
            // it is "we were told nothing", and the two must not share a card.
            emptyCard('warn', 'No agent family reported', view.transport_error
                ? 'The status endpoint did not answer, so nothing is claimed about accounts.'
                : 'The answer carried no agent family. Nothing is claimed about accounts.');
        } else {
            // A family the host knows but has never been logged into is not an
            // error and not an unread one: it is a fact, said as one — with
            // whatever the harness itself has to say about why.
            var card = emptyCard('info', 'No accounts in ' + familyName(selection.group),
                'The catalog lists this agent family, but no account is set up for it yet.');
            var notes = el('div', 'empty-notes');
            appendHarnessNotes(notes, selection.group, facets);
            if (notes.childNodes.length) card.appendChild(notes);
        }

        if (focusAccountBtn) {
            focusAccountBtn = false;
            restoreFocus('account-btn');
        } else if (focusWas) {
            restoreFocus(focusWas);
        }
        restoreScroll(scrollWas);
        askForChart();
    }

    // `reuse` is a chart or family switch: the skill may answer from a status
    // read it made moments ago. The timed poll never passes it.
    //
    // Every way a read can end releases inFlight exactly once, for the read
    // that set it: an answer, an HTTP error, a bridge error, an abort, a body
    // that is not JSON or not an object, no answer within GET_TIMEOUT_MS, and
    // an answer that cannot be drawn. An answer that arrives after the frame
    // was stopped or disposed changes nothing.
    function load(reuse) {
        if (stopped || inFlight) return;
        inFlight = true;
        if (!reuse) chartAsked = '';
        rerender();

        var mine = ++generation;
        // A reading asked for before the reader's latest choice may carry the
        // choice before it; its display choices are then not drawn.
        var prefsAsked = prefsSeq;
        var url = reserveUrl(!!reuse);
        request(url, { method: 'GET' }, GET_TIMEOUT_MS).then(function (result) {
            if (mine !== generation || stopped) return;
            inFlight = false;
            var view = null;
            if (result.ok) {
                try { view = JSON.parse(result.body); } catch (err) { view = null; }
            }
            if (!view || typeof view !== 'object' || Array.isArray(view)) {
                var why = result.timedOut ? 'no answer within ' + Math.round(GET_TIMEOUT_MS / 1000) + ' s'
                    : (result.error ? 'bridge error: ' + result.error
                        : (result.ok ? 'unreadable response body' : 'HTTP ' + result.status));
                if (lastGood) {
                    safeRender(keptView(lastGood), why);
                } else {
                    safeRender({ facets: {}, groups: [], daemon: {}, transport_error: 'widget route ' + why }, '');
                }
                return;
            }
            // An answer that reports itself as not ok normally carries its own
            // reason. If it does not, say so rather than draw it as fresh.
            var unexplained = !view.ok && !view.transport_error && !view.facet_note;
            if (unexplained && !hasContent(view)) {
                // Nothing in it to draw, and no reason given: not a screen.
                // The newest one drawn stays, with what happened said above
                // it; the reader's family, limit and account stay chosen.
                if (lastGood) {
                    safeRender(keptView(lastGood), 'response reported itself incomplete');
                } else {
                    safeRender({ facets: {}, groups: [], daemon: {},
                                 transport_error: 'widget route: response reported itself incomplete' }, '');
                }
                return;
            }
            actionMessage = '';
            refreshedSinceSummary = false;
            if (prefsAsked === prefsSeq) applyPrefs(view.prefs);
            var drawn = safeRender(view, unexplained ? 'response reported itself incomplete' : '');
            // The newest answer drawn — a degraded one included, with its own
            // dated last-known values and its own transport error — is what
            // this frame falls back to when a later read fails outright. An
            // older whole answer must not come back over it: its bars would
            // look fresher than what was last known.
            if (drawn) { lastGood = view; lastGoodAt = Date.now(); }
        })['catch'](function (err) {
            // Only a fault in the handler above can land here.
            if (mine === generation) inFlight = false;
            failNow(err);
        });
    }

    // A read that failed outright keeps the newest screen drawn, re-read at
    // this moment: a record, and nothing in it is current any more. A current
    // bar becomes the dated last-known value it now is; past its reported
    // reset — or, with none reported, past its window — it is a "?" whose
    // last reading stays on record, never a value. What only current
    // readings support (the current figure, the next reset, pace and the
    // estimate, the no-new-use line, an account held back now) is withdrawn,
    // and each account's windows become last-known readings. The rest of the
    // answer is read the same way, as the skill words a status it could not
    // read: no facet was read now, the daemon's word is its last one, an
    // account's check is last known and none is next up. Re-derived from
    // the answer itself at every failed read, so the cut follows the clock;
    // nothing is invented and nothing on record is dropped. Every screen this
    // frame falls back to — a failed read, an answer it cannot draw — is this.
    function keptView(view) {
        if (!view || typeof view !== 'object') return view;
        var daemon = view.daemon || {};
        // Before the first answer there is nothing to keep, and a read facet
        // here would turn "Reading accounts…" into a claim about accounts.
        if (!daemon.state && !daemon.last_state && !(view.groups || []).length && !view.reserve
            && !Object.keys(view.facets || {}).length && !view.transport_error) return view;
        var nowMs = Date.now();
        var summary = view.reserve && view.reserve.summary;
        var readAt = (summary && summary.status_read_at)
            || (lastGoodAt ? new Date(lastGoodAt).toISOString() : '');
        var cached = Object.assign({}, (summary && summary.cached) || {}, view.cached || {});
        FACET_ORDER.forEach(function (f) { if (!cached[f] && readAt) cached[f] = readAt; });
        var facets = {};
        FACET_ORDER.forEach(function (f) { facets[f] = 'indeterminate'; });
        var out = Object.assign({}, view, {
            ok: false, complete: false, kept: true, cached: cached,
            // The banner of the kept screen says it; a note per facet would
            // only say it again.
            facets: facets, facet_note: '',
            daemon: { state: '', last_state: daemon.state || daemon.last_state || '',
                      engine_version: daemon.engine_version || '', read_at: daemon.read_at || readAt }
        });
        if (view.reserve && typeof view.reserve === 'object') {
            var reserve = Object.assign({}, view.reserve);
            if (summary && typeof summary === 'object') {
                reserve.summary = Object.assign({}, summary, {
                    cached: cached,
                    roster: summary.roster === 'unknown' ? 'unknown' : 'cached',
                    groups: (summary.groups || []).map(function (g) { return keptGroup(g, nowMs); })
                });
            }
            if (reserve.chart) reserve.chart = keptChart(reserve.chart);
            out.reserve = reserve;
        }
        out.groups = (view.groups || []).map(function (group) {
            if (!group || !Array.isArray(group.accounts)) return group;
            // Who is next up is routing read now: not stated on a kept screen.
            return Object.assign({}, group, { routing_read: false,
                accounts: group.accounts.map(function (a) { return keptAccount(a, nowMs); }) });
        });
        return out;
    }

    // An account of a kept screen, as the skill words one whose list was not
    // read now (verification_view): its check last known, never live.
    function keptAccount(a, nowMs) {
        if (!a || typeof a !== 'object') return a;
        var label = String((a.verification || {}).label || 'Not verified');
        return Object.assign({}, a, {
            verification: { tone: 'muted', label: /— last known$/.test(label) ? label : label + ' — last known' },
            verified_live: false,
            next_up: false,
            quota: a.quota ? keptQuota(a.quota, nowMs) : a.quota
        });
    }

    // One bar of a kept screen, by the skill's own carry rule (carry_verdict).
    function keptBar(b, windowSeconds, nowMs) {
        if (!b || b.state === 'unknown') return b;
        var left = typeof b.left === 'number' && isFinite(b.left) ? b.left : null;
        var observed = Date.parse(String(b.observed_at || ''));
        var reset = Date.parse(String(b.resets_at || ''));
        var why = '';
        if (b.resets_at && isFinite(reset)) {
            if (reset <= nowMs) why = 'reset_passed';
        } else if (!isFinite(observed)
                   || nowMs - observed > (windowSeconds > 0 ? windowSeconds : 86400) * 1000) {
            why = 'too_old';
        }
        var origin = b.state === 'current' ? 'screen' : b.origin;
        // Whether an account is held back now is not known on a kept screen.
        var base = { account: b.account, flags: ['account_state_unknown'], restricted: false,
                     observed_at: b.observed_at };
        if (why || left === null) {
            return Object.assign(base, { state: 'unknown', left: null, at_limit: false, why: why || 'not_read',
                last_reading: left === null ? null : { left: left, observed_at: b.observed_at,
                                                       resets_at: b.resets_at || null, origin: origin } });
        }
        return Object.assign(base, { state: 'last_known', left: left, at_limit: !!b.at_limit,
            resets_at: b.resets_at || null, origin: origin,
            age_seconds: isFinite(observed) ? Math.max(0, Math.round((nowMs - observed) / 1000)) : null });
    }

    function keptGroup(g, nowMs) {
        if (!g || typeof g !== 'object') return g;
        var known = [];
        var unknown = [];
        barsOf(g).forEach(function (b) {
            var kept = keptBar(b, g.window_seconds, nowMs);
            if (kept) (kept.state === 'unknown' ? unknown : known).push(kept);
        });
        unknown.sort(function (a, b) {
            var x = String(a.account || ''), y = String(b.account || '');
            return x < y ? -1 : (x > y ? 1 : 0);
        });
        var windows = 0, oldest = '', newest = '', origins = {}, reasons = {};
        known.forEach(function (b) {
            windows += b.left;
            if (b.observed_at && (!oldest || b.observed_at < oldest)) oldest = b.observed_at;
            if (b.observed_at && (!newest || b.observed_at > newest)) newest = b.observed_at;
            origins[b.origin] = (origins[b.origin] || 0) + 1;
        });
        unknown.forEach(function (b) { reasons[b.why] = (reasons[b.why] || 0) + 1; });
        windows = Math.round(windows * 10000) / 10000;
        var coverage = g.coverage || {};
        return Object.assign({}, g, {
            bars: known.concat(unknown),
            measured: { accounts: 0, windows: 0, average_remaining_pct: null, at_limit: 0 },
            shares: [],
            // Counts of the answer's own moment (stale, unreadable, …) are
            // re-derived above as last known and unknown; only who the limit
            // may not apply to stays. Plans split current readings: withdrawn.
            coverage: { measured: 0, other_family_accounts: coverage.other_family_accounts || 0 },
            plans: null,
            unrestricted_windows: 0,
            restrictions: {},
            observed: { newest_at: null, oldest_at: null, stale_newest_at: newest || null },
            next_reset: null,
            reset_unknown: 0,
            pace_to_reset: null,
            recent_pace: { state: 'no_measured_accounts', accounts_known: 0, of: 0, not_known: {} },
            last_known: { accounts: known.length, windows: windows, oldest_observed_at: oldest || null,
                          newest_observed_at: newest || null, origins: origins },
            unknown: { accounts: unknown.length, reasons: reasons },
            with_last_known: { windows: windows, accounts: known.length }
        });
    }

    // The chart of a kept screen: the observed record stays; nothing is
    // drawn at or after its "now" — no value now, no line ahead, no reset —
    // and that moment is labelled by its clock time, not as now.
    function keptChart(chart) {
        if (!chart || typeof chart !== 'object') return chart;
        var past = (chart.past || []).slice();
        if (past.length) past[past.length - 1] = [past[past.length - 1][0], null];
        var scope = chart.recent_pace_scope || {};
        return Object.assign({}, chart, {
            kept: true,
            past: past, accounts: 0, past_current_accounts: 0,
            past_basis: chart.past_accounts ? 'last_known' : 'none',
            no_new_use: null, recent_pace: null, recent_pace_refill_scenario: null, cohort_past: null,
            recent_pace_note: '', resets: [],
            recent_pace_scope: { accounts: 0, of: 0, slots: scope.slots, until: null,
                                 stops_at_reset: false, excluded: {} },
            table: (chart.table || []).filter(function (row) {
                return row && row.observed !== undefined && row.event !== 'now';
            })
        });
    }

    // An account's card on a kept screen: its windows are last-known
    // readings; a cooldown whose end has passed no longer holds, and a model
    // exhaustion whose reset has passed is disclosed as passed.
    function keptQuota(q, nowMs) {
        if (!q || typeof q !== 'object') return q;
        var ahead = function (iso) {
            var t = Date.parse(String(iso || ''));
            return !iso || !isFinite(t) || t > nowMs;
        };
        var current = Array.isArray(q.constraints) ? q.constraints : [];
        var stale = (Array.isArray(q.stale) ? q.stale : []).slice();
        if (current.length) {
            stale.unshift({ observed_at: q.observed_at || '', freshness: 'stale', source: '', constraints: current });
        }
        var out = Object.assign({}, q, {
            constraints: [],
            stale: stale,
            resets_at: '',
            cooldowns: (q.cooldowns || []).filter(function (c) { return c && ahead(c.until); })
                .map(function (c) { return Object.assign({}, c, { freshness: 'stale' }); }),
            model_exhaustions: (q.model_exhaustions || []).filter(Boolean).map(function (e) {
                var passed = e.live && e.resets_at && !ahead(e.resets_at);
                return Object.assign({}, e, { freshness: 'stale' }, passed ? { live: false, reset_note: 'passed' } : {});
            }),
            cooling_until: q.cooling_until && ahead(q.cooling_until) ? q.cooling_until : ''
        });
        if (stale.length && q.state !== 'not_checked') {
            out.state = 'no_fresh_window';
            out.label = 'No fresh reading — last reading is stale';
            out.note = 'Stale percentages do not grant routing; live cooldown evidence may still deny or rank.';
        } else if (q.state === 'cooling' && !cooldownsOf(out, 'account').length) {
            // Held by a cooldown alone, and its end has passed: not cooling
            // now, and nothing read since says what it is.
            out.state = 'no_fresh_window';
            out.label = 'No fresh reading — the cooldown last reported has ended';
            out.note = '';
        }
        return out;
    }

    // Whether an answer carries anything a screen can be drawn from: the
    // account list or the reserve overview.
    function hasContent(view) {
        var summary = view && view.reserve && view.reserve.summary;
        return (Array.isArray(view.groups) && view.groups.length > 0)
            || !!(summary && Array.isArray(summary.groups) && summary.groups.length > 0);
    }

    function failNow(err) {
        try { console.error('claudexor quotas: reading not handled', err); } catch (e) { /* no console */ }
        actionMessage = 'The widget could not handle the last reading ('
            + String((err && err.message) || err) + '). Retry, or wait for the next reading.';
        rerender();
    }

    // The owner's foreground refresh. A POST that gets no answer in time may
    // still have reached the host: its outcome is unknown, it is said so, and
    // it is never sent again on its own. The next ordinary reading shows
    // whatever it changed.
    function refreshQuota() {
        if (stopped || inFlight) return;
        inFlight = true;
        actionMessage = '';
        rerender();

        var mine = ++generation;
        request(REFRESH_ROUTE, { method: 'POST' }, REFRESH_TIMEOUT_MS).then(function (result) {
            if (mine !== generation || stopped) return;
            inFlight = false;
            if (result.timedOut) {
                actionMessage = 'Live refresh got no answer within ' + Math.round(REFRESH_TIMEOUT_MS / 1000)
                    + ' s, so whether it ran is unknown. It is not sent again; cached values remain, and '
                    + 'the next reading shows any change.';
                rerender();
                return;
            }
            var response = null;
            try { response = JSON.parse(result.body); } catch (err) { response = null; }
            // Only the route's own answer — an object that says ok true or
            // false — can say what happened.
            var readable = !!response && typeof response === 'object' && typeof response.ok === 'boolean';
            // The skill read no answer from the host (its own bound, a
            // connection closed after sending), or this answer — a success
            // status whose body broke off, could not be parsed or is not the
            // route's — cannot be read: the refresh may have run. Unknown,
            // never "failed".
            if ((readable && response.outcome_unknown === true) || (result.ok && !readable)
                    || result.bodyLost || result.status === 0) {
                actionMessage = 'Live refresh got no readable answer, so whether it ran is unknown. It is not '
                    + 'sent again; cached values remain, and the next reading shows any change.';
                rerender();
                return;
            }
            if (!result.ok || !response || typeof response !== 'object' || !response.ok) {
                var compatible = response && response.compatibility_error;
                actionMessage = compatible
                    ? 'Live refresh requires a newer Ouroboros host.'
                    : 'Live quota refresh failed. Cached quota data remains visible.';
                rerender();
                return;
            }
            var merged = mergeQuotaFacet(currentView || lastGood || {}, response);
            staleMessage = '';
            actionMessage = '';
            refreshedSinceSummary = !!merged.reserve;
            if (safeRender(merged, '', '')) {
                lastGood = merged;
                lastGoodAt = Date.now();
            }
        })['catch'](function (err) {
            if (mine === generation) inFlight = false;
            failNow(err);
        });
    }

    // Three empty states differ by icon and words only — the same three lines
    // of scaffolding were written out for each, the way the banners were before
    // they got a helper of their own.
    function emptyCard(iconName, title, desc) {
        var card = el('div', 'empty-card');
        card.appendChild(withIcon(el('div', 'empty-icon'), iconName, 26));
        card.appendChild(el('h4', 'empty-title', title));
        card.appendChild(el('p', 'empty-desc', desc));
        root.appendChild(card);
        return card;
    }

    // Four banners were four copies of the same three lines. They also have to
    // announce themselves: one can appear on its own, thirty seconds after the
    // last time anybody looked at the widget.
    function banner(iconName, text, bad) {
        var node = el('div', 'banner' + (bad ? ' bad' : ''));
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), iconName));
        node.appendChild(el('span', null, text));
        root.appendChild(node);
        return node;
    }

    // Keys are compared by hand rather than through a selector: an account key
    // is engine-shaped ("codex:codex-default") and would need escaping.
    function restoreFocus(key) {
        if (!key) return;
        var nodes = root.querySelectorAll('[data-focus]');
        var i;
        var held = false;
        for (i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-focus') !== key) continue;
            if (!nodes[i].disabled) {
                nodes[i].focus();
                return;
            }
            held = true;
        }
        // There, but disabled for now: kept for the redraw that enables it.
        if (held) {
            focusParked = key;
            return;
        }
        // The row the keyboard was on is gone — the list closed under it, or
        // that account did. Dropping to the body would strand whoever is on
        // the keyboard; the button the list belongs to is the nearest home.
        if (key.indexOf('opt:') === 0) restoreFocus('account-btn');
    }

    // Focus is nowhere the reader put it: on nothing, on the page itself, or on
    // a node a redraw has since removed. A frame the reader has left is not
    // pulled back into.
    function focusLost(node) {
        if (typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
        return !node || node === document.body || node === document.documentElement
            || node.isConnected === false;
    }

    // Two things scroll: the page itself and the open account list. Which node
    // carries the page scroll depends on the browser — the body here, the
    // document element elsewhere — so both are asked, and writing to the one
    // that does not scroll costs nothing.
    function pageNodes() {
        var out = [];
        if (document.body) out.push(document.body);
        var el = document.scrollingElement || document.documentElement;
        if (el && el !== document.body) out.push(el);
        return out;
    }

    function readScroll() {
        var page = 0;
        pageNodes().forEach(function (node) {
            if (typeof node.scrollTop === 'number' && node.scrollTop > page) page = node.scrollTop;
        });
        return {
            page: page,
            pop: accountPop ? (accountPop.scrollTop || 0) : 0
        };
    }

    // A shorter list after a refresh clamps the value on its own, which is the
    // right answer: the row that was under the cursor is simply not there any
    // more, and the list stops at its own end rather than pretending otherwise.
    function restoreScroll(saved) {
        if (!saved) return;
        if (saved.page) {
            pageNodes().forEach(function (node) { node.scrollTop = saved.page; });
        }
        if (saved.pop && accountPop) accountPop.scrollTop = saved.pop;
    }

    function rerender() {
        if (stopped) return;
        safeRender(currentView || { facets: {}, groups: [], daemon: {} }, staleMessage, actionMessage);
    }

    // What opens inside the row has to be dismissible the way every popover is:
    // a click elsewhere or Escape. Both listeners are named rather than inline
    // so that stop() can take them off the document again.
    function onDocumentClick(e) {
        var t = e.target;
        var inside = t && typeof t.closest === 'function' ? t.closest('.settings-panel, .acct-wrap') : null;
        if (inside) return;
        var changed = false;
        if (settingsOpen) { settingsOpen = false; changed = true; }
        if (accountsOpen) { accountsOpen = false; changed = true; }
        if (changed) rerender();
    }

    // A redraw under a resting pointer keeps the chart's cursor; once the
    // pointer moves anywhere but the chart, the cursor goes with it.
    function onDocumentPointer(e) {
        if (!chartCursor || chartCursor.via !== 'pointer') return;
        var t = e.target;
        var inside = t && typeof t.closest === 'function' ? t.closest('.chart-plot') : null;
        if (!inside) {
            chartCursor = null;
            var nodes = root.querySelectorAll ? root.querySelectorAll('.chart-tip') : [];
            for (var i = 0; i < nodes.length; i++) nodes[i].style.display = 'none';
            var lines = root.querySelectorAll ? root.querySelectorAll('.cursor-line,.cursor-dot') : [];
            for (var j = 0; j < lines.length; j++) lines[j].setAttribute('visibility', 'hidden');
        }
    }

    function onDocumentKey(e) {
        if (e.key !== 'Escape') return;
        if (settingsOpen) {
            settingsOpen = false;
            rerender();
            restoreFocus('settings');
            return;
        }
        // Only one of the two is ever open — each opening closes the other —
        // so this order is a safety rail, not a stack.
        if (accountsOpen) {
            accountsOpen = false;
            focusAccountBtn = true;
            rerender();
        }
    }

    function stop() {
        stopped = true;
        if (themeOff) { themeOff(); themeOff = null; }
        generation++;
        if (dataTimer !== null) { window.clearInterval(dataTimer); dataTimer = null; }
        document.removeEventListener('click', onDocumentClick);
        document.removeEventListener('keydown', onDocumentKey);
        document.removeEventListener('pointermove', onDocumentPointer);
    }

    // Both the first run and the return from the back/forward cache need the
    // same three things started; describing them twice is how they drift.
    // The sheet is static, so it is written once and lives outside the tree
    // render() clears. Rebuilding it on every redraw re-applied 18 KB of CSS
    // twice a cycle — and made the progress bars' transition impossible, since
    // a transition needs the previous computed width and every fill was new.
    function installStyle() {
        // Marked, because the question is "is MY sheet installed", not "is
        // there any sheet at all": the frame is generated by the host and may
        // carry a bootstrap style of its own, and asking the loose question
        // would leave this widget unstyled.
        if (document.getElementById(STYLE_ID)) return;
        var style = el('style');
        style.id = STYLE_ID;
        style.textContent = STYLE;
        document.head.appendChild(style);
    }

    function start() {
        installStyle();
        // The host resolves Light/Dark/System; older hosts keep the original dark palette.
        if (!themeOff && window.OuroborosWidget && typeof window.OuroborosWidget.onTheme === 'function') {
            themeOff = window.OuroborosWidget.onTheme(function (theme) {
                document.documentElement.dataset.theme = theme;
            });
        }
        document.addEventListener('click', onDocumentClick);
        document.addEventListener('keydown', onDocumentKey);
        document.addEventListener('pointermove', onDocumentPointer);
        if (dataTimer === null) {
            dataTimer = window.setInterval(function () {
                if (document.visibilityState === 'visible') load();
            }, REFRESH_MS);
        }
        load();
    }

    window.addEventListener('pagehide', stop);
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () {
            disposed = true;
            stop();
            return flushPrefs();
        });
    }
    // Restored from the back/forward cache the widget is otherwise dead for
    // good: the timer is gone and every answer is dropped by the stopped flag,
    // while Refresh keeps spinning as if it worked.
    window.addEventListener('pageshow', function () {
        if (disposed || !stopped) return;
        stopped = false;
        inFlight = false;
        start();
    });
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !stopped) {
            load();
        }
    });

    start();
})();
