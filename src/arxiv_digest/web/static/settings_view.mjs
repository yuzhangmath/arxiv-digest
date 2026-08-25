export class SettingsController {
  constructor(api, dependencies = {}) {
    if (!api || typeof api.json !== "function") {
      throw new TypeError("SettingsController requires an API client");
    }
    this.api = api;
    this.dependencies = dependencies;
    this.pendingRestores = new Map();
    this.testedDestinationToken = null;
  }

  openFolder() {
    return this.api.json(
      "settings-folder-open",
      "/api/v1/settings/folder/open",
      { method: "POST" },
    );
  }

  pickFolder() {
    return this.api.json(
      "settings-folder-pick",
      "/api/v1/settings/folder/pick",
      { method: "POST" },
    );
  }

  async testFolder(destinationChoice) {
    assertDestinationChoice(destinationChoice);
    const result = await this.api.json(
      "settings-folder-test",
      "/api/v1/settings/folder/test",
      jsonPost({ destination_choice: destinationChoice }),
    );
    if (
      typeof result?.tested_destination_token !== "string" ||
      !/^destination_[A-Za-z0-9_-]{8,112}$/.test(result.tested_destination_token)
    ) {
      throw new TypeError("Folder test returned an invalid token");
    }
    this.testedDestinationToken = result.tested_destination_token;
    return result;
  }

  saveTestedFolder(expectedRevision) {
    if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) {
      throw new TypeError("Invalid settings revision");
    }
    if (!this.testedDestinationToken) throw new TypeError("Test the destination first");
    const token = this.testedDestinationToken;
    this.testedDestinationToken = null;
    return this.api.json(
      "settings-folder-save",
      "/api/v1/settings/folder",
      jsonPut({
        expected_revision: expectedRevision,
        tested_destination_token: token,
      }),
    );
  }

  extendCoverage(category, newStart, expectedRevision) {
    if (typeof category !== "string" || !category.trim()) {
      throw new TypeError("Invalid category");
    }
    assertIsoDate(newStart);
    if (!Number.isSafeInteger(expectedRevision) || expectedRevision < 1) {
      throw new TypeError("Invalid settings revision");
    }
    return this.api.json(
      "settings-coverage",
      "/api/v1/settings/coverage",
      jsonPut({
        category: category.trim(),
        new_start: newStart,
        expected_revision: expectedRevision,
      }),
    );
  }

  retrySynchronization() {
    return this.api.json(
      "settings-sync-start",
      "/api/v1/sync/start",
      jsonPost({ retry_failed_dates: true }),
    );
  }

  clearCache(confirmed) {
    if (confirmed !== true) throw new TypeError("Confirm cache deletion");
    return this.api.json(
      "settings-cache-clear",
      "/api/v1/settings/cache/clear",
      { method: "POST" },
    );
  }

  doctor() {
    return this.api.json("settings-doctor", "/api/v1/settings/doctor", undefined);
  }

  launcherStatus() {
    return this.api.json("settings-launcher", "/api/v1/settings/launcher", undefined);
  }

  createLauncher() {
    return this.api.json(
      "settings-launcher-create",
      "/api/v1/settings/launcher/create",
      { method: "POST" },
    );
  }

  notNowLauncher() {
    return this.api.json(
      "settings-launcher-not-now",
      "/api/v1/settings/launcher/not-now",
      { method: "POST" },
    );
  }

  removeLauncher() {
    return this.api.json(
      "settings-launcher-remove",
      "/api/v1/settings/launcher/remove",
      { method: "POST" },
    );
  }

  quit() {
    return this.api.json(
      "application-quit",
      "/api/v1/application/quit",
      { method: "POST" },
    );
  }

  async downloadBackup() {
    const fetchImpl = this.dependencies.fetchImpl ?? globalThis.fetch?.bind(globalThis);
    const documentImpl = this.dependencies.document ?? globalThis.document;
    const urlApi = this.dependencies.urlApi ?? globalThis.URL;
    if (
      typeof fetchImpl !== "function" ||
      !documentImpl?.createElement ||
      typeof urlApi?.createObjectURL !== "function" ||
      typeof urlApi?.revokeObjectURL !== "function"
    ) {
      throw new TypeError("Backup download is unavailable");
    }
    const origin = new URL(this.api.origin).origin;
    const url = new URL("/api/v1/backup/export", origin);
    const response = await fetchImpl(url, {
      method: "GET",
      headers: { Authorization: `Bearer ${this.api.token}` },
      credentials: "omit",
      referrerPolicy: "no-referrer",
    });
    if (!response.ok) {
      if (
        response.status === 401 &&
        typeof this.api.onAuthenticationRejected === "function"
      ) {
        this.api.onAuthenticationRejected();
      }
      throw new Error("Backup export failed");
    }
    const contentType = response.headers?.get?.("content-type") ?? "";
    if (contentType.split(";", 1)[0].trim().toLowerCase() !== "application/zip") {
      throw new TypeError("Backup export returned an invalid content type");
    }
    const blob = await response.blob();
    if (!(blob instanceof Blob) || blob.size > BACKUP_UPLOAD_LIMIT) {
      throw new TypeError("Backup export exceeded the allowed size");
    }
    const objectUrl = urlApi.createObjectURL(blob);
    try {
      const anchor = documentImpl.createElement("a");
      anchor.setAttribute("href", objectUrl);
      anchor.setAttribute("download", "arxiv-digest-backup.zip");
      anchor.click();
    } finally {
      urlApi.revokeObjectURL(objectUrl);
    }
  }

  async inspectBackup(archive) {
    if (
      !archive ||
      !Number.isSafeInteger(archive.size) ||
      archive.size < 1 ||
      archive.size > BACKUP_UPLOAD_LIMIT
    ) {
      throw new TypeError("Backup upload size is outside the route limit");
    }
    if (!(archive instanceof Blob)) throw new TypeError("Backup must be a ZIP Blob");
    const result = await this.api.json(
      "backup-inspect",
      "/api/v1/backup/inspect",
      {
        method: "POST",
        headers: { "Content-Type": "application/zip" },
        body: archive,
      },
    );
    assertPendingRestoreId(result?.pending_restore_id);
    this.pendingRestores.clear();
    this.pendingRestores.set(result.pending_restore_id, {
      inspection: result,
      destinationChoice: null,
    });
    return result;
  }

  reconfirmRestoreDestination(pendingRestoreId, destinationChoice) {
    assertPendingRestoreId(pendingRestoreId);
    assertDestinationChoice(destinationChoice);
    const pending = this.pendingRestores.get(pendingRestoreId);
    if (!pending) throw new TypeError("Restore inspection is missing or expired");
    pending.destinationChoice = destinationChoice;
  }

  restoreBackup(pendingRestoreId, options = {}) {
    assertPendingRestoreId(pendingRestoreId);
    const pending = this.pendingRestores.get(pendingRestoreId);
    if (!pending) throw new TypeError("Restore inspection is missing or expired");
    if (!pending.destinationChoice) {
      throw new TypeError("PDF destination must be reconfirmed after inspection");
    }
    if (options.confirmedPreRestoreBackup !== true) {
      throw new TypeError("Confirm that a pre-restore backup will be created");
    }
    if (typeof options.cancelActive !== "boolean") {
      throw new TypeError("Choose whether active work may be cancelled");
    }
    const request = this.api.json(
      "backup-restore",
      "/api/v1/backup/restore",
      jsonPost({
        pending_restore_id: pendingRestoreId,
        destination_choice: pending.destinationChoice,
        cancel_active: options.cancelActive,
      }),
    );
    return Promise.resolve(request).then((result) => {
      this.pendingRestores.delete(pendingRestoreId);
      return result;
    });
  }
}

export const BACKUP_UPLOAD_LIMIT = 64 * 1024 * 1024;

const SERVER_ID = /^[A-Za-z0-9_-]{8,128}$/;
const PICKER_ID = /^picker_[A-Za-z0-9_-]{8,120}$/;

function assertPendingRestoreId(value) {
  if (typeof value !== "string" || !SERVER_ID.test(value)) {
    throw new TypeError("Invalid pending restore ID");
  }
}

function assertDestinationChoice(value) {
  if (value !== "downloads" && value !== "documents" && !PICKER_ID.test(value)) {
    throw new TypeError("Invalid PDF destination choice");
  }
}

function jsonPost(value) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function jsonPut(value) {
  return {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function assertIsoDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new TypeError("Expected an ISO date");
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new TypeError("Expected an ISO date");
  }
}

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(document, label, action) {
  const node = element(document, "button", label);
  node.setAttribute("type", "button");
  node.addEventListener("click", action);
  return node;
}

function renderSyncSettings(document, model, actions) {
  const wrapper = element(document, "div", undefined, "settings-sync-sections");
  const online = model.online !== false;
  const synchronizing = model.synchronizing === true;
  const number = (value) => Number.isSafeInteger(value) && value >= 0 ? value : 0;
  const safeCodes = (values) => Array.isArray(values)
    ? values.filter((value) =>
      typeof value === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(value))
    : [];

  const metadata = element(document, "section", undefined, "settings-section");
  metadata.append(
    element(document, "h2", "Metadata synchronization"),
    element(
      document,
      "p",
      synchronizing
        ? "Metadata synchronization is running."
        : online
        ? "Metadata synchronization is online."
        : "Synchronization offline. Cached Review and Library remain available.",
      online ? "sync-online" : "sync-offline",
    ),
    element(
      document,
      "p",
      `Metadata checkpoints: ${number(model.metadata_sync?.checkpoint_count)}.`,
    ),
  );
  for (const category of Array.isArray(model.metadata_sync?.categories)
    ? model.metadata_sync.categories
    : []) {
    if (typeof category?.category !== "string") continue;
    const card = element(document, "article", undefined, "category-sync-state");
    card.append(element(document, "h3", category.category));
    card.append(
      element(
        document,
        "p",
        typeof category.synchronized_through === "string"
          ? `Metadata synchronized through ${category.synchronized_through}.`
          : "No metadata checkpoint has been recorded.",
      ),
    );
    const codes = safeCodes(category.error_codes);
    if (codes.length) {
      card.append(element(document, "p", `Error codes: ${codes.join(", ")}.`, "error-banner"));
    }
    metadata.append(card);
  }
  wrapper.append(metadata);

  const coverage = element(document, "section", undefined, "settings-section");
  const coverageState = model.daily_list_coverage ?? {};
  coverage.append(
    element(document, "h2", "Historical daily-list coverage"),
    element(
      document,
      "p",
      `Target dates: ${number(coverageState.target)} · ` +
        `${number(coverageState.checked)} checked · ` +
        `${number(coverageState.with_papers)} with papers · ` +
        `${number(coverageState.empty)} empty · ` +
        `${number(coverageState.failed)} failed · ` +
        `${number(coverageState.pending)} pending · ` +
        `${number(coverageState.unavailable)} unavailable.`,
    ),
  );
  let retryableCount = 0;
  const coverageMin = typeof model.coverage_min === "string" ? model.coverage_min : null;
  const coverageMax = typeof model.coverage_max === "string" ? model.coverage_max : null;
  for (const category of Array.isArray(coverageState.categories)
    ? coverageState.categories
    : []) {
    if (typeof category?.category !== "string") continue;
    const card = element(document, "article", undefined, "category-sync-state");
    card.append(
      element(document, "h3", category.category),
      element(
        document,
        "p",
        `Coverage starts ${category.coverage_start}. ` +
          `${number(category.checked)} of ${number(category.target)} checked · ` +
          `${number(category.with_papers)} with papers · ` +
          `${number(category.empty)} empty · ` +
          `${number(category.failed)} failed · ` +
          `${number(category.pending)} pending · ` +
          `${number(category.unavailable)} unavailable.`,
      ),
    );
    const codes = safeCodes(category.error_codes);
    if (codes.length) card.append(element(document, "p", `Error codes: ${codes.join(", ")}.`));
    const retryable = Array.isArray(category.retryable_failed_dates)
      ? category.retryable_failed_dates.filter((value) =>
        typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value))
      : [];
    retryableCount += retryable.length;

    const coverageLabel = element(document, "label", `Extend ${category.category} coverage to`);
    const coverageInput = element(document, "input");
    coverageInput.setAttribute("type", "date");
    coverageInput.setAttribute("aria-label", `New coverage start for ${category.category}`);
    if (coverageMin) coverageInput.setAttribute("min", coverageMin);
    if (coverageMax) coverageInput.setAttribute("max", coverageMax);
    const extend = actionButton(document, `Extend ${category.category} coverage`, () =>
      actions.extendCoverage?.(category.category, coverageInput.value));
    const updateExtension = () => {
      const value = coverageInput.value;
      extend.disabled = !/^\d{4}-\d{2}-\d{2}$/.test(value) ||
        (coverageMin && value < coverageMin) ||
        (coverageMax && value > coverageMax) ||
        (typeof category.coverage_start === "string" && value >= category.coverage_start);
    };
    coverageInput.addEventListener("input", updateExtension);
    coverageInput.addEventListener("change", updateExtension);
    updateExtension();
    card.append(coverageLabel, coverageInput, extend);
    coverage.append(card);
  }

  const retryProgress = model.daily_list_retry;
  const retryTotal = Number.isSafeInteger(retryProgress?.total) && retryProgress.total > 0
    ? retryProgress.total
    : 0;
  const retryCompleted = Number.isSafeInteger(retryProgress?.completed) &&
      retryProgress.completed > 0
    ? Math.min(retryProgress.completed, retryTotal)
    : 0;
  const retryRunning = retryProgress?.status === "running" && retryTotal > 0;
  if (retryableCount || retryRunning) {
    const count = retryRunning ? retryTotal : retryableCount;
    if (retryRunning) {
      const progress = element(
        document,
        "progress",
        undefined,
        "daily-list-retry-progress",
      );
      progress.setAttribute("value", retryCompleted);
      progress.setAttribute("max", retryTotal);
      progress.setAttribute(
        "aria-label",
        `Retrying failed daily-list dates: ${retryCompleted} of ${retryTotal} completed`,
      );
      coverage.append(progress);
    }
    const retry = actionButton(
      document,
      retryRunning
        ? `Retrying failed daily-list dates… ${retryCompleted} of ${count} completed`
        : synchronizing
        ? `Retrying ${count} failed daily-list ${count === 1 ? "date" : "dates"}…`
        : `Retry ${count} failed daily-list ${count === 1 ? "date" : "dates"}`,
      () => actions.retrySynchronization?.(count),
    );
    retry.disabled = synchronizing || retryRunning;
    coverage.append(retry);
  }
  wrapper.append(coverage);

  const durable = element(document, "section", undefined, "settings-section");
  durable.append(
    element(document, "h2", "Library and PDF presence"),
    element(document, "p", `Saved Library papers: ${number(model.library?.saved_paper_count)}.`),
    element(document, "p", `Downloaded PDFs present: ${number(model.pdf_presence?.downloaded_pdf_count)}.`),
  );
  wrapper.append(durable);
  return wrapper;
}

function renderDoctor(document, report) {
  const section = element(document, "section", undefined, "settings-section");
  section.append(element(document, "h2", "Redacted diagnostics"));
  const allowlist = [
    ["Version", report?.application_version],
    ["Database", report?.database_status],
    ["Application generation", report?.application_generation],
    ["Schema", report?.schema_version],
    ["Profile revision", report?.profile_revision],
    ["Projection revision", report?.projection_revision],
    ["Active categories", report?.active_category_count],
    ["Metadata checkpoints", report?.metadata_checkpoint_count],
    ["Daily-list targets", report?.daily_list_target_count],
    ["Daily-list gaps", report?.daily_list_gap_count],
    ["Saved papers", report?.saved_paper_count],
    ["Downloaded PDFs", report?.downloaded_pdf_count],
    ["Candidate cache", report?.candidate_cache_status],
    ["Maintenance", report?.maintenance_state],
  ];
  const list = element(document, "dl");
  for (const [label, value] of allowlist) {
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") continue;
    list.append(element(document, "dt", label), element(document, "dd", String(value)));
  }
  section.append(list);
  return section;
}

function renderLauncher(document, launcher, actions) {
  const section = element(document, "section", undefined, "settings-section");
  section.append(
    element(document, "h2", "Desktop launcher"),
    element(
      document,
      "p",
      "The launcher opens the installed arxiv-digest command. This does not start arXiv Digest in the background.",
    ),
  );
  const controls = element(document, "div", undefined, "launcher-choices");
  if (launcher?.operation === "create_failed") {
    section.append(
      element(
        document,
        "p",
        "Launcher creation did not complete. Your interests and library are unchanged.",
        "error-banner",
      ),
    );
    controls.append(
      actionButton(document, "Retry launcher", () => actions.retryLauncher?.()),
      actionButton(document, "Not now", () => actions.notNowLauncher?.()),
    );
  } else if (launcher?.installed === true) {
    controls.append(
      actionButton(document, "Recreate desktop launcher", () => actions.createLauncher?.()),
      actionButton(document, "Remove desktop launcher", () => actions.removeLauncher?.()),
    );
  } else {
    controls.append(
      actionButton(document, "Create desktop launcher", () => actions.createLauncher?.()),
    );
  }
  section.append(controls);
  return section;
}

export function renderSettingsView(document, container, model = {}, actions = {}) {
  container.replaceChildren();
  container.append(element(document, "h1", "Settings"));
  const progress = element(document, "div", "", "settings-progress");
  progress.setAttribute("role", "status");
  progress.setAttribute("aria-live", "polite");
  container.append(progress);

  const folder = element(document, "section", undefined, "settings-section");
  const currentDisplayPath =
    typeof model.pdf_destination?.display_path === "string" &&
    model.pdf_destination.display_path.trim()
      ? model.pdf_destination.display_path.trim()
      : null;
  folder.append(
    element(document, "h2", "PDF destination"),
    element(
      document,
      "p",
      currentDisplayPath
        ? `Current PDF folder: ${currentDisplayPath}`
        : "Current PDF folder is configured.",
    ),
  );
  const pickerChoice =
    typeof model.picker_choice === "string" && PICKER_ID.test(model.picker_choice)
      ? model.picker_choice
      : null;
  if (pickerChoice) {
    const displayName =
      typeof model.picker_display_name === "string" && model.picker_display_name.trim()
        ? model.picker_display_name.trim()
        : "Chosen folder";
    folder.append(
      element(document, "p", `Selected folder: ${displayName}`, "selected-destination"),
    );
  }
  const folderControls = element(document, "div", undefined, "settings-actions");
  if (pickerChoice) {
    folderControls.append(
      actionButton(document, "Test and use folder", () => actions.testFolder?.(pickerChoice)),
    );
  }
  folderControls.append(
    actionButton(document, "Open folder", () => actions.openFolder?.()),
    actionButton(document, "Choose PDF folder", () => actions.pickFolder?.()),
  );
  if (model.picker_unavailable === true) {
    folder.append(
      element(
        document,
        "p",
        "The native folder picker is unavailable. You can use an app-managed fallback folder instead.",
        "picker-status",
      ),
    );
    folderControls.append(
      actionButton(document, "Test and use Downloads fallback", () =>
        actions.testFolder?.("downloads")),
      actionButton(document, "Test and use Documents fallback", () =>
        actions.testFolder?.("documents")),
    );
  }
  folder.append(folderControls);
  container.append(folder, renderSyncSettings(document, model, actions));

  const backup = element(document, "section", undefined, "settings-section");
  backup.append(
    element(document, "h2", "Backup and restore"),
    element(
      document,
      "p",
      "Export downloads a portable ZIP containing your interests, paper metadata, " +
        "synchronization history, review progress, and saved Library papers. It does not " +
        "include downloaded PDFs, suggestion cache data, or your machine-specific PDF folder.",
    ),
    element(
      document,
      "p",
      "Inspect validates and previews a backup without changing anything. Restore replaces " +
        "your current local data with the backup after you choose a PDF folder. Before " +
        "replacing anything, arXiv Digest creates a recovery backup of your current data.",
    ),
    actionButton(document, "Export backup", () => actions.exportBackup?.()),
  );
  const importInput = element(document, "input");
  importInput.setAttribute("type", "file");
  importInput.setAttribute("accept", "application/zip,.zip");
  importInput.setAttribute("aria-label", "Portable backup ZIP");
  backup.append(
    importInput,
    actionButton(document, "Inspect backup", () => actions.inspectBackup?.(importInput.files?.[0])),
  );
  container.append(backup, renderDoctor(document, model.doctor));

  const cache = element(document, "section", undefined, "settings-section");
  const cacheStatus = ["missing", "empty", "ready"].includes(model.candidate_cache?.status)
    ? model.candidate_cache.status
    : "unknown";
  const cacheFileCount = Number.isSafeInteger(model.candidate_cache?.file_count) &&
      model.candidate_cache.file_count >= 0
    ? model.candidate_cache.file_count
    : 0;
  cache.append(
    element(document, "h2", "Candidate cache"),
    element(document, "p", `Candidate cache: ${cacheStatus} · ${cacheFileCount} files.`),
    element(
      document,
      "p",
      "Stores a temporary recent-paper sample used to build setup and Interests suggestions. " +
        "Delete it to free space or clear a stale sample; arXiv Digest will download and " +
        "rebuild it when you refresh suggestions. Interests, synchronization checkpoints, " +
        "review progress, Library papers, and downloaded PDFs are not removed.",
    ),
  );
  const confirmClear = actionButton(
    document,
    "Confirm delete suggestion cache",
    () => actions.clearCache?.(),
  );
  confirmClear.hidden = true;
  cache.append(
    actionButton(document, "Delete suggestion cache", () => {
      confirmClear.hidden = false;
      progress.textContent =
        "Confirm suggestion cache deletion. Durable data and downloaded PDFs will be kept.";
    }),
    confirmClear,
  );
  container.append(cache, renderLauncher(document, model.launcher, actions));

  const quit = element(document, "section", undefined, "settings-section");
  quit.append(
    element(document, "h2", "Quit"),
    element(
      document,
      "p",
      "The digest can be reopened with the arxiv-digest command or desktop launcher.",
    ),
    actionButton(document, "Quit arXiv Digest", () => actions.quit?.()),
  );
  container.append(quit);
  return container;
}

export function renderRestoreInspection(document, container, inspection, actions = {}) {
  assertPendingRestoreId(inspection?.pending_restore_id);
  container.replaceChildren();
  container.append(
    element(document, "h2", "Restore inspection"),
    element(
      document,
      "p",
      "Inspection made no changes. Reconfirm a PDF destination before restoring.",
    ),
  );
  const summary = element(document, "dl");
  const summaryFields = [
    ["Categories", inspection.summary?.categories],
    ["Saved papers", inspection.summary?.saved_papers],
    ["Review events", inspection.summary?.review_events],
    ["Profile revision", inspection.summary?.profile_revision],
  ];
  for (const [label, value] of summaryFields) {
    if (!Number.isSafeInteger(value) || value < 0) continue;
    summary.append(element(document, "dt", label), element(document, "dd", String(value)));
  }
  container.append(summary);

  const destination = element(document, "fieldset");
  destination.append(element(document, "legend", "Reconfirm PDF destination"));
  let destinationChoice = null;
  const pendingId = inspection.pending_restore_id;
  let restoreButton;
  let preBackup;
  const refresh = () => {
    if (restoreButton) restoreButton.disabled = !destinationChoice || !preBackup.checked;
  };
  if (
    typeof inspection.picker_choice === "string" &&
    PICKER_ID.test(inspection.picker_choice)
  ) {
    const value = inspection.picker_choice;
    const displayName =
      typeof inspection.picker_display_name === "string" &&
      inspection.picker_display_name.trim()
        ? inspection.picker_display_name.trim()
        : "Chosen folder";
    destination.append(
      element(document, "p", `Selected folder: ${displayName}`, "selected-destination"),
      actionButton(document, "Use this folder", () => {
        destinationChoice = value;
        actions.confirmDestination?.(pendingId, value);
        refresh();
      }),
      actionButton(document, "Choose another folder", () => actions.pickFolder?.(pendingId)),
    );
  } else if (inspection.picker_unavailable === true) {
    destination.append(
      element(
        document,
        "p",
        "The native folder picker is unavailable. Choose an app-managed fallback folder for restored PDFs.",
        "picker-status",
      ),
      actionButton(document, "Use Downloads fallback", () => {
        destinationChoice = "downloads";
        actions.confirmDestination?.(pendingId, destinationChoice);
        refresh();
      }),
      actionButton(document, "Use Documents fallback", () => {
        destinationChoice = "documents";
        actions.confirmDestination?.(pendingId, destinationChoice);
        refresh();
      }),
      actionButton(document, "Try folder picker again", () => actions.pickFolder?.(pendingId)),
    );
  } else {
    destination.append(
      actionButton(document, "Choose PDF folder", () => actions.pickFolder?.(pendingId)),
    );
  }
  container.append(destination);

  const preBackupLabel = element(document, "label");
  preBackup = element(document, "input");
  preBackup.setAttribute("type", "checkbox");
  preBackup.setAttribute("name", "confirm-pre-restore-backup");
  preBackup.addEventListener("change", refresh);
  preBackupLabel.append(
    preBackup,
    document.createTextNode(" I understand a pre-restore backup will be created before local state changes."),
  );
  const cancelLabel = element(document, "label");
  const cancelActive = element(document, "input");
  cancelActive.setAttribute("type", "checkbox");
  cancelActive.setAttribute("name", "cancel-active-work");
  cancelLabel.append(
    cancelActive,
    document.createTextNode(" Cancel active synchronization or downloads if needed."),
  );
  container.append(preBackupLabel, cancelLabel);
  restoreButton = actionButton(document, "Restore backup", () => {
    actions.restore?.(pendingId, {
      cancelActive: cancelActive.checked,
      confirmedPreRestoreBackup: preBackup.checked,
    });
  });
  restoreButton.disabled = true;
  container.append(restoreButton);
  return container;
}

export function renderRestoreError(document, container, message, retry) {
  const banner = element(document, "section", undefined, "error-banner");
  banner.setAttribute("role", "alert");
  banner.append(
    element(
      document,
      "p",
      typeof message === "string" ? message : "Restore did not complete. Local data remains usable.",
    ),
    actionButton(document, "Retry restore", () => retry?.()),
  );
  container.append(banner);
  return banner;
}
