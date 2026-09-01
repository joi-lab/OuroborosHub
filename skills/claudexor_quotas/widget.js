/* Claudexor Quotas widget — v0.3.1
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
    var FACET_ORDER = ['catalog', 'accounts', 'quota'];
    var TONE_WORD = { ok: 'ready', warn: 'heavy use', bad: 'alert', muted: 'no live reading' };

    var root = document.getElementById('root');
    var generation = 0;
    var dataTimer = null;
    var lastGood = null;
    var lastGoodAt = 0;
    var stopped = false;
    var inFlight = false;
    var statusOpen = false;
    // The screen shows one account at a time: which family, and which account
    // inside it. Both survive the 30-second redraw, and both fall back on their
    // own when what they point at stops existing.
    var selectedHarness = '';
    var selectedAccountKey = '';
    var accountsOpen = false;
    var focusAccountBtn = false;
    var currentView = null;
    var staleMessage = '';
    var actionMessage = '';

    var STYLE_ID = 'claudexor-quotas-style';

    var STYLE = [
        ':root{',
        'color-scheme:dark;',
        '--bg-canvas:#0d0b0f;',
        '--bg-surface-inset:rgba(0, 0, 0, 0.30);',
        /* Glass is exactly three things: a light fill, a blurred backdrop and a
           hairline edge that is brighter along the top. No shadow, no glow. */
        '--glass-fill:rgba(255, 255, 255, 0.05);',
        '--glass-blur:saturate(150%) blur(18px);',
        '--glass-edge:rgba(255, 255, 255, 0.10);',
        '--glass-edge-top:rgba(255, 255, 255, 0.16);',
        '--accent-core:#c93545;',
        /* A flat accent fill reads as a sticker; the vertical gradient plus a
           light top edge is what makes the button feel like glass. */
        '--accent-grad:linear-gradient(180deg, #d84152 0%, #b62c3c 100%);',
        '--accent-grad-hover:linear-gradient(180deg, #e04b5c 0%, #c53544 100%);',
        '--accent-edge:rgba(255, 255, 255, 0.14);',
        '--border-prominent:rgba(255, 255, 255, 0.20);',
        '--text-primary:#e2e8f0;',
        '--text-secondary:rgba(255, 255, 255, 0.68);',
        '--text-muted:rgba(255, 255, 255, 0.54);',
        '--status-ok:#22c55e;',
        '--grad-ok:linear-gradient(90deg, #16a34a 0%, #4ade80 100%);',
        '--status-warn:#f59e0b;',
        '--status-stale:#d6a54a;',
        '--status-warn-bg:rgba(245, 158, 11, 0.12);',
        '--status-warn-border:rgba(245, 158, 11, 0.35);',
        '--grad-warn:linear-gradient(90deg, #d97706 0%, #fbbf24 100%);',
        '--status-bad:#f07a86;',
        '--status-bad-bg:rgba(201, 53, 69, 0.18);',
        '--status-bad-border:rgba(201, 53, 69, 0.55);',
        '--grad-bad:linear-gradient(90deg, #8f1f2c 0%, #e04b5c 100%);',
        /* Every element of the control row is exactly this tall, so the row
           does not jump when the status strip opens in place of the rest. */
        '--row-h:32px;',
        '--radius-sm:6px;',
        '--radius-md:10px;',
        '--radius-lg:14px;',
        '--radius-pill:9999px;',
        '--font-mono:ui-monospace, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", monospace;',
        '--transition-fast:0.15s ease;',
        '}',
        'body{margin:0;font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
        /* The frame opens at 320px and grows to fit #root when the module asks.
           min-height keeps the usual case exactly one screen — the ground and
           the pinned row need a full height to sit on — while letting an
           account with more windows than fit push the frame taller instead of
           hiding the rest behind a scrollbar the frame does not draw. */
        'color:var(--text-primary);min-height:100vh;box-sizing:border-box;',
        /* The frame paints its own ground: glass has nothing to blur over a
           transparent body, and two faint pools give it something to work with. */
        'background:radial-gradient(900px 420px at 12% -8%, rgba(201, 53, 69, 0.16), transparent 60%),',
        'radial-gradient(700px 380px at 92% 4%, rgba(120, 60, 160, 0.12), transparent 62%), var(--bg-canvas);',
        'background-attachment:fixed;',
        'overflow-y:auto;padding:0 12px 12px;-webkit-font-smoothing:antialiased}',
        '*{box-sizing:border-box}',
        '@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}',

        /* Round buttons close the row on the right: status and Refresh. Status
           opens sideways, in place of the family segment and the account
           selector; only the account list is allowed to lie over the card. */
        /* Glass is one recipe, written once: blur behind, hairline edge, and a
           brighter top edge on the surfaces that are meant to look lifted. */
        '.round-btn,.status-strip,.harness-seg,.acct-btn,.acct-pop,.empty-card{',
        'backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);',
        'border:1px solid var(--glass-edge)}',
        '.acct-btn,.acct-pop,.empty-card{border-top-color:var(--glass-edge-top)}',
        '.round-btn{width:var(--row-h);height:var(--row-h);padding:0;border-radius:50%;background:var(--bg-surface-inset);',
        'color:var(--text-muted);cursor:pointer;display:inline-flex;',
        'align-items:center;justify-content:center;flex:none;transition:all var(--transition-fast)}',
        '.round-btn:hover{color:var(--text-primary);background:var(--glass-fill)}',
        '.round-btn.is-open{background:var(--glass-fill);border-color:var(--glass-edge-top);color:var(--text-primary)}',
        '.round-btn.has-problem{background:var(--status-bad-bg);border-color:var(--status-bad-border);color:var(--status-bad)}',
        '.status-wrap{position:relative;display:inline-flex;flex:none}',
        /* The pip is why folding these away is honest at all: an unread facet
           says so from the outside, without the row being opened. */
        '.pip{position:absolute;width:6px;height:6px;border-radius:50%;',
        /* The ring separates the pip from whatever it sits on. It has to be
           darker than both, not the canvas colour: over the inset ground of the
           segment a canvas-coloured ring lit up as a pale halo. */
        'border:1px solid rgba(0, 0, 0, 0.55);pointer-events:none}',
        '.pip.ok{background:var(--status-ok)}',
        '.pip.bad{background:var(--status-bad)}',
        '.pip.muted{background:rgba(255, 255, 255, 0.30)}',
        '.status-pip{top:-1px;right:-1px}',
        '.status-strip{display:flex;align-items:center;gap:12px;height:var(--row-h);flex:1 1 auto;min-width:0;',
        'padding:0 12px;border-radius:var(--radius-pill);background:var(--bg-surface-inset);',
        'overflow-x:auto;scrollbar-width:none}',
        '.status-strip::-webkit-scrollbar{display:none}',
        '.dot-label{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);white-space:nowrap}',
        '.dot-label.strong{color:var(--text-primary)}',

        /* Refresh is a circle like the status button: the word cost 76px of a
           row this narrow, and the arrow says the same thing. */
        '.round-btn.accent{background:var(--accent-grad);border-color:var(--accent-edge);color:#fff}',
        '.round-btn.accent:hover:not(:disabled){background:var(--accent-grad-hover);color:#fff}',
        '.round-btn:disabled{opacity:0.42;cursor:not-allowed}',
        '.icon-spin{display:inline-flex}',
        '.round-btn.is-refreshing .icon-spin{animation:spin 0.8s linear infinite}',

        /* Control Bar (family segment + account selector) */
        /* The row stays put while the account scrolls under it — but it is not
           a panel: it has no ground of its own. Each control is its own glass
           capsule, and the content passes between them and blurs under each. */
        '.control-bar{display:flex;align-items:center;gap:10px;flex-wrap:nowrap;position:sticky;top:0;z-index:30;',
        'margin:0 -12px 12px;padding:12px 12px 10px;pointer-events:none}',
        '.control-bar > *{pointer-events:auto}',
        /* Family segment: one button per agent family. The mark inside is the
           vendor's own, and the pip on it is why hiding the other families is
           honest at all — trouble anywhere shows without opening anything. */
        '.harness-seg{display:inline-flex;align-items:center;height:var(--row-h);background:var(--bg-surface-inset);',
        'padding:3px;border-radius:var(--radius-pill);flex:none}',
        '.harness-btn{position:relative;width:30px;height:100%;padding:0;border-radius:var(--radius-pill);',
        'border:1px solid transparent;background:transparent;color:var(--text-muted);cursor:pointer;',
        'display:inline-flex;align-items:center;justify-content:center;transition:all var(--transition-fast)}',
        '.harness-btn:hover:not(.active){color:var(--text-primary);background:var(--glass-fill)}',
        '.harness-btn.active{background:var(--glass-fill);border-color:var(--glass-edge-top);color:var(--text-primary)}',
        '.harness-btn.empty{opacity:0.45}',
        '.harness-btn.loading{opacity:0.22;cursor:default}',
        '.harness-initial{font-size:12px;font-weight:600;line-height:1}',
        /* Inside the button, clear of the segment's own outline: at the corner
           the pip crossed the pill's edge and read as a chip out of it. */
        '.harness-pip{top:1px;right:2px}',

        /* Mailbox selector: the count that used to be a caption under the group
           title now rides on the button, and the list carries the state dots. */
        '.acct-wrap{position:relative;flex:1 1 auto;min-width:0;height:var(--row-h)}',
        '.acct-btn{width:100%;height:100%;font:inherit;font-size:12px;display:flex;align-items:center;gap:8px;',
        'padding:0 10px;border-radius:var(--radius-pill);background:var(--glass-fill);',
        'color:var(--text-primary);cursor:pointer;text-align:left;transition:border-color var(--transition-fast)}',
        '.acct-btn:hover:not(:disabled){border-color:var(--border-prominent)}',
        '.acct-btn:disabled{cursor:not-allowed;color:var(--text-muted)}',
        '.acct-name{flex:1 1 auto;min-width:64px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        /* The figure never shrinks: "100%" clipped to "1…" is a different
           number. What gives way is the name, and after it the badge. */
        '.acct-count,.acct-opt-tail{font-size:11px;color:var(--text-muted);flex:none;',
        'font-variant-numeric:tabular-nums;white-space:nowrap}',
        /* The number of accounts calling for a look is the reason to open the
           list at all, so it is the only tinted background on the button. */
        '.acct-alarm{font-size:10.5px;color:var(--status-bad);background:var(--status-bad-bg);',
        'border-radius:var(--radius-pill);padding:1px 8px;white-space:nowrap;',
        'flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}',
        /* One bar shape for the row and the button: the same fills as the big
           bars in the card, so a colour never means two different things. */
        /* Both selectors carry .progress-bar so that the shared rules below —
           written later in this sheet — cannot win on source order. */
        '.progress-bar.spark{width:44px;height:5px;border-color:rgba(255, 255, 255, 0.14);flex:none;',
        'display:inline-block;vertical-align:middle}',
        '.acct-caret{display:flex;flex:none;color:var(--text-muted)}',
        /* Below this the button cannot hold a bar as well as a name and a
           figure. The figure stays: it is the number, the bar only pictures it. */
        '@media (max-width:460px){.acct-btn .spark{display:none}',
        '.control-bar{gap:8px}.acct-name{min-width:52px}}',
        /* The list lies over the account, never above it: what is open must not
           change how much data fits in a frame that cannot grow. */
        '.acct-pop{position:absolute;top:calc(var(--row-h) + 6px);left:0;right:0;z-index:40;padding:4px;',
        'border-radius:var(--radius-md);background:rgba(20, 16, 26, 0.97);',
        /* Two-storey rows: five of them fit before the list starts scrolling
           inside itself rather than pushing the screen. Fewer if a row's
           windows wrap — the number is a floor, not a promise. */
        'max-height:260px;overflow-y:auto}',
        '.acct-opt{width:100%;font:inherit;font-size:12px;display:flex;align-items:flex-start;gap:8px;',
        'padding:6px 8px;border:0;border-radius:var(--radius-sm);background:transparent;',
        'color:var(--text-secondary);cursor:pointer;text-align:left}',
        '.acct-opt .state-dot{margin-top:5px}',
        '.acct-opt-body{flex:1 1 auto;min-width:0}',

        /* The second floor: windows when there are windows, the engine's own
           reason when something is wrong, plan and reading age otherwise. */
        '.acct-line2{font-size:10.5px;color:var(--text-muted);margin-top:2px;',
        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.acct-wins{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px}',
        /* Narrower inside a row than on the button. In the app's own width
           four windows still stand on one line; in a 520px frame they wrap and
           the row grows — data intact, height paid. */
        '.acct-win .progress-bar.spark{width:30px}',
        '.acct-win{display:inline-flex;align-items:center;gap:5px;font-size:10.5px;',
        'color:var(--text-muted);font-variant-numeric:tabular-nums;white-space:nowrap;',
        'max-width:100%;min-width:0}',
        /* A window name can be a whole phrase ("1 reset credit available") and
           in the narrowest frame it is wider than the list itself. It gives way
           rather than pushing the list sideways. */
        '.acct-win-tag{color:var(--text-muted);min-width:0;overflow:hidden;text-overflow:ellipsis}',
        '.acct-win-pct{color:var(--text-secondary)}',
        '.acct-line3{font-size:10px;color:var(--text-muted);margin-top:3px;overflow:hidden;',
        'text-overflow:ellipsis;white-space:nowrap}',
        '.acct-cap{font-size:10px;color:var(--text-secondary);background:rgba(255, 255, 255, 0.07);',
        'border-radius:var(--radius-sm);padding:0 5px}',
        '.acct-cap.exhausted{color:var(--status-bad);background:var(--status-bad-bg)}',
        '.acct-opt:hover{background:var(--glass-fill)}',
        '.acct-opt.active{background:var(--glass-fill);color:var(--text-primary)}',
        /* The name became a span inside a block, and overflow rules do not
           apply to an inline box: without display:block the list grew a
           sideways scrollbar on any long address. */
        '.acct-opt-name{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        /* The button's figure and the row's tail are in different places but
           say the same kinds of things — a share, or a word about trouble — so
           they share one rule and cannot drift apart in weight or colour. */
        '.acct-opt-tail{margin-top:1px}',

        /* Banners */
        '.banner{border-radius:var(--radius-md);padding:10px 14px;margin-bottom:12px;font-size:12px;',
        'border:1px solid var(--status-warn-border);background:var(--status-warn-bg);color:#fde68a;',
        'display:flex;gap:10px;align-items:flex-start}',
        '.banner.bad{border-color:var(--status-bad-border);background:var(--status-bad-bg);color:#fecdd3}',
        '.banner-icon{display:flex;line-height:1;margin-top:1px}',

        /* Status Pills & Chips */
        '.meta{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-muted)}',

        /* The one account on screen: no card, no plate. The control row above
           it is a set of capsules, and a box under them would put the plate
           back that letter Ф just took away. */
        '.acct-family{font-size:14px;font-weight:600;color:var(--text-primary)}',

        '.account-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap}',
        '.account-head-left{min-width:0}',
        '.account-header{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}',
        /* The state of an account is one dot. It sits beside the family name,
           because the account's own name lives in the selector above: a state
           label in a capsule was tried three ways and read as decoration every
           time. The plan chip beside it is not that label — it carries a fact
           the dot cannot, and it is the only chip in this row. */
        '.state-dot{width:5px;height:5px;border-radius:50%;display:inline-block;flex:0 0 auto}',
        '.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;',
        'clip:rect(0 0 0 0);white-space:nowrap;border:0}',
        '.state-dot.ok{background:var(--status-ok)}',
        '.state-dot.warn{background:var(--status-warn)}',
        '.state-dot.bad{background:var(--accent-core)}',
        '.state-dot.muted{background:rgba(255, 255, 255, 0.30)}',
        '.account-title-wrap{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
        '.account-meta{color:var(--text-muted);font-size:11px;margin:2px 0 10px;padding-left:13px;display:flex;align-items:center;',
        'gap:6px;flex-wrap:wrap}',
        '.account-meta span+span:before{content:"·";margin-right:6px;color:var(--text-muted)}',
        /* The plan is what these accounts differ by, so it carries the accent
           — and carries it the way the Refresh button does: a flat fill reads
           as a sticker and a hairline outline dates the whole row, while a
           vertical gradient sits in the same glass language as everything
           else. No border at all. */
        '.plan-chip{font-size:10px;font-weight:600;letter-spacing:0.06em;text-transform:uppercase;',
        'padding:2px 8px;border-radius:var(--radius-sm);border:0;white-space:nowrap;',
        'background:linear-gradient(180deg, rgba(240, 122, 134, 0.24) 0%, rgba(201, 53, 69, 0.16) 100%);',
        'color:var(--status-bad)}',
        '.account-next-up{font-size:11px;font-weight:500;color:var(--text-secondary);letter-spacing:0.02em}',
        '.meta-bad{color:var(--status-bad)}',
        '.meta-muted{color:var(--text-muted)}',

        /* Quotas & Progress Visualization */
        /* Two windows per row: four of them cost two rows instead of twelve
           lines. Exactly two — auto-fit made three at this width, and a model
           name is the first thing that loses when a column narrows. A lone
           window takes the whole row instead of leaving half of it blank, and
           a narrow frame folds the tiles into one column. */
        '.quotas-container{display:grid;grid-template-columns:repeat(2, minmax(0, 1fr));',
        'gap:10px 16px;margin-top:10px}',
        '.quota-tile:only-child{grid-column:1 / -1}',
        '@media (max-width:520px){.quotas-container{grid-template-columns:minmax(0, 1fr)}}',
        '.quota-tile{background:var(--bg-surface-inset);border-radius:var(--radius-md);padding:8px 10px;min-width:0;',
        'border:1px solid var(--glass-edge);border-top-color:var(--glass-edge-top)}',
        '.tile-name{display:flex;align-items:center;gap:6px;min-width:0}',
        '.tile-len{color:var(--text-muted);white-space:nowrap}',
        '.tile-model{font-size:10px;font-weight:500;padding:1px 6px;border-radius:var(--radius-sm);min-width:0;',
        'background:rgba(255, 255, 255, 0.07);color:var(--text-secondary);',
        'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
        '.tile-model.spent{background:var(--status-bad-bg);color:var(--status-bad)}',
        '.tile-pct{white-space:nowrap;font-variant-numeric:tabular-nums}',
        '.tile-foot{display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden}',
        '.tile-role{letter-spacing:0.03em}',
        '.tile-when{display:flex;align-items:center;gap:6px;flex-wrap:wrap;white-space:nowrap}',
        '.tile-when-word{color:var(--status-bad)}',
        '.rel-time{color:var(--text-muted)}',
        /* One means of emphasis, not three: the inset ground and a shared radius
           token, no outline of its own. It reads as a status line, not a card —
           and since it moved beside the name it also stops short of the width
           it does not need. */
        '.quota-primary-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;flex:0 1 auto;min-width:0;',
        'font-size:12px;padding:5px 10px;border-radius:var(--radius-md);background:var(--bg-surface-inset)}',
        '.quota-when{display:inline-flex;align-items:center;gap:6px;color:var(--text-muted);font-size:11px}',
        '.quota-primary-text{font-weight:500;color:var(--text-secondary)}',
        '.quota-primary-text.exhausted{color:var(--status-bad);font-weight:600}',
        '.quota-primary-text.okstate{color:var(--text-primary)}',
        '.quota-primary-text.unknown{color:var(--text-muted);font-style:italic}',

        /* 8px Gradient Progress Bars */
        '.progress-wrap{margin:6px 0 2px}',
        '.progress-bar{height:8px;border-radius:var(--radius-pill);background:var(--bg-surface-inset);',
        'border:1px solid rgba(255, 255, 255, 0.05);overflow:hidden}',
        '.progress-fill{height:100%;border-radius:var(--radius-pill);transition:width 0.6s cubic-bezier(0.16, 1, 0.3, 1)}',
        '.progress-fill.ok{background:var(--grad-ok)}',
        '.progress-fill.warn{background:var(--grad-warn)}',
        '.progress-fill.bad{background:var(--grad-bad)}',
        '.progress-fill.stale{background:linear-gradient(90deg, #9a6b21 0%, #d6a54a 100%)}',
        '.progress-fill.unmetered{background:repeating-linear-gradient(45deg, rgba(255,255,255,0.10), rgba(255,255,255,0.10) 6px, rgba(255,255,255,0.04) 6px, rgba(255,255,255,0.04) 12px);width:100%}',

        /* Window tiles — the grid gives them their place, the inset ground and
           a hairline edge give them their shape. Chips and stamps below are
           shared with the card above. */

        '.tile-head{display:flex;align-items:center;justify-content:space-between;font-size:11px;',
        'font-weight:500;color:var(--text-secondary);gap:8px}',
        '.tile-meta{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;',
        'font-size:11px;color:var(--text-muted);margin-top:6px}',
        '.quota-last-known{margin-top:10px;padding:9px 10px;border:1px solid rgba(214,165,74,0.34);',
        'border-radius:var(--radius-md);background:rgba(214,165,74,0.07)}',
        '.last-known-copy{font-size:11px;color:#e5c27b}',
        '.quota-tile.stale{border-color:rgba(214,165,74,0.30);background:rgba(214,165,74,0.06)}',
        '.quota-tile.stale .tile-pct,.quota-tile.stale .tile-when-word{color:var(--status-stale)!important}',
        '.quota-tile.stale .tile-model,.quota-tile.stale .model-chip,.quota-tile.stale .ticker{',
        'color:#e5c27b;background:rgba(214,165,74,0.10)}',
        '.quota-observed{font-size:11px;color:var(--text-muted)}',
        '.quota-unavailable{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:8px;',
        'font-size:11px;color:#fde68a}',
        '.quota-action{padding:1px 7px;border-radius:var(--radius-pill);background:var(--status-warn-bg);',
        'color:#fde68a}',

        /* Tickers & Monospace Counters */
        '.ticker{font-family:var(--font-mono);font-variant-numeric:tabular-nums;font-weight:500;',
        'background:rgba(255,255,255,0.07);padding:1px 6px;border-radius:var(--radius-sm)}',
        '.ticker.bad{color:var(--status-bad);background:var(--status-bad-bg)}',

        /* Model Chips */
        '.model-chips-wrap{display:flex;align-items:center;gap:4px;flex-wrap:wrap}',
        '.model-chip{display:inline-flex;align-items:center;font-size:11px;padding:1px 6px;',
        'border-radius:var(--radius-sm);background:rgba(255,255,255,0.07);color:var(--text-secondary)}',
        '.model-chip.exhausted{color:var(--status-bad);background:var(--status-bad-bg)}',

        /* Empty State */
        '.empty-card{background:var(--glass-fill);border-radius:var(--radius-lg);',
        'padding:22px 20px;text-align:center;color:var(--text-muted);display:flex;flex-direction:column;align-items:center;gap:8px}',
        '.empty-icon{opacity:0.55;color:var(--text-muted)}',
        '.empty-title{font-size:14px;font-weight:600;color:var(--text-primary);margin:0}',
        '.empty-desc{font-size:12px;color:var(--text-muted);max-width:320px;margin:0}',
        '.empty-notes{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:center}',
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
        caret: ['M6 9l6 6 6-6']
    };

    // Family marks are the vendors' own: a widget that names an account's CLI
    // and then draws a shape of its own invention makes the reader guess. These
    // are filled marks, not stroked outlines, so they take their own renderer.
    // Cursor publishes no vector mark, so that one is a plain pointer — said
    // out loud rather than passed off as official.
    var BRAND_PATHS = {
        codex: 'M8.086.457a6.105 6.105 0 0 1 3.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 0 0 .107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198a5.62 5.62 0 0 1 .421 2.126 5.655 5.655 0 0 1-.18 1.631.167.167 0 0 0 .04.155 5.982 5.982 0 0 1 1.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 0 1-2.934 1.851.162.162 0 0 0-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 0 0-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 0 1-2.595-.622 6.058 6.058 0 0 1-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 0 1-.495-1.283 6.11 6.11 0 0 1-.017-3.064.166.166 0 0 0 .008-.074.115.115 0 0 0-.037-.064 5.958 5.958 0 0 1-1.38-2.202 5.196 5.196 0 0 1-.333-1.589 6.915 6.915 0 0 1 .188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 0 0 .087-.087A6.016 6.016 0 0 1 5.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 0 0-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 0 0 1.46.864l1.94-3.272a.849.849 0 0 0 .007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 0 0 0 1.695h4.848a.849.849 0 0 0 0-1.696h-4.848z',
        claude: 'm4.709 15.955 4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 0 1-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z',
        cursor: 'M5 2.5l14.5 8.2-6.4 1.6-2.9 6.2z'
    };

    function svgIcon(paths, size, filled) {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('width', String(size));
        svg.setAttribute('height', String(size));
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
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
            var path = document.createElementNS(ns, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function icon(name, size) {
        return svgIcon(ICON_PATHS[name] || [], size || 14, false);
    }

    // Vendor marks are filled shapes on the same 24-grid as the stroked icons:
    // they take a fill and no stroke, where icon() does the opposite. Two names
    // rather than one function with a flag — a bare "true" at the call site
    // says nothing about what it switches.
    function brandIcon(name, size) {
        return svgIcon([BRAND_PATHS[name]], size, true);
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

    function relTime(iso) {
        if (!iso) return '';
        var at = Date.parse(String(iso));
        if (!isFinite(at)) return '';
        var deltaMin = Math.round((at - Date.now()) / 60000);
        var future = deltaMin >= 0;
        var mins = Math.abs(deltaMin);
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
        if (state === 'ok') return 'quota-primary-text okstate';
        return 'quota-primary-text unknown';
    }

    function hasPct(value) {
        return value !== null && value !== undefined;
    }

    function getProgressTone(usedPct) {
        if (!hasPct(usedPct)) return 'unmetered';
        if (usedPct >= 85) return 'bad';
        if (usedPct >= 60) return 'warn';
        return 'ok';
    }

    // The bar and the number above it must never disagree, so both take the
    // threshold from the one function that knows it.
    var TONE_COLOR = {
        ok: 'var(--status-ok)',
        warn: 'var(--status-warn)',
        bad: 'var(--status-bad)',
        unmetered: 'var(--text-muted)'
    };

    // The fill is the same everywhere: same threshold, same clamp, same class.
    // Only the shell around it differs — a full-width bar in the card, a short
    // one on the button and in a row.
    function progressFill(usedPct, stale) {
        var fill = el('div', 'progress-fill ' + (stale ? 'stale' : getProgressTone(usedPct)));
        if (hasPct(usedPct)) {
            fill.style.width = Math.min(100, Math.max(0, usedPct)) + '%';
        }
        return fill;
    }

    function renderProgressBar(usedPct, stale) {
        var wrap = el('div', 'progress-wrap');
        var bar = el('div', 'progress-bar');
        bar.appendChild(progressFill(usedPct, stale));
        wrap.appendChild(bar);
        return wrap;
    }

    // A failed check arrives as tone 'warn' — 'bad' is never emitted by
    // verification_view, so testing for it alone matched nothing.
    function verificationFailed(account) {
        var tone = (account.verification || {}).tone;
        return tone === 'warn' || tone === 'bad';
    }

    // Two questions, deliberately different answers, and both are asked in the
    // same row of the selector — so the difference is written here rather than
    // being inferred from two call sites.
    //
    //   isAlertAccount — "calls for a look": spent quota, cooldown, a failed
    //   check, a switched-off account. It feeds the dot and the "N need
    //   attention" badge. An account nobody has logged into is NOT in it:
    //   there is nothing to attend to, only something to set up.
    //
    //   isTroubled — "something to fix": not signed in, switched off, or the
    //   check failed. It decides whether the engine's own explanation is worth
    //   a line. A stale reading is in neither: nobody has to do anything.

    // Why this account is in trouble, in the engine's own words — or '' when it
    // is not. Both the yes/no question and the word for the screen come from
    // here, so the two can never answer differently.
    function troubleReason(account) {
        if (!account.signed_in) return 'not signed in';
        if (account.enabled === false) return 'disabled';
        if (verificationFailed(account)) {
            // The engine always sends a label with the tone — but if it ever
            // sends the tone alone, an empty string here would make a failed
            // check look like a healthy account, which is the one thing this
            // widget must never do.
            return (account.verification || {}).label || 'verification failed';
        }
        return '';
    }

    function isTroubled(account) {
        return !!troubleReason(account);
    }

    function isAlertAccount(account) {
        var hasCooldown = false;
        liveWindows(account).forEach(function (c) {
            // Honesty rule 4: a per-model cap never marks the whole account.
            // The cooldown branch used to ignore that and did mark it.
            if (c.scoped_models && c.scoped_models.length) return;
            if (isCooling(c)) hasCooldown = true;
        });
        return (account.quota && account.quota.state === 'exhausted') ||
               verificationFailed(account) ||
               hasCooldown ||
               account.enabled === false;
    }

    function liveWindows(account) {
        return ((account.quota || {}).constraints || []);
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
    function splitWindowName(label, seconds) {
        var length = windowLength(seconds);
        var text = String(label || '').trim();
        if (!text) return { model: length ? '' : 'Window Limit', role: '' };
        var parts = text.split(/\s+/);
        var role = ROLE_WORDS.indexOf(parts[parts.length - 1].toLowerCase()) !== -1
            ? parts.pop() : '';
        var rest = parts.join(' ');
        var bracket = /\(([^()]+)\)/.exec(rest);
        if (bracket) return { model: bracket[1], role: role };
        // A name that only repeats the length is not worth a chip of its own —
        // but only when it repeats it exactly.
        if (length && sameLength(rest, seconds)) return { model: '', role: role };
        // "primary" on its own leaves nothing behind once the role is taken;
        // with no length either, the tile would carry no name at all.
        if (!rest && !length) return { model: 'Window Limit', role: role };
        return { model: rest, role: role };
    }

    function renderConstraint(view, stale) {
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
        // The version is the point of the chip — Ian asked to see "GPT-5.3",
        // not a family word — so the name is never shortened here; it ellipsises
        // when the column is narrow and keeps the whole of itself in the title.
        var scoped = !!(view.scoped_models && view.scoped_models.length);
        // Only a model-scoped window carries a red name chip, and only when it
        // is actually spent: red here means "this model is out", which is the
        // one thing a per-model cap says and a plain window does not. A scoped
        // window at 10 %, or one with no ratio at all, is not an exhausted one.
        var spent = !stale && scoped && hasPct(pct) && pct >= 100;
        if (name.model) {
            var chip = el('span', 'tile-model' + (spent ? ' spent' : ''), name.model);
            chip.title = view.label || '';
            left.appendChild(chip);
        }

        var right = el('span', 'tile-pct');
        right.textContent = hasPct(pct) ? (pct + '% used') : 'Unmetered / No ratio';
        right.style.color = stale ? 'var(--status-stale)' : TONE_COLOR[getProgressTone(pct)];
        header.appendChild(left);
        header.appendChild(right);
        card.appendChild(header);

        card.appendChild(renderProgressBar(view.used_pct, stale));

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
                    var chipCls = 'model-chip' + (spent ? ' exhausted' : '');
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
    // not parse, so callers can tell "nothing to say" from "said it".
    function appendStamp(wrap, iso, bad, withRel) {
        // A value that was read but cannot be parsed is not a missing value.
        // formatResetAt returns '' for both, so the two used to look identical:
        // an unreadable timestamp printed nothing at all, which is the display
        // law of this widget read backwards.
        if (iso && !formatResetAt(iso)) {
            wrap.appendChild(el('span', 'ticker' + (bad ? ' bad' : ''), 'unreadable date'));
            return true;
        }
        var stamp = formatResetAt(iso);
        if (!stamp) return false;
        wrap.appendChild(el('span', 'ticker' + (bad ? ' bad' : ''), stamp));
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
        var unreadableCooldown = view.cooldown_until && !formatResetAt(view.cooldown_until);
        var cooling = isCooling(view) || unreadableCooldown;
        if (cooling) {
            wrap.appendChild(el('span', 'tile-when-word', 'cooldown'));
            appendStamp(wrap, view.cooldown_until, !stale, true);
        }
        // With a cooldown already spelled out, the reset needs its date, not a
        // second relative reading beside the first. The word goes in only once
        // a date is known to follow it: the engine reports a cooldown with no
        // reset often enough, and "· resets" alone ends the line on a promise.
        var reset = el('span');
        if (appendStamp(reset, view.resets_at, false, !cooling)) {
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

    function renderQuota(parent, quota, account) {
        if (quota.note) {
            var noteP = withIcon(el('div', 'meta', quota.note), 'info');
            noteP.style.margin = '4px 0 0 2px';
            parent.appendChild(noteP);
        }

        renderAbsence(parent, account, quota.absence);

        if (quota.constraints && quota.constraints.length) {
            var constraintsWrap = el('div', 'quotas-container');
            quota.constraints.forEach(function (view) {
                constraintsWrap.appendChild(renderConstraint(view, false));
            });
            parent.appendChild(constraintsWrap);
        }

        if (quota.stale && quota.stale.length) {
            quota.stale.forEach(function (snap) {
                var staleBlock = el('div', 'quota-last-known');
                staleBlock.appendChild(withIcon(el(
                    'div',
                    'last-known-copy',
                    'Last known · observed ' + (relTime(snap.observed_at) || 'at an unreported time')
                        + ' · not used to grant routing'
                ), 'warn', 12));
                if (snap.constraints && snap.constraints.length) {
                    var staleConstraints = el('div', 'quotas-container');
                    snap.constraints.forEach(function (view) {
                        staleConstraints.appendChild(renderConstraint(view, true));
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
    function worstUsedPct(account) {
        var worst = -1;
        liveWindows(account).forEach(function (c) {
            if (c.scoped_models && c.scoped_models.length) return;
            if (typeof c.used_pct === 'number' && c.used_pct > worst) worst = c.used_pct;
        });
        return worst < 0 ? null : worst;
    }

    // Green means "read, and fine". A window that was never read, or read and
    // refused, is grey — the same display law the header states: a value that
    // was not read is never dressed as a good one.
    // The dot answers "how is this account", and that answer cannot be greener
    // than the reading it stands on: with the accounts facet unread, identity
    // and quota are last-known values, and a live green dot over them is the
    // exact claim this widget exists to refuse.
    function accountTone(account, facets) {
        if (isAlertAccount(account)) return 'bad';
        if (facets && facets.accounts && facets.accounts !== 'ok') return 'muted';
        if (!isActive(account)) return 'muted';
        var quota = account.quota || {};
        if (quota.state && quota.state !== 'ok') return 'muted';
        var worst = worstUsedPct(account);
        if (worst === null) return 'muted';
        return getProgressTone(worst);
    }

    function renderAccount(parent, group, account, facets) {
        var tile = el('div');
        var head = el('div', 'account-head');
        var headLeft = el('div', 'account-head-left');
        var header = el('div', 'account-header');

        var titleWrap = el('div', 'account-title-wrap');
        var tone = accountTone(account, facets);
        titleWrap.appendChild(stateDot(tone, true));
        // The dot is the account's state; the same state goes in as a word a
        // screen reader can read.
        titleWrap.appendChild(el('span', 'sr-only', TONE_WORD[tone] || tone));
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
        headLeft.appendChild(header);

        var meta = el('div', 'account-meta');
        // The selector above shows the account's display name, which is not its
        // address: a profile called "work" has no e-mail in it at all. And when
        // the status strip is open the selector is not on screen, so without
        // this line the numbers below would have no owner.
        if (account.email && account.email !== account.label) {
            meta.appendChild(el('span', null, account.email));
        }
        if (account.kind !== 'profile') {
            meta.appendChild(el('span', null, 'Vendor CLI login'));
        }
        var observed = relTime(quotaObservedAt(account));
        meta.appendChild(el('span', 'quota-observed', observed
            ? ('Quota observed ' + observed)
            : 'No quota observation time reported'));

        if (account.verification && account.verification.label) {
            var vClass = verificationFailed(account) ? 'meta-bad' : null;
            meta.appendChild(el('span', vClass, account.verification.label));
        }
        if (!account.signed_in) {
            meta.appendChild(el('span', 'meta-muted', 'not signed in'));
        } else if (account.enabled === false) {
            meta.appendChild(el('span', 'meta-muted', 'disabled'));
        }
        if (facets.accounts && facets.accounts !== 'ok') {
            meta.appendChild(el('span', null, 'Accounts facet: ' + facetWord(facets.accounts) + ' (last known)'));
        }
        headLeft.appendChild(meta);
        head.appendChild(headLeft);
        // When the engine's verdict IS the worst window — state 'ok' spells it
        // as "58% used" with that window's own reset time — the tiles below say
        // the same number and the same date, and the frame opens at 320. The
        // verdict earns its place when it says something they cannot: a limit
        // reached, a facet unread, no window reported at all.
        var quota = account.quota || {};
        var echoesTiles = quota.state === 'ok'
            && !!(quota.constraints && quota.constraints.length);
        if (!echoesTiles) head.appendChild(renderQuotaVerdict(quota));
        tile.appendChild(head);

        renderQuota(tile, account.quota || {}, account);
        parent.appendChild(tile);
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

    function groupHasAlert(group) {
        return groupTrouble(group) || (group.accounts || []).some(isAlertAccount);
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

    function renderHarnessSeg(parent, groups, selected, hasAnswer) {
        var seg = el('div', 'harness-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Agent family');

        // Before the first answer the row would otherwise hold an empty 8px
        // capsule where the marks belong, and the widget looks broken while it
        // is merely reading. These are the families this widget knows how to
        // draw, shown faint and dead: a shape waiting to be filled, not a
        // claim that the reader has these three.
        if (!groups.length && !hasAnswer) {
            ['codex', 'claude', 'cursor'].forEach(function (name) {
                var ghost = el('button', 'harness-btn loading');
                ghost.disabled = true;
                ghost.appendChild(brandIcon(name, 15));
                seg.appendChild(ghost);
            });
            seg.setAttribute('aria-label', 'Reading agent families');
            parent.appendChild(seg);
            return;
        }

        groups.forEach(function (group) {
            var isOn = selected && group.harness_id === selected.harness_id;
            var count = (group.accounts || []).length;
            var alert = groupHasAlert(group);
            var btn = el('button', 'harness-btn' + (isOn ? ' active' : '') + (count ? '' : ' empty'));
            // A family the widget has no mark for gets its own initial, never
            // somebody else's logo: a wrong mark is a wrong claim about whose
            // account this is.
            btn.appendChild(BRAND_PATHS[group.harness_id]
                ? brandIcon(group.harness_id, 15)
                : el('span', 'harness-initial', (familyName(group).charAt(0) || '?').toUpperCase()));
            var name = familyName(group);
            var say = name + ' — ' + (count
                ? (count + (count === 1 ? ' account' : ' accounts') + (alert ? ', needs attention' : ''))
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
                rerender();
            });
            if (alert) btn.appendChild(el('span', 'pip bad harness-pip'));
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

    // Codex holds two seven-day windows, so "week" twice would name neither.
    // Colliding lengths are marked instead, and the row prints their full names
    // on a line of its own, in the same order as the bars. Windows scoped to a
    // model stay out of the collision: their model chip already tells them apart.
    function windowTags(constraints) {
        var marks = constraints.map(function (c) {
            return {
                c: c,
                scoped: !!(c.scoped_models && c.scoped_models.length),
                tag: windowLength(c.window_seconds)
            };
        });
        var seen = {};
        marks.forEach(function (m) {
            if (m.tag && !m.scoped) seen[m.tag] = (seen[m.tag] || 0) + 1;
        });
        marks.forEach(function (m) {
            if (!m.tag) {
                // No length reported: the window keeps its own name. With no
                // name either it takes the same stand-in the card uses, so a
                // read window is never silently dropped from the row.
                m.tag = String(m.c.label || '') || 'Window Limit';
                return;
            }
            if (m.scoped || seen[m.tag] < 2) return;
            // Two windows of the same length are told apart by their names, but
            // the names run to 27 characters. They go on a line of their own,
            // in the same order as the bars above them.
            m.named = true;
        });
        return marks;
    }

    // One sentence per fact: the card says "83% used" because it has room for
    // the verb, the row and the button say "83%". The missing ratio takes the
    // half of the card's own wording that is true — the engine reported no
    // number. "unmetered" alone would read as "unlimited", which is the one
    // word the display law at the top of this file forbids.
    function usedText(usedPct) {
        return hasPct(usedPct) ? (usedPct + '%') : 'no ratio';
    }

    // Null, not an empty bar, when there is no ratio: at 44px the card's hatched
    // "not measured" fill reads as a full dark bar, which is the opposite of
    // what it means. Callers check before appending.
    function spark(usedPct) {
        if (!hasPct(usedPct)) return null;
        var bar = el('span', 'progress-bar spark');
        bar.appendChild(progressFill(usedPct));
        return bar;
    }

    // The second floor of a row is one of three things, in this order, and each
    // has its own condition: nothing falls through to it by default, because a
    // line drawn "because there was room" is a claim nobody checked.
    function appendSecondFloor(parent, account) {
        var windows = liveWindows(account);
        var spoken = [];
        var named = [];
        if (windows.length) {
            var wins = el('div', 'acct-wins');
            windowTags(windows).forEach(function (m) {
                var win = el('span', 'acct-win');
                var bar = spark(m.c.used_pct);
                if (bar) win.appendChild(bar);
                var pct = usedText(m.c.used_pct);
                // A tag can be a whole phrase — "1 reset credit available" is a
                // real window name — and "name no ratio" then runs together as
                // one sentence. With no ratio there is no bar either, so the
                // absent figure is already visible: the name stands alone and
                // the full wording stays in the tooltip.
                win.appendChild(el('span', 'acct-win-tag', m.tag));
                if (hasPct(m.c.used_pct)) {
                    win.appendChild(el('span', 'acct-win-pct', pct));
                }
                if (m.scoped) {
                    var models = m.c.scoped_models;
                    var chip = models[0] + (models.length > 1 ? ' +' + (models.length - 1) : '');
                    // Red is spent, and only spent — the card next to it paints
                    // this chip by the same threshold.
                    var chipCls = 'acct-cap' + (m.c.used_pct >= 100 ? ' exhausted' : '');
                    win.appendChild(el('span', chipCls, chip));
                }
                var cooling = isCooling(m.c);
                if (cooling) win.appendChild(el('span', 'acct-cap exhausted', 'cooldown'));
                win.title = (m.c.label || m.tag) + ' — ' + pct
                    + (m.c.resets_at ? ', resets ' + formatResetAt(m.c.resets_at) : '')
                    // The tile above says "cooldown" with the same date; the
                    // row used to drop it, leaving a red mark with no reason.
                    + (cooling ? ', cooldown until ' + formatResetAt(m.c.cooldown_until) : '');
                // Without the model, "week 58%" and "week 100%" are the same
                // sentence twice — and the chip that tells them apart is exactly
                // what a screen reader cannot see.
                spoken.push(m.tag + ' ' + pct
                    + (m.scoped ? ' ' + m.c.scoped_models.join(', ') : '')
                    + (cooling ? ' cooldown' : ''));
                if (m.named && m.c.label) named.push(String(m.c.label));
                wins.appendChild(win);
            });
            parent.appendChild(wins);
            if (named.length) {
                parent.appendChild(el('div', 'acct-line3', 'windows: ' + named.join(' · ')));
            }
            return spoken.join(', ') + (named.length ? ' · windows: ' + named.join(', ') : '');
        }
        // Raw engine detail may contain local paths or vendor bodies. The row
        // uses only the independently typed account state; quota actions are
        // mapped separately from typed quota absences.
        if (isTroubled(account)) {
            var reason = troubleReason(account);
            parent.appendChild(el('div', 'acct-line2', reason));
            return reason;
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
        // window past 85% turns the dot without being an alert.
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

        var worst = account ? worstUsedPct(account) : null;
        var word = account ? tailWord(account) : '';
        if (worst !== null) {
            btn.appendChild(spark(worst));
            btn.appendChild(el('span', 'acct-count', usedText(worst)));
        }
        // A figure and a fault are not alternatives: an account can be read,
        // counted and still have a check that failed. Showing only the number
        // left the fault to be discovered by opening the list.
        if (word) btn.appendChild(el('span', 'acct-count', word));
        if (elsewhere) {
            btn.appendChild(el('span', 'acct-alarm',
                elsewhere + (elsewhere === 1 ? ' needs' : ' need') + ' attention'));
        }
        btn.appendChild(withIcon(el('span', 'acct-caret'), 'caret', 12));

        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.setAttribute('aria-label', accounts.length
            ? ('Account: ' + (account ? account.label : '')
               + (worst !== null ? ' — ' + worst + '% used' : '')
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
            rerender();
        });
        wrap.appendChild(btn);

        if (open) {
            // Buttons in a labelled group, not a listbox: role="listbox"
            // promises arrow-key navigation, and promising a contract that is
            // not implemented is worse for a screen reader than plain buttons.
            var pop = el('div', 'acct-pop');
            pop.setAttribute('role', 'group');
            pop.setAttribute('aria-label', 'Accounts in ' + familyName(group));
            accounts.forEach(function (candidate) {
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
                var floorSpeech = appendSecondFloor(body, candidate);
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
                    focusAccountBtn = true;
                    rerender();
                });
                pop.appendChild(opt);
            });
            wrap.appendChild(pop);
        }
        parent.appendChild(wrap);
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

    function render(view, staleText, actionText) {
        currentView = view;
        staleMessage = staleText;
        actionMessage = actionText || '';

        // The whole tree is rebuilt every 30 seconds on its own. Without this
        // the keyboard focus would drop to the body mid-use — including focus
        // sitting on a row of an open list.
        var was = document.activeElement;
        var focusWas = (was && was.getAttribute) ? was.getAttribute('data-focus') : null;

        root.textContent = '';

        var facets = view.facets || {};
        var groups = view.groups || [];

        var selection = syncSelection(groups);

        /* 1. Control Bar: family segment, account selector, status, refresh.
           There is no header row: the frame opens at 320px and the host already
           prints the widget's own name above it. */
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

        if (statusOpen) {
            /* Opened, status takes the row sideways: the strip stands in place
               of the family segment and the account selector rather than
               covering them, so nothing is hidden underneath. */
            var strip = el('div', 'status-strip');
            // A daemon whose state never arrived is not a running daemon: it
            // says so, exactly the way an unread facet says so.
            var dRow = daemon.state
                ? dotLabel('daemon ' + daemon.state
                    + (daemon.engine_version ? ' · ' + daemon.engine_version : ''),
                    daemonDown ? 'bad' : 'ok')
                : dotLabel('daemon not reported', 'muted');
            dRow.className = 'dot-label strong';
            strip.appendChild(dRow);
            FACET_ORDER.forEach(function (f) {
                var state = facets[f] || 'indeterminate';
                // A green dot already says "read". The word is spent only on
                // the states where something actually went wrong.
                strip.appendChild(dotLabel(state === 'ok' ? f : f + ' ' + facetWord(state), facetTone(state)));
            });
            controlBar.appendChild(strip);
        } else {
            renderHarnessSeg(controlBar, groups, selection.group, hasAnswer);
            renderAccountSelect(controlBar, selection.group, selection.account, hasAnswer, facets);
        }

        var statusWrap = el('div', 'status-wrap');
        var statusBtn = el('button', 'round-btn' + (statusOpen ? ' is-open' : '') + (statusProblem ? ' has-problem' : ''));
        statusBtn.appendChild(icon('signal', 13));
        statusBtn.setAttribute('aria-expanded', statusOpen ? 'true' : 'false');
        statusBtn.setAttribute('aria-label', !hasAnswer
            ? 'Status — nothing read yet'
            : (statusProblem
                ? 'Status — the daemon or a facet did not answer'
                : 'Status — daemon running, all facets read'));
        statusBtn.title = statusBtn.getAttribute('aria-label');
        statusBtn.setAttribute('data-focus', 'status');
        statusBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            statusOpen = !statusOpen;
            accountsOpen = false;
            rerender();
        });
        statusWrap.appendChild(statusBtn);
        statusWrap.appendChild(el('span', 'pip status-pip ' + (!hasAnswer ? 'muted' : (statusProblem ? 'bad' : 'ok'))));
        controlBar.appendChild(statusWrap);

        var refreshBtn = el('button', 'round-btn accent' + (inFlight ? ' is-refreshing' : ''));
        refreshBtn.appendChild(withIcon(el('span', 'icon-spin'), 'refresh', 13));
        refreshBtn.disabled = inFlight;
        refreshBtn.setAttribute('aria-label', inFlight ? 'Refreshing…' : 'Refresh');
        refreshBtn.title = refreshBtn.getAttribute('aria-label');
        refreshBtn.setAttribute('data-focus', 'refresh');
        refreshBtn.addEventListener('click', function () {
            if (inFlight) return;
            refreshQuota();
        });
        controlBar.appendChild(refreshBtn);
        root.appendChild(controlBar);

        /* 2. Banners. What the status button folds away is only ever the good
           news: a daemon that is down and a facet that did not answer both
           still say so in the open, above the account — and they speak for
           every family, not only the one on screen. */
        if (daemonDown) {
            banner('warn', 'Claudexor daemon is ' + daemon.state
                + '. Readings below are last known, not live.', true);
        }
        if (staleMessage) {
            banner('warn', 'Reading could not be refreshed (' + staleMessage + '). Cached data from '
                + (relTime(new Date(lastGoodAt).toISOString()) || 'earlier') + ' is shown.', true);
        }
        if (actionMessage) {
            banner('warn', actionMessage, true);
        }
        if (view.transport_error && !staleMessage) {
            banner('error', 'Endpoint unreachable. No quota claims made.', true);
        }
        if (view.facet_note) {
            banner('info', 'Facets unavailable: ' + view.facet_note + '. Values shown as unread/last known, not zero.', false);
        }

        /* 3. The one selected account. */
        if (selection.account) {
            renderAccount(root, selection.group, selection.account, facets);
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
    }

    function load() {
        if (inFlight) return;
        inFlight = true;
        rerender();

        var mine = ++generation;
        // The bridge is not the native fetch: if it throws synchronously the
        // promise chain never forms, and inFlight would stay true forever.
        Promise.resolve().then(function () {
            return window.fetch(ROUTE, { method: 'GET' });
        }).then(function (response) {
            return response.text().then(function (body) {
                return { ok: response.ok, status: response.status, body: body };
            });
        }).then(function (result) {
            inFlight = false;
            if (mine !== generation || stopped) return;
            var view = null;
            try { view = JSON.parse(result.body); } catch (err) { view = null; }
            if (!result.ok || !view || typeof view !== 'object') {
                var why = result.ok ? 'unreadable response body' : ('HTTP ' + result.status);
                if (lastGood) { render(lastGood, why); } else {
                    render({ facets: {}, groups: [], daemon: {}, transport_error: 'widget route ' + why }, '');
                }
                return;
            }
            if (view.ok) { lastGood = view; lastGoodAt = Date.now(); }
            actionMessage = '';
            // An answer that reports itself as not ok normally carries its own
            // reason. If it does not, say so rather than draw it as fresh.
            var unexplained = !view.ok && !view.transport_error && !view.facet_note;
            render(view, unexplained ? 'response reported itself incomplete' : '');
        }).catch(function (err) {
            inFlight = false;
            if (mine !== generation || stopped) return;
            var why = 'bridge error';
            if (lastGood) { render(lastGood, why); } else {
                render({ facets: {}, groups: [], daemon: {}, transport_error: why }, '');
            }
        });
    }

    function refreshQuota() {
        if (inFlight) return;
        inFlight = true;
        actionMessage = '';
        rerender();

        var mine = ++generation;
        Promise.resolve().then(function () {
            return window.fetch(REFRESH_ROUTE, { method: 'POST' });
        }).then(function (response) {
            return response.text().then(function (body) {
                return { ok: response.ok, status: response.status, body: body };
            });
        }).then(function (result) {
            inFlight = false;
            if (mine !== generation || stopped) return;
            var response = null;
            try { response = JSON.parse(result.body); } catch (err) { response = null; }
            if (!result.ok || !response || typeof response !== 'object' || !response.ok) {
                var compatible = response && response.compatibility_error;
                actionMessage = compatible
                    ? 'Live refresh requires a newer Ouroboros host.'
                    : 'Live quota refresh failed. Cached quota data remains visible.';
                rerender();
                return;
            }
            var merged = mergeQuotaFacet(currentView || lastGood || {}, response);
            currentView = merged;
            lastGood = merged;
            lastGoodAt = Date.now();
            staleMessage = '';
            actionMessage = '';
            render(merged, '', '');
        }).catch(function () {
            inFlight = false;
            if (mine !== generation || stopped) return;
            actionMessage = 'Live quota refresh failed. Cached quota data remains visible.';
            rerender();
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
        for (i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-focus') === key && !nodes[i].disabled) {
                nodes[i].focus();
                return;
            }
        }
        // The row the keyboard was on is gone — the list closed under it, or
        // that account did. Dropping to the body would strand whoever is on
        // the keyboard; the button the list belongs to is the nearest home.
        if (key.indexOf('opt:') === 0) restoreFocus('account-btn');
    }

    function rerender() {
        render(currentView || { facets: {}, groups: [], daemon: {} }, staleMessage, actionMessage);
    }

    // What opens inside the row has to be dismissible the way every popover is:
    // a click elsewhere or Escape. Both listeners are named rather than inline
    // so that stop() can take them off the document again.
    function onDocumentClick(e) {
        var t = e.target;
        var inside = t && typeof t.closest === 'function' ? t.closest('.status-wrap, .acct-wrap') : null;
        if (inside) return;
        var changed = false;
        if (statusOpen) { statusOpen = false; changed = true; }
        if (accountsOpen) { accountsOpen = false; changed = true; }
        if (changed) rerender();
    }

    function onDocumentKey(e) {
        if (e.key !== 'Escape') return;
        // Only one of the two is ever open — each opening closes the other —
        // so this order is a safety rail, not a stack.
        if (accountsOpen) {
            accountsOpen = false;
            focusAccountBtn = true;
            rerender();
            return;
        }
        if (!statusOpen) return;
        statusOpen = false;
        rerender();
    }

    function stop() {
        stopped = true;
        generation++;
        if (dataTimer !== null) { window.clearInterval(dataTimer); dataTimer = null; }
        document.removeEventListener('click', onDocumentClick);
        document.removeEventListener('keydown', onDocumentKey);
    }

    // Both the first run and the return from the back/forward cache need the
    // same three things started; describing them twice is how they drift.
    // The sheet is static, so it is written once and lives outside the tree
    // render() clears. Rebuilding it on every redraw re-applied 14 KB of CSS
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
        document.addEventListener('click', onDocumentClick);
        document.addEventListener('keydown', onDocumentKey);
        if (dataTimer === null) {
            dataTimer = window.setInterval(function () {
                if (document.visibilityState === 'visible') load();
            }, REFRESH_MS);
        }
        load();
    }

    window.addEventListener('pagehide', stop);
    // Restored from the back/forward cache the widget is otherwise dead for
    // good: the timer is gone and every answer is dropped by the stopped flag,
    // while Refresh keeps spinning as if it worked.
    window.addEventListener('pageshow', function () {
        if (!stopped) return;
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
