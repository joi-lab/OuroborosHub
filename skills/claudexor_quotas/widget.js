/* Claudexor Quotas widget — v0.2.0
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
    var REFRESH_MS = 30000;
    var TICK_MS = 1000;

    var root = document.getElementById('root');
    var generation = 0;
    var dataTimer = null;
    var tickTimer = null;
    var lastGood = null;
    var lastGoodAt = 0;
    var stopped = false;
    var inFlight = false;
    var currentFilter = 'all'; // 'all' | 'active' | 'alerts'
    var currentView = null;

    var STYLE = [
        ':root{color-scheme:dark}',
        'body{margin:0;font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;',
        'color:#e8ecf3;background:transparent;height:100vh;box-sizing:border-box;overflow-y:auto;padding:10px}',
        '*{box-sizing:border-box}',
        '.hdr{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 12px}',
        '.hdr h2{font-size:14px;margin:0;font-weight:600;letter-spacing:.01em;display:flex;align-items:center;gap:6px}',
        '.spacer{flex:1 1 auto}',
        'button{font:inherit;color:#e8ecf3;background:rgba(255,255,255,.07);cursor:pointer;',
        'border:1px solid rgba(255,255,255,.16);border-radius:8px;padding:4px 10px;transition:all .15s ease}',
        'button:hover{background:rgba(255,255,255,.13)}',
        'button:disabled{opacity:.5;cursor:not-allowed}',
        '.tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1px solid rgba(255,255,255,.1);padding-bottom:6px}',
        '.tab-btn{font-size:12px;padding:4px 10px;border-radius:6px;border:none;background:transparent;color:#9fb0c7}',
        '.tab-btn.active{background:rgba(255,255,255,.12);color:#fff;font-weight:600}',
        '.tab-btn .badge{font-size:10px;padding:1px 5px;border-radius:999px;background:rgba(255,255,255,.15);margin-left:5px}',
        '.chip{display:inline-flex;align-items:center;gap:5px;font-size:11px;padding:2px 8px;',
        'border-radius:999px;border:1px solid rgba(255,255,255,.16);white-space:nowrap}',
        '.chip.ok{color:#8ee7b0;border-color:rgba(142,231,176,.4);background:rgba(142,231,176,.06)}',
        '.chip.warn{color:#ffc78a;border-color:rgba(255,199,138,.45);background:rgba(255,199,138,.06)}',
        '.chip.bad{color:#ff9b9b;border-color:rgba(255,155,155,.45);background:rgba(255,155,155,.06)}',
        '.chip.muted{color:#9fb0c7}',
        '.banner{border-radius:10px;padding:9px 12px;margin:0 0 10px;font-size:12px;',
        'border:1px solid rgba(255,199,138,.45);background:rgba(255,199,138,.09);color:#ffd9ac;display:flex;gap:8px;align-items:flex-start}',
        '.banner.bad{border-color:rgba(255,155,155,.5);background:rgba(255,155,155,.09);color:#ffc4c4}',
        '.card{border:1px solid rgba(255,255,255,.11);border-radius:12px;padding:12px 14px;',
        'margin:0 0 12px;background:rgba(255,255,255,.035)}',
        '.card>h3{margin:0;font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
        '.sub{color:#9fb0c7;font-size:11px;margin:3px 0 0}',
        '.row{border-top:1px solid rgba(255,255,255,.08);padding:10px 0 4px;margin-top:10px}',
        '.row .name{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-weight:500}',
        '.meta{color:#9fb0c7;font-size:11px;margin:6px 0 0}',
        '.q{margin:6px 0 0;font-size:12px}',
        '.q.exhausted{color:#ff9b9b}',
        '.q.okstate{color:#cfe0f5}',
        '.q.unknown{color:#9fb0c7;font-style:italic}',
        '.progress-wrap{margin:6px 0}',
        '.progress-bar{height:6px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden;position:relative}',
        '.progress-fill{height:100%;border-radius:999px;transition:width .4s ease, background-color .4s ease}',
        '.progress-fill.ok{background:#8ee7b0}',
        '.progress-fill.warn{background:#ffc78a}',
        '.progress-fill.bad{background:#ff9b9b}',
        '.constraint-card{background:rgba(0,0,0,.15);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:8px 10px;margin-top:6px}',
        '.constraint-header{display:flex;justify-content:space-between;font-size:11px;color:#c3d1e4}',
        '.ticker{font-variant-numeric:tabular-nums;font-weight:600}',
        '.model-badge{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);margin:2px 3px 2px 0}',
        '.model-badge.exhausted{color:#ff9b9b;background:rgba(255,155,155,.12);border-color:rgba(255,155,155,.3)}',
        'details{margin:6px 0 0}',
        'summary{cursor:pointer;color:#9fb0c7;font-size:11px;user-select:none}',
        'summary:hover{color:#e8ecf3}',
        'ul{margin:6px 0 0;padding-left:17px;color:#c3d1e4;font-size:11px}',
        '.stale{color:#ffc78a}',
        '.empty{color:#9fb0c7;font-size:12px;text-align:center;padding:24px 0}'
    ].join('');

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null && text !== '') node.textContent = String(text);
        return node;
    }

    function chip(text, tone) {
        return el('span', 'chip ' + (tone || 'muted'), text);
    }

    function formatCountdown(iso) {
        if (!iso) return '';
        var target = Date.parse(String(iso));
        if (!isFinite(target)) return '';
        var diffSec = Math.floor((target - Date.now()) / 1000);
        if (diffSec <= 0) return 'resetting now';
        
        var d = Math.floor(diffSec / 86400);
        var h = Math.floor((diffSec % 86400) / 3600);
        var m = Math.floor((diffSec % 3600) / 60);
        var s = diffSec % 60;
        
        var pad = function (num) { return (num < 10 ? '0' : '') + num; };
        if (d > 0) return d + 'd ' + pad(h) + ':' + pad(m) + ':' + pad(s);
        if (h > 0) return pad(h) + ':' + pad(m) + ':' + pad(s);
        return pad(m) + ':' + pad(s);
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
        return 'warn';
    }

    function facetWord(state) {
        if (state === 'ok') return 'read';
        if (state === 'not_read') return 'not read';
        if (state === 'failed') return 'failed';
        return 'unknown';
    }

    function quotaClass(state) {
        if (state === 'exhausted') return 'q exhausted';
        if (state === 'ok') return 'q okstate';
        return 'q unknown';
    }

    function getProgressTone(usedPct) {
        if (usedPct === null || usedPct === undefined) return 'muted';
        if (usedPct >= 85) return 'bad';
        if (usedPct >= 60) return 'warn';
        return 'ok';
    }

    function renderProgressBar(usedPct) {
        var wrap = el('div', 'progress-wrap');
        var bar = el('div', 'progress-bar');
        var fill = el('div', 'progress-fill ' + getProgressTone(usedPct));
        fill.style.width = Math.min(100, Math.max(0, usedPct || 0)) + '%';
        bar.appendChild(fill);
        wrap.appendChild(bar);
        return wrap;
    }

    function renderConstraint(view) {
        var card = el('div', 'constraint-card');
        var header = el('div', 'constraint-header');
        
        var left = el('span', null, view.label);
        var right = el('span');
        if (view.used_pct !== null && view.used_pct !== undefined) {
            right.textContent = view.used_pct + '% used';
        } else {
            right.textContent = 'unmetered / no ratio';
        }
        header.appendChild(left);
        header.appendChild(right);
        card.appendChild(header);

        if (view.used_pct !== null && view.used_pct !== undefined) {
            card.appendChild(renderProgressBar(view.used_pct));
        }

        var meta = [];
        if (view.resets_at) {
            var ticker = el('span', 'ticker');
            ticker.setAttribute('data-target', view.resets_at);
            ticker.textContent = formatCountdown(view.resets_at);
            var resets = el('span', null, 'resets in ');
            resets.appendChild(ticker);
            meta.push(resets);
        }
        if (view.cooldown_until) {
            var cTicker = el('span', 'ticker');
            cTicker.setAttribute('data-target', view.cooldown_until);
            cTicker.textContent = formatCountdown(view.cooldown_until);
            var cooling = el('span', 'q exhausted', 'cooldown: ');
            cooling.appendChild(cTicker);
            meta.push(cooling);
        }
        if (view.scoped_models && view.scoped_models.length) {
            var mWrap = el('span');
            view.scoped_models.forEach(function (m) {
                mWrap.appendChild(el('span', 'model-badge' + (view.used_pct >= 100 ? ' exhausted' : ''), m));
            });
            meta.push(mWrap);
        }

        if (meta.length) {
            var sub = el('div', 'meta');
            for (var i = 0; i < meta.length; i++) {
                if (i > 0) sub.appendChild(document.createTextNode(' · '));
                sub.appendChild(meta[i]);
            }
            card.appendChild(sub);
        }

        return card;
    }

    function renderQuota(parent, quota) {
        var p = el('p', quotaClass(quota.state));
        var text = quota.label || 'No data';
        p.textContent = text;
        if (quota.resets_at) {
            var span = el('span');
            span.textContent = ' · resets in ';
            var ticker = el('span', 'ticker');
            ticker.setAttribute('data-target', quota.resets_at);
            ticker.textContent = formatCountdown(quota.resets_at);
            span.appendChild(ticker);
            p.appendChild(span);
        }
        if (quota.note) {
            p.appendChild(document.createTextNode(' · ' + quota.note));
        }
        parent.appendChild(p);

        if (quota.constraints && quota.constraints.length) {
            var constraintsWrap = el('div');
            quota.constraints.forEach(function (view) {
                constraintsWrap.appendChild(renderConstraint(view));
            });
            parent.appendChild(constraintsWrap);
        }

        if (quota.stale && quota.stale.length) {
            var stale = el('details');
            stale.appendChild(el('summary', 'stale', 'stale reading(s) — not used for routing'));
            var slist = el('ul');
            quota.stale.forEach(function (snap) {
                var head = 'observed ' + (relTime(snap.observed_at) || 'at an unreported time')
                    + ' · freshness ' + (snap.freshness || 'unknown');
                slist.appendChild(el('li', 'stale', head));
            });
            stale.appendChild(slist);
            parent.appendChild(stale);
        }
    }

    function isAlertAccount(account) {
        return (account.quota && account.quota.state === 'exhausted') ||
               (account.verification && account.verification.tone === 'bad') ||
               account.enabled === false;
    }

    function renderAccount(parent, account, facets) {
        var row = el('div', 'row');
        var name = el('div', 'name');
        name.appendChild(el('span', null, account.label));
        if (account.caption) name.appendChild(chip(account.caption, 'muted'));
        name.appendChild(chip(account.verification.label, account.verification.tone));
        if (account.next_up) name.appendChild(chip('next up', 'ok'));
        if (!account.signed_in) name.appendChild(chip('not signed in', 'warn'));
        if (account.enabled === false) name.appendChild(chip('disabled', 'warn'));
        row.appendChild(name);

        renderQuota(row, account.quota);

        var meta = [];
        if (account.email) meta.push(account.email);
        if (account.plan) meta.push('plan ' + account.plan);
        if (account.kind === 'profile') {
            var checked = relTime(account.last_verified_at);
            meta.push(checked ? ('checked ' + checked) : 'no check time reported');
        } else {
            meta.push('vendor CLI login');
        }
        if (facets.accounts !== 'ok') {
            meta.push('account facet ' + facetWord(facets.accounts) + ' — row is last known');
        }
        row.appendChild(el('p', 'meta', meta.join(' · ')));
        parent.appendChild(row);
    }

    function renderGroup(parent, group, facets, filter) {
        var filteredAccounts = group.accounts.filter(function (acc) {
            if (filter === 'active') return acc.signed_in && acc.enabled !== false;
            if (filter === 'alerts') return isAlertAccount(acc);
            return true;
        });

        if (!filteredAccounts.length && filter !== 'all') return;

        var card = el('div', 'card');
        var title = el('h3');
        title.appendChild(el('span', null, group.family_label));
        if (group.harness_status && group.harness_status !== 'ok') {
            title.appendChild(chip('harness ' + group.harness_status, 'warn'));
        }
        if (group.harness_enabled === false) title.appendChild(chip('harness disabled', 'warn'));
        if (!group.catalog_known) {
            title.appendChild(chip('catalog ' + facetWord(facets.catalog), facetTone(facets.catalog)));
        }
        card.appendChild(title);

        var signed = group.accounts_signed_in;
        var sub = (signed > 1) ? (signed + ' accounts signed in — rotating.') :
                  (signed === 1) ? '1 account signed in.' : 'No account signed in.';
        card.appendChild(el('p', 'sub', sub));

        filteredAccounts.forEach(function (account) {
            renderAccount(card, account, facets);
        });

        parent.appendChild(card);
    }

    function render(view, staleView) {
        currentView = view;
        root.textContent = '';
        var style = el('style');
        style.textContent = STYLE;
        root.appendChild(style);

        var facets = view.facets || {};
        var groups = view.groups || [];

        var totalAccounts = 0;
        var activeAccounts = 0;
        var alertAccounts = 0;
        groups.forEach(function (g) {
            (g.accounts || []).forEach(function (a) {
                totalAccounts++;
                if (a.signed_in && a.enabled !== false) activeAccounts++;
                if (isAlertAccount(a)) alertAccounts++;
            });
        });

        var hdr = el('div', 'hdr');
        hdr.appendChild(el('h2', null, 'Claudexor quotas'));
        ['catalog', 'accounts', 'quota'].forEach(function (f) {
            var state = facets[f] || 'indeterminate';
            hdr.appendChild(chip(f + ': ' + facetWord(state), facetTone(state)));
        });
        var daemon = view.daemon || {};
        if (daemon.state) {
            hdr.appendChild(chip('daemon ' + daemon.state + (daemon.engine_version ? ' · ' + daemon.engine_version : ''), daemon.state === 'running' ? 'ok' : 'warn'));
        }
        hdr.appendChild(el('span', 'spacer'));
        var refreshBtn = el('button', null, 'Refresh');
        refreshBtn.disabled = inFlight;
        refreshBtn.addEventListener('click', function () {
            if (inFlight) return;
            refreshBtn.disabled = true;
            refreshBtn.textContent = 'Refreshing…';
            load();
        });
        hdr.appendChild(refreshBtn);
        root.appendChild(hdr);

        var tabs = el('div', 'tabs');
        [
            { id: 'all', label: 'All', count: totalAccounts },
            { id: 'active', label: 'Active', count: activeAccounts },
            { id: 'alerts', label: 'Alerts', count: alertAccounts }
        ].forEach(function (tab) {
            var btn = el('button', 'tab-btn' + (currentFilter === tab.id ? ' active' : ''));
            btn.textContent = tab.label;
            var badge = el('span', 'badge', tab.count);
            btn.appendChild(badge);
            btn.addEventListener('click', function () {
                currentFilter = tab.id;
                render(currentView, staleView);
            });
            tabs.appendChild(btn);
        });
        root.appendChild(tabs);

        if (staleView) {
            root.appendChild(el('div', 'banner bad', 'Reading could not be refreshed (' + staleView + '). Cached data from ' + (relTime(new Date(lastGoodAt).toISOString()) || 'earlier') + ' is shown.'));
        }
        if (view.transport_error && !staleView) {
            root.appendChild(el('div', 'banner bad', 'Endpoint unreachable: ' + view.transport_error + '. No quota claims made.'));
        }
        if (view.facet_note) {
            root.appendChild(el('div', 'banner', 'Facets unavailable: ' + view.facet_note + '. Shown as unread, not zero.'));
        }

        groups.forEach(function (group) {
            renderGroup(root, group, facets, currentFilter);
        });

        if (!groups.length || !root.querySelector('.card')) {
            root.appendChild(el('p', 'empty', 'No accounts match the "' + currentFilter + '" filter.'));
        }
    }

    function updateTickers() {
        if (stopped || !root) return;
        var tickers = root.querySelectorAll('.ticker[data-target]');
        for (var i = 0; i < tickers.length; i++) {
            var node = tickers[i];
            var target = node.getAttribute('data-target');
            if (target) {
                node.textContent = formatCountdown(target);
            }
        }
    }

    function load() {
        if (inFlight) return;
        inFlight = true;
        var mine = ++generation;
        window.fetch(ROUTE, { method: 'GET' }).then(function (response) {
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
            render(view, '');
        }).catch(function (err) {
            inFlight = false;
            if (mine !== generation || stopped) return;
            var why = (err && err.message) ? err.message : 'bridge error';
            if (lastGood) { render(lastGood, why); } else {
                render({ facets: {}, groups: [], daemon: {}, transport_error: why }, '');
            }
        });
    }

    function stop() {
        stopped = true;
        generation++;
        if (dataTimer !== null) { window.clearInterval(dataTimer); dataTimer = null; }
        if (tickTimer !== null) { window.clearInterval(tickTimer); tickTimer = null; }
    }

    window.addEventListener('pagehide', stop);
    window.addEventListener('unload', stop);
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !stopped) {
            load();
        }
    });

    render({ facets: {}, groups: [], daemon: {} }, '');
    load();
    dataTimer = window.setInterval(function () {
        if (document.visibilityState === 'visible') load();
    }, REFRESH_MS);
    tickTimer = window.setInterval(updateTickers, TICK_MS);
})();
