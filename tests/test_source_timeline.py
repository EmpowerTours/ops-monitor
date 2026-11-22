"""F2: public timeline of official page readings (docs/SOURCE-WATCH.md, "Public timeline").

Save Page Now per reading, a public-mode source_read record queued for the proof batcher, and a
public JSON timeline per source. Hashes and short quotes only, never page text."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_timeline as tl  # noqa: E402

SAVE = "https://web.archive.org/save"
STATUS = "https://web.archive.org/save/status/"


class Resp:
    def __init__(self, status: int, body: dict | bytes):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body


class FakeArchive:
    """A stand-in for SPN2: POST /save -> job id, GET /save/status/<job> -> pending then result."""

    def __init__(self, results: dict[str, dict] | None = None, pending_rounds: int = 1, http_error: dict[str, int] | None = None):
        self.results = results or {}
        self.pending_rounds = pending_rounds
        self.http_error = http_error or {}
        self.requests: list[dict] = []
        self.polls: dict[str, int] = {}
        self.jobs: dict[str, str] = {}

    def __call__(self, req, timeout=0):
        headers = {k.lower(): v for k, v in req.header_items()}
        self.requests.append({"method": req.get_method(), "url": req.full_url, "headers": headers,
                              "data": urllib.parse.parse_qs((req.data or b"").decode())})
        if req.full_url == SAVE:
            url = urllib.parse.parse_qs(req.data.decode())["url"][0]
            if url in self.http_error:
                raise urllib.error.HTTPError(SAVE, self.http_error[url], "err", {}, io.BytesIO(b"{}"))
            r = self.results.get(url, {"status": "success", "timestamp": "20261122051800", "original_url": url})
            if r.get("status") == "refused":
                return Resp(200, {"status": "error", "status_ext": r["status_ext"], "message": "refused"})
            if url not in self.jobs:
                self.jobs[url], self.polls[url] = f"job-{len(self.jobs)}", 0
            return Resp(200, {"url": url, "job_id": self.jobs[url]})
        if req.full_url.startswith(STATUS):
            job = req.full_url[len(STATUS):]
            url = {j: u for u, j in self.jobs.items()}[job]
            self.polls[url] += 1
            if self.polls[url] <= self.pending_rounds:
                return Resp(200, {"status": "pending", "job_id": job})
            return Resp(200, {**self.results.get(url, {"status": "success", "timestamp": "20261122051800", "original_url": url}), "job_id": job})
        raise AssertionError(f"unexpected request {req.full_url}")


def archiver(fake: FakeArchive, sleeps: list | None = None, **kw) -> tl.Archiver:
    sl = sleeps if sleeps is not None else []
    return tl.Archiver(opener=fake, sleep=sl.append, user_agent="source-watch/1.0", **kw)


class SavePageNow(unittest.TestCase):
    def test_saves_each_url_once_and_returns_the_archive_url(self):
        fake = FakeArchive()
        got = archiver(fake, min_interval_s=21).archive(["https://a.gov.ae/x", "https://b.gov.ae/y", "https://a.gov.ae/x"])
        self.assertEqual(got["https://a.gov.ae/x"], {"archive_url": "https://web.archive.org/web/20261122051800/https://a.gov.ae/x",
                                                     "archive_status": "archived"})
        self.assertEqual(got["https://b.gov.ae/y"]["archive_status"], "archived")
        posts = [r for r in fake.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 2, "a page cited twice is saved once per run")

    def test_polite_generic_and_no_personal_info(self):
        fake, sleeps = FakeArchive(), []
        archiver(fake, sleeps, min_interval_s=21, not_within="20h").archive(["https://a.gov.ae/x", "https://b.gov.ae/y"])
        post = [r for r in fake.requests if r["method"] == "POST"][0]
        self.assertEqual(post["headers"]["user-agent"], "source-watch/1.0")
        self.assertNotIn("authorization", post["headers"], "anonymous unless a key is configured")
        self.assertNotIn("from", post["headers"])
        self.assertNotIn("cookie", post["headers"])
        self.assertEqual(post["data"]["if_not_archived_within"], ["20h"], "no second copy of a page the Archive saved today")
        self.assertEqual(set(post["data"]), {"url", "if_not_archived_within"}, "only the page URL and the window are sent")
        self.assertIn(21, sleeps, "at most one capture request per min_interval_s")

    def test_key_sent_only_when_configured(self):
        fake = FakeArchive()
        archiver(fake, auth="acc:sec").archive(["https://a.gov.ae/x"])
        self.assertEqual(fake.requests[0]["headers"]["authorization"], "LOW acc:sec")

    def test_refusals_and_errors_are_recorded_not_raised(self):
        fake = FakeArchive(results={"https://a.gov.ae/x": {"status": "refused", "status_ext": "error:blocked-url"},
                                    "https://b.gov.ae/y": {"status": "error", "status_ext": "error:not-found"}},
                           http_error={"https://c.gov.ae/z": 503})
        got = archiver(fake).archive(["https://a.gov.ae/x", "https://b.gov.ae/y", "https://c.gov.ae/z"])
        self.assertEqual(got["https://a.gov.ae/x"], {"archive_url": "", "archive_status": "not archived: error:blocked-url"})
        self.assertEqual(got["https://b.gov.ae/y"], {"archive_url": "", "archive_status": "not archived: error:not-found"})
        self.assertEqual(got["https://c.gov.ae/z"], {"archive_url": "", "archive_status": "not archived: HTTP 503"})

    def test_429_stops_the_run_politely(self):
        fake = FakeArchive(http_error={"https://a.gov.ae/x": 429})
        got = archiver(fake).archive(["https://a.gov.ae/x", "https://b.gov.ae/y"])
        self.assertEqual(got["https://a.gov.ae/x"]["archive_status"], "not archived: rate limited (HTTP 429); stopped for this run")
        self.assertEqual(got["https://b.gov.ae/y"]["archive_status"], "not archived: rate limited (HTTP 429); stopped for this run")
        self.assertEqual(len([r for r in fake.requests if r["method"] == "POST"]), 1, "no request after a 429")

    def test_per_run_cap_and_poll_limit(self):
        fake = FakeArchive(pending_rounds=99)
        got = archiver(fake, max_per_run=1, poll_rounds=3).archive(["https://a.gov.ae/x", "https://b.gov.ae/y"])
        self.assertEqual(got["https://a.gov.ae/x"]["archive_status"], "not archived: still pending after 3 status checks")
        self.assertEqual(got["https://b.gov.ae/y"]["archive_status"], "not archived: per-run cap of 1 reached")
        self.assertLessEqual(fake.polls["https://a.gov.ae/x"], 3)

    def test_only_http_urls(self):
        fake = FakeArchive()
        got = archiver(fake).archive(["file:///etc/passwd", "javascript:alert(1)"])
        self.assertTrue(all(v["archive_status"].startswith("not archived") for v in got.values()))
        self.assertEqual(fake.requests, [])


READING = {"source_id": "s-000000000001", "url": "https://a.gov.ae/release", "url_ar": "https://a.gov.ae/ar/release",
           "status": "verified", "fetched_at": "2026-11-22T05:17:09Z", "http_status": 200,
           "sha256": "a" * 64, "sha256_ar": "b" * 64}
QUOTES = [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]},
          {"text": "x " * 300}]  # an over-long quote is never published
QUOTES_AR = [{"text": "الرسوم 200 درهم."}]
ARCHIVE = {"archive_url": "https://web.archive.org/web/20261122051800/https://a.gov.ae/release", "archive_status": "archived"}


class Record(unittest.TestCase):
    def record(self, **kw):
        return tl.source_read_record("cosecha", dict(READING, **kw), QUOTES, QUOTES_AR, ARCHIVE, "r1", "2026-11-22T05:20:00Z")

    def test_public_source_read_record(self):
        r = self.record()
        self.assertEqual((r["v"], r["mode"], r["app"], r["kind"]), (1, "public", "ops", "source_read"))
        self.assertEqual(r["ref"], "cosecha/s-000000000001/2026-11-22T05:17:09Z")
        p = r["payload"]
        self.assertEqual(p["url"], READING["url"])
        self.assertEqual(p["sha256"], "a" * 64)
        self.assertEqual(p["hash_of"], "normalised-text-v1")
        self.assertEqual(p["fetched_at"], READING["fetched_at"])
        self.assertEqual(p["status"], "verified")
        self.assertEqual(p["archive_url"], ARCHIVE["archive_url"])
        self.assertEqual(p["quotes"], ["Release a consignment of agricultural products"])
        self.assertEqual(p["quotes_ar"], ["الرسوم 200 درهم."])
        self.assertIsNone(r["anchor"], "anchoring is not wired yet")
        self.assertEqual(tl.validate_record(r), [])

    def test_payload_hash_is_canonical_and_recomputable(self):
        r = self.record()
        canon = json.dumps(r["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        import hashlib
        self.assertEqual(r["payload_sha256"], hashlib.sha256(canon).hexdigest())
        self.assertEqual(r["payload_sha256"], self.record()["payload_sha256"], "deterministic")
        self.assertNotEqual(r["payload_sha256"], self.record(sha256="c" * 64)["payload_sha256"])

    def test_validator_refuses_text_and_private_mode(self):
        r = self.record()
        self.assertTrue(tl.validate_record({**r, "mode": "private"}))
        self.assertTrue(tl.validate_record({**r, "payload": {**r["payload"], "text": "page text"}}))
        self.assertTrue(tl.validate_record({**r, "payload": {**r["payload"], "quotes": ["y " * 300]}}))
        self.assertTrue(tl.validate_record({**r, "payload": {**r["payload"], "status": "line\nbreak"}}))
        self.assertTrue(tl.validate_record({**r, "extra": 1}))


class Queue(unittest.TestCase):
    def test_enqueue_once_pending_and_stub_anchorer(self):
        with tempfile.TemporaryDirectory() as d:
            q = tl.ProofQueue(os.path.join(d, "proof-queue", "source_read.jsonl"))
            r = tl.source_read_record("cosecha", READING, QUOTES, QUOTES_AR, ARCHIVE, "r1", "2026-11-22T05:20:00Z")
            self.assertTrue(q.enqueue(r))
            self.assertFalse(q.enqueue(r), "same ref is queued once (a re-dispatched run)")
            self.assertEqual([x["ref"] for x in q.pending()], [r["ref"]])
            self.assertEqual(tl.validate_queue_file(q.path), [])
            self.assertEqual(tl.NotWiredAnchorer().anchor(q.pending()), [], "the stub anchors nothing")
            self.assertIsInstance(tl.NotWiredAnchorer(), tl.Anchorer)
            with open(q.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({**r, "ref": "x", "payload": {**r["payload"], "text": "leak"}}) + "\n")
            self.assertTrue(tl.validate_queue_file(q.path))


class Timeline(unittest.TestCase):
    def test_one_entry_per_reading_hashes_and_short_quotes_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "timeline", "cosecha", "s-000000000001.json")
            r1 = tl.source_read_record("cosecha", READING, QUOTES, QUOTES_AR, ARCHIVE, "r1", "2026-11-22T05:20:00Z")
            r2 = tl.source_read_record("cosecha", dict(READING, fetched_at="2026-11-23T05:17:00Z", sha256="c" * 64, status="quote_missing"),
                                       QUOTES, QUOTES_AR, ARCHIVE, "r2", "2026-11-23T05:20:00Z")
            tl.update_timeline(path, r1)
            tl.update_timeline(path, r1)  # re-dispatch: no duplicate
            tl.update_timeline(path, r2)
            doc = json.load(open(path, encoding="utf-8"))
            self.assertEqual((doc["source_id"], doc["url"], doc["cited_by"]), ("s-000000000001", READING["url"], "cosecha"))
            self.assertEqual([e["fetched_at"] for e in doc["entries"]], ["2026-11-22T05:17:09Z", "2026-11-23T05:17:00Z"])
            self.assertEqual([e["changed_since_previous"] for e in doc["entries"]], [None, True])
            self.assertEqual(doc["entries"][1]["archive_url"], ARCHIVE["archive_url"])
            self.assertEqual(doc["entries"][1]["payload_sha256"], r2["payload_sha256"])
            self.assertEqual(tl.validate_timeline(doc), [])
            bad = {**doc, "entries": doc["entries"] + [{**doc["entries"][0], "text": "the whole page"}]}
            self.assertTrue(tl.validate_timeline(bad))

    def test_unread_page_keeps_the_hash_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            tl.update_timeline(path, tl.source_read_record("cosecha", dict(READING, status="unreachable", sha256="", sha256_ar="", http_status=403),
                                                           QUOTES, [], {"archive_url": "", "archive_status": "not archived: HTTP 503"}, "r1", "t"))
            e = json.load(open(path))["entries"][0]
            self.assertEqual((e["sha256"], e["status"], e["http_status"], e["changed_since_previous"]), ("", "unreachable", 403, None))


class Switch(unittest.TestCase):
    def test_no_key_no_request(self):
        import source_watch as sw
        cfg = {"archive": {"enabled": True}}
        self.assertEqual(sw.archiver_from(cfg, {}), (None, "no Save Page Now key (SOURCE_WATCH_SPN_KEY)"))
        self.assertEqual(sw.archiver_from(cfg, {"SOURCE_WATCH_SPN_KEY": "a:b", "SOURCE_WATCH_SPN": "off"})[1], "Save Page Now is off")
        self.assertEqual(sw.archiver_from({}, {"SOURCE_WATCH_SPN_KEY": "a:b"})[1], "Save Page Now is off")
        a, why = sw.archiver_from(cfg, {"SOURCE_WATCH_SPN_KEY": "a:b"})
        self.assertEqual((why, a.auth, a.ua, a.min_interval), ("", "a:b", "source-watch/1.0", 21.0))


class EndToEnd(unittest.TestCase):
    """A real run: the public state gets a queued record and a timeline entry per reading, the
    checker passes, and no page text is anywhere in the public state."""

    PAGE = (b"<html><body><p>" + b"Through this service the importer applies to release a consignment. " * 6 +
            b"</p><table><tr><td>Release a consignment of agricultural products</td><td>AED 200</td></tr></table></body></html>")

    def run_once(self, d, dry_run=False, run_id="r1"):
        import argparse
        import datetime as dt
        import source_watch as sw
        text, *_ = sw.extract(self.PAGE, "text/html", "")
        man = {"v": 1, "app": "demo", "sources": [{"id": "s-000000000001", "url": "https://a.gov.ae/release", "fingerprint": "f" * 64,
                                                    "facts": ["verdict:x"], "sha256": sw.sha256_text(text),
                                                    "quotes": [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}]}]}
        mp, cp = os.path.join(d, "m.json"), os.path.join(d, "cfg.json")
        json.dump(man, open(mp, "w"))
        json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "apps": [{"app": "demo", "manifest": mp, "token_env": "NONE"}]}, open(cp, "w"))
        orig = sw.Fetcher.get

        def fake(self_, url, accept="", fresh=False):
            if url.startswith("http://127.0.0.1"):
                return orig(self_, url, accept, fresh)
            return sw.Fetched(url=url, status=200, content_type="text/html", body=self.PAGE, final_url=url)

        sw.Fetcher.get = fake
        fa = FakeArchive()
        out, old_out, old_err = io.StringIO(), sys.stdout, sys.stderr
        sys.stdout = sys.stderr = out
        try:
            args = argparse.Namespace(config=cp, captures="", state=os.path.join(d, "state"), only=None, manifest=None, verify_url=None,
                                      run_id=run_id, dry_run=dry_run, offline=False, report=None)
            code = sw.run(args, {}, lambda: dt.datetime(2026, 11, 22, 5, 17, tzinfo=dt.timezone.utc), archiver=archiver(fa))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            sw.Fetcher.get = orig
        return code, fa, sw, text

    def test_run_queues_records_and_writes_the_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            code, fa, sw, text = self.run_once(d)
            self.assertEqual(code, 0)
            st = os.path.join(d, "state")
            q = tl.ProofQueue(os.path.join(st, "proof-queue", "source_read.jsonl")).pending()
            self.assertEqual(len(q), 1)
            self.assertEqual(q[0]["payload"]["sha256"], sw.sha256_text(text))
            self.assertEqual(q[0]["payload"]["archive_url"], "https://web.archive.org/web/20261122051800/https://a.gov.ae/release")
            doc = json.load(open(os.path.join(st, "timeline", "demo", "s-000000000001.json")))
            self.assertEqual(len(doc["entries"]), 1)
            self.assertEqual(sw.check_public_state(st), [])
            for dirpath, _, files in os.walk(st):
                for fn in files:
                    self.assertNotIn("Through this service the importer applies", open(os.path.join(dirpath, fn), encoding="utf-8").read())
            # The same run re-dispatched adds nothing.
            self.run_once(d, run_id="r1b")
            self.assertEqual(len(tl.ProofQueue(os.path.join(st, "proof-queue", "source_read.jsonl")).pending()), 1)

    def test_dry_run_saves_nothing_to_the_archive_or_timeline(self):
        with tempfile.TemporaryDirectory() as d:
            code, fa, sw, _ = self.run_once(d, dry_run=True)
            self.assertEqual(code, 0)
            self.assertEqual(fa.requests, [])
            self.assertFalse(os.path.exists(os.path.join(d, "state", "proof-queue")))
            self.assertFalse(os.path.exists(os.path.join(d, "state", "timeline")))


if __name__ == "__main__":
    unittest.main()
