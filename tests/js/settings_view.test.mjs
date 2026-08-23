import assert from "node:assert/strict";
import test from "node:test";

import {
  BACKUP_UPLOAD_LIMIT,
  SettingsController,
  renderRestoreError,
  renderRestoreInspection,
  renderSettingsView,
} from "../../src/arxiv_digest/web/static/settings_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

test("Open folder never accepts or sends a request path", async () => {
  const calls = [];
  const api = {
    origin: "http://127.0.0.1:8123",
    token: "secret-token",
    async json(key, path, options) {
      calls.push({ key, path, options });
      return { opened: true };
    },
  };
  const controller = new SettingsController(api);
  assert.equal(controller.openFolder.length, 0);
  assert.deepEqual(await controller.openFolder(), { opened: true });
  assert.deepEqual(calls, [
    {
      key: "settings-folder-open",
      path: "/api/v1/settings/folder/open",
      options: { method: "POST" },
    },
  ]);
  assert.doesNotMatch(JSON.stringify(calls), /\/Users\/|\/home\//i);
});

test("browser backup export uses authenticated fetch and a Blob download without an output path", async () => {
  const fetchCalls = [];
  const anchors = [];
  const document = {
    createElement(tag) {
      const node = new FakeNode(tag);
      if (tag === "a") {
        node.click = () => anchors.push(node);
      }
      return node;
    },
  };
  const revoked = [];
  const controller = new SettingsController(
    {
      origin: "http://127.0.0.1:8123",
      token: "secret-token",
      json() {
        throw new Error("binary export must not use the JSON decoder");
      },
    },
    {
      document,
      urlApi: {
        createObjectURL(blob) {
          assert.equal(blob instanceof Blob, true);
          return "blob:local-export";
        },
        revokeObjectURL(url) {
          revoked.push(url);
        },
      },
      async fetchImpl(url, options) {
        fetchCalls.push({ url: String(url), options });
        return {
          ok: true,
          status: 200,
          headers: { get: (name) => name.toLowerCase() === "content-type" ? "application/zip" : null },
          blob: async () => new Blob(["portable backup"], { type: "application/zip" }),
        };
      },
    },
  );

  await controller.downloadBackup();

  assert.equal(fetchCalls.length, 1);
  assert.equal(fetchCalls[0].url, "http://127.0.0.1:8123/api/v1/backup/export");
  assert.deepEqual(fetchCalls[0].options, {
    method: "GET",
    headers: { Authorization: "Bearer secret-token" },
    credentials: "omit",
    referrerPolicy: "no-referrer",
  });
  assert.equal(Object.hasOwn(fetchCalls[0].options, "body"), false);
  assert.equal(anchors.length, 1);
  assert.equal(anchors[0].getAttribute("href"), "blob:local-export");
  assert.equal(anchors[0].getAttribute("download"), "arxiv-digest-backup.zip");
  assert.deepEqual(revoked, ["blob:local-export"]);
});

test("default browser fetch stays bound to globalThis during backup export", async () => {
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = async function (_url, _options) {
    assert.equal(this, globalThis);
    called = true;
    return {
      ok: true,
      headers: { get: () => "application/zip" },
      blob: async () => new Blob(["backup"]),
    };
  };
  try {
    const controller = new SettingsController(
      {
        origin: "http://127.0.0.1:8123",
        token: "secret-token",
        json() {},
      },
      {
        document: {
          createElement() {
            const anchor = new FakeNode("a");
            anchor.click = () => {};
            return anchor;
          },
        },
        urlApi: {
          createObjectURL: () => "blob:bound-fetch",
          revokeObjectURL() {},
        },
      },
    );
    await controller.downloadBackup();
    assert.equal(called, true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("backup export authentication rejection clears the local session", async () => {
  let authenticationRejections = 0;
  const controller = new SettingsController(
    {
      origin: "http://127.0.0.1:8123",
      token: "expired-token",
      json() {},
      onAuthenticationRejected() {
        authenticationRejections += 1;
      },
    },
    {
      document: { createElement: () => new FakeNode("a") },
      urlApi: {
        createObjectURL: () => "blob:unused",
        revokeObjectURL() {},
      },
      async fetchImpl() {
        return { ok: false, status: 401 };
      },
    },
  );

  await assert.rejects(() => controller.downloadBackup(), /failed/i);

  assert.equal(authenticationRejections, 1);
});

test("restore is inspect-first, size-bounded, and reconfirms destination and pre-restore backup", async () => {
  const calls = [];
  let failRestore = true;
  const api = {
    origin: "http://127.0.0.1:8123",
    token: "secret-token",
    async json(key, path, options) {
      calls.push({ key, path, options });
      if (path.endsWith("/inspect")) {
        return {
          pending_restore_id: "pending_restore_123",
          summary: { saved_papers: 7 },
        };
      }
      if (path.endsWith("/restore") && failRestore) {
        failRestore = false;
        throw new Error("restore fixture failed");
      }
      return { restored: true, pre_restore_backup_created: true };
    },
  };
  const controller = new SettingsController(api);
  await assert.rejects(
    () => controller.inspectBackup({ size: BACKUP_UPLOAD_LIMIT + 1 }),
    /size/i,
  );
  assert.equal(calls.length, 0);

  const archive = new Blob(["fixture zip"], { type: "application/zip" });
  const inspection = await controller.inspectBackup(archive);
  assert.equal(calls[0].path, "/api/v1/backup/inspect");
  assert.equal(calls[0].options.body, archive);
  assert.equal(calls[0].options.headers["Content-Type"], "application/zip");
  assert.equal(calls.some((call) => call.path.endsWith("/restore")), false);

  assert.throws(
    () => controller.restoreBackup(inspection.pending_restore_id, { cancelActive: false, confirmedPreRestoreBackup: true }),
    /destination/i,
  );
  controller.reconfirmRestoreDestination(
    inspection.pending_restore_id,
    "documents",
  );
  assert.throws(
    () => controller.restoreBackup(inspection.pending_restore_id, { cancelActive: false, confirmedPreRestoreBackup: false }),
    /pre-restore backup/i,
  );

  await assert.rejects(
    () => controller.restoreBackup(inspection.pending_restore_id, { cancelActive: true, confirmedPreRestoreBackup: true }),
    /fixture failed/,
  );
  const restored = await controller.restoreBackup(inspection.pending_restore_id, {
    cancelActive: true,
    confirmedPreRestoreBackup: true,
  });
  assert.deepEqual(restored, {
    restored: true,
    pre_restore_backup_created: true,
  });
  const restoreBodies = calls
    .filter((call) => call.path.endsWith("/restore"))
    .map((call) => JSON.parse(call.options.body));
  assert.deepEqual(restoreBodies, [
    {
      pending_restore_id: "pending_restore_123",
      destination_choice: "documents",
      cancel_active: true,
    },
    {
      pending_restore_id: "pending_restore_123",
      destination_choice: "documents",
      cancel_active: true,
    },
  ]);
});

test("settings actions use the exact scoped API routes and require destructive confirmation", async () => {
  const calls = [];
  const api = {
    origin: "http://127.0.0.1:8123",
    token: "secret-token",
    async json(key, path, options) {
      calls.push({ key, path, options });
      if (path.endsWith("/folder/test")) {
        return { tested_destination_token: "destination_12345678" };
      }
      return {};
    },
  };
  const controller = new SettingsController(api);
  await controller.testFolder("downloads");
  await controller.saveTestedFolder(7);
  await controller.extendCoverage("math.AG", "2026-07-01");
  assert.throws(() => controller.clearCache(false), /confirm/i);
  await controller.clearCache(true);
  await controller.doctor();
  await controller.launcherStatus();
  await controller.createLauncher();
  await controller.notNowLauncher();
  await controller.removeLauncher();
  await controller.quit();

  assert.deepEqual(calls.map((call) => call.path), [
    "/api/v1/settings/folder/test",
    "/api/v1/settings/folder",
    "/api/v1/settings/coverage",
    "/api/v1/settings/cache/clear",
    "/api/v1/settings/doctor",
    "/api/v1/settings/launcher",
    "/api/v1/settings/launcher/create",
    "/api/v1/settings/launcher/not-now",
    "/api/v1/settings/launcher/remove",
    "/api/v1/application/quit",
  ]);
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    destination_choice: "downloads",
  });
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    expected_revision: 7,
    tested_destination_token: "destination_12345678",
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    category: "math.AG",
    new_start: "2026-07-01",
  });
  assert.equal(calls[3].options.body, undefined);
});

test("settings separates sync states and exposes safe folder, backup, doctor, launcher, cache, and Quit controls", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      revision: 7,
      online: false,
      pdf_destination: { kind: "documents", writable: true },
      categories: [
        {
          category: "math.AG",
          metadata_synchronized_through: "2026-08-21",
          coverage_start: "2026-07-01",
          historical_backfill: { status: "interrupted", pending_start: "2026-06-01" },
          exact_enrichment: {
            start: "2026-08-01",
            end: "2026-08-21",
            holes: ["2026-08-12"],
          },
          current_sync: { status: "failed", error_code: "offline" },
        },
      ],
      doctor: {
        application_version: "0.1.0",
        database_status: "ok",
        category_count: 1,
        saved_paper_count: 12,
        destination_kind: "documents",
        secret_path: ["", "Users", "private", "papers"].join("/"),
      },
      launcher: {
        installed: false,
        operation: "create_failed",
        error_code: "launcher_install_failed",
      },
    },
    {
      openFolder: () => calls.push("open-folder"),
      testFolder: () => calls.push("test-folder"),
      exportBackup: () => calls.push("export"),
      inspectBackup: () => calls.push("inspect"),
      clearCache: () => calls.push("clear-cache"),
      retryLauncher: () => calls.push("retry-launcher"),
      notNowLauncher: () => calls.push("not-now"),
      quit: () => calls.push("quit"),
    },
  );

  assert.match(root.textContent, /Synchronization offline/i);
  assert.match(root.textContent, /cached Review and Library remain available/i);
  assert.match(root.textContent, /Metadata synchronized through 2026-08-21/);
  assert.match(root.textContent, /Historical coverage backfill: interrupted/i);
  assert.match(root.textContent, /current metadata remains synchronized/i);
  assert.match(root.textContent, /Exact announcement enrichment 2026-08-01 through 2026-08-21/);
  assert.match(root.textContent, /Missing exact dates: 2026-08-12/);
  assert.match(root.textContent, /math.AG synchronization failed/i);
  assert.doesNotMatch(root.textContent, /all categories synchronized/i);
  assert.doesNotMatch(root.textContent, /\/Users\/private/);
  assert.match(root.textContent, /This does not start arXiv Digest in the background/i);
  assert.match(root.textContent, /reopened with the arxiv-digest command or desktop launcher/i);

  findButton(root, "Open folder").click();
  findButton(root, "Test download").click();
  findButton(root, "Export backup").click();
  findButton(root, "Inspect backup").click();
  findButton(root, "Delete cache").click();
  assert.equal(calls.includes("clear-cache"), false);
  findButton(root, "Confirm delete cache").click();
  findButton(root, "Retry launcher").click();
  findButton(root, "Not now").click();
  findButton(root, "Quit arXiv Digest").click();
  assert.deepEqual(calls, [
    "open-folder",
    "test-folder",
    "export",
    "inspect",
    "clear-cache",
    "retry-launcher",
    "not-now",
    "quit",
  ]);
  const progress = descendants(root, "div").find(
    (node) => node.getAttribute("role") === "status",
  );
  assert.equal(progress.getAttribute("aria-live"), "polite");
});

test("folder testing follows the selected standard or server-issued picker choice", () => {
  const root = new FakeNode("main");
  const tested = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      revision: 7,
      pdf_destination: { kind: "downloads", writable: true },
      picker_choice: "picker_12345678",
      categories: [],
      doctor: {},
      launcher: {},
    },
    { testFolder: (choice) => tested.push(choice) },
  );
  const inputs = descendants(root, "input");
  const documents = inputs.find(
    (node) => node.getAttribute("value") === "documents",
  );
  documents.checked = true;
  documents.dispatchEvent({ type: "change" });
  findButton(root, "Test download").click();

  const picker = inputs.find(
    (node) => node.getAttribute("value") === "picker_12345678",
  );
  assert.ok(picker, "the opaque picker result must be selectable");
  assert.equal(
    inputs.some((node) => node.getAttribute("value") === "picker"),
    false,
  );
  picker.checked = true;
  picker.dispatchEvent({ type: "change" });
  findButton(root, "Test download").click();
  assert.deepEqual(tested, ["documents", "picker_12345678"]);
});

test("restore inspection is non-mutating and requires fresh destination and pre-backup confirmation", () => {
  const document = new FakeDocument();
  const root = new FakeNode("section");
  const calls = [];
  renderRestoreInspection(
    document,
    root,
    {
      pending_restore_id: "pending_restore_123",
      summary: {
        categories: 2,
        saved_papers: 9,
        hidden_path: ["", "Users", "private", "archive.zip"].join("/"),
      },
    },
    {
      confirmDestination: (...args) => calls.push(["destination", ...args]),
      restore: (...args) => calls.push(["restore", ...args]),
    },
  );
  assert.match(root.textContent, /Inspection made no changes/i);
  assert.match(root.textContent, /pre-restore backup/i);
  assert.doesNotMatch(root.textContent, /\/Users\/private/);
  const restore = findButton(root, "Restore backup");
  assert.equal(restore.disabled, true);

  const documentChoice = descendants(root, "input").find(
    (node) => node.getAttribute("value") === "documents",
  );
  documentChoice.checked = true;
  documentChoice.dispatchEvent({ type: "change" });
  const preBackup = descendants(root, "input").find(
    (node) => node.getAttribute("name") === "confirm-pre-restore-backup",
  );
  preBackup.checked = true;
  preBackup.dispatchEvent({ type: "change" });
  assert.equal(restore.disabled, false);
  restore.click();
  assert.deepEqual(calls, [
    ["destination", "pending_restore_123", "documents"],
    [
      "restore",
      "pending_restore_123",
      { cancelActive: false, confirmedPreRestoreBackup: true },
    ],
  ]);

  renderRestoreError(document, root, "Restore failed safely.", () => calls.push("retry"));
  assert.match(root.textContent, /Inspection made no changes/i);
  findButton(root, "Retry restore").click();
  assert.equal(calls.at(-1), "retry");
});
