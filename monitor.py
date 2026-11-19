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
import os
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
    return {"apps": apps, "outages": state.get("outages", []), "last_summary": state.get("last_summary")}


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
                 state: dict[str, Any], samples: dict[str, list[list[Any]]], now: dt.datetime) -> str:
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
    return "\n".join(lines)


# ------------------------------------------------------------------ main
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

    # latency samples (cache only, never committed)
    samples: dict[str, list[list[Any]]] = load_json(SAMPLES_FILE, {})
    cutoff = time.time() - 86400
    for r in results_list:
        samples.setdefault(r["app"], []).append([int(time.time()), int(r["ok"]), r["ms"]])
    samples = {a: [s for s in v if s[0] >= cutoff] for a, v in samples.items() if a in results}

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
            gh.comment(num, summary_body(checks, results, state, samples, now))
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
