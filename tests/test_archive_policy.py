"""Save Page Now only where it adds something (owner decision 2026-11-23).

Each run queues ~185 readings and the archive step completes 20-30, so the backlog only grew (run
45102813992: 4 archived, 344 deferred). A reading is archived only when its page text changed
since the last archived copy of that URL, or that copy is over 30 days old; otherwise it points to
the previous copy. The deferred backlog is worked down the same way."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_timeline as tl  # noqa: E402


def wb(stamp: str, url: str) -> str:
    return f"https://web.archive.org/web/{stamp}/{url}"


class Fake:
    def __init__(self, max_per_run=40):
        self.max_per_run, self.asked = max_per_run, []

    def archive(self, urls):
        self.asked += list(urls)
        return {u: {"archive_url": wb("20261123180000", u), "archive_status": "archived"} for u in urls}


class Policy(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.q = tl.ProofQueue(os.path.join(self.d, "proof-queue", "source_read.jsonl"))

    def add(self, n, url, sha, fetched_at, status=tl.PENDING, archive_url="", anchor=None, app="demo"):
        reading = {"source_id": f"s-{n:012d}", "url": url, "status": "verified", "fetched_at": fetched_at, "http_status": 200, "sha256": sha}
        rec = tl.source_read_record(app, reading, [{"text": "Fee AED 200."}], [], {"archive_url": archive_url, "archive_status": status},
                                    "r", fetched_at)
        rec["anchor"] = anchor
        self.q.enqueue(rec)
        tl.update_timeline(os.path.join(self.d, "timeline", app, f"s-{n:012d}.json"), rec)
        return rec

    def by_ref(self):
        return {r["ref"]: r["payload"] for r in self.q._all()}

    def test_unchanged_recent_copy_gets_a_pointer_not_a_capture(self):
        u = "https://a.gov.ae/p"
        self.add(1, u, "a" * 64, "2026-11-10T05:17:00Z", status="archived", archive_url=wb("20261110052000", u), anchor={"batch": 1})
        new = self.add(1, u, "a" * 64, "2026-11-23T05:17:00Z")
        f = Fake()
        n = tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked, [], "same text, copy 13 days old: no capture")
        p = self.by_ref()[new["ref"]]
        self.assertEqual(p["archive_url"], wb("20261110052000", u))
        self.assertEqual(p["archive_status"], tl.POINTER)
        self.assertEqual(n["pointed"], 1)
        self.assertEqual(tl.validate_record(self.q._all()[-1]), [])
        e = json.load(open(os.path.join(self.d, "timeline", "demo", "s-000000000001.json")))["entries"][-1]
        self.assertEqual((e["archive_status"], e["archive_url"]), (tl.POINTER, wb("20261110052000", u)))

    def test_changed_text_is_archived(self):
        u = "https://a.gov.ae/p"
        self.add(1, u, "a" * 64, "2026-11-10T05:17:00Z", status="archived", archive_url=wb("20261110052000", u))
        new = self.add(1, u, "b" * 64, "2026-11-23T05:17:00Z")
        f = Fake()
        tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked, [u])
        self.assertEqual(self.by_ref()[new["ref"]]["archive_status"], "archived")

    def test_copy_over_30_days_old_is_refreshed(self):
        u = "https://a.gov.ae/p"
        self.add(1, u, "a" * 64, "2026-10-20T05:17:00Z", status="archived", archive_url=wb("20261020052000", u))
        self.add(1, u, "a" * 64, "2026-11-23T05:17:00Z")
        f = Fake()
        tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked, [u], "34 days old: archive again")

    def test_never_archived_url_is_archived_once_for_all_its_readings(self):
        u = "https://a.gov.ae/p"
        r1 = self.add(1, u, "a" * 64, "2026-11-22T05:17:00Z", status=tl.DEFERRED)
        r2 = self.add(1, u, "a" * 64, "2026-11-23T05:17:00Z")
        r3 = self.add(2, u, "a" * 64, "2026-11-23T05:17:00Z", app="other")  # the same page cited by another app
        f = Fake()
        tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked, [u])
        got = self.by_ref()
        self.assertEqual(got[r2["ref"]]["archive_status"], "archived")
        self.assertEqual(got[r3["ref"]]["archive_status"], "archived")
        self.assertEqual(got[r1["ref"]]["archive_status"], tl.POINTER, "the deferred older reading of the same text points to it")
        self.assertEqual(got[r1["ref"]]["archive_url"], got[r2["ref"]]["archive_url"])

    def test_an_older_reading_of_other_text_can_no_longer_be_archived(self):
        u = "https://a.gov.ae/p"
        old = self.add(1, u, "a" * 64, "2026-11-21T05:17:00Z", status=tl.DEFERRED)
        self.add(1, u, "b" * 64, "2026-11-23T05:17:00Z")
        tl.resolve_pending(self.d, Fake())
        self.assertEqual(self.by_ref()[old["ref"]]["archive_status"], tl.SUPERSEDED,
                         "the page has changed since: the Archive can only copy what it says now")

    def test_backlog_beyond_the_cap_stays_deferred_changed_pages_first(self):
        for i in range(5):  # five never-archived pages
            self.add(i, f"https://a.gov.ae/p{i}", "c" * 64, "2026-11-23T05:17:00Z")
        u = "https://a.gov.ae/changed"
        self.add(9, u, "a" * 64, "2026-11-20T05:17:00Z", status="archived", archive_url=wb("20261120052000", u))
        self.add(9, u, "b" * 64, "2026-11-23T05:17:00Z")
        f = Fake(max_per_run=2)
        n = tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked[0], u, "a changed page goes before a never-archived one")
        self.assertEqual((n["archived"], n["deferred"]), (2, 4))

    def test_unread_page_with_a_recent_copy_points_to_it(self):
        u = "https://a.gov.ae/p"
        self.add(1, u, "a" * 64, "2026-11-20T05:17:00Z", status="archived", archive_url=wb("20261120052000", u))
        new = self.add(1, u, "", "2026-11-23T05:17:00Z")  # not read this run: no text to compare
        f = Fake()
        tl.resolve_pending(self.d, f)
        self.assertEqual(f.asked, [])
        self.assertEqual(self.by_ref()[new["ref"]]["archive_status"], tl.POINTER)

    def test_anchored_records_are_never_rewritten(self):
        u = "https://a.gov.ae/p"
        rec = self.add(1, u, "a" * 64, "2026-11-23T05:17:00Z", anchor={"batch": 7})
        tl.resolve_pending(self.d, Fake())
        self.assertEqual(self.by_ref()[rec["ref"]]["archive_status"], tl.PENDING)


if __name__ == "__main__":
    unittest.main()
