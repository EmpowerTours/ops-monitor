"""Unit tests for monitor.py. Standard library only: python3 -m unittest discover -s tests -v

Fixtures in tests/fixtures/readyz-*.txt are the real /readyz bodies of the seven apps, captured with plain GETs on
2026-11-19."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    with open(os.path.join(FIX, f"readyz-{name}.txt"), encoding="utf-8") as f:
        return f.read()


class ReadyzParsers(unittest.TestCase):
    EXPECTED = {  # app: (day, spent, reserved, cap) as captured
        "asas": ("2026-11-20", 0.0, 0.0, 4.0),
        "faez": ("2026-11-19", 0.0, 0.0, 0.75),
        "cosecha": ("2026-11-19", 0.0, 0.0, 1.0),
        "haqiqi": ("2026-11-19", 0.0, 0.0, 1.0),
        "relocatedubai": ("2026-11-19", 0.0, 0.0, 1.0),
        "hunt-dubai": ("2026-11-19", 0.0, 0.0, 0.5),
        "sawweeli": ("2026-11-19", 0.0, 0.0, 0.5),
    }

    def test_every_captured_fixture_parses(self) -> None:
        for app, (day, spent, reserved, cap) in self.EXPECTED.items():
            with self.subTest(app=app):
                r = monitor.parse_readyz(fixture(app))
                self.assertIsNotNone(r)
                self.assertEqual(r, {"day": day, "spent_usd": spent, "reserved_usd": reserved, "cap_usd": cap})

    def test_text_line_with_nonzero_amounts(self) -> None:
        body = ("ready\nai_spend day=2026-11-19 spent=USD 3.21 reserved=USD 0.04 cap=USD 4.00 remaining=USD 0.75 "
                "month_committed=USD 9.00 month_cap=USD 40.00 closed=false\n")
        r = monitor.parse_readyz(body)
        self.assertEqual(r, {"day": "2026-11-19", "spent_usd": 3.21, "reserved_usd": 0.04, "cap_usd": 4.0})
        self.assertAlmostEqual(monitor.spend_pct(r), 81.25)

    def test_text_line_without_reserved(self) -> None:
        r = monitor.parse_readyz("ai_spend day=2026-11-19 spent=USD 0.10 cap=USD 1.00")
        self.assertEqual(r["reserved_usd"], 0.0)

    def test_json_with_string_numbers(self) -> None:
        r = monitor.parse_readyz('{"ai_spend":{"day":"2026-11-19","spent_usd":"0.4","reserved_usd":"0","cap_usd":"0.5"}}')
        self.assertAlmostEqual(monitor.spend_pct(r), 80.0)

    def test_garbage_and_missing_fields_are_none(self) -> None:
        for body in (None, "", "ready", "<html>Bad gateway</html>", '{"status":"ready"}', '{"ai_spend":"n/a"}',
                     '{"ai_spend":{"day":"x","spent_usd":-1,"cap_usd":1}}', '{"ai_spend":{"spent_usd":0}}',
                     "ai_spend day=2026-11-19 spent=USD abc cap=USD 1.00", "ai_spend day=2026-11-19 spent=USD 1.00",
                     '{"ai_spend":{"spent_usd":true,"cap_usd":1}}', "[1,2]", '{"ai_spend":{"spent_usd":"NaN","cap_usd":1}}'):
            with self.subTest(body=body):
                self.assertIsNone(monitor.parse_readyz(body))

    def test_cap_zero_is_closed_not_an_alert(self) -> None:
        self.assertIsNone(monitor.spend_pct({"spent_usd": 0.0, "reserved_usd": 0.0, "cap_usd": 0.0}))

    def test_hub_spend_rows(self) -> None:
        rows = monitor.parse_hub_spend({"day": "2026-11-19",
                                        "llm": {"spent_usd": 0.41, "cap_usd": 0.5},
                                        "tts": {"spent_usd": 0.01, "cap_usd": 1},
                                        "stt": {"spent_usd": "bad", "cap_usd": 0.5}})
        self.assertEqual([r["app"] for r in rows], ["sawweeli-hub/llm", "sawweeli-hub/tts"])
        self.assertAlmostEqual(monitor.spend_pct(rows[0]["reading"]), 82.0)
        self.assertEqual(monitor.parse_hub_spend(None), [])


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.next = 10

    def find_open_issue(self, title: str, label: str | None = None) -> int | None:
        for n, i in self.issues.items():
            if i["open"] and i["title"] == title and (label is None or label in i["labels"]):
                return n
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        self.next += 1
        self.issues[self.next] = {"title": title, "body": body, "labels": labels, "open": True}
        return self.next

    def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def close(self, number: int) -> None:
        self.issues[number]["open"] = False

    def open_titles(self) -> list[str]:
        return sorted(i["title"] for i in self.issues.values() if i["open"])


def row(app: str, day: str, spent: float, cap: float, reserved: float = 0.0) -> dict:
    return {"app": app, "error": None,
            "reading": {"day": day, "spent_usd": spent, "reserved_usd": reserved, "cap_usd": cap}}


class SpendAlertCycle(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 19, 15, 7, tzinfo=dt.timezone.utc)
    APPS = {"asas", "faez"}

    def run_once(self, gh: FakeGitHub, state: dict, rows: list[dict], now: dt.datetime | None = None,
                 configured: set[str] | None = None) -> list[str]:
        errors: list[str] = []
        monitor.process_spend_alerts(gh, state, rows, configured or self.APPS, now or self.NOW, errors)
        return errors

    def test_below_80_opens_nothing(self) -> None:
        gh, state = FakeGitHub(), {}
        self.assertEqual(self.run_once(gh, state, [row("asas", "2026-11-19", 3.19, 4.0)]), [])
        self.assertEqual(gh.issues, {})
        self.assertEqual(state["spend_alerts"], {})

    def test_open_at_80_then_close_when_it_drops(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row("asas", "2026-11-19", 3.10, 4.0, reserved=0.10), row("faez", "2026-11-19", 0.1, 0.75)])
        self.assertEqual(gh.open_titles(), ["SPEND: asas"])
        n = state["spend_alerts"]["asas"]["issue"]
        self.assertIn("spend", gh.issues[n]["labels"])
        self.assertIn("80%", gh.issues[n]["body"])
        # still over: no new issue, no comment
        self.run_once(gh, state, [row("asas", "2026-11-19", 3.6, 4.0)])
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.comments, [])
        self.assertEqual(state["spend_alerts"]["asas"]["pct"], 90.0)
        # a settle brings it under 80%: closed
        self.run_once(gh, state, [row("asas", "2026-11-19", 3.0, 4.0)])
        self.assertEqual(gh.open_titles(), [])
        self.assertNotIn("asas", state["spend_alerts"])
        self.assertIn("Back below 80%", gh.comments[-1][1])

    def test_close_on_day_rollover(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row("faez", "2026-11-19", 0.75, 0.75)])
        self.assertEqual(gh.open_titles(), ["SPEND: faez"])
        self.run_once(gh, state, [row("faez", "2026-11-20", 0.0, 0.75)], now=self.NOW + dt.timedelta(hours=10))
        self.assertEqual(gh.open_titles(), [])
        self.assertIn("Day rolled over", gh.comments[-1][1])

    def test_rollover_still_over_reopens_for_the_new_day(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row("faez", "2026-11-19", 0.7, 0.75)])
        first = state["spend_alerts"]["faez"]["issue"]
        self.run_once(gh, state, [row("faez", "2026-11-20", 0.7, 0.75)])
        self.assertFalse(gh.issues[first]["open"])
        self.assertEqual(state["spend_alerts"]["faez"]["day"], "2026-11-20")
        self.assertEqual(gh.open_titles(), ["SPEND: faez"])

    def test_unreadable_keeps_the_alert_until_it_is_stale(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row("asas", "2026-11-19", 4.0, 4.0)])
        down = [{"app": "asas", "error": "HTTP 502", "reading": None}]
        self.run_once(gh, state, down, now=self.NOW + dt.timedelta(hours=5))
        self.assertEqual(gh.open_titles(), ["SPEND: asas"])
        self.run_once(gh, state, down, now=self.NOW + dt.timedelta(days=2))
        self.assertEqual(gh.open_titles(), [])

    def test_removed_app_closes(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row("asas", "2026-11-19", 4.0, 4.0)])
        self.run_once(gh, state, [], configured={"faez"})
        self.assertEqual(gh.open_titles(), [])

    def test_existing_open_issue_is_reused(self) -> None:
        gh, state = FakeGitHub(), {}
        n = gh.create_issue("SPEND: asas", "old", ["spend"])
        self.run_once(gh, state, [row("asas", "2026-11-19", 3.5, 4.0)])
        self.assertEqual(state["spend_alerts"]["asas"]["issue"], n)
        self.assertEqual(len(gh.issues), 1)

    def test_api_error_is_reported_not_raised(self) -> None:
        class Broken(FakeGitHub):
            def create_issue(self, *a, **k):  # type: ignore[override]
                raise RuntimeError("GitHub API POST /issues -> HTTP 500")
        state: dict = {}
        errors = self.run_once(Broken(), state, [row("asas", "2026-11-19", 4.0, 4.0)])
        self.assertEqual(len(errors), 1)
        self.assertEqual(state["spend_alerts"], {})

    def test_alert_state_counts_as_significant(self) -> None:
        a = monitor.significant({"apps": {}, "outages": []})
        b = monitor.significant({"apps": {}, "outages": [], "spend_alerts": {"asas": {"issue": 3, "day": "2026-11-19", "pct": 90}}})
        self.assertNotEqual(a, b)


class RunGapFlag(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 19, 14, 7, tzinfo=dt.timezone.utc)

    def samples_at(self, minutes_ago: list[int]) -> dict:
        t0 = int(self.NOW.timestamp())
        # two apps, a few seconds apart, as in a real run
        return {"asas": [[t0 - m * 60, 1, 100] for m in minutes_ago],
                "faez": [[t0 - m * 60 + 3, 1, 120] for m in minutes_ago]}

    def test_run_times_merges_apps_of_one_run(self) -> None:
        self.assertEqual(len(monitor.run_times(self.samples_at([0, 15, 30]))), 3)

    def test_45_minutes_is_not_flagged_46_is(self) -> None:
        now = int(self.NOW.timestamp())
        self.assertEqual(monitor.run_gaps([now - 45 * 60], now), [])
        self.assertEqual(monitor.run_gaps([now - 46 * 60], now), [(now - 46 * 60, now)])

    def test_gaps_in_history_and_up_to_now(self) -> None:
        now = int(self.NOW.timestamp())
        times = [now - 600 * 60, now - 585 * 60, now - 300 * 60, now - 285 * 60, now - 50 * 60]
        gaps = monitor.run_gaps(times, now)
        self.assertEqual(gaps, [(now - 585 * 60, now - 300 * 60), (now - 285 * 60, now - 50 * 60), (now - 50 * 60, now)])

    def test_gaps_older_than_24h_are_dropped(self) -> None:
        now = int(self.NOW.timestamp())
        every_30_min = [now - 26 * 3600 + k * 1800 for k in range(52)]  # -26 h up to -0.5 h
        self.assertEqual(monitor.run_gaps([now - 30 * 3600] + every_30_min, now), [])
        # a gap that ends inside the window is kept even if it started before it
        self.assertEqual(len(monitor.run_gaps([now - 30 * 3600, now - 10 * 60], now)), 1)

    def summary(self, samples: dict, **kw) -> str:
        checks = [{"app": "asas"}, {"app": "faez"}]
        res = {a: {"ms": 100, "cert_days": 70, "ok": True} for a in ("asas", "faez")}
        return monitor.summary_body(checks, res, {"outages": []}, samples, self.NOW, **kw)

    def test_summary_flags_the_gap(self) -> None:
        # one run every 15 min, then nothing for 14 h (the real failure), then this run
        body = self.summary(self.samples_at([0, 840, 855, 870]))
        self.assertIn("**Run gaps over 45 min: 1**", body)
        self.assertIn("14h 0m", body)

    def test_summary_without_gaps(self) -> None:
        body = self.summary(self.samples_at([0, 15, 30, 45]))
        self.assertIn("Run gaps: none over 45 min (4 run(s) in 24 h).", body)

    def test_summary_without_history(self) -> None:
        self.assertIn("Run gaps:** unknown", self.summary({}))

    def test_summary_has_spend_and_keepalive(self) -> None:
        body = self.summary(self.samples_at([0, 15]), spend_rows=[row("asas", "2026-11-19", 3.5, 4.0),
                                                                    {"app": "faez", "error": "HTTP 502", "reading": None}],
                            keepalive="refreshed none; checked 2 voice(s).")
        self.assertIn("| asas | 2026-11-19 | 3.5000 | 0.0000 | 4.00 | 88% **ALERT** |", body)
        self.assertIn("| faez | - | - | - | - | unavailable (HTTP 502) |", body)
        self.assertIn("Voice keep-alive: refreshed none", body)


if __name__ == "__main__":
    unittest.main()
