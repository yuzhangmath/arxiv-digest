import assert from "node:assert/strict";
import test from "node:test";
import { UpdateFlow, COMMIT_MESSAGE } from "../../src/arxiv_digest/web/static/update_flow.mjs";
import { MARKER_KEY, TransitionStore } from "../../src/arxiv_digest/web/static/update_transition.mjs";
import { memoryStorage } from "./dom_test_helper.mjs";

const nonce = "fixture_startup_nonce";
const a = "a".repeat(64);
const b = "b".repeat(64);
const available = {status: "available_automatic", automatic_update: true, installed_version: "0.3.0", available_version: "0.3.1"};
const running = (job = a, phase = "downloading") => ({job_id: job, state: "running", phase, complete: false});
const ready = {job_id: a, state: "ready_to_restart", phase: "ready_to_restart", complete: false};
const flush = () => new Promise((resolve) => setImmediate(resolve));
function harness(overrides = {}, options = {}) {
  const requests = [], events = [], views = [], timers = new Map();
  const storage = options.storage ?? memoryStorage();
  let timerId = 0;
  const responses = {
    "/api/v1/status": {startup_nonce: nonce},
    "/api/v1/update": available,
    "/api/v1/update/receipt": {receipt: null},
    "/api/v1/update/start": {job_id: a, state: "running", phase: "downloading"},
    [`/api/v1/update/jobs/${a}`]: running(),
    [`/api/v1/update/jobs/${a}/commit`]: {job_id: a, state: "restarting", phase: "restarting", message: COMMIT_MESSAGE},
    [`/api/v1/update/jobs/${a}/handoff-ack`]: {job_id: a, state: "restarting", phase: "restarting"},
    ...overrides,
  };
  const flow = new UpdateFlow({
    api: {
      json: async (key, path, body) => {
        requests.push({key, path, body});
        if (path.endsWith("handoff-ack")) {
          events.push("ack");
          assert.equal(views.at(-1).mode, "restarting");
        }
        const value = responses[path];
        return typeof value === "function" ? value() : value;
      },
      abort() {},
    },
    storage, broadcast: (message) => events.push(message.type),
    render: (view) => {views.push(view); if (view.mode === "restarting") events.push("render");},
    now: () => 1000, wallNow: () => 1000,
    setTimer: (callback, delay) => {const id = ++timerId; timers.set(id, {callback, delay}); return id;},
    clearTimer: (id) => timers.delete(id),
  });
  return {flow, requests, events, views, timers, storage, responses};
}

test("one click is one start; handoff renders before marker broadcast and acknowledgement", async () => {
  const h = harness({[`/api/v1/update/jobs/${a}`]: ready});
  await h.flow.start(); await flush();
  await Promise.all([h.flow.startUpdate("0.3.1"), h.flow.startUpdate("0.3.1")]);
  await flush();
  assert.equal(h.requests.filter((r) => r.path.endsWith("/start")).length, 1);
  assert.deepEqual(h.events.slice(-3), ["render", "update_restarting", "ack"]);
  assert.equal(JSON.parse(h.storage.getItem(MARKER_KEY)).job_id, a);
  assert.equal(h.views.at(-1).mode, "restarting");
  assert.equal(h.timers.size, 0);
});

test("early preparation leaves content usable; 409 creates only a reversible block", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  await h.flow.startUpdate("0.3.1"); await flush();
  assert.equal(h.views.at(-1).blocked, false);
  h.flow.handleApiError({status: 409, code: "update_in_progress"});
  assert.equal(h.views.at(-1).blocked, true);
  assert.equal(h.views.at(-1).mode, "preparing");
  assert.equal(h.storage.getItem(MARKER_KEY), null);
});

test("only exact commit responses permit handoff", async () => {
  for (const mutation of [{job_id: b}, {unknown: true}, {message: "foreign"}]) {
    const h = harness({[`/api/v1/update/jobs/${a}`]: ready});
    h.responses[`/api/v1/update/jobs/${a}/commit`] = {...h.responses[`/api/v1/update/jobs/${a}/commit`], ...mutation};
    await h.flow.start(); await flush();
    await h.flow.startUpdate("0.3.1"); await flush();
    assert.equal(h.requests.some((r) => r.path.endsWith("handoff-ack")), false);
    assert.equal(h.storage.getItem(MARKER_KEY), null);
  }
});

test("storage and broadcasts can fail without blocking initiating handoff", async () => {
  const storage = {getItem() {throw Error();}, setItem() {throw Error();}, removeItem() {throw Error();}};
  const h = harness({[`/api/v1/update/jobs/${a}`]: ready}, {storage});
  h.flow.broadcast = () => {throw Error();};
  await h.flow.start(); await flush();
  await h.flow.startUpdate("0.3.1"); await flush();
  assert.equal(h.requests.filter((r) => r.path.endsWith("handoff-ack")).length, 1);
  assert.equal(h.views.at(-1).mode, "restarting");
});

test("safe cancellation clears only A; late A cannot overwrite or clear retry B", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  await h.flow.startUpdate("0.3.1"); await flush();
  h.responses[`/api/v1/update/jobs/${a}`] = {job_id: a, state: "canceled", phase: "ready_to_restart", complete: true, error_code: "handoff_not_acknowledged", message: "safe"};
  await h.flow.reconcile();
  assert.equal(h.views.at(-1).blocked, false);
  h.responses["/api/v1/update/start"] = {job_id: b, state: "running", phase: "downloading"};
  h.responses[`/api/v1/update/jobs/${b}`] = running(b);
  await h.flow.startUpdate("0.3.1"); await flush();
  h.flow.receive({schema_version: 1, type: "update_restarting", startup_nonce: nonce, job_id: b});
  new TransitionStore({storage: h.storage, now: () => 1000}).write(nonce, b);
  h.flow.receive({schema_version: 1, type: "update_canceled", startup_nonce: nonce, job_id: a});
  h.flow.receive({schema_version: 1, type: "update_restarting", startup_nonce: nonce, job_id: a});
  assert.equal(h.views.at(-1).job_id, b);
  assert.equal(JSON.parse(h.storage.getItem(MARKER_KEY)).job_id, b);
});

test("a suspended tab checks its exact marker before any resumed ordinary work", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  h.flow.receive({schema_version: 1, type: "update_preparing", startup_nonce: nonce, job_id: a});
  h.flow.suspend();
  new TransitionStore({storage: h.storage, now: () => 1000}).write(nonce, a);
  h.flow.resume();
  assert.equal(h.views.at(-1).mode, "restarting");
  assert.equal(h.flow.blocksOrdinaryWork, true);
});

test("a fresh startup ignores an older server marker", async () => {
  const h = harness();
  new TransitionStore({storage: h.storage, now: () => 1000}).write("different_startup_nonce", a);
  await h.flow.start(); await flush();
  assert.equal(h.views.at(-1).mode, "idle");
});

test("guarded commit failure retains the marker and permits fully-quit guidance", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  await h.flow.startUpdate("0.3.1"); await flush();
  h.flow.receive({schema_version: 1, type: "update_restarting", startup_nonce: nonce, job_id: a});
  new TransitionStore({storage: h.storage, now: () => 1000}).write(nonce, a);
  h.responses[`/api/v1/update/jobs/${a}`] = {job_id: a, state: "failed", phase: "restarting", complete: true, error_code: "helper_commit_failed", message: "private text ignored"};
  await h.flow.reconcile();
  assert.equal(h.views.at(-1).mode, "guarded");
  assert.equal(h.views.at(-1).allowQuit, true);
  assert.equal(JSON.parse(h.storage.getItem(MARKER_KEY)).job_id, a);
});

test("receipt is rendered before acknowledgement and failed ack retains visible notice", async () => {
  const receipt = {receipt_id: a, outcome: "updated", installed_version: "0.3.1", attempted_version: "0.3.1", message_code: "update_succeeded"};
  const h = harness({"/api/v1/update/receipt": {receipt}});
  h.responses[`/api/v1/update/receipt/${a}/ack`] = () => {
    assert.deepEqual(h.views.at(-1).receipt, receipt);
    throw Error("offline");
  };
  await h.flow.start(); await flush();
  assert.deepEqual(h.views.at(-1).receipt, receipt);
  assert.equal(h.requests.filter((r) => r.path.endsWith("/ack")).length, 1);
});


test("late commit A cannot enter handoff after safe cancellation and retry B", async () => {
  let resolveCommit;
  const h = harness({
    [`/api/v1/update/jobs/${a}`]: ready,
    [`/api/v1/update/jobs/${a}/commit`]: () => new Promise((resolve) => {resolveCommit = resolve;}),
  });
  await h.flow.start(); await flush();
  const startA = h.flow.startUpdate("0.3.1");
  await flush();
  h.responses[`/api/v1/update/jobs/${a}`] = {job_id: a, state: "canceled", phase: "ready_to_restart", complete: true, error_code: "handoff_not_acknowledged", message: "safe"};
  await h.flow.reconcile();
  h.responses["/api/v1/update/start"] = {job_id: b, state: "running", phase: "downloading"};
  h.responses[`/api/v1/update/jobs/${b}`] = running(b);
  await h.flow.startUpdate("0.3.1");
  resolveCommit({job_id: a, state: "restarting", phase: "restarting", message: COMMIT_MESSAGE});
  await startA;
  assert.equal(h.views.at(-1).job_id, b);
  assert.equal(h.views.at(-1).mode, "preparing");
  assert.equal(h.requests.some((request) => request.path.endsWith("handoff-ack")), false);
});


test("preparation broadcasts do not echo back into a cross-tab loop", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  h.flow.receive({schema_version: 1, type: "update_preparing", startup_nonce: nonce, job_id: a});
  assert.equal(h.events.filter((event) => event === "update_preparing").length, 0);
});


test("job polling ends at its observation budget without claiming cleanup", async () => {
  const h = harness();
  await h.flow.start(); await flush();
  await h.flow.startUpdate("0.3.1");
  h.flow.startedAt = -1_000_000;
  await h.flow.observeJob(h.flow.generation);
  assert.equal(h.timers.size, 0);
  assert.equal(h.views.at(-1).mode, "preparing");
  assert.equal(h.flow.busy, true);
});


test("malformed receipts never acknowledge durable outcomes", async () => {
  const h = harness({"/api/v1/update/receipt": {receipt: {
    receipt_id: a, outcome: "updated", installed_version: "0.3.1", attempted_version: "0.3.1",
    message_code: "update_succeeded", path: "unsafe",
  }}});
  await h.flow.start(); await flush();
  assert.equal(h.requests.some((request) => request.path.endsWith("/ack")), false);
  assert.equal(h.views.at(-1).receipt, null);
});


test("lost receipt acknowledgement gates starts and retries once after resumption", async () => {
  const receipt = {receipt_id: a, outcome: "updated", installed_version: "0.3.1", attempted_version: "0.3.1", message_code: "update_succeeded"};
  const h = harness({"/api/v1/update/receipt": {receipt}});
  let calls = 0;
  h.responses[`/api/v1/update/receipt/${a}/ack`] = () => {
    assert.deepEqual(h.views.at(-1).receipt, receipt);
    if (++calls === 1) throw Error("lost acknowledgement");
    return {acknowledged: true};
  };
  await h.flow.start(); await flush();
  assert.equal(h.views.at(-1).receipt_pending, true);
  await h.flow.startUpdate("0.3.1");
  assert.equal(h.requests.some((request) => request.path.endsWith("/start")), false);
  h.flow.resume(); await flush();
  assert.equal(calls, 2);
  assert.equal(h.views.at(-1).receipt_pending, false);
  await h.flow.startUpdate("0.3.1");
  assert.equal(h.requests.filter((request) => request.path.endsWith("/start")).length, 1);
});


test("receipt authentication rejection permanently stops further update requests", async () => {
  const h = harness({"/api/v1/update/receipt": () => {throw Object.assign(Error(), {status: 401});}});
  await h.flow.start(); await flush();
  const count = h.requests.length;
  h.flow.resume(); await flush();
  assert.equal(h.requests.length, count);
  assert.equal(h.flow.stopped, true);
});
