import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiClient,
  ApiError,
  StaleResponseError,
  validateEnvelope,
} from "../../src/arxiv_digest/web/static/api.mjs";

const TOKEN = "A".repeat(43);

function response(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return payload;
    },
  };
}

test("API client sends the token only as authorization to the local versioned API", async () => {
  const seen = [];
  const client = new ApiClient(
    "http://127.0.0.1:8765",
    TOKEN,
    async (url, options) => {
      seen.push({ url: String(url), options });
      return response(200, {
        api_version: "v1",
        ok: true,
        data: { ready: true },
      });
    },
  );
  assert.deepEqual(await client.json("status", "/api/v1/status"), { ready: true });
  assert.equal(seen[0].url, "http://127.0.0.1:8765/api/v1/status");
  assert.equal(seen[0].options.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(seen[0].options.credentials, "omit");
  assert.equal(seen[0].options.referrerPolicy, "no-referrer");
  assert.doesNotMatch(seen[0].url, new RegExp(TOKEN));
  assert.throws(
    () => client.json("bad", "https://example.test/api/v1/status"),
    /escaped the local origin/i,
  );
  assert.throws(() => client.json("bad", "/not-api"), /escaped the local origin/i);
});

test("envelope validation is exact", () => {
  assert.deepEqual(
    validateEnvelope({ api_version: "v1", ok: true, data: [1, 2] }),
    [1, 2],
  );
  for (const payload of [
    null,
    { data: {} },
    { api_version: "v2", ok: true, data: {} },
    { api_version: "v1", ok: false, data: {} },
    { api_version: "v1", ok: true, data: {}, surprise: true },
  ]) {
    assert.throws(() => validateEnvelope(payload), /response envelope/i);
  }
});

test("latest request wins even when a fetch implementation ignores abort", async () => {
  const pending = [];
  const client = new ApiClient("http://127.0.0.1:8765", TOKEN, (...args) => {
    return new Promise((resolve) => pending.push({ args, resolve }));
  });
  const first = client.json("review", "/api/v1/review/date?date=2026-01-01");
  const second = client.json("review", "/api/v1/review/date?date=2026-01-02");
  assert.equal(pending[0].args[1].signal.aborted, true);
  pending[1].resolve(
    response(200, { api_version: "v1", ok: true, data: { date: "new" } }),
  );
  assert.deepEqual(await second, { date: "new" });
  pending[0].resolve(
    response(200, { api_version: "v1", ok: true, data: { date: "old" } }),
  );
  await assert.rejects(first, StaleResponseError);
});

test("401 invokes authentication clearing and exposes a structured redacted error", async () => {
  let cleared = 0;
  const client = new ApiClient(
    "http://127.0.0.1:8765",
    TOKEN,
    async () =>
      response(401, {
        api_version: "v1",
        ok: false,
        error: { code: "authentication_required", message: "Session expired" },
      }),
    () => {
      cleared += 1;
    },
  );
  await assert.rejects(
    client.json("status", "/api/v1/status"),
    (error) =>
      error instanceof ApiError &&
      error.status === 401 &&
      error.code === "authentication_required",
  );
  assert.equal(cleared, 1);
});

test("detached browser fetch is invoked with the global object", async () => {
  function browserLikeFetch(_url, _options) {
    if (this !== globalThis) throw new TypeError("Illegal invocation");
    return Promise.resolve(
      response(200, { api_version: "v1", ok: true, data: { bound: true } }),
    );
  }
  const client = new ApiClient(
    "http://127.0.0.1:8765",
    TOKEN,
    browserLikeFetch,
  );
  assert.deepEqual(await client.json("status", "/api/v1/status"), {
    bound: true,
  });
});
