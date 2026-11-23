"""Pinned copies: every page version an app relies on, kept by its hash (docs/SOURCE-WATCH.md,
"Pinned copies").

The daily run overwrites a page's capture at <app>/<id>.txt. An app's manifest records the sha256 of
the version its maintainers read; once the page's wording shifts, that version would survive only in
git history. So the PRIVATE capture store also keeps, per recorded hash:

  pinned/<sha256>.txt   the capture whose text hashes to that sha256 (the watcher's normal form, or
                        its stable part for a host listed in `volatile`), byte for byte as saved
  pinned/<sha256>.json  {sha256, url, fetched_at, hash_of, origin, refs[{app, id, lang}], pinned_at,
                        unreferenced_since}

Page text lives only in the private store (never call this with the public state directory). The
reports name apps, ids, URLs and hashes, which are already public in the manifests: never page text.

Rules (owner decision 2026-11-23): a copy any manifest still references is never deleted; one no
manifest references is pruned only 18 months after the last reference went away.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from typing import Any, Callable

PIN_DIR = "pinned"
PRUNE_AFTER_DAYS = 548  # 18 months
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# A capture in the store: <app>/<id>.txt or <app>/<id>.ar.txt (not pinned/, not runs/).
CAPTURE_RE = re.compile(r"^([a-z0-9][a-z0-9._-]{0,63})/([a-z0-9][a-z0-9._-]{0,63}?)(\.ar)?\.txt$")
NOT_APP_DIRS = {PIN_DIR, "runs"}
HASH_OF_WHOLE = "normalised-text-v1"
HASH_OF_STABLE = "normalised-text-v1, stable part (volatile host)"

HashForm = Callable[[str, str], str]  # (url, text) -> the text that is hashed


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def referenced(manifests: dict[str, list[dict]]) -> dict[str, dict]:
    """sha256 -> {"url", "refs": [{app, id, lang}]} for every hash a manifest records (empty skipped)."""
    out: dict[str, dict] = {}
    for app in sorted(manifests):
        for s in manifests[app]:
            for lang, ukey, hkey in (("en", "url", "sha256"), ("ar", "url_ar", "sha256_ar")):
                sha = str(s.get(hkey) or "").strip().lower()
                if not sha:
                    continue
                e = out.setdefault(sha, {"url": str(s.get(ukey) or s.get("url") or ""), "refs": []})
                e["refs"].append({"app": app, "id": str(s.get("id", "")), "lang": lang})
    return out


def matches(sha: str, text: str, url: str, hash_form: HashForm) -> str | None:
    """How `text` hashes to `sha`: over its stable part (volatile host), whole, or not at all."""
    stable = hash_form(url, text) if url else text
    if sha256_text(stable) == sha:
        return HASH_OF_STABLE if stable != text else HASH_OF_WHOLE
    if stable != text and sha256_text(text) == sha:
        return HASH_OF_WHOLE
    return None


def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _meta_fetched_at(meta: Any, lang: str, text_hashes: set[str]) -> str:
    """The fetch time a capture's metadata gives for this very text (else empty)."""
    if not isinstance(meta, dict):
        return ""
    if str(meta.get("sha256_ar" if lang == "ar" else "sha256") or "") not in text_hashes:
        return ""
    return str(meta.get("last_read_fetched_at") or meta.get("fetched_at") or "")


class Candidates:
    """Captures that may hold a referenced version, indexed by every hash they answer to."""

    def __init__(self, hash_form: HashForm, urls: dict[tuple[str, str, str], str]):
        self.hash_form, self.urls = hash_form, urls  # (app, id, lang) -> url from the manifests
        self.index: dict[str, dict] = {}

    def add(self, app: str, sid: str, lang: str, text: str, meta: Any, origin: str, fallback_time: str = "") -> None:
        url = self.urls.get((app, sid, lang)) or (str((meta or {}).get("url_ar" if lang == "ar" else "url") or "") if isinstance(meta, dict) else "")
        whole = sha256_text(text)
        stable = sha256_text(self.hash_form(url, text)) if url else whole
        hashes = {whole, stable}
        fetched = _meta_fetched_at(meta, lang, hashes) or fallback_time
        for sha in hashes:
            self.index.setdefault(sha, {"text": text, "url": url, "fetched_at": fetched, "origin": origin})


def _current(root: str, cands: Candidates) -> None:
    for app in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, app)
        if app in NOT_APP_DIRS or app.startswith(".") or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            m = CAPTURE_RE.match(f"{app}/{fn}")
            if not m:
                continue
            text = _read(os.path.join(d, fn))
            if text is None:
                continue
            sid, lang = m.group(2), "ar" if m.group(3) else "en"
            cands.add(app, sid, lang, text, _load_json(os.path.join(d, sid + ".json")), f"capture:{app}/{fn}")


def _git(root: str, *a: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", root, *a], input=data, check=True, capture_output=True).stdout


class HistoryError(Exception):
    pass


def _history(root: str, cands: Candidates) -> None:
    """Every capture version in the store's git history (newest commit first; a version already
    found keeps its first, i.e. newest, occurrence)."""
    try:
        if _git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
            raise HistoryError("the capture store is a shallow clone: its history is incomplete (clone it in full)")
        log = _git(root, "log", "--format=%H %cI", "HEAD").decode().split("\n")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise HistoryError(f"git history not readable ({type(e).__name__})") from e
    wanted: list[tuple[str, str, str, str, str, str | None]] = []  # blob, app, id, lang, commit:path, meta blob
    seen: set[str] = set()
    for line in filter(None, log):
        commit, when = line.split(" ", 1)
        tree: dict[str, str] = {}
        for ent in _git(root, "ls-tree", "-r", "-z", commit).split(b"\0"):
            if not ent:
                continue
            head, path = ent.decode("utf-8", "replace").split("\t", 1)
            _, typ, blob = head.split(" ")
            if typ == "blob":
                tree[path] = blob
        for path, blob in sorted(tree.items()):
            m = CAPTURE_RE.match(path)
            if not m or m.group(1) in NOT_APP_DIRS or blob in seen:
                continue
            seen.add(blob)
            app, sid, lang = m.group(1), m.group(2), "ar" if m.group(3) else "en"
            wanted.append((blob, app, sid, lang, f"history:{commit[:12]}:{path}|{when}", tree.get(f"{app}/{sid}.json")))
    if not wanted:
        return
    blobs = list(dict.fromkeys([w[0] for w in wanted] + [w[5] for w in wanted if w[5]]))
    out = _git(root, "cat-file", "--batch", data=("\n".join(blobs) + "\n").encode())
    content: dict[str, bytes] = {}
    i = 0
    for b in blobs:
        nl = out.index(b"\n", i)
        parts = out[i:nl].split(b" ")
        if len(parts) < 3:  # "<id> missing"
            i = nl + 1
            continue
        size = int(parts[2])
        content[b] = out[nl + 1: nl + 1 + size]
        i = nl + 1 + size + 1
    for blob, app, sid, lang, origin_when, meta_blob in wanted:
        if blob not in content:
            continue
        try:
            text = content[blob].decode("utf-8")
        except UnicodeDecodeError:
            continue
        meta = None
        if meta_blob and meta_blob in content:
            try:
                meta = json.loads(content[meta_blob].decode("utf-8"))
            except ValueError:
                meta = None
        origin, when = origin_when.rsplit("|", 1)
        commit_time = iso(parse_iso(when)) if parse_iso(when) else ""
        cands.add(app, sid, lang, text, meta, origin, fallback_time=commit_time)


def _pin_ok(root: str, sha: str, url: str, hash_form: HashForm) -> str | None:
    text = _read(os.path.join(root, PIN_DIR, sha + ".txt"))
    return matches(sha, text, url, hash_form) if text is not None else None


def _write_json(path: str, doc: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def ensure_pins(root: str, manifests: dict[str, list[dict]], hash_form: HashForm, now: dt.datetime,
                history: bool = False, dry_run: bool = False) -> dict:
    """Make sure every hash the given manifests record has its pinned copy; keep the references
    current; prune copies unreferenced for 18 months. `manifests` holds the apps read this run:
    references from other apps are kept as they are. `history`: also search the store's git history
    (the one-time migration; needs a full clone). `dry_run`: count, write nothing."""
    want = referenced(manifests)
    urls = {(r["app"], r["id"], r["lang"]): e["url"] for e in want.values() for r in e["refs"]}
    stamp = iso(now)
    pdir = os.path.join(root, PIN_DIR)
    rep: dict[str, Any] = {"referenced": len(want), "already_pinned": 0, "pinned_now": 0, "recovered_from_history": 0,
                           "missing": [], "pruned": [], "history_searched": False}
    need = [sha for sha in sorted(want) if not (SHA_RE.match(sha) and _pin_ok(root, sha, want[sha]["url"], hash_form))]
    rep["already_pinned"] = len(want) - len(need)
    found: dict[str, dict] = {}
    if need:
        cands = Candidates(hash_form, urls)
        _current(root, cands)
        found = {sha: cands.index[sha] for sha in need if sha in cands.index}
        if history and len(found) < len(need):
            hist = Candidates(hash_form, urls)
            _history(root, hist)
            rep["history_searched"] = True
            for sha in need:
                if sha not in found and sha in hist.index:
                    found[sha] = hist.index[sha]
    if not dry_run:
        os.makedirs(pdir, exist_ok=True)
    for sha in need:
        c = found.get(sha) if SHA_RE.match(sha) else None
        if c is None:
            rep["missing"].append({"sha256": sha, "url": want[sha]["url"], "refs": want[sha]["refs"]})
            continue
        rep["pinned_now"] += 1
        if c["origin"].startswith("history:"):
            rep["recovered_from_history"] += 1
        if dry_run:
            continue
        with open(os.path.join(pdir, sha + ".txt"), "w", encoding="utf-8") as fh:
            fh.write(c["text"])
        _write_json(os.path.join(pdir, sha + ".json"), {
            "sha256": sha, "url": want[sha]["url"] or c["url"], "fetched_at": c["fetched_at"],
            "hash_of": matches(sha, c["text"], want[sha]["url"] or c["url"], hash_form) or HASH_OF_WHOLE,
            "origin": c["origin"].split("|", 1)[0], "refs": [], "pinned_at": stamp, "unreferenced_since": None})
    # References and pruning, over every pinned copy.
    loaded = set(manifests)
    names = sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []
    for fn in names:
        if not fn.endswith(".json"):
            continue
        sha = fn[:-5]
        path = os.path.join(pdir, fn)
        meta = _load_json(path)
        if not isinstance(meta, dict):
            continue
        old_refs = [r for r in meta.get("refs") or [] if isinstance(r, dict)]
        refs = [r for r in old_refs if r.get("app") not in loaded] + (want[sha]["refs"] if sha in want else [])
        refs = sorted({json.dumps(r, sort_keys=True): r for r in refs}.values(), key=lambda r: (r["app"], r["id"], r["lang"]))
        new = dict(meta, refs=refs)
        if refs:
            new["unreferenced_since"] = None
        elif not meta.get("unreferenced_since") or old_refs:
            new["unreferenced_since"] = stamp  # the clock starts when the last reference goes
        since = parse_iso(str(new.get("unreferenced_since") or ""))
        if not refs and since and now - since > dt.timedelta(days=PRUNE_AFTER_DAYS):
            rep["pruned"].append(sha)
            if not dry_run:
                for ext in (".txt", ".json"):
                    try:
                        os.remove(os.path.join(pdir, sha + ext))
                    except FileNotFoundError:
                        pass
            continue
        if new != meta and not dry_run:
            _write_json(path, new)
    # A copy that exists but lost its metadata: rebuild it, so its references are recorded.
    for sha in sorted(want):
        if dry_run or sha in found or not SHA_RE.match(sha) or os.path.exists(os.path.join(pdir, sha + ".json")):
            continue
        hof = _pin_ok(root, sha, want[sha]["url"], hash_form)
        if hof:
            _write_json(os.path.join(pdir, sha + ".json"), {
                "sha256": sha, "url": want[sha]["url"], "fetched_at": "", "hash_of": hof, "origin": "unknown",
                "refs": want[sha]["refs"], "pinned_at": stamp, "unreferenced_since": None})
    return rep


def verify_pins(root: str, manifests: dict[str, list[dict]], hash_form: HashForm) -> list[str]:
    """Every hash the manifests record has a pinned copy that hashes to its name (empty: all good).
    Messages name refs, URLs and hashes only, never page text."""
    problems = []
    for sha, e in sorted(referenced(manifests).items()):
        who = ", ".join(f"{r['app']}/{r['id']} ({r['lang']})" for r in e["refs"])
        if not SHA_RE.match(sha):
            problems.append(f"{who}: recorded sha256 is not 64 hex digits")
            continue
        txt = os.path.join(root, PIN_DIR, sha + ".txt")
        if not os.path.exists(txt):
            problems.append(f"{who} {sha}: no pinned copy ({e['url']})")
        elif not _pin_ok(root, sha, e["url"], hash_form):
            problems.append(f"{who} {sha}: pinned/{sha}.txt does not hash to its name")
        elif not isinstance(_load_json(os.path.join(root, PIN_DIR, sha + ".json")), dict):
            problems.append(f"{who} {sha}: pinned/{sha}.json missing or unreadable")
    return problems
