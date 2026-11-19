# ops-monitor

Uptime monitor for the EmpowerTours Dubai championship apps. It runs only on
GitHub Actions (every 15 minutes), uses the Python 3 standard library, makes
plain read-only `GET` requests, and stores no credentials: the only token it
uses is the workflow's built-in `GITHUB_TOKEN`.

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

GitHub's scheduler can start runs a few minutes late, so "every 15 minutes" is
best effort.

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
