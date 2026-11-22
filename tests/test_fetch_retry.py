"""Retries for pages that did not load (run 44974248996: 2 timeouts, a PDF timeout, HTTP 202, HTTP 429,
and an encrypted PDF). Transient failures get up to two retries with backoff, timeouts grow, PDFs
start with a longer timeout, 429 honours Retry-After, and a run-wide budget keeps the job short.
A 403 (a block) is not retried."""

from __future__ import annotations

import io
import os
import socket
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import source_watch as sw  # noqa: E402

TEXT = b"<html><body><p>" + b"An official page with enough text to count as read. " * 10 + b"</p></body></html>"


class Resp:
    def __init__(self, status, body, ctype, url):
        self.status, self._body, self.headers, self._url = status, body, {"Content-Type": ctype}, url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body

    def geturl(self):
        return self._url


class Seq:
    """Answers one scripted step per request: an int status, ("429", retry_after), "timeout", or 200."""

    def __init__(self, steps, ctype="text/html", body=TEXT):
        self.steps, self.ctype, self.body = list(steps), ctype, body
        self.timeouts: list[float] = []

    def __call__(self, req, timeout=0):
        self.timeouts.append(timeout)
        step = self.steps.pop(0) if self.steps else 200
        if step == "timeout":
            raise socket.timeout("The read operation timed out")
        if isinstance(step, tuple):
            raise urllib.error.HTTPError(req.full_url, 429, "too many", {"Retry-After": step[1]}, io.BytesIO(b""))
        if step >= 400:
            raise urllib.error.HTTPError(req.full_url, step, "err", {}, io.BytesIO(b""))
        return Resp(step, self.body if step == 200 else b"", self.ctype, req.full_url)


def fetcher(seq, sleeps, **kw):
    return sw.Fetcher("t/1", 30, 0, sleep=sleeps.append, opener=seq, **kw)


class Retry(unittest.TestCase):
    URL = "https://a.gov.ae/page"

    def test_202_is_transient(self):
        seq, sleeps = Seq([202, 200]), []
        r = sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertTrue(r.ok)
        self.assertEqual(len(seq.timeouts), 2)

    def test_timeouts_back_off_and_grow(self):
        seq, sleeps = Seq(["timeout", "timeout", 200]), []
        r = sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertTrue(r.ok)
        self.assertEqual(seq.timeouts, [30, 60, 120])
        self.assertEqual(sleeps, [10, 40])

    def test_pdf_starts_with_a_longer_timeout(self):
        seq, sleeps = Seq(["timeout", 200]), []
        sw.read_page("https://www.adafsa.gov.ae/CMS/Guidelines/Code%20of%20Practice.pdf", fetcher(seq, sleeps))
        self.assertEqual(seq.timeouts, [90, 120])

    def test_429_honours_retry_after_within_a_cap(self):
        seq, sleeps = Seq([("429", "7"), 200]), []
        self.assertTrue(sw.read_page(self.URL, fetcher(seq, sleeps)).ok)
        self.assertEqual(sleeps, [7])
        seq, sleeps = Seq([("429", "3600"), 200]), []
        sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertEqual(sleeps, [120], "never wait longer than 120 s")
        seq, sleeps = Seq([("429", "soon"), 200]), []
        sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertEqual(sleeps, [30], "no usable Retry-After: 30 s")

    def test_gives_up_after_two_retries(self):
        seq, sleeps = Seq([503, 503, 503, 200]), []
        r = sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertFalse(r.ok)
        self.assertEqual((r.status, r.detail), ("unreachable", "HTTP 503 (after 2 retries)"))
        self.assertEqual(len(seq.timeouts), 3)

    def test_block_is_not_retried(self):
        seq, sleeps = Seq([403, 200]), []
        r = sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertEqual((r.ok, r.detail, len(seq.timeouts)), (False, "HTTP 403", 1))

    def test_gone_still_needs_a_second_404(self):
        seq, sleeps = Seq([404, 404]), []
        r = sw.read_page(self.URL, fetcher(seq, sleeps))
        self.assertEqual((r.status, len(seq.timeouts)), ("gone", 2))

    def test_run_budget_stops_retries(self):
        seq, sleeps = Seq([503, 200]), []
        f = fetcher(seq, sleeps, retry_budget_s=0)
        r = sw.read_page(self.URL, f)
        self.assertFalse(r.ok)
        self.assertEqual(len(seq.timeouts), 1)
        self.assertIn("retry budget", r.detail)


class EncryptedPdf(unittest.TestCase):
    def test_cryptography_is_pinned_for_pypdf(self):
        wf = open(os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "source-watch.yml"), encoding="utf-8").read()
        self.assertTrue("cryptography==50.0.2" in wf, "pypdf needs cryptography for AES-encrypted PDFs (INEGI)")


if __name__ == "__main__":
    unittest.main()
