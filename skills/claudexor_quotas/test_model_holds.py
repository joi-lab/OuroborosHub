"""Model-scoped exhaustions, model holds and the two colours of "out".

``availability.model_scoped_exhaustions`` is canonical engine evidence
(``{constraint_id, applies_to_models, resets_at}``). It is read apart from
cooldowns and from the account's state, holds only its own whole scope, and
only while its reported reset is still ahead; a missing, unreadable or passed
reset is disclosed, never made a live hold. The widget draws a measured share
at its limit in red and a cooldown or a reported model limit in amber.

Everything is synthetic. RECORDED_SHAPE_ROWS keep the field layout of two
recorded Claude OAuth status snapshots (a stale and a fresh one, each with a
reported Fable exhaustion); their identities, times and shares are invented.
"""
import json
import os
import subprocess
from pathlib import Path

import plugin
import quota_summary as qs
import test_quotas
from test_reserve import NOW, at, constraint, group, payload, profile, snap, summary_of, _node, _route_and_tool

FABLE = ["fable", "claude-fable-5-1", "claude-fable-5", "best"]
OPUS = ["opus", "claude-opus-5-5"]
WIDE = [f"m{i:02d}" for i in range(24)]
DAY = 86400


def real_iso(offset):
    """The engine's own spelling: microseconds and an explicit +00:00."""
    return qs.iso(NOW + offset).replace("Z", ".000500+00:00")


def exhaustion(reset, models=FABLE, cid="weekly_scoped:Fable"):
    return {"constraint_id": cid, "applies_to_models": models, "resets_at": reset}


def oauth(sid, *, fable, exhaustions=(), fresh=True, observed=-60.0, source="claude_oauth_usage",
          extra=()):
    """One snapshot in the shape the engine reports for a Claude account."""
    return {
        "subject": {"harness": "claude", "credential_route": "vendor_native", "plan_label": "max",
                    "subject_id": sid},
        "source": source, "observed_at": at(observed), "freshness": "fresh" if fresh else "stale",
        "availability": {"state": "available", "blocking_constraints": [], "resets_at": None,
                         "model_scoped_exhaustions": list(exhaustions)},
        "constraints": [
            {"id": "five_hour", "label": "5 hour", "used_ratio": 0.1, "window_seconds": 18000,
             "resets_at": real_iso(3600), "cooldown_until": None},
            {"id": "seven_day", "label": "7 day", "used_ratio": 0.7, "window_seconds": 604800,
             "resets_at": real_iso(DAY), "cooldown_until": None},
            {"id": "weekly_scoped:Fable", "label": "7 day (Fable)", "applies_to_models": FABLE,
             "used_ratio": fable, "window_seconds": 604800, "resets_at": real_iso(DAY),
             "cooldown_until": None},
        ] + list(extra),
    }


def _bare_cooldown(offset, models=None):
    return {"id": "cooldown", "label": "Cooldown", "used_ratio": None, "window_seconds": None,
            "resets_at": at(offset), "cooldown_until": at(offset), "applies_to_models": models}


def _holds_status():
    snaps = [
        # At the limit, and the engine says so too: one fact, counted once.
        oauth("measured", fable=1, exhaustions=[exhaustion(real_iso(DAY))]),
        # The exhaustion comes from a stale reading; a newer source measures
        # the Fable share below the limit: the share is held back.
        oauth("split", fable=1, exhaustions=[exhaustion(real_iso(DAY))], fresh=False, observed=-7200),
        oauth("split", fable=0.4, source="claude_api", observed=-30),
        # Only disclosed: a passed reset (a new cycle is measured), no reset,
        # an unreadable one.
        oauth("passed", fable=0.3, exhaustions=[exhaustion(real_iso(-60))]),
        oauth("unreported", fable=0.5, exhaustions=[exhaustion(None)]),
        oauth("unreadable", fable=0.5, exhaustions=[exhaustion("soon")]),
        # Live, but naming no model: it can hold no window.
        oauth("unnamed", fable=0.5, exhaustions=[exhaustion(real_iso(DAY), models=[])]),
        # Two holds on two different model scopes, both reported apart from
        # the windows drawn (by stale readings): a Fable limit reported out
        # and an Opus cooldown.
        oauth("two", fable=1, exhaustions=[exhaustion(real_iso(DAY))], fresh=False, observed=-7200,
              extra=[_bare_cooldown(3600, models=OPUS)]),
        oauth("two", fable=0.3, source="claude_api", observed=-30),
        # Two model cooldowns on two scopes, no exhaustion.
        oauth("twocool", fable=0.3),
        oauth("twocool", fable=0.3, fresh=False, observed=-7200, source="claude_api_retry",
              extra=[_bare_cooldown(3600, models=OPUS), _bare_cooldown(5400, models=["sonnet"])]),
        # The same Opus cooldown on the reading drawn: the row shows it as its
        # own window ("Opus", "cooldown") and does not say it twice.
        oauth("drawncool", fable=0.3, extra=[_bare_cooldown(3600, models=OPUS)]),
        # A 25-name scope, cooling: the 25th name is not listed, and says so.
        oauth("wide", fable=0.3, extra=[
            _bare_cooldown(3600, models=WIDE + ["m99-x"]),
            {"id": "weekly_scoped:Wide", "label": "7 day wide", "applies_to_models": WIDE + ["m99-x"],
             "used_ratio": 0.2, "window_seconds": 604800, "resets_at": real_iso(DAY),
             "cooldown_until": None}]),
        # A spent shared window and an account cooldown: two facts, two colours.
        oauth("fullcool", fable=0.3, extra=[_bare_cooldown(7200)]),
    ]
    snaps[-1]["constraints"][1]["used_ratio"] = 1
    sids = ("measured", "split", "passed", "unreported", "unreadable", "unnamed", "two", "twocool",
            "drawncool", "wide", "fullcool")
    return payload(snaps, [profile("claude", sid) for sid in sids], harnesses=("claude",))


def _accounts(view):
    return {a["subject_id"]: a["quota"] for g in view["groups"] for a in g["accounts"]}


def test_exhaustions_are_their_own_facts_across_reserve_account_view_and_tool(tmp_path, monkeypatch):
    view, tool = _route_and_tool(tmp_path, monkeypatch, _holds_status())
    summary = view["reserve"]["summary"]
    fable = group(summary, "|weekly_scoped:Fable|")
    scope = fable["key"].rsplit("|", 1)[1]
    assert scope == qs._scope_hash(qs._models_of(FABLE))

    # The reserve: only accounts whose counted Fable share is below the limit
    # while a live exhaustion holds that scope are restricted ("split" 0.6,
    # "two" 0.7) — by "model_exhausted", never "cooling". "measured", at the
    # limit, is already said by its share. The only cooldown here is
    # "fullcool"'s, on the whole account (and its spent week blocks Fable).
    assert fable["measured"]["at_limit"] == 1
    assert fable["restrictions"] == {
        "cooling": {"accounts": 1, "windows": 0.7},
        "model_exhausted": {"accounts": 2, "windows": 1.3},
        "other_limit_spent": {"accounts": 1, "windows": 0.7},
    }
    # Never the whole account: its shared windows carry no such restriction.
    for part in ("|seven_day|", "|five_hour|"):
        assert "model_exhausted" not in group(summary, part)["restrictions"], part
    rows = {row["key"]: row for row in tool["groups"]}
    assert rows[fable["key"]]["restrictions"] == fable["restrictions"]

    accounts = _accounts(view)
    # Not a cooldown, and never the account's state.
    for sid in ("measured", "split", "passed", "unreported", "unreadable", "unnamed"):
        quota = accounts[sid]
        assert quota["cooldowns"] == [], sid
        assert quota["state"] == "ok" and quota["label"] == "70% used", (sid, quota["label"])

    def facts(sid):
        return [(e["constraint_id"], e["live"], e["reset_note"], e["resets_at"], e["freshness"],
                 e["scope_key"], e["models"], e["models_omitted"])
                for e in accounts[sid]["model_exhaustions"]]

    live = ("weekly_scoped:Fable", True, "", at(DAY), "fresh", scope, sorted(FABLE), 0)
    assert facts("measured") == [live]
    assert facts("split") == [live[:4] + ("stale",) + live[5:]]
    assert facts("passed") == [("weekly_scoped:Fable", False, "passed", at(-60), "fresh", scope,
                                sorted(FABLE), 0)]
    assert facts("unreported") == [("weekly_scoped:Fable", False, "not_reported", "", "fresh", scope,
                                    sorted(FABLE), 0)]
    assert facts("unreadable") == [("weekly_scoped:Fable", False, "unreadable", "", "fresh", scope,
                                    sorted(FABLE), 0)]
    assert facts("unnamed") == [("weekly_scoped:Fable", True, "", at(DAY), "fresh", "-", [], 0)]
    # The window it names carries the same identity.
    fable_view = next(c for c in accounts["split"]["constraints"] if c["id"] == "weekly_scoped:Fable")
    assert (fable_view["scope_key"], fable_view["used_text"], fable_view["at_limit"]) == (scope, "40", False)
    # A bare cooldown constraint names its own scope, not the account's "-".
    wide_bare = next(c for c in accounts["wide"]["constraints"] if c["id"] == "cooldown")
    assert wide_bare["scope_key"] == accounts["wide"]["cooldowns"][0]["scope_key"] != "-"
    # Past 24 names: the first 24 travel, the count of the rest with them.
    wide = accounts["wide"]["cooldowns"][0]
    assert (len(wide["models"]), wide["models_omitted"]) == (24, 1)


def test_a_live_exhaustion_holds_only_its_whole_scope_and_only_until_its_reset():
    twin_a, twin_b = WIDE + ["m99-x"], WIDE + ["m99-y"]

    def data(entry_models, reset):
        rows = [snap("codex", sid, [constraint("scoped", .2, reset=DAY, models=models)])
                for sid, models in (("a", twin_a), ("b", twin_b))]
        rows[0]["availability"]["model_scoped_exhaustions"] = [exhaustion(reset, entry_models, "scoped")]
        return payload(rows, [profile("codex", "a"), profile("codex", "b")], harnesses=("codex",))

    # On the twin that shares every listed name: nothing is held.
    summary, _ = summary_of(data(twin_b, at(DAY)))
    assert [g["restrictions"] for g in summary["groups"]] == [{}, {}]
    # On its own scope: that group, that account, only.
    summary, _ = summary_of(data(twin_a, at(DAY)))
    held = sorted(g["restrictions"].get("model_exhausted", {}).get("accounts", 0) for g in summary["groups"])
    assert held == [0, 1]
    # Its reset passed, none reported, one unreadable: disclosed, never a hold.
    for reset in (at(-1), None, "", "tomorrow"):
        summary, _ = summary_of(data(twin_a, reset))
        assert [g["restrictions"] for g in summary["groups"]] == [{}, {}], reset
    # The account view keeps the whole identity and says what it leaves out.
    raw = data(twin_a, at(DAY))
    quota = plugin.quota_for(raw["quota"], "codex", "a", "ok", now=NOW, attributed=qs.attribute(raw))
    entry = quota["model_exhaustions"][0]
    assert (len(entry["models"]), entry["models_omitted"], entry["live"]) == (24, 1, True)
    own = next(g for g in summary_of(raw)[0]["groups"] if g["restrictions"])
    assert own["key"].endswith("|" + entry["scope_key"])


# The field layout of two recorded snapshots. Identities, times and shares are
# invented; kept are only the relations the test reads: a stale and a fresh
# snapshot, each at its Fable limit with a live reported exhaustion, the stale
# one's five-hour reset already passed, the fresh one's five-hour window idle
# with no reported reset, and a reset whose fraction of a second is past one
# half (shown truncated, not rounded up).
RECORDED_SHAPE_NOW = "2031-03-02T12:00:00Z"
RECORDED_SHAPE_ROWS = [
    {"subject": {"harness": "claude", "credential_route": "vendor_native", "plan_label": "max",
                 "subject_id": "acct-one"},
     "source": "claude_oauth_usage", "observed_at": "2031-03-02T04:30:00.125Z", "freshness": "stale",
     "availability": {"state": "available", "blocking_constraints": [], "resets_at": None,
                      "model_scoped_exhaustions": [
                          {"constraint_id": "weekly_scoped:Fable",
                           "applies_to_models": ["fable", "claude-fable-5-1", "claude-fable-5", "best"],
                           "resets_at": "2031-03-04T08:00:00.000200+00:00"}]},
     "constraints": [
         {"id": "five_hour", "label": "5 hour", "used_ratio": 0.12, "window_seconds": 18000,
          "resets_at": "2031-03-02T08:00:00.000100+00:00", "cooldown_until": None},
         {"id": "seven_day", "label": "7 day", "used_ratio": 0.4, "window_seconds": 604800,
          "resets_at": "2031-03-04T08:00:00.000150+00:00", "cooldown_until": None},
         {"id": "weekly_scoped:Fable", "label": "7 day (Fable)",
          "applies_to_models": ["fable", "claude-fable-5-1", "claude-fable-5", "best"],
          "used_ratio": 1, "window_seconds": 604800, "resets_at": "2031-03-04T08:00:00.000200+00:00",
          "cooldown_until": None}]},
    {"subject": {"harness": "claude", "credential_route": "vendor_native", "plan_label": "max",
                 "subject_id": "user-example-org"},
     "source": "claude_oauth_usage", "observed_at": "2031-03-02T11:59:50.500Z", "freshness": "fresh",
     "availability": {"state": "available", "blocking_constraints": [], "resets_at": None,
                      "model_scoped_exhaustions": [
                          {"constraint_id": "weekly_scoped:Fable",
                           "applies_to_models": ["fable", "claude-fable-5-1", "claude-fable-5", "best"],
                           "resets_at": "2031-03-02T18:29:59.750400+00:00"}]},
     "constraints": [
         {"id": "five_hour", "label": "5 hour", "used_ratio": 0, "window_seconds": 18000,
          "resets_at": None, "cooldown_until": None},
         {"id": "seven_day", "label": "7 day", "used_ratio": 0.8, "window_seconds": 604800,
          "resets_at": "2031-03-02T18:29:59.750300+00:00", "cooldown_until": None},
         {"id": "weekly_scoped:Fable", "label": "7 day (Fable)",
          "applies_to_models": ["fable", "claude-fable-5-1", "claude-fable-5", "best"],
          "used_ratio": 1, "window_seconds": 604800, "resets_at": "2031-03-02T18:29:59.750400+00:00",
          "cooldown_until": None}]},
]


def test_the_recorded_fable_exhaustions_are_read_as_reported():
    now = qs.parse_instant(RECORDED_SHAPE_NOW)
    data = payload(RECORDED_SHAPE_ROWS, [profile("claude", "acct-one"), profile("claude", "user-example-org")],
                   harnesses=("claude",))
    found = [(sid, e.live, e.fresh, qs.iso(e.resets_at), e.models, e.scope)
             for hid, sid, row in qs.attribute(data).rows for e in qs.exhaustions_of(row, hid, sid, now)]
    scope = qs._scope_hash(qs._models_of(RECORDED_SHAPE_ROWS[0]["constraints"][2]["applies_to_models"]))
    models = ("best", "claude-fable-5", "claude-fable-5-1", "fable")
    assert found == [("acct-one", True, False, "2031-03-04T08:00:00Z", models, scope),
                     ("user-example-org", True, True, "2031-03-02T18:29:59Z", models, scope)]
    summary, _ = summary_of(data, now=now)
    fable = group(summary, "|weekly_scoped:Fable|")
    # The fresh account is measured at the limit (said once); the stale one is
    # not measured, and its exhaustion restricts no share of the total.
    assert (fable["measured"]["accounts"], fable["measured"]["at_limit"], fable["restrictions"]) == (1, 1, {})
    assert all(not g["restrictions"] for g in summary["groups"])
    # Neither account is cooling or exhausted as a whole.
    for sid in ("acct-one", "user-example-org"):
        quota = plugin.quota_for(data["quota"], "claude", sid, "ok", now=now, attributed=qs.attribute(data))
        assert quota["state"] in ("ok", "no_fresh_window") and quota["cooldowns"] == [], sid
        assert [e["live"] for e in quota["model_exhaustions"]] == [True], sid


NODE_MODEL_HOLDS = r"""
function click(env, key) {
  const node = byFocus(env.root, key);
  assert.ok(node, 'no node with data-focus ' + key);
  node.listeners.click[0]({ stopPropagation() {} });
}
(async () => {
  Date.now = () => Number(process.env.FIXED_NOW);
  const route = JSON.parse(process.env.ROUTE);
  const only = (sid) => {
    const v = JSON.parse(JSON.stringify(route));
    v.groups.forEach((g) => { g.accounts = g.accounts.filter((a) => a.subject_id === sid); });
    return v;
  };
  const plane = (root) => classes(root, 'account-plane')[0];
  const holdLines = (root) => classes(plane(root), 'quota-exhaustion');
  const dotOf = (root) => classes(byFocus(root, 'account-btn'), 'state-dot')[0].className;
  const meterOf = (node) => classes(node, 'meter')[0];
  const tileOf = (root, label) => classes(root, 'quota-tile').find((t) => t.title === label);
  const holdChips = (opt) => classes(opt, 'acct-cap')
    .filter((c) => !/\bacct-pool\b/.test(c.className) && c.textContent !== 'cooldown');
  const keyOf = (part) => route.reserve.summary.groups.find((g) => g.key.includes(part)).key;
  const tickers = (node) => classes(node, 'ticker').map((t) => t.className);

  // A live exhaustion reported by a stale reading, the Fable share below the
  // limit: an amber model hold with its reset, never a cooldown, never the account.
  let env = await boot(only('split'));
  let lines = holdLines(env.root);
  assert.equal(lines.length, 1);
  assert.match(lines[0].textContent, /^Model limit reached · Fable · until /);
  assert.match(lines[0].textContent, /reported by a stale reading/);
  assert.doesNotMatch(lines[0].className, /\bpast\b/);
  assert.ok(tickers(lines[0]).length && tickers(lines[0]).every((c) => /\bwarn\b/.test(c) && !/\bbad\b/.test(c)));
  assert.equal(classes(plane(env.root), 'quota-cooldown').length, 0);
  assert.doesNotMatch(allSpoken(plane(env.root)), /Limit reached|Cooling/);
  assert.match(dotOf(env.root), /\bwarn\b/);
  const fableRow = byFocus(env.root, 'reserve:' + keyOf('|weekly_scoped:Fable|'));
  assert.match(fableRow.getAttribute('aria-label'), /2 with this model limit reported reached \(1\.30\)/);
  // 0.7.0: the row's details button carries the spoken summary; the tail
  // is beside it in the row (accounts unnamed here: no account keys).
  assert.match(classes(fableRow.parentNode, 'l-tail')[0].textContent,
    /^1 at the limit · 3 restricted/);
  click(env, 'account-details');
  assert.match(meterOf(tileOf(env.root, '7 day (Fable)')).className, /\brestricted\b/);
  assert.match(classes(tileOf(env.root, '7 day (Fable)'), 'tile-model')[0].className, /\bheld\b/);
  assert.doesNotMatch(meterOf(tileOf(env.root, '7 day')).className, /\brestricted\b/);
  click(env, 'account-btn');
  let opt = byFocus(env.root, 'opt:claude:split');
  assert.deepEqual(holdChips(opt).map((c) => c.textContent), ['model limit reached: Fable']);
  assert.ok(holdChips(opt).every((c) => /\bheld\b/.test(c.className) && !/\bexhausted\b/.test(c.className)));
  assert.match(opt.getAttribute('aria-label'), /model limit reached: Fable until .*reported by a stale reading/);
  assert.doesNotMatch(opt.textContent, /model cooldown|account cooldown/);
  assert.ok(classes(opt, 'acct-pool').every((c) => /\bheld\b/.test(c.className)));

  // Measured at the limit and reported out: red, said once in the brief, the
  // reported reset in the details.
  env = await boot(only('measured'));
  assert.equal(holdLines(env.root).length, 0);
  assert.match(plane(env.root).textContent, /Fable: limit reached · in the details/);
  click(env, 'account-details');
  lines = holdLines(env.root);
  assert.equal(lines.length, 1);
  assert.match(lines[0].textContent, /^Model limit reached · Fable · until /);
  assert.match(classes(tileOf(env.root, '7 day (Fable)'), 'tile-model')[0].className, /\bspent\b/);
  assert.match(meterOf(tileOf(env.root, '7 day (Fable)')).className, /\bspent\b/);
  click(env, 'account-btn');
  opt = byFocus(env.root, 'opt:claude:measured');
  assert.deepEqual(holdChips(opt).map((c) => c.textContent), []);
  assert.ok(classes(opt, 'acct-pool').some((c) => /\bexhausted\b/.test(c.className)));

  // No reset, an unreadable one: disclosed in the muted voice, no hold.
  for (const [sid, words] of [['unreported', 'no reset time reported'], ['unreadable', 'reset time unreadable']]) {
    env = await boot(only(sid));
    lines = holdLines(env.root);
    assert.equal(lines.length, 1, sid);
    assert.equal(lines[0].textContent, 'Reported model limit reached · Fable · ' + words, sid);
    assert.match(lines[0].className, /\bpast\b/, sid);
    assert.match(dotOf(env.root), /\bok\b/, sid);
    click(env, 'account-details');
    assert.doesNotMatch(meterOf(tileOf(env.root, '7 day (Fable)')).className, /\brestricted\b/, sid);
    assert.doesNotMatch(classes(tileOf(env.root, '7 day (Fable)'), 'tile-model')[0].className, /\bheld\b|\bspent\b/, sid);
    click(env, 'account-btn');
    assert.deepEqual(holdChips(byFocus(env.root, 'opt:claude:' + sid)).map((c) => c.textContent), [], sid);
  }
  // A passed reset is history: only in the details.
  env = await boot(only('passed'));
  assert.equal(holdLines(env.root).length, 0);
  click(env, 'account-details');
  lines = holdLines(env.root);
  assert.equal(lines.length, 1);
  assert.match(lines[0].textContent, /^Reported model limit reached · Fable · its reported reset has passed/);
  assert.match(lines[0].className, /\bpast\b/);
  assert.match(dotOf(env.root), /\bok\b/);

  // Live, but naming no model (scope '-'): the reserve counts it against no
  // window, so it holds nothing here either — no amber dot, no brief line,
  // no hold chip, no held tile or pool. It is disclosed in the details, in
  // the muted voice, with its reset as reported.
  env = await boot(only('unnamed'));
  assert.match(dotOf(env.root), /\bok\b/);
  assert.equal(holdLines(env.root).length, 0);
  assert.doesNotMatch(plane(env.root).textContent, /limit reached/i);
  click(env, 'account-details');
  lines = holdLines(env.root);
  assert.equal(lines.length, 1);
  assert.match(lines[0].textContent,
    /^Reported model limit reached · models not named · reported until .* · holds no window$/);
  assert.match(lines[0].className, /\bpast\b/);
  assert.ok(tickers(lines[0]).length && tickers(lines[0]).every((c) => !/\bwarn\b|\bbad\b/.test(c)));
  assert.match(lines[0].getAttribute('aria-label'), /holds no window/);
  assert.doesNotMatch(meterOf(tileOf(env.root, '7 day (Fable)')).className, /\brestricted\b/);
  assert.doesNotMatch(classes(tileOf(env.root, '7 day (Fable)'), 'tile-model')[0].className, /\bheld\b|\bspent\b/);
  for (const label of ['5 hour', '7 day']) {
    assert.doesNotMatch(meterOf(tileOf(env.root, label)).className, /\brestricted\b/, label);
  }
  click(env, 'account-btn');
  opt = byFocus(env.root, 'opt:claude:unnamed');
  assert.deepEqual(holdChips(opt).map((c) => c.textContent), []);
  assert.match(classes(opt, 'state-dot')[0].className, /\bok\b/);
  assert.ok(classes(opt, 'acct-pool').every((c) => !/\bheld\b|\bexhausted\b/.test(c.className)));
  assert.ok(classes(opt, 'meter').every((c) => !/\brestricted\b|\bspent\b/.test(c.className)));
  assert.doesNotMatch(opt.getAttribute('aria-label'), /limit reached|cooldown|needs a look/);
  // Its family mark and the selector's alarm are not raised by it either.
  env = await boot(only('unnamed'));
  assert.equal(classes(env.root, 'acct-alarm').length, 0);

  // Two holds on two scopes are two named facts, never one "model cooldown".
  env = await boot(route);
  click(env, 'account-btn');
  opt = byFocus(env.root, 'opt:claude:two');
  assert.deepEqual(holdChips(opt).map((c) => c.textContent), ['2 model holds']);
  const both = holdChips(opt)[0].title;
  assert.match(both, /model limit reached: Fable until /);
  assert.match(both, /model cooldown: Opus until /);
  assert.match(opt.getAttribute('aria-label'), /2 model holds: model cooldown: Opus until [^;]*; model limit reached: Fable until /);
  const cool2 = byFocus(env.root, 'opt:claude:twocool');
  assert.deepEqual(holdChips(cool2).map((c) => c.textContent), ['2 model cooldowns']);
  assert.match(holdChips(cool2)[0].title, /model cooldown: Opus until .*; model cooldown: Sonnet until /);
  assert.match(classes(byFocus(env.root, 'opt:claude:two'), 'state-dot')[0].className, /\bwarn\b/);
  const drawn = byFocus(env.root, 'opt:claude:drawncool');
  assert.deepEqual(holdChips(drawn).map((c) => c.textContent), []);
  const opusWin = classes(drawn, 'acct-grp').find((g) => /^Opus/.test(g.textContent));
  assert.ok(opusWin, classes(drawn, 'acct-grp').map((g) => g.textContent).join(' | '));
  assert.match(opusWin.textContent, /cooldown/);
  assert.ok(classes(opusWin, 'acct-cap').every((c) => !/\bexhausted\b/.test(c.className)));
  assert.ok(classes(opusWin, 'acct-pool').every((c) => /\bheld\b/.test(c.className)));

  // Past 24 names the count of the rest is said: on the cooldown line, in its
  // title, and in the reserve row's name.
  env = await boot(only('wide'));
  const wideLine = classes(plane(env.root), 'quota-cooldown')[0];
  assert.match(wideLine.textContent, /^Cooling down · M00 \+24 · until /);
  assert.match(wideLine.title, /m23 \+1 more not listed$/);
  assert.match(wideLine.getAttribute('aria-label'), /\+1 more not listed/);
  const wideRow = byFocus(env.root, 'reserve:' + keyOf('|weekly_scoped:Wide|'));
  assert.equal(classes(wideRow.parentNode, 'l-name-text')[0].textContent, 'Weekly · M00 +24');
  assert.match(wideRow.getAttribute('aria-label'), /models: m00, m01, .*m23 \+1 more/);

  // A spent shared window and an account cooldown: the reset red, the
  // cooldown's end amber — each on its own line.
  env = await boot(only('fullcool'));
  const verdict = classes(plane(env.root), 'quota-primary-row')[0];
  assert.match(verdict.textContent, /^Limit reached/);
  assert.ok(tickers(verdict).length && tickers(verdict).every((c) => /\bbad\b/.test(c)));
  const coolLine = classes(plane(env.root), 'quota-cooldown')[0];
  assert.ok(tickers(coolLine).length && tickers(coolLine).every((c) => /\bwarn\b/.test(c) && !/\bbad\b/.test(c)));
  // An older answer without the cooldown list: the verdict itself, amber.
  const older = only('fullcool');
  const q = older.groups[0].accounts[0].quota;
  q.cooldowns = []; q.state = 'cooling'; q.label = 'Cooling down';
  q.cooling_until = new Date(Number(process.env.FIXED_NOW) + 7200000).toISOString();
  env = await boot(older);
  const coolVerdict = classes(plane(env.root), 'quota-primary-text')[0];
  assert.match(coolVerdict.className, /\bcooling\b/);
  assert.ok(tickers(classes(plane(env.root), 'quota-primary-row')[0]).every((c) => /\bwarn\b/.test(c)));
  assert.match(widgetSource, /\.quota-primary-text\.cooling\{color:var\(--warn-text\)\}/);
  assert.doesNotMatch(widgetSource, /\.quota-primary-text\.cooling[^{]*\{color:var\(--status-bad\)/);
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
"""


def test_real_widget_model_holds_and_the_two_colours(tmp_path, monkeypatch):
    route, _tool = _route_and_tool(tmp_path, monkeypatch, _holds_status())
    monkeypatch.undo()
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", harness + NODE_MODEL_HOLDS], cwd=widget.parent,
        env={**os.environ, "WIDGET_PATH": str(widget), "ROUTE": json.dumps(route),
             "FIXED_NOW": str(int(NOW * 1000))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _floor_status():
    """Fable-only holds on an account whose shared windows are fine: a Fable
    cooldown and a Fable limit reported out, each carried apart from the
    window drawn (by an older reading); the Fable window's own cooldown; and
    the Fable share measured at the limit."""
    snaps = [
        oauth("fablecool", fable=0.4),
        oauth("fablecool", fable=0.4, fresh=False, observed=-7200, source="claude_api_retry",
              extra=[_bare_cooldown(3600, models=FABLE)]),
        oauth("fablelimit", fable=1, exhaustions=[exhaustion(real_iso(DAY))], fresh=False, observed=-7200),
        oauth("fablelimit", fable=0.4, source="claude_api", observed=-30),
        oauth("fableown", fable=0.4),
        oauth("fablespent", fable=1, exhaustions=[exhaustion(real_iso(DAY))]),
    ]
    snaps[4]["constraints"][2]["cooldown_until"] = at(3600)
    sids = ("fablecool", "fablelimit", "fableown", "fablespent")
    return payload(snaps, [profile("claude", sid) for sid in sids], harnesses=("claude",))


NODE_SECOND_FLOOR = r"""
(async () => {
  Date.now = () => Number(process.env.FIXED_NOW);
  const route = JSON.parse(process.env.ROUTE);
  const opened = async (density) => {
    const v = JSON.parse(JSON.stringify(route));
    v.prefs = { density, models: {}, fold: {} };
    const env = await boot(v);
    byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
    return env;
  };
  const rowOf = (env, sid) => byFocus(env.root, 'opt:claude:' + sid);
  const linesOf = (opt) => classes(opt, 'acct-rl');
  const tagTone = (line) => classes(line, 'acct-rl-tag')[0].className;
  const fableLines = (opt) => linesOf(opt).filter((l) => classes(l, 'acct-pool').length);
  const sharedLines = (opt) => linesOf(opt).filter((l) => !classes(l, 'acct-pool').length);
  const sharedGroup = (opt) => classes(opt, 'acct-grp').find((g) => !classes(g, 'acct-pool').length);

  let env = await opened('detailed');
  for (const sid of ['fablecool', 'fablelimit', 'fableown', 'fablespent']) {
    const opt = rowOf(env, sid);
    // A model's hold is the model's: the shared windows stay neutral and
    // available, and nothing on the row speaks for the whole account.
    assert.equal(sharedLines(opt).length, 2, sid);
    sharedLines(opt).forEach((l) => {
      assert.match(l.textContent, /available$/, sid);
      assert.match(tagTone(l), /\bok\b/, sid);
    });
    assert.ok(classes(sharedGroup(opt), 'meter').every((m) => !/\brestricted\b|\bspent\b/.test(m.className)), sid);
    assert.match(classes(opt, 'state-dot')[0].className, /\bwarn\b/, sid);
    assert.doesNotMatch(opt.getAttribute('aria-label'), /account cooldown|alert/, sid);
    assert.doesNotMatch(opt.textContent, /account cooldown/, sid);
  }
  // Cooling (a cooldown on exactly the Fable scope, from an older reading):
  // amber, until the cooldown's end, and never "available".
  let fable = fableLines(rowOf(env, 'fablecool'));
  assert.ok(fable.length >= 1);
  fable.forEach((l) => {
    assert.match(l.textContent, /^[^]*Fablecooling down/);
    assert.match(tagTone(l), /\bwarn\b/);
    assert.match(classes(l, 'acct-pool')[0].className, /\bheld\b/);
  });
  assert.match(rowOf(env, 'fablecool').getAttribute('aria-label'),
    /Fable cooling down until [^·]*reported by a stale reading/);
  // A Fable limit reported out while the share read here is below it:
  // amber, "limit reported", until that limit's reset — not spent, not red.
  fable = fableLines(rowOf(env, 'fablelimit'));
  assert.equal(fable.length, 1);
  assert.match(fable[0].textContent, /^weekFablelimit reported/);
  assert.match(tagTone(fable[0]), /\bwarn\b/);
  assert.doesNotMatch(fable[0].textContent, /spent|available/);
  assert.match(classes(fable[0], 'acct-pool')[0].className, /\bheld\b/);
  assert.match(rowOf(env, 'fablelimit').getAttribute('aria-label'),
    /week Fable model limit reported reached until [^·]*reported by a stale reading/);
  // The window's own cooldown: amber, cooling down.
  fable = fableLines(rowOf(env, 'fableown'));
  assert.equal(fable.length, 1);
  assert.match(fable[0].textContent, /^weekFablecooling down/);
  // Measured at the limit: red, spent, back at its reset — never amber.
  fable = fableLines(rowOf(env, 'fablespent'));
  assert.equal(fable.length, 1);
  assert.match(fable[0].textContent, /^weekFablespent/);
  assert.match(tagTone(fable[0]), /\bbad\b/);
  assert.match(classes(fable[0], 'acct-pool')[0].className, /\bexhausted\b/);

  // Normal density keeps one line for the rest, named "others", neutral.
  env = await opened('normal');
  for (const sid of ['fablecool', 'fablelimit']) {
    const lines = linesOf(rowOf(env, sid));
    assert.ok(lines.length >= 2, sid);
    assert.match(lines[0].textContent, /^weekFable(cooling down|limit reported)/, sid);
    const last = lines[lines.length - 1];
    assert.match(last.textContent, /^others70% usedavailable$/, sid);
    assert.match(tagTone(last), /\bok\b/, sid);
  }
})().catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
"""


def test_real_widget_second_floor_keeps_a_model_hold_on_its_model(tmp_path, monkeypatch):
    route, _tool = _route_and_tool(tmp_path, monkeypatch, _floor_status())
    accounts = _accounts(route)
    # The skill's own verdicts first: none of these is held as a whole.
    for sid in ("fablecool", "fablelimit", "fableown", "fablespent"):
        assert accounts[sid]["state"] == "ok", (sid, accounts[sid]["state"])
        assert all(c["scope"] == "models" for c in accounts[sid]["cooldowns"]), sid
    monkeypatch.undo()
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    widget = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", harness + NODE_SECOND_FLOOR], cwd=widget.parent,
        env={**os.environ, "WIDGET_PATH": str(widget), "ROUTE": json.dumps(route),
             "FIXED_NOW": str(int(NOW * 1000))},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
