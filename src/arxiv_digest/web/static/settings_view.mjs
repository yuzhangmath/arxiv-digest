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

  extendCoverage(category, newStart) {
    if (typeof category !== "string" || !category.trim()) {
      throw new TypeError("Invalid category");
    }
    assertIsoDate(newStart);
    return this.api.json(
      "settings-coverage",
      "/api/v1/settings/coverage",
      jsonPut({ category: category.trim(), new_start: newStart }),
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

function renderDestinationChoice(document, value, label, checked) {
  const row = element(document, "label", undefined, "destination-choice");
  const input = element(document, "input");
  input.setAttribute("type", "radio");
  input.setAttribute("name", "pdf-destination");
  input.setAttribute("value", value);
  input.checked = checked;
  row.append(input, document.createTextNode(` ${label}`));
  return row;
}

function renderSyncSettings(document, model, actions) {
  const section = element(document, "section", undefined, "settings-section");
  section.append(element(document, "h2", "Synchronization and history"));
  const online = model.online !== false;
  section.append(
    element(
      document,
      "p",
      online
        ? "Synchronization is online."
        : "Synchronization offline. Cached Review and Library remain available.",
      online ? "sync-online" : "sync-offline",
    ),
  );
  for (const category of Array.isArray(model.categories) ? model.categories : []) {
    if (typeof category?.category !== "string") continue;
    const card = element(document, "article", undefined, "category-sync-state");
    card.append(element(document, "h3", category.category));
    if (typeof category.metadata_synchronized_through === "string") {
      card.append(
        element(
          document,
          "p",
          `Metadata synchronized through ${category.metadata_synchronized_through}`,
        ),
      );
    }
    const backfillStatus = category.historical_backfill?.status ?? "not started";
    card.append(
      element(
        document,
        "p",
        `Historical coverage backfill: ${backfillStatus}` +
          (backfillStatus === "interrupted"
            ? "; current metadata remains synchronized."
            : "."),
      ),
    );
    const enrichment = category.exact_enrichment ?? {};
    if (typeof enrichment.start === "string" && typeof enrichment.end === "string") {
      card.append(
        element(
          document,
          "p",
          `Exact announcement enrichment ${enrichment.start} through ${enrichment.end}`,
        ),
      );
    } else {
      card.append(element(document, "p", "Exact announcement enrichment: not available"));
    }
    const holes = Array.isArray(enrichment.holes)
      ? enrichment.holes.filter((value) => typeof value === "string")
      : [];
    card.append(
      element(
        document,
        "p",
        holes.length ? `Missing exact dates: ${holes.join(", ")}` : "Missing exact dates: none",
      ),
    );
    if (category.current_sync?.status === "failed") {
      const code = typeof category.current_sync.error_code === "string"
        ? ` (${category.current_sync.error_code})`
        : "";
      card.append(
        element(
          document,
          "p",
          `${category.category} synchronization failed${code}.`,
          "error-banner",
        ),
      );
    }
    const coverageLabel = element(document, "label", `Extend ${category.category} history to`);
    const coverageInput = element(document, "input");
    coverageInput.setAttribute("type", "date");
    coverageInput.setAttribute("aria-label", `New coverage start for ${category.category}`);
    card.append(
      coverageLabel,
      coverageInput,
      actionButton(document, `Extend ${category.category} history`, () =>
        actions.extendCoverage?.(category.category, coverageInput.value)),
    );
    section.append(card);
  }
  return section;
}

function renderDoctor(document, report) {
  const section = element(document, "section", undefined, "settings-section");
  section.append(element(document, "h2", "Redacted diagnostics"));
  const allowlist = [
    ["Version", report?.application_version],
    ["Database", report?.database_status],
    ["Categories", report?.category_count],
    ["Saved papers", report?.saved_paper_count],
    ["Destination kind", report?.destination_kind],
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
  folder.append(element(document, "h2", "PDF destination"));
  const destinationKind = model.pdf_destination?.kind;
  const pickerChoice =
    typeof model.picker_choice === "string" && PICKER_ID.test(model.picker_choice)
      ? model.picker_choice
      : null;
  let selectedDestinationChoice =
    destinationKind === "downloads" || destinationKind === "documents"
      ? destinationKind
      : destinationKind === "custom"
        ? pickerChoice
        : null;
  let testButton;
  const choices = [
    ["downloads", "Downloads / Arxiv Digest"],
    ["documents", "Documents / Arxiv Digest"],
  ];
  if (pickerChoice) choices.push([pickerChoice, "Chosen folder"]);
  for (const [value, label] of choices) {
    const row = renderDestinationChoice(
      document,
      value,
      label,
      selectedDestinationChoice === value,
    );
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      if (!input.checked) return;
      selectedDestinationChoice = value;
      if (testButton) testButton.disabled = false;
    });
    folder.append(row);
  }
  const folderControls = element(document, "div", undefined, "settings-actions");
  testButton = actionButton(document, "Test download", () => {
    if (selectedDestinationChoice) actions.testFolder?.(selectedDestinationChoice);
  });
  testButton.disabled = !selectedDestinationChoice;
  folderControls.append(
    testButton,
    actionButton(document, "Open folder", () => actions.openFolder?.()),
    actionButton(document, "Choose another folder", () => actions.pickFolder?.()),
  );
  folder.append(folderControls);
  container.append(folder, renderSyncSettings(document, model, actions));

  const backup = element(document, "section", undefined, "settings-section");
  backup.append(
    element(document, "h2", "Backup and restore"),
    element(
      document,
      "p",
      "Inspecting a backup makes no changes. Restore asks you to reconfirm the PDF destination and creates a pre-restore backup.",
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
  cache.append(
    element(document, "h2", "Disposable cache"),
    element(
      document,
      "p",
      "Deleting the cache keeps your interests, synchronization checkpoints, review progress, and saved-paper library.",
    ),
  );
  const confirmClear = actionButton(document, "Confirm delete cache", () => actions.clearCache?.());
  confirmClear.hidden = true;
  cache.append(
    actionButton(document, "Delete cache", () => {
      confirmClear.hidden = false;
      progress.textContent = "Confirm cache deletion. Durable data will be kept.";
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
  for (const [value, label] of [
    ["downloads", "Downloads / Arxiv Digest"],
    ["documents", "Documents / Arxiv Digest"],
  ]) {
    const row = renderDestinationChoice(document, value, label, false);
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      if (!input.checked) return;
      destinationChoice = value;
      actions.confirmDestination?.(pendingId, value);
      refresh();
    });
    destination.append(row);
  }
  if (
    typeof inspection.picker_choice === "string" &&
    PICKER_ID.test(inspection.picker_choice)
  ) {
    const value = inspection.picker_choice;
    const row = renderDestinationChoice(document, value, "Chosen folder", false);
    const input = row.querySelector("input");
    input.addEventListener("change", () => {
      if (!input.checked) return;
      destinationChoice = value;
      actions.confirmDestination?.(pendingId, value);
      refresh();
    });
    destination.append(row);
  } else {
    destination.append(
      actionButton(document, "Choose another folder", () => actions.pickFolder?.(pendingId)),
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
