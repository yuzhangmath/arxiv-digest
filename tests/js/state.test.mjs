import assert from "node:assert/strict";
import test from "node:test";

import {
  ALLOWED_VIEWS,
  TOKEN_STORAGE_KEY,
  ViewState,
  bootstrapSession,
  clearSession,
} from "../../src/arxiv_digest/web/static/state.mjs";
import { memoryStorage } from "./dom_test_helper.mjs";

const TOKEN_A = "A".repeat(43);
const TOKEN_B = "b".repeat(43);

function browserState(url, stored = {}) {
  const location = new URL(url);
  const calls = [];
  const history = {
    state: null,
    replaceState(state, _unused, next) {
      this.state = state;
      calls.push({ state, next });
    },
  };
  return { location, history, storage: memoryStorage(stored), calls };
}

test("fragment token is tab-scoped and stripped while independently validated view survives", () => {
  const browser = browserState(
    `http://127.0.0.1:8765/#token=${TOKEN_A}&view=library`,
  );
  const result = bootstrapSession({
    location: browser.location,
    history: browser.history,
    sessionStorage: browser.storage,
  });

  assert.deepEqual(result, { token: TOKEN_A, view: "library" });
  assert.equal(browser.storage.getItem(TOKEN_STORAGE_KEY), TOKEN_A);
  assert.deepEqual(browser.calls, [
    {
      state: { view: "library" },
      next: "/?view=library",
    },
  ]);
  assert.doesNotMatch(browser.calls[0].next, /token|#/);
});

test("same-tab reload recovers authentication and every fixed launch view", () => {
  assert.deepEqual(
    [...ALLOWED_VIEWS],
    ["setup", "review", "calendar", "library", "interests", "settings"],
  );
  for (const view of ALLOWED_VIEWS) {
    const browser = browserState(`http://127.0.0.1:8765/?view=${view}`, {
      [TOKEN_STORAGE_KEY]: TOKEN_A,
    });
    assert.deepEqual(
      bootstrapSession({
        location: browser.location,
        history: browser.history,
        sessionStorage: browser.storage,
      }),
      { token: TOKEN_A, view },
    );
  }
});

test("a new valid fragment replaces token and invalid view cannot select a route", () => {
  const browser = browserState(
    `http://127.0.0.1:8765/?view=settings#token=${TOKEN_B}&view=javascript:alert(1)`,
    { [TOKEN_STORAGE_KEY]: TOKEN_A },
  );
  const result = bootstrapSession({
    location: browser.location,
    history: browser.history,
    sessionStorage: browser.storage,
    defaultView: "setup",
  });
  assert.deepEqual(result, { token: TOKEN_B, view: "setup" });
  assert.equal(browser.calls[0].next, "/?view=setup");
});

test("malformed fragment tokens are rejected and explicit Quit clears the tab", () => {
  const browser = browserState(
    "http://127.0.0.1:8765/#token=too-short&view=review",
    { [TOKEN_STORAGE_KEY]: TOKEN_A },
  );
  assert.throws(
    () =>
      bootstrapSession({
        location: browser.location,
        history: browser.history,
        sessionStorage: browser.storage,
      }),
    /session token/i,
  );
  assert.equal(browser.storage.getItem(TOKEN_STORAGE_KEY), null);
  browser.storage.setItem(TOKEN_STORAGE_KEY, TOKEN_A);
  clearSession(browser.storage);
  assert.equal(browser.storage.getItem(TOKEN_STORAGE_KEY), null);
});

test("immutable view state ignores a response older than the current request", () => {
  const state = new ViewState("review", { page: 1 });
  const older = state.begin("review-page");
  const newer = state.begin("review-page");

  assert.equal(state.commit(older, { page: 2 }), false);
  assert.deepEqual(state.snapshot, { view: "review", page: 1 });
  assert.equal(state.commit(newer, { page: 3 }), true);
  assert.deepEqual(state.snapshot, { view: "review", page: 3 });
  assert.throws(() => {
    state.snapshot.page = 4;
  }, TypeError);
});
