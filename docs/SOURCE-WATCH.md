# Source watch: contract and operation

The Dubai apps cite official pages. When one of those pages changes, the apps should notice
without anyone editing code, and they should never claim a check that did not happen. The source
watch does this in two halves:

- **ops-monitor** (`source_watch.py`) runs daily. It reads each app's manifest, fetches every cited
  page, checks the exact words the app relies on with plain code, saves what it read, and reports
  the results to the app.
- **each app** publishes the manifest, accepts the results on an authenticated endpoint, stores
  them, and shows the affected facts as unconfirmed when their words have gone from the page.

No model makes or influences any check. The only model use is an optional draft of a fix text,
which is off by default (see [Fix proposals](#fix-proposals)).

Contents: [Manifest](#1-manifest-get-well-knownsourcesjson) ·
[Verify endpoint](#2-verify-endpoint-post-internalsourcesverify) ·
[What the app does with results](#3-what-the-app-does-with-results) ·
[Status and judge-facing Sources data](#4-status-get-well-knownsources-statusjson) ·
[The watcher](#5-the-watcher) · [Canary](#6-canary-the-verifier-checks-itself) ·
[Dead-man switch](#7-dead-man-switch) · [Shared pages](#8-shared-source-registry) ·
[New-rule radar](#9-new-rule-radar) · [Public timeline](#10-public-timeline-of-readings) · [Fix proposals](#fix-proposals) ·
[Owner setup](#owner-setup) · [Adopting it in the other apps](#adopting-it-in-the-other-apps) ·
[Prior art](#prior-art-why-our-own-fetcher)

---

## 1. Manifest: `GET /.well-known/sources.json`

Public and read-only. No secrets in it: it lists pages the app already shows to its users.

```json
{
  "v": 1,
  "app": "cosecha",
  "app_version": "<git sha or unknown>",
  "radar_keywords": ["honey", "عسل", "import permit"],
  "sources": [
    {
      "id": "s-13d73aaae1e2",
      "url": "https://moiat.gov.ae/en/services/issue-conformity-certificates-for-regulated-products",
      "kind": "page",
      "check": "auto",
      "read_at": "2026-11-20",
      "sha256": "<sha256 of the saved copy's normalised text, or empty>",
      "facts": ["step:honey:mx-importer-step-honey-is-a-moiat-regulated-product", "verdict:honey"],
      "quotes": [
        {"text": "Issue Conformity Certificates for Regulated Products (ECAS)"},
        {"text": "Validity of Test report should be less than 3 Year.", "values": ["3 Year"], "facts": ["step:honey:..."]}
      ],
      "fields": [],
      "index_url": "https://example.gov.ae/handbooks",
      "link_pattern": "handbook-rev\\d+\\.pdf$",
      "url_ar": "https://example.gov.ae/ar/page",
      "sha256_ar": "<sha256 of the Arabic saved copy>",
      "quotes_ar": [{"text": "..."}],
      "fingerprint": "<64 hex>"
    }
  ]
}
```

| field | rule |
|---|---|
| `id` | stable per page: `s-` + the first 12 hex digits of SHA-256(url). Lowercase `[a-z0-9._-]`, at most 64 characters. |
| `url` | the page as the app cites it (http or https). |
| `kind` | `page` (default) or `api`: an official API answer checked by `fields` instead of `quotes`. |
| `check` | `auto` (default) or `manual`: the host blocks robots or needs a browser. A manual page is never fetched and is always reported as **manual re-check due**, never as verified. |
| `read_at` | the day the app's maintainers read the page (`YYYY-MM-DD`). Never today's date. |
| `sha256` | SHA-256 of the saved copy's text in the watcher's normal form (below). Empty: the watcher cannot tell "changed" from "unchanged", so it says so. Copy it from the public state (`source-watch-state` branch, `<app>/<id>.json`, field `sha256`). |
| `facts` | ids of the app's facts that rest on this page, in the app's own naming. |
| `quotes[]` | exact words the app relies on. Optional `values[]`: exact strings (a fee, a date, a percentage) that must stand in the **same paragraph or table row** as the quote (for a PDF: within 300 characters). Optional `facts[]`: the subset of the page's facts this quote backs (default: all). |
| `fields[]` | for `kind: "api"`: `{"path": "data.0.primaryValue", "equals": 220681.649, "facts"?: [...]}`; the JSON value at the dotted path must equal `equals` exactly. |
| `index_url`, `link_pattern` | optional, together: the page that links to a versioned document, and a regular expression for its links. Another matching link (a new edition, e.g. Rev 19 to Rev 21) is reported as `new_edition`. |
| `url_ar`, `sha256_ar`, `quotes_ar` | optional, together: the page's Arabic version (often updated first). Both versions are checked; a missing Arabic quote counts like a missing English one; one version changing while the other did not is flagged as **language versions disagree**. |
| `fingerprint` | set by the app: SHA-256 over url, kind, sha256, quotes, fields and the Arabic fields. Results name the fingerprint they were checked against, so a deploy that changes a page's words makes older results stale (see 3). |
| `radar_keywords` | words for the [new-rule radar](#9-new-rule-radar). |

**Normal form** (both sides): Unicode NFKC; curly quotes to straight, every dash to `-`;
soft hyphens, zero-width and bidi marks removed; runs of whitespace to one space; empty lines
dropped. Case is kept. HTML: script, style, head, svg and similar are dropped; block elements and
table rows end a line (a line is a "paragraph or row"). PDF: text of each page in order (pypdf).
A quote matches across line breaks. The sha256 is over the normalised text, UTF-8.

## 2. Verify endpoint: `POST /internal/sources/verify`

- `Authorization: Bearer <per-app token>`, compared in constant time on digests. Token unset or
  shorter than 32 characters: **503, nothing is read** (fail closed). Wrong token: 401.
- Rate-limited per token (Cosecha: 6 per minute, 30 per day). `Content-Type: application/json`,
  body at most 512 KiB, unknown fields refused.

```json
{"v": 1, "app": "cosecha", "run_id": "20261122T051703Z", "results": [
  {"id": "s-...", "fingerprint": "<from the manifest>", "status": "verified",
   "fetched_at": "2026-11-22T05:17:09Z", "capture_sha256": "<hex>", "detail": "..."},
  {"id": "s-...", "fingerprint": "...", "status": "quote_missing", "fetched_at": "...",
   "capture_sha256": "<hex>", "missing": [0], "capture_sha256_ar": "<hex>", "missing_ar": [1],
   "languages_disagree": true},
  {"id": "s-...", "fingerprint": "...", "status": "new_edition", "fetched_at": "...", "new_url": "https://..."}
]}
```

| status | meaning | the app |
|---|---|---|
| `verified` | every quote (and value, field) found; page unchanged or no saved sha | records `verified_at` (= `fetched_at`, never moves backwards) |
| `changed_quote_ok` | page changed since the saved copy, every quote still there | records `verified_at`, flags the source for a look |
| `quote_missing` | at least one quote (or a value next to it, or a field) gone; `missing` / `missing_ar` are the quote indexes | first time: recorded and flagged, **cards unchanged**; see anti-flap |
| `gone` | HTTP 404/410 on two tries | as `quote_missing`, for every fact of the page |
| `unreachable` | not read (network, 403, challenge page, browser-only page, unreadable file, manual host) | recorded; **changes nothing** |
| `new_edition` | the index lists another matching document | flagged; sent besides the quote result |

Rules: at most one quote result and one `new_edition` result per id. `fetched_at` must lie within
the last 8 days (and not more than 5 minutes ahead). `detail` is clipped to 300 characters.

Answer (200):

```json
{"v": 1, "run_id": "...", "applied": 61, "recorded": 9, "stale": [], "unknown": [],
 "unconfirmed": ["s-..."], "replayed": false}
```

- **Idempotent**: the same `run_id` with the same body returns the stored answer with
  `"replayed": true` and applies nothing; the same `run_id` with another body is 409.
- `stale`: results whose fingerprint is not the running manifest's (checked against an older
  deploy): audited, not applied. `unknown`: ids the manifest does not hold.
- Every result is written to an **append-only audit log** (Cosecha: `source_watch_audit`, a
  trigger refuses UPDATE and DELETE). Times written come from the app's clock and the watcher's
  fetch times, never from the database's `now()`.

## 3. What the app does with results

- **Anti-flap.** A source flips to unconfirmed only after **two daily `quote_missing` (or
  `gone`) results on the same page text** (same capture sha256, English and Arabic), fetched at
  least 20 hours apart. A single miss, two misses on the same day, or misses on different texts
  change no card. A `verified` or `changed_quote_ok` result ends the miss.
- **Unconfirmed facts.** The facts behind each missing quote (a quote's own `facts`, else every
  fact of the page; a missing Arabic quote: every fact of the page) show as unconfirmed on every
  card and draft that uses them, until a deploy changes the page's manifest entry (its
  fingerprint), which clears the flag, or the page shows the words again.
- **Last checked.** The date a page shows is `max(read_at, verified_at)`; an answer's date is the
  latest of the pages it cites. It is never today's date unless a check happened today.
- **A follow-up contact** whose page changed is not offered (its contacts may be stale).

Cosecha: verdicts and steps become "to be confirmed" with the line "The official page behind this
changed and no longer says what we quoted", on the check page, the checklist and the Sawweeli
`export_check` card.

## 4. Status: `GET /.well-known/sources-status.json`

Public, read-only, cached 60 s. The data for a judge-facing **Sources** list (each app renders it
in its own pages during adoption) and for the dead-man alert.

```json
{"v": 1, "app": "cosecha", "verify_open": true, "last_report_at": "2026-11-22T05:18:02Z",
 "stale_after_hours": 48, "paused": false, "unconfirmed_facts": 0,
 "sources": [{"id": "s-...", "url": "...", "url_ar": "...", "check": "auto", "quotes": 2,
   "read_at": "2026-11-17", "last_checked": "2026-11-22", "status": "verified",
   "status_at": "...", "verified_at": "...", "unconfirmed": false, "needs_look": false,
   "languages_disagree": false, "unreachable_at": null, "new_edition_url": "",
   "history": [{"received_at": "...", "fetched_at": "...", "status": "verified", "outcome": "applied"}]}]}
```

`status` is one of `verified`, `changed_quote_ok`, `quote_missing`, `gone`, `not_checked_yet`,
`no_quotes` (the page carries no quote yet: nothing can be verified), `manual`. A page no check
reached is never `verified`.

## 5. The watcher

`source_watch.py`, Python standard library plus pinned `pypdf` (PDF text) and, only when drafts
are on, the pinned `anthropic` SDK.

- **Trigger:** the Cloudflare cron Worker `ops-monitor-dispatch` dispatches
  `.github/workflows/source-watch.yml` daily at 05:17 UTC (`17 5 * * *`; GitHub's own scheduler is
  unreliable for this repository). The workflow's GitHub cron (06:41 UTC) is a backup; a second
  run on the same day is harmless (anti-flap needs 20 h between misses).
- **Apps:** `source-watch.json` (`apps[]`: manifest and verify URLs, the name of the token
  secret, the app repo, optional `fix` and `radar_keywords`). A 404 manifest is reported as "not
  adopted yet".
- **Fetching:** plain GETs, user agent `EmpowerTours-source-watch/1.0 (+repo URL)` (no personal
  details), 30 s timeout, 25 MB cap, 3 s between two requests to the same host (12 s for UN
  Comtrade, which answers 429 faster), one retry for 404/410, 429, 5xx and network errors.
- **Never verified without reading:** a challenge page ("Just a moment...", "Access denied",
  ...), a page with under 200 characters of text, a non-JSON API answer or an unreadable file is
  `unreachable`; hosts in `manual_hosts` and entries with `check: "manual"` are a **manual
  re-check due**. If the page is byte-for-byte the app's saved copy but a quote is not on it, the
  manifest is wrong: `manifest_error`, an issue, nothing sent.
- **No page text in public.** ops-monitor is a public repository and official pages may not be
  reproduced (the u.ae copyright notice forbids it), so page text never goes to this repository:
  not to a branch, an issue, the run log or an artifact. Two stores:
  - **Public state** (`--state`, the `source-watch-state` branch here): per source only `id`,
    `url`, `status`, `fetched_at`, `last_read_fetched_at`, `sha256`, `url_ar`, `sha256_ar`,
    `manifest_fingerprint`, `run_id` (`PUBLIC_STATE_KEYS`), plus the radar's seen list
    (`radar/seen.json`, item URLs only), the proof queue and the timelines
    ([10](#10-public-timeline-of-readings)). `--check-public-state DIR` refuses
    any other file or key, or a long or multi-line value, and the workflow runs it before every
    commit, so a leak fails the run instead of being published.
  - **Private captures** (`--captures`, a checkout of the PRIVATE repository named by
    `SOURCE_CAPTURES_REPO`): each page's normalised text (`<app>/<id>.txt`, Arabic `<id>.ar.txt`),
    its full metadata, `<id>.review.md` (closest text to each missing quote and the diff against
    the previous capture), the manifest it checked against, and the run report
    (`runs/<run_id>-<app>.json`). The workflow writes only after `GET /repos/<repo>` says
    `private: true`. `--offline` replays the checks on these captures without the network.
  - **No private store configured** (variable unset, token unset, repository not private or not
    reachable): captures, diffs and excerpts are not saved, a warning is logged, nothing fails, and
    the public state is still kept. Workflow artifacts are not used as a fallback: on a public
    repository anyone signed in to GitHub can download them.
- **Alerts:** issues in the app repo (with `SOURCE_WATCH_ISSUES_TOKEN`; otherwise in
  ops-monitor), one per source, for `quote_missing`, `gone`, `manifest_error`, `new_edition` and
  language disagreements: the page URL and hashes, the app's own quote (already public in its
  manifest), any missing value, and where the excerpts and diff are in the private store. Never
  page text. One summary per run on the pinned `Source watch` issue in ops-monitor.

Run locally: `python3 source_watch.py --dry-run` (no issue, nothing sent), or against a local app:
`--manifest cosecha=http://localhost:8080/.well-known/sources.json --verify-url cosecha=http://localhost:8080/internal/sources/verify`
with the token in `SOURCE_VERIFY_TOKEN_COSECHA`.

## 6. Canary: the verifier checks itself

Before any app is checked, the watcher serves a control site of its own on `127.0.0.1` and runs
the real fetch, extraction and check code against it. The site's words change on a schedule (odd
and even days of the year); one case per check kind has a fixed expected result:

| case | even day | odd day |
|---|---|---|
| quote (HTML) | verified | quote_missing |
| value in the quote's table row | verified | quote_missing (value) |
| PDF quote and value | verified | quote_missing |
| API field | verified | quote_missing |
| index page with editions | verified | verified + new_edition |
| English/Arabic pair | verified | changed_quote_ok + languages disagree |
| challenge page | unreachable | unreachable |
| browser-only shell page | unreachable | unreachable |
| 404 | gone | gone |

Any other answer fails the run: **nothing is sent to any app**, the summary says CANARY FAILED,
an issue `CANARY FAILED: source watch` opens, and the workflow goes red. Unit tests cover each check
kind separately, and show that a checker that never sees a missing quote, or one that trusts
challenge pages, fails the canary.

## 7. Dead-man switch

- **App:** no verify request applied for more than 48 hours: the app stops showing `verified_at`
  dates (only the manifest's `read_at` dates show), cards and pages say "Automatic source checks
  paused since <date of the last report>", and unconfirmed facts stay unconfirmed. A new report
  resumes it.
- **ops-monitor:** the 15-minute uptime run reads each app's `/.well-known/sources-status.json`
  (`sources_status` in `checks.json`) and opens `SOURCE WATCH STALE: <app>` when the last report
  is older than the app's `stale_after_hours`, or when the verify endpoint is open and no report
  ever arrived. It closes when a report arrives.

## 8. Shared source registry

The same official page is often cited by several apps. In one run every URL is fetched once and
the result is checked against each app's quotes. The summary lists every page cited by more than
one app with the apps that cite it, and flags a **conflict** when, on the same capture, one app's
quotes are found and another's are not, or two apps quote the same words with different values.

## 9. New-rule radar

Beyond the cited pages, the watcher reads official announcement and circular lists
(`radar_feeds` in `source-watch.json`) once per run: RSS/Atom entries, or the page's headline
links. New items (not seen in earlier runs; the seen list of item URLs is kept on the public `source-watch-state` branch) are
matched against each app's `radar_keywords`: whole words, case-folded, Arabic included, no model.
Matches go into one digest issue per app (`Source watch radar: <app>`). **Nothing in any app
changes.** A list read for the first time is seeded silently. A list that yields no items (it
needs a browser, or blocks robots) is reported as not readable, never as watched.

Lists configured, as read with a plain GET on 2026-11-21: GDRFA Dubai news and MOIAT news give
items; MOCCAE news, MOCCAE legislation and ICP news render in the browser (no items); the Dubai
Legislation Portal home lists categories, not new laws; Dubai DET news answers 403. The last five
stay in the list so their state shows in every summary; they need an official feed or API (see
Prior art) to become useful.

## 10. Public timeline of readings

F2 in `dubai-docs/MONAD-RECORDS.md` §12. Each real run (not `--dry-run`, not `--offline`, canary
passed), for every page the watch actually fetched (`source_timeline.py`):

1. **Save Page Now.** The Internet Archive is asked to keep its own copy, from its own network
   and with its own timestamp: an independent witness that we did not invent the page. SPN2
   (`POST https://web.archive.org/save`, then `GET /save/status/<job_id>`) with only the page URL
   and `if_not_archived_within=20h`, user agent `source-watch/1.0`, no cookies and no personal
   data. Polite: one capture request per 21 s (anonymous SPN2 allows about 3 per minute and 200 per
   day), at most 40 per run, each URL once per run even when several apps cite it, and the first
   HTTP 429 stops archiving for the run. A refusal or error is recorded as
   `not archived: <reason>`, never raised. Optional `SOURCE_WATCH_SPN_KEY` secret (SPN2 S3 key
   `access:secret`, higher limits). `SOURCE_WATCH_SPN=off` (variable) or `archive.enabled: false`
   in `source-watch.json` switch it off.
   Limits are from the SPN2 API document (a Google Doc copy, read 2026-11-21, MEDIUM: it calls
   the endpoint both "requires authentication" and open to anonymous users; if anonymous
   requests are refused, every reading says `not archived` until the key is set).
2. **`source_read` record, public mode** (MONAD-RECORDS §3.1 and §3.3), one per app and source,
   appended to `proof-queue/source_read.jsonl` on the public `source-watch-state` branch:
   ```json
   {"v": 1, "mode": "public", "app": "ops", "kind": "source_read",
    "ref": "cosecha/s-13d73aaae1e2/2026-11-22T05:17:09Z",
    "payload": {"source_id": "s-13d73aaae1e2", "cited_by": "cosecha", "url": "https://...",
                "sha256": "<hex>", "hash_of": "normalised-text-v1", "fetched_at": "...",
                "status": "verified", "http_status": 200, "quotes": ["<the app's short quote>"],
                "archive_url": "https://web.archive.org/web/<14 digits>/https://...",
                "archive_status": "archived", "run_id": "..."},
    "payload_sha256": "<sha256 of the canonical payload>", "queued_at": "...", "anchor": null}
   ```
   Canonical payload = JSON with sorted keys, no spaces, UTF-8. `sha256` is over the watcher's
   normal form of the page text (empty when the page was not read). Quotes are the app's own,
   from its manifest, and only those of 400 characters or fewer. A ref is queued once (a
   re-dispatched run adds nothing). **Anchoring is not wired yet**: `Anchorer` is the interface
   the proof batcher implements (`anchor(records) -> receipts`); today `NotWiredAnchorer` anchors
   nothing and `anchor` stays `null`. The batcher computes the public leaf from app, kind, ref and
   payload when the proof module's public mode exists.
3. **Timeline**, one public JSON file per source: `timeline/<app>/<id>.json` on the same branch,
   with `url`, `url_ar` and one entry per reading (`fetched_at`, `status`, `http_status`,
   `sha256`, `sha256_ar`, `quotes`, `archive_url`, `archive_status`, `run_id`, `ref`,
   `payload_sha256`, `changed_since_previous` = the text hash differs from the last read one).
   It shows what the page said (its hash, its archived copy and whether our quotes were on it),
   never our reading of it.

`--check-public-state` validates the queue and every timeline before each commit: only the keys
above, quotes of at most 400 characters, short single-line strings. Nothing else may be public.

## Fix proposals

When a source changes, the issue carries the evidence. Optionally the watcher also opens a
**draft PR** in the app repo (`fix.quotes_path` in `source-watch.json`; the file maps page URL to
`{sha256, quotes[], sha256_ar?, quotes_ar?}` as Cosecha's `internal/sources/quotes.json` does):

- every quote still present on a changed page: the PR refreshes the saved-copy `sha256`;
- one quote missing as a whole, and a model draft that passed the gate: the PR replaces that
  quote's words with the draft's quote, **keeps its values**, and only if those values stand next
  to the new words on the new capture.

A missing **value** never gets a PR: numbers change only by a person. One PR per source (branch
`source-watch/<id>`); a later run updates it. Never merged by the watcher. The app's CI
(`source-fix-evidence.yml` in Cosecha) runs the whole test suite, which includes every offline
eval case and grader, on the PR's base and head and comments the before/after; it goes red when
the PR fails a test the base passes.

**Model draft** (off by default): `SOURCE_WATCH_LLM=on` (repository variable) and an
`ANTHROPIC_API_KEY` secret. Model `claude-opus-5-5` (override `SOURCE_WATCH_LLM_MODEL`; a model
without a price in the code is refused), effort `low`, at most 4000 output tokens per draft.
Per-run cap `SOURCE_WATCH_LLM_MAX_USD` (default 0.50, hard ceiling 5.00): each call reserves its
worst case before it runs and is refused if that would pass the cap; any error gives no draft.
The page text is passed as untrusted data. The draft is kept only if its quote is at least 20
and at most 400 characters (it is published in the issue, so it must stay a short quote), appears verbatim in the new capture, and every number in the proposed fact is also in
that quote.

## Owner setup

Nothing below is set by this change.

| where | name | value |
|---|---|---|
| ops-monitor, Actions secret | `SOURCE_VERIFY_TOKEN_COSECHA` | a random string of 32+ characters (e.g. `openssl rand -hex 32`) |
| Cosecha, Railway variable | `SOURCES_VERIFY_TOKEN` | the same string |
| ops-monitor, Actions secret (optional) | `SOURCE_WATCH_ISSUES_TOKEN` | fine-grained PAT, resource owner EmpowerTours, only the app repos (cosecha now), permissions **Issues: read and write**, **Contents: read and write**, **Pull requests: read and write**. Without it issues go to ops-monitor and no fix PR is opened. |
| GitHub, new **private** repository (optional) | e.g. `EmpowerTours/source-captures` | holds the full page captures; created by the owner, not by this change. Must stay private. |
| ops-monitor, Actions variable or secret (optional) | `SOURCE_CAPTURES_REPO` | `owner/name` of that private repository. Unset: captures, diffs and excerpts are not saved (warning only). |
| ops-monitor, Actions secret (optional) | `SOURCE_CAPTURES_TOKEN` | fine-grained PAT, only that repository, **Contents: read and write** (and Metadata: read, for the private check). |
| ops-monitor, Actions secret (optional) | `SOURCE_WATCH_SPN_KEY` | Internet Archive SPN2 S3 key `access:secret` (archive.org account, free). Unset: anonymous Save Page Now. |
| ops-monitor, Actions variable (optional) | `SOURCE_WATCH_SPN` = `off` | stops Save Page Now requests |
| ops-monitor, Actions secret (optional) | `ANTHROPIC_API_KEY` | only for model drafts |
| ops-monitor, Actions variables (optional) | `SOURCE_WATCH_LLM` = `on`, `SOURCE_WATCH_LLM_MAX_USD` | drafts on, per-run cap |
| Cloudflare | redeploy `worker/` (`npx wrangler deploy`) | adds the daily cron. The existing `GH_DISPATCH_TOKEN` already has Actions read/write on ops-monitor, which covers the new workflow: **no new scope**. |

Per further app: one `SOURCE_VERIFY_TOKEN_<APP>` secret here and the same value in that app's
verify-token variable.

## Adopting it in the other apps

Cosecha (Go) is the first adopter: `internal/sources` (manifest, validation), `internal/store`
(`009_source_watch.sql`: `source_checks`, `source_watch_runs`, `source_watch_audit`),
`internal/web/sources.go` (the three endpoints, the overlay refresh) and `internal/rules/watch.go`
(the overlay: fact ids, uncertain steps, derived dates). Each other app does the same in its own
stack; the contract above is the interface. For every app:

1. **Facts and fact ids.** Name each fact the app states from a page (a fee, a requirement, a
   verdict) with a stable id, and map each cited page to its facts. Start from the store the app
   already has (read dates per source, quotes, `sources[]`).
2. **Quotes.** For each page, copy one or two short exact quotes per fact from a watcher capture
   (run `python3 source_watch.py --dry-run --manifest <app>=<file>` with a manifest that has empty
   quotes and `--captures <local dir>`; the text lands in `<local dir>/<app>/<id>.txt`; keep that
   directory out of any public repository), add `values` for every number or date the
   app shows, and the capture's `sha256`. Pages a robot cannot read: `check: "manual"`.
3. **Endpoints:** manifest, verify (token, 503 when unset, constant-time compare, rate limit,
   idempotent `run_id`, audit log, fingerprint check, anti-flap) and status, as in 1, 2 and 4.
4. **Display:** an unconfirmed fact reads "to be confirmed" on every card and draft; dates are
   `max(read_at, verified_at)` per answer; the paused line after 48 h without a report; EN/AR (and
   ES where the app has it) strings, flagged for native review.
5. **Tests first:** forged token refused, unset token fails closed, a single miss changes nothing,
   two daily misses flip the card, verified bumps "last checked", a fingerprint change clears, the
   dead-man pause, replay and conflict.
6. **Register** the app in `source-watch.json` (and `sources_status` in `checks.json`), set the
   two tokens, and optionally `fix.quotes_path` plus a `source-fix-evidence` workflow running the
   app's tests and evals.

| app | stack | what it already has to start from | notes |
|---|---|---|---|
| Asas | (its repo) | per-source read dates with an 18-month staleness limit | shares ICP golden residency pages with RelocateDubai: registry conflicts will show |
| Faez | (its repo) | Dubai Pulse / data.dubai datasets | declare those as `kind: "api"` with `fields` rather than scraping the pages |
| Haqiqi | (its repo) | cited official pages | |
| RelocateDubai | (its repo) | facts with several `sources[{url, read_at, quote, value}]` | the quote and value per source map directly to `quotes[].text` and `values` |
| Hunt Dubai | (its repo) | real places only | official pages behind any rule it states (entry rules, opening hours it cites) |
| Sawweeli Trips | (its repo) | cited official pages | |
| Sawweeli hub (TS) | TypeScript | relays app facts; cites the MOCCAE contact page too | manifest for its own cited pages only; it already shows the apps' `read_at`, so it needs no overlay for theirs |

Pages that could move to official feeds or APIs (`kind: "api"`) now: Cosecha's UN Comtrade
figures already are API sources (the two honey lines are checked by field). Faez's Dubai Pulse
datasets. The radar lists in 9 that render in the browser would need an official RSS or open-data
feed; none was found by a plain GET on 2026-11-21.

## Prior art: why our own fetcher

Checked on 2026-11-21 (sources: the project's GitHub README, Railway's deploy template page, a
competitor's comparison; memory and price figures are those pages' claims, not measured here).

- **changedetection.io** (open source, Apache-2.0, Python, Docker; hosted plan about USD 8.99 a
  month): it fetches pages, diffs them, filters text, and notifies through Apprise. It does not
  know about our quotes, values, facts, fingerprints, Arabic pairs or the verify contract, so we
  would still write the quote check, the reporting and the app side, and add its API in between.
  Self-hosted it is one more always-on service: Railway's template asks for 1 GB RAM minimum and
  2-4 GB recommended once a Chrome/Playwright fetcher is added for browser-rendered pages. At
  Railway's RAM pricing that is several dollars a month more on a bill that is RAM-driven
  (roughly USD 31 a month today), against zero for a daily GitHub Actions job.
- **urlwatch**, **Visualping**, **Distill.io**: the same diff-and-notify layer; the hosted ones
  add a subscription and send our page list to a third party.

Our fetch-and-extract layer is a few hundred lines with no service to run, and the canary tests it
every day. Recommendation: keep it. Revisit if many cited pages turn out to need a real browser:
then a Playwright step in the same Actions job (or changedetection.io's browser fetcher, called
only for `check: "browser"` pages) is the next step, still feeding our own quote check.
