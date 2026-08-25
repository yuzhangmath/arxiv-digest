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

test("Settings retry starts synchronization through the scoped API", async () => {
  const calls = [];
  const controller = new SettingsController({
    async json(key, path, options) {
      calls.push({ key, path, options });
      return { job_id: "sync_retry_1234" };
    },
  });

  assert.deepEqual(await controller.retrySynchronization(), {
    job_id: "sync_retry_1234",
  });
  assert.deepEqual(calls, [{
    key: "settings-sync-start",
    path: "/api/v1/sync/start",
    options: {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: '{"retry_failed_dates":true}',
    },
  }]);
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
  await controller.extendCoverage("math.AG", "2026-07-01", 7);
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
    expected_revision: 7,
  });
  assert.equal(calls[3].options.body, undefined);
});

test("settings keeps canonical version-resolution accounting internal", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      revision: 7,
      online: false,
      coverage_min: "2026-05-25",
      coverage_max: "2026-08-22",
      pdf_destination: { kind: "documents", writable: true },
      metadata_sync: {
        checkpoint_count: 1,
        categories: [{
          category: "math.AG",
          status: "failed",
          synchronized_through: "2026-08-21",
          error_codes: ["sync_error"],
        }],
      },
      daily_list_coverage: {
        target: 24,
        checked: 20,
        with_papers: 12,
        empty: 7,
        failed: 1,
        pending: 2,
        unavailable: 1,
        gaps: 4,
        categories: [{
          category: "math.AG",
          coverage_start: "2026-07-01",
          target: 24,
          checked: 20,
          with_papers: 12,
          empty: 7,
          failed: 1,
          pending: 2,
          unavailable: 1,
          gaps: 4,
          error_codes: ["catchup_layout_changed"],
          retryable_failed_dates: ["2026-08-12"],
        }],
      },
      version_resolution: {
        canonical_event_count: 20,
        atom_confirmed: 8,
        chronology_matched: 9,
        unconfirmed: 3,
      },
      candidate_cache: { status: "ready", file_count: 3 },
      library: { saved_paper_count: 12 },
      pdf_presence: { downloaded_pdf_count: 5 },
      doctor: {
        application_version: "0.2.0",
        database_status: "ok",
        application_generation: 2,
        schema_version: 4,
        profile_revision: 7,
        projection_revision: 3,
        active_category_count: 1,
        metadata_checkpoint_count: 1,
        daily_list_target_count: 24,
        daily_list_gap_count: 4,
        canonical_event_count: 20,
        atom_confirmed_count: 8,
        chronology_matched_count: 9,
        unconfirmed_count: 3,
        saved_paper_count: 12,
        downloaded_pdf_count: 5,
        candidate_cache_status: "ready",
        maintenance_state: "idle",
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
      exportBackup: () => calls.push("export"),
      inspectBackup: () => calls.push("inspect"),
      clearCache: () => calls.push("clear-cache"),
      retryLauncher: () => calls.push("retry-launcher"),
      notNowLauncher: () => calls.push("not-now"),
      quit: () => calls.push("quit"),
    },
  );

  assert.match(root.textContent, /Metadata synchronization/i);
  assert.match(root.textContent, /Historical daily-list coverage/i);
  assert.doesNotMatch(root.textContent, /Canonical-event version resolution/i);
  assert.doesNotMatch(root.textContent, /Canonical events/i);
  assert.doesNotMatch(root.textContent, /Atom-confirmed/i);
  assert.doesNotMatch(root.textContent, /Chronology-matched/i);
  assert.doesNotMatch(root.textContent, /Unconfirmed/i);
  assert.match(root.textContent, /Synchronization offline/i);
  assert.match(root.textContent, /cached Review and Library remain available/i);
  assert.match(root.textContent, /Metadata synchronized through 2026-08-21/);
  assert.match(root.textContent, /Target dates: 24/i);
  assert.match(root.textContent, /12 with papers/i);
  assert.match(root.textContent, /7 empty/i);
  assert.match(root.textContent, /1 failed/i);
  assert.match(root.textContent, /2 pending/i);
  assert.match(root.textContent, /1 unavailable/i);
  assert.match(root.textContent, /catchup_layout_changed/);
  assert.match(root.textContent, /Candidate cache: ready/i);
  assert.match(root.textContent, /Saved Library papers: 12/i);
  assert.match(root.textContent, /Downloaded PDFs present: 5/i);
  assert.doesNotMatch(root.textContent, /current daily feed/i);
  assert.doesNotMatch(root.textContent, /inferred from version history/i);
  assert.doesNotMatch(root.textContent, /submission dates/i);
  assert.doesNotMatch(root.textContent, /fallback event/i);
  assert.doesNotMatch(root.textContent, /\/Users\/private/);
  assert.doesNotMatch(root.textContent, /Downloads \/ Arxiv Digest|Documents \/ Arxiv Digest/);
  assert.match(root.textContent, /Current PDF folder is configured/i);
  assert.match(
    root.textContent,
    /portable ZIP containing your interests, paper metadata, synchronization history, review progress, and saved Library papers/i,
  );
  assert.match(
    root.textContent,
    /does not include downloaded PDFs, suggestion cache data, or your machine-specific PDF folder/i,
  );
  assert.match(root.textContent, /Restore replaces your current local data with the backup/i);
  assert.match(root.textContent, /creates a recovery backup of your current data/i);
  assert.doesNotMatch(root.textContent, /Destination kind/);
  assert.match(root.textContent, /Candidate cache/);
  assert.doesNotMatch(root.textContent, /Disposable cache/);
  assert.match(
    root.textContent,
    /temporary recent-paper sample used to build setup and Interests suggestions/i,
  );
  assert.match(root.textContent, /download and rebuild it when you refresh suggestions/i);
  assert.match(
    root.textContent,
    /Interests, synchronization checkpoints, review progress, Library papers, and downloaded PDFs are not removed/i,
  );
  assert.match(root.textContent, /This does not start arXiv Digest in the background/i);
  assert.match(root.textContent, /reopened with the arxiv-digest command or desktop launcher/i);
  findButton(root, "Choose PDF folder");

  findButton(root, "Open folder").click();
  findButton(root, "Export backup").click();
  findButton(root, "Inspect backup").click();
  findButton(root, "Delete suggestion cache").click();
  assert.equal(calls.includes("clear-cache"), false);
  findButton(root, "Confirm delete suggestion cache").click();
  findButton(root, "Retry launcher").click();
  findButton(root, "Not now").click();
  findButton(root, "Quit arXiv Digest").click();
  assert.deepEqual(calls, [
    "open-folder",
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

test("settings shows and tests only the server-issued picker choice", () => {
  const root = new FakeNode("main");
  const tested = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      revision: 7,
      pdf_destination: {
        kind: "custom",
        writable: true,
        display_path: "~/Documents/Research PDFs",
      },
      picker_choice: "picker_12345678",
      picker_display_name: "Research PDFs",
      categories: [],
      doctor: {},
      launcher: {},
    },
    { testFolder: (choice) => tested.push(choice) },
  );
  const inputs = descendants(root, "input");
  assert.equal(inputs.some((node) => node.getAttribute("type") === "radio"), false);
  assert.doesNotMatch(root.textContent, /Downloads \/ Arxiv Digest|Documents \/ Arxiv Digest/);
  assert.match(root.textContent, /Current PDF folder: ~\/Documents\/Research PDFs/);
  assert.match(root.textContent, /Selected folder: Research PDFs/);
  findButton(root, "Choose PDF folder");
  findButton(root, "Test and use folder").click();
  assert.deepEqual(tested, ["picker_12345678"]);
});

test("settings reveals standard fallbacks only when the native picker is unavailable", () => {
  const root = new FakeNode("main");
  const tested = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      revision: 7,
      pdf_destination: { kind: "custom", writable: true },
      picker_unavailable: true,
      categories: [],
      doctor: {},
      launcher: {},
    },
    { testFolder: (choice) => tested.push(choice) },
  );

  assert.match(root.textContent, /native folder picker is unavailable/i);
  findButton(root, "Test and use Downloads fallback").click();
  findButton(root, "Test and use Documents fallback").click();
  assert.deepEqual(tested, ["downloads", "documents"]);
});

test("settings coverage extension is bounded to the server-issued recovery window", () => {
  const root = new FakeNode("main");
  const extensions = [];
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      coverage_min: "2026-05-25",
      coverage_max: "2026-08-22",
      pdf_destination: { kind: "custom" },
      daily_list_coverage: {
        categories: [{
          category: "math.AG",
          coverage_start: "2026-07-01",
          retryable_failed_dates: [],
        }],
      },
      doctor: {},
      launcher: {},
    },
    { extendCoverage: (...values) => extensions.push(values) },
  );

  const input = descendants(root, "input").find(
    (node) => node.getAttribute("aria-label") === "New coverage start for math.AG",
  );
  const extend = findButton(root, "Extend math.AG coverage");
  assert.equal(input.getAttribute("min"), "2026-05-25");
  assert.equal(input.getAttribute("max"), "2026-08-22");
  input.value = "2026-05-24";
  input.dispatchEvent({ type: "input" });
  assert.equal(extend.disabled, true);
  input.value = "2026-06-01";
  input.dispatchEvent({ type: "input" });
  assert.equal(extend.disabled, false);
  extend.click();
  assert.deepEqual(extensions, [["math.AG", "2026-06-01"]]);
});

test("settings reports a zero confirmed daily-list target without fallback claims", () => {
  const root = new FakeNode("main");
  renderSettingsView(new FakeDocument(), root, {
    pdf_destination: { kind: "custom" },
    daily_list_coverage: {
      target: 0, checked: 0, with_papers: 0, empty: 0,
      failed: 0, pending: 0, unavailable: 0, categories: [],
    },
    doctor: {},
    launcher: {},
  });

  assert.match(root.textContent, /Target dates: 0/);
  assert.doesNotMatch(root.textContent, /submission date|inferred|fallback/i);
});

test("settings identifies pending confirmed daily-list work as still running", () => {
  const root = new FakeNode("main");
  renderSettingsView(new FakeDocument(), root, {
    online: true,
    synchronizing: true,
    pdf_destination: { kind: "custom" },
    daily_list_coverage: {
      target: 2, checked: 0, with_papers: 0, empty: 0,
      failed: 0, pending: 2, unavailable: 0, categories: [],
    },
    doctor: {},
    launcher: {},
  });

  assert.match(root.textContent, /Metadata synchronization is running/);
  assert.match(root.textContent, /2 pending/);
  assert.doesNotMatch(root.textContent, /submission date|inferred|fallback/i);
});

test("settings keeps permanently unavailable daily-list gaps explicit", () => {
  const root = new FakeNode("main");
  renderSettingsView(new FakeDocument(), root, {
    online: true,
    synchronizing: false,
    pdf_destination: { kind: "custom" },
    daily_list_coverage: {
      target: 2, checked: 0, with_papers: 0, empty: 0,
      failed: 0, pending: 0, unavailable: 2, categories: [],
    },
    doctor: {},
    launcher: {},
  });

  assert.match(root.textContent, /2 unavailable/);
  assert.doesNotMatch(root.textContent, /submission date|inferred|fallback/i);
});

test("settings counts failed daily-list dates and offers an idle retry", () => {
  const root = new FakeNode("main");
  let retries = 0;
  renderSettingsView(
    new FakeDocument(),
    root,
    {
      online: true,
      synchronizing: false,
      pdf_destination: { kind: "custom" },
      daily_list_coverage: {
        target: 4, checked: 3, with_papers: 0, empty: 0,
        failed: 3, pending: 0, unavailable: 1,
        categories: [{
          category: "math.AT",
          coverage_start: "2026-07-01",
          target: 4, checked: 3, with_papers: 0, empty: 0,
          failed: 3, pending: 0, unavailable: 1,
          retryable_failed_dates: ["2026-08-01", "2026-08-02"],
          error_codes: ["catchup_fetch_failed"],
        }],
      },
      doctor: {},
      launcher: {},
    },
    { retrySynchronization: () => { retries += 1; } },
  );

  assert.match(root.textContent, /3 failed/);
  assert.match(root.textContent, /1 unavailable/);
  assert.doesNotMatch(root.textContent, /2026-08-01/);
  findButton(root, "Retry 2 failed daily-list dates").click();
  assert.equal(retries, 1);
});

test("settings reports completed failed-date retries while syncing", () => {
  const root = new FakeNode("main");
  renderSettingsView(new FakeDocument(), root, {
    online: true,
    synchronizing: true,
    daily_list_retry: { status: "running", completed: 20, total: 32 },
    pdf_destination: { kind: "custom" },
    categories: [{
      category: "math.AT",
      historical_backfill: { status: "idle" },
      exact_enrichment: {
        holes: ["2026-08-01", "2026-08-02"],
        failed_dates: ["2026-08-01", "2026-08-02"],
        retryable_failed_dates: ["2026-08-01", "2026-08-02"],
      },
    }],
    doctor: {},
    launcher: {},
  });

  const retry = findButton(
    root,
    "Retrying failed daily-list dates… 20 of 32 completed",
  );
  const progress = descendants(root, "progress").find(
    (node) => node.getAttribute("aria-label") ===
      "Retrying failed daily-list dates: 20 of 32 completed",
  );
  assert.equal(retry.disabled, true);
  assert.ok(progress);
  assert.equal(progress.getAttribute("value"), "20");
  assert.equal(progress.getAttribute("max"), "32");
});

test("restore inspection requires a freshly picked destination and pre-backup confirmation", () => {
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
  assert.doesNotMatch(root.textContent, /Downloads \/ Arxiv Digest|Documents \/ Arxiv Digest/);
  findButton(root, "Choose PDF folder");
  const restore = findButton(root, "Restore backup");
  assert.equal(restore.disabled, true);

  renderRestoreInspection(
    document,
    root,
    {
      pending_restore_id: "pending_restore_123",
      picker_choice: "picker_12345678",
      picker_display_name: "Restored PDFs",
      summary: { categories: 2, saved_papers: 9 },
    },
    {
      confirmDestination: (...args) => calls.push(["destination", ...args]),
      restore: (...args) => calls.push(["restore", ...args]),
    },
  );
  assert.match(root.textContent, /Selected folder: Restored PDFs/);
  assert.doesNotMatch(root.textContent, /Downloads \/ Arxiv Digest|Documents \/ Arxiv Digest/);
  findButton(root, "Use this folder").click();
  const preBackup = descendants(root, "input").find(
    (node) => node.getAttribute("name") === "confirm-pre-restore-backup",
  );
  preBackup.checked = true;
  preBackup.dispatchEvent({ type: "change" });
  const confirmedRestore = findButton(root, "Restore backup");
  assert.equal(confirmedRestore.disabled, false);
  confirmedRestore.click();
  assert.deepEqual(calls, [
    ["destination", "pending_restore_123", "picker_12345678"],
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

test("restore offers standard fallbacks only when the native picker is unavailable", () => {
  const root = new FakeNode("section");
  const calls = [];
  renderRestoreInspection(
    new FakeDocument(),
    root,
    {
      pending_restore_id: "pending_restore_123",
      picker_unavailable: true,
      summary: { categories: 1 },
    },
    {
      confirmDestination: (...args) => calls.push(args),
    },
  );

  assert.match(root.textContent, /native folder picker is unavailable/i);
  findButton(root, "Use Downloads fallback").click();
  assert.deepEqual(calls, [["pending_restore_123", "downloads"]]);
});
