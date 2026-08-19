"""test_quotas.py — Exhaustive unit tests for claudexor_quotas plugin data normalization.

Tests all pure projection logic:
- Per-facet provenance and notes (ok, not_read, failed, indeterminate, missing reads).
- Timestamp and future-detection handling.
- Constraint views, ratio clamping, and NaN handling.
- Quota projection: fresh, stale, no-data, degraded facet, global exhaustion vs per-model caps.
- Verification view tones and "last known" degradation.
- Account and group structuring (native vs profile, next_up resolution, harness ordering).
- Full view building and transport error handling.
"""

import datetime as dt
import math
import pytest

from plugin import (
    FACETS,
    READ_OK,
    READ_STATES,
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
        assert native_acc["next_up"] is True
        assert native_acc["quota"]["state"] == "ok"

        prof_acc = group["accounts"][1]
        assert prof_acc["kind"] == "profile"
        assert prof_acc["next_up"] is False
        assert prof_acc["verification"]["label"] == "Verified live"
        assert prof_acc["quota"]["state"] == "no_data"

    def test_build_view_with_transport_error(self):
        view = build_view(None, "Connection refused")
        assert view["ok"] is False
        assert view["transport_error"] == "Connection refused"
        assert view["facets"] == {f: "indeterminate" for f in FACETS}
        assert view["groups"] == []
