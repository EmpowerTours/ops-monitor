"""Monad records alerts from the hub's GET /api/ops/spend `records` section (EmpowerTours/sawweeli-agent PR #26,
lib/proof/ops.ts). Standard library only: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402
from test_monitor import FakeGitHub  # noqa: E402

NOW = dt.datetime(2026, 11, 24, 15, 7, tzinfo=dt.timezone.utc)
ACTIVE = NOW - dt.timedelta(days=2)


def hub(**records) -> dict:
    """A /api/ops/spend body as the hub sends it, with a healthy `records` section overridden by **records."""
    base = {"day": "2026-11-24", "spent_mon": 0.042, "anchors": 3, "waiting": 0, "oldest_waiting_min": 0,
            "refill_alert": False, "cap_alert": False}
    base.update(records)
    return {"day": "2026-11-24", "llm": {"spent_usd": 0.1, "cap_usd": 0.5}, "records": base}


def rec(**records) -> dict:
    r = monitor.parse_hub_records(hub(**records))
    assert r is not None and "error" not in r, r
    return r


class ParseHubRecords(unittest.TestCase):
    def test_missing_section_is_not_configured(self) -> None:
        self.assertIsNone(monitor.parse_hub_records({"day": "2026-11-24", "llm": {"spent_usd": 0, "cap_usd": 1}}))

    def test_healthy_section(self) -> None:
        r = monitor.parse_hub_records(hub(waiting=2, oldest_waiting_min=4))
        self.assertEqual(r, {"day": "2026-11-24", "spent_mon": 0.042, "anchors": 3, "waiting": 2,
                             "oldest_waiting_min": 4.0, "refill_alert": False, "cap_alert": False,
                             "balance_mon": None, "last_alert_at": None})

    def test_alert_fields(self) -> None:
        r = monitor.parse_hub_records(hub(refill_alert=True, balance_mon=12.5, last_alert_at="2026-11-24T14:00:00.000Z"))
        self.assertTrue(r["refill_alert"])
        self.assertEqual(r["balance_mon"], 12.5)
        self.assertEqual(r["last_alert_at"], "2026-11-24T14:00:00.000Z")

    def test_hub_read_error_is_an_error_not_healthy(self) -> None:
        r = monitor.parse_hub_records({"day": "2026-11-24", "records": {"error": "unavailable"}})
        self.assertEqual(r, {"error": "hub: records unavailable"})

    def test_malformed_sections_are_errors(self) -> None:
        for bad in ("yes", [], {"waiting": 1}, {"waiting": "x", "oldest_waiting_min": 0, "refill_alert": False, "cap_alert": False},
                    {"waiting": 0, "oldest_waiting_min": 0, "refill_alert": "true", "cap_alert": False},
                    {"waiting": -1, "oldest_waiting_min": 0, "refill_alert": False, "cap_alert": False}):
            with self.subTest(bad=bad):
                r = monitor.parse_hub_records({"records": bad})
                self.assertIsNotNone(r)
                self.assertIn("error", r)

    def test_non_object_body_is_an_error(self) -> None:
        for body in (None, [], "x"):
            with self.subTest(body=body):
                self.assertIn("error", monitor.parse_hub_records(body))


class RecordsConditions(unittest.TestCase):
    def kinds(self, r: dict, now: dt.datetime = NOW, active: dt.datetime | None = ACTIVE) -> list[str]:
        return sorted(monitor.records_conditions(r, now, active))

    def test_healthy_is_quiet(self) -> None:
        self.assertEqual(self.kinds(rec()), [])
        self.assertEqual(self.kinds(rec(waiting=5, oldest_waiting_min=60)), [])

    def test_refill(self) -> None:
        c = monitor.records_conditions(rec(refill_alert=True, balance_mon=14.2), NOW, ACTIVE)
        self.assertEqual(sorted(c), ["refill"])
        self.assertIn("14.2", c["refill"])
        self.assertIn("15", c["refill"])

    def test_refill_without_balance(self) -> None:
        self.assertEqual(self.kinds(rec(refill_alert=True)), ["refill"])

    def test_reserve_reached_with_records_waiting(self) -> None:
        # the e2e case: 10 MON + 0.000001, the record is held back
        c = monitor.records_conditions(rec(refill_alert=True, balance_mon=10.000001, waiting=1, oldest_waiting_min=3),
                                       NOW, ACTIVE)
        self.assertEqual(sorted(c), ["refill", "reserve"])
        self.assertIn("paused", c["reserve"])

    def test_reserve_margin(self) -> None:
        self.assertIn("reserve", self.kinds(rec(refill_alert=True, balance_mon=10.09, waiting=1)))
        self.assertNotIn("reserve", self.kinds(rec(refill_alert=True, balance_mon=10.1, waiting=1)))

    def test_reserve_with_nothing_waiting_is_only_refill(self) -> None:
        self.assertEqual(self.kinds(rec(refill_alert=True, balance_mon=10.0, waiting=0)), ["refill"])

    def test_cap_hit(self) -> None:
        c = monitor.records_conditions(rec(cap_alert=True, waiting=4, oldest_waiting_min=20, spent_mon=9.99), NOW, ACTIVE)
        self.assertEqual(sorted(c), ["cap"])
        self.assertIn("next UTC day", c["cap"])

    def test_cap_alert_with_queue_drained_is_quiet(self) -> None:
        self.assertEqual(self.kinds(rec(cap_alert=True, waiting=0)), [])

    def test_stuck_after_activation(self) -> None:
        c = monitor.records_conditions(rec(waiting=3, oldest_waiting_min=61), NOW, ACTIVE)
        self.assertEqual(sorted(c), ["stuck"])
        self.assertIn("3 record(s)", c["stuck"])

    def test_stuck_needs_activation(self) -> None:
        self.assertEqual(self.kinds(rec(waiting=3, oldest_waiting_min=600), active=None), [])

    def test_stuck_counts_only_time_since_activation(self) -> None:
        # records queued for 10 h before the relayer was activated 30 min ago are not stuck yet
        r = rec(waiting=3, oldest_waiting_min=600)
        self.assertEqual(self.kinds(r, active=NOW - dt.timedelta(minutes=30)), [])
        self.assertEqual(self.kinds(r, active=NOW - dt.timedelta(minutes=61)), ["stuck"])
        self.assertEqual(self.kinds(r, active=NOW + dt.timedelta(hours=1)), [])

    def test_everything_at_once(self) -> None:
        r = rec(refill_alert=True, balance_mon=10.0, cap_alert=True, waiting=9, oldest_waiting_min=90)
        self.assertEqual(self.kinds(r), ["cap", "refill", "reserve", "stuck"])


class Activation(unittest.TestCase):
    def test_from_checks(self) -> None:
        checks = [{"app": "asas"}, {"app": "sawweeli-hub", "records_activated_at": "2026-11-22T10:00:00Z"}]
        self.assertEqual(monitor.records_activation(checks), dt.datetime(2026, 11, 22, 10, tzinfo=dt.timezone.utc))

    def test_unset(self) -> None:
        self.assertIsNone(monitor.records_activation([{"app": "sawweeli-hub", "records_activated_at": None}]))
        self.assertIsNone(monitor.records_activation([{"app": "sawweeli-hub"}]))
        self.assertIsNone(monitor.records_activation([]))

    def test_bad_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            monitor.records_activation([{"app": "sawweeli-hub", "records_activated_at": "next week"}])


class RecordsAlertCycle(unittest.TestCase):
    def run_once(self, gh: FakeGitHub, state: dict, r: dict | None, now: dt.datetime = NOW,
                 active: dt.datetime | None = ACTIVE) -> list[str]:
        errors: list[str] = []
        monitor.process_records_alerts(gh, state, r, now, active, errors)
        return errors

    def test_not_configured_opens_nothing(self) -> None:
        gh, state = FakeGitHub(), {}
        self.assertEqual(self.run_once(gh, state, None), [])
        self.assertEqual(gh.issues, {})
        self.assertEqual(state["records_alerts"], {})

    def test_healthy_opens_nothing(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, rec(waiting=1, oldest_waiting_min=2))
        self.assertEqual(gh.issues, {})

    def test_one_issue_per_condition_with_label(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, rec(refill_alert=True, balance_mon=10.0, cap_alert=True, waiting=9, oldest_waiting_min=90))
        self.assertEqual(gh.open_titles(), ["RECORDS CAP: sawweeli-hub", "RECORDS REFILL: sawweeli-hub",
                                            "RECORDS RESERVE: sawweeli-hub", "RECORDS STUCK: sawweeli-hub"])
        for i in gh.issues.values():
            self.assertEqual(i["labels"], ["records"])
        self.assertEqual(sorted(state["records_alerts"]), ["sawweeli-hub/cap", "sawweeli-hub/refill",
                                                           "sawweeli-hub/reserve", "sawweeli-hub/stuck"])

    def test_dedupe_while_it_lasts_then_close(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, rec(refill_alert=True, balance_mon=13.0))
        n = state["records_alerts"]["sawweeli-hub/refill"]["issue"]
        self.assertIn("13", gh.issues[n]["body"])
        for _ in range(3):  # later runs: no new issue, no comment
            self.run_once(gh, state, rec(refill_alert=True, balance_mon=12.0), now=NOW + dt.timedelta(minutes=15))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.comments, [])
        self.run_once(gh, state, rec(), now=NOW + dt.timedelta(hours=1))
        self.assertEqual(gh.open_titles(), [])
        self.assertNotIn("sawweeli-hub/refill", state["records_alerts"])
        self.assertEqual(gh.comments[-1][0], n)
        self.assertIn("Closing", gh.comments[-1][1])

    def test_existing_open_issue_is_reused(self) -> None:
        gh, state = FakeGitHub(), {}
        n = gh.create_issue("RECORDS CAP: sawweeli-hub", "old", ["records"])
        self.run_once(gh, state, rec(cap_alert=True, waiting=2))
        self.assertEqual(state["records_alerts"]["sawweeli-hub/cap"]["issue"], n)
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.comments[-1][0], n)

    def test_unreadable_keeps_open_issues(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, rec(cap_alert=True, waiting=2))
        self.run_once(gh, state, {"error": "hub GET /api/ops/spend -> HTTP 502"})
        self.assertEqual(gh.open_titles(), ["RECORDS CAP: sawweeli-hub"])
        self.assertEqual(gh.comments, [])

    def test_section_removed_closes_as_not_configured(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, rec(cap_alert=True, waiting=2))
        self.run_once(gh, state, None)
        self.assertEqual(gh.open_titles(), [])
        self.assertIn("not configured", gh.comments[-1][1])
        self.assertEqual(state["records_alerts"], {})

    def test_api_error_is_reported_not_raised(self) -> None:
        class Broken(FakeGitHub):
            def create_issue(self, *a, **k):  # type: ignore[override]
                raise RuntimeError("GitHub API POST /issues -> HTTP 500")
        state: dict = {}
        errors = self.run_once(Broken(), state, rec(cap_alert=True, waiting=2))
        self.assertEqual(len(errors), 1)
        self.assertEqual(state["records_alerts"], {})

    def test_state_counts_as_significant(self) -> None:
        a = monitor.significant({"apps": {}, "outages": []})
        b = monitor.significant({"apps": {}, "outages": [],
                                 "records_alerts": {"sawweeli-hub/cap": {"issue": 3, "since": "2026-11-24T15:07Z"}}})
        self.assertNotEqual(a, b)


class RecordsSummary(unittest.TestCase):
    def test_line(self) -> None:
        line = monitor.records_line(rec(waiting=2, oldest_waiting_min=7, refill_alert=True, balance_mon=13.5))
        self.assertIn("2 waiting", line)
        self.assertIn("13.5", line)
        self.assertIn("refill", line)
        self.assertEqual(monitor.records_line(None), "not configured on the hub")
        self.assertIn("unavailable", monitor.records_line({"error": "hub: records unavailable"}))

    def test_daily_summary_includes_records(self) -> None:
        body = monitor.summary_body([{"app": "asas"}], {"asas": {"ms": 100, "cert_days": 70, "ok": True}},
                                    {"outages": []}, {}, NOW, records=rec(waiting=1))
        self.assertIn("Monad records", body)
        body = monitor.summary_body([{"app": "asas"}], {"asas": {"ms": 100, "cert_days": 70, "ok": True}},
                                    {"outages": []}, {}, NOW)
        self.assertNotIn("Monad records", body)


class MainWiring(unittest.TestCase):
    """monitor.main() end to end with the network and GitHub faked (10:00 UTC, before the daily summary): the hub's
    one /api/ops/spend response feeds both the spend rows and the records alerts, and a hub without `records` opens
    nothing."""

    def run_main(self, hub_body: object, gh: FakeGitHub, tmp: str) -> int:
        from unittest import mock
        checks = [{"app": "sawweeli-hub", "url": "https://x.invalid/", "records_activated_at": "2026-11-20T00:00:00Z"}]
        gh.ensure_label = lambda *a: None  # type: ignore[attr-defined]
        gh.try_pin = lambda n: None  # type: ignore[attr-defined]
        real_load = monitor.load_json
        env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "OPS_TOKEN": "o"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(monitor, "load_json", side_effect=lambda p, d: checks if p == monitor.CHECKS_FILE else real_load(p, d)), \
                mock.patch.object(monitor, "STATE_FILE", os.path.join(tmp, "status.json")), \
                mock.patch.object(monitor, "SAMPLES_FILE", os.path.join(tmp, "samples.json")), \
                mock.patch.object(monitor, "run_checks", return_value=[{"app": "sawweeli-hub", "ok": True, "ms": 5,
                                                                        "cert_days": 60, "reasons": []}]), \
                mock.patch.object(monitor, "table", return_value=""), \
                mock.patch.object(monitor, "hub_call", return_value=hub_body), \
                mock.patch.object(monitor, "GitHub", return_value=gh), \
                mock.patch.object(monitor, "now_utc", return_value=NOW.replace(hour=10)), \
                mock.patch.object(monitor, "voice_keepalive", return_value="skipped"), \
                mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": "", "GITHUB_OUTPUT": ""}):
            return monitor.main()

    def test_records_alerts_from_main(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub()
            body = hub(cap_alert=True, waiting=4, oldest_waiting_min=90)
            self.assertEqual(self.run_main(body, gh, tmp), 0)
            self.assertEqual(gh.open_titles(), ["RECORDS CAP: sawweeli-hub", "RECORDS STUCK: sawweeli-hub"])
            # next run, same reading: deduped through the saved state
            self.assertEqual(self.run_main(body, gh, tmp), 0)
            self.assertEqual(len(gh.issues), 2)
            self.assertEqual(gh.comments, [])
            # the cap clears (records went out): its issue closes, the stuck one too
            self.assertEqual(self.run_main(hub(cap_alert=True, waiting=0), gh, tmp), 0)
            self.assertEqual(gh.open_titles(), [])

    def test_hub_without_records_opens_nothing(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gh = FakeGitHub()
            self.assertEqual(self.run_main({"day": "2026-11-24", "llm": {"spent_usd": 0, "cap_usd": 1}}, gh, tmp), 0)
            self.assertEqual(gh.issues, {})


if __name__ == "__main__":
    unittest.main()
