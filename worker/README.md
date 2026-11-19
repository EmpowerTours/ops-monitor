# ops-monitor-dispatch (Cloudflare cron Worker)

GitHub's scheduler barely fires the `uptime` workflow's cron for this repository (one run in 14 hours on
2026-11-19). This Worker is the primary trigger: every 15 minutes (`*/15 * * * *`, UTC) it calls the GitHub REST API
`POST /repos/EmpowerTours/ops-monitor/actions/workflows/uptime.yml/dispatches` with `{"ref": "main"}`. GitHub answers
`204` when the run is queued. The cron in `.github/workflows/uptime.yml` stays as a backup.

- Cron only: `workers_dev = false` and no routes, so the Worker has no public URL.
- No dependencies and no build step: `src/index.js` is the whole Worker.
- The token is the Worker secret `GH_DISPATCH_TOKEN`. It is never logged. A failed dispatch (bad or expired token,
  wrong permission) throws, so it shows as an error under the Worker's logs in the Cloudflare dashboard.
- Optional plain vars: `GH_REPO`, `GH_WORKFLOW`, `GH_REF` (defaults: `EmpowerTours/ops-monitor`, `uptime.yml`, `main`).

## Owner steps (in order)

1. **Create the token.** GitHub -> Settings -> Developer settings -> Personal access tokens -> *Fine-grained tokens* ->
   *Generate new token*:
   - Resource owner: `EmpowerTours`
   - Repository access: *Only select repositories* -> `EmpowerTours/ops-monitor` (nothing else)
   - Repository permissions: **Actions: Read and write** (Metadata: Read-only is added automatically). Nothing else.
   - Expiration: your choice; note the date, because the Worker stops dispatching when it expires (the GitHub cron
     backup keeps running, and the daily summary flags the gaps).

   Copy the token; it is shown once. Do not paste it anywhere except step 3.
2. **Deploy the Worker** from this directory:

   ```sh
   cd worker
   npx wrangler login        # once, if wrangler is not logged in to the EmpowerTours Cloudflare account
   npx wrangler deploy
   ```

3. **Set the secret** (wrangler prompts for the value; paste the token from step 1):

   ```sh
   npx wrangler secret put GH_DISPATCH_TOKEN
   ```

4. **Check it.** Within 15 minutes a new `uptime` run with event `workflow_dispatch` appears under Actions, or:

   ```sh
   gh run list -R EmpowerTours/ops-monitor -w uptime -e workflow_dispatch -L 3
   ```

   If no run appears, open the Worker in the Cloudflare dashboard -> *Logs* and look for `workflow_dispatch failed:
   HTTP 401/403/404` (token wrong, expired, or missing the Actions permission).

## Stop

`npx wrangler delete` from this directory removes the Worker; the GitHub cron backup keeps running.

## Test

```sh
node --test worker/test/index.test.mjs
```
