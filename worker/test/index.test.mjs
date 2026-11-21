// Run: node --test worker/test/
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { dispatch, dispatchRequest, workflowFor, SOURCE_WATCH_CRON } from "../src/index.js";
import { readFileSync } from "node:fs";

const ENV = { GH_DISPATCH_TOKEN: "test-token-not-real" };

test("request: POST to uptime.yml dispatches on main with the bearer token", () => {
  const { url, init } = dispatchRequest(ENV);
  assert.equal(
    url,
    "https://api.github.com/repos/EmpowerTours/ops-monitor/actions/workflows/uptime.yml/dispatches",
  );
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer test-token-not-real");
  assert.equal(init.headers.Accept, "application/vnd.github+json");
  assert.ok(init.headers["User-Agent"]);
  assert.deepEqual(JSON.parse(init.body), { ref: "main" });
});

test("request: no token throws before any network call", async () => {
  assert.throws(() => dispatchRequest({}), /GH_DISPATCH_TOKEN/);
  let called = false;
  await assert.rejects(
    dispatch({ GH_DISPATCH_TOKEN: "  " }, async () => {
      called = true;
    }),
    /GH_DISPATCH_TOKEN/,
  );
  assert.equal(called, false);
});

test("dispatch: 204 resolves; 403 and 404 throw without echoing the token", async () => {
  assert.equal(
    await dispatch(ENV, async () => new Response(null, { status: 204 })),
    204,
  );
  for (const status of [401, 403, 404, 422]) {
    const err = await dispatch(
      ENV,
      async () =>
        new Response('{"message":"Resource not accessible"}', { status }),
    ).catch((e) => e);
    assert.ok(err instanceof Error);
    assert.match(err.message, new RegExp(`HTTP ${status}`));
    assert.doesNotMatch(err.message, /test-token-not-real/);
  }
});

test("scheduled handler: one dispatch per invocation", async () => {
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push([url, init.method]);
    return new Response(null, { status: 204 });
  };
  try {
    await worker.scheduled({ cron: "*/15 * * * *" }, ENV, {});
  } finally {
    globalThis.fetch = orig;
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1], "POST");
});

test("daily cron dispatches source-watch.yml; the 15-minute cron stays on uptime.yml", async () => {
  assert.equal(workflowFor(SOURCE_WATCH_CRON, {}), "source-watch.yml");
  assert.equal(workflowFor("*/15 * * * *", {}), "uptime.yml");
  const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.ok(toml.includes(`"${SOURCE_WATCH_CRON}"`), "wrangler.toml must schedule the source watch cron");
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(url);
    return new Response(null, { status: 204 });
  };
  try {
    await worker.scheduled({ cron: SOURCE_WATCH_CRON }, ENV, {});
  } finally {
    globalThis.fetch = orig;
  }
  assert.equal(
    calls[0],
    "https://api.github.com/repos/EmpowerTours/ops-monitor/actions/workflows/source-watch.yml/dispatches",
  );
});
