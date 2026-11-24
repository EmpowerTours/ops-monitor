"""Pinned copies: every page version an app's manifest records (its sha256) is kept in the private
store under pinned/<sha256>.txt, even after the daily run overwrites the latest copy at its path."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import source_pins as pins  # noqa: E402
import source_watch as sw  # noqa: E402

NOW = dt.datetime(2026, 11, 23, 6, 0, tzinfo=dt.timezone.utc)
V1 = "Golden visa\nThe fee is AED 2,800.\n" + "Line of text on the official page.\n" * 10
V2 = "Golden visa\nThe fee is AED 3,000.\n" + "Line of text on the official page.\n" * 10
AR = "التأشيرة الذهبية\nالرسوم 2800 درهم\n" + "نص الصفحة الرسمية\n" * 10
URL = "https://u.ae/en/golden-visa"
MOCCAE = "https://www.moccae.gov.ae/en/services/release"
RULES = [{"hosts": ["moccae.gov.ae"], "cut_from": "Related Services", "mask": []}]


def h(text: str) -> str:
    return sw.sha256_text(text)


def plain(url: str, text: str) -> str:
    return text


def src(sid: str = "s-000000000001", url: str = URL, sha: str = "", **kw) -> dict:
    return {"id": sid, "url": url, "fingerprint": "f" * 64, "sha256": sha, **kw}


def write_capture(root: str, app: str, sid: str, text: str, fetched_at: str = "2026-11-20T05:17:00Z", ar: str | None = None) -> None:
    d = os.path.join(root, app)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    meta = {"id": sid, "sha256": h(text), "fetched_at": fetched_at, "last_read_fetched_at": fetched_at}
    if ar is not None:
        with open(os.path.join(d, sid + ".ar.txt"), "w", encoding="utf-8") as fh:
            fh.write(ar)
        meta["sha256_ar"] = h(ar)
    with open(os.path.join(d, sid + ".json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)


def git(root: str, *a: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, text=True, env=env).stdout


def read_pin(root: str, sha: str) -> tuple[str, dict]:
    with open(os.path.join(root, "pinned", sha + ".txt"), encoding="utf-8") as fh:
        text = fh.read()
    with open(os.path.join(root, "pinned", sha + ".json"), encoding="utf-8") as fh:
        return text, json.load(fh)


class Ensure(unittest.TestCase):
    def test_pins_the_current_capture_the_manifest_records(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V1)
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V1))]}, plain, NOW)
            text, meta = read_pin(d, h(V1))
            self.assertEqual(text, V1)
            self.assertEqual(meta["url"], URL)
            self.assertEqual(meta["fetched_at"], "2026-11-20T05:17:00Z")
            self.assertEqual(meta["refs"], [{"app": "asas", "id": "s-000000000001", "lang": "en"}])
            self.assertEqual((rep["referenced"], rep["pinned_now"], rep["missing"]), (1, 1, []))
            # A second run adds nothing and keeps the copy.
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V1))]}, plain, NOW)
            self.assertEqual((rep["referenced"], rep["pinned_now"], rep["already_pinned"]), (1, 0, 1))

    def test_empty_hash_is_not_a_reference(self):
        with tempfile.TemporaryDirectory() as d:
            rep = pins.ensure_pins(d, {"asas": [src(sha="")]}, plain, NOW)
            self.assertEqual((rep["referenced"], rep["missing"]), (0, []))

    def test_a_hash_with_no_copy_anywhere_is_reported_missing(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V2)  # the page moved on; the manifest still records V1
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V1))]}, plain, NOW)
            self.assertEqual(rep["missing"], [{"sha256": h(V1), "url": URL, "refs": [{"app": "asas", "id": "s-000000000001", "lang": "en"}]}])
            self.assertFalse(os.path.exists(os.path.join(d, "pinned", h(V1) + ".txt")))
            self.assertNotIn(V1[:20], json.dumps(rep), "the report never carries page text")

    def test_arabic_hash_is_pinned_too(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V1, ar=AR)
            pins.ensure_pins(d, {"asas": [src(sha=h(V1), url_ar="https://u.ae/ar/golden-visa", sha256_ar=h(AR))]}, plain, NOW)
            text, meta = read_pin(d, h(AR))
            self.assertEqual(text, AR)
            self.assertEqual(meta["url"], "https://u.ae/ar/golden-visa")
            self.assertEqual(meta["refs"], [{"app": "asas", "id": "s-000000000001", "lang": "ar"}])

    def test_a_page_cited_by_two_apps_is_one_copy_with_two_refs(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V1)
            pins.ensure_pins(d, {"asas": [src(sha=h(V1))], "relocatedubai": [src(sha=h(V1))]}, plain, NOW)
            _, meta = read_pin(d, h(V1))
            self.assertEqual([r["app"] for r in meta["refs"]], ["asas", "relocatedubai"])

    def test_volatile_host_hash_form(self):
        f = sw.Fetcher("t/1", 5, 0, volatile=RULES)
        page = "Release\nService Fees AED 200\n" + "Body line\n" * 10 + "Related Services\nSomething that rotates\n"
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "cosecha", "s-000000000001", page)
            sha = h(f.hash_form(MOCCAE, page))
            self.assertNotEqual(sha, h(page))
            rep = pins.ensure_pins(d, {"cosecha": [src(url=MOCCAE, sha=sha)]}, f.hash_form, NOW)
            self.assertEqual(rep["pinned_now"], 1)
            text, meta = read_pin(d, sha)
            self.assertEqual(text, page, "the whole capture is kept; the hash is over its stable part")
            self.assertEqual(meta["hash_of"], pins.HASH_OF_STABLE)
            self.assertEqual(pins.verify_pins(d, {"cosecha": [src(url=MOCCAE, sha=sha)]}, f.hash_form), [])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V1)
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V1))]}, plain, NOW, dry_run=True)
            self.assertEqual(rep["pinned_now"], 1)
            self.assertFalse(os.path.exists(os.path.join(d, "pinned")))


class History(unittest.TestCase):
    def repo(self, d: str) -> None:
        git(d, "init", "-q", "-b", "main")
        write_capture(d, "asas", "s-000000000001", V1, fetched_at="2026-11-19T05:17:00Z")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "captures 1")
        write_capture(d, "asas", "s-000000000001", V2, fetched_at="2026-11-21T05:17:00Z")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "captures 2")

    def test_recovers_an_overwritten_version_from_history(self):
        with tempfile.TemporaryDirectory() as d:
            self.repo(d)
            man = {"asas": [src(sha=h(V1))]}
            self.assertEqual(len(pins.ensure_pins(d, man, plain, NOW, dry_run=True)["missing"]), 1, "not in the current files")
            rep = pins.ensure_pins(d, man, plain, NOW, history=True)
            self.assertEqual((rep["pinned_now"], rep["recovered_from_history"], rep["missing"]), (1, 1, []))
            text, meta = read_pin(d, h(V1))
            self.assertEqual(text, V1)
            self.assertEqual(meta["fetched_at"], "2026-11-19T05:17:00Z", "the fetch time recorded with that version")
            self.assertTrue(meta["origin"].startswith("history:"))
            self.assertEqual(pins.verify_pins(d, man, plain), [])

    def test_history_finds_a_copy_saved_under_another_path(self):
        with tempfile.TemporaryDirectory() as d:
            self.repo(d)  # V1 lived at asas/s-000000000001.txt; relocatedubai cites the same page under another id
            rep = pins.ensure_pins(d, {"relocatedubai": [src(sid="s-000000000009", sha=h(V1))]}, plain, NOW, history=True)
            self.assertEqual(rep["recovered_from_history"], 1)

    def test_current_copy_preferred_over_history(self):
        with tempfile.TemporaryDirectory() as d:
            self.repo(d)
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V2))]}, plain, NOW, history=True)
            self.assertEqual((rep["pinned_now"], rep["recovered_from_history"]), (1, 0))


    def test_shallow_clone_history_is_used_only_when_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            src_repo, clone = os.path.join(d, "src"), os.path.join(d, "clone")
            os.makedirs(src_repo)
            self.repo(src_repo)
            subprocess.run(["git", "clone", "-q", "--depth", "2", "file://" + src_repo, clone], check=True, capture_output=True)
            man = {"asas": [src(sha=h(V1))]}
            with self.assertRaises(pins.HistoryError):
                pins.ensure_pins(clone, man, plain, NOW, history=True)
            rep = pins.ensure_pins(clone, man, plain, NOW, history=True, allow_shallow=True)
            self.assertEqual(rep["recovered_from_history"], 1)


class Prune(unittest.TestCase):
    def pin(self, d: str, text: str, last: str, refs: list[dict]) -> None:
        os.makedirs(os.path.join(d, "pinned"), exist_ok=True)
        with open(os.path.join(d, "pinned", h(text) + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(os.path.join(d, "pinned", h(text) + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"sha256": h(text), "url": URL, "fetched_at": "", "refs": refs, "pinned_at": last,
                       "unreferenced_since": None if refs else last}, fh)

    def test_never_deletes_a_referenced_copy_however_old(self):
        with tempfile.TemporaryDirectory() as d:
            self.pin(d, V1, "2020-01-01T00:00:00Z", [{"app": "asas", "id": "s-000000000001", "lang": "en"}])
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V1))]}, plain, NOW)
            self.assertEqual(rep["pruned"], [])
            self.assertTrue(os.path.exists(os.path.join(d, "pinned", h(V1) + ".txt")))
            self.assertIsNone(read_pin(d, h(V1))[1]["unreferenced_since"])

    def test_prunes_only_after_18_months_unreferenced(self):
        with tempfile.TemporaryDirectory() as d:
            self.pin(d, V1, "2025-04-01T00:00:00Z", [])  # unreferenced for ~20 months
            self.pin(d, V2, "2025-07-01T00:00:00Z", [])  # ~17 months
            rep = pins.ensure_pins(d, {"asas": []}, plain, NOW)
            self.assertEqual(rep["pruned"], [h(V1)])
            self.assertFalse(os.path.exists(os.path.join(d, "pinned", h(V1) + ".txt")))
            self.assertTrue(os.path.exists(os.path.join(d, "pinned", h(V2) + ".txt")))

    def test_dropping_a_reference_starts_the_clock_and_keeps_the_copy(self):
        with tempfile.TemporaryDirectory() as d:
            self.pin(d, V1, "2020-01-01T00:00:00Z", [{"app": "asas", "id": "s-000000000001", "lang": "en"}])
            rep = pins.ensure_pins(d, {"asas": [src(sha=h(V2))]}, plain, NOW)  # asas moved on to V2
            self.assertEqual(rep["pruned"], [], "the reference was there until this run")
            _, meta = read_pin(d, h(V1))
            self.assertEqual((meta["refs"], meta["unreferenced_since"]), ([], "2026-11-23T06:00:00Z"))

    def test_references_from_apps_not_read_this_run_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            self.pin(d, V1, "2020-01-01T00:00:00Z", [{"app": "faez", "id": "s-000000000001", "lang": "en"}])
            rep = pins.ensure_pins(d, {"asas": []}, plain, NOW)  # faez's manifest was not read this run
            self.assertEqual(rep["pruned"], [])
            self.assertEqual(read_pin(d, h(V1))[1]["refs"][0]["app"], "faez")


class Verify(unittest.TestCase):
    def test_fails_on_a_missing_or_altered_copy(self):
        with tempfile.TemporaryDirectory() as d:
            write_capture(d, "asas", "s-000000000001", V1)
            man = {"asas": [src(sha=h(V1)), src(sid="s-000000000002", url="https://u.ae/x", sha=h(V2))]}
            pins.ensure_pins(d, man, plain, NOW)
            problems = pins.verify_pins(d, man, plain)
            self.assertEqual(len(problems), 1)
            self.assertIn("asas/s-000000000002", problems[0])
            self.assertIn(h(V2), problems[0])
            with open(os.path.join(d, "pinned", h(V1) + ".txt"), "a", encoding="utf-8") as fh:
                fh.write("tampered\n")
            problems = pins.verify_pins(d, man, plain)
            self.assertEqual(len(problems), 2)
            self.assertTrue(any("does not hash" in p for p in problems))
            self.assertNotIn("tampered", "".join(problems))


class Wiring(unittest.TestCase):
    """The daily run pins into the private store, never the public state; --verify-pins is the gate."""

    PAGE = b"<html><body><p>" + b"Release a consignment of agricultural products. " * 8 + b"</p></body></html>"

    def setup_run(self, d: str, sha: str) -> tuple[str, str]:
        man = {"v": 1, "app": "demo", "sources": [{"id": "s-000000000001", "url": "https://a.gov.ae/release", "fingerprint": "f" * 64,
                                                   "facts": ["x"], "quotes": [{"text": "Release a consignment"}], "sha256": sha}]}
        mp, cp = os.path.join(d, "m.json"), os.path.join(d, "cfg.json")
        json.dump(man, open(mp, "w"))
        json.dump({"user_agent": "t/1", "per_host_delay_s": 0, "apps": [{"app": "demo", "manifest": mp, "token_env": "NONE"}]}, open(cp, "w"))
        return mp, cp

    def run_watch(self, d: str, cp: str) -> int:
        orig = sw.Fetcher.get

        def fake(self_, url, accept="", fresh=False, timeout=None):
            if url.startswith("http://127.0.0.1"):
                return orig(self_, url, accept, fresh)
            return sw.Fetched(url=url, status=200, content_type="text/html", body=self.PAGE, final_url=url)

        sw.Fetcher.get = fake
        try:
            args = argparse.Namespace(config=cp, captures=os.path.join(d, "cap"), state=os.path.join(d, "state"), only=None, manifest=None,
                                      verify_url=None, run_id="r1", dry_run=True, offline=False, report=os.path.join(d, "rep.json"))
            return sw.run(args, {}, lambda: NOW)
        finally:
            sw.Fetcher.get = orig

    def test_run_pins_and_verify_passes(self):
        text, *_ = sw.extract(self.PAGE, "text/html", "")
        with tempfile.TemporaryDirectory() as d:
            _, cp = self.setup_run(d, h(text))
            self.assertEqual(self.run_watch(d, cp), 0)
            self.assertTrue(os.path.exists(os.path.join(d, "cap", "pinned", h(text) + ".txt")))
            self.assertFalse(os.path.exists(os.path.join(d, "state", "pinned")), "page text never goes to the public state")
            self.assertEqual(sw.check_public_state(os.path.join(d, "state")), [])
            rep = json.load(open(os.path.join(d, "rep.json")))
            self.assertEqual((rep["pins"]["referenced"], rep["pins"]["missing"]), (1, []))
            self.assertTrue(os.path.exists(os.path.join(d, "cap", "runs", "r1-pins.json")))
            self.assertEqual(sw.main(["--config", cp, "--captures", os.path.join(d, "cap"), "--verify-pins"]), 0)

    def test_run_pins_the_saved_copy_before_overwriting_it(self):
        """The manifest records the version saved last run; today the page reads differently. The
        copy must be pinned before the run overwrites it at its path."""
        with tempfile.TemporaryDirectory() as d:
            _, cp = self.setup_run(d, h(V1))
            write_capture(os.path.join(d, "cap"), "demo", "s-000000000001", V1)
            self.assertEqual(self.run_watch(d, cp), 0)
            self.assertEqual(read_pin(os.path.join(d, "cap"), h(V1))[0], V1)
            self.assertNotEqual(open(os.path.join(d, "cap", "demo", "s-000000000001.txt"), encoding="utf-8").read(), V1, "overwritten")
            self.assertEqual(json.load(open(os.path.join(d, "rep.json")))["pins"]["missing"], [])

    def test_run_reports_a_missing_hash_and_verify_fails(self):
        with tempfile.TemporaryDirectory() as d:
            _, cp = self.setup_run(d, "a" * 64)
            self.assertEqual(self.run_watch(d, cp), 0, "a missing pin is reported, it does not fail the watch")
            rep = json.load(open(os.path.join(d, "rep.json")))
            self.assertEqual([m["sha256"] for m in rep["pins"]["missing"]], ["a" * 64])
            self.assertEqual(sw.main(["--config", cp, "--captures", os.path.join(d, "cap"), "--verify-pins"]), 1)

    def test_verify_pins_needs_the_private_store(self):
        with tempfile.TemporaryDirectory() as d:
            _, cp = self.setup_run(d, "a" * 64)
            self.assertEqual(sw.main(["--config", cp, "--verify-pins"]), 2)

    def test_migration_cli_dry_run_then_real(self):
        text, *_ = sw.extract(self.PAGE, "text/html", "")
        with tempfile.TemporaryDirectory() as d:
            _, cp = self.setup_run(d, h(text))
            cap = os.path.join(d, "cap")
            os.makedirs(cap)
            git(cap, "init", "-q", "-b", "main")
            write_capture(cap, "demo", "s-000000000001", text)
            git(cap, "add", "-A")
            git(cap, "commit", "-q", "-m", "c1")
            write_capture(cap, "demo", "s-000000000001", V2)
            git(cap, "add", "-A")
            git(cap, "commit", "-q", "-m", "c2")
            rp = os.path.join(d, "mig.json")
            self.assertEqual(sw.main(["--config", cp, "--captures", cap, "--pin-history", "--dry-run", "--run-id", "m1", "--report", rp]), 0)
            self.assertFalse(os.path.exists(os.path.join(cap, "pinned")))
            self.assertEqual(json.load(open(rp))["pins"]["recovered_from_history"], 1)
            self.assertEqual(sw.main(["--config", cp, "--captures", cap, "--pin-history", "--run-id", "m2", "--report", rp]), 0)
            self.assertEqual(read_pin(cap, h(text))[0], text)
            self.assertTrue(os.path.exists(os.path.join(cap, "runs", "m2-pins.json")))
            self.assertEqual(sw.main(["--config", cp, "--captures", cap, "--verify-pins"]), 0)


if __name__ == "__main__":
    unittest.main()
