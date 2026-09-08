import assert from "node:assert/strict";
import test from "node:test";
import { UpdatePoller, DISCOVERY_DEADLINE_MS } from "../../src/arxiv_digest/web/static/update_poll.mjs";

const checking = {status: "checking", automatic_update: false};
const available = {
  status: "available_manual", automatic_update: false,
  installed_version: "0.2.1", available_version: "0.3.0",
};
const flush = () => new Promise((resolve) => setImmediate(resolve));

function harness(request = async () => checking, {defaultClock = false} = {}) {
  let time = 0;
  let nextId = 0;
  let canceled = 0;
  const timers = new Map();
  const requests = [];
  const rendered = [];
  const poller = new UpdatePoller({
    request: () => { requests.push(time); return request(time); },
    cancelRequest: () => { canceled++; },
    render: (value) => rendered.push(value),
    now: defaultClock ? undefined : () => time,
    setTimer: (callback, delay) => {
      const id = ++nextId;
      timers.set(id, {at: time + delay, callback});
      return id;
    },
    clearTimer: (id) => timers.delete(id),
  });
  return {
    poller, requests, rendered, timers,
    get canceled() { return canceled; },
    get time() { return time; },
    async advance(target) {
      await flush();
      for (;;) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > target) break;
        time = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await flush();
      }
      time = target;
      await flush();
    },
  };
}

for (const clockShift of [-3_600_000, 3_600_000]) {
  test(`wall-clock correction by ${clockShift}ms cannot change the observation deadline`, async (t) => {
    const h = harness(async (time) => time >= 30_000 ? available : checking, {defaultClock: true});
    let wall = 0;
    t.mock.method(performance, "now", () => h.time);
    t.mock.method(Date, "now", () => wall);
    h.poller.start();
    await h.advance(10_000);
    wall += clockShift;
    await h.advance(70_000);
    assert.deepEqual(h.rendered.at(-1), available);
    assert.equal(h.requests.at(-1), 30_000);
    assert.equal(h.timers.size, 0);
  });
}

for (const readyAt of [10_500, 60_000, 60_500]) {
  test(`observes a backend result at ${readyAt}ms and then stops`, async () => {
    const h = harness(async (time) => time >= readyAt ? available : checking);
    h.poller.start();
    await h.advance(70_000);
    assert.deepEqual(h.rendered.at(-1), available);
    assert.ok(h.requests.at(-1) >= readyAt);
    assert.ok(h.requests.at(-1) <= 61_000);
    assert.equal(h.timers.size, 0);
  });
}

test("pending responses stop after one final observation beyond the backend deadline", async () => {
  const h = harness();
  h.poller.start();
  await h.advance(120_000);
  assert.equal(DISCOVERY_DEADLINE_MS, 60_000);
  assert.equal(h.requests.at(-1), 61_000);
  assert.equal(h.rendered.at(-1).status, "manual_fallback");
  assert.equal(h.timers.size, 0);
});

test("transport failures retry within the original budget and end in manual guidance", async () => {
  const h = harness(async () => { throw new Error("offline"); });
  h.poller.start();
  await h.advance(120_000);
  assert.ok(h.requests.length > 1);
  assert.equal(h.requests.at(-1), 61_000);
  assert.equal(h.rendered.at(-1).status, "manual_fallback");
});

test("hung requests have finite timeouts, including the final response", async () => {
  const h = harness(() => new Promise(() => {}));
  h.poller.start();
  await h.advance(120_000);
  assert.equal(h.requests.at(-1), 61_000);
  assert.equal(h.rendered.at(-1).status, "manual_fallback");
  assert.equal(h.canceled, h.requests.length);
  assert.equal(h.timers.size, 0);
});

test("suspension preserves the deadline and a late response cannot overwrite resumed results", async () => {
  let resolveFirst;
  let calls = 0;
  const h = harness(() => ++calls === 1
    ? new Promise((resolve) => { resolveFirst = resolve; })
    : Promise.resolve(available));
  h.poller.start();
  await flush();
  h.poller.suspend();
  await h.advance(90_000);
  assert.equal(calls, 1);
  h.poller.resume();
  await flush();
  resolveFirst({status: "current"});
  await flush();
  assert.deepEqual(h.rendered, [available]);
  assert.equal(h.timers.size, 0);
});

test("resuming pending discovery after its deadline makes only one bounded final request", async () => {
  const h = harness();
  h.poller.start();
  await h.advance(10_000);
  h.poller.suspend();
  const before = h.requests.length;
  await h.advance(90_000);
  h.poller.resume();
  await h.advance(180_000);
  assert.equal(h.requests.length, before + 1);
  assert.equal(h.rendered.at(-1).status, "manual_fallback");
});

test("duplicate starts have one owner and permanent stop rejects late replies/resumption", async () => {
  let resolve;
  const h = harness(() => new Promise((done) => { resolve = done; }));
  h.poller.start();
  h.poller.start();
  await flush();
  assert.equal(h.requests.length, 1);
  h.poller.stop();
  resolve(available);
  await flush();
  h.poller.resume();
  await h.advance(120_000);
  assert.deepEqual(h.rendered, []);
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.size, 0);
});

test("terminal and malformed responses never start another poll", async () => {
  for (const value of [available, {status: "current"}, {status: "manual_fallback"}, {}]) {
    const h = harness(async () => value);
    h.poller.start();
    await h.advance(120_000);
    assert.equal(h.requests.length, 1);
    assert.equal(h.timers.size, 0);
  }
});

test("authentication expiry stops observation without retrying an expired session", async () => {
  const h = harness(async () => { throw Object.assign(new Error("expired"), {status: 401}); });
  h.poller.start();
  await h.advance(120_000);
  h.poller.resume();
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.size, 0);
});
