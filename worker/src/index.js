/* Cron Worker that starts the uptime workflow of EmpowerTours/ops-monitor every 15 minutes, and the
   source-watch workflow once a day (05:17 UTC).

   GitHub's own scheduler fires the workflow's cron unreliably for this repository, so this Worker calls the GitHub REST
   API "create a workflow dispatch event" for .github/workflows/uptime.yml on main:
     POST https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches   body {"ref": "main"}
   GitHub answers 204 No Content when the run was queued.

   The token is the Worker secret GH_DISPATCH_TOKEN: a fine-grained personal access token limited to the one repository
   with "Actions: Read and write". It is never logged. A failed dispatch throws, so the invocation shows as an error in
   the Worker's logs; the GitHub cron in uptime.yml stays as the backup. */

export const DEFAULTS = {
  repo: "EmpowerTours/ops-monitor",
  workflow: "uptime.yml",
  ref: "main",
};

/* The daily source watch (docs/SOURCE-WATCH.md) runs from the same Worker on its own cron. It is
   the same repository, so the same token (Actions: Read and write on ops-monitor) dispatches it. */
export const SOURCE_WATCH_CRON = "17 5 * * *";
export const SOURCE_WATCH_WORKFLOW = "source-watch.yml";

/** The workflow a cron firing dispatches. */
export function workflowFor(cron, env) {
  if (cron === SOURCE_WATCH_CRON) return SOURCE_WATCH_WORKFLOW;
  return env.GH_WORKFLOW || DEFAULTS.workflow;
}

/** Build the dispatch request. Pure, so it can be tested without the network. */
export function dispatchRequest(env, workflowOverride) {
  const token = (env.GH_DISPATCH_TOKEN || "").trim();
  if (!token) throw new Error("GH_DISPATCH_TOKEN is not set");
  const repo = env.GH_REPO || DEFAULTS.repo;
  const workflow = workflowOverride || env.GH_WORKFLOW || DEFAULTS.workflow;
  const ref = env.GH_REF || DEFAULTS.ref;
  return {
    url: `https://api.github.com/repos/${repo}/actions/workflows/${encodeURIComponent(workflow)}/dispatches`,
    init: {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects API requests without a User-Agent.
        "User-Agent": "EmpowerTours-ops-monitor-dispatch/1.0",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref }),
    },
  };
}

/** One dispatch. Resolves with the HTTP status on success (2xx), throws otherwise. */
export async function dispatch(env, fetchImpl = fetch, workflow) {
  const { url, init } = dispatchRequest(env, workflow);
  const res = await fetchImpl(url, {
    ...init,
    signal: AbortSignal.timeout(15_000),
  });
  if (res.status < 200 || res.status >= 300) {
    // The response body is GitHub's error message; it never contains the token.
    const text = (await res.text().catch(() => "")).slice(0, 300);
    throw new Error(`workflow_dispatch failed: HTTP ${res.status} ${text}`);
  }
  return res.status;
}

export default {
  async scheduled(controller, env, ctx) {
    const workflow = workflowFor(controller.cron, env);
    const status = await dispatch(env, fetch, workflow);
    console.log(`dispatched ${workflow} on ${env.GH_REF || DEFAULTS.ref}: HTTP ${status}`);
  },
};
