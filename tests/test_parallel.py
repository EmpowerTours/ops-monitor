"""A daily run of every app finishes inside the job limit and never loses results (run 45088779261
was cancelled at the 40-minute limit with nothing reported).

- different hosts are read concurrently by a bounded pool; one host is never read twice at once
  and keeps its per-host delay;
- each app is reported as soon as its own pages are read;
- a hard run budget: when it is spent nothing more is fetched and the rest is "not read", never
  verified;
- a host can be capped per run; its pages rotate, least recently read first;
- Save Page Now runs after the reports, as its own step."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_timeline as tl  # noqa: E402
import source_watch as sw  # noqa: E402

PAGE = b"<html><body><p>" + b"An official page with enough words to count as read. " * 8 + b"Fee AED 200.</p></body></html>"


class Resp:
    def __init__(self, body, url):
        self.status, self._body, self.headers, self._url = 200, body, {"Content-Type": "text/html"}, url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body

    def geturl(self):
        return self._url


class SlowOpener:
    """Every request takes `cost` seconds of real time; records when each host was busy."""

    def __init__(self, cost: dict[str, float], fail: set[str] | None = None):
        self.cost, self.fail = cost, fail or set()
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.starts: dict[str, list[float]] = {}
        self.calls: list[str] = []

    def __call__(self, req, timeout=0):
        host = req.full_url.split("/")[2]
        with self.lock:
            self.active[host] = self.active.get(host, 0) + 1
            self.max_active[host] = max(self.max_active.get(host, 0), self.active[host])
            self.starts.setdefault(host, []).append(time.monotonic())
            self.calls.append(req.full_url)
        try:
            time.sleep(self.cost.get(host, 0.0))
            if req.full_url in self.fail:
                raise urllib.error.HTTPError(req.full_url, 404, "gone", {}, io.BytesIO(b""))
            return Resp(PAGE, req.full_url)
        finally:
            with self.lock:
                self.active[host] -= 1


def fetcher(opener, delay=0.0, host_delay=None) -> sw.Fetcher:
    return sw.Fetcher("t/1", 5, delay, opener=opener, host_delay=host_delay)


def src(i: int, host: str, **kw) -> dict:
    return {"id": f"s-{i:012d}", "url": f"https://{host}/p{i}", "fingerprint": "f" * 64, "facts": ["verdict:x"],
            "quotes": [{"text": "Fee AED 200."}], **kw}


class Pool(unittest.TestCase):
    def test_hosts_in_parallel_one_request_per_host_at_a_time(self):
        op = SlowOpener({"a.gov.ae": 0.2, "b.gov.ae": 0.2, "c.gov.ae": 0.2})
        plan = {"app": [(f"https://{h}/p{i}", False) for h in ("a.gov.ae", "b.gov.ae", "c.gov.ae") for i in range(3)]}
        t0 = time.monotonic()
        sw.prefetch(plan, fetcher(op), workers=4, on_app_done=lambda app: None)
        wall = time.monotonic() - t0
        self.assertLess(wall, 1.2, f"9 reads of 0.2 s on 3 hosts took {wall:.2f} s: not concurrent")
        self.assertEqual(set(op.max_active.values()), {1}, "a host is never read twice at once")

    def test_per_host_delay_still_holds(self):
        op = SlowOpener({})
        plan = {"app": [(f"https://a.gov.ae/p{i}", False) for i in range(4)] + [(f"https://b.gov.ae/p{i}", False) for i in range(4)]}
        sw.prefetch(plan, fetcher(op, delay=0.15), workers=4, on_app_done=lambda app: None)
        for host, starts in op.starts.items():
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            self.assertTrue(all(g >= 0.14 for g in gaps), f"{host}: gaps {gaps}")

    def test_pool_is_bounded(self):
        op = SlowOpener({f"h{i}.gov.ae": 0.15 for i in range(8)})
        plan = {"app": [(f"https://h{i}.gov.ae/p", False) for i in range(8)]}
        t0 = time.monotonic()
        sw.prefetch(plan, fetcher(op), workers=2, on_app_done=lambda app: None)
        self.assertGreaterEqual(time.monotonic() - t0, 0.55, "8 hosts on 2 workers take 4 rounds")

    def test_each_app_is_reported_when_its_pages_are_read(self):
        op = SlowOpener({"fast.gov.ae": 0.05, "slow.gov.ae": 0.6})
        plan = {"quick": [("https://fast.gov.ae/p1", False)], "late": [("https://slow.gov.ae/p1", False), ("https://fast.gov.ae/p2", False)]}
        done: list[tuple[str, float]] = []
        t0 = time.monotonic()
        sw.prefetch(plan, fetcher(op), workers=4, on_app_done=lambda app: done.append((app, time.monotonic() - t0)))
        self.assertEqual([a for a, _ in done], ["quick", "late"])
        self.assertLess(done[0][1], 0.4, "the quick app is reported before the slow host finishes")

    def test_a_page_cited_twice_is_read_once(self):
        op = SlowOpener({})
        plan = {"a": [("https://x.gov.ae/p", False)], "b": [("https://x.gov.ae/p", False)]}
        sw.prefetch(plan, fetcher(op), workers=2, on_app_done=lambda app: None)
        self.assertEqual(op.calls, ["https://x.gov.ae/p"])


class Groups(unittest.TestCase):
    """Run 45093562592: read at the same time, every MOCCAE (moccae.gov.ae and www.), MOIAT and
    u.ae page timed out, though read one by one at 18:24Z they all verified. www. is the same site,
    and a configured group of hosts is read by one worker with one delay clock."""

    def test_www_and_a_group_are_one_host(self):
        groups = {"uae-federal": ["moccae.gov.ae", "u.ae", "moiat.gov.ae"]}
        f = sw.Fetcher("t/1", 5, 0.12, opener=SlowOpener({"moccae.gov.ae": 0.05, "www.moccae.gov.ae": 0.05, "u.ae": 0.05, "x.gov.ae": 0.05}),
                       host_groups=groups)
        self.assertEqual(f.group("www.moccae.gov.ae"), f.group("moccae.gov.ae"))
        self.assertEqual(f.group("u.ae"), "uae-federal")
        self.assertEqual(f.group("www.x.gov.ae"), "x.gov.ae", "www. folds into the site")
        op = f.opener
        plan = {"app": [("https://moccae.gov.ae/a", False), ("https://www.moccae.gov.ae/b", False), ("https://u.ae/c", False),
                        ("https://x.gov.ae/d", False)]}
        t0 = time.monotonic()
        sw.prefetch(plan, f, workers=4, on_app_done=lambda app: None)
        group_starts = sorted(op.starts["moccae.gov.ae"] + op.starts["www.moccae.gov.ae"] + op.starts["u.ae"])
        gaps = [b - a for a, b in zip(group_starts, group_starts[1:])]
        self.assertTrue(all(g >= 0.11 for g in gaps), f"one group, one request at a time and its delay: {gaps}")
        self.assertLess(op.starts["x.gov.ae"][0] - t0, 0.1, "another host does not wait for the group")

    def test_retry_budget_is_per_group(self):
        f = sw.Fetcher("t/1", 5, 0, opener=SlowOpener({}), retry_budget_s=10)
        f.spend_retry("https://slow.gov.ae/a", 10)
        self.assertFalse(f.retry_allowed(5, "https://slow.gov.ae/b"))
        self.assertTrue(f.retry_allowed(5, "https://other.gov.ae/a"), "one slow host does not use up the others' retries")


class Budget(unittest.TestCase):
    def test_spent_budget_reads_nothing_more_and_never_verifies(self):
        op = SlowOpener({"a.gov.ae": 0.3})
        f = fetcher(op)
        f.deadline = time.monotonic() + 0.45  # time for two reads, not five
        plan = {"app": [(f"https://a.gov.ae/p{i}", False) for i in range(5)]}
        sw.prefetch(plan, f, workers=2, on_app_done=lambda app: None)
        self.assertLessEqual(len(op.calls), 2)
        for i in range(len(op.calls), 5):
            o = sw.check_source(src(i, "a.gov.ae", url=f"https://a.gov.ae/p{i}"), f, set(), lambda: __import__("datetime").datetime(2026, 11, 23))
            self.assertEqual(o.status, "unreachable")
            self.assertIn("time budget", o.detail)

    def test_retries_stop_at_the_deadline(self):
        f = fetcher(SlowOpener({}))
        f.deadline = time.monotonic() + 5
        self.assertFalse(f.retry_allowed(30, "https://a.gov.ae/p"), "a retry that would end after the deadline is not made")


class Rotation(unittest.TestCase):
    def test_capped_host_reads_the_least_recently_read_first(self):
        sources = [src(i, "comtradeapi.un.org") for i in range(5)] + [src(9, "a.gov.ae")]
        last = {"s-000000000000": "2026-11-22T05:00:00Z", "s-000000000001": "2026-11-20T05:00:00Z",
                "s-000000000002": "", "s-000000000003": "2026-11-21T05:00:00Z", "s-000000000004": "2026-11-22T06:00:00Z"}
        chosen, skipped = sw.rotate(sources, {"comtradeapi.un.org": 2}, last)
        self.assertEqual({s["id"] for s in chosen if "comtrade" in s["url"]}, {"s-000000000002", "s-000000000001"},
                         "never read first, then the oldest reading (kept in manifest order)")
        self.assertIn("s-000000000009", [s["id"] for s in chosen], "uncapped hosts are all read")
        self.assertEqual(len(skipped), 3)

    def test_rotation_is_shared_across_apps(self):
        a = [src(i, "comtradeapi.un.org") for i in range(3)]
        b = [dict(src(i + 10, "comtradeapi.un.org")) for i in range(3)]
        chosen, _ = sw.rotate_apps({"a": a, "b": b}, {"comtradeapi.un.org": 2}, {})
        self.assertEqual(sum(len(v) for v in chosen.values()), 2, "the cap is per run, not per app")


class ArchiveAfter(unittest.TestCase):
    READING = {"source_id": "s-000000000001", "url": "https://a.gov.ae/p", "status": "verified", "fetched_at": "2026-11-23T05:17:00Z",
               "http_status": 200, "sha256": "a" * 64}

    def queue_one(self, d, n=1):
        reading = dict(self.READING, source_id=f"s-{n:012d}", url=f"https://a.gov.ae/p{n}")
        rec = tl.source_read_record("demo", reading, [{"text": "Fee AED 200."}], [],
                                    {"archive_url": "", "archive_status": tl.PENDING}, "r1", "2026-11-23T05:20:00Z")
        tl.ProofQueue(os.path.join(d, "proof-queue", "source_read.jsonl")).enqueue(rec)
        tl.update_timeline(os.path.join(d, "timeline", "demo", f"s-{n:012d}.json"), rec)
        return rec

    def test_pending_readings_are_archived_later_and_stay_valid(self):
        with tempfile.TemporaryDirectory() as d:
            rec = self.queue_one(d)

            class FakeArchiver:
                max_per_run = 40

                def archive(self, urls):
                    return {u: {"archive_url": "https://web.archive.org/web/20261123052000/" + u, "archive_status": "archived"} for u in urls}

            n = tl.resolve_pending(d, FakeArchiver())
            self.assertEqual(n, {"archived": 1, "deferred": 0})
            q = tl.ProofQueue(os.path.join(d, "proof-queue", "source_read.jsonl")).pending()[0]
            self.assertEqual(q["payload"]["archive_status"], "archived")
            self.assertNotEqual(q["payload_sha256"], rec["payload_sha256"], "the payload hash follows the payload")
            self.assertEqual(tl.validate_record(q), [])
            e = json.load(open(os.path.join(d, "timeline", "demo", "s-000000000001.json")))["entries"][0]
            self.assertEqual((e["archive_status"], e["payload_sha256"]), ("archived", q["payload_sha256"]))

    def test_over_the_cap_is_deferred_not_lost(self):
        with tempfile.TemporaryDirectory() as d:
            self.queue_one(d, 1)
            self.queue_one(d, 2)

            class OneOnly:
                max_per_run = 1

                def archive(self, urls):
                    return {u: {"archive_url": "https://web.archive.org/web/20261123052000/" + u, "archive_status": "archived"} for u in urls}

            n = tl.resolve_pending(d, OneOnly())
            self.assertEqual(n, {"archived": 1, "deferred": 1})
            statuses = sorted(r["payload"]["archive_status"] for r in tl.ProofQueue(os.path.join(d, "proof-queue", "source_read.jsonl")).pending())
            self.assertEqual(statuses, ["archived", "not archived: deferred (per-run cap)"])

    def test_anchored_records_are_never_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            rec = self.queue_one(d)
            path = os.path.join(d, "proof-queue", "source_read.jsonl")
            anchored = dict(rec, anchor={"batch": 1})
            open(path, "w").write(json.dumps(anchored) + "\n")

            class Never:
                max_per_run = 40

                def archive(self, urls):
                    raise AssertionError("an anchored record is sealed")

            self.assertEqual(tl.resolve_pending(d, Never()), {"archived": 0, "deferred": 0})


class WholeRun(unittest.TestCase):
    """run(): the quick app is reported before the slow one is read; past the budget the slow app's
    unread pages go out as not read; a capped host rotates; readings wait for Save Page Now."""

    def test_report_early_budget_rotation_and_pending_archive(self):
        import argparse
        import datetime as dt
        with tempfile.TemporaryDirectory() as d:
            def manifest(app, sources):
                path = os.path.join(d, f"{app}.json")
                json.dump({"v": 1, "app": app, "sources": sources}, open(path, "w"))
                return path
            quick = manifest("quick", [src(1, "fast.gov.ae")])
            slow = manifest("slow", [src(i, "slow.gov.ae") for i in range(2, 6)] + [src(i, "comtradeapi.un.org") for i in range(6, 9)])
            cp = os.path.join(d, "cfg.json")
            json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "workers": 4, "run_budget_s": 1.0,
                       "host_max_per_run": {"comtradeapi.un.org": 2}, "archive": {"enabled": True},
                       "apps": [{"app": "quick", "manifest": quick, "token_env": "TQ", "verify": "https://quick.example/verify"},
                                {"app": "slow", "manifest": slow, "token_env": "TS", "verify": "https://slow.example/verify"}]}, open(cp, "w"))
            op = SlowOpener({"fast.gov.ae": 0.05, "slow.gov.ae": 0.4})
            orig_get, orig_post = sw.Fetcher._get, sw.post_json
            posted: list[tuple[str, float, dict]] = []
            t0 = time.monotonic()

            def fake_get(self_, url, accept, timeout=None):
                if url.startswith("http://127.0.0.1"):  # the canary's own control site
                    return orig_get(self_, url, accept, timeout)
                self_.opener = op
                return orig_get(self_, url, accept, timeout)

            def fake_post(url, token, payload, ua, timeout=30):
                posted.append((url, time.monotonic() - t0, payload))
                return 200, {"applied": len(payload["results"]), "stale": [], "unknown": []}

            sw.Fetcher._get, sw.post_json = fake_get, fake_post
            out = io.StringIO()
            old = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = out
            try:
                args = argparse.Namespace(config=cp, captures="", state=os.path.join(d, "state"), only=None, manifest=None, verify_url=None,
                                          run_id="r1", dry_run=False, offline=False, report=os.path.join(d, "rep.json"), budget_s=None)
                code = sw.run(args, {"TQ": "q" * 40, "TS": "s" * 40, "SOURCE_WATCH_SPN_KEY": "a:b"},
                              lambda: dt.datetime(2026, 11, 23, 5, 17, tzinfo=dt.timezone.utc))
            finally:
                sys.stdout, sys.stderr = old
                sw.Fetcher._get, sw.post_json = orig_get, orig_post
            self.assertEqual(code, 0, out.getvalue()[-2000:])
            self.assertEqual([u.split("/")[2] for u, _, _ in posted], ["quick.example", "slow.example"])
            self.assertLess(posted[0][1], posted[1][1])
            slow_res = {r["id"]: r for r in posted[1][2]["results"]}
            self.assertEqual(len([i for i in slow_res if int(i[2:]) >= 6]), 2, "the capped host sends only its rotation share")
            unread = [r for r in slow_res.values() if r["status"] == "unreachable" and "time budget" in r.get("detail", "")]
            self.assertTrue(unread, "pages past the budget are reported as not read")
            self.assertFalse(any(r["status"] == "verified" for r in unread))
            self.assertLessEqual(len([c for c in op.calls if "slow.gov.ae" in c]), 4)
            recs = tl.ProofQueue(os.path.join(d, "state", "proof-queue", "source_read.jsonl")).pending()
            self.assertTrue(recs)
            self.assertTrue(all(r["payload"]["archive_status"] == tl.PENDING for r in recs), "Save Page Now waits for its own step")


if __name__ == "__main__":
    unittest.main()
