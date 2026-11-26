"""Records health per app, with no key: each relayer's balance read from the public Monad RPC (chain 143) and the
`records` block of the app's public /readyz (same fields as the hub's /api/ops/spend `records` section; faez#75,
hunt-dubai#34, landing-dubai#35). Standard library only: python3 -m unittest discover -s tests -v"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor  # noqa: E402
from test_monitor import FakeGitHub  # noqa: E402

NOW = dt.datetime(2026, 11, 27, 15, 7, tzinfo=dt.timezone.utc)
ACTIVE = NOW - dt.timedelta(days=1)
FAEZ = "0x892B282bA8E74cdDd5E5A5a584110aCB59f819B7"
WEI = 10 ** 18


def readyz(**records) -> str:
    base = {"day": "2026-11-27", "spent_mon": 0.05, "cap_mon": 5, "anchors": 3, "waiting": 0, "oldest_waiting_min": 0,
            "refill_alert": False, "cap_alert": False, "relayer": FAEZ, "chain_id": 143}
    base.update(records)
    return json.dumps({"status": "ready", "ai_spend": {"spent_usd": 0, "cap_usd": 1}, "records": base})


def row(balance: float | None = 30.0, body: str | None = None, active: dt.datetime | None = ACTIVE, app: str = "faez",
        balance_error: str | None = None, **records) -> dict:
    rec = monitor.parse_app_records(app, json.loads(body if body is not None else readyz(**records)))
    return {"app": app, "relayer": FAEZ, "balance_mon": balance, "balance_error": balance_error, "records": rec,
            "activated_at": active}


class ParseAppRecords(unittest.TestCase):
    def test_no_records_key_is_not_configured(self) -> None:
        self.assertIsNone(monitor.parse_app_records("faez", {"status": "ready", "ai_spend": {}}))

    def test_healthy_block_with_cap(self) -> None:
        r = monitor.parse_app_records("faez", json.loads(readyz(waiting=2, oldest_waiting_min=3)))
        self.assertEqual(r["waiting"], 2)
        self.assertEqual(r["cap_mon"], 5.0)
        self.assertFalse(r["cap_alert"])

    def test_unreadable_block_is_an_error_naming_the_app(self) -> None:
        for body in ({"records": {"error": "unavailable"}}, {"records": "x"}, {"records": {"waiting": 1}}, [], None):
            with self.subTest(body=body):
                r = monitor.parse_app_records("hunt-dubai", body)
                self.assertIn("error", r)
                self.assertTrue(r["error"].startswith("hunt-dubai:"), r)

    def test_hub_parser_is_unchanged(self) -> None:
        self.assertEqual(monitor.parse_hub_records({"records": {"error": "unavailable"}}), {"error": "hub: records unavailable"})


class Conditions(unittest.TestCase):
    def kinds(self, r: dict) -> list[str]:
        return sorted(monitor.relayer_conditions(r, NOW))

    def test_not_activated_is_quiet_even_unfunded(self) -> None:
        self.assertEqual(self.kinds(row(balance=0.0, active=None, cap_alert=True, waiting=9)), [])

    def test_healthy_is_quiet(self) -> None:
        self.assertEqual(self.kinds(row()), [])
        self.assertEqual(self.kinds(row(balance=15.0)), [])

    def test_low_funds_under_15(self) -> None:
        c = monitor.relayer_conditions(row(balance=14.99), NOW)
        self.assertEqual(sorted(c), ["low_funds"])
        self.assertIn("14.99", c["low_funds"])

    def test_reserve_at_10_plus_margin(self) -> None:
        self.assertEqual(self.kinds(row(balance=10.05)), ["low_funds", "reserve"])
        self.assertEqual(self.kinds(row(balance=10.1)), ["low_funds"])

    def test_cap_only_while_records_wait(self) -> None:
        self.assertEqual(self.kinds(row(cap_alert=True, waiting=0)), [])
        c = monitor.relayer_conditions(row(cap_alert=True, waiting=4, spent_mon=4.98), NOW)
        self.assertEqual(sorted(c), ["cap"])
        self.assertIn("4.98", c["cap"])
        self.assertIn("5", c["cap"])

    def test_stuck_counts_from_activation(self) -> None:
        self.assertEqual(self.kinds(row(waiting=1, oldest_waiting_min=61)), ["stuck"])
        self.assertEqual(self.kinds(row(waiting=1, oldest_waiting_min=600, active=NOW - dt.timedelta(minutes=30))), [])

    def test_unknown_balance_opens_nothing(self) -> None:
        self.assertEqual(self.kinds(row(balance=None, balance_error="rpc: timeout")), [])

    def test_unknown_kinds(self) -> None:
        self.assertEqual(monitor.relayer_unknown(row(balance=None, balance_error="x")), {"low_funds", "reserve"})
        self.assertEqual(monitor.relayer_unknown(row(body=json.dumps({"records": {"error": "unavailable"}}))), {"cap", "stuck"})
        self.assertEqual(monitor.relayer_unknown(row()), set())


class AlertCycle(unittest.TestCase):
    def run_once(self, gh: FakeGitHub, state: dict, rows: list[dict]) -> list[str]:
        errors: list[str] = []
        monitor.process_relayer_alerts(gh, state, rows, NOW, errors)
        return errors

    def test_open_dedupe_close(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row(balance=12.0)])
        self.assertEqual(gh.open_titles(), ["RECORDS LOW FUNDS: faez"])
        self.assertIn("faez/low_funds", state["records_alerts"])
        n = len(gh.comments)
        self.run_once(gh, state, [row(balance=11.0)])  # still low: nothing new
        self.assertEqual(len(gh.comments), n)
        self.assertEqual(len(gh.issues), 1)
        self.run_once(gh, state, [row(balance=40.0)])
        self.assertEqual(gh.open_titles(), [])
        self.assertNotIn("faez/low_funds", state["records_alerts"])
        self.assertIn("Cleared", gh.comments[-1][1])

    def test_unreadable_changes_nothing(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row(balance=10.0, cap_alert=True, waiting=3)])
        self.assertEqual(gh.open_titles(), ["RECORDS CAP: faez", "RECORDS LOW FUNDS: faez", "RECORDS RESERVE: faez"])
        self.run_once(gh, state, [row(balance=None, balance_error="rpc: timeout",
                                      body=json.dumps({"records": {"error": "unavailable"}}))])
        self.assertEqual(len(gh.open_titles()), 3)

    def test_not_configured_closes_cap_and_stuck(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row(cap_alert=True, waiting=3)])
        self.assertEqual(gh.open_titles(), ["RECORDS CAP: faez"])
        self.run_once(gh, state, [row(body=json.dumps({"status": "ready"}))])  # the app stopped reporting records
        self.assertEqual(gh.open_titles(), [])

    def test_reuses_an_open_issue_with_the_same_title(self) -> None:
        gh, state = FakeGitHub(), {}
        n = gh.create_issue("RECORDS LOW FUNDS: faez", "earlier", ["records"])
        self.run_once(gh, state, [row(balance=1.0)])
        self.assertEqual(state["records_alerts"]["faez/low_funds"]["issue"], n)

    def test_leaves_the_hubs_issues_alone(self) -> None:
        gh, state = FakeGitHub(), {"records_alerts": {"sawweeli-hub/refill": {"issue": 3, "since": "x"}}}
        gh.issues[3] = {"title": "RECORDS REFILL: sawweeli-hub", "body": "", "labels": ["records"], "open": True}
        self.run_once(gh, state, [row()])
        self.assertTrue(gh.issues[3]["open"])
        self.assertIn("sawweeli-hub/refill", state["records_alerts"])

    def test_the_hub_check_leaves_the_apps_issues_alone(self) -> None:
        gh, state = FakeGitHub(), {}
        self.run_once(gh, state, [row(balance=2.0)])
        hub_reading = monitor.parse_hub_records({"records": {"day": "2026-11-27", "spent_mon": 0, "anchors": 0, "waiting": 0,
                                                             "oldest_waiting_min": 0, "refill_alert": False, "cap_alert": False}})
        errors: list[str] = []
        monitor.process_records_alerts(gh, state, hub_reading, NOW, ACTIVE, errors)
        self.assertIn("faez/low_funds", state["records_alerts"])
        self.assertIn("RECORDS LOW FUNDS: faez", gh.open_titles())

    def test_github_error_is_reported_not_raised(self) -> None:
        gh, state = FakeGitHub(), {}

        def boom(*a, **k):
            raise RuntimeError("github: 502")
        gh.create_issue = boom  # type: ignore[method-assign]
        self.assertEqual(self.run_once(gh, state, [row(balance=1.0)]), ["github: 502", "github: 502"])
        self.assertEqual(state["records_alerts"], {})


class Balances(unittest.TestCase):
    def rpc(self, answers: dict):
        def call(method: str, params: list) -> object:
            a = answers[method]
            if isinstance(a, Exception):
                raise a
            return a(params) if callable(a) else a
        return call

    def test_balances_in_mon(self) -> None:
        call = self.rpc({"eth_chainId": "0x8f", "eth_getBalance": lambda p: hex(int(14.25 * WEI)) if p[0] == FAEZ else "0x0"})
        with mock.patch.object(monitor, "rpc_call", call):
            got, err = monitor.relayer_balances([FAEZ, "0x" + "1" * 40])
        self.assertIsNone(err)
        self.assertEqual(got[FAEZ], 14.25)
        self.assertEqual(got["0x" + "1" * 40], 0.0)

    def test_wrong_chain_reads_nothing(self) -> None:
        with mock.patch.object(monitor, "rpc_call", self.rpc({"eth_chainId": "0x279f", "eth_getBalance": "0x1"})):
            got, err = monitor.relayer_balances([FAEZ])
        self.assertEqual(got, {})
        self.assertIn("143", err)

    def test_rpc_error_reads_nothing(self) -> None:
        with mock.patch.object(monitor, "rpc_call", self.rpc({"eth_chainId": RuntimeError("rpc: timeout")})):
            got, err = monitor.relayer_balances([FAEZ])
        self.assertEqual(got, {})
        self.assertIn("timeout", err)

    def test_bad_balance_value_is_unknown(self) -> None:
        with mock.patch.object(monitor, "rpc_call", self.rpc({"eth_chainId": "0x8f", "eth_getBalance": "zz"})):
            got, err = monitor.relayer_balances([FAEZ])
        self.assertIsNone(got[FAEZ])


class _Serial:
    """ThreadPoolExecutor stand-in: map in order, in this thread."""

    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self) -> "_Serial":
        return self

    def __exit__(self, *a) -> None:
        pass

    def map(self, f, xs):  # noqa: ANN001
        return [f(x) for x in xs]


class Wiring(unittest.TestCase):
    CHECKS = [
        {"app": "faez", "url": "https://faez.empowertours.xyz/", "readyz": "/readyz", "relayer": FAEZ, "records_activated_at": None},
        {"app": "sawweeli-hub", "url": "https://sawweeli-agent.empowertours.xyz/", "relayer": "0x" + "2" * 40,
         "records_activated_at": "2026-11-26T12:00:00Z"},
        {"app": "demo-studio", "url": "https://demo-studio.empowertours.xyz/healthz"},
    ]

    def test_collect_reads_every_relayer_and_only_app_readyz(self) -> None:
        fetched: list[str] = []

        def fake_fetch(url: str) -> dict:
            fetched.append(url)
            return {"status": 200, "ms": 5, "body": readyz(), "error": None}
        with mock.patch.object(monitor, "fetch", fake_fetch), \
                mock.patch.object(monitor, "relayer_balances", lambda addrs: ({a: 20.0 for a in addrs}, None)):
            rows = monitor.collect_relayers(self.CHECKS)
        self.assertEqual([r["app"] for r in rows], ["faez", "sawweeli-hub"])
        self.assertEqual(fetched, ["https://faez.empowertours.xyz/readyz"])
        self.assertIsNone(rows[0]["activated_at"])
        self.assertEqual(rows[1]["activated_at"], dt.datetime(2026, 11, 26, 12, tzinfo=dt.timezone.utc))
        self.assertIsNone(rows[1]["records"])  # the hub's records come through OPS_TOKEN
        self.assertEqual(rows[0]["balance_mon"], 20.0)

    def test_text_readyz_is_no_block_and_http_error_is_unreadable(self) -> None:
        answers = iter([{"status": 200, "ms": 1, "body": "ok\nai_spend day=2026-11-27 spent=USD 0.00 cap=USD 1.00", "error": None},
                        {"status": 503, "ms": 1, "body": "not ready", "error": None}])
        with mock.patch.object(monitor, "fetch", lambda u: next(answers)), \
                mock.patch.object(monitor, "relayer_balances", lambda addrs: ({a: 20.0 for a in addrs}, None)), \
                mock.patch.object(monitor, "ThreadPoolExecutor", _Serial):
            rows = monitor.collect_relayers([self.CHECKS[0], dict(self.CHECKS[0], app="asas")])
        self.assertIsNone(rows[0]["records"])
        self.assertEqual(rows[1]["records"], {"error": "asas: /readyz HTTP 503"})

    def test_bad_activation_time_is_reported_and_alerts_stay_off(self) -> None:
        checks = [dict(self.CHECKS[0], records_activated_at="soon")]
        with mock.patch.object(monitor, "fetch", lambda u: {"status": 200, "ms": 1, "body": readyz(), "error": None}), \
                mock.patch.object(monitor, "relayer_balances", lambda addrs: ({a: 0.0 for a in addrs}, None)):
            rows = monitor.collect_relayers(checks)
        self.assertIsNone(rows[0]["activated_at"])
        self.assertIn("records_activated_at", rows[0]["config_error"])

    def test_hub_rows_never_alert_here(self) -> None:
        gh, state = FakeGitHub(), {}
        hub_row = dict(row(balance=1.0), app="sawweeli-hub", records=None)
        monitor.process_relayer_alerts(gh, state, [hub_row], NOW, [])
        self.assertEqual(gh.open_titles(), [])

    def test_summary_line(self) -> None:
        self.assertIn("not activated", monitor.relayer_line(row(active=None)))
        line = monitor.relayer_line(row(balance=14.0, waiting=2, spent_mon=0.1))
        self.assertIn("14 MON", line)
        self.assertIn("2 waiting", line)
        self.assertIn("unknown", monitor.relayer_line(row(balance=None, balance_error="rpc: timeout")))

    def test_checks_json_relayers_are_the_published_addresses(self) -> None:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "checks.json")) as f:
            checks = json.load(f)
        got = {c["app"]: c["relayer"] for c in checks if c.get("relayer")}
        self.assertEqual(got, {
            "sawweeli-hub": "0xc4f1cCB74A75E84f5f9b6fC7dE45f6C329Fa9555",
            "sawweeli-trips": "0xFcb98729456c49967D0A99D9C7eBEDE2e7d6bCCC",
            "asas": "0xAFe6b0018d4175711505b03A541f254CB8FDC064",
            "faez": "0x892B282bA8E74cdDd5E5A5a584110aCB59f819B7",
            "cosecha": "0x8F3241076A75817e75B242757831694b5b50c07a",
            "haqiqi": "0xcd6F5a56b8B7b3361e26705BC66D021B197c2623",
            "relocatedubai": "0x50428d62E61B2c2Db1346Fd34a05932613FB3672",
            "hunt-dubai": "0xB258aE31349fc7D450C52B29a84C9cF99C1d2F21",
        })
        for c in checks:
            if c.get("relayer"):
                self.assertIn("records_activated_at", c, c["app"])


class MainWiring(unittest.TestCase):
    """monitor.main() end to end, network and GitHub faked, no OPS_TOKEN: the per-app check needs no key."""

    def run_main(self, balance: float, gh: FakeGitHub, tmp: str, summary: str) -> int:
        checks = [{"app": "faez", "url": "https://faez.invalid/", "readyz": "/readyz", "relayer": FAEZ,
                   "records_activated_at": "2026-11-26T12:00:00Z"}]
        gh.ensure_label = lambda *a: None  # type: ignore[attr-defined]
        real_load = monitor.load_json
        env = {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r", "OPS_TOKEN": "", "GITHUB_STEP_SUMMARY": summary, "GITHUB_OUTPUT": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(monitor, "load_json", side_effect=lambda p, d: checks if p == monitor.CHECKS_FILE else real_load(p, d)), \
                mock.patch.object(monitor, "STATE_FILE", os.path.join(tmp, "status.json")), \
                mock.patch.object(monitor, "SAMPLES_FILE", os.path.join(tmp, "samples.json")), \
                mock.patch.object(monitor, "run_checks", return_value=[{"app": "faez", "ok": True, "ms": 5, "cert_days": 60, "reasons": []}]), \
                mock.patch.object(monitor, "table", return_value=""), \
                mock.patch.object(monitor, "fetch", return_value={"status": 200, "ms": 3, "body": readyz(), "error": None}), \
                mock.patch.object(monitor, "relayer_balances", lambda addrs: ({a: balance for a in addrs}, None)), \
                mock.patch.object(monitor, "hub_call", side_effect=AssertionError("the hub must not be called without OPS_TOKEN")), \
                mock.patch.object(monitor, "GitHub", return_value=gh), \
                mock.patch.object(monitor, "now_utc", return_value=NOW.replace(hour=10)):
            return monitor.main()

    def test_low_funds_from_main_without_any_token(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            gh, summary = FakeGitHub(), os.path.join(tmp, "summary.md")
            self.assertEqual(self.run_main(12.0, gh, tmp, summary), 0)
            self.assertEqual(gh.open_titles(), ["RECORDS LOW FUNDS: faez"])
            with open(summary) as f:
                self.assertIn("faez: relayer 12 MON", f.read())
            self.assertEqual(self.run_main(12.0, gh, tmp, summary), 0)  # deduped through the saved state
            self.assertEqual(len(gh.issues), 1)
            self.assertEqual(self.run_main(31.0, gh, tmp, summary), 0)
            self.assertEqual(gh.open_titles(), [])


if __name__ == "__main__":
    unittest.main()
