"""Run 45233542202: MOCCAE service pages read as "changed" on every run because their "Related
Services" block comes out different on each load (and they carry usage counters). The hash is
taken over the page without those parts; quotes are still checked against the whole page."""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_watch as sw  # noqa: E402

RULES = [{"hosts": ["moccae.gov.ae"], "cut_from": "Related Services",
          "mask": [r"(Number of (?:users|Transactions) - (?:Companies|Individuals)\n(?:[^\n]*\n)?)[\d,]+"]}]


def page(related: str, users: str, fee: str = "AED 200") -> str:
    return ("Release of a consignment\nService Fees\n" + fee + "\nNumber of users - Companies\nعدد المستخدمين - الشركات\n"
            + users + "\nService Code\n122-003-026-000\nRelated Services\n" + related + "\nStart Service\n")


class Volatile(unittest.TestCase):
    def setUp(self):
        self.f = sw.Fetcher("t/1", 5, 0, volatile=RULES)

    def test_rotating_block_and_counters_do_not_change_the_hash(self):
        a = self.f.hash_form("https://www.moccae.gov.ae/en/services/x", page("Request agricultural extension services", "4647"))
        b = self.f.hash_form("https://moccae.gov.ae/en/services/x", page("Accreditation of a heat treatment facility", "4,712"))
        self.assertEqual(sw.sha256_text(a), sw.sha256_text(b))

    def test_a_real_change_still_changes_the_hash(self):
        a = self.f.hash_form("https://moccae.gov.ae/x", page("x", "1", fee="AED 200"))
        b = self.f.hash_form("https://moccae.gov.ae/x", page("x", "1", fee="AED 250"))
        self.assertNotEqual(sw.sha256_text(a), sw.sha256_text(b))

    def test_other_hosts_are_hashed_whole(self):
        t = page("x", "1")
        self.assertEqual(self.f.hash_form("https://u.ae/en/x", t), t)

    def test_check_source_hashes_the_stable_part_and_checks_the_whole_page(self):
        text = page("Request agricultural extension services", "4647")
        s = {"id": "s-000000000001", "url": "https://moccae.gov.ae/en/services/x", "fingerprint": "f" * 64, "facts": ["verdict:x"],
             "sha256": sw.sha256_text(self.f.hash_form("https://moccae.gov.ae/en/services/x", page("Other related service", "9"))),
             "quotes": [{"text": "Service Fees AED 200"}, {"text": "Request agricultural extension services"}]}
        self.f.reads[(s["url"], False)] = sw.Read(True, text=text, kind="html", http_status=200)
        o = sw.check_source(s, self.f, set(), lambda: dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc))
        self.assertEqual(o.status, "verified", (o.status, o.detail))
        self.assertEqual(o.text, text, "the capture keeps the whole page")


if __name__ == "__main__":
    unittest.main()
