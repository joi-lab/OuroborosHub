"""Full model-scope identity survives bounded labels in the account widget."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import quota_summary as qs
import test_quotas
from test_reserve import (NOW, WEEK, at, constraint, payload, profile, snap, summary_of,
                          _node, _route_and_tool)


def test_a_newline_inside_a_name_is_not_a_second_name(tmp_path, monkeypatch):
    joined, split = ("a\nb",), ("a", "b")
    assert qs._scope_hash(joined) != qs._scope_hash(split)
    assert qs.group_key("claude", "p", WEEK, joined) != qs.group_key("claude", "p", WEEK, split)
    # Every scope without a newline in a name — every one recorded so far —
    # keeps the key its history is stored under.
    for models in (("fable",), ("best", "claude-fable-5", "claude-fable-5-1", "fable"),
                   tuple(f"m{i:02d}" for i in range(30)), ("x" * 200,)):
        assert qs._scope_hash(models) == hashlib.sha256("\n".join(models).encode()).hexdigest()[:10]
    # Nor can the new serialization meet an old one.
    assert qs._scope_hash(("[\"a\\nb\"]",)) != qs._scope_hash(joined)
    # Through a payload: two limits, never summed; a cooldown on one scope
    # does not hold the other.
    data = payload([
        snap("claude", "x", [constraint("scoped", .3, reset=3600, models=["a\nb"])]),
        snap("claude", "y", [constraint("scoped", .4, reset=3600, models=["a", "b"]),
                              {"id": "cooldown", "used_ratio": None, "window_seconds": None,
                               "cooldown_until": at(1800), "applies_to_models": ["a\nb"]}]),
    ], [profile("claude", "x"), profile("claude", "y")], harnesses=("claude",))
    summary, _ = summary_of(data)
    scoped = [g for g in summary["groups"] if "|scoped|" in g["key"]]
    assert sorted(g["measured"]["windows"] for g in scoped) == [.6, .7]
    assert [g["restrictions"] for g in scoped] == [{}, {}]
    route, _tool = _route_and_tool(tmp_path, monkeypatch, data)
    quotas = {a["subject_id"]: a["quota"] for g in route["groups"] for a in g["accounts"]}
    keys = {sid: next(c["scope_key"] for c in q["constraints"] if c["id"] == "scoped")
            for sid, q in quotas.items()}
    assert keys["x"] != keys["y"]
    assert quotas["y"]["cooldowns"][0]["scope_key"] == keys["x"]


def test_model_cooldown_does_not_hold_another_pool(tmp_path, monkeypatch):
    first, second = "m" * 80 + "a", "m" * 80 + "b"
    data = payload([
        snap("claude", "a", [constraint("scoped", .3, reset=3600,
                                        models=[first], cooldown=at(1800))],
             source="old", observed=-600),
        snap("claude", "a", [constraint("scoped", .4, reset=3600, models=[first]),
                              constraint("scoped", .2, reset=3600, models=[second]),
                              constraint("shared", .1, reset=3600)],
             source="new", observed=-60),
    ], [profile("claude", "a")], harnesses=("claude",))
    route, _ = _route_and_tool(tmp_path, monkeypatch, data)
    quota = route["groups"][0]["accounts"][0]["quota"]
    scoped = [c for c in quota["constraints"] if c["id"] == "scoped"]
    assert len(scoped) == 2
    assert scoped[0]["scope_key"] != scoped[1]["scope_key"]
    assert quota["cooldowns"][0]["scope_key"] == scoped[0]["scope_key"]
    monkeypatch.undo()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    js = r"""
    (async () => {
      Date.now = () => Number(process.env.FIXED_NOW);
      const env = await boot(JSON.parse(process.env.ROUTE));
      const meterOf = (node) => classes(node, 'meter')[0];
      byFocus(env.root, 'account-details').listeners.click[0]({ stopPropagation() {} });
      const tiles = classes(env.root, 'quota-tile').filter((n) => n.title === 'scoped');
      assert.equal(tiles.length, 2);
      assert.match(meterOf(tiles[0]).className, /\brestricted\b/);
      assert.doesNotMatch(meterOf(tiles[1]).className, /\brestricted\b/);
      const shared = classes(env.root, 'quota-tile').find((n) => n.title === 'shared');
      assert.ok(shared);
      assert.doesNotMatch(meterOf(shared).className, /\brestricted\b/);
      byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
      const opt = byFocus(env.root, 'opt:claude:a');
      const wins = classes(opt, 'acct-win').filter((w) => w.title.startsWith('scoped —'));
      assert.equal(wins.length, 2);
      assert.match(meterOf(wins[0]).className, /\brestricted\b/);
      assert.doesNotMatch(meterOf(wins[1]).className, /\brestricted\b/);
      assert.match(opt.textContent, /model cooldown/);
      assert.doesNotMatch(opt.textContent, /account cooldown/);
    })().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
    """
    node = _node()
    assert node is not None
    widget = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", harness + js], cwd=widget.parent,
        env={**os.environ, "WIDGET_PATH": str(widget), "ROUTE": json.dumps(route),
             "FIXED_NOW": str(int(NOW * 1000))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_two_scopes_that_print_one_name_are_two_pools(tmp_path, monkeypatch):
    """Two model scopes whose shown names are the same — the first 24 names
    shared, only the 25th different, so both print "M00 +24" — are two pools
    in the account row, each coloured only by the hold on its own scope."""
    wide = [f"m{i:02d}" for i in range(24)]
    held, free = wide + ["m99-x"], wide + ["m99-y"]
    cooldown = {"id": "cooldown", "used_ratio": None, "window_seconds": None,
                "cooldown_until": at(1800), "applies_to_models": held}
    data = payload([
        # The cooldown comes from another (older) reading than the windows
        # drawn: only the scope identity can tie it to its window.
        snap("claude", "a", [cooldown], source="old", observed=-600),
        snap("claude", "a", [constraint("wide", .3, reset=3600, models=held, label="7 day wide"),
                              constraint("wide", .2, reset=3600, models=free, label="7 day wide"),
                              constraint("shared", .1, reset=3600)],
             source="new", observed=-60),
    ], [profile("claude", "a")], harnesses=("claude",))
    route, _ = _route_and_tool(tmp_path, monkeypatch, data)
    quota = route["groups"][0]["accounts"][0]["quota"]
    keys = {c["scope_key"] for c in quota["constraints"] if c["id"] == "wide"}
    assert len(keys) == 2 and quota["cooldowns"][0]["scope_key"] in keys
    held_key = quota["cooldowns"][0]["scope_key"]
    route["prefs"] = {"density": "detailed", "models": {}, "fold": {}}
    monkeypatch.undo()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    js = r"""
    (async () => {
      Date.now = () => Number(process.env.FIXED_NOW);
      const env = await boot(JSON.parse(process.env.ROUTE));
      const meterOf = (node) => classes(node, 'meter')[0];
      byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
      const opt = byFocus(env.root, 'opt:claude:a');
      const pools = classes(opt, 'acct-grp').filter((g) => classes(g, 'acct-pool').length);
      assert.equal(pools.length, 2, pools.map((g) => g.textContent).join(' | '));
      const chips = pools.map((g) => classes(g, 'acct-pool')[0]);
      assert.deepEqual(chips.map((c) => c.textContent), ['M00 +24', 'M00 +24']);
      // The chips print one name; their titles keep the two scopes apart.
      const heldPool = pools.find((g) => /m99-x/.test(classes(g, 'acct-pool')[0].title));
      const freePool = pools.find((g) => /m99-y/.test(classes(g, 'acct-pool')[0].title));
      assert.ok(heldPool && freePool && heldPool !== freePool);
      assert.match(classes(heldPool, 'acct-pool')[0].className, /\bheld\b/);
      assert.doesNotMatch(classes(freePool, 'acct-pool')[0].className, /\bheld\b|\bexhausted\b/);
      assert.match(meterOf(heldPool).className, /\brestricted\b/);
      assert.doesNotMatch(meterOf(freePool).className, /\brestricted\b/);
      // The lines under the bars: the held scope (its window, and the bare
      // cooldown the engine lists beside it) cools down in amber; the twin
      // and the shared window stay available, neutral.
      const lines = classes(opt, 'acct-rl');
      const cooling = lines.filter((l) => /cooling down/.test(l.textContent));
      assert.ok(cooling.length >= 1);
      cooling.forEach((l) => {
        assert.match(classes(l, 'acct-pool')[0].title, /m99-x/);
        assert.match(classes(l, 'acct-rl-tag')[0].className, /\bwarn\b/);
      });
      const open = lines.filter((l) => /available$/.test(l.textContent));
      assert.equal(open.length, 2);
      assert.ok(open.every((l) => /\bok\b/.test(classes(l, 'acct-rl-tag')[0].className)));
      assert.ok(open.every((l) => classes(l, 'acct-pool').every((c) => /m99-y/.test(c.title)
        && !/\bheld\b/.test(c.className))));
      // Details: one tile held, its twin not.
      byFocus(env.root, 'account-details').listeners.click[0]({ stopPropagation() {} });
      const tiles = classes(env.root, 'quota-tile').filter((n) => n.title === '7 day wide');
      assert.equal(tiles.length, 2);
      assert.equal(tiles.filter((t) => /\brestricted\b/.test(meterOf(t).className)).length, 1);
    })().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
    """
    node = _node()
    assert node is not None
    widget = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", harness + js], cwd=widget.parent,
        env={**os.environ, "WIDGET_PATH": str(widget), "ROUTE": json.dumps(route),
             "FIXED_NOW": str(int(NOW * 1000))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert held_key != "-"
