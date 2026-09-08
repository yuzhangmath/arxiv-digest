import assert from "node:assert/strict";
import test from "node:test";
import { TransitionStore, validMarker, validBroadcast, MARKER_KEY, MARKER_LIFETIME_MS } from "../../src/arxiv_digest/web/static/update_transition.mjs";
import { memoryStorage } from "./dom_test_helper.mjs";

const nonce = "fixture_startup_nonce";
const a = "a".repeat(64);
const b = "b".repeat(64);

test("markers are closed, bounded, nonsecret and expire in 24 hours", () => {
  const storage = memoryStorage();
  let now = 1000;
  const store = new TransitionStore({storage, now: () => now});
  assert.equal(store.write(nonce, a), true);
  const marker = JSON.parse(storage.getItem(MARKER_KEY));
  assert.deepEqual(marker, {schema_version: 1, startup_nonce: nonce, job_id: a, expires_at: now + MARKER_LIFETIME_MS});
  assert.equal(validMarker({...marker, token: "secret"}, now), false);
  assert.equal(validMarker({...marker, expires_at: now + MARKER_LIFETIME_MS + 1}, now), false);
  now += MARKER_LIFETIME_MS;
  assert.equal(store.read(), null);
});

test("compare-and-clear cannot remove another attempt or another startup", () => {
  const storage = memoryStorage();
  const store = new TransitionStore({storage, now: () => 1000});
  store.write(nonce, b);
  assert.equal(store.clear(nonce, a), false);
  assert.equal(store.clear("different_startup_nonce", b), false);
  assert.equal(store.read().job_id, b);
  assert.equal(store.clear(nonce, b), true);
  assert.equal(store.read(), null);
});

test("malformed storage and denied reads writes clears are harmless", () => {
  for (const storage of [
    memoryStorage({[MARKER_KEY]: "[malformed"}),
    memoryStorage({[MARKER_KEY]: "x".repeat(4096)}),
    {getItem() {throw Error();}, setItem() {throw Error();}, removeItem() {throw Error();}},
  ]) {
    const store = new TransitionStore({storage, now: () => 1000});
    assert.equal(store.read(), null);
    assert.doesNotThrow(() => store.write(nonce, a));
    assert.doesNotThrow(() => store.clear(nonce, a));
  }
});

test("broadcasts require exact known type and both canonical identities", () => {
  const message = {schema_version: 1, type: "update_restarting", startup_nonce: nonce, job_id: a};
  assert.equal(validBroadcast(message), true);
  for (const value of [{...message, token: "x"}, {...message, job_id: a.toUpperCase()}, {...message, type: "updated"}, {...message, startup_nonce: "short"}]) {
    assert.equal(validBroadcast(value), false);
  }
});
