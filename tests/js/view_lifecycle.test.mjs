import assert from "node:assert/strict";
import test from "node:test";

import { ViewLifecycle } from "../../src/arxiv_digest/web/static/view_lifecycle.mjs";
import { StaleResponseError } from "../../src/arxiv_digest/web/static/api.mjs";

test("leaving a view invalidates pending responses and scheduled callbacks", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const lifecycle = new ViewLifecycle();
  const pending = lifecycle.capture();
  let calls = 0;
  lifecycle.schedule(() => calls++, 100);
  const current = lifecycle.invalidate();
  lifecycle.schedule(() => calls++, 100);

  assert.equal(lifecycle.isCurrent(pending), false);
  assert.equal(lifecycle.isCurrent(current), true);
  context.mock.timers.tick(100);
  assert.equal(calls, 1);
});

test("inactive application work cannot publish responses or run its timer", (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  let active = true;
  const lifecycle = new ViewLifecycle(() => active);
  const pending = lifecycle.capture();
  let calls = 0;
  lifecycle.schedule(() => calls++, 100);
  active = false;

  assert.equal(lifecycle.isCurrent(pending), false);
  context.mock.timers.tick(100);
  assert.equal(calls, 0);
});

test("late response success and failure are both discarded after navigation", async () => {
  const lifecycle = new ViewLifecycle();
  let resolve;
  let reject;
  const success = lifecycle.wait(new Promise((done) => { resolve = done; }));
  const failure = lifecycle.wait(new Promise((_done, fail) => { reject = fail; }));
  lifecycle.invalidate();
  resolve("old view content");
  reject(new Error("old view error"));
  await assert.rejects(success, StaleResponseError);
  await assert.rejects(failure, StaleResponseError);
  assert.equal(await lifecycle.wait(Promise.resolve("current view")), "current view");
});
