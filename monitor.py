#!/usr/bin/env python3
"""Uptime monitor for the EmpowerTours Dubai championship apps.

Standard library only. Runs on GitHub Actions (see .github/workflows/uptime.yml).

For every entry in checks.json it records: HTTP status (must be 200), keyword
present, judge path reachable, /healthz reachable, TLS certificate days left
(fails below CERT_MIN_DAYS) and response time (TIMEOUT_S limit). A failing app
is retried once after RETRY_DELAY_S before it is declared down.

On a down transition it opens (or comments on) one issue "DOWN: <app>" labelled
`outage`; on recovery it closes that issue with the downtime duration. State is
kept in status/status.json and only rewritten when something meaningful changes
or once a day (with the daily summary posted to the "Daily status" issue).

Without GITHUB_TOKEN it runs as a dry run: checks only, no issues, no state write.
Response headers and cookies are never printed or stored.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urljoin, urlsplit

ROOT = os.path.dirname(os.path.abspath(__file__))
CHECKS_FILE = os.path.join(ROOT, "checks.json")
STATE_FILE = os.path.join(ROOT, "status", "status.json")
SAMPLES_FILE = os.environ.get("SAMPLES_FILE", os.path.join(ROOT, ".cache", "samples.json"))

TIMEOUT_S = 20
RETRY_DELAY_S = int(os.environ.get("RETRY_DELAY_S", "30"))
CERT_MIN_DAYS = 14
SUMMARY_HOUR_UTC = 14
OUTAGE_LABEL = "outage"
SUMMARY_TITLE = "Daily status"
MAX_BODY = 2_000_000
OUTAGE_KEEP_DAYS = 8
USER_AGENT = "EmpowerTours-ops-monitor/1.0 (+https://github.com/EmpowerTours/ops-monitor)"
# A run more than this long after the previous one is flagged in the daily summary (GitHub's scheduler drops runs).
GAP_FLAG_S = 45 * 60
# AI spend: an alert issue opens when an app's spend reaches this share of its daily cap.
SPEND_ALERT_PCT = 80.0
SPEND_LABEL = "spend"
# The Sawweeli hub's ops endpoints, called only when OPS_TOKEN is set.
OPS_HUB_URL = os.environ.get("OPS_HUB_URL", "https://sawweeli-agent.empowertours.xyz").rstrip("/")
# Monad records: the `records` section of the hub's /api/ops/spend. The thresholds mirror the proof worker's
# (sawweeli-agent proof-worker/third_party/proof/budget.go): refill alert under 15 MON, nothing sent that would take
# the relayer under the 10 MON reserve. The margin covers one anchor's max cost (gas limit x fee cap), so a balance
# just above 10 MON already counts as "at the reserve".
RECORDS_APP = "sawweeli-hub"
RECORDS_LABEL = "records"
RECORDS_REFILL_MON = 15.0
RECORDS_RESERVE_MON = 10.0
RECORDS_RESERVE_MARGIN_MON = 0.1
RECORDS_STUCK_MIN = 60.0
RECORDS_ACTION = {
    "refill": "Fund the hub relayer (runbook: back to 40 MON). The worker keeps sending until the 10 MON reserve.",
    "reserve": "Records are queueing and none go out until the relayer is funded. Fund the hub relayer now.",
    "cap": "Nothing is lost: records keep queueing and go out after 00:00 UTC. Raise PROOF_DAILY_CAP_MON on the "
           "proof-worker service only if the volume is expected.",
    "stuck": "Check the proof-worker service logs (RPC, relayer activation, contract address) and the cap and "
             "reserve issues, which also hold records back.",
}

SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------- time utils
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h or d:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


# ------------------------------------------------------------------ probing
def fetch(url: str) -> dict[str, Any]:
    """Plain GET. Returns status, elapsed ms, decoded body (or None) and error."""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.5"},
    )
    t0 = time.monotonic()
    status: int | None = None
    body: str | None = None
    error: str | None = None
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=SSL_CTX) as resp:
            status = resp.status
            body = resp.read(MAX_BODY).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError) as e:
        reason = getattr(e, "reason", e)
        error = type(reason).__name__ if not isinstance(reason, str) else reason
    ms = int((time.monotonic() - t0) * 1000)
    if error is None and ms > TIMEOUT_S * 1000:
        error = "timeout"
    return {"status": status, "ms": ms, "body": body, "error": error}


def cert_days_left(host: str) -> float | None:
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT_S) as sock:
            with SSL_CTX.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        expires = ssl.cert_time_to_seconds(cert["notAfter"])
        return (expires - time.time()) / 86400
    except (OSError, ssl.SSLError, KeyError, ValueError):
        return None


def probe(check: dict[str, Any]) -> dict[str, Any]:
    url: str = check["url"]
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    keyword: str = check["keyword"]
    reasons: list[str] = []

    main = fetch(url)
    keyword_ok = main["body"] is not None and keyword in main["body"]
    if main["error"]:
        reasons.append(f"error: {main['error']}")
    elif main["status"] != 200:
        reasons.append(f"HTTP {main['status']}")
    elif not keyword_ok:
        reasons.append(f"keyword {keyword!r} missing")

    judge_status: int | None = None
    if check.get("judge_path"):
        judge = fetch(urljoin(base, check["judge_path"]))
        judge_status = judge["status"]
        if judge["error"]:
            reasons.append(f"judge {check['judge_path']} error: {judge['error']}")
        elif judge["status"] != 200:
            reasons.append(f"judge {check['judge_path']} HTTP {judge['status']}")
        elif keyword not in (judge["body"] or ""):
            reasons.append(f"judge {check['judge_path']} keyword missing")

    healthz_status: int | None = None
    if check.get("healthz"):
        hz = fetch(urljoin(base, check["healthz"]))
        healthz_status = hz["status"]
        hz_kw = check.get("healthz_keyword")
        if hz["error"]:
            reasons.append(f"healthz error: {hz['error']}")
        elif hz["status"] != 200:
            reasons.append(f"healthz HTTP {hz['status']}")
        elif hz_kw and hz_kw not in (hz["body"] or ""):
            reasons.append("healthz keyword missing")

    days = cert_days_left(parts.hostname or "")
    if days is None:
        reasons.append("TLS certificate unreadable")
    elif days < CERT_MIN_DAYS:
        reasons.append(f"TLS certificate expires in {int(days)} days")

    return {
        "app": check["app"],
        "url": url,
        "ok": not reasons,
        "reasons": reasons,
        "status": main["status"],
        "ms": main["ms"],
        "keyword_ok": keyword_ok,
        "judge_status": judge_status,
        "healthz_status": healthz_status,
        "cert_days": None if days is None else int(days),
        "attempts": 1,
    }


def run_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(probe, checks))
    failed = [i for i, r in enumerate(results) if not r["ok"]]
    if failed:
        print(f"{len(failed)} check(s) failed, retrying once in {RETRY_DELAY_S}s: "
              + ", ".join(results[i]["app"] for i in failed), flush=True)
        time.sleep(RETRY_DELAY_S)
        with ThreadPoolExecutor(max_workers=8) as pool:
            retried = list(pool.map(probe, [checks[i] for i in failed]))
        for i, r in zip(failed, retried):
            r["attempts"] = 2
            results[i] = r
    return results


# ------------------------------------------------------------- run gaps
def run_times(samples: dict[str, list[list[Any]]]) -> list[int]:
    """Start times of past runs, from the latency samples (every run adds one sample per app at the same moment)."""
    times = sorted({int(s[0]) for v in samples.values() for s in v})
    out: list[int] = []
    for t in times:
        if not out or t - out[-1] > 120:  # samples of one run land within seconds of each other
            out.append(t)
    return out


def run_gaps(times: list[int], now_ts: int, threshold_s: int = GAP_FLAG_S) -> list[tuple[int, int]]:
    """(previous run, next run) pairs more than threshold_s apart in the last 24 h, including the gap up to now_ts."""
    seq = [t for t in times if t <= now_ts] + [now_ts]
    cutoff = now_ts - 86400
    return [(a, b) for a, b in zip(seq, seq[1:]) if b - a > threshold_s and b >= cutoff]


def fmt_gaps(gaps: list[tuple[int, int]]) -> str:
    def hm(t: int) -> str:
        return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M")
    return "; ".join(f"{hm(a)} to {hm(b)} UTC ({fmt_duration(b - a)})" for a, b in gaps)


# ------------------------------------------------------------- AI spend
_SPEND_TOKEN = re.compile(r"(\w+)=(?:USD\s+)?(\S+)")


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = float(str(v).strip().lstrip("$"))
    except ValueError:
        return None
    return n if math.isfinite(n) and n >= 0 else None


def parse_readyz(body: str | None) -> dict[str, Any] | None:
    """Today's AI spend from an app's /readyz body, in either published format:
    JSON  {"ai_spend": {"day", "spent_usd", "reserved_usd", "cap_usd", ...}, ...}
    text  a line "ai_spend day=YYYY-MM-DD spent=USD 0.00 reserved=USD 0.00 cap=USD 4.00 ..."
    Returns {"day", "spent_usd", "reserved_usd", "cap_usd"} or None when neither format is found or a number is bad."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        s = data.get("ai_spend")
        if isinstance(s, dict):
            spent, cap = _num(s.get("spent_usd")), _num(s.get("cap_usd"))
            reserved = _num(s.get("reserved_usd", 0))
            if spent is None or cap is None or reserved is None:
                return None
            return {"day": str(s.get("day") or ""), "spent_usd": spent, "reserved_usd": reserved, "cap_usd": cap}
        return None
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("ai_spend "):
            continue
        kv = dict(_SPEND_TOKEN.findall(line))
        spent, cap = _num(kv.get("spent")), _num(kv.get("cap"))
        reserved = _num(kv.get("reserved", "0"))
        if spent is None or cap is None or reserved is None:
            return None
        return {"day": kv.get("day", ""), "spent_usd": spent, "reserved_usd": reserved, "cap_usd": cap}
    return None


def spend_pct(r: dict[str, Any]) -> float | None:
    """Spent plus in-flight reservations as a % of the daily cap. None when the cap is 0 (spending is closed)."""
    cap = r["cap_usd"]
    if cap <= 0:
        return None
    return 100.0 * (r["spent_usd"] + r.get("reserved_usd", 0.0)) / cap


def fetch_spend(check: dict[str, Any]) -> dict[str, Any]:
    parts = urlsplit(check["url"])
    res = fetch(urljoin(f"{parts.scheme}://{parts.netloc}", check["readyz"]))
    reading = parse_readyz(res["body"]) if res["status"] == 200 else None
    err = None if reading else (res["error"] or (f"HTTP {res['status']}" if res["status"] != 200 else "no ai_spend in /readyz"))
    return {"app": check["app"], "reading": reading, "error": err}


def hub_call(method: str, path: str, token: str) -> Any:
    """Authenticated call to the hub's /api/ops endpoints. Raises RuntimeError without the token in the message."""
    if not OPS_HUB_URL.startswith("https://"):
        raise RuntimeError("OPS_HUB_URL must be https")
    req = urllib.request.Request(f"{OPS_HUB_URL}{path}", method=method, data=b"{}" if method == "POST" else None, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            return json.loads(resp.read(MAX_BODY) or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"hub {method} {path} -> HTTP {e.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        raise RuntimeError(f"hub {method} {path} -> {type(e).__name__}") from None


def parse_hub_spend(data: Any) -> list[dict[str, Any]]:
    """GET /api/ops/spend -> one spend row per metered service: sawweeli-hub/llm, /tts, /stt."""
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for kind in ("llm", "tts", "stt"):
        s = data.get(kind)
        if not isinstance(s, dict):
            continue
        spent, cap = _num(s.get("spent_usd")), _num(s.get("cap_usd"))
        if spent is None or cap is None:
            continue
        out.append({"app": f"sawweeli-hub/{kind}", "error": None,
                    "reading": {"day": str(data.get("day") or ""), "spent_usd": spent, "reserved_usd": 0.0, "cap_usd": cap}})
    return out


def spend_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| app | day | spent USD | reserved USD | daily cap USD | used |", "|---|---|---|---|---|---|"]
    for row in rows:
        r = row["reading"]
        if r is None:
            lines.append(f"| {row['app']} | - | - | - | - | unavailable ({row['error']}) |")
            continue
        p = spend_pct(r)
        used = "cap 0 (closed)" if p is None else f"{p:.0f}%" + (" **ALERT**" if p >= SPEND_ALERT_PCT else "")
        lines.append(f"| {row['app']} | {r['day'] or '-'} | {r['spent_usd']:.4f} | {r['reserved_usd']:.4f} | "
                     f"{r['cap_usd']:.2f} | {used} |")
    return "\n".join(lines)


def process_spend_alerts(gh: Any, state: dict[str, Any], rows: list[dict[str, Any]], configured: set[str],
                         now: dt.datetime, errors: list[str]) -> None:
    """One issue "SPEND: <app>" (label `spend`) while an app is at SPEND_ALERT_PCT or more of its daily cap.
    It closes when the share drops below, when the app's day rolls over, when the app stops being checked, or (if
    /readyz stays unreachable) once the alert is more than a day old."""
    alerts: dict[str, Any] = state.setdefault("spend_alerts", {})
    stamp = iso(now)
    by_app = {row["app"]: row for row in rows}

    def close(app: str, why: str) -> None:
        a = alerts[app]
        try:
            gh.comment(a["issue"], f"{why} at {stamp}. Closing.")
            gh.close(a["issue"])
            print(f"closed spend issue #{a['issue']} for {app} ({why})")
            del alerts[app]
        except RuntimeError as e:
            errors.append(str(e))

    for app in list(alerts):
        a = alerts[app]
        row = by_app.get(app)
        r = row["reading"] if row else None
        if app not in configured:
            close(app, "No longer checked")
        elif r is None:
            try:
                stale = (now.date() - dt.date.fromisoformat(a["day"])).days >= 2
            except ValueError:
                stale = True
            if stale:
                close(app, f"Spend unreadable and the alert day {a['day']} is over")
        elif r["day"] != a["day"]:
            close(app, f"Day rolled over ({a['day']} -> {r['day']})")
        else:
            p = spend_pct(r)
            if p is None or p < SPEND_ALERT_PCT:
                close(app, f"Back below {SPEND_ALERT_PCT:.0f}% of the daily cap "
                           f"(now {'closed' if p is None else f'{p:.0f}%'})")
            else:
                a["pct"] = round(p, 1)

    for row in rows:
        app, r = row["app"], row["reading"]
        if r is None or app in alerts:
            continue
        p = spend_pct(r)
        if p is None or p < SPEND_ALERT_PCT:
            continue
        title = f"SPEND: {app}"
        body = (f"**{app}** has used **{p:.0f}%** of its daily AI cap ({r['day']}): "
                f"USD {r['spent_usd']:.4f} spent + USD {r['reserved_usd']:.4f} reserved of USD {r['cap_usd']:.2f}.\n\n"
                f"The app's own cap still stops spending at 100%. This issue closes automatically when the share "
                f"drops below {SPEND_ALERT_PCT:.0f}% or the app's day rolls over.")
        try:
            existing = gh.find_open_issue(title, SPEND_LABEL)
            if existing:
                gh.comment(existing, f"At {p:.0f}% of the daily cap at {stamp} ({r['day']}).")
                num = existing
            else:
                num = gh.create_issue(title, body, [SPEND_LABEL])
            alerts[app] = {"issue": num, "day": r["day"], "pct": round(p, 1), "since": stamp}
            print(f"opened/updated spend issue #{num} for {app} ({p:.0f}%)")
        except RuntimeError as e:
            errors.append(str(e))


# ------------------------------------------------------------- Monad records (hub)
def parse_hub_records(data: Any) -> dict[str, Any] | None:
    """The `records` section of the hub's GET /api/ops/spend (sawweeli-agent lib/proof/ops.ts).
    None: the hub answered without `records`, so the record layer is not configured (no alert).
    {"error": ...}: the section is unreadable (the hub's own read error, or a shape this code does not know).
    Otherwise the normalized reading. The hub's refill_alert / cap_alert mean "the worker raised that alert in the
    last 24 h"; balance_mon is the relayer balance at the latest refill alert."""
    if not isinstance(data, dict):
        return {"error": "hub: /api/ops/spend body is not a JSON object"}
    if "records" not in data:
        return None
    s = data["records"]
    if not isinstance(s, dict):
        return {"error": "hub: records is not an object"}
    if "error" in s:
        return {"error": f"hub: records {str(s['error'])[:60]}"}
    waiting, oldest = _num(s.get("waiting")), _num(s.get("oldest_waiting_min"))
    refill, cap = s.get("refill_alert"), s.get("cap_alert")
    if waiting is None or oldest is None or not isinstance(refill, bool) or not isinstance(cap, bool):
        return {"error": "hub: records has missing or bad fields"}
    last = s.get("last_alert_at")
    return {"day": str(s.get("day") or ""), "spent_mon": _num(s.get("spent_mon")) or 0.0,
            "anchors": int(_num(s.get("anchors")) or 0), "waiting": int(waiting), "oldest_waiting_min": oldest,
            "refill_alert": refill, "cap_alert": cap, "balance_mon": _num(s.get("balance_mon")),
            "last_alert_at": str(last) if last else None}


def records_activation(checks: list[dict[str, Any]]) -> dt.datetime | None:
    """`records_activated_at` of the hub's check in checks.json: when the hub relayer was activated on the
    ProofAnchor contract. Unset: anchoring is not live, so waiting records are expected and never "stuck".
    Raises ValueError on a value that is not a time."""
    for c in checks:
        if c.get("app") == RECORDS_APP and c.get("records_activated_at"):
            return parse_any_iso(str(c["records_activated_at"]))
    return None


def records_conditions(r: dict[str, Any], now: dt.datetime, activated_at: dt.datetime | None) -> dict[str, str]:
    """Alert kind -> reason for a readable records section. Kinds: refill (relayer under the refill threshold),
    reserve (at the reserve and records are held back: anchoring paused), cap (daily cap hit and records are held
    back until the next UTC day), stuck (a record waited more than RECORDS_STUCK_MIN since activation)."""
    out: dict[str, str] = {}
    bal = r.get("balance_mon")
    bal_txt = f"{bal:g} MON" if bal is not None else "unknown"
    if r["refill_alert"]:
        out["refill"] = (f"the relayer balance fell under the {RECORDS_REFILL_MON:g} MON refill threshold "
                         f"(balance at the last alert: {bal_txt})")
    if r["refill_alert"] and bal is not None and bal < RECORDS_RESERVE_MON + RECORDS_RESERVE_MARGIN_MON \
            and r["waiting"] > 0:
        out["reserve"] = (f"the relayer is at the {RECORDS_RESERVE_MON:g} MON reserve ({bal_txt}); anchoring is "
                          f"paused and {r['waiting']} record(s) are waiting")
    if r["cap_alert"] and r["waiting"] > 0:
        out["cap"] = (f"the daily MON cap was hit (spent today: {r['spent_mon']:g} MON); {r['waiting']} record(s) "
                      "wait for the next UTC day")
    if activated_at is not None and r["waiting"] > 0:
        since_active = (now - activated_at).total_seconds() / 60
        stuck_min = min(r["oldest_waiting_min"], since_active)
        if stuck_min > RECORDS_STUCK_MIN:
            out["stuck"] = (f"{r['waiting']} record(s) not anchored; the oldest has waited "
                            f"{fmt_duration(stuck_min * 60)} since anchoring was activated")
    return out


def records_line(r: dict[str, Any] | None) -> str:
    if r is None:
        return "not configured on the hub"
    if "error" in r:
        return f"unavailable ({r['error']})"
    flags = [k for k in ("refill", "cap") if r[f"{k}_alert"]]
    bal = f", balance at last refill alert {r['balance_mon']:g} MON" if r.get("balance_mon") is not None else ""
    return (f"{r['day'] or '-'}: {r['spent_mon']:g} MON spent, {r['anchors']} anchor(s), {r['waiting']} waiting "
            f"(oldest {r['oldest_waiting_min']:.0f} min){bal}; worker alerts in 24 h: {', '.join(flags) or 'none'}")


def process_records_alerts(gh: Any, state: dict[str, Any], r: dict[str, Any] | None, now: dt.datetime,
                           activated_at: dt.datetime | None, errors: list[str]) -> None:
    """One issue "RECORDS <KIND>: sawweeli-hub" (label `records`) per active condition, reused if already open and
    not commented on while it lasts. It closes when the condition clears or the hub stops reporting `records`
    (not configured). An unreadable section changes nothing: open issues stay open, none are opened."""
    alerts: dict[str, Any] = state.setdefault("records_alerts", {})
    if r is not None and "error" in r:
        return
    stamp = iso(now)
    conds = {} if r is None else records_conditions(r, now, activated_at)
    for key in [k for k in alerts if k.startswith(RECORDS_APP + "/")]:  # other apps: process_relayer_alerts
        kind = key.split("/", 1)[1]
        if kind in conds:
            continue
        why = "The hub no longer reports records (not configured)" if r is None else f"Cleared ({records_line(r)})"
        try:
            gh.comment(alerts[key]["issue"], f"{why} at {stamp}. Closing.")
            gh.close(alerts[key]["issue"])
            print(f"closed records issue #{alerts[key]['issue']} ({key})")
            del alerts[key]
        except RuntimeError as e:
            errors.append(str(e))
    for kind, reason in conds.items():
        key = f"{RECORDS_APP}/{kind}"
        if key in alerts:
            continue
        title = f"RECORDS {kind.upper()}: {RECORDS_APP}"
        body = (f"**{RECORDS_APP}** Monad records: {reason}.\n\n{RECORDS_ACTION[kind]}\n\n"
                f"Hub reading at {stamp}: {records_line(r)}.\n\nThis issue closes automatically when the condition "
                "clears. The hub reports the worker's refill and cap alerts for 24 h after the last one.")
        try:
            existing = gh.find_open_issue(title, RECORDS_LABEL)
            if existing:
                gh.comment(existing, f"Again at {stamp}: {reason}.")
                num = existing
            else:
                num = gh.create_issue(title, body, [RECORDS_LABEL])
            alerts[key] = {"issue": num, "since": stamp}
            print(f"opened/updated records issue #{num} ({key})")
        except RuntimeError as e:
            errors.append(str(e))

# ------------------------------------------------------- Monad records (every app, no key)
# Each app's relayer is read straight from the public Monad RPC (eth_getBalance, chain 143 checked first), and each
# app's public /readyz `records` block (faez#75, hunt-dubai#34, landing-dubai#35; same fields as the hub's
# /api/ops/spend `records` section plus cap_mon). No key or token: addresses and blocks are public. Nothing opens
# before an app's `records_activated_at` is set in checks.json (relayers are unfunded until then). The hub's
# conditions stay with process_records_alerts (OPS_TOKEN); here the hub's balance is only reported.
MONAD_RPC_URL = os.environ.get("MONAD_RPC_URL", "https://rpc.monad.xyz")
MONAD_CHAIN_ID = 143
RELAYER_ACTION = {
    "low_funds": "Fund this app's relayer (runbook: back to 30 MON, 40 on sawweeli-trips). The app keeps sending "
                 "until the 10 MON reserve.",
    "reserve": "Anchoring is paused: nothing is sent that would take the relayer under the 10 MON reserve. Records "
               "queue and nothing is lost. Fund the relayer now.",
    "cap": "Nothing is lost: records keep queueing and go out after 00:00 UTC. Raise PROOF_DAILY_CAP_MON on the "
           "app's service only if the volume is expected.",
    "stuck": "Check the app's logs (RPC, relayer activation on ProofAnchor, contract address) and the cap and "
             "reserve issues, which also hold records back.",
}
RELAYER_TITLE = {"low_funds": "LOW FUNDS", "reserve": "RESERVE", "cap": "CAP", "stuck": "STUCK"}


def rpc_call(method: str, params: list[Any]) -> Any:
    """One JSON-RPC call to MONAD_RPC_URL. Raises RuntimeError with the method and the error kind only."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(MONAD_RPC_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=SSL_CTX) as resp:
            out = json.loads(resp.read(MAX_BODY) or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"rpc {method}: HTTP {e.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, ValueError) as e:
        raise RuntimeError(f"rpc {method}: {type(e).__name__}") from None
    if not isinstance(out, dict) or "result" not in out:
        raise RuntimeError(f"rpc {method}: no result")
    return out["result"]


def relayer_balances(addrs: list[str]) -> tuple[dict[str, float | None], str | None]:
    """Balances in MON at the latest block, after checking the RPC is Monad mainnet. ({}, reason) when the chain
    cannot be read or is another chain; a single bad answer is None (unknown) for that address only."""
    try:
        chain = rpc_call("eth_chainId", [])
        if not isinstance(chain, str) or int(chain, 16) != MONAD_CHAIN_ID:
            return {}, f"rpc is chain {chain}, not Monad mainnet ({MONAD_CHAIN_ID})"
    except (RuntimeError, ValueError) as e:
        return {}, str(e)
    out: dict[str, float | None] = {}
    for a in addrs:
        try:
            v = rpc_call("eth_getBalance", [a, "latest"])
            out[a] = int(v, 16) / 10 ** 18
        except (RuntimeError, ValueError, TypeError):
            out[a] = None
    return out, None


def parse_app_records(app: str, data: Any) -> dict[str, Any] | None:
    """The `records` block of an app's /readyz. None: no block (the app's record layer is off). {"error"}: unreadable.
    Otherwise the hub's normalized fields plus cap_mon (None when not stated)."""
    if not isinstance(data, dict):
        return {"error": f"{app}: /readyz body is not a JSON object"}
    if "records" not in data:
        return None
    r = parse_hub_records(data)
    if r is not None and "error" in r:
        return {"error": app + r["error"][len("hub"):]}
    assert r is not None
    r["cap_mon"] = _num(data["records"].get("cap_mon"))
    return r


def collect_relayers(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per check with a `relayer`: chain balance, the /readyz records block (apps with `readyz` only; the
    hub reports through OPS_TOKEN) and the activation time from checks.json."""
    rcs = [c for c in checks if c.get("relayer")]
    if not rcs:
        return []
    balances, berr = relayer_balances([c["relayer"] for c in rcs])

    def one(c: dict[str, Any]) -> dict[str, Any]:
        records: dict[str, Any] | None = None
        if c.get("readyz"):
            parts = urlsplit(c["url"])
            res = fetch(urljoin(f"{parts.scheme}://{parts.netloc}", c["readyz"]))
            if res["status"] != 200:
                records = {"error": f"{c['app']}: /readyz " + (res["error"] or f"HTTP {res['status']}")}
            else:
                try:
                    records = parse_app_records(c["app"], json.loads(res["body"] or "null"))
                except ValueError:  # a text /readyz (asas, haqiqi today) carries no records block
                    records = None
        active, cfg_err = None, None
        if c.get("records_activated_at"):
            try:
                active = parse_any_iso(str(c["records_activated_at"]))
            except ValueError:
                cfg_err = "checks.json: records_activated_at is not a time; alerts for this app are off"
        bal = balances.get(c["relayer"])
        return {"app": c["app"], "relayer": c["relayer"], "balance_mon": bal,
                "balance_error": berr or (None if bal is not None else "balance not read"),
                "records": records, "activated_at": active, "config_error": cfg_err}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, rcs))


def relayer_conditions(r: dict[str, Any], now: dt.datetime) -> dict[str, str]:
    """Alert kind -> reason, for an activated app only. low_funds: chain balance under the refill threshold;
    reserve: at the 10 MON reserve (anchoring paused); cap: the daily MON cap was hit and records wait; stuck: a
    record waited over RECORDS_STUCK_MIN since activation. Unknown inputs open nothing."""
    out: dict[str, str] = {}
    active = r.get("activated_at")
    if active is None:
        return out
    bal = r.get("balance_mon")
    if bal is not None:
        if bal < RECORDS_REFILL_MON:
            out["low_funds"] = f"the relayer holds {bal:g} MON on Monad, under the {RECORDS_REFILL_MON:g} MON refill threshold"
        if bal < RECORDS_RESERVE_MON + RECORDS_RESERVE_MARGIN_MON:
            out["reserve"] = f"the relayer holds {bal:g} MON, at the {RECORDS_RESERVE_MON:g} MON reserve: anchoring is paused"
    rec = r.get("records")
    if rec is not None and "error" not in rec and rec["waiting"] > 0:
        if rec["cap_alert"]:
            cap = f" of {rec['cap_mon']:g}" if rec.get("cap_mon") is not None else ""
            out["cap"] = (f"the daily MON cap was hit (spent today: {rec['spent_mon']:g}{cap} MON); {rec['waiting']} "
                          "record(s) wait for the next UTC day")
        stuck_min = min(rec["oldest_waiting_min"], (now - active).total_seconds() / 60)
        if stuck_min > RECORDS_STUCK_MIN:
            out["stuck"] = (f"{rec['waiting']} record(s) not anchored; the oldest has waited "
                            f"{fmt_duration(stuck_min * 60)} since anchoring was activated")
    return out


def relayer_unknown(r: dict[str, Any]) -> set[str]:
    """Kinds whose input could not be read this run: their issues stay as they are."""
    out: set[str] = set()
    if r.get("balance_mon") is None:
        out |= {"low_funds", "reserve"}
    rec = r.get("records")
    if rec is not None and "error" in rec:
        out |= {"cap", "stuck"}
    return out


def relayer_line(r: dict[str, Any]) -> str:
    bal = f"{r['balance_mon']:g} MON" if r.get("balance_mon") is not None else f"balance unknown ({r.get('balance_error')})"
    rec = r.get("records")
    if rec is None:
        rtxt = "records via the hub" if r["app"] == RECORDS_APP else "no records block (layer off)"
    elif "error" in rec:
        rtxt = f"records unavailable ({rec['error']})"
    else:
        cap = f"/{rec['cap_mon']:g}" if rec.get("cap_mon") is not None else ""
        rtxt = (f"{rec['spent_mon']:g}{cap} MON spent today, {rec['anchors']} anchor(s), {rec['waiting']} waiting "
                f"(oldest {rec['oldest_waiting_min']:.0f} min)")
    act = "" if r.get("activated_at") else "; not activated (no alerts)"
    return f"{r['app']}: relayer {bal}; {rtxt}{act}"


def process_relayer_alerts(gh: Any, state: dict[str, Any], rows: list[dict[str, Any]], now: dt.datetime,
                           errors: list[str]) -> None:
    """One issue "RECORDS <KIND>: <app>" (label `records`) per active condition, keyed "<app>/<kind>" next to the
    hub's keys in records_alerts. Reused if already open, not commented on while it lasts, closed when it clears.
    An input that could not be read changes nothing for its kinds. The hub's own keys are never touched here."""
    alerts: dict[str, Any] = state.setdefault("records_alerts", {})
    stamp = iso(now)
    for r in rows:
        app = r["app"]
        if app == RECORDS_APP:
            continue
        if r.get("config_error"):
            errors.append(r["config_error"])
        conds, unknown = relayer_conditions(r, now), relayer_unknown(r)
        for key in [k for k in alerts if k.split("/", 1)[0] == app]:
            kind = key.split("/", 1)[1]
            if kind in conds or kind in unknown:
                continue
            try:
                gh.comment(alerts[key]["issue"], f"Cleared ({relayer_line(r)}) at {stamp}. Closing.")
                gh.close(alerts[key]["issue"])
                print(f"closed records issue #{alerts[key]['issue']} ({key})")
                del alerts[key]
            except RuntimeError as e:
                errors.append(str(e))
        for kind, reason in conds.items():
            key = f"{app}/{kind}"
            if key in alerts:
                continue
            title = f"RECORDS {RELAYER_TITLE[kind]}: {app}"
            body = (f"**{app}** Monad records: {reason}.\n\n{RELAYER_ACTION[kind]}\n\nReading at {stamp}: {relayer_line(r)}."
                    f"\n\nRelayer `{r['relayer']}` (balance read from {MONAD_RPC_URL}, chain {MONAD_CHAIN_ID}). This issue "
                    "closes automatically when the condition clears.")
            try:
                existing = gh.find_open_issue(title, RECORDS_LABEL)
                if existing:
                    gh.comment(existing, f"Again at {stamp}: {reason}.")
                    num = existing
                else:
                    num = gh.create_issue(title, body, [RECORDS_LABEL])
                alerts[key] = {"issue": num, "since": stamp}
                print(f"opened/updated records issue #{num} ({key})")
            except RuntimeError as e:
                errors.append(str(e))


def voice_keepalive(token: str) -> str:
    """POST /api/ops/voice-keepalive once a day (with the summary). The hub pays for at most one short phrase per cloned
    voice, and only for a voice with no paid call in 5 days. Returns one line for the summary."""
    try:
        r = hub_call("POST", "/api/ops/voice-keepalive", token)
    except RuntimeError as e:
        return f"failed ({e})"
    if not isinstance(r, dict):
        return "unexpected answer"
    refreshed = [str(x) for x in r.get("refreshed") or []]
    failed = [f"{x.get('voice')} ({x.get('why')})" for x in r.get("failed") or [] if isinstance(x, dict)]
    return (f"refreshed {', '.join(refreshed) or 'none'}"
            + (f"; **failed {', '.join(failed)}**" if failed else "")
            + f"; checked {len(r.get('voices') or [])} voice(s).")


# ------------------------------------------------------------- GitHub API
class GitHub:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GitHub API {method} {urlsplit(url).path} -> HTTP {e.code}") from None
        return json.loads(raw) if raw else None

    def r(self, suffix: str) -> str:
        return f"/repos/{self.repo}{suffix}"

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self.call("GET", self.r(f"/labels/{name}"))
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
            self.call("POST", self.r("/labels"), {"name": name, "color": color, "description": description})

    def find_open_issue(self, title: str, label: str | None = None) -> int | None:
        q = "?state=open&per_page=100" + (f"&labels={label}" if label else "")
        for issue in self.call("GET", self.r(f"/issues{q}")) or []:
            if "pull_request" not in issue and issue["title"] == title:
                return int(issue["number"])
        return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        return int(self.call("POST", self.r("/issues"), {"title": title, "body": body, "labels": labels})["number"])

    def comment(self, number: int, body: str) -> None:
        self.call("POST", self.r(f"/issues/{number}/comments"), {"body": body})

    def close(self, number: int) -> None:
        self.call("PATCH", self.r(f"/issues/{number}"), {"state": "closed", "state_reason": "completed"})

    def try_pin(self, number: int) -> None:
        try:
            node = self.call("GET", self.r(f"/issues/{number}"))["node_id"]
            self.call("POST", "https://api.github.com/graphql", {
                "query": "mutation($id:ID!){pinIssue(input:{issueId:$id}){issue{number}}}",
                "variables": {"id": node},
            })
        except (RuntimeError, KeyError, TypeError) as e:
            print(f"note: could not pin issue #{number} ({e}); pin it once by hand", flush=True)


# ------------------------------------------------------------ state files
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def significant(state: dict[str, Any]) -> Any:
    """The part of the state whose change justifies a commit."""
    apps = {
        name: {k: a.get(k) for k in ("status", "down_since", "issue", "reasons")}
        for name, a in state.get("apps", {}).items()
    }
    alerts = {name: {k: a.get(k) for k in ("issue", "day")} for name, a in state.get("spend_alerts", {}).items()}
    records = {name: a.get("issue") for name, a in state.get("records_alerts", {}).items()}
    return {"apps": apps, "outages": state.get("outages", []), "last_summary": state.get("last_summary"),
            "spend_alerts": alerts, "records_alerts": records}


def uptime_24h(app: str, outages: list[dict[str, Any]], now: dt.datetime) -> float:
    start = now - dt.timedelta(hours=24)
    down = 0.0
    for o in outages:
        if o["app"] != app:
            continue
        s = max(parse_iso(o["start"]), start)
        e = min(parse_iso(o["end"]) if o.get("end") else now, now)
        if e > s:
            down += (e - s).total_seconds()
    return max(0.0, 100.0 * (1 - down / 86400))


# ------------------------------------------------------------ reporting
def table(results: list[dict[str, Any]]) -> str:
    def mark(v: bool) -> str:
        return "yes" if v else "NO"

    rows = ["| app | result | HTTP | keyword | judge | healthz | cert days | ms | attempts | notes |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        rows.append("| {app} | {res} | {st} | {kw} | {j} | {h} | {c} | {ms} | {a} | {n} |".format(
            app=r["app"], res="UP" if r["ok"] else "DOWN", st=r["status"] if r["status"] is not None else "-",
            kw=mark(r["keyword_ok"]), j=r["judge_status"] if r["judge_status"] is not None else "-",
            h=r["healthz_status"] if r["healthz_status"] is not None else "-",
            c=r["cert_days"] if r["cert_days"] is not None else "?", ms=r["ms"], a=r["attempts"],
            n="; ".join(r["reasons"]) or ""))
    return "\n".join(rows)


def summary_body(checks: list[dict[str, Any]], results: dict[str, dict[str, Any]],
                 state: dict[str, Any], samples: dict[str, list[list[Any]]], now: dt.datetime,
                 spend_rows: list[dict[str, Any]] | None = None, keepalive: str | None = None,
                 records: dict[str, Any] | None = None, relayers: list[dict[str, Any]] | None = None) -> str:
    lines = [f"**Daily status, {now.strftime('%Y-%m-%d %H:%M')} UTC** (last 24 h)", "",
             "| app | uptime 24h | avg ms | max ms | cert days | now |", "|---|---|---|---|---|---|"]
    slowest: tuple[str, float] | None = None
    for c in checks:
        app = c["app"]
        r = results[app]
        ms_list = [s[2] for s in samples.get(app, [])] or [r["ms"]]
        avg = sum(ms_list) / len(ms_list)
        if slowest is None or avg > slowest[1]:
            slowest = (app, avg)
        lines.append(f"| {app} | {uptime_24h(app, state.get('outages', []), now):.2f}% | {avg:.0f} | "
                     f"{max(ms_list)} | {r['cert_days'] if r['cert_days'] is not None else '?'} | "
                     f"{'UP' if r['ok'] else 'DOWN'} |")
    n = max((len(samples.get(c["app"], [])) for c in checks), default=0)
    certs = sorted((r["cert_days"] if r["cert_days"] is not None else -1, a) for a, r in results.items())
    lines += ["", f"Slowest app: **{slowest[0]}** (avg {slowest[1]:.0f} ms)" if slowest else "",
              f"Soonest certificate expiry: **{certs[0][1]}** ({certs[0][0]} days left)." if certs else "",
              f"Latency is from {n} sample(s) in the last 24 h; uptime is from recorded outages."]
    times = run_times(samples)
    if not times:
        lines += ["", "**Run gaps:** unknown (no run history in the cache)."]
    else:
        gaps = run_gaps(times, int(now.timestamp()))
        if gaps:
            lines += ["", f"**Run gaps over {GAP_FLAG_S // 60} min: {len(gaps)}** ({len(times)} run(s) in 24 h): "
                          + fmt_gaps(gaps)]
        else:
            lines += ["", f"Run gaps: none over {GAP_FLAG_S // 60} min ({len(times)} run(s) in 24 h)."]
    if spend_rows:
        lines += ["", "**AI spend today** (each app's own day; used = spent + reserved over the daily cap; "
                      f"an alert opens at {SPEND_ALERT_PCT:.0f}%)", "", spend_table(spend_rows)]
    if records is not None:
        lines += ["", f"**Monad records (hub):** {records_line(records)}"]
    if relayers:
        lines += ["", "**Monad records, per app (relayer balance on chain, /readyz block):**", ""]
        lines += [f"- {relayer_line(r)}" for r in relayers]
    if keepalive:
        lines += ["", f"Voice keep-alive: {keepalive}"]
    return "\n".join(lines)


# ------------------------------------------------------------------ main
# ---------------------------------------------------------------- source watch dead-man switch
WATCH_LABEL = "source-watch"


def parse_any_iso(v: str) -> dt.datetime:
    """An RFC 3339 time as apps write it (Z or an offset, optional fractions)."""
    t = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def watch_stale(doc: Any, now: dt.datetime) -> str | None:
    """Why an app's source watch counts as stale, from its /.well-known/sources-status.json, or
    None. Stale: the last report is older than the app's own limit (stale_after_hours, 48 by
    default), or the verify endpoint is open and no report ever arrived. An unreadable status is
    not judged here (the uptime check covers a down app)."""
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    limit = float(doc.get("stale_after_hours") or 48)
    last = doc.get("last_report_at")
    if last:
        try:
            age = (now - parse_any_iso(str(last))).total_seconds()
        except ValueError:
            return f"last_report_at is not a time ({str(last)[:40]})"
        if age > limit * 3600:
            return f"no source watch report for {fmt_duration(age)} (last {last}); the app shows its checks as paused"
        return None
    if doc.get("verify_open"):
        return "the verify endpoint is open but no source watch report has ever arrived"
    return None


def fetch_watch_status(check: dict[str, Any]) -> dict[str, Any]:
    parts = urlsplit(check["url"])
    res = fetch(urljoin(f"{parts.scheme}://{parts.netloc}", check["sources_status"]))
    doc = None
    if res["status"] == 200 and res["body"]:
        try:
            doc = json.loads(res["body"])
        except ValueError:
            doc = None
    return {"app": check["app"], "doc": doc}


def process_watch_alerts(gh: Any, state: dict[str, Any], rows: list[dict[str, Any]], now: dt.datetime,
                         errors: list[str]) -> None:
    """One issue "SOURCE WATCH STALE: <app>" (label source-watch) while an app's source watch is
    stale; it closes when a report arrives again or the check is removed."""
    alerts: dict[str, Any] = state.setdefault("watch_alerts", {})
    stamp = iso(now)
    why = {r["app"]: watch_stale(r["doc"], now) for r in rows}
    readable = {r["app"] for r in rows if r["doc"] is not None}
    for app in list(alerts):
        if app not in why or (app in readable and why[app] is None):
            try:
                gh.comment(alerts[app]["issue"], f"Source watch reports again (or the check was removed) at {stamp}. Closing.")
                gh.close(alerts[app]["issue"])
                del alerts[app]
            except RuntimeError as e:
                errors.append(str(e))
    for app, reason in why.items():
        if reason is None or app in alerts:
            continue
        title = f"SOURCE WATCH STALE: {app}"
        body = (f"**{app}**: {reason}.\n\nThe app stops showing the watch's dates after 48 h without a report, so nothing "
                "looks checked that is not, but nobody is checking its official pages either. Look at the `source-watch` "
                "workflow runs and the dispatch Worker. This issue closes when a report arrives.")
        try:
            existing = gh.find_open_issue(title, WATCH_LABEL)
            num = existing or gh.create_issue(title, body, [WATCH_LABEL])
            if existing:
                gh.comment(existing, f"Still stale at {stamp}: {reason}")
            alerts[app] = {"issue": num, "since": stamp}
        except RuntimeError as e:
            errors.append(str(e))


def main() -> int:
    checks: list[dict[str, Any]] = load_json(CHECKS_FILE, [])
    if not checks:
        print("checks.json is empty or missing", file=sys.stderr)
        return 2
    now = now_utc()
    results_list = run_checks(checks)
    results = {r["app"]: r for r in results_list}
    out = table(results_list)
    print(out, flush=True)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"### Uptime check {iso(now)}\n\n{out}\n")

    # latency samples (cache only, never committed); they also give the run history for the gap flag
    samples: dict[str, list[list[Any]]] = load_json(SAMPLES_FILE, {})
    prev = run_times(samples)
    gap_note = (f"previous run {fmt_duration(now.timestamp() - prev[-1])} ago"
                + (f" (**over {GAP_FLAG_S // 60} min**)" if now.timestamp() - prev[-1] > GAP_FLAG_S else "")
                if prev else "no previous run in the cache")
    print(f"gap: {gap_note}", flush=True)
    cutoff = time.time() - 86400
    for r in results_list:
        samples.setdefault(r["app"], []).append([int(time.time()), int(r["ok"]), r["ms"]])
    samples = {a: [s for s in v if s[0] >= cutoff] for a, v in samples.items() if a in results}

    # AI spend: each app's public /readyz, plus the hub's /api/ops/spend when OPS_TOKEN is set
    spend_checks = [c for c in checks if c.get("readyz")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        spend_rows = list(pool.map(fetch_spend, spend_checks))
    configured = {c["app"] for c in spend_checks}
    ops_token = (os.environ.get("OPS_TOKEN") or "").strip()
    # Monad records from the same hub response: None = not configured on the hub, {"error"} = unreadable.
    records: dict[str, Any] | None = None
    if ops_token:
        configured |= {f"sawweeli-hub/{k}" for k in ("llm", "tts", "stt")}
        try:
            hub_spend = hub_call("GET", "/api/ops/spend", ops_token)
            spend_rows += parse_hub_spend(hub_spend)
            records = parse_hub_records(hub_spend)
        except RuntimeError as e:
            print(f"note: {e}", flush=True)
            records = {"error": str(e)}
    stbl = spend_table(spend_rows)
    print(stbl, flush=True)
    if ops_token:
        print(f"Monad records: {records_line(records)}", flush=True)
    # Monad records per app, with no key: relayer balances from the public RPC, /readyz records blocks.
    relayers = collect_relayers(checks)
    for r in relayers:
        print(f"Monad records: {relayer_line(r)}", flush=True)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(f"\nGap since the previous run: {gap_note}\n\n### AI spend\n\n{stbl}\n")
            if ops_token:
                f.write(f"\n### Monad records\n\n{records_line(records)}\n")
            if relayers:
                f.write("\n### Monad records per app\n\n" + "".join(f"- {relayer_line(r)}\n" for r in relayers))

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("dry run: GITHUB_TOKEN/GITHUB_REPOSITORY not set, no issues and no state written")
        return 0
    gh = GitHub(token, repo)

    state: dict[str, Any] = load_json(STATE_FILE, {"apps": {}, "outages": [], "last_summary": None})
    before = json.dumps(significant(state), sort_keys=True)
    apps: dict[str, Any] = state.setdefault("apps", {})
    outages: list[dict[str, Any]] = state.setdefault("outages", [])
    errors: list[str] = []
    stamp = iso(now)

    try:
        gh.ensure_label(OUTAGE_LABEL, "d73a4a", "An app failed its uptime check twice in a row")
    except RuntimeError as e:
        errors.append(str(e))

    for c in checks:
        app = c["app"]
        r = results[app]
        a = apps.setdefault(app, {"status": "up", "since": stamp, "down_since": None, "issue": None})
        title = f"DOWN: {app}"
        if not r["ok"]:
            if a["status"] != "down":
                a.update(status="down", since=stamp, down_since=stamp)
                outages.append({"app": app, "start": stamp, "end": None})
            a["reasons"] = r["reasons"]
            if a.get("issue") is None:
                body = (f"**{app}** failed its uptime check twice (30 s apart) at {stamp}.\n\n"
                        f"- URL: {c['url']}\n- Judge path: {c.get('judge_path') or '-'}\n"
                        f"- Reasons: {'; '.join(r['reasons'])}\n- Response time: {r['ms']} ms\n\n"
                        "This issue closes automatically when the app recovers.")
                try:
                    existing = gh.find_open_issue(title, OUTAGE_LABEL)
                    if existing:
                        gh.comment(existing, f"Down again at {stamp}: {'; '.join(r['reasons'])}")
                        a["issue"] = existing
                    else:
                        a["issue"] = gh.create_issue(title, body, [OUTAGE_LABEL])
                    print(f"opened/updated issue #{a['issue']} for {app}")
                except RuntimeError as e:
                    errors.append(str(e))
        else:
            if a["status"] == "down":
                a["last_outage_s"] = (now - parse_iso(a["down_since"])).total_seconds()
                for o in outages:
                    if o["app"] == app and o.get("end") is None:
                        o["end"] = stamp
                a.update(status="up", since=stamp, down_since=None)
            a["reasons"] = []
            if a.get("issue") is not None:
                dur = fmt_duration(a.get("last_outage_s", 0))
                try:
                    gh.comment(a["issue"], f"Recovered at {stamp}. Downtime: **{dur}**.")
                    gh.close(a["issue"])
                    print(f"closed issue #{a['issue']} for {app} (down {dur})")
                    a["issue"] = None
                except RuntimeError as e:
                    errors.append(str(e))
        a["cert_days"] = r["cert_days"]
        a["ms"] = r["ms"]

    # checks removed from checks.json: close their issues and forget them
    for app in [x for x in apps if x not in results]:
        a = apps[app]
        if a.get("down_since"):
            for o in outages:
                if o["app"] == app and o.get("end") is None:
                    o["end"] = stamp
        if a.get("issue") is not None:
            since = a.get("down_since") or stamp
            dur = fmt_duration((now - parse_iso(since)).total_seconds())
            try:
                gh.comment(a["issue"], f"Check removed from checks.json at {stamp}. "
                                       f"It had been down for **{dur}**. Closing.")
                gh.close(a["issue"])
                print(f"closed issue #{a['issue']} for removed check {app} (down {dur})")
            except RuntimeError as e:
                errors.append(str(e))
                continue
        del apps[app]

    try:
        gh.ensure_label(SPEND_LABEL, "fbca04", "An app reached 80% of its daily AI spend cap")
        process_spend_alerts(gh, state, spend_rows, configured, now, errors)
    except RuntimeError as e:
        errors.append(str(e))

    # Monad records alerts (only when the hub was asked: without OPS_TOKEN nothing is known, nothing changes)
    if ops_token:
        try:
            activated_at = records_activation(checks)
        except ValueError:
            activated_at = None
            errors.append("checks.json: records_activated_at is not a time; the stuck-records alert is off")
        try:
            gh.ensure_label(RECORDS_LABEL, "0e8a16", "Monad records: relayer refill, reserve, daily cap, stuck records")
            process_records_alerts(gh, state, records, now, activated_at, errors)
        except RuntimeError as e:
            errors.append(str(e))

    if relayers:
        try:
            gh.ensure_label(RECORDS_LABEL, "0e8a16", "Monad records: relayer refill, reserve, daily cap, stuck records")
            process_relayer_alerts(gh, state, relayers, now, errors)
        except RuntimeError as e:
            errors.append(str(e))

    # source watch dead-man switch: apps that serve /.well-known/sources-status.json
    watch_checks = [c for c in checks if c.get("sources_status")]
    if watch_checks:
        with ThreadPoolExecutor(max_workers=8) as pool:
            watch_rows = list(pool.map(fetch_watch_status, watch_checks))
        try:
            gh.ensure_label(WATCH_LABEL, "5319e7", "Source watch: official pages the apps cite")
            process_watch_alerts(gh, state, watch_rows, now, errors)
        except RuntimeError as e:
            errors.append(str(e))

    keep_after = now - dt.timedelta(days=OUTAGE_KEEP_DAYS)
    state["outages"] = [o for o in outages if o.get("end") is None or parse_iso(o["end"]) >= keep_after]

    # daily summary (also the daily keep-alive commit)
    today = now.strftime("%Y-%m-%d")
    force = os.environ.get("FORCE_SUMMARY", "").lower() in ("1", "true", "yes")
    if force or (now.hour >= SUMMARY_HOUR_UTC and state.get("last_summary") != today):
        try:
            num = gh.find_open_issue(SUMMARY_TITLE)
            if num is None:
                num = gh.create_issue(SUMMARY_TITLE, "Daily uptime summary, one comment per day at 14:00 UTC.", [])
                gh.try_pin(num)
            keepalive = voice_keepalive(ops_token) if ops_token else None
            gh.comment(num, summary_body(checks, results, state, samples, now, spend_rows, keepalive,
                                         records if ops_token else None, relayers))
            if not force:  # a manual summary does not replace the scheduled one
                state["last_summary"] = today
            print(f"posted daily summary to issue #{num}")
        except RuntimeError as e:
            errors.append(str(e))

    changed = json.dumps(significant(state), sort_keys=True) != before
    if changed:
        state["updated"] = stamp
        save_json(STATE_FILE, state)
    save_json(SAMPLES_FILE, samples)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
    print(f"state changed: {changed}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
