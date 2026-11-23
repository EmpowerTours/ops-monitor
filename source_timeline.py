"""Public timeline of official page readings (F2, docs/SOURCE-WATCH.md "Public timeline").

For each reading the source watch makes:
- asks the Internet Archive's Save Page Now to keep its own copy (an independent witness with its
  own timestamp), politely: one capture request per min_interval_s, a per-run cap, stop on 429;
- builds a PUBLIC-mode `source_read` record (url, sha256 of the normalised text, fetched_at,
  status, the app's short quotes, the archive URL) and queues it for the proof batcher, which
  anchors it later (anchoring is not wired yet: NotWiredAnchorer);
- appends an entry to the source's public JSON timeline.

Never page text: only hashes, URLs, times, statuses and the app's own short quotes (already public
in its manifest). Every public file is validated before it is committed (validate_record,
validate_timeline, validate_queue_file). Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Protocol, runtime_checkable

QUOTE_MAX_CHARS = 400  # a published quote stays a short quote, never a passage
MAX_STR = 2048
HASH_OF = "normalised-text-v1"  # sha256 over the watcher's normal form of the page text

# A reading queued before Save Page Now ran: the archive step after the reports resolves it.
PENDING = "pending: Save Page Now runs after the reports"
DEFERRED = "not archived: deferred (per-run cap)"

SAVE_URL = "https://web.archive.org/save"
STATUS_URL = "https://web.archive.org/save/status/"
WEB_URL = "https://web.archive.org/web/"

RECORD_KEYS = frozenset({"v", "mode", "app", "kind", "ref", "payload", "payload_sha256", "queued_at", "anchor"})
PAYLOAD_KEYS = frozenset({"source_id", "cited_by", "url", "url_ar", "sha256", "sha256_ar", "hash_of", "fetched_at", "status",
                          "http_status", "quotes", "quotes_ar", "archive_url", "archive_status", "run_id"})
TIMELINE_KEYS = frozenset({"v", "source_id", "cited_by", "url", "url_ar", "entries"})
ENTRY_KEYS = frozenset({"fetched_at", "status", "http_status", "sha256", "sha256_ar", "quotes", "quotes_ar", "archive_url",
                        "archive_status", "run_id", "ref", "payload_sha256", "changed_since_previous"})


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).split())


def canonical(obj: Any) -> bytes:
    """The bytes a verifier hashes: sorted keys, no spaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def public_quotes(quotes: list[dict] | None) -> list[str]:
    """The app's quotes that are short enough to publish; longer ones are left out."""
    out = []
    for q in quotes or []:
        t = _norm(q.get("text", "")) if isinstance(q, dict) else ""
        if t and len(t) <= QUOTE_MAX_CHARS:
            out.append(t)
    return out


# ---------------------------------------------------------------------------------------------
# Save Page Now


class Archiver:
    """Save Page Now (SPN2): POST /save with the page URL, then poll /save/status/<job_id>.

    Polite by construction: one capture request per min_interval_s (anonymous SPN2 allows about
    3 per minute), at most max_per_run captures, if_not_archived_within so a page the Archive
    already saved today is not saved again, and the first HTTP 429 stops archiving for the run.
    Sends only the page URL and that window, with a generic user agent; the key (SPN2 S3 key
    "access:secret") only when configured. Never raises: every failure becomes a status."""

    def __init__(self, opener: Callable[..., Any] | None = None, sleep: Callable[[float], None] = time.sleep,
                 user_agent: str = "source-watch/1.0", auth: str = "", min_interval_s: float = 21.0, max_per_run: int = 40,
                 poll_s: float = 10.0, poll_rounds: int = 12, not_within: str = "20h", timeout_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic, deadline: float | None = None):
        self.opener, self.sleep = opener or urllib.request.urlopen, sleep
        self.ua, self.auth, self.min_interval, self.max_per_run = user_agent, auth, min_interval_s, max_per_run
        self.poll_s, self.poll_rounds, self.not_within, self.timeout = poll_s, poll_rounds, not_within, timeout_s
        # By `deadline` (clock seconds) everything is done: no capture starts unless there is still
        # time to poll for it; what is not started is "deferred" to a later run, not lost.
        self.clock, self.deadline = clock, deadline

    def _call(self, url: str, data: dict | None = None) -> dict:
        headers = {"User-Agent": self.ua, "Accept": "application/json"}
        if self.auth:
            headers["Authorization"] = "LOW " + self.auth
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method="POST" if data is not None else "GET")
        with self.opener(req, timeout=self.timeout) as r:
            doc = json.loads(r.read(1 << 20) or b"{}")
        return doc if isinstance(doc, dict) else {}

    @staticmethod
    def _failed(why: str) -> dict:
        return {"archive_url": "", "archive_status": ("not archived: " + why)[:200]}

    def archive(self, urls: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        jobs: dict[str, str] = {}
        stop, sent = "", 0
        for url in dict.fromkeys(urls):  # once per run, in order
            if not re.match(r"https?://[^\s/]+", url or ""):
                out[url] = self._failed("not an http(s) URL")
                continue
            if stop:
                out[url] = self._failed(stop)
                continue
            if sent >= self.max_per_run:
                out[url] = self._failed(f"per-run cap of {self.max_per_run} reached")
                continue
            if self.deadline is not None and \
                    self.clock() + (self.min_interval if sent else 0) + self.poll_s * self.poll_rounds > self.deadline:
                out[url] = {"archive_url": "", "archive_status": DEFERRED}
                continue
            if sent:
                self.sleep(self.min_interval)
            sent += 1
            try:
                doc = self._call(SAVE_URL, {"url": url, "if_not_archived_within": self.not_within})
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    stop = "rate limited (HTTP 429); stopped for this run"
                    out[url] = self._failed(stop)
                else:
                    out[url] = self._failed(f"HTTP {e.code}")
                continue
            except (OSError, ValueError) as e:
                out[url] = self._failed(type(e).__name__)
                continue
            if doc.get("job_id"):
                jobs[url] = str(doc["job_id"])
            else:
                out[url] = self._failed(str(doc.get("status_ext") or doc.get("message") or "no job id"))
        for rnd in range(self.poll_rounds):
            if not jobs or (self.deadline is not None and self.clock() + self.poll_s > self.deadline):
                break
            self.sleep(self.poll_s)
            for url, job in list(jobs.items()):
                try:
                    doc = self._call(STATUS_URL + urllib.parse.quote(job, safe=""))
                except (OSError, ValueError) as e:
                    doc = {"status": "pending"} if rnd + 1 < self.poll_rounds else {"status": "error", "status_ext": type(e).__name__}
                st = doc.get("status")
                if st == "success" and re.fullmatch(r"\d{14}", str(doc.get("timestamp", ""))):
                    out[url] = {"archive_url": f"{WEB_URL}{doc['timestamp']}/{doc.get('original_url') or url}", "archive_status": "archived"}
                    del jobs[url]
                elif st not in ("pending", None):
                    out[url] = self._failed(str(doc.get("status_ext") or doc.get("message") or st))
                    del jobs[url]
        for url in jobs:
            out[url] = self._failed(f"still pending after {self.poll_rounds} status checks")
        return out


# ---------------------------------------------------------------------------------------------
# Public source_read record and the queue for the proof batcher


def source_read_record(cited_by: str, reading: dict, quotes: list[dict] | None, quotes_ar: list[dict] | None, archive: dict,
                       run_id: str, queued_at: str) -> dict:
    """A PUBLIC-mode `source_read` record (MONAD-RECORDS.md 3.1 and 3.3). The payload is all a
    verifier needs; payload_sha256 = sha256(canonical(payload)). The proof batcher computes the
    public leaf from app, kind, ref and payload when anchoring is wired."""
    payload = {
        "source_id": reading["source_id"], "cited_by": cited_by, "url": reading["url"],
        "sha256": reading.get("sha256", ""), "hash_of": HASH_OF, "fetched_at": reading.get("fetched_at", ""),
        "status": reading.get("status", ""), "http_status": int(reading.get("http_status") or 0),
        "quotes": public_quotes(quotes), "archive_url": archive.get("archive_url", ""),
        "archive_status": archive.get("archive_status", ""), "run_id": run_id,
    }
    if reading.get("url_ar"):
        payload.update(url_ar=reading["url_ar"], sha256_ar=reading.get("sha256_ar", ""), quotes_ar=public_quotes(quotes_ar))
    return {"v": 1, "mode": "public", "app": "ops", "kind": "source_read",
            "ref": f"{cited_by}/{reading['source_id']}/{reading.get('fetched_at', '')}",
            "payload": payload, "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
            "queued_at": queued_at, "anchor": None}


def key_name(k: Any) -> str:
    """A key as it may appear in a guard message: a field name, or nothing of its content."""
    return repr(k) if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", k) else "(a key that is not a field name)"


def _check_scalars(where: str, d: dict, allowed: frozenset) -> list[str]:
    out = [f"{where}: key {key_name(k)} may not be public" for k in sorted(set(d) - allowed, key=str)]
    for k, v in d.items():
        if k in ("quotes", "quotes_ar"):
            if not isinstance(v, list) or not all(isinstance(q, str) and len(q) <= QUOTE_MAX_CHARS and "\n" not in q for q in v):
                out.append(f"{where}: {k} must be short single-line quotes (<= {QUOTE_MAX_CHARS} characters)")
        elif isinstance(v, str):
            if len(v) > MAX_STR or "\n" in v:
                out.append(f"{where}: {key_name(k)} must be a short single-line string (value not shown)")
        elif not (v is None or isinstance(v, (bool, int))):
            out.append(f"{where}: {key_name(k)} must be a scalar (value not shown)")
    return out


def validate_record(r: Any) -> list[str]:
    """Problems that keep a record from being public (empty: fine)."""
    if not isinstance(r, dict):
        return ["record: not an object"]
    out = [f"record: key {key_name(k)} may not be public" for k in sorted(set(r) - RECORD_KEYS, key=str)]
    if r.get("mode") != "public" or r.get("kind") != "source_read":
        out.append("record: only public source_read records go here")
    p = r.get("payload")
    if not isinstance(p, dict):
        return out + ["record: payload must be an object"]
    out += _check_scalars("payload", p, PAYLOAD_KEYS)
    out += _check_scalars("record", {k: v for k, v in r.items() if k not in ("payload", "anchor")}, RECORD_KEYS)
    if r.get("payload_sha256") != hashlib.sha256(canonical(p)).hexdigest():
        out.append("record: payload_sha256 does not match the payload")
    return out


@runtime_checkable
class Anchorer(Protocol):
    """What the proof batcher implements: anchor queued public records, return their receipts."""

    def anchor(self, records: list[dict]) -> list[dict]: ...


class NotWiredAnchorer:
    """Anchoring is not wired yet: records wait in the queue."""

    def anchor(self, records: list[dict]) -> list[dict]:
        return []


class ProofQueue:
    """Append-only JSON Lines file of public records waiting for the proof batcher. One line per
    ref: a re-dispatched run does not queue a reading twice."""

    def __init__(self, path: str):
        self.path = path

    def _all(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except OSError:
            return []

    def enqueue(self, record: dict) -> bool:
        if any(x.get("ref") == record["ref"] for x in self._all()):
            return False
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        return True

    def pending(self) -> list[dict]:
        return [x for x in self._all() if x.get("anchor") is None]

    def rewrite(self, records: list[dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)


def validate_queue_file(path: str) -> list[str]:
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    out.append(f"line {i}: not JSON")
                    continue
                out += [f"line {i}: {x}" for x in validate_record(rec)]
    except OSError as e:
        out.append(f"not readable ({type(e).__name__})")
    return out


# ---------------------------------------------------------------------------------------------
# Public JSON timeline per source


def resolve_pending(state: str, archiver: Any, why_off: str = "") -> dict[str, int]:
    """The Save Page Now step, after the reports: archive the readings queued as PENDING (at most
    archiver.max_per_run pages, each URL once), then write the archive URL into each record and its
    timeline entry and recompute payload_sha256. A record already anchored is sealed and never
    touched. Over the cap: "deferred", nothing lost. No archiver: the reason, why_off."""
    q = ProofQueue(os.path.join(state, "proof-queue", "source_read.jsonl"))
    recs = q._all()
    todo = [r for r in recs if r.get("anchor") is None and r.get("payload", {}).get("archive_status") == PENDING]
    if not todo:
        return {"archived": 0, "deferred": 0}
    urls = list(dict.fromkeys(r["payload"]["url"] for r in todo))
    cap = int(getattr(archiver, "max_per_run", 0)) if archiver else 0
    got = archiver.archive(urls[:cap]) if archiver and cap else {}
    n = {"archived": 0, "deferred": 0}
    for r in todo:
        p = r["payload"]
        a = got.get(p["url"]) or {"archive_url": "", "archive_status": DEFERRED if archiver else "not archived: " + (why_off or "Save Page Now is off")}
        p["archive_url"], p["archive_status"] = a.get("archive_url", ""), a.get("archive_status", "")[:200]
        r["payload_sha256"] = hashlib.sha256(canonical(p)).hexdigest()
        n["archived"] += p["archive_status"] == "archived"
        n["deferred"] += p["archive_status"] == DEFERRED
        path = os.path.join(state, "timeline", p["cited_by"], p["source_id"] + ".json")
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in doc.get("entries", []):
            if e.get("ref") == r["ref"]:
                e.update(archive_url=p["archive_url"], archive_status=p["archive_status"], payload_sha256=r["payload_sha256"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
    q.rewrite(recs)
    return n


def update_timeline(path: str, record: dict) -> dict:
    """Append the record's reading to the source's timeline (once per ref) and return the doc."""
    p = record["payload"]
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        doc = {"v": 1, "source_id": p["source_id"], "cited_by": p["cited_by"], "url": p["url"], "entries": []}
    doc["url"] = p["url"]
    if p.get("url_ar"):
        doc["url_ar"] = p["url_ar"]
    entries = doc["entries"]
    if any(e.get("ref") == record["ref"] for e in entries):
        return doc
    prev = next((e["sha256"] for e in reversed(entries) if e.get("sha256")), "")
    entry = {k: p[k] for k in ("fetched_at", "status", "http_status", "sha256", "quotes", "archive_url", "archive_status", "run_id")}
    for k in ("sha256_ar", "quotes_ar"):
        if k in p:
            entry[k] = p[k]
    entry.update(ref=record["ref"], payload_sha256=record["payload_sha256"],
                 changed_since_previous=(p["sha256"] != prev) if (p["sha256"] and prev) else None)
    entries.append(entry)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return doc


def validate_timeline(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["timeline: not an object"]
    out = _check_scalars("timeline", {k: v for k, v in doc.items() if k != "entries"}, TIMELINE_KEYS)
    if not isinstance(doc.get("entries"), list):
        return out + ["timeline: entries must be a list"]
    for i, e in enumerate(doc["entries"]):
        out += _check_scalars(f"entry {i}", e, ENTRY_KEYS) if isinstance(e, dict) else [f"entry {i}: not an object"]
    return out
