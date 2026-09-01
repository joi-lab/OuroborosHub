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
  let intervalCallback = null;
  let intervalCleared = false;
  const window = {
    fetch(url, options = {}) {
      const method = options.method || 'GET';
      calls.push({ url, method });
      return Promise.resolve(method === 'POST'
        ? response(postValue || { ok: true, quota_updates: [] })
        : response(view));
    },
    setInterval(callback) { intervalCallback = callback; return 17; },
    clearInterval(id) { if (id === 17) intervalCleared = true; },
    addEventListener(name, handler) { (windowListeners[name] ||= []).push(handler); },
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


class TestPrefs:
    def test_clean_prefs_defaults_on_junk(self):
        for junk in (None, "", 0, [], "density", {"density": "huge"}):
            assert clean_prefs(junk) == DEFAULT_PREFS

    def test_clean_prefs_keeps_known_values(self):
        cleaned = clean_prefs({"density": "detailed", "models": {"claude": "models"}})
        assert cleaned == {"density": "detailed", "models": {"claude": "models"}}

    def test_clean_prefs_drops_unknown_choice_but_keeps_the_rest(self):
        cleaned = clean_prefs({
            "density": "compact",
            "models": {"claude": "models", "codex": "everything", "": "all"},
        })
        assert cleaned == {"density": "compact", "models": {"claude": "models"}}

    def test_clean_prefs_ignores_a_models_value_that_is_not_a_map(self):
        assert clean_prefs({"density": "compact", "models": ["claude"]}) == {
            "density": "compact", "models": {},
        }

    def test_clean_prefs_caps_the_number_of_families(self):
        many = {"h%d" % i: "models" for i in range(MAX_MODEL_ENTRIES + 20)}
        cleaned = clean_prefs({"models": many})
        assert len(cleaned["models"]) <= MAX_MODEL_ENTRIES

    def test_write_then_read_round_trips(self, tmp_path):
        api = _Api(tmp_path)
        stored, error = write_prefs(api, {"density": "compact", "models": {"claude": "shared"}})
        assert error == ""
        assert stored == {"density": "compact", "models": {"claude": "shared"}}
        assert read_prefs(api) == stored

    def test_read_returns_defaults_when_nothing_was_written(self, tmp_path):
        assert read_prefs(_Api(tmp_path)) == DEFAULT_PREFS

    def test_read_survives_a_corrupt_file(self, tmp_path):
        api = _Api(tmp_path)
        (tmp_path / "prefs.json").write_text("{not json", encoding="utf-8")
        assert read_prefs(api) == DEFAULT_PREFS

    def test_no_state_directory_is_reported_not_raised(self, tmp_path):
        api = _Api(tmp_path, broken=True)
        stored, error = write_prefs(api, {"density": "detailed"})
        assert stored == {"density": "detailed", "models": {}}
        assert error == "no state directory"
        assert read_prefs(api) == DEFAULT_PREFS

    def test_stored_file_holds_only_the_cleaned_shape(self, tmp_path):
        api = _Api(tmp_path)
        write_prefs(api, {"density": "detailed", "models": {"claude": "models"}, "token": "secret"})
        written = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
        assert written == {"density": "detailed", "models": {"claude": "models"}}
