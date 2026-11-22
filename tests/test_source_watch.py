"""Unit tests for source_watch.py. Standard library only: python3 -m unittest discover -s tests -v

No network: every fetch goes through a fake opener. The checks under test are deterministic code."""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import source_watch as sw  # noqa: E402

CLOCK = lambda: dt.datetime(2026, 11, 22, 6, 0, tzinfo=dt.timezone.utc)  # noqa: E731

PAGE = b"""<html><head><title>Release of consignment</title><script>var x = "Fee 999";</script></head><body>
<nav>Home</nav>
<p>Through this service the importer applies to release a consignment of agricultural products.</p>
<table><tr><td>Release a consignment of agricultural products</td><td>AED&nbsp;200</td></tr>
<tr><td>Release of cut flowers</td><td>AED 150</td></tr></table>
<p>The time to obtain the service: 1 working day. Payment by card \xe2\x80\x93 Visa or Mastercard.</p>
<a href="/docs/handbook-rev19.pdf">Handbook Rev 19</a> <a href="/docs/handbook-rev21.pdf">Handbook Rev 21</a>
</body></html>"""


class FakeResp:
    def __init__(self, status: int, body: bytes, ctype: str, url: str):
        self.status, self._body, self.headers, self._url = status, body, {"Content-Type": ctype}, url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url


def opener(routes: dict[str, tuple[int, bytes, str]], calls: list | None = None):
    def _open(req, timeout=0):
        if calls is not None:
            calls.append(req.full_url)
        status, body, ctype = routes.get(req.full_url, (404, b"", "text/html"))
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
        return FakeResp(status, body, ctype, req.full_url)

    return _open


def fetcher(routes, calls=None) -> sw.Fetcher:
    return sw.Fetcher("test-agent/1.0", 5, 0, sleep=lambda s: None, opener=opener(routes, calls))


def src(url="https://a.gov.ae/release", quotes=None, **kw) -> dict:
    return {"id": "s-000000000001", "url": url, "fingerprint": "f" * 64, "facts": ["verdict:x"], "quotes": quotes or [], **kw}


class Text(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(sw.norm_line("  AED 200 – “fee”­  "), 'AED 200 - "fee"')
        text, links, title, kind = sw.extract(PAGE, "text/html; charset=utf-8", "https://a.gov.ae/release")
        self.assertEqual(kind, "html")
        self.assertEqual(title, "Release of consignment")
        self.assertNotIn("Fee 999", text, "script text must not count as page text")
        self.assertIn("Release a consignment of agricultural products AED 200", text)
        self.assertIn("/docs/handbook-rev21.pdf", links)

    def test_quote_found_across_line_breaks_and_case_kept(self):
        t = "the importer applies\nto release a consignment"
        self.assertIsNotNone(sw.find_quote(t, "applies to release"))
        self.assertIsNone(sw.find_quote(t, "Applies to release"))


class Checks(unittest.TestCase):
    def setUp(self):
        self.text, *_ = sw.extract(PAGE, "text/html", "https://a.gov.ae/release")

    def test_value_must_sit_in_the_quotes_row(self):
        q = [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}]
        self.assertEqual(sw.check_quotes(self.text, q, pdf=False), [])
        # AED 150 is on the page, but in another table row: it does not back this quote.
        q = [{"text": "Release a consignment of agricultural products", "values": ["AED 150"]}]
        self.assertEqual(sw.check_quotes(self.text, q, pdf=False), [{"index": 0, "reason": "value", "value": "AED 150"}])

    def test_pdf_window(self):
        t = "Fee schedule. " + "x" * 400 + " Release fee AED 200"
        q = [{"text": "Fee schedule.", "values": ["AED 200"]}]
        self.assertEqual(len(sw.check_quotes(t, q, pdf=True)), 1, "a value 400 characters away is not near")
        q = [{"text": "Release fee", "values": ["AED 200"]}]
        self.assertEqual(sw.check_quotes(t, q, pdf=True), [])

    def test_api_fields(self):
        body = json.dumps({"data": [{"partnerCode": 484, "primaryValue": 220681.649}]})
        ok = [{"path": "data.0.partnerCode", "equals": 484}, {"path": "data.0.primaryValue", "equals": 220681.649}]
        self.assertEqual(sw.check_fields(body, ok), [])
        self.assertEqual(sw.check_fields(body, [{"path": "data.0.primaryValue", "equals": 1.0}])[0]["reason"], "value")
        self.assertEqual(sw.check_fields(body, [{"path": "data.3.x", "equals": 1}])[0]["reason"], "path")
        self.assertIsNone(sw.check_fields("<html>", ok))


class Classify(unittest.TestCase):
    def run_one(self, routes, s, manual=()):
        return sw.check_source(s, fetcher(routes), set(manual), CLOCK)

    def test_verified_changed_and_missing(self):
        text, *_ = sw.extract(PAGE, "text/html", "")
        q = [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}]
        routes = {"https://a.gov.ae/release": (200, PAGE, "text/html")}
        o = self.run_one(routes, src(quotes=q, sha256=sw.sha256_text(text)))
        self.assertEqual(o.status, "verified")
        o = self.run_one(routes, src(quotes=q, sha256="0" * 64))
        self.assertEqual(o.status, "changed_quote_ok")
        o = self.run_one(routes, src(quotes=q + [{"text": "Fees are waived"}], sha256="0" * 64))
        self.assertEqual((o.status, [m["index"] for m in o.missing]), ("quote_missing", [1]))
        # Same text as the app's saved copy but the quote is not on it: the manifest is wrong.
        o = self.run_one(routes, src(quotes=[{"text": "Fees are waived"}], sha256=sw.sha256_text(text)))
        self.assertEqual(o.status, "manifest_error")
        o = self.run_one(routes, src(quotes=[]))
        self.assertEqual(o.status, "no_quotes")

    def test_never_verified_without_reading(self):
        q = [{"text": "Release"}]
        challenge = b"<html><title>Just a moment...</title><body>" + b"Checking your browser. " * 20 + b"</body></html>"
        cases = {
            "403": {"https://a.gov.ae/release": (403, b"", "text/html")},
            "challenge": {"https://a.gov.ae/release": (200, challenge, "text/html")},
            "shell": {"https://a.gov.ae/release": (200, b"<html><body><div id=app></div>Loading</body></html>", "text/html")},
        }
        for name, routes in cases.items():
            o = self.run_one(routes, src(quotes=q))
            self.assertEqual(o.status, "unreachable", name)
        o = self.run_one({}, src(quotes=q), manual=["a.gov.ae"])
        self.assertEqual(o.status, "unreachable")
        self.assertTrue(o.detail.startswith("manual re-check due"))
        o = self.run_one({}, src(quotes=q, check="manual"))
        self.assertTrue(o.detail.startswith("manual re-check due"))

    def test_gone_after_retry(self):
        calls: list = []
        o = sw.check_source(src(quotes=[{"text": "x"}]), fetcher({}, calls), set(), CLOCK)
        self.assertEqual(o.status, "gone")
        self.assertEqual(len(calls), 2, "a 404 is retried once before it counts")

    def test_new_edition(self):
        routes = {"https://a.gov.ae/docs/handbook-rev19.pdf": (200, PAGE, "text/html"),
                  "https://a.gov.ae/index": (200, PAGE, "text/html")}
        s = src(url="https://a.gov.ae/docs/handbook-rev19.pdf", quotes=[{"text": "Release of cut flowers"}],
                index_url="https://a.gov.ae/index", link_pattern=r"handbook-rev\d+\.pdf$")
        o = self.run_one(routes, s)
        self.assertEqual((o.status, o.edition, o.new_url), ("verified", "new_edition", "https://a.gov.ae/docs/handbook-rev21.pdf"))
        p = sw.verify_payload("app", "r1", [o], [s])
        self.assertEqual([r["status"] for r in p["results"]], ["verified", "new_edition"])


class Payload(unittest.TestCase):
    def test_local_statuses_are_not_sent(self):
        s1, s2 = src(), dict(src(), id="s-000000000002")
        outs = [sw.Outcome(id=s1["id"], url=s1["url"], status="no_quotes"), sw.Outcome(id=s2["id"], url=s2["url"], status="manifest_error")]
        self.assertEqual(sw.verify_payload("app", "r1", outs, [s1, s2])["results"], [])

    def test_manifest_validation(self):
        good = {"v": 1, "app": "cosecha", "sources": [src(quotes=[{"text": "a"}])]}
        self.assertEqual(len(sw.validate_manifest(good, "cosecha")), 1)
        for bad in ({"v": 2, "app": "cosecha", "sources": []}, {"v": 1, "app": "asas", "sources": []},
                    {"v": 1, "app": "cosecha", "sources": [src(url="file:///etc/passwd")]},
                    {"v": 1, "app": "cosecha", "sources": [src(quotes=["bare string"])]},
                    {"v": 1, "app": "cosecha", "sources": [src(), src()]}):
            with self.assertRaises(sw.ManifestError):
                sw.validate_manifest(bad, "cosecha")


class Draft(unittest.TestCase):
    NEW = "The fee to release a consignment of agricultural products is AED 250 per consignment."

    def test_gate(self):
        ok = json.dumps({"proposed_fact": "Release costs AED 250.", "quote": "release a consignment of agricultural products is AED 250"})
        self.assertTrue(sw.verify_draft(ok, self.NEW)["ok"])
        not_verbatim = json.dumps({"proposed_fact": "Release costs AED 250.", "quote": "release costs AED 250 per consignment"})
        self.assertFalse(sw.verify_draft(not_verbatim, self.NEW)["ok"])
        new_number = json.dumps({"proposed_fact": "Release costs AED 300.", "quote": "release a consignment of agricultural products is AED 250"})
        self.assertIn("300", sw.verify_draft(new_number, self.NEW)["why"])
        self.assertFalse(sw.verify_draft("no json here", self.NEW)["ok"])

    def test_draft_quote_stays_short(self):
        # The draft quote is published in an issue: a passage of the page is refused even if verbatim.
        long_page = "Release fee. " + "The importer applies to release a consignment of agricultural products. " * 12
        long_quote = sw.norm_line(long_page)[: sw.QUOTE_MAX_CHARS + 40]
        r = sw.verify_draft(json.dumps({"proposed_fact": "Release fee.", "quote": long_quote}), long_page)
        self.assertFalse(r["ok"])
        self.assertIn("short quote", r["why"])

    def test_off_by_default_and_capped(self):
        self.assertFalse(sw.Drafter({}).on)
        self.assertFalse(sw.Drafter({"SOURCE_WATCH_LLM": "on"}).on, "no key: off")
        self.assertFalse(sw.Drafter({"SOURCE_WATCH_LLM": "on", "ANTHROPIC_API_KEY": "k", "SOURCE_WATCH_LLM_MODEL": "other"}).on)
        d = sw.Drafter({"SOURCE_WATCH_LLM": "on", "ANTHROPIC_API_KEY": "k", "SOURCE_WATCH_LLM_MAX_USD": "1000"})
        self.assertEqual(d.cap, sw.HARD_MAX_USD)

        calls = []

        class Usage:
            input_tokens, output_tokens = 1000, 200

        class Block:
            type = "text"
            text = json.dumps({"proposed_fact": "Release costs AED 250.", "quote": "release a consignment of agricultural products is AED 250"})

        class Resp:
            model, stop_reason, usage, content = "claude-opus-5-5", "end_turn", Usage(), [Block()]

        class Client:
            class beta:
                class messages:
                    @staticmethod
                    def create(**kw):
                        calls.append(kw)
                        return Resp()

        env = {"SOURCE_WATCH_LLM": "on", "ANTHROPIC_API_KEY": "k", "SOURCE_WATCH_LLM_MAX_USD": "0.085"}
        d = sw.Drafter(env, client_factory=Client)
        r = d.draft("old", ["verdict:x"], self.NEW, self.NEW)
        self.assertTrue(r["ok"], r)
        self.assertAlmostEqual(d.spent, (1000 * 4 + 200 * 20) / 1e6)
        # The cap holds a worst case per call: with USD 0.085, the next reservation (4000 output
        # tokens at USD 20/M = 0.08 plus input) does not fit.
        r = d.draft("old", ["verdict:x"], self.NEW, self.NEW)
        self.assertFalse(r["ok"])
        self.assertIn("cap", r["why"])
        self.assertEqual(len(calls), 1)

        class Broken:
            class beta:
                class messages:
                    @staticmethod
                    def create(**kw):
                        raise RuntimeError("network")

        r = sw.Drafter(dict(env, SOURCE_WATCH_LLM_MAX_USD="1"), client_factory=Broken).draft("o", [], self.NEW, self.NEW)
        self.assertFalse(r["ok"], "a failed call gives no draft")


def day(n: int):
    return lambda: dt.datetime(2026, 11, n, 6, 0, tzinfo=dt.timezone.utc)


class Canary(unittest.TestCase):
    """The control site exercises every check kind with the real fetch and extraction code."""

    def test_passes_on_both_schedule_days(self):
        for n in (22, 23):  # an even and an odd day of the year
            r = sw.run_canary(day(n), "t/1")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["cases"], 9)

    def test_catches_a_blind_checker(self):
        orig = sw.check_quotes
        sw.check_quotes = lambda text, quotes, pdf: []  # a checker that never sees a missing quote
        try:
            r = sw.run_canary(day(23), "t/1")
        finally:
            sw.check_quotes = orig
        self.assertFalse(r["ok"])
        self.assertTrue(any("canary-quote" in f for f in r["failures"]))

    def test_catches_a_checker_that_trusts_challenge_pages(self):
        orig = sw.looks_challenged
        sw.looks_challenged = lambda text, title: ""
        try:
            r = sw.run_canary(day(22), "t/1")
        finally:
            sw.looks_challenged = orig
        self.assertTrue(any("canary-challenge" in f for f in r["failures"]), r)

    def test_failed_canary_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            man = {"v": 1, "app": "demo", "sources": []}
            mp, cp = os.path.join(d, "m.json"), os.path.join(d, "cfg.json")
            json.dump(man, open(mp, "w"))
            json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "apps": [{"app": "demo", "manifest": mp, "token_env": "TOK", "verify": "https://x/"}]}, open(cp, "w"))
            orig, posted = sw.run_canary, []
            sw.run_canary = lambda clock, ua: {"ok": False, "day": "even", "cases": 9, "failures": ["forced"]}
            orig_post = sw.post_json
            sw.post_json = lambda *a, **k: posted.append(a) or (200, {})
            try:
                args = argparse.Namespace(config=cp, captures=os.path.join(d, "cap"), state=os.path.join(d, "state"), only=None, manifest=None, verify_url=None,
                                          run_id="r1", dry_run=False, offline=False, report=os.path.join(d, "rep.json"))
                self.assertEqual(sw.run(args, {"TOK": "x"}, CLOCK), 1)
            finally:
                sw.run_canary, sw.post_json = orig, orig_post
            self.assertEqual(posted, [])
            self.assertEqual(json.load(open(os.path.join(d, "rep.json")))["apps"][0]["sent"], "NOT SENT: canary failed")


class Registry(unittest.TestCase):
    def test_shared_page_fetched_once_and_conflicts_flagged(self):
        calls: list = []
        f = fetcher({"https://a.gov.ae/release": (200, PAGE, "text/html")}, calls)
        a = src(quotes=[{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}])
        b = dict(src(quotes=[{"text": "Release a consignment of agricultural products", "values": ["AED 250"]}]), id="s-000000000009")
        oa = sw.check_source(a, f, set(), CLOCK)
        ob = sw.check_source(b, f, set(), CLOCK)
        self.assertEqual(len(calls), 1, "one fetch for a page two apps cite")
        self.assertEqual((oa.status, ob.status), ("verified", "quote_missing"))
        shared = sw.shared_pages({"asas": ([a], [oa]), "relocatedubai": ([b], [ob])})
        self.assertEqual(shared[0]["apps"], ["asas", "relocatedubai"])
        self.assertEqual(len(shared[0]["conflicts"]), 2, shared)


class Arabic(unittest.TestCase):
    EN = b"<html><body><p>" + b"Filler text for a long enough page. " * 8 + b"</p><p>Fee AED 200.</p></body></html>"
    AR = ("<html><body><p>" + "نص طويل بما يكفي للصفحة. " * 10 + "</p><p>الرسوم 200 درهم.</p></body></html>").encode()

    def setUp(self):
        self.en_text = sw.extract(self.EN, "text/html", "")[0]
        self.ar_text = sw.extract(self.AR, "text/html; charset=utf-8", "")[0]

    def source(self, **kw):
        return src(url="https://a.gov.ae/en", url_ar="https://a.gov.ae/ar", sha256=sw.sha256_text(self.en_text),
                   sha256_ar=sw.sha256_text(self.ar_text), quotes=[{"text": "Fee AED 200."}],
                   quotes_ar=[{"text": "الرسوم 200 درهم."}], **kw)

    def test_both_checked(self):
        routes = {"https://a.gov.ae/en": (200, self.EN, "text/html"), "https://a.gov.ae/ar": (200, self.AR, "text/html; charset=utf-8")}
        o = sw.check_source(self.source(), fetcher(routes), set(), CLOCK)
        self.assertEqual((o.status, o.languages_disagree), ("verified", False))

    def test_arabic_changed_first(self):
        ar2 = self.AR.replace("200".encode(), "250".encode())
        routes = {"https://a.gov.ae/en": (200, self.EN, "text/html"), "https://a.gov.ae/ar": (200, ar2, "text/html; charset=utf-8")}
        o = sw.check_source(self.source(), fetcher(routes), set(), CLOCK)
        self.assertEqual(o.status, "quote_missing")
        self.assertTrue(o.languages_disagree)
        self.assertEqual([m["index"] for m in o.missing_ar], [0])
        r = sw.verify_payload("app", "r", [o], [self.source()])["results"][0]
        self.assertEqual((r["missing_ar"], r["languages_disagree"]), ([0], True))


class Radar(unittest.TestCase):
    LIST1 = b"""<html><body><a href="/en/news/1">MOIAT updates the technical regulation for honey products</a>
<a href="/en/about">About us</a><a href="/en/news/2">Honeywell opens a new factory in Abu Dhabi industrial city</a></body></html>"""
    LIST2 = LIST1.replace(b"</body>", "<a href='/en/news/3'>قرار جديد بشأن استيراد العسل من الخارج</a></body>".encode())
    RSS = b"""<?xml version="1.0"?><rss><channel><item><title>Green visa fee changes for investors</title><link>https://icp.gov.ae/n/9</link></item></channel></rss>"""

    def test_items_and_whole_word_matching(self):
        items = sw.feed_items("https://moiat.gov.ae/en/media-center/news", self.LIST1, "text/html")
        self.assertEqual([i["url"] for i in items], ["https://moiat.gov.ae/en/news/1", "https://moiat.gov.ae/en/news/2"])
        self.assertEqual(sw.keyword_hits(items[0]["title"], ["honey"]), ["honey"])
        self.assertEqual(sw.keyword_hits(items[1]["title"], ["honey"]), [], "Honeywell is not honey")
        self.assertEqual(sw.keyword_hits("استيراد عسل", ["عسل"]), ["عسل"])
        rss = sw.feed_items("https://icp.gov.ae/rss", self.RSS, "application/rss+xml")
        self.assertEqual(rss, [{"title": "Green visa fee changes for investors", "url": "https://icp.gov.ae/n/9"}])
        self.assertEqual(sw.keyword_hits(rss[0]["title"], ["green visa", "bid bond"]), ["green visa"])

    def test_first_read_seeds_then_new_items_match(self):
        feeds = [{"name": "MOIAT news", "url": "https://moiat.gov.ae/en/media-center/news"}]
        seen: dict = {}
        r = sw.run_radar(feeds, {"cosecha": ["honey", "العسل"]}, fetcher({feeds[0]["url"]: (200, self.LIST1, "text/html")}), seen, CLOCK)
        self.assertEqual(r["matches"]["cosecha"], [], "a first read only seeds")
        self.assertIn("seeded", r["feeds"][0]["status"])
        r = sw.run_radar(feeds, {"cosecha": ["honey", "العسل"]}, fetcher({feeds[0]["url"]: (200, self.LIST2, "text/html")}), seen, CLOCK)
        self.assertEqual([m["url"] for m in r["matches"]["cosecha"]], ["https://moiat.gov.ae/en/news/3"])
        empty = sw.run_radar([{"name": "x", "url": "https://x.gov.ae/"}], {"cosecha": ["honey"]},
                             fetcher({"https://x.gov.ae/": (200, b"<html><div id=app></div></html>", "text/html")}), {}, CLOCK)
        self.assertIn("no items read", empty["feeds"][0]["status"])

    def test_seen_list_is_public_safe(self):
        feeds = [{"name": "MOIAT news", "url": "https://moiat.gov.ae/en/media-center/news"}]
        seen: dict = {}
        sw.run_radar(feeds, {"cosecha": ["honey"]}, fetcher({feeds[0]["url"]: (200, self.LIST1, "text/html")}), seen, CLOCK)
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "radar"))
            json.dump(seen, open(os.path.join(d, "radar", "seen.json"), "w"))
            self.assertEqual(sw.check_public_state(d), [])
            json.dump({feeds[0]["url"]: ["MOIAT updates the technical regulation for honey products"]}, open(os.path.join(d, "radar", "seen.json"), "w"))
            self.assertEqual(len(sw.check_public_state(d)), 1, "a title is not a URL")


class PublicSide(unittest.TestCase):
    """ops-monitor is public and official pages may not be reproduced: page text never reaches the
    public state, an issue or the log; only the private capture store holds it."""

    PAGE_SENTENCES = ["Through this service the importer applies", "Release of cut flowers", "The time to obtain the service"]

    def outcome(self):
        q = [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}, {"text": "Fees are waived for farmers"}]
        s = src(quotes=q, sha256="0" * 64)
        o = sw.check_source(s, fetcher({"https://a.gov.ae/release": (200, PAGE, "text/html")}), set(), CLOCK)
        self.assertEqual(o.status, "quote_missing")
        return s, o

    def assertNoPageText(self, text):
        for sentence in self.PAGE_SENTENCES:
            self.assertNotIn(sentence, text)

    def test_issue_body_has_no_page_text(self):
        s, o = self.outcome()
        body = sw.issue_body("demo", s, o, "private capture store, `demo/x.review.md`", None, "")
        self.assertNoPageText(body)
        self.assertIn("Fees are waived for farmers", body, "the app's own quote (public in its manifest) is shown")
        self.assertIn("demo/x.review.md", body)
        # The excerpts and the diff exist, but only in the private review file.
        review = sw.review_md("demo", s, o, "an older version of the page")
        self.assertIn("Through this service the importer applies", review)
        self.assertIn("```diff", review)

    def test_state_is_public_safe_and_the_checker_bites(self):
        s, o = self.outcome()
        with tempfile.TemporaryDirectory() as d:
            sw.save_state(d, "demo", "r1", [o], [s])
            self.assertEqual(sw.check_public_state(d), [])
            meta = json.load(open(os.path.join(d, "demo", s["id"] + ".json")))
            self.assertEqual(meta["sha256"], o.capture_sha256)
            self.assertLessEqual(set(meta), sw.PUBLIC_STATE_KEYS)
            self.assertNoPageText(json.dumps(meta))
            # What a leak would look like: each is refused.
            open(os.path.join(d, "demo", s["id"] + ".txt"), "w").write(o.text)
            json.dump({**meta, "text": o.text}, open(os.path.join(d, "demo", "leak.json"), "w"))
            json.dump({**meta, "detail": "x" * 3000}, open(os.path.join(d, "demo", "long.json"), "w"))
            problems = sw.check_public_state(d)
            self.assertTrue(any(".txt" in x for x in problems), problems)
            self.assertTrue(any("'text'" in x for x in problems), problems)
            self.assertTrue(any("long.json" in x for x in problems), problems)
            err, old_err = io.StringIO(), sys.stderr
            sys.stderr = err
            try:
                self.assertEqual(sw.main(["--check-public-state", d]), 1)
            finally:
                sys.stderr = old_err

    def test_no_private_store_saves_no_text_warns_and_does_not_fail(self):
        text, *_ = sw.extract(PAGE, "text/html", "")
        with tempfile.TemporaryDirectory() as d:
            man = {"v": 1, "app": "demo", "sources": [src(quotes=[{"text": "Fees are waived for farmers"}], sha256="0" * 64)]}
            mp, cp = os.path.join(d, "m.json"), os.path.join(d, "cfg.json")
            json.dump(man, open(mp, "w"))
            json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "apps": [{"app": "demo", "manifest": mp, "token_env": "NONE"}]}, open(cp, "w"))
            orig = sw.Fetcher.get

            def fake(self_, url, accept="", fresh=False):
                if url.startswith("http://127.0.0.1"):
                    return orig(self_, url, accept, fresh)
                return sw.Fetched(url=url, status=200, content_type="text/html", body=PAGE, final_url=url)

            sw.Fetcher.get = fake
            err, out = io.StringIO(), io.StringIO()
            old_err, old_out = sys.stderr, sys.stdout
            sys.stderr, sys.stdout = err, out
            try:
                args = argparse.Namespace(config=cp, captures="", state=os.path.join(d, "state"), only=None, manifest=None, verify_url=None,
                                          run_id="r1", dry_run=True, offline=False, report=os.path.join(d, "rep.json"))
                code = sw.run(args, {}, CLOCK)
                args.offline = True
                code_offline = sw.run(args, {}, CLOCK)
            finally:
                sys.stderr, sys.stdout = old_err, old_out
                sw.Fetcher.get = orig
            self.assertEqual(code, 0)
            self.assertEqual(code_offline, 2, "a replay needs the private captures")
            self.assertIn("no private capture store configured", err.getvalue())
            self.assertIn("not saved (no private capture store configured)", out.getvalue())
            self.assertNoPageText(out.getvalue())  # the dry-run issue printed to the (public) log
            self.assertEqual(sw.check_public_state(os.path.join(d, "state")), [])
            for dirpath, _, files in os.walk(d):
                for fn in files:
                    if fn in ("m.json", "cfg.json"):
                        continue
                    self.assertNotIn(text[:60], open(os.path.join(dirpath, fn), encoding="utf-8").read(), fn)


class FixPR(unittest.TestCase):
    URL = "https://a.gov.ae/release"
    NEW = (b"<html><body><p>" + b"Filler text that makes the page long enough to count as read. " * 4 +
           b"</p><table><tr><td>Release a consignment of farm products</td><td>AED 200</td></tr></table></body></html>")

    def outcome(self, status, missing):
        text = sw.extract(self.NEW, "text/html", "")[0]
        return sw.Outcome(id="s-1", url=self.URL, status=status, text=text, kind="html", capture_sha256=sw.sha256_text(text), missing=missing)

    def store(self):
        return {self.URL: {"sha256": "0" * 64, "quotes": [{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}]}}

    def test_sha_refresh_and_quote_swap_keep_values(self):
        new, why = sw.propose_fix(self.store(), {}, self.outcome("changed_quote_ok", []), None)
        self.assertIn("sha256", why)
        self.assertEqual(new[self.URL]["quotes"], self.store()[self.URL]["quotes"])
        o = self.outcome("quote_missing", [{"index": 0, "reason": "quote"}])
        draft = {"ok": True, "quote": "Release a consignment of farm products", "proposed_fact": "x"}
        new, why = sw.propose_fix(self.store(), {}, o, draft)
        q = new[self.URL]["quotes"][0]
        self.assertEqual((q["text"], q["values"]), ("Release a consignment of farm products", ["AED 200"]))
        self.assertEqual(new[self.URL]["sha256"], o.capture_sha256)

    def test_no_fix_for_numbers_or_without_a_verified_draft(self):
        o = self.outcome("quote_missing", [{"index": 0, "reason": "value", "value": "AED 200"}])
        self.assertIsNone(sw.propose_fix(self.store(), {}, o, {"ok": True, "quote": "Release a consignment of farm products"}))
        o = self.outcome("quote_missing", [{"index": 0, "reason": "quote"}])
        self.assertIsNone(sw.propose_fix(self.store(), {}, o, None), "no model draft, no PR")
        self.assertIsNone(sw.propose_fix(self.store(), {}, o, {"ok": False}))
        st = self.store()
        st[self.URL]["quotes"][0]["values"] = ["AED 999"]  # the old value is not next to the new words
        self.assertIsNone(sw.propose_fix(st, {}, o, {"ok": True, "quote": "Release a consignment of farm products"}))

    def test_open_fix_pr_is_a_draft(self):
        calls = []

        class GH:
            def call(self, method, path, body=None):
                calls.append((method, path, body))
                if method == "GET" and "/pulls?" in path:
                    return []
                if "/git/ref/heads/" in path:
                    return {"object": {"sha": "abc"}}
                if method == "GET" and "/contents/" in path:
                    return {"sha": "filesha"}
                if method == "POST" and path.endswith("/pulls"):
                    return {"html_url": "https://github.com/EmpowerTours/cosecha/pull/99"}
                return {}

        url = sw.open_fix_pr(GH(), "EmpowerTours/cosecha", "main", "internal/sources/quotes.json", "source-watch/s-1", "{}\n", "t", "b")
        self.assertTrue(url.endswith("/pull/99"))
        pr = [c for c in calls if c[0] == "POST" and c[1].endswith("/pulls")][0][2]
        self.assertTrue(pr["draft"])
        self.assertEqual((pr["head"], pr["base"]), ("source-watch/s-1", "main"))
        self.assertFalse(any("merge" in c[1] for c in calls), "never merges")


class EndToEnd(unittest.TestCase):
    """A dry run over a manifest file, then an offline replay of the saved captures."""

    def test_dry_run_and_replay(self):
        text, *_ = sw.extract(PAGE, "text/html", "")
        with tempfile.TemporaryDirectory() as d:
            man = {"v": 1, "app": "demo", "sources": [src(quotes=[{"text": "Release a consignment of agricultural products", "values": ["AED 200"]}],
                                                          sha256=sw.sha256_text(text))]}
            mp, cp = os.path.join(d, "m.json"), os.path.join(d, "cfg.json")
            json.dump(man, open(mp, "w"))
            json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "apps": [{"app": "demo", "manifest": mp, "token_env": "NONE"}]}, open(cp, "w"))
            orig = sw.Fetcher.get

            def fake(self, url, accept="", fresh=False):
                if url.startswith("http://127.0.0.1"):  # the canary's own control site
                    return orig(self, url, accept, fresh)
                return sw.Fetched(url=url, status=200, content_type="text/html", body=PAGE, final_url=url)

            sw.Fetcher.get = fake
            try:
                args = argparse.Namespace(config=cp, captures=os.path.join(d, "cap"), state=os.path.join(d, "state"), only=None, manifest=None, verify_url=None,
                                          run_id="r1", dry_run=True, offline=False, report=os.path.join(d, "rep.json"))
                self.assertEqual(sw.run(args, {}, CLOCK), 0)
            finally:
                sw.Fetcher.get = orig
            rep = json.load(open(os.path.join(d, "rep.json")))
            self.assertTrue(rep["canary"]["ok"], rep["canary"])
            self.assertEqual(rep["apps"][0]["counts"], {"verified": 1})
            meta = json.load(open(os.path.join(d, "cap", "demo", "s-000000000001.json")))
            self.assertEqual(meta["sha256"], sw.sha256_text(text))
            self.assertTrue(os.path.exists(os.path.join(d, "cap", "runs", "r1-demo.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "cap", "demo", "s-000000000001.txt")), "full text: private store")
            self.assertEqual(sw.check_public_state(os.path.join(d, "state")), [])
            self.assertEqual(json.load(open(os.path.join(d, "state", "demo", "s-000000000001.json")))["sha256"], sw.sha256_text(text))
            # Replay: no network, same answer from the saved capture.
            args.offline, args.run_id = True, "r2"
            def no_fetch(self_, url, accept="", fresh=False):
                if url.startswith("http://127.0.0.1"):
                    return orig(self_, url, accept, fresh)
                self.fail("replay must not fetch")

            sw.Fetcher.get = no_fetch
            try:
                self.assertEqual(sw.run(args, {}, CLOCK), 0)
            finally:
                sw.Fetcher.get = orig
            self.assertEqual(json.load(open(os.path.join(d, "rep.json")))["apps"][0]["counts"], {"verified": 1})


if __name__ == "__main__":
    unittest.main()
