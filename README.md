# ops-monitor

Uptime monitor for the EmpowerTours Dubai championship apps. It runs only on
GitHub Actions (every 15 minutes), uses the Python 3 standard library, makes
plain read-only `GET` requests, and stores no credentials: the only token it
uses are the workflow's built-in `GITHUB_TOKEN` and, optionally, `OPS_TOKEN`
for the Sawweeli hub's ops endpoints (see [AI spend](#ai-spend)).

## What is checked

Each entry in [`checks.json`](checks.json) is checked for:

| check | rule |
|---|---|
| HTTP status of `url` | must be `200` (redirects followed) |
| keyword | `keyword` must appear in the page body (and on the judge page) |
| judge path | `judge_path` must return `200` and contain the keyword |
| `/healthz` | must return `200` (and contain `healthz_keyword` if set) |
| TLS certificate | fails when fewer than 14 days are left |
| response time | recorded; a request taking longer than 20 s fails |

A failing app is retried once after 30 s. Only if the retry also fails is it
declared down. Response headers and cookies are never printed or stored.

Current state is in [`status/status.json`](status/status.json); every run also
writes a status table to the run's summary page (Actions tab).

## How alerts arrive

Alerts are GitHub issues, and GitHub emails them to you:

- **Down:** the workflow opens one issue per app, titled `DOWN: <app>`, labelled
  `outage` (or comments on it if one is already open).
- **Recovery:** it comments the downtime duration and closes the issue.
- **Daily summary:** at about 14:00 UTC it comments on the pinned `Daily status`
  issue with 24 h uptime %, average and max latency, the slowest app and
  certificate days left.

To get the emails you must **Watch** this repository: *Watch -> All activity*
(or *Custom -> Issues*), and have email enabled for "Watching" in
<https://github.com/settings/notifications>. Issues are opened by
`github-actions[bot]`, so they do notify you.

## What starts a run

GitHub's scheduler fired this workflow's cron only once in 14 hours on
2026-11-19, so the primary trigger is a Cloudflare cron Worker
([`worker/`](worker/README.md)) that calls `workflow_dispatch` for `uptime.yml`
on `main` every 15 minutes. The GitHub cron (`7,22,37,52 * * * *`) stays as a
backup. The daily summary flags every gap of more than 45 minutes between runs
in the last 24 h (from the run history in the Actions cache), so a broken
trigger shows up the next day.

## AI spend

Seven apps publish today's AI spend against their daily cap at the public
`GET /readyz` (the `readyz` field in `checks.json`). Two formats are parsed:
JSON `{"ai_spend": {"day", "spent_usd", "reserved_usd", "cap_usd"}}` and the
text line `ai_spend day=... spent=USD x reserved=USD y cap=USD z`. Every run
writes a spend table to the run's summary page, and the daily summary includes
it.

- **Alert:** when an app's spent + reserved reaches **80%** of its daily cap,
  the workflow opens one issue `SPEND: <app>` labelled `spend`. It closes the
  issue when the share drops below 80%, when the app's day rolls over (each
  app's own `day`), when the check is removed, or, if `/readyz` stays
  unreadable, two days after the alert's day. A cap of 0 means spending is
  closed and never alerts. The app's own cap still stops spending at 100%;
  this is only the warning.
- **Hub (optional):** with the repository secret `OPS_TOKEN` set, each run also
  calls `GET /api/ops/spend` on the Sawweeli hub (LLM, TTS and STT spend against
  their caps, alerting the same way as `sawweeli-hub/llm` etc.), and the daily
  summary calls `POST /api/ops/voice-keepalive`, which keeps the two cloned
  MiniMax voices alive (MiniMax deletes a clone unused for 7 days). Without
  `OPS_TOKEN` both are skipped quietly. The token must equal the hub's
  `OPS_TOKEN` Railway variable.

## Monad records (hub)

With `OPS_TOKEN` set, the same `GET /api/ops/spend` response carries the hub's
`records` section (ProofAnchor record layer, sawweeli-agent PR #26:
`spent_mon`, `anchors`, `waiting`, `oldest_waiting_min`, `refill_alert`,
`cap_alert`, `balance_mon`). Each condition gets one issue
`RECORDS <KIND>: sawweeli-hub` labelled `records`, reused if already open, not
commented on while it lasts, and closed when it clears:

| kind | opens when |
|---|---|
| `REFILL` | the worker raised its refill alert (relayer under 15 MON) |
| `RESERVE` | that alert's balance is within 0.1 MON of the 10 MON reserve and records are waiting: anchoring is paused |
| `CAP` | the worker raised its daily-cap alert and records are waiting (they go out the next UTC day) |
| `STUCK` | a record has waited more than 1 h, counted from `records_activated_at` |

- No `records` section in the response: the record layer is not configured,
  nothing opens, and open `RECORDS` issues close.
- `records` unreadable (`{"error": ...}`, a bad shape, or the hub call failed):
  nothing opens and nothing closes.
- `records_activated_at` (on the `sawweeli-hub` entry in `checks.json`, an
  RFC 3339 time) is when the hub relayer was activated on the contract. Until
  it is set the `STUCK` alert is off, because records queue on purpose before
  activation. Waiting time before activation does not count.
- The hub reports the worker's refill and cap alerts for 24 h after the last
  one, so `REFILL` can stay open up to a day after a refill.

## Source watch

Daily, a second workflow (`source-watch.yml`, dispatched by the same Worker at 05:17 UTC) checks
that the official pages the apps cite still say what the apps rely on, and reports to each app,
which then shows a fact as unconfirmed when its words are gone (two daily misses on the same page
text). It tests itself first with a canary, never counts a page it could not read as verified,
keeps only url, sha256, fetched_at and status here (page text goes only to a private capture
repository, `SOURCE_CAPTURES_REPO`, or is not saved), and alerts through issues labelled `source-watch`.
The uptime run alerts when an app's source watch has not reported for 48 h. Contract, owner setup
and adoption guide: [`docs/SOURCE-WATCH.md`](docs/SOURCE-WATCH.md).

## Tests

```sh
python3 -m unittest discover -s tests -v   # monitor and source watch (pip install pypdf==6.21.4 for the PDF cases)
node --test worker/test/index.test.mjs     # dispatch Worker
```

`tests/fixtures/readyz-*.txt` are the real `/readyz` bodies captured with plain
GETs on 2026-11-19.

## How to add an app

Add an object to `checks.json` and push:

```json
{
  "app": "my-app",
  "url": "https://my-app.empowertours.xyz/",
  "judge_path": "/judge",
  "healthz": "/healthz",
  "keyword": "My App"
}
```

`judge_path` and `healthz` may be `null`. Pick a keyword that is part of the
page template (the brand name in `<title>` works well), not live data.
Removing an entry closes any open `DOWN` issue for it on the next run.

Test locally without touching issues (no `GITHUB_TOKEN` means dry run):
`python3 monitor.py`.

## Pause and stop

- **Pause:** Actions -> `uptime` -> `...` -> *Disable workflow*, or
  `gh workflow disable uptime -R EmpowerTours/ops-monitor`. Re-enable with
  `gh workflow enable uptime -R EmpowerTours/ops-monitor`.
- **Stop for good:** delete `.github/workflows/uptime.yml`, or archive the
  repository (Settings -> Archive), which stops all workflows.

GitHub disables scheduled workflows in a repository with no activity for 60
days. The workflow commits `status/status.json` at least once a day (with the
daily summary), which keeps the repository active.

To stop the dispatch Worker as well: `npx wrangler delete` in `worker/`.
