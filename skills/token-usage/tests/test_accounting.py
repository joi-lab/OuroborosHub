"""Public synthetic accounting acceptance cases; no runtime data access."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from accounting import query_snapshot, summarize, export_snapshot
from oracle import TOKEN_FIELDS, assert_subset_equal, physical_summary, verify_fixture, independent_summary, verify_export

FIXTURE_PATH = ROOT / "fixtures" / "numeric_snapshot.json"
NOW = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)


class AccountingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def query(self, **params):
        return query_snapshot(self.fixture, {"period": "all", **params}, now=NOW)

    def row(self, attempt_id):
        return next(row for row in self.fixture["rows"] if row["attempt_id"] == attempt_id)

    def test_independent_oracle_matches_frozen_numbers(self):
        verify_fixture(FIXTURE_PATH)

    def test_complete_snapshot_matches_every_frozen_count_and_null(self):
        response = self.query()
        assert_subset_equal(self.fixture["oracle"]["all"], response["summary"])
        self.assertTrue(response["snapshot_id"])
        self.assertIn("query", response)
        self.assertIn("coverage", response)

    def test_summarize_matches_independent_decimal_oracle(self):
        actual = summarize(self.fixture["rows"])
        assert_subset_equal(physical_summary(self.fixture["rows"]), actual)

    def test_null_is_not_measured_zero_for_each_token_field(self):
        unknown = summarize([self.row("undated")])
        zero = summarize([self.row("measured-zero")])
        for field in TOKEN_FIELDS:
            assert_subset_equal({"value": None, "known": 0, "missing": 1}, unknown["metrics"][field], field)
            assert_subset_equal({"value": 0, "known": 1, "missing": 0}, zero["metrics"][field], field)
        self.assertIsNone(unknown["reported_tokens"])
        self.assertEqual(zero["reported_tokens"], 0)
        self.assertEqual(unknown["fully_reported_rows"], 0)
        self.assertEqual(zero["fully_reported_rows"], 1)

    def test_partial_zero_output_retains_unknown_input_and_missing_counts(self):
        summary = summarize([self.row("null-child")])
        assert_subset_equal({"value": None, "known": 0, "missing": 1}, summary["metrics"]["prompt_tokens"])
        assert_subset_equal({"value": 0, "known": 1, "missing": 0}, summary["metrics"]["completion_tokens"])
        assert_subset_equal({"value": None, "known": 0, "missing": 1}, summary["metrics"]["cache_write_tokens"])
        self.assertEqual(summary["reported_tokens"], 0)
        self.assertEqual(summary["fully_reported_rows"], 0)

    def test_empty_selection_is_unknown_with_no_missing_observations(self):
        summary = self.query(model="does-not-exist")["summary"]
        self.assertEqual(summary["physical_calls"], 0)
        self.assertIsNone(summary["reported_tokens"])
        for field in TOKEN_FIELDS:
            assert_subset_equal({"value": None, "known": 0, "missing": 0}, summary["metrics"][field])

    def test_legacy_cache_larger_than_input_is_preserved_and_not_added(self):
        summary = summarize([self.row("legacy-input")])
        self.assertEqual(summary["metrics"]["prompt_tokens"]["value"], 15)
        self.assertEqual(summary["metrics"]["cached_tokens"]["value"], 3037316)
        self.assertEqual(summary["metrics"]["cache_write_tokens"]["value"], 448991)
        self.assertEqual(summary["reported_tokens"], 39843)

    def test_native_input_already_includes_cache_so_no_double_count(self):
        summary = summarize([self.row("native-child")])
        self.assertEqual(summary["metrics"]["prompt_tokens"]["value"], 1200)
        self.assertEqual(summary["reported_tokens"], 1500)
        self.assertNotEqual(summary["reported_tokens"], 2300)

    def test_subscriptions_and_legacy_are_never_physical_calls(self):
        rows = [row for row in self.fixture["rows"] if row["kind"] != "attempt"]
        summary = summarize(rows)
        self.assertEqual(summary["rows"], 0)
        self.assertEqual(summary["physical_calls"], 0)
        self.assertIsNone(summary["reported_tokens"])
        self.assertEqual(summary["subscription_sessions"], 2)
        self.assertEqual(summary["unknown_subscription_sessions"], 1)
        self.assertEqual(summary["subscription_summary"]["reported_tokens"], 1777776)
        self.assertIsNone(summary["metrics"]["cost_confirmed_usd"]["value"])

    def test_reserved_and_released_are_not_physical_or_held_call_totals(self):
        rows = [self.row("reservation-only"), self.row("released-reservation")]
        summary = summarize(rows)
        self.assertEqual(summary["physical_calls"], 0)
        self.assertIsNone(summary["metrics"]["cost_held_usd"]["value"])

    def test_own_and_descendants_are_disjoint_exact_root_selections(self):
        own = self.query(task="root-a", scope="own")
        descendants = self.query(task="root-a", scope="descendants")
        assert_subset_equal(self.fixture["oracle"]["own_root_a"], own["summary"])
        assert_subset_equal(self.fixture["oracle"]["descendants_root_a"], descendants["summary"])
        own_ids = {row["attempt_id"] for row in own["details"]["rows"]}
        descendant_ids = {row["attempt_id"] for row in descendants["details"]["rows"]}
        self.assertFalse(own_ids & descendant_ids)
        self.assertNotIn("similar-prefix", own_ids | descendant_ids)
        self.assertIn("native-child", descendant_ids)
        self.assertIn("null-child", descendant_ids)

    def test_model_route_project_and_task_intersection_before_pagination(self):
        params = {"model": "native-test", "route": "launched-native", "project": "aurora", "task": "root-a", "scope": "descendants", "page_size": 1}
        first, second = self.query(page=1, **params), self.query(page=2, **params)
        self.assertEqual(first["summary"], second["summary"])
        self.assertEqual(first["trend"], second["trend"])
        self.assertEqual(first["rankings"], second["rankings"])
        self.assertEqual(first["summary"]["physical_calls"], 2)
        self.assertEqual(first["summary"]["reported_tokens"], 1500)
        self.assertEqual(len(first["details"]["rows"]), 1)
        self.assertEqual(len(second["details"]["rows"]), 1)
        self.assertGreaterEqual(first["details"]["total"], 2)
        self.assertNotEqual(first["details"]["rows"], second["details"]["rows"])
        for row in first["details"]["rows"] + second["details"]["rows"]:
            self.assertEqual(row["model"], "native-test")
            self.assertEqual(row["project_id"], "aurora")
            self.assertEqual(row["root_task_id"], "root-a")
            self.assertNotEqual(row["task_id"], "root-a")

    def test_recorded_route_does_not_infer_provider_or_source(self):
        self.assertEqual(self.query(route="anthropic")["summary"]["physical_calls"], 0)
        self.assertEqual(self.query(route="claude_code.readonly")["summary"]["physical_calls"], 0)
        actual = self.query(route="__unknown__")["summary"]
        self.assertEqual(actual["physical_calls"], 6)
        self.assertEqual(actual["reported_tokens"], 39890)

    def test_unknown_model_and_unassigned_project_are_explicit(self):
        unknown = self.query(model="__unknown__")["summary"]
        self.assertEqual(unknown["physical_calls"], 1)
        self.assertIsNone(unknown["reported_tokens"])
        unassigned = self.query(project="__unassigned__")["summary"]
        self.assertEqual(unassigned["physical_calls"], 2)
        self.assertIsNone(unassigned["reported_tokens"])

    def test_period_presets_use_utc_half_open_boundaries(self):
        hour = self.query(period="1h")
        day = self.query(period="24h")
        assert_subset_equal(self.fixture["oracle"]["hour"], hour["summary"])
        assert_subset_equal(self.fixture["oracle"]["day"], day["summary"])
        hour_ids = {row["attempt_id"] for row in hour["details"]["rows"]}
        self.assertIn("native-child", hour_ids)
        self.assertNotIn("before-hour", hour_ids)
        self.assertNotIn("measured-zero", hour_ids)
        self.assertNotIn("undated", hour_ids)
        self.assertIn("estimated-day-boundary", {row["attempt_id"] for row in day["details"]["rows"]})

    def test_custom_offset_calendar_range_matches_same_utc_window(self):
        utc = self.query(period="custom", start="2026-09-10T09:00:00Z", end="2026-09-10T10:00:00Z")
        local = self.query(period="custom", start="2026-09-10T12:00:00+03:00", end="2026-09-10T13:00:00+03:00")
        self.assertEqual(utc["summary"], local["summary"])
        self.assertEqual(utc["trend"], local["trend"])
        self.assertEqual(utc["query"]["start"], local["query"]["start"])
        self.assertEqual(utc["query"]["end"], local["query"]["end"])
        assert_subset_equal(self.fixture["oracle"]["hour"], local["summary"])

    def test_all_history_keeps_undated_separate_and_includes_old_retained_rows(self):
        response = self.query()
        self.assertEqual(response["summary"]["undated_rows"], 1)
        ids = {row["attempt_id"] for row in response["details"]["rows"]}
        self.assertIn("legacy-input", ids)
        self.assertIn("undated", ids)

    def test_invalid_custom_ranges_rejected_instead_of_returning_empty(self):
        for start, end in (("invalid", "2026-09-10T10:00:00Z"), ("2026-09-10T10:00:00Z", "2026-09-10T09:00:00Z"), ("2026-09-10T10:00:00Z", "2026-09-10T10:00:00Z"), ("2026-09-10T09:00:00", "2026-09-10T10:00:00")):
            with self.subTest(start=start, end=end):
                with self.assertRaises((ValueError, TypeError)):
                    self.query(period="custom", start=start, end=end)

    def test_missing_cost_finality_never_invents_estimate_or_confirmation(self):
        row = copy.deepcopy(self.row("native-child"))
        row.pop("cost_final")
        row["cost_usd"] = 3.5
        summary = summarize([row])
        self.assertEqual(summary["unknown_cost_rows"], 1)
        self.assertIsNone(summary["metrics"]["cost_confirmed_usd"]["value"])
        self.assertIsNone(summary["metrics"]["cost_estimated_usd"]["value"])

    def test_settled_unknown_cost_does_not_reuse_previous_reservation(self):
        summary = summarize([self.row("null-child")])
        self.assertEqual(summary["unknown_cost_rows"], 1)
        self.assertIsNone(summary["metrics"]["cost_held_usd"]["value"])
        assert_subset_equal({"value": None, "known": 0, "missing": 1}, summary["metrics"]["cost_estimated_usd"])

    def test_pure_queries_do_not_mutate_frozen_snapshot(self):
        before = copy.deepcopy(self.fixture)
        first = self.query()
        self.query(task="root-a", scope="descendants", page_size=1)
        second = self.query()
        self.assertEqual(self.fixture, before)
        self.assertEqual(first, second)

    def test_partial_coverage_is_preserved_in_query_response(self):
        self.fixture["coverage"] = {"status": "partial", "issues": ["archive hash mismatch"]}
        response = self.query()
        self.assertEqual(response["coverage"]["status"], "partial")
        dates = sorted(row["ts"] for row in self.fixture["rows"] if row.get("ts"))
        self.assertEqual(response["coverage"]["retained_start"], dates[0])
        self.assertEqual(response["coverage"]["retained_end"], dates[-1])
        self.assertEqual(response["coverage"]["retained_undated_rows"], 1)
        self.assertIn("archive hash mismatch", response["coverage"]["issues"])

    def test_single_timestamp_trend_has_positive_contiguous_intervals(self):
        self.fixture["rows"] = [self.row("native-child")]
        response = self.query()
        self.assertTrue(response["trend"])
        previous_end = None
        calls = 0
        for bucket in response["trend"]:
            start = datetime.fromisoformat(bucket["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(bucket["end"].replace("Z", "+00:00"))
            self.assertLess(start, end)
            if previous_end is not None:
                self.assertEqual(start, previous_end)
            previous_end = end
            calls += bucket["summary"]["physical_calls"]
        self.assertEqual(calls, 1)

    def test_trend_and_rankings_conserve_filtered_physical_token_counts(self):
        response = self.query(period="24h", project="aurora")
        expected = response["summary"]
        for field in TOKEN_FIELDS:
            for groups in (response["trend"], *response["rankings"].values()):
                measurements = [group["summary"]["metrics"][field] for group in groups]
                values = [measurement["value"] for measurement in measurements if measurement["value"] is not None]
                value = sum(values) if values else None
                self.assertEqual(value, expected["metrics"][field]["value"])
                self.assertEqual(sum(measurement["known"] for measurement in measurements), expected["metrics"][field]["known"])
                self.assertEqual(sum(measurement["missing"] for measurement in measurements), expected["metrics"][field]["missing"])

    def test_large_integer_totals_are_exact_and_have_lossless_display_strings(self):
        row = copy.deepcopy(self.row("native-child"))
        row["prompt_tokens"] = 2**53 + 1
        row["completion_tokens"] = 2**53 + 3
        summary = summarize([row, copy.deepcopy(row)])
        expected_input, expected_output = 2 * (2**53 + 1), 2 * (2**53 + 3)
        self.assertEqual(summary["metrics"]["prompt_tokens"]["value"], expected_input)
        self.assertEqual(summary["metrics"]["prompt_tokens"]["exact"], str(expected_input))
        self.assertEqual(summary["reported_tokens"], expected_input + expected_output)
        self.assertEqual(summary["reported_tokens_exact"], str(expected_input + expected_output))
        assert_subset_equal(physical_summary([row, copy.deepcopy(row)]), summary)

    def test_invalid_token_values_are_unknown_instead_of_coerced(self):
        for value in (True, False, -1, 1.5, "12", None):
            with self.subTest(value=value):
                row = copy.deepcopy(self.row("native-child"))
                for field in TOKEN_FIELDS:
                    row[field] = value
                summary = summarize([row])
                for field in TOKEN_FIELDS:
                    assert_subset_equal({"value": None, "known": 0, "missing": 1}, summary["metrics"][field])
                self.assertIsNone(summary["reported_tokens"])

    def test_7d_and_30d_presets_retain_full_range_without_legacy_july(self):
        for period in ("7d", "30d"):
            with self.subTest(period=period):
                response = self.query(period=period)
                assert_subset_equal(self.fixture["oracle"]["day"], response["summary"])
                self.assertNotIn("legacy-input", {row["attempt_id"] for row in response["details"]["rows"]})


class NormalizedSessionTests(unittest.TestCase):
    def session(self, normalized=None, **changes):
        return dict(kind="subscription_session", attempt_id="session", ts="2026-09-10T09:30:00Z",
                    prompt_tokens=900, completion_tokens=3, cached_tokens=700, cache_write_tokens=50,
                    input_token_usage=normalized, **changes)

    def test_whole_object_contract_and_independent_oracle(self):
        valid = {"total_tokens": 100, "cache_read_tokens": 70, "cache_write_tokens": 5}
        cases = [None, {}, {"total_tokens": 100}, {**valid, "extra": 0}]
        for field in valid:
            for invalid in (True, False, -1, 0.5, "1", [], {}):
                cases.append({**valid, field: invalid})
        for value in cases:
            with self.subTest(value=value):
                row = self.session(value)
                result = summarize([row])
                session = result["subscription_summary"]
                self.assertEqual(session["reported_tokens"], 903)
                self.assertEqual(session["normalized_input_usage"], {"known": 0, "missing": 1})
                self.assertIsNone(session["normalized_metrics"]["total_tokens"]["value"])
                self.assertEqual(session["legacy_input_sessions"], 1)
                assert_subset_equal(independent_summary([row]), result)
        for value in (valid, {field: None for field in valid}, {field: 0 for field in valid},
                      {**valid, "total_tokens": 2**80 + 1}):
            with self.subTest(value=value):
                row = self.session(value)
                result = summarize([row])
                session = result["subscription_summary"]
                self.assertEqual(session["normalized_input_usage"], {"known": 1, "missing": 0})
                self.assertEqual(session["metrics"]["prompt_tokens"]["value"], value["total_tokens"])
                self.assertEqual(session["reported_tokens"], (value["total_tokens"] or 0) + 3)
                self.assertEqual(session["legacy_metrics"]["prompt_tokens"]["value"], 900)
                self.assertEqual(session["legacy_input_sessions"], 0)
                assert_subset_equal(independent_summary([row]), result)

    def test_normalized_input_independent_of_absent_legacy_and_exact_export(self):
        huge = 2**80 + 7
        row = self.session({"total_tokens": huge, "cache_read_tokens": huge - 1, "cache_write_tokens": None})
        del row["prompt_tokens"]
        del row["cached_tokens"]
        snapshot = {"rows": [row]}
        response = query_snapshot(snapshot, {}, NOW)
        summary = response["summary"]["subscription_summary"]
        self.assertEqual(summary["reported_tokens_exact"], str(huge + 3))
        self.assertEqual(summary["normalized_metrics"]["total_tokens"]["exact"], str(huge))
        for document_row in (response["details"]["rows"][0], export_snapshot(snapshot, {}, NOW)["rows"][0]):
            self.assertEqual(document_row["input_token_usage_exact"]["total_tokens"], str(huge))
            self.assertEqual(document_row["session_token_exact"]["prompt_tokens"], str(huge))
            self.assertIsNone(document_row["input_token_usage_exact"]["cache_write_tokens"])
        verify_export(export_snapshot(snapshot, {}, NOW))
        self.assertIsNone(response["summary"]["reported_tokens"])

    def test_oracle_rejects_rehashed_tampered_exact_companions(self):
        huge = 2**80 + 7
        normalized = self.session({"total_tokens": huge, "cache_read_tokens": 9, "cache_write_tokens": None})
        legacy = self.session()
        document = export_snapshot({"rows": [normalized, legacy]}, {}, NOW)
        verify_export(document)
        paths = [
            ("rows", 0, "session_token_exact", "prompt_tokens"),
            ("rows", 0, "session_token_exact", "cached_tokens"),
            ("rows", 0, "session_token_exact", "cache_write_tokens"),
            ("rows", 0, "session_token_exact", "completion_tokens"),
            ("rows", 1, "session_token_exact", "prompt_tokens"),
            ("rows", 1, "session_token_exact", "cached_tokens"),
            ("summary", "reported_tokens_exact"),
            ("summary", "metrics", "prompt_tokens", "exact"),
            ("summary", "subscription_summary", "reported_tokens_exact"),
            ("summary", "subscription_summary", "metrics", "prompt_tokens", "exact"),
            ("summary", "subscription_summary", "normalized_metrics", "total_tokens", "exact"),
            ("summary", "subscription_summary", "normalized_metrics", "cache_write_tokens", "exact"),
            ("summary", "subscription_summary", "legacy_metrics", "prompt_tokens", "exact"),
        ]
        for path in paths:
            with self.subTest(path=path):
                tampered = copy.deepcopy(document)
                target = tampered
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = "123456789"
                content = {key: value for key, value in tampered.items() if key != "content_sha256"}
                tampered["content_sha256"] = hashlib.sha256(json.dumps(content, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()
                with self.assertRaisesRegex(AssertionError, "exact"):
                    verify_export(tampered)

    def test_session_rankings_trend_and_tree_conservation_after_filters(self):
        rows = []
        for i, total in enumerate((40, 100, 20)):
            row = self.session({"total_tokens": total, "cache_read_tokens": total, "cache_write_tokens": 0},
                               task_id="root" if i == 0 else "child" + str(i), root_task_id="root",
                               subscription_route="harness-a" if i != 2 else "harness-b", model="provider-model", project_id="42")
            row["attempt_id"] = str(i)
            rows.append(row)
        snapshot = {"rows": rows}
        response = query_snapshot(snapshot, {"task": "root"}, NOW)
        self.assertEqual(response["session_rankings"]["routes"][0]["id"], "harness-a")
        self.assertEqual(response["session_rankings"]["routes"][0]["summary"]["reported_tokens"], 146)
        self.assertEqual(response["trend"], [])
        self.assertEqual(response["rankings"]["routes"], [])
        for params in ({}, {"route": "harness-a", "model": "provider-model", "project": "42", "task": "root"},
                       {"task": "root", "scope": "own"}, {"task": "root", "scope": "descendants"}):
            result = query_snapshot(snapshot, params, NOW)
            expected = result["summary"]["subscription_summary"]
            for groups in (result["session_trend"], *result["session_rankings"].values()):
                for field in TOKEN_FIELDS:
                    metrics = [group["summary"]["metrics"][field] for group in groups]
                    self.assertEqual(sum(m["value"] or 0 for m in metrics), expected["metrics"][field]["value"])
                    self.assertEqual(sum(m["known"] for m in metrics), expected["metrics"][field]["known"])
                    self.assertEqual(sum(m["missing"] for m in metrics), expected["metrics"][field]["missing"])
        own = query_snapshot(snapshot, {"task": "root", "scope": "own"}, NOW)["summary"]["subscription_summary"]
        children = query_snapshot(snapshot, {"task": "root", "scope": "descendants"}, NOW)["summary"]["subscription_summary"]
        for field in TOKEN_FIELDS:
            self.assertEqual(own["metrics"][field]["value"] + children["metrics"][field]["value"],
                             response["summary"]["subscription_summary"]["metrics"][field]["value"])
        self.assertEqual(query_snapshot(snapshot, {"route": "provider-model"}, NOW)["session_trend"], [])

    def test_every_preset_exact_start_end_and_local_calendar_dst(self):
        for period, duration in (("1h", timedelta(hours=1)), ("24h", timedelta(days=1)),
                                 ("7d", timedelta(days=7)), ("30d", timedelta(days=30))):
            start = NOW - duration
            rows = []
            for name, timestamp in (("before", start - timedelta(microseconds=1)), ("start", start),
                                    ("inside", NOW - timedelta(microseconds=1)), ("end", NOW), ("undated", None)):
                row = self.session()
                row.update(attempt_id=name, ts=timestamp.isoformat() if timestamp else None)
                rows.append(row)
            result = query_snapshot({"rows": rows}, {"period": period}, NOW)
            self.assertEqual({r["attempt_id"] for r in result["details"]["rows"]}, {"start", "inside"})
            self.assertEqual(sum(b["summary"]["rows"] for b in result["session_trend"]), 2)
            self.assertEqual(query_snapshot({"rows": rows}, {}, NOW)["summary"]["subscription_summary"]["rows"], 5)
        # A local calendar day across DST is 23 hours; offsets belong to each boundary.
        params = {"period": "custom", "start": "2026-03-08T00:00:00-05:00", "end": "2026-03-09T00:00:00-04:00"}
        rows = []
        for timestamp in ("2026-03-08T04:59:59Z", "2026-03-08T05:00:00Z", "2026-03-09T03:59:59Z", "2026-03-09T04:00:00Z"):
            row = self.session()
            row["ts"] = timestamp
            rows.append(row)
        result = query_snapshot({"rows": rows}, params, NOW)
        self.assertEqual(result["summary"]["subscription_sessions"], 2)
        self.assertEqual(result["query"]["start"], "2026-03-08T05:00:00Z")
        self.assertEqual(result["query"]["end"], "2026-03-09T04:00:00Z")


if __name__ == "__main__":
    unittest.main()
