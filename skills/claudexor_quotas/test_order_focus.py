"""0.6.3 repairs: an availability cooldown whose end has passed, keyboard focus
across Refresh, and the reader's latest display choice.

Each runs through the real code: the projector and routes for the cooldown,
the prefs route for the order of saves, and widget.js itself — in the fake DOM
of the other widget tests, taught here what a browser does with focus on a
node a redraw removes (the stub alone keeps focus on the detached node, which
hides the defect), and, where Playwright has a Chrome at hand, in a real one.
"""
import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import plugin
import quota_summary as qs
import test_quotas
from test_r3 import RunsBackwards, _Body, _prefs_route
from test_reserve import (NOW, _node, _route_and_tool, _widget_fixture, at, constraint, group,
                          payload, profile, snap)

WIDGET = Path(os.environ.get("ORDER_FOCUS_WIDGET_PATH", Path(__file__).with_name("widget.js"))).resolve()


# ---------------------------------------------------------------------------
# A fresh availability cooldown holds by the constraint's clock.

def _held_by_availability(sid, resets_at="absent", *, fresh=True):
    row = snap("claude", sid, [constraint("seven_day", .20, reset=3 * 86400, label="7 day")],
               fresh=fresh, availability="cooldown")
    if resets_at != "absent":
        row["availability"]["resets_at"] = resets_at
    return row


@pytest.mark.parametrize("resets_at, holds, note", [
    (at(600), True, ""),
    ("absent", True, "not_reported"),
    (None, True, "not_reported"),
    ("", True, "not_reported"),
    ("not-a-date", True, "unreadable"),
    (at(-60), False, None),
    (at(0), False, None),  # ends now: not ahead any more
])
def test_a_fresh_availability_cooldown_holds_while_its_end_is_ahead_or_unknown(resets_at, holds, note):
    found = qs.cooldowns_of(_held_by_availability("a", resets_at), "claude", "a", NOW)
    assert [(c.kind, c.scope, c.fresh) for c in found] == ([("availability", "-", True)] if holds else [])
    if holds:
        assert plugin._cooldown_view(found[0], NOW)["until_note"] == note


def test_a_stale_availability_cooldown_still_holds_nothing():
    for resets_at in (at(600), "absent", at(-60)):
        assert qs.cooldowns_of(_held_by_availability("a", resets_at, fresh=False), "claude", "a", NOW) == []


def test_a_passed_availability_cooldown_leaves_the_reserve_and_the_account_view(tmp_path, monkeypatch):
    snaps = [
        # A fresh reading carried past the end the engine reported.
        _held_by_availability("ended", at(-60)),
        _held_by_availability("live", at(1800)),
        _held_by_availability("unknown"),
        # A passed availability end does not wipe a constraint's own live cooldown.
        dict(_held_by_availability("both", at(-60)),
             constraints=[constraint("seven_day", .20, reset=3 * 86400, label="7 day",
                                     cooldown=at(900))]),
    ]
    sids = ("ended", "live", "unknown", "both")
    data = payload(snaps, [profile("claude", sid) for sid in sids], harnesses=("claude",))
    view, tool = _route_and_tool(tmp_path, monkeypatch, data)
    accounts = {a["subject_id"]: a["quota"] for g in view["groups"] for a in g["accounts"]}

    ended = accounts["ended"]
    assert (ended["state"], ended["label"], ended["cooldowns"]) == ("ok", "20% used", [])
    assert ended["availability"] == "cooldown"  # the engine's word, as reported
    live, unknown, both = accounts["live"], accounts["unknown"], accounts["both"]
    assert (live["state"], live["cooling_until"]) == ("cooling", at(1800))
    assert (unknown["state"], unknown["cooling_until"]) == ("cooling", "")
    assert [(c["kind"], c["until_note"]) for c in unknown["cooldowns"]] == [("availability", "not_reported")]
    assert (both["state"], both["cooling_until"]) == ("cooling", at(900))
    assert [c["kind"] for c in both["cooldowns"]] == ["constraint"]

    week = group(view["reserve"]["summary"], "|seven_day|")
    assert week["measured"]["accounts"] == 4 and week["measured"]["windows"] == 3.2
    # Only the three that hold now are held back; the passed one is reserve.
    assert week["restrictions"] == {"cooling": {"accounts": 3, "windows": 2.4}}
    assert week["unrestricted_windows"] == 0.8
    rows = {row["key"]: row for row in tool["groups"]}
    assert rows[week["key"]]["restrictions"] == week["restrictions"]


# ---------------------------------------------------------------------------
# The route keeps the reader's latest choice, not the latest arrival.

def _stored(tmp_path):
    return json.loads((tmp_path / "prefs.json").read_text())


def test_prefs_order_reads_a_frame_name_and_a_positive_number_only():
    assert plugin.prefs_order({"frame": "f", "seq": 3}) == ("f", 3)
    for junk in (None, [], {}, {"frame": "f"}, {"seq": 1}, {"frame": "", "seq": 1},
                 {"frame": "x" * (plugin.MAX_PREFS_FRAME + 1), "seq": 1}, {"frame": 7, "seq": 1},
                 {"frame": "f", "seq": 0}, {"frame": "f", "seq": -1}, {"frame": "f", "seq": True},
                 {"frame": "f", "seq": "2"}, {"frame": "f", "seq": 2.0}):
        assert plugin.prefs_order(junk) is None, junk


def test_a_save_overtaken_by_a_newer_choice_of_its_frame_is_not_written(tmp_path):
    _host, route = _prefs_route(tmp_path)
    newer = asyncio.run(route(_Body({"density": "detailed", "frame": "f1", "seq": 2})))
    assert newer == {"prefs": {**plugin.clean_prefs(None), "density": "detailed"}, "error": ""}
    # The older choice reaches the skill last: it answers with what is kept.
    older = asyncio.run(route(_Body({"density": "compact", "frame": "f1", "seq": 1})))
    assert older == newer
    # A replay of the newest is not a newer choice either.
    again = asyncio.run(route(_Body({"density": "compact", "frame": "f1", "seq": 2})))
    assert again == newer
    stored = _stored(tmp_path)
    assert stored["density"] == "detailed" and "frame" not in stored and "seq" not in stored
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prefs.json"]

    # Another load of the frame (a reload) numbers from one again.
    reloaded = asyncio.run(route(_Body({"density": "normal", "frame": "f2", "seq": 1})))
    assert reloaded["prefs"]["density"] == "normal" and _stored(tmp_path)["density"] == "normal"
    # And a save with no order at all keeps the later arrival, as before.
    asyncio.run(route(_Body({"density": "compact"})))
    assert _stored(tmp_path)["density"] == "compact"
    assert asyncio.run(route(_Body({"density": "detailed", "frame": "f1", "seq": 3})))["error"] == ""
    assert _stored(tmp_path)["density"] == "detailed"


def test_ordered_saves_that_cross_on_the_way_to_the_file_keep_the_newest(tmp_path):
    _host, route = _prefs_route(tmp_path)

    async def scenario():
        loop = asyncio.get_running_loop()
        executor = RunsBackwards()
        loop.set_default_executor(executor)
        first = asyncio.create_task(route(_Body({"density": "compact", "frame": "f", "seq": 1})))
        second = asyncio.create_task(route(_Body({"density": "detailed", "frame": "f", "seq": 2})))
        deadline = time.monotonic() + 5
        while len(executor.jobs) < 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(.002)
        worker = threading.Thread(target=executor.run_backwards)
        worker.start()
        answers = await asyncio.wait_for(asyncio.gather(first, second), 5)
        worker.join(5)
        return answers

    first, second = asyncio.run(scenario())
    assert second == {"prefs": {**plugin.clean_prefs(None), "density": "detailed"}, "error": ""}
    assert first == second
    assert _stored(tmp_path)["density"] == "detailed"


def test_a_failed_newest_save_is_reported_and_its_overtaken_choice_stays_unwritten(tmp_path, monkeypatch):
    host, route = _prefs_route(tmp_path)
    assert asyncio.run(route(_Body({"density": "normal", "frame": "f", "seq": 1})))["error"] == ""
    replace = os.replace

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(plugin.os, "replace", full_disk)
    failed = asyncio.run(route(_Body({"density": "detailed", "frame": "f", "seq": 3})))
    assert "No space left on device" in failed["error"]
    assert any("prefs write failed" in message for _level, message in host.logs)
    monkeypatch.setattr(plugin.os, "replace", replace)
    # The choice before it, arriving late, is still not the reader's latest.
    late = asyncio.run(route(_Body({"density": "compact", "frame": "f", "seq": 2})))
    assert late == {"prefs": {**plugin.clean_prefs(None), "density": "normal"}, "error": ""}
    assert _stored(tmp_path)["density"] == "normal"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["prefs.json"]
    # The reader's next choice is written.
    assert asyncio.run(route(_Body({"density": "detailed", "frame": "f", "seq": 4})))["error"] == ""
    assert _stored(tmp_path)["density"] == "detailed"


# ---------------------------------------------------------------------------
# widget.js in the fake DOM.

NODE_ORDER_FOCUS = r"""
// A browser drops focus to the body when the focused node leaves the
// document, and says whether a node is still in it. The stub did neither: the
// old, detached Refresh button kept "focus", so a lost focus looked kept.
function inside(node, ancestor) {
  for (let n = node; n; n = n.parentNode) if (n === ancestor) return true;
  return false;
}
const textProp = Object.getOwnPropertyDescriptor(Element.prototype, 'textContent');
Object.defineProperty(Element.prototype, 'textContent', {
  get: textProp.get,
  set(value) {
    const doc = this.ownerDocument;
    const lost = doc.activeElement && doc.activeElement !== this && inside(doc.activeElement, this);
    textProp.set.call(this, value);
    if (lost) doc.activeElement = doc.body;
  },
});
Object.defineProperty(Element.prototype, 'isConnected', {
  get() { return inside(this, this.ownerDocument.body); },
});

function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'missing control ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
function deferredFetch(env) {
  const pending = [];
  env.window.fetch = (url, options = {}) => {
    const call = { url, method: options.method || 'GET', body: options.body };
    env.calls.push(call);
    return new Promise((resolve, reject) => pending.push({ call, resolve, reject }));
  };
  return pending;
}
const saves = (pending) => pending.filter((p) => /\/prefs$/.test(p.call.url));
const sent = (p) => JSON.parse(p.call.body);
const kept = (p) => { const b = sent(p); return { density: b.density, models: b.models, fold: b.fold }; };
const chosen = (env) => classes(env.root, 'density-opt')
  .filter((n) => /\bactive\b/.test(n.className)).map((n) => n.getAttribute('data-focus'));
const warned = (env) => /Display choice was not saved/.test(env.root.textContent);
const active = (env) => env.document.activeElement;

(async () => {
  const fx = JSON.parse(process.env.ORDER_FOCUS_FIXTURE);
  const mode = process.env.ORDER_FOCUS_CASE;
  const withPrefs = (density) => Object.assign(JSON.parse(JSON.stringify(fx.view)),
    { prefs: { density, models: {}, fold: {} } });

  async function twoSaves() {
    const env = await boot(withPrefs('normal'));
    const pending = deferredFetch(env);
    click(env, 'settings'); click(env, 'density:compact'); click(env, 'density:detailed');
    await settle();
    const [older, newer] = saves(pending);
    assert.ok(older && newer && saves(pending).length === 2);
    assert.deepEqual([sent(older).density, sent(newer).density], ['compact', 'detailed']);
    assert.deepEqual([sent(older).seq, sent(newer).seq], [1, 2]);
    assert.equal(sent(older).frame, sent(newer).frame);
    assert.ok(sent(older).frame.length > 0 && sent(older).frame.length <= 64);
    return { env, pending, older, newer };
  }

  if (mode === 'prefs_reversed') {
    const { env, older, newer } = await twoSaves();
    newer.resolve(response({ prefs: kept(newer), error: '' })); await settle();
    assert.deepEqual(chosen(env), ['density:detailed']);
    older.resolve(response({ prefs: kept(older), error: '' })); await settle();
    assert.deepEqual(chosen(env), ['density:detailed'], 'the older answer came last and won');
    assert.ok(!warned(env));
    // With nothing in the air, a reading's choice is drawn again.
    env.window.fetch = () => Promise.resolve(response(withPrefs('compact')));
    env.interval()(); await settle();
    assert.deepEqual(chosen(env), ['density:compact']);
  } else if (mode === 'prefs_older_fails_late') {
    const { env, older, newer } = await twoSaves();
    newer.resolve(response({ prefs: kept(newer), error: '' })); await settle();
    older.reject(new Error('bridge closed')); await settle();
    // The newest, a full copy of every choice, is kept: nothing reverts.
    assert.deepEqual(chosen(env), ['density:detailed']);
    assert.ok(!warned(env));
  } else if (mode === 'prefs_older_fails_first') {
    const { env, older, newer } = await twoSaves();
    older.reject(new Error('bridge closed')); await settle();
    assert.ok(warned(env), 'a failure is said while nothing newer has answered');
    assert.match(env.root.textContent, /bridge closed/);
    assert.deepEqual(chosen(env), ['density:detailed']);
    newer.resolve(response({ prefs: kept(newer), error: '' })); await settle();
    assert.ok(!warned(env));
    assert.deepEqual(chosen(env), ['density:detailed']);
  } else if (mode === 'prefs_newest_fails') {
    const { env, older, newer } = await twoSaves();
    newer.resolve(response({ prefs: kept(newer), error: 'OSError: disk full' })); await settle();
    assert.ok(warned(env)); assert.match(env.root.textContent, /disk full/);
    older.resolve(response({ prefs: kept(older), error: '' })); await settle();
    // The older success neither hides the newest failure nor brings its choice back.
    assert.ok(warned(env)); assert.match(env.root.textContent, /disk full/);
    assert.deepEqual(chosen(env), ['density:detailed']);
  } else if (mode === 'prefs_reading_asked_before') {
    const env = await boot(withPrefs('normal'));
    const pending = deferredFetch(env);
    env.interval()(); await settle();
    const reading = pending.find((p) => p.call.method === 'GET');
    assert.ok(reading, 'the timed reading is in the air');
    click(env, 'settings'); click(env, 'density:compact'); await settle();
    const [save] = saves(pending);
    save.resolve(response({ prefs: kept(save), error: '' })); await settle();
    // Read before the choice was made, answered after it was saved.
    reading.resolve(response(withPrefs('normal'))); await settle();
    assert.deepEqual(chosen(env), ['density:compact'], 'a reading from before the choice reverted it');
    // A reading asked after it is drawn, whatever it says.
    env.interval()(); await settle();
    pending.filter((p) => p.call.method === 'GET').at(-1).resolve(response(withPrefs('detailed')));
    await settle();
    assert.deepEqual(chosen(env), ['density:detailed']);
  } else if (mode.startsWith('focus_')) {
    // Nodes are compared with ===: a failed deep assert on two of them would
    // walk the whole cyclic tree to print a diff.
    const env = await boot(fx.view);
    const pending = deferredFetch(env);
    const refresh = () => byFocus(env.root, 'refresh');
    if (mode !== 'focus_never_on_refresh') {
      refresh().focus();
      assert.ok(active(env) === refresh(), 'Refresh did not take focus');
    }
    if (mode === 'focus_after_poll') env.interval()(); else click(env, 'refresh');
    await settle();
    assert.equal(refresh().disabled, true);
    assert.ok(!(active(env) || {}).isConnected || active(env) === env.document.body,
      'focus rests on a disconnected node');
    if (mode !== 'focus_never_on_refresh') {
      // The disabled redraw dropped it, as a browser does.
      assert.ok(active(env) === env.document.body, 'focus did not fall to the body');
      // Another redraw while the read is in the air keeps the place.
      click(env, 'account-details'); await settle();
      assert.ok(active(env) === env.document.body, 'a redraw in flight moved focus');
    }
    if (mode === 'focus_moved_on') byFocus(env.root, 'settings').focus();
    if (mode === 'focus_left_frame') env.document.hasFocus = () => false;
    const job = pending.at(-1);
    if (mode === 'focus_failure') job.reject(new Error('host refused'));
    else if (job.call.method === 'GET') job.resolve(response(fx.view));
    else job.resolve(response({ ok: true, quota_updates: [] }));
    await settle();
    const now = active(env);
    assert.equal(refresh().disabled, false);
    if (mode === 'focus_moved_on') {
      assert.equal(now.getAttribute('data-focus'), 'settings');
      assert.equal(now.isConnected, true);
    } else if (mode === 'focus_left_frame' || mode === 'focus_never_on_refresh') {
      assert.ok(now === null || now === env.document.body, 'focus was pulled onto Refresh');
    } else {
      assert.ok(now === refresh(), 'keyboard focus did not come back to Refresh');
      assert.equal(now.isConnected, true);
      if (mode === 'focus_failure') assert.match(env.root.textContent, /whether it ran is unknown/);
    }
    env.disposeHooks[0]();
  } else {
    throw new Error('unknown case ' + mode);
  }
})().catch((error) => { console.error(error && error.stack ? error.stack : error); process.exitCode = 1; });
"""


@pytest.fixture(scope="module")
def widget_view(tmp_path_factory):
    return _widget_fixture(tmp_path_factory.mktemp("fixture"))["view"]


@pytest.mark.parametrize("case", [
    "prefs_reversed", "prefs_older_fails_late", "prefs_older_fails_first", "prefs_newest_fails",
    "prefs_reading_asked_before",
    "focus_after_refresh", "focus_after_poll", "focus_failure", "focus_moved_on",
    "focus_left_frame", "focus_never_on_refresh",
])
def test_widget_order_and_focus(widget_view, case):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    harness = harness.replace("    root: made.root,", "    window,\n    root: made.root,")
    assert "    window,\n    root: made.root," in harness
    result = subprocess.run(
        [str(node), "-e", harness + NODE_ORDER_FOCUS], cwd=WIDGET.parent,
        env={**os.environ, "WIDGET_PATH": str(WIDGET), "ORDER_FOCUS_CASE": case,
             "ORDER_FOCUS_FIXTURE": json.dumps({"view": widget_view})},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# widget.js in a real Chrome: focus on a removed node really does fall to the
# body, and a keyboard Refresh gets it back when the read ends.

REAL_PAGE_STUB = r"""
(view) => {
  window.__pending = [];
  window.fetch = (url, options = {}) => {
    const method = (options && options.method) || 'GET';
    if (method === 'GET') {
      return Promise.resolve({ ok: true, status: 200, text: async () => JSON.stringify(view) });
    }
    return new Promise((resolve, reject) => window.__pending.push({ url, method, resolve, reject }));
  };
  window.__answer = (ok) => {
    const job = window.__pending.shift();
    if (ok) job.resolve({ ok: true, status: 200, text: async () => JSON.stringify({ ok: true, quota_updates: [] }) });
    else job.reject(new Error('host refused'));
    return job.url;
  };
}
"""

FOCUS_STATE = """() => {
  const a = document.activeElement;
  const r = document.querySelector('[data-focus="refresh"]');
  return { key: a && a.getAttribute ? a.getAttribute('data-focus') : null,
           body: a === document.body, connected: !!(a && a.isConnected), same: a === r,
           disabled: !!(r && r.disabled), text: document.getElementById('root').textContent };
}"""


def _launch(playwright):
    errors = []
    for options in ({}, {"channel": "chrome"}):
        try:
            return playwright.chromium.launch(**options)
        except Exception as exc:  # no browser build for this Playwright here
            errors.append(f"{options or 'bundled'}: {type(exc).__name__}")
    pytest.skip("no Chromium or Chrome for Playwright on this machine (" + "; ".join(errors) + ")")


def test_real_chrome_keyboard_refresh_keeps_focus(widget_view):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = _launch(playwright)
        try:
            page = browser.new_page()
            page.set_content('<!doctype html><html><head></head><body><div id="root"></div></body></html>')
            page.evaluate(REAL_PAGE_STUB, widget_view)
            page.add_script_tag(content=WIDGET.read_text(encoding="utf-8"))
            page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            assert page.evaluate("document.hasFocus()") is True

            for ok in (True, False):
                page.focus('[data-focus="refresh"]')
                page.keyboard.press("Enter")
                page.wait_for_function("window.__pending.length === 1")
                during = page.evaluate(FOCUS_STATE)
                # The redraw removed the focused button and drew it disabled.
                assert during["disabled"] and during["body"] and during["key"] is None, during
                assert page.evaluate("window.__answer(%s)" % ("true" if ok else "false")).endswith("/refresh")
                page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
                after = page.evaluate(FOCUS_STATE)
                assert (after["key"], after["same"], after["connected"]) == ("refresh", True, True), after
                if not ok:
                    assert "whether it ran is unknown" in after["text"]

            # A keyboard reader who moved on keeps their place.
            page.focus('[data-focus="refresh"]')
            page.keyboard.press("Enter")
            page.wait_for_function("window.__pending.length === 1")
            page.focus('[data-focus="settings"]')
            page.evaluate("window.__answer(true)")
            page.wait_for_selector('[data-focus="refresh"]:not([disabled])')
            moved = page.evaluate(FOCUS_STATE)
            assert (moved["key"], moved["connected"]) == ("settings", True), moved
        finally:
            browser.close()
