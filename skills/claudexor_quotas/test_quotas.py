"""Tests for Claudexor quota normalization, routes, and the real module widget.

The Python cases cover projection and actual registered handlers. A bundled-Node
in-process harness executes widget.js itself (without a browser framework) for:
- Per-facet provenance and notes (ok, not_read, failed, indeterminate, missing reads).
- Timestamp and future-detection handling.
- Constraint views, ratio clamping, and NaN handling.
- Quota projection: fresh, stale, no-data, degraded facet, global exhaustion vs per-model caps.
- Verification view tones and "last known" degradation.
- Account and group structuring (native vs profile, next_up resolution, harness ordering).
- Fresh/stale/exhausted rendering, approved absence actions, exact-subject merging,
  passive polling, foreground refresh, old-host failure, ARIA/title honesty, and teardown.
- Full view building and transport error handling.
- Display preferences: what the skill agrees to remember, and what it drops.
"""

import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path
import pytest

import plugin
from plugin import (
    DEFAULT_PREFS,
    FOLD_REASONS,
    MAX_MODEL_ENTRIES,
    clean_prefs,
    read_prefs,
    write_prefs,
    FACETS,
    READ_OK,
    facet_states,
    facet_note,
    _subject_key,
    _is_future,
    _constraint_view,
    _spent,
    quota_for,
    verification_view,
    build_groups,
    build_view,
    build_quota_updates,
)


class TestFacetStates:
    def test_all_facets_ok(self):
        payload = {"reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"}}
        assert facet_states(payload) == {
            "catalog": "ok",
            "accounts": "ok",
            "quota": "ok",
        }

    def test_mixed_facet_states(self):
        payload = {"reads": {"catalog": "ok", "accounts": "failed", "quota": "not_read"}}
        assert facet_states(payload) == {
            "catalog": "ok",
            "accounts": "failed",
            "quota": "not_read",
        }

    def test_invalid_facet_values_become_indeterminate(self):
        payload = {"reads": {"catalog": "unknown", "accounts": None, "quota": 123}}
        assert facet_states(payload) == {
            "catalog": "indeterminate",
            "accounts": "indeterminate",
            "quota": "indeterminate",
        }

    def test_missing_or_non_dict_payload_is_indeterminate(self):
        assert facet_states(None) == {f: "indeterminate" for f in FACETS}
        assert facet_states({}) == {f: "indeterminate" for f in FACETS}
        assert facet_states({"reads": "invalid"}) == {f: "indeterminate" for f in FACETS}


class TestFacetNote:
    def test_all_ok_returns_empty_string(self):
        states = {"catalog": "ok", "accounts": "ok", "quota": "ok"}
        assert facet_note(states) == ""

    def test_degraded_facets_listed(self):
        states = {"catalog": "ok", "accounts": "failed", "quota": "not_read"}
        note = facet_note(states)
        assert "accounts: failed" in note
        assert "quota: not_read" in note
        assert "catalog" not in note


class TestSubjectKey:
    def test_subject_key_normalizes_null_and_empty(self):
        assert _subject_key(None) == ""
        assert _subject_key("") == ""
        assert _subject_key("   ") == ""

    def test_subject_key_preserves_strings(self):
        assert _subject_key("prof_123") == "prof_123"
        assert _subject_key(" prof_abc ") == "prof_abc"


class TestIsFuture:
    def test_future_iso_timestamp(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        assert _is_future(future) is True

    def test_past_iso_timestamp(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        assert _is_future(past) is False

    def test_iso_with_z_suffix(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert _is_future(future) is True

    def test_invalid_and_empty_returns_none(self):
        assert _is_future("") is None
        assert _is_future(None) is None
        assert _is_future("not-a-timestamp") is None


class TestConstraintView:
    def test_valid_ratio_converted_to_pct(self):
        c = {
            "id": "c1",
            "label": "5-hour window",
            "used_ratio": 0.456,
            "window_seconds": 18000,
            "resets_at": "2026-08-16T00:00:00Z",
            "cooldown_until": "",
            "applies_to_models": ["claude-3-opus"],
        }
        view = _constraint_view(c)
        assert view["label"] == "5-hour window"
        assert view["used_pct"] == 46
        assert view["window_seconds"] == 18000
        assert view["scoped_models"] == ["claude-3-opus"]

    def test_ratio_clamping(self):
        assert _constraint_view({"used_ratio": 1.5})["used_pct"] == 100
        assert _constraint_view({"used_ratio": -0.2})["used_pct"] == 0

    def test_missing_or_nan_ratio(self):
        assert _constraint_view({"used_ratio": None})["used_pct"] is None
        assert _constraint_view({"used_ratio": float("nan")})["used_pct"] is None
        assert _constraint_view({})["used_pct"] is None


class TestSpentLogic:
    def test_spent_when_used_pct_100_or_more(self):
        assert _spent({"used_pct": 100, "cooldown_until": ""}) is True
        assert _spent({"used_pct": 105, "cooldown_until": ""}) is True
        assert _spent({"used_pct": 99, "cooldown_until": ""}) is False

    def test_spent_when_cooldown_active(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        assert _spent({"used_pct": 10, "cooldown_until": future}) is True

    def test_not_spent_when_cooldown_in_past(self):
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)).isoformat()
        assert _spent({"used_pct": 50, "cooldown_until": past}) is False

    def test_spent_when_cooldown_unparseable(self):
        assert _spent({"used_pct": 20, "cooldown_until": "corrupt-date"}) is True


class TestQuotaFor:
    def test_quota_facet_not_ok_returns_not_checked(self):
        res = quota_for([], "claude", "prof_1", "failed")
        assert res["state"] == "not_checked"
        assert "Limits not checked" in res["label"]
        assert res["constraints"] == []

    def test_no_data_when_no_matching_snapshots(self):
        snapshots = [
            {"subject": {"harness": "cursor", "subject_id": "prof_1"}, "freshness": "fresh"}
        ]
        res = quota_for(snapshots, "claude", "prof_1", READ_OK)
        assert res["state"] == "no_data"
        assert "No quota window reported" in res["label"]

    def test_stale_snapshot_reported_without_gating(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "prof_1"},
                "freshness": "stale",
                "observed_at": "2026-08-15T12:00:00Z",
                "constraints": [{"label": "Weekly", "used_ratio": 0.8}],
            }
        ]
        res = quota_for(snapshots, "claude", "prof_1", READ_OK)
        assert res["state"] == "no_fresh_window"
        assert len(res["stale"]) == 1
        assert res["stale"][0]["freshness"] == "stale"
        assert res["constraints"] == []

    def test_fresh_quota_ok_state(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": ""},
                "freshness": "fresh",
                "availability": {"state": "available"},
                "constraints": [
                    {
                        "label": "Hourly",
                        "used_ratio": 0.35,
                        "resets_at": "2026-08-15T22:00:00Z",
                        "applies_to_models": [],
                    },
                    {
                        "label": "Daily",
                        "used_ratio": 0.70,
                        "resets_at": "2026-08-16T00:00:00Z",
                        "applies_to_models": [],
                    },
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "", READ_OK)
        assert res["state"] == "ok"
        assert "70% used" in res["label"]
        assert res["resets_at"] == "2026-08-16T00:00:00Z"
        assert len(res["constraints"]) == 2

    def test_fresh_quota_exhausted_global_constraint(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "constraints": [
                    {
                        "label": "5-Hour",
                        "used_ratio": 1.0,
                        "resets_at": "2026-08-15T23:00:00Z",
                        "applies_to_models": [],
                    }
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "p1", READ_OK)
        assert res["state"] == "exhausted"
        assert res["label"] == "Limit reached"
        assert res["resets_at"] == "2026-08-15T23:00:00Z"

    def test_per_model_scoped_exhaustion_does_not_exhaust_account(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "constraints": [
                    {
                        "label": "Opus Cap",
                        "used_ratio": 1.0,
                        "resets_at": "2026-08-16T00:00:00Z",
                        "applies_to_models": ["claude-3-opus"],
                    },
                    {
                        "label": "General Cap",
                        "used_ratio": 0.40,
                        "resets_at": "2026-08-16T04:00:00Z",
                        "applies_to_models": [],
                    },
                ],
            }
        ]
        res = quota_for(snapshots, "claude", "p1", READ_OK)
        assert res["state"] == "ok"
        assert "40% used" in res["label"]
        assert "per-model caps spent: Opus Cap" in res["note"]

    def test_exact_subject_matching_isolation(self):
        snapshots = [
            {
                "subject": {"harness": "claude", "subject_id": None},
                "freshness": "fresh",
                "constraints": [{"label": "Native", "used_ratio": 0.99}],
            },
            {
                "subject": {"harness": "claude", "subject_id": "profile_1"},
                "freshness": "fresh",
                "constraints": [{"label": "Profile", "used_ratio": 0.10}],
            },
        ]
        native_res = quota_for(snapshots, "claude", "", READ_OK)
        profile_res = quota_for(snapshots, "claude", "profile_1", READ_OK)
        assert native_res["constraints"][0]["label"] == "Native"
        assert profile_res["constraints"][0]["label"] == "Profile"

    @pytest.mark.parametrize(
        ("reason", "retry_ms", "action_kind"),
        [
            ("not_logged_in", None, "sign_in_if_unverified"),
            ("auth_revoked", None, "sign_in_if_unverified"),
            ("no_source", None, "source_missing"),
            ("rate_limited", 240_000, "retry"),
            ("rate_limited", None, ""),
            ("transport_unavailable", None, ""),
            ("platform_unsupported", None, ""),
            ("refresh_failed", None, ""),
            ("probe_skipped_rate_limited", None, ""),
            ("poll_paced", None, ""),
            ("credential_profile_ambiguous", None, ""),
        ],
    )
    def test_typed_absence_mapping_is_generic(self, reason, retry_ms, action_kind):
        absence = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "reason": reason,
            "detail": "/private/secret/path vendor body",
            "observed_at": "2026-09-01T08:00:00+00:00",
        }
        if retry_ms is not None:
            absence["retry_after_ms"] = retry_ms
        result = quota_for([], "claude", "p1", READ_OK, [absence])
        assert result["absence"]["message"] == "Quota temporarily unavailable"
        assert result["absence"]["action_kind"] == action_kind
        visible_model = json.dumps(result)
        assert reason not in visible_model
        assert "/private/secret/path" not in visible_model
        assert "vendor body" not in visible_model

    def test_refresh_skip_supplies_retry_without_erasing_stale(self):
        stale = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "freshness": "stale",
            "observed_at": "2026-09-01T08:00:00+00:00",
            "constraints": [{
                "label": "Weekly",
                "used_ratio": 0.83,
                "cooldown_until": "2099-09-01T09:00:00+00:00",
            }],
        }
        result = quota_for(
            [stale],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "poll_paced",
                "observed_at": "2026-09-01T08:01:00+00:00",
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        assert result["state"] == "no_fresh_window"
        assert result["stale"][0]["constraints"][0]["used_pct"] == 83
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:05:00+00:00"
        assert "do not grant routing" in result["note"]
        assert "cooldown evidence may still deny or rank" in result["note"]

    def test_vendor_refresh_skip_discloses_fresh_snapshot_is_last_known(self):
        fresh = {
            "subject": {"harness": "claude", "subject_id": "p1"},
            "freshness": "fresh",
            "observed_at": "2026-09-01T08:00:00+00:00",
            "constraints": [{"label": "Weekly", "used_ratio": 0.41}],
        }
        result = quota_for(
            [fresh],
            "claude",
            "p1",
            READ_OK,
            [],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        assert result["state"] == "ok"
        assert result["constraints"][0]["used_pct"] == 41
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:05:00+00:00"

    def test_vendor_refresh_skip_raises_older_rate_limit_deadline(self):
        result = quota_for(
            [],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "rate_limited",
                "observed_at": "2099-09-01T08:00:00+00:00",
                "retry_after_ms": 300_000,
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:20:00+00:00"}],
        )
        assert result["absence"]["action_kind"] == "retry"
        assert result["absence"]["retry_at"] == "2099-09-01T08:20:00+00:00"

    def test_vendor_refresh_skip_does_not_replace_subject_sign_in_action(self):
        result = quota_for(
            [],
            "claude",
            "p1",
            READ_OK,
            [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "auth_revoked",
                "observed_at": "2026-09-01T08:00:00+00:00",
            }],
            [{"vendor": "claude", "not_before": "2099-09-01T08:05:00+00:00"}],
        )
        assert result["absence"]["action_kind"] == "sign_in_if_unverified"
        assert result["absence"]["retry_at"] == ""


class TestVerificationView:
    def test_vendor_live_passed(self):
        view = verification_view("passed", "vendor", READ_OK, signed_in=True)
        assert view["tone"] == "ok"
        assert view["label"] == "Verified live"

    def test_local_store_passed(self):
        view = verification_view("passed", "local_store", READ_OK, signed_in=True)
        assert view["tone"] == "muted"
        assert "not verified live (local_store)" in view["label"]

    def test_failed_verification(self):
        view = verification_view("failed", "vendor", READ_OK, signed_in=False)
        assert view["tone"] == "warn"
        assert view["label"] == "Verification failed"

    def test_degraded_accounts_facet_appends_last_known(self):
        view = verification_view("passed", "vendor", "failed", signed_in=True)
        assert "last known" in view["label"]
        assert view["tone"] == "muted"


class TestBuildGroupsAndView:
    def test_build_groups_native_and_profiles(self):
        payload = {
            "harnesses": [
                {
                    "id": "claude",
                    "display_name": "Anthropic Claude",
                    "status": "ready",
                    "enabled": True,
                    "provider_family": "anthropic",
                }
            ],
            "profiles": {
                "harnessAccounts": [
                    {
                        "harness_id": "claude",
                        "native_credentials_enabled": True,
                        "native_login_detected": True,
                        "identity": {"email": "user@example.com", "plan": "Pro"},
                        "next_up": {"kind": "native"},
                    }
                ],
                "profiles": [
                    {
                        "profile": {
                            "profile_id": "prof_1",
                            "harness_id": "claude",
                            "display_name": "Work Account",
                            "credential_kind": "oauth",
                            "enabled": True,
                        },
                        "status": {
                            "verification": "passed",
                            "verification_source": "vendor",
                            "availability": "available",
                        },
                        "identity": {"email": "work@company.com", "plan": "Team"},
                    }
                ],
            },
            "quota": [
                {
                    "subject": {"harness": "claude", "subject_id": None},
                    "freshness": "fresh",
                    "constraints": [{"label": "Session", "used_ratio": 0.2}],
                }
            ],
            "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
            "daemon": {"state": "running", "engine_version": "3.3.15"},
        }
        states = facet_states(payload)
        groups = build_groups(payload, states)
        assert len(groups) == 1
        group = groups[0]
        assert group["harness_id"] == "claude"
        assert group["family_label"] == "Anthropic Claude"
        assert len(group["accounts"]) == 2

        native_acc = group["accounts"][0]
        assert native_acc["kind"] == "native"
        assert native_acc["subject_id"] is None
        assert native_acc["next_up"] is True
        assert native_acc["quota"]["state"] == "ok"

        prof_acc = group["accounts"][1]
        assert prof_acc["kind"] == "profile"
        assert prof_acc["subject_id"] == "prof_1"
        assert prof_acc["verified_live"] is True
        assert prof_acc["next_up"] is False
        assert prof_acc["verification"]["label"] == "Verified live"
        assert prof_acc["quota"]["state"] == "no_data"

    def test_build_view_with_transport_error(self):
        view = build_view(None, "Connection refused")
        assert view["ok"] is False
        assert view["transport_error"] == "Connection refused"
        assert view["facets"] == {f: "indeterminate" for f in FACETS}
        assert view["groups"] == []

    def test_build_view_keeps_auth_data_but_projects_quota_age(self):
        payload = {
            "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
            "harnesses": [{"id": "claude", "display_name": "Claude"}],
            "profiles": {
                "harnessAccounts": [],
                "profiles": [{
                    "profile": {
                        "profile_id": "p1",
                        "harness_id": "claude",
                        "display_name": "Personal",
                        "enabled": True,
                    },
                    "status": {
                        "verification": "passed",
                        "verification_source": "vendor",
                        "availability": "available",
                        "last_verified_at": "2026-09-01T07:00:00+00:00",
                    },
                }],
            },
            "quota": [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:00:00+00:00",
                "constraints": [{"label": "Weekly", "used_ratio": 0.3}],
            }],
            "quota_absences": [],
        }
        account = build_view(payload, "")["groups"][0]["accounts"][0]
        assert account["last_verified_at"] == "2026-09-01T07:00:00+00:00"
        assert account["verification_state"] == "passed"
        assert account["verification_source"] == "vendor"
        assert account["quota"]["observed_at"] == "2026-09-01T08:00:00+00:00"

    def test_failed_accounts_read_cannot_suppress_auth_revoked_action(self):
        payload = {
            "reads": {"catalog": "ok", "accounts": "failed", "quota": "ok"},
            "harnesses": [{"id": "claude", "display_name": "Claude"}],
            "profiles": {
                "harnessAccounts": [],
                "profiles": [{
                    "profile": {
                        "profile_id": "p1",
                        "harness_id": "claude",
                        "display_name": "Personal",
                        "enabled": True,
                    },
                    "status": {
                        "verification": "passed",
                        "verification_source": "vendor",
                        "availability": "available",
                        "last_verified_at": "2026-09-01T07:00:00+00:00",
                    },
                }],
            },
            "quota": [],
            "quota_absences": [{
                "subject": {"harness": "claude", "subject_id": "p1"},
                "reason": "auth_revoked",
                "observed_at": "2026-09-01T08:00:00+00:00",
            }],
        }
        account = build_view(payload, "")["groups"][0]["accounts"][0]
        assert account["verification_state"] == "passed"
        assert account["verification_source"] == "vendor"
        assert account["last_verified_at"] == "2026-09-01T07:00:00+00:00"
        assert account["verification"] == {
            "tone": "muted",
            "label": "Verified live — last known",
        }
        assert account["verified_live"] is False
        assert account["quota"]["absence"] == {
            "message": "Quota temporarily unavailable",
            "action_kind": "sign_in_if_unverified",
            "retry_at": "",
            "observed_at": "2026-09-01T08:00:00+00:00",
        }


def test_foreground_updates_are_exact_subject_quota_only():
    payload = {
        "snapshots": [
            {
                "subject": {"harness": "claude", "subject_id": None},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:00:00+00:00",
                "constraints": [{"label": "Native", "used_ratio": 0.9}],
            },
            {
                "subject": {"harness": "claude", "subject_id": "p1"},
                "freshness": "fresh",
                "observed_at": "2026-09-01T08:01:00+00:00",
                "constraints": [{"label": "Named", "used_ratio": 0.2}],
            },
        ],
        "absences": [],
        "refreshed_at": "2026-09-01T08:02:00+00:00",
    }
    result = build_quota_updates(payload)
    assert [(row["harness"], row["subject_id"]) for row in result["quota_updates"]] == [
        ("claude", None),
        ("claude", "p1"),
    ]
    assert result["quota_updates"][0]["quota"]["constraints"][0]["label"] == "Native"
    assert result["quota_updates"][1]["quota"]["constraints"][0]["label"] == "Named"


def test_foreground_update_rejects_malformed_success_envelope():
    assert build_quota_updates({}) == {
        "ok": False,
        "message": "Live quota refresh returned an invalid response",
    }


class _MockAPI:
    def __init__(self):
        self.routes = {}
        self.tabs = {}
        self.logs = []

    def get_runtime_info(self):
        return {"server_port": 8765}

    def register_route(self, name, handler, methods=("GET",)):
        self.routes[name] = {"handler": handler, "methods": methods}

    def register_ui_tab(self, tab_id, title, icon=None, render=None):
        self.tabs[tab_id] = {"title": title, "icon": icon, "render": render}

    def log(self, level, message):
        self.logs.append((level, message))


def test_real_plugin_routes_keep_get_passive_and_post_foreground(monkeypatch):
    calls = []
    status_payload = {
        "reads": {"catalog": "ok", "accounts": "ok", "quota": "ok"},
        "harnesses": [],
        "profiles": {"harnessAccounts": [], "profiles": []},
        "quota": [],
        "quota_absences": [],
    }

    def fake_request(_port, path, method="GET", timeout_sec=plugin.STATUS_TIMEOUT_SEC):
        calls.append((method, path, timeout_sec))
        if method == "GET":
            return status_payload, "", 200
        return {"snapshots": [], "absences": [], "refreshed_at": None}, "", 200

    monkeypatch.setattr(plugin, "_request_json", fake_request)
    api = _MockAPI()
    plugin.register(api)
    assert api.routes["quotas"]["methods"] == ("GET",)
    assert api.tabs["quotas"]["render"]["appearance"] == "host"
    assert api.routes["refresh"]["methods"] == ("POST",)

    assert api.routes["quotas"]["handler"]({})["ok"] is True
    assert calls == [("GET", plugin.STATUS_PATH, plugin.STATUS_TIMEOUT_SEC)]
    assert api.routes["refresh"]["handler"]({})["ok"] is True
    assert calls == [
        ("GET", plugin.STATUS_PATH, plugin.STATUS_TIMEOUT_SEC),
        ("POST", plugin.REFRESH_PATH, plugin.REFRESH_TIMEOUT_SEC),
    ]


def test_real_plugin_old_host_failure_is_honest_and_does_not_get(monkeypatch):
    calls = []

    def old_host(_port, path, method="GET", timeout_sec=plugin.STATUS_TIMEOUT_SEC):
        calls.append((method, path, timeout_sec))
        return None, f"HTTP 404 from {path}", 404

    monkeypatch.setattr(plugin, "_request_json", old_host)
    api = _MockAPI()
    plugin.register(api)
    result = api.routes["refresh"]["handler"]({})
    assert result == {
        "ok": False,
        "compatibility_error": True,
        "message": "Live refresh requires a newer Ouroboros host",
    }
    assert calls == [
        ("POST", plugin.REFRESH_PATH, plugin.REFRESH_TIMEOUT_SEC),
    ]


NODE_WIDGET_MATRIX = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const widgetSource = fs.readFileSync(process.env.WIDGET_PATH, 'utf8');
const instrumentedWidgetSource = widgetSource.replace(
  '    start();\n})();',
  '    window.__quotaTest = { mergeQuotaFacet: mergeQuotaFacet };\n    start();\n})();',
);
assert.notEqual(instrumentedWidgetSource, widgetSource, 'widget test hook insertion failed');

class Element {
  constructor(tag, document) {
    this.tagName = String(tag).toUpperCase();
    this.ownerDocument = document;
    this.childNodes = [];
    this.parentNode = null;
    this.className = '';
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    // className is the whole of it here, so classList reads and writes that
    // string. The widget toggles one class on #root while the settings panel
    // is open, and a stub without it would fail on a standard DOM call.
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      add: (name) => {
        if (!this.classList.contains(name)) {
          this.className = (this.className ? this.className + ' ' : '') + name;
        }
      },
      remove: (name) => {
        this.className = this.className.split(/\s+/).filter((x) => x && x !== name).join(' ');
      },
      toggle: (name, force) => {
        const on = force === undefined ? !this.classList.contains(name) : !!force;
        if (on) this.classList.add(name); else this.classList.remove(name);
        return on;
      },
    };
    this.disabled = false;
    this._text = '';
  }
  set id(value) {
    this.attributes.id = String(value);
    this.ownerDocument.ids[String(value)] = this;
  }
  get id() { return this.attributes.id || ''; }
  set textContent(value) {
    this._text = value === undefined || value === null ? '' : String(value);
    this.childNodes.forEach((child) => { child.parentNode = null; });
    this.childNodes = [];
  }
  get textContent() {
    return this._text + this.childNodes.map((child) => child.textContent).join('');
  }
  get firstChild() { return this.childNodes[0] || null; }
  appendChild(child) {
    if (child.parentNode) {
      const at = child.parentNode.childNodes.indexOf(child);
      if (at >= 0) child.parentNode.childNodes.splice(at, 1);
    }
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  insertBefore(child, before) {
    if (!before) return this.appendChild(child);
    if (child.parentNode) {
      const old = child.parentNode.childNodes.indexOf(child);
      if (old >= 0) child.parentNode.childNodes.splice(old, 1);
    }
    const at = this.childNodes.indexOf(before);
    child.parentNode = this;
    this.childNodes.splice(at < 0 ? this.childNodes.length : at, 0, child);
    return child;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] === undefined ? null : this.attributes[name]; }
  addEventListener(name, handler) { (this.listeners[name] ||= []).push(handler); }
  focus() { this.ownerDocument.activeElement = this; }
  querySelectorAll(selector) {
    const out = [];
    const visit = (node) => {
      node.childNodes.forEach((child) => {
        if (selector === '[data-focus]' && child.getAttribute('data-focus') !== null) out.push(child);
        visit(child);
      });
    };
    visit(this);
    return out;
  }
}

function makeDocument() {
  const document = {
    ids: {}, listeners: {}, visibilityState: 'visible', activeElement: null,
    createElement(tag) { return new Element(tag, document); },
    createElementNS(_ns, tag) { return new Element(tag, document); },
    createTextNode(text) { const node = new Element('#text', document); node._text = String(text); return node; },
    getElementById(id) { return document.ids[id] || null; },
    addEventListener(name, handler) { (document.listeners[name] ||= []).push(handler); },
    removeEventListener(name, handler) {
      document.listeners[name] = (document.listeners[name] || []).filter((item) => item !== handler);
    },
  };
  document.head = document.createElement('head');
  document.body = document.createElement('body');
  const root = document.createElement('div');
  root.id = 'root';
  document.body.appendChild(root);
  return { document, root };
}

function response(value, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(value),
  };
}

function walk(root) {
  const out = [root];
  root.childNodes.forEach((child) => out.push(...walk(child)));
  return out;
}

function byFocus(root, key) {
  return walk(root).find((node) => node.getAttribute('data-focus') === key);
}

function classes(root, name) {
  return walk(root).filter((node) => String(node.className || '').split(/\s+/).includes(name));
}

function allSpoken(root) {
  return walk(root).map((node) => [
    node.textContent,
    node.getAttribute('aria-label') || '',
    node.title || '',
  ].join(' ')).join(' ');
}

async function settle() {
  for (let i = 0; i < 8; i++) await new Promise((resolve) => setImmediate(resolve));
}

async function boot(view, postValue) {
  const made = makeDocument();
  const calls = [];
  const windowListeners = {};
  const disposeHooks = [];
  let intervalCallback = null;
  let intervalCleared = false;
  const window = {
    fetch(url, options = {}) {
      const method = options.method || 'GET';
      // The body is recorded only when there is one: a GET call with an extra
      // undefined field no longer equals the call the older assertions expect.
      const call = { url, method };
      if (options.body !== undefined) call.body = options.body;
      calls.push(call);
      return Promise.resolve(method === 'POST'
        ? response(postValue || { ok: true, quota_updates: [] })
        : response(view));
    },
    setInterval(callback) { intervalCallback = callback; return 17; },
    clearInterval(id) { if (id === 17) intervalCleared = true; },
    addEventListener(name, handler) { (windowListeners[name] ||= []).push(handler); },
    setTimeout: (callback, ms) => setTimeout(callback, ms),
    __ouroWidgetOnDispose(fn) { disposeHooks.push(fn); },
  };
  const context = vm.createContext({
    window,
    document: made.document,
    console,
    Date,
    Math,
    Object,
    Array,
    String,
    Number,
    RegExp,
    Promise,
    setImmediate,
  });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  await settle();
  return {
    root: made.root,
    document: made.document,
    calls,
    windowListeners,
    disposeHooks,
    testHooks: window.__quotaTest,
    interval: () => intervalCallback,
    intervalCleared: () => intervalCleared,
  };
}

function quota(overrides = {}) {
  return Object.assign({
    state: 'ok', label: '30% used', resets_at: '', note: '',
    constraints: [{
      id: 'weekly', label: 'Weekly', used_pct: 30, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800,
    }],
    stale: [], availability: 'available',
    observed_at: new Date(Date.now() - 120000).toISOString(), absence: null,
  }, overrides);
}

function account(subjectId, q, overrides = {}) {
  return Object.assign({
    key: 'claude:' + (subjectId || 'native'), kind: subjectId ? 'profile' : 'native',
    subject_id: subjectId || '', label: subjectId ? 'Same label' : 'Same label',
    caption: 'oauth', email: subjectId ? 'named@example.com' : 'native@example.com',
    plan: 'Pro', enabled: true, signed_in: true, next_up: false,
    last_verified_at: new Date(Date.now() - 3600000).toISOString(),
    verification_state: 'passed', verification_source: 'vendor', verified_live: true,
    verification: { tone: 'ok', label: 'Verified live' }, detail: '', quota: q,
  }, overrides);
}

function view(accounts) {
  return {
    ok: true, transport_error: '',
    facets: { catalog: 'ok', accounts: 'ok', quota: 'ok' }, facet_note: '',
    daemon: { state: 'running', engine_version: '3.9.4' },
    groups: [{
      harness_id: 'claude', family_label: 'Claude', harness_status: 'ok',
      harness_enabled: true, provider_family: 'anthropic', catalog_known: true,
      accounts, accounts_signed_in: accounts.length, accounts_unavailable: false,
    }],
  };
}

(async () => {
  // Fresh values keep normal bars, auth state, and quota age; auth age is not shown.
  let env = await boot(view([account('p1', quota())]));
  assert.match(env.root.textContent, /30% used/);
  assert.match(env.root.textContent, /Quota observed .* ago/);
  assert.match(env.root.textContent, /Verified live/);
  assert.doesNotMatch(allSpoken(env.root), /Checked |checked /);
  assert.equal(classes(env.root, 'stale').length, 0);

  env = await boot(view([account('p1', quota({
    state: 'no_data', label: 'No quota window reported', constraints: [],
  }))]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.match(allSpoken(env.root), /quota observed .* ago/i);
  assert.doesNotMatch(allSpoken(env.root), /Checked |checked /);

  // The same 100% constraint marked stale stays visible and amber, never exhaustion red.
  const staleConstraint = {
    id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: '',
    cooldown_until: new Date(Date.now() + 3600000).toISOString(),
    scoped_models: ['fable'], window_seconds: 604800,
  };
  env = await boot(view([account('p1', quota({
    state: 'no_fresh_window', label: 'No fresh reading — last reading is stale',
    constraints: [], observed_at: new Date(Date.now() - 360000).toISOString(),
    note: 'Stale percentages do not grant routing; live cooldown evidence may still deny or rank.',
    stale: [{
      observed_at: new Date(Date.now() - 360000).toISOString(),
      freshness: 'stale', source: 'claude_oauth_usage', constraints: [staleConstraint],
    }],
  }))]));
  assert.match(env.root.textContent, /100% used/);
  assert.match(env.root.textContent, /not used to grant routing/);
  assert.match(env.root.textContent, /cooldown/);
  assert.match(env.root.textContent, /cooldown evidence may still deny or rank/);
  assert.ok(classes(env.root, 'quota-tile').some((node) => String(node.className).includes('stale')));
  assert.equal(walk(env.root).filter((node) => String(node.className).includes('progress-fill bad')).length, 0);
  assert.doesNotMatch(env.root.textContent, /Limit reached/);

  // Fresh exhaustion remains the distinct red state.
  env = await boot(view([account('p1', quota({
    state: 'exhausted', label: 'Limit reached',
    constraints: [Object.assign({}, staleConstraint, { cooldown_until: '' })], stale: [],
  }))]));
  assert.match(env.root.textContent, /Limit reached/);
  assert.ok(walk(env.root).some((node) => String(node.className).includes('progress-fill bad')));

  // Approved absence actions only, with raw diagnostics excluded from text, ARIA, and titles.
  for (const item of [
    ['sign_in_if_unverified', false, 'Sign-in required'],
    ['sign_in_if_unverified', true, null],
    ['source_missing', true, 'No live quota source'],
    ['retry', true, 'Retry after'],
    ['', true, null],
  ]) {
    const absence = {
      message: 'Quota temporarily unavailable', action_kind: item[0],
      retry_at: new Date(Date.now() + 240000).toISOString(),
      raw_reason: 'auth_revoked', detail: '/private/secret/path vendor body',
    };
    env = await boot(view([account('p1', quota({ absence }), {
      verified_live: item[1], detail: '/private/other/account/path vendor response body',
    })]));
    const spoken = allSpoken(env.root);
    assert.match(spoken, /Quota temporarily unavailable/);
    if (item[2]) assert.match(spoken, new RegExp(item[2]));
    if (!item[2]) assert.doesNotMatch(spoken, /Sign-in required|No live quota source|Retry after/);
    assert.doesNotMatch(spoken, /auth_revoked|private\/secret|vendor body/);
  }

  // A failed accounts facet makes a previous vendor pass last-known only, so
  // a fresh typed auth_revoked absence still exposes the approved owner action.
  const revokedAfterFailedAccountsRead = view([account('p1', quota({
    absence: {
      message: 'Quota temporarily unavailable',
      action_kind: 'sign_in_if_unverified', retry_at: '',
      observed_at: new Date().toISOString(),
    },
  }), {
    verification_state: 'passed', verification_source: 'vendor',
    verified_live: false,
    verification: { tone: 'muted', label: 'Verified live — last known' },
  })]);
  revokedAfterFailedAccountsRead.facets.accounts = 'failed';
  env = await boot(revokedAfterFailedAccountsRead);
  assert.match(allSpoken(env.root), /Sign-in required/);
  assert.match(allSpoken(env.root), /Verified live — last known/);

  const degraded = view([account('p1', quota(), {
    detail: '/private/account/path raw vendor response',
  })]);
  degraded.ok = false;
  degraded.transport_error = '/private/transport/path refused vendor body';
  degraded.daemon = {
    state: 'unreachable', engine_version: '3.9.4',
    last_error: '/private/daemon/path raw provider response',
  };
  env = await boot(degraded);
  assert.doesNotMatch(allSpoken(env.root), /private\/(account|transport|daemon)|vendor body|provider response/);

  // Automatic polling stays GET-only. The explicit action is one POST, is disabled
  // while in flight, and merges only the exact named subject's quota.
  const base = view([
    account('', quota({ label: '10% used', constraints: [Object.assign({}, staleConstraint, {
      used_pct: 10, cooldown_until: '', scoped_models: [], label: 'Native',
    })] })),
    account('p1', quota({ label: '20% used', constraints: [Object.assign({}, staleConstraint, {
      used_pct: 20, cooldown_until: '', scoped_models: [], label: 'Named',
    })] })),
  ]);
  const post = {
    ok: true,
    quota_updates: [{
      harness: 'claude', subject_id: 'p1',
      quota: quota({ label: '91% used', constraints: [Object.assign({}, staleConstraint, {
        used_pct: 91, cooldown_until: '', scoped_models: [], label: 'Named refreshed',
      })] }),
    }],
  };
  env = await boot(base, post);
  const mergedDirect = env.testHooks.mergeQuotaFacet(base, post);
  function withoutQuota(value) {
    return value.groups.map((group) => Object.assign({}, group, {
      accounts: group.accounts.map((entry) => {
        const copy = Object.assign({}, entry);
        delete copy.quota;
        return copy;
      }),
    }));
  }
  assert.deepEqual(withoutQuota(mergedDirect), withoutQuota(base));
  assert.deepEqual(mergedDirect.daemon, base.daemon);
  assert.equal(mergedDirect.facets.catalog, base.facets.catalog);
  assert.equal(mergedDirect.facets.accounts, base.facets.accounts);
  assert.equal(mergedDirect.groups[0].accounts[0].quota.label, '10% used');
  assert.equal(mergedDirect.groups[0].accounts[1].quota.label, '91% used');
  const PREFIX = process.env.WIDGET_ROUTE_PREFIX;
  assert.deepEqual(env.calls, [{ url: PREFIX + 'quotas', method: 'GET' }]);
  env.interval()();
  await settle();
  assert.equal(env.calls[1].method, 'GET');
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'opt:claude:p1').listeners.click[0]({ stopPropagation() {} });
  const refresh = byFocus(env.root, 'refresh');
  refresh.listeners.click[0]();
  refresh.listeners.click[0]();
  assert.equal(byFocus(env.root, 'refresh').disabled, true);
  await settle();
  assert.equal(env.calls.filter((call) => call.method === 'POST').length, 1);
  assert.equal(env.calls.at(-1).url, PREFIX + 'refresh');
  assert.match(env.root.textContent, /91% used/);
  assert.match(env.root.textContent, /named@example.com/);
  assert.match(env.root.textContent, /Verified live/);
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'opt:claude:native').listeners.click[0]({ stopPropagation() {} });
  assert.match(env.root.textContent, /10% used/);
  assert.doesNotMatch(env.root.textContent, /91% used/);

  // Old hosts fail honestly and never fall back to a second GET.
  env = await boot(view([account('p1', quota())]), {
    ok: false, compatibility_error: true,
    message: 'Live refresh requires a newer Ouroboros host',
  });
  byFocus(env.root, 'refresh').listeners.click[0]();
  await settle();
  assert.match(env.root.textContent, /Live refresh requires a newer Ouroboros host/);
  assert.deepEqual(env.calls.map((call) => call.method), ['GET', 'POST']);

  // Each row-detail choice carries a picture of itself, and the pictures differ
  // by the one thing the choices differ by: how many lines open under the bars.
  env = await boot(view([account('p1', quota())]));
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  // An svg's class lives in the attribute, not in className: in a browser the
  // property is not a string there, so the widget sets the attribute.
  const previews = walk(env.root).filter(
    (node) => String(node.getAttribute('class') || '').includes('dens-pv'));
  assert.equal(previews.length, 3);
  const lineCounts = previews.map((pv) => pv.childNodes.filter(
    (node) => String(node.getAttribute('class') || '').includes('pv-line')).length);
  assert.deepEqual(lineCounts, [0, 2, 4]);

  // Every family is named with something in front of it — its own mark, or the
  // ring with its initial. A family the widget has no mark for is the common
  // case, not the rare one, and it must not start the row with bare text.
  const twoFamilies = view([account('p1', quota())]);
  twoFamilies.groups.push({
    harness_id: 'openrouter', family_label: 'OpenRouter', harness_status: 'ok',
    harness_enabled: true, provider_family: '', catalog_known: true,
    accounts: [], accounts_signed_in: 0, accounts_unavailable: false,
  });
  env = await boot(twoFamilies);
  assert.equal(classes(env.root, 'harness-initial').length, 1);
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'settings-tab:models').listeners.click[0]({ stopPropagation() {} });
  const familyRows = classes(env.root, 'models-family');
  assert.equal(familyRows.length, 2);
  familyRows.forEach((row) => {
    const marked = row.childNodes.some(
      (node) => String(node.getAttribute('class') || node.className || '').includes('harness-initial')
        || node.tagName === 'SVG');
    assert.ok(marked, 'a family row starts with a mark or a ring');
  });

  // The family mark answers in three colours, not by appearing: green while the
  // windows have room, amber once one is at its edge, red when nothing runs.
  const pipTone = (root) => {
    const pip = classes(root, 'seg-pip')[0];
    return pip ? String(pip.className).split(/\s+/).find((c) => ['ok', 'warn', 'bad', 'muted'].includes(c)) : null;
  };
  env = await boot(view([account('p1', quota())]));
  assert.equal(pipTone(env.root), 'ok');
  env = await boot(view([account('p1', quota({
    label: '90% used',
    constraints: [{ id: 'weekly', label: 'Weekly', used_pct: 90, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 }],
  }))]));
  assert.equal(pipTone(env.root), 'warn');
  env = await boot(view([account('p1', quota({
    state: 'exhausted', label: 'Limit reached',
    constraints: [{ id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 }],
  }))]));
  assert.equal(pipTone(env.root), 'bad');
  // Every tone the mark can carry has a colour rule of its own; an unpainted
  // pip would be an invisible answer.
  ['ok', 'warn', 'bad', 'muted'].forEach((tone) => {
    assert.ok(widgetSource.includes(`'.pip.${tone}{`), `.pip.${tone} has no colour`);
  });

  // A redraw arriving under the reader's hand must not throw them back to the
  // first row: the tree is rebuilt whole, so the list's scroll is carried over.
  env = await boot(view([account('p1', quota()), account('p2', quota())]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  const popBefore = classes(env.root, 'acct-pop')[0];
  assert.ok(popBefore, 'the account list opens');
  popBefore.scrollTop = 64;
  env.interval()();
  await settle();
  const popAfter = classes(env.root, 'acct-pop')[0];
  assert.ok(popAfter, 'the list is still open after a redraw');
  assert.notEqual(popAfter, popBefore);
  assert.equal(popAfter.scrollTop, 64);

  // A spent window's line in the list says once when it comes back. relTime
  // already answers as a phrase ("in 6d"); the line used to put a second "in"
  // in front of it, and the reader saw "in in 6d".
  const sixDays = new Date(Date.now() + 6 * 86400000).toISOString();
  env = await boot(view([
    account('p1', quota({
      state: 'exhausted', label: 'Limit reached', resets_at: sixDays,
      constraints: [{
        id: 'weekly', label: 'Weekly', used_pct: 100, resets_at: sixDays,
        cooldown_until: '', scoped_models: [], window_seconds: 604800,
      }],
    })),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  const backIn = classes(env.root, 'acct-rl-in').map((node) => node.textContent);
  assert.deepEqual(backIn, ['in 6d']);

  // Codex keeps two pools, and the engine lists their windows in an order that
  // changes from one reading to the next. The widget keeps none of it: windows
  // are grouped by pool, the pool's chip stands once in front of its own bars,
  // and the tiles follow the same order — whichever order came in.
  const CODEX = {
    'codex-week': { id: 'cw', label: 'codex primary', used_pct: 100, resets_at: sixDays,
      cooldown_until: '', scoped_models: [], window_seconds: 604800 },
    'spark-5h': { id: 's5', label: 'GPT-5.3-Codex-Spark primary', used_pct: 0, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 18000 },
    'spark-week': { id: 'sw', label: 'GPT-5.3-Codex-Spark secondary', used_pct: 0, resets_at: '',
      cooldown_until: '', scoped_models: [], window_seconds: 604800 },
  };
  const openedWith = async (order) => {
    const e = await boot(view([
      account('p1', quota({ state: 'exhausted', label: 'Limit reached', resets_at: sixDays,
        constraints: order.map((key) => CODEX[key]) })),
    ]));
    byFocus(e.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
    return e;
  };
  const groupsOf = (e) => classes(e.root, 'acct-grp').map((node) => node.textContent);
  const tilesOf = (e) => classes(e.root, 'quota-tile').map((node) => node.title);
  const linesOf = (e) => classes(e.root, 'acct-rl').map((node) => node.textContent);
  env = await openedWith(['spark-week', 'codex-week', 'spark-5h']);
  assert.deepEqual(groupsOf(env), ['codexweek100%', 'GPT-5.3-Codex-Spark5 hours0%week0%']);
  assert.deepEqual(tilesOf(env), ['codex primary', 'GPT-5.3-Codex-Spark primary', 'GPT-5.3-Codex-Spark secondary']);
  // The line under the bars names the pool beside the length, so two "week"
  // lines cannot read as one window twice; the old "windows:" footnote is gone.
  assert.equal(linesOf(env).length, 2);
  assert.match(linesOf(env)[0], /^weekcodexspent.*in 6d$/);
  assert.equal(linesOf(env)[1], '5 hoursGPT-5.3-Codex-Spark0% usedavailable');
  assert.doesNotMatch(env.root.textContent, /windows:/);
  const seenOnce = groupsOf(env);
  env = await openedWith(['spark-5h', 'spark-week', 'codex-week']);
  assert.deepEqual(groupsOf(env), seenOnce);
  assert.deepEqual(tilesOf(env), ['codex primary', 'GPT-5.3-Codex-Spark primary', 'GPT-5.3-Codex-Spark secondary']);

  // Two spellings of one pool name are two pools, side by side, in an order of
  // their own — never the order the engine happened to send them in.
  // Both spellings carry a 5-hour and a week window, so merging them by
  // case and then splitting the row by exact name would alternate the two
  // and print four chips for two pools.
  CODEX['codex-5h'] = { id: 'c5', label: 'codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 18000 };
  CODEX['Codex-5h'] = { id: 'C5', label: 'Codex primary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 18000 };
  CODEX['Codex-week'] = { id: 'Cw', label: 'Codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 604800 };
  const spelled = ['Codex5 hours0%week0%', 'codex5 hours0%week100%'];
  env = await openedWith(['codex-week', 'Codex-5h', 'codex-5h', 'Codex-week']);
  assert.deepEqual(groupsOf(env), spelled);
  env = await openedWith(['Codex-week', 'codex-5h', 'Codex-5h', 'codex-week']);
  assert.deepEqual(groupsOf(env), spelled);

  // Two windows of one length inside one pool take their role word as well,
  // and only then: the everyday row keeps "week" on its own.
  CODEX['codex-week-2'] = { id: 'cw2', label: 'codex secondary', used_pct: 0, resets_at: '',
    cooldown_until: '', scoped_models: [], window_seconds: 604800 };
  env = await openedWith(['codex-week-2', 'codex-week']);
  assert.deepEqual(groupsOf(env), ['codexweek primary100%week secondary0%']);
  assert.equal(linesOf(env).length, 2);
  assert.match(linesOf(env)[0], /^week primarycodexspent/);
  assert.equal(linesOf(env)[1], 'week secondarycodex0% usedavailable');

  // Claude names no pool for its plain windows; those stand first, and the
  // window scoped to a model follows under that model's chip.
  env = await boot(view([account('p1', quota({ constraints: [
    { id: 'f', label: '7 day (Fable)', used_pct: 100, resets_at: sixDays, cooldown_until: '',
      scoped_models: ['fable', 'claude-fable-5'], window_seconds: 604800 },
    { id: 'w', label: '7 day', used_pct: 40, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 604800 },
    { id: 'h', label: '5 hour', used_pct: 20, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 18000 },
  ] }))]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(groupsOf(env), ['5 hours20%week40%', 'Fableweek100%']);
  assert.deepEqual(tilesOf(env), ['5 hour', '7 day', '7 day (Fable)']);

  // One word for "spent": a model window cooling until a date the widget
  // cannot read is spent everywhere at once — the tile's chip, the pool's chip
  // in the row, the line under the bars, the family mark — the way plugin.py
  // counts it. Each place used to ask its own way, and the card was red while
  // the row stayed neutral.
  env = await boot(view([account('p1', quota({ constraints: [
    { id: 'f', label: '7 day (Fable)', used_pct: 10, resets_at: '', cooldown_until: 'not-a-date',
      scoped_models: ['fable'], window_seconds: 604800 },
    { id: 'w', label: '7 day', used_pct: 40, resets_at: '', cooldown_until: '', scoped_models: [], window_seconds: 604800 },
  ] }))]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.match(String(classes(env.root, 'tile-model')[0].className), /\bspent\b/);
  assert.match(String(classes(env.root, 'acct-pool')[0].className), /\bexhausted\b/);
  assert.equal(pipTone(env.root), 'warn');
  assert.deepEqual(linesOf(env), ['weekFablecooling downno reset time', 'others40% usedavailable']);

  // All three display choices travel in one body. The skill writes what it is
  // given, so a save that carried only the density would wipe the folds — and
  // the reader would find them back on after touching an unrelated setting.
  const folded = view([account('p1', quota())]);
  folded.prefs = {
    density: 'normal', models: {},
    fold: { failed: false, disabled: true, signed_out: true },
  };
  env = await boot(folded);
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'density:compact').listeners.click[0]({ stopPropagation() {} });
  await settle();
  const savedBody = JSON.parse(
    env.calls.filter((call) => call.url === PREFIX + 'prefs').at(-1).body);
  assert.equal(savedBody.density, 'compact');
  assert.deepEqual(savedBody.fold, { failed: false, disabled: true, signed_out: true });

  // A value that is not a plain yes or no leaves the account folded away. The
  // skill drops such a value too, so the two ends agree without asking.
  const junkFold = view([account('p1', quota())]);
  junkFold.prefs = { density: 'normal', models: {}, fold: { failed: 'no', disabled: null } };
  env = await boot(junkFold);
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'density:compact').listeners.click[0]({ stopPropagation() {} });
  await settle();
  const cleanedBody = JSON.parse(
    env.calls.filter((call) => call.url === PREFIX + 'prefs').at(-1).body);
  assert.deepEqual(cleanedBody.fold, { failed: true, disabled: true, signed_out: true });

  // The fold. Accounts that do not work go under one row at the bottom,
  // gathered by reason; the live ones keep the order the engine sent them in.
  const mixedAccounts = () => [
    account('live1', quota(), { label: 'live-one' }),
    account('out1', quota(), { label: 'out-one', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
    // A failed check arrives from the engine as tone 'warn' with the check
    // itself marked failed; 'bad' is never emitted by verification_view.
    account('broken', quota(), { label: 'broken-one', verification_state: 'failed',
      verified_live: false, verification: { tone: 'warn', label: 'Verification failed' } }),
    account('off1', quota(), { label: 'off-one', enabled: false }),
    account('live2', quota(), { label: 'live-two' }),
    account('out2', quota(), { label: 'out-two', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
  ];
  const namesOf = (root) => classes(root, 'acct-opt-name').map((node) => node.textContent);

  env = await boot(view(mixedAccounts()));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(namesOf(env.root), ['live-one', 'live-two']);
  assert.equal(classes(env.root, 'acct-fold-count')[0].textContent, '4');
  // Shut, the row still carries the colour of what is under it: a failed check
  // and a switched-off account are red, an account nobody logged into is not.
  assert.deepEqual(
    classes(env.root, 'acct-fold-dots')[0].childNodes.map((dot) => String(dot.className)),
    ['state-dot bad', 'state-dot bad', 'state-dot muted']);

  byFocus(env.root, 'fold').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'acct-sec-title').map((node) => node.textContent),
    ['Verification failed', 'Disabled', 'Not signed in']);
  assert.deepEqual(classes(env.root, 'acct-sec-count').map((node) => node.textContent),
    ['1', '1', '2']);
  assert.deepEqual(namesOf(env.root),
    ['live-one', 'live-two', 'broken-one', 'off-one', 'out-one', 'out-two']);
  assert.match(String(classes(env.root, 'acct-sec')[0].className), /\bbroken\b/);
  assert.doesNotMatch(String(classes(env.root, 'acct-sec')[1].className), /\bbroken\b/);

  // A reason switched off is not a reason here: those accounts stand upstairs
  // again, in the engine's own order, and the fold counts what is left.
  const partly = view(mixedAccounts());
  partly.prefs = { density: 'normal', models: {},
    fold: { failed: true, disabled: true, signed_out: false } };
  env = await boot(partly);
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(namesOf(env.root), ['live-one', 'out-one', 'live-two', 'out-two']);
  assert.equal(classes(env.root, 'acct-fold-count')[0].textContent, '2');

  // Nothing works in this family: there is nothing to fold under, so the
  // sections stand on their own and say why the list looks empty.
  env = await boot(view([
    account('out1', quota(), { label: 'out-one', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
    account('out2', quota(), { label: 'out-two', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.equal(classes(env.root, 'acct-fold').length, 0);
  assert.deepEqual(classes(env.root, 'acct-sec-title').map((node) => node.textContent),
    ['Not signed in']);
  assert.deepEqual(namesOf(env.root), ['out-one', 'out-two']);

  // The account on screen is inside the fold: the list opens with it open,
  // because a shut fold would hide the very row that is selected.
  env = await boot(view(mixedAccounts()));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'fold').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'opt:claude:out2').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.equal(byFocus(env.root, 'fold').getAttribute('aria-expanded'), 'true');
  assert.equal(classes(env.root, 'acct-sec-title').length, 3);

  // Two reasons at once is a shape the engine really produces: plugin.py
  // answers a failed check with signed_in false. foldReason takes the first of
  // them, and the account is hidden for the missing login — pinned here because
  // every order of those checks passed until this test existed.
  env = await boot(view([
    account('live1', quota(), { label: 'live-one' }),
    account('both', quota(), { label: 'both-one', signed_in: false,
      verification_state: 'failed', verified_live: false,
      verification: { tone: 'warn', label: 'Verification failed' } }),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'fold').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'acct-sec-title').map((node) => node.textContent),
    ['Not signed in']);
  assert.match(allSpoken(env.root), /both-one — not signed in/);
  // Its dot still says alert: what hides it is the missing login, what colours
  // it is the check that failed. Two different questions, two answers.
  assert.equal(String(classes(env.root, 'acct-opt')[1].childNodes[0].className),
    'state-dot bad');

  // 'bad' never comes from the engine, but the widget takes it as a failed
  // check all the same. This is the test that keeps that half of the condition
  // honest now that the fixture above stands on 'warn'.
  env = await boot(view([
    account('live1', quota(), { label: 'live-one' }),
    account('odd', quota(), { label: 'odd-one',
      verification: { tone: 'bad', label: 'Verification failed' } }),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'fold').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'acct-sec-title').map((node) => node.textContent),
    ['Verification failed']);

  // The button counts the accounts in trouble apart from the one on screen, and
  // the fold counts those same accounts among the ones it hides — in the same
  // pill the button wears. It does not open itself: an account switched off on
  // purpose would then hold the fold open for good.
  env = await boot(view(mixedAccounts()));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'acct-alarm').map((node) => node.textContent),
    ['2 need attention', '2 need attention']);
  assert.equal(byFocus(env.root, 'fold').getAttribute('aria-expanded'), 'false');
  assert.equal(byFocus(env.root, 'fold').title,
    byFocus(env.root, 'fold').getAttribute('aria-label'));
  assert.match(byFocus(env.root, 'fold').title, /4 hidden — 1 verification failed, 1 disabled, 2 not signed in · 2 need attention$/);
  // Open or shut is said by aria-expanded alone: "shown" in this list names the
  // account on screen.
  assert.doesNotMatch(byFocus(env.root, 'fold').getAttribute('aria-label'), /shown|folded/);
  // The words are a piece of their own: a list too narrow for the phrase drops
  // them and keeps the number.
  assert.deepEqual(classes(byFocus(env.root, 'fold'), 'acct-alarm-words').map((node) => node.textContent),
    [' need attention']);

  // The account on screen is one of the hidden: the button leaves it out of its
  // count, and so does the fold — the same number twice, not two numbers.
  byFocus(env.root, 'fold').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'opt:claude:off1').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.equal(byFocus(env.root, 'fold').getAttribute('aria-expanded'), 'true');
  assert.deepEqual(classes(env.root, 'acct-alarm').map((node) => node.textContent),
    ['1 needs attention', '1 needs attention']);

  // A spent account does not fold: it stands upstairs with its red dot in plain
  // sight, so the button counts it and the fold does not.
  env = await boot(view([
    account('live1', quota(), { label: 'live-one' }),
    account('spent', quota({ state: 'exhausted', label: 'Limit reached' }), { label: 'spent-one' }),
    account('off1', quota(), { label: 'off-one', enabled: false }),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'acct-alarm').map((node) => node.textContent),
    ['2 need attention', '1 needs attention']);

  // Nothing but accounts nobody logged into: there is nothing to attend to, so
  // the row carries no pill.
  env = await boot(view([
    account('live1', quota(), { label: 'live-one' }),
    account('out1', quota(), { label: 'out-one', signed_in: false,
      verification: { tone: 'muted', label: 'Not verified' } }),
  ]));
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.equal(classes(env.root, 'acct-alarm').length, 0);

  // The accounts facet did not answer: every state on screen is last known, and
  // the engine answers a failed check with a muted tone then. Folding some
  // reasons and not others under one "last known" banner is the contradiction;
  // nothing folds at all until the facet answers.
  const unread = view(mixedAccounts());
  unread.facets = { catalog: 'ok', accounts: 'not_read', quota: 'ok' };
  unread.facet_note = 'accounts: not_read';
  env = await boot(unread);
  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.equal(classes(env.root, 'acct-fold').length, 0);
  assert.deepEqual(namesOf(env.root),
    ['live-one', 'out-one', 'broken-one', 'off-one', 'live-two', 'out-two']);
  // The Accounts tab says so too: its switches change nothing until the facet
  // answers, the way the Models tab says when its choice changes nothing yet.
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'settings-tab:accounts').listeners.click[0]({ stopPropagation() {} });
  assert.match(classes(env.root, 'settings-note').map((node) => node.textContent).join(' '),
    /changes nothing yet: nothing folds until they are/);

  // The three switches. Each folds its own reason away, the choice travels to
  // the skill with the other two, and the list obeys without a second reading.
  env = await boot(view(mixedAccounts()));
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'settings-tab:accounts').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(classes(env.root, 'fold-name').map((node) => node.textContent),
    ['Verification failed', 'Disabled', 'Not signed in']);
  assert.deepEqual(classes(env.root, 'switch').map((node) => node.getAttribute('aria-checked')),
    ['true', 'true', 'true']);
  // A tab in a narrow frame can be only its icon, so every tab names itself.
  assert.deepEqual(classes(env.root, 'settings-tab').map((node) => node.title),
    ['Row detail', 'Models', 'Accounts', 'System state']);
  // Read accounts: the switches work, and the tab has nothing to excuse.
  assert.doesNotMatch(classes(env.root, 'settings-note').map((node) => node.textContent).join(' '),
    /changes nothing yet/);

  byFocus(env.root, 'fold-pref:signed_out').listeners.click[0]({ stopPropagation() {} });
  await settle();
  assert.equal(byFocus(env.root, 'fold-pref:signed_out').getAttribute('aria-checked'), 'false');
  const switched = JSON.parse(
    env.calls.filter((call) => call.url === PREFIX + 'prefs').at(-1).body);
  assert.deepEqual(switched.fold, { failed: true, disabled: true, signed_out: false });

  byFocus(env.root, 'account-btn').listeners.click[0]({ stopPropagation() {} });
  assert.deepEqual(namesOf(env.root), ['live-one', 'out-one', 'live-two', 'out-two']);
  assert.equal(classes(env.root, 'acct-fold-count')[0].textContent, '2');

  // The frame is disposable. The widget registers one dispose hook; with no
  // save in the air it has nothing to wait for, and with one it hands the host
  // a promise that settles once the save has landed.
  env = await boot(view([account('p1', quota())]));
  assert.equal(env.disposeHooks.length, 1);
  assert.equal(env.disposeHooks[0](), undefined);
  byFocus(env.root, 'settings').listeners.click[0]({ stopPropagation() {} });
  byFocus(env.root, 'density:compact').listeners.click[0]({ stopPropagation() {} });
  const flushed = env.disposeHooks[0]();
  assert.equal(typeof (flushed && flushed.then), 'function');
  await flushed;
  assert.equal(env.calls.filter((call) => call.url === PREFIX + 'prefs').length, 1);
  await settle();
  assert.equal(env.disposeHooks[0](), undefined);

  // Teardown owns the one poll timer and removes the named action listeners.
  env.windowListeners.pagehide[0]();
  assert.equal(env.intervalCleared(), true);
  assert.equal((env.document.listeners.click || []).length, 0);
  assert.equal((env.document.listeners.keydown || []).length, 0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def _widget_route_prefix(widget_path: Path) -> str:
    """The route prefix the widget actually asks for, read out of its own
    source. Asserting a literal here is what let a renamed copy of this skill
    drift away from its tests."""
    text = widget_path.read_text(encoding="utf-8")
    found = re.search(r"var ROUTE = '([^']*/)[a-z]+';", text)
    assert found, "widget.js must declare ROUTE as a single-quoted literal"
    return found.group(1)


def test_real_widget_in_process_matrix():
    candidates = [
        os.environ.get("OUROBOROSHUB_NODE", ""),
        str(Path.home() / ".claudexor" / "node" / "bin" / "node"),
        "/Applications/Claudexor.app/Contents/Resources/node",
        shutil.which("node") or "",
    ]
    node = next((Path(item) for item in candidates if item and Path(item).is_file()), None)
    assert node is not None, "a Node runtime is required for widget tests"
    widget_path = Path(__file__).with_name("widget.js").resolve()
    result = subprocess.run(
        [str(node), "-e", textwrap.dedent(NODE_WIDGET_MATRIX)],
        cwd=widget_path.parent,
        env={
            **dict(os.environ),
            "WIDGET_PATH": str(widget_path),
            # The prefix comes from the widget itself. Spelling it out a second
            # time here is how the pair drifted apart: a copy of this skill
            # under another name renamed its routes and left the assertions
            # asserting the old ones.
            "WIDGET_ROUTE_PREFIX": _widget_route_prefix(widget_path),
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

class _Api:
    """The two calls this plugin makes on the host, and nothing else."""

    def __init__(self, state_dir, broken=False):
        self._state_dir = state_dir
        self._broken = broken
        self.logged = []

    def get_state_dir(self):
        if self._broken:
            raise RuntimeError("no state dir for this skill")
        return str(self._state_dir)

    def log(self, level, message):
        self.logged.append((level, message))


ALL_FOLDED = {reason: True for reason in FOLD_REASONS}


class TestPrefs:
    def test_clean_prefs_defaults_on_junk(self):
        for junk in (None, "", 0, [], "density", {"density": "huge"}):
            assert clean_prefs(junk) == DEFAULT_PREFS

    def test_clean_prefs_keeps_known_values(self):
        cleaned = clean_prefs({"density": "detailed", "models": {"claude": "models"}})
        assert cleaned == {
            "density": "detailed", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_drops_unknown_choice_but_keeps_the_rest(self):
        cleaned = clean_prefs({
            "density": "compact",
            "models": {"claude": "models", "codex": "everything", "": "all"},
        })
        assert cleaned == {
            "density": "compact", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_ignores_a_models_value_that_is_not_a_map(self):
        assert clean_prefs({"density": "compact", "models": ["claude"]}) == {
            "density": "compact", "models": {}, "fold": ALL_FOLDED,
        }

    def test_clean_prefs_caps_the_number_of_families(self):
        many = {"h%d" % i: "models" for i in range(MAX_MODEL_ENTRIES + 20)}
        cleaned = clean_prefs({"models": many})
        assert len(cleaned["models"]) <= MAX_MODEL_ENTRIES

    def test_write_then_read_round_trips(self, tmp_path):
        api = _Api(tmp_path)
        stored, error = write_prefs(api, {"density": "compact", "models": {"claude": "shared"}})
        assert error == ""
        assert stored == {
            "density": "compact", "models": {"claude": "shared"}, "fold": ALL_FOLDED,
        }
        assert read_prefs(api) == stored

    def test_read_returns_defaults_when_nothing_was_written(self, tmp_path):
        assert read_prefs(_Api(tmp_path)) == DEFAULT_PREFS

    def test_read_survives_a_corrupt_file(self, tmp_path):
        api = _Api(tmp_path)
        (tmp_path / "prefs.json").write_text("{not json", encoding="utf-8")
        assert read_prefs(api) == DEFAULT_PREFS

    def test_a_failed_write_leaves_the_last_choice_readable(self, tmp_path, monkeypatch):
        api = _Api(tmp_path)
        write_prefs(api, {"density": "detailed", "models": {"claude": "models"}})

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(plugin.os, "replace", refuse)
        stored, error = write_prefs(api, {"density": "compact"})
        assert error.startswith("OSError")
        # The file the widget reads is the one it read before, not a half of the
        # new one; and the temporary file does not stay behind.
        assert read_prefs(api)["density"] == "detailed"
        assert [item.name for item in tmp_path.iterdir()] == ["prefs.json"]

    def test_no_state_directory_is_reported_not_raised(self, tmp_path):
        api = _Api(tmp_path, broken=True)
        stored, error = write_prefs(api, {"density": "detailed"})
        assert stored == {"density": "detailed", "models": {}, "fold": ALL_FOLDED}
        assert error == "no state directory"
        assert read_prefs(api) == DEFAULT_PREFS

    def test_stored_file_holds_only_the_cleaned_shape(self, tmp_path):
        api = _Api(tmp_path)
        write_prefs(api, {"density": "detailed", "models": {"claude": "models"}, "token": "secret"})
        written = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
        assert written == {
            "density": "detailed", "models": {"claude": "models"}, "fold": ALL_FOLDED,
        }

    def test_fold_folds_every_reason_until_the_reader_says_otherwise(self):
        assert clean_prefs({"density": "normal"})["fold"] == ALL_FOLDED

    def test_fold_keeps_a_reason_switched_off(self):
        cleaned = clean_prefs({"fold": {"failed": False}})
        assert cleaned["fold"] == {"failed": False, "disabled": True, "signed_out": True}

    def test_fold_ignores_a_value_that_is_not_a_boolean(self):
        cleaned = clean_prefs({"fold": {"failed": "no", "disabled": 0, "signed_out": None}})
        assert cleaned["fold"] == ALL_FOLDED

    def test_fold_drops_a_reason_this_skill_does_not_know(self):
        cleaned = clean_prefs({"fold": {"exhausted": False, "disabled": False}})
        assert cleaned["fold"] == {"failed": True, "disabled": False, "signed_out": True}

    def test_fold_that_is_not_a_map_leaves_every_reason_folded(self):
        for junk in (["failed"], "failed", 0, None, True):
            assert clean_prefs({"fold": junk})["fold"] == ALL_FOLDED
