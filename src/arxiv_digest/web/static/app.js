import { ApiClient, StaleResponseError } from "./api.mjs";
import { renderCalendar } from "./calendar_view.mjs";
import {
  InterestsController,
  InterestsDraft,
  renderInterestsView,
} from "./interests_view.mjs";
import { LibraryController, renderLibraryView } from "./library_view.mjs";
import { renderReviewError, renderReviewView } from "./review_view.mjs";
import {
  SettingsController,
  renderRestoreError,
  renderRestoreInspection,
  renderSettingsView,
} from "./settings_view.mjs";
import { renderSetupError, renderSetupView } from "./setup_view.mjs";
import {
  ViewState,
  bootstrapSession,
  clearSession,
  validatedView,
} from "./state.mjs";

const content = document.querySelector("#content");
const status = document.querySelector("#status");
const navigation = document.querySelector(".primary-navigation");

function statusText(message) {
  status.textContent = message;
}

let session;
try {
  session = bootstrapSession({ location, history, sessionStorage });
} catch {
  statusText("This dashboard link is invalid or expired. Reopen arXiv Digest from the command line.");
  content.replaceChildren();
  const heading = document.createElement("h1");
  heading.textContent = "Session unavailable";
  content.append(heading);
  throw new Error("Dashboard bootstrap rejected");
}

const state = new ViewState(session.view);
const api = new ApiClient(location.origin, session.token, fetch, () => {
  clearSession(sessionStorage);
  statusText("Your local session expired. Reopen arXiv Digest to continue.");
});
const libraryController = new LibraryController(api);
const interestsController = new InterestsController(api);
const settingsController = new SettingsController(api);

function jsonBody(value) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function jsonRequest(method, value) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function markNavigation(view) {
  for (const control of navigation.querySelectorAll("[data-view]")) {
    const selected = control.dataset.view === view ||
      (control.dataset.view === "review" && view === "setup");
    if (selected) control.setAttribute("aria-current", "page");
    else control.removeAttribute("aria-current");
  }
}

function tokenFreeViewUrl(view) {
  return `${location.pathname}?view=${encodeURIComponent(view)}`;
}

let reviewRequestSequence = 0;

async function requestReview(destination = {}) {
  const requestSequence = ++reviewRequestSequence;
  let date = destination.date;
  if (!date) {
    const summary = await api.json("review-summary", "/api/v1/review/summary");
    if (requestSequence !== reviewRequestSequence) return;
    content.replaceChildren();
    const heading = document.createElement("h1");
    heading.textContent = "Review";
    const paragraph = document.createElement("p");
    paragraph.textContent = summary.oldest_unreviewed_date
      ? `${summary.unreviewed_papers} papers across ${summary.unreviewed_dates} dates are ready. ${summary.newly_discovered} are newly discovered.`
      : "You are caught up. Newly discovered papers will appear here.";
    content.append(heading, paragraph);
    if (summary.oldest_unreviewed_date) {
      const oldest = String(summary.oldest_unreviewed_date);
      const start = document.createElement("button");
      start.type = "button";
      start.textContent = "Start review";
      start.addEventListener("click", () => requestReview({ date: oldest }));
      content.append(start);
    }
    return;
  }
  const parameters = new URLSearchParams({ date });
  if (destination.anchor_event_id != null) {
    parameters.set("anchor_event_id", String(destination.anchor_event_id));
  }
  const page = await api.json("review-page", `/api/v1/review/date?${parameters}`);
  if (requestSequence !== reviewRequestSequence) return;
  renderReviewView(document, content, page, {
    navigate: requestReview,
    finish: async (openedDay, snapshotRevision) => {
      await api.json(
        "review-finish",
        "/api/v1/review/date/finish",
        jsonBody({ date: openedDay, snapshot_revision: snapshotRevision }),
      );
      await requestReview({});
    },
    paperActions: {
      save: (arxivId, version) => api.json("paper-save", "/api/v1/library/save", jsonBody({ arxiv_id: arxivId, version: version || null })),
      download: (arxivId, version) => api.json("paper-pdf", "/api/v1/library/pdf", jsonBody({ arxiv_id: arxivId, version, save_first: false })),
      saveAndDownload: (arxivId, version) => api.json("paper-pdf", "/api/v1/library/pdf", jsonBody({ arxiv_id: arxivId, version, save_first: true })),
    },
  });
  if (page.anchor_event_id != null) {
    api.json(
      "review-position",
      "/api/v1/review/date/position",
      jsonRequest("PUT", {
        date: String(page.day),
        snapshot_revision: Number(page.snapshot_revision),
        anchor_event_id: Number(page.anchor_event_id),
      }),
    ).catch(() => {});
  }
}

async function requestCalendar() {
  reviewRequestSequence += 1;
  const now = new Date();
  const start = `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}-01`;
  const endDate = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0));
  const end = endDate.toISOString().slice(0, 10);
  const entries = await api.json("calendar", `/api/v1/review/calendar?start=${start}&end=${end}`);
  content.replaceChildren();
  const heading = document.createElement("h1");
  heading.textContent = "Calendar";
  content.append(heading);
  const calendar = document.createElement("section");
  content.append(calendar);
  renderCalendar(document, calendar, entries, (date) => {
    navigate("review", { reviewDate: date });
  });
}

let libraryPage = null;
const libraryDownloadState = new Map();
const libraryJobPaper = new Map();

function projectedLibraryPage(page) {
  return {
    ...page,
    entries: (page?.entries ?? []).map((entry) => {
      const arxivId = entry?.metadata?.arxiv_id;
      const download = libraryDownloadState.get(arxivId);
      return download ? { ...entry, download } : entry;
    }),
  };
}

function redrawLibrary() {
  if (!libraryPage) return;
  renderLibraryView(document, content, projectedLibraryPage(libraryPage), libraryActions);
}

async function pollLibraryDownload(jobId) {
  const arxivId = libraryJobPaper.get(jobId);
  const result = await libraryController.downloadStatus(jobId);
  if (arxivId) libraryDownloadState.set(arxivId, { ...result, job_id: jobId });
  redrawLibrary();
  if (result.failed || result.status === "failed") {
    statusText("PDF download did not complete. You can retry it from the paper.");
    return;
  }
  if (result.complete || result.status === "completed") {
    statusText("PDF download complete.");
    return;
  }
  statusText("Downloading PDF…");
  setTimeout(() => pollLibraryDownload(jobId).catch(showActionFailure), 350);
}

async function startLibraryDownload(arxivId, version) {
  const result = await libraryController.download(arxivId, version);
  libraryJobPaper.set(result.job_id, arxivId);
  libraryDownloadState.set(arxivId, { job_id: result.job_id, status: "running" });
  redrawLibrary();
  statusText("Downloading PDF…");
  await pollLibraryDownload(result.job_id);
}

const libraryActions = {
  search(query) {
    requestLibrary(query, 0).catch(showActionFailure);
  },
  page(offset) {
    requestLibrary(libraryPage?.query ?? "", offset).catch(showActionFailure);
  },
  async remove(arxivId) {
    try {
      await libraryController.remove(arxivId);
      libraryDownloadState.delete(arxivId);
      await requestLibrary(libraryPage?.query ?? "", libraryPage?.offset ?? 0);
      statusText("Paper removed from the library.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  downloadPdf(arxivId, version) {
    startLibraryDownload(arxivId, version).catch(showActionFailure);
  },
  async retryPdf(jobId) {
    try {
      const arxivId = libraryJobPaper.get(jobId);
      const result = await libraryController.retryDownload(jobId);
      if (arxivId) {
        libraryJobPaper.set(result.job_id, arxivId);
        libraryDownloadState.set(arxivId, { job_id: result.job_id, status: "running" });
      }
      redrawLibrary();
      await pollLibraryDownload(result.job_id);
    } catch (error) {
      showActionFailure(error);
    }
  },
};

async function requestLibrary(query = "", offset = 0) {
  libraryPage = await libraryController.search(query, offset);
  redrawLibrary();
}

let interestsModel = null;

function interestsPayloadModel(payload, draft = null) {
  const profile = payload?.profile ?? payload;
  return {
    draft: draft ?? new InterestsDraft(profile),
    suggestions: payload?.suggestions ?? {},
    suggestions_generated_at: payload?.suggestions_generated_at,
  };
}

function redrawInterests() {
  if (!interestsModel) return;
  renderInterestsView(document, content, interestsModel, interestsActions);
}

const interestsActions = {
  async refreshSuggestions() {
    try {
      const payload = await interestsController.freshSuggestions();
      interestsModel = interestsPayloadModel(payload, interestsModel?.draft);
      redrawInterests();
      statusText("Suggestions refreshed. Nothing changes until you save.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async save(draft) {
    try {
      await interestsController.save(draft);
      redrawInterests();
      statusText("Interests saved.");
    } catch (error) {
      showActionFailure(error);
    }
  },
};

async function requestInterests() {
  interestsModel = interestsPayloadModel(await interestsController.load());
  redrawInterests();
}

let settingsModel = null;
let settingsPickerChoice = null;

function redrawSettings() {
  if (!settingsModel) return;
  renderSettingsView(
    document,
    content,
    {
      ...settingsModel,
      picker_choice: settingsPickerChoice,
    },
    settingsActions,
  );
}

async function requestSettings() {
  const [settings, doctor, launcher] = await Promise.all([
    api.json("settings-load", "/api/v1/settings"),
    settingsController.doctor(),
    settingsController.launcherStatus(),
  ]);
  settingsModel = { ...settings, doctor, launcher };
  redrawSettings();
}

function normalizedRestoreInspection(inspection) {
  if (inspection?.summary) return inspection;
  return {
    ...inspection,
    summary: {
      categories: inspection?.category_count,
      saved_papers: inspection?.saved_paper_count,
      review_events: inspection?.review_event_count,
      profile_revision: inspection?.profile_revision,
    },
  };
}

function showRestoreInspection(inspectionValue) {
  const inspection = normalizedRestoreInspection(inspectionValue);
  const pendingId = inspection.pending_restore_id;
  const restore = async (options) => {
    try {
      await settingsController.restoreBackup(pendingId, options);
      await requestSettings();
      statusText("Backup restored after creating a pre-restore backup.");
    } catch (error) {
      renderRestoreError(document, content, error.message, () => restore(options));
      statusText("Restore did not complete. Local data remains usable.");
    }
  };
  renderRestoreInspection(document, content, inspection, {
    confirmDestination: (id, choice) =>
      settingsController.reconfirmRestoreDestination(id, choice),
    async pickFolder() {
      try {
        const result = await settingsController.pickFolder();
        const choice = result?.destination_choice ?? result?.picker_result_id;
        if (choice) {
          showRestoreInspection({ ...inspection, picker_choice: choice });
        } else {
          statusText("Folder selection was cancelled.");
        }
      } catch (error) {
        showActionFailure(error);
      }
    },
    restore: (_id, options) => restore(options),
  });
}

const settingsActions = {
  async openFolder() {
    try {
      await settingsController.openFolder();
      statusText("PDF folder opened.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async pickFolder() {
    try {
      const result = await settingsController.pickFolder();
      const choice = result?.destination_choice ?? result?.picker_result_id;
      if (choice) {
        settingsPickerChoice = choice;
        redrawSettings();
        statusText("Folder selected. Test it before saving.");
      } else {
        statusText("Folder selection was cancelled.");
      }
    } catch (error) {
      showActionFailure(error);
    }
  },
  async testFolder(choice) {
    try {
      await settingsController.testFolder(choice);
      await settingsController.saveTestedFolder(Number(settingsModel?.revision));
      settingsPickerChoice = null;
      await requestSettings();
      statusText("PDF destination saved.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async extendCoverage(category, newStart) {
    try {
      await settingsController.extendCoverage(category, newStart);
      await requestSettings();
      statusText(`Historical coverage for ${category} was extended.`);
    } catch (error) {
      showActionFailure(error);
    }
  },
  async clearCache() {
    try {
      await settingsController.clearCache(true);
      statusText("Cache deleted. Interests, checkpoints, review progress, and library were kept.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  createLauncher() {
    settingsController.createLauncher()
      .then(requestSettings)
      .then(() => statusText("Desktop launcher created."))
      .catch(showActionFailure);
  },
  retryLauncher() {
    this.createLauncher();
  },
  notNowLauncher() {
    settingsController.notNowLauncher()
      .then(requestSettings)
      .then(() => statusText("Desktop launcher deferred."))
      .catch(showActionFailure);
  },
  removeLauncher() {
    settingsController.removeLauncher()
      .then(requestSettings)
      .then(() => statusText("Desktop launcher removed."))
      .catch(showActionFailure);
  },
  exportBackup() {
    settingsController.downloadBackup()
      .then(() => statusText("Portable backup downloaded."))
      .catch(showActionFailure);
  },
  async inspectBackup(archive) {
    try {
      showRestoreInspection(await settingsController.inspectBackup(archive));
      statusText("Backup inspected. No local data has changed.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  quit() {
    quitApplication();
  },
};

function showActionFailure(error) {
  if (error instanceof StaleResponseError || error?.name === "AbortError") return;
  statusText(error?.message || "The local operation did not complete.");
}

const setupUi = {
  draft: null,
  categoryOptions: [],
  categorySelections: new Map(),
  paperOptions: [],
  keywordOptions: [],
  phraseOptions: [],
  authorOptions: [],
  accepted: {
    seed_papers: new Set(),
    keywords: new Set(),
    phrases: new Set(),
    authors: new Set(),
  },
  custom: {
    paper_ids: [],
    keywords: [],
    phrases: [],
    authors: [],
  },
  coverageStart: "",
  destinationChoice: "downloads",
  testedDestinationToken: null,
  pickerState: "available",
  corpusJob: null,
  launcherChoice: null,
};

function setupStep(draft = setupUi.draft) {
  return String(draft?.current_step ?? draft?.step ?? "categories");
}

function optionWithChecked(kind, option) {
  const id = String(option?.suggestion_id ?? option?.id ?? option?.value ?? option ?? "");
  return typeof option === "string"
    ? { value: option, label: option, checked: setupUi.accepted[kind]?.has(id) }
    : { ...option, checked: setupUi.accepted[kind]?.has(id) };
}

function setupModel() {
  const draft = setupUi.draft ?? {};
  return {
    ...draft,
    categoryOptions: setupUi.categoryOptions.map((option) => ({
      ...option,
      checked: setupUi.categorySelections.has(
        `${option.category}\u0000${option.set_spec}`,
      ),
    })),
    categorySelections: [...setupUi.categorySelections.values()],
    recommendedCoverageStart:
      draft.recommended_coverage_start ?? draft.recommendedCoverageStart ?? "",
    coverageStart: setupUi.coverageStart || draft.coverage_start || "",
    coverageWarning: draft.coverage_warning,
    corpusJob: setupUi.corpusJob ?? {
      complete: draft.corpus_complete === true,
      corpus_hash: draft.corpus_hash,
      reduced_breadth: draft.corpus_reduced_breadth === true,
      can_resume: draft.corpus_can_resume === true,
    },
    paperOptions: setupUi.paperOptions.map((option) => optionWithChecked("seed_papers", option)),
    keywordOptions: setupUi.keywordOptions.map((option) => optionWithChecked("keywords", option)),
    phraseOptions: setupUi.phraseOptions.map((option) => optionWithChecked("phrases", option)),
    authorOptions: setupUi.authorOptions.map((option) => optionWithChecked("authors", option)),
    customPaperIds: setupUi.custom.paper_ids,
    customKeywords: setupUi.custom.keywords,
    customPhrases: setupUi.custom.phrases,
    customAuthors: setupUi.custom.authors,
    destinationChoice: setupUi.destinationChoice,
    testedDestinationToken: setupUi.testedDestinationToken,
    pickerState: setupUi.pickerState,
    launcherChoice: setupUi.launcherChoice,
    profileSummary: draft.profile_summary ?? draft.review_summary,
    profileSummarySha256:
      draft.profile_summary_sha256 ?? draft.review_summary_sha256,
  };
}

function redrawSetup() {
  renderSetupView(document, content, setupModel(), setupActions);
}

function replaceOptions(kind, payload) {
  const items = Array.isArray(payload)
    ? payload
    : payload?.items ?? payload?.suggestions ?? payload?.categories ?? [];
  if (kind === "categories") setupUi.categoryOptions = items;
  else if (kind === "seed_papers") setupUi.paperOptions = items;
  else if (kind === "authors") setupUi.authorOptions = items;
}

async function loadSetupOptions(query = "") {
  const step = setupStep();
  if (step === "categories") {
    const payload = await api.json(
      "setup-categories",
      `/api/v1/categories?q=${encodeURIComponent(query)}`,
    );
    replaceOptions("categories", payload);
  } else if (step === "seed_papers") {
    const payload = await api.json(
      "setup-papers",
      `/api/v1/setup/candidates/papers?q=${encodeURIComponent(query)}&offset=0`,
    );
    replaceOptions("seed_papers", payload);
  } else if (step === "keywords_and_phrases" || step === "terms") {
    const payload = await api.json("setup-terms", "/api/v1/setup/candidates/terms");
    setupUi.keywordOptions = payload?.keywords ?? [];
    setupUi.phraseOptions = payload?.phrases ?? [];
  } else if (step === "authors") {
    const payload = await api.json(
      "setup-authors",
      `/api/v1/setup/candidates/authors?q=${encodeURIComponent(query)}`,
    );
    replaceOptions("authors", payload);
  }
}

async function refreshSetup({ loadOptions = true } = {}) {
  setupUi.draft = await api.json("setup-draft", "/api/v1/setup/draft");
  setupUi.coverageStart ||= setupUi.draft.coverage_start ?? "";
  if (loadOptions) await loadSetupOptions();
  redrawSetup();
}

async function submitSetupStep(step) {
  const revision = Number(setupUi.draft?.revision);
  let payload;
  if (step === "categories") {
    payload = {
      revision,
      step,
      selections: [...setupUi.categorySelections.values()],
    };
  } else if (step === "coverage") {
    payload = { revision, step, coverage_start: setupUi.coverageStart };
  } else if (step === "seed_papers") {
    payload = {
      revision,
      step,
      accepted_suggestion_ids: [...setupUi.accepted.seed_papers],
      custom_arxiv_ids: setupUi.custom.paper_ids.filter(Boolean),
    };
  } else if (step === "terms") {
    payload = {
      revision,
      step,
      accepted_keyword_suggestion_ids: [...setupUi.accepted.keywords],
      accepted_phrase_suggestion_ids: [...setupUi.accepted.phrases],
      custom_keywords: setupUi.custom.keywords.filter(Boolean),
      custom_phrases: setupUi.custom.phrases.filter(Boolean),
    };
  } else if (step === "authors") {
    payload = {
      revision,
      step,
      accepted_suggestion_ids: [...setupUi.accepted.authors],
      custom_authors: setupUi.custom.authors.filter(Boolean),
    };
  } else if (step === "pdf_destination") {
    payload = {
      revision,
      step,
      tested_destination_token: setupUi.testedDestinationToken,
    };
  } else if (step === "review") {
    payload = {
      revision,
      step,
      confirmed: true,
      profile_summary_sha256: setupModel().profileSummarySha256,
    };
  } else if (step === "launcher") {
    await api.json(
      "setup-complete",
      "/api/v1/setup/complete",
      jsonBody({
        draft_revision: revision,
        launcher_choice: setupUi.launcherChoice,
      }),
    );
    try {
      await api.json(
        "sync-start",
        "/api/v1/sync/start",
        jsonBody({}),
      );
    } catch {
      statusText("Setup is complete. Synchronization can be retried from Settings.");
    }
    navigate("review");
    return;
  } else {
    return;
  }
  const updated = await api.json(
    "setup-update",
    "/api/v1/setup/draft",
    jsonRequest("PUT", payload),
  );
  setupUi.draft = updated?.revision == null ? null : updated;
  await refreshSetup();
}

async function pollCorpusJob(jobId) {
  const job = await api.json("setup-job", `/api/v1/setup/jobs/${encodeURIComponent(jobId)}`);
  setupUi.corpusJob = job;
  redrawSetup();
  if (!job.complete && !job.failed) {
    setTimeout(() => pollCorpusJob(jobId).catch(showSetupFailure), 300);
  }
}

function showSetupFailure(error) {
  renderSetupError(document, content, error.message, () => refreshSetup());
}

const setupActions = {
  async onSearch(kind, query) {
    try {
      if (kind === "categories") {
        const payload = await api.json(
          "setup-categories",
          `/api/v1/categories?q=${encodeURIComponent(query)}`,
        );
        replaceOptions(kind, payload);
      } else if (kind === "seed_papers") {
        const payload = await api.json(
          "setup-papers",
          `/api/v1/setup/candidates/papers?q=${encodeURIComponent(query)}&offset=0`,
        );
        replaceOptions(kind, payload);
      } else if (kind === "authors") {
        const payload = await api.json(
          "setup-authors",
          `/api/v1/setup/candidates/authors?q=${encodeURIComponent(query)}`,
        );
        replaceOptions(kind, payload);
      }
      redrawSetup();
    } catch (error) {
      showSetupFailure(error);
    }
  },
  onSuggestion(kind, stable, checked) {
    if (kind === "categories") {
      const key = `${stable.category}\u0000${stable.set_spec}`;
      if (checked) setupUi.categorySelections.set(key, stable);
      else setupUi.categorySelections.delete(key);
    } else {
      if (checked) setupUi.accepted[kind].add(stable);
      else setupUi.accepted[kind].delete(stable);
    }
    redrawSetup();
  },
  onCustomAdd(kind) {
    setupUi.custom[kind].push("");
    redrawSetup();
  },
  onCustomChange(kind, index, value) {
    setupUi.custom[kind][index] = value;
  },
  onCustomRemove(kind, index) {
    setupUi.custom[kind].splice(index, 1);
    redrawSetup();
  },
  onCoverageChange(value) {
    setupUi.coverageStart = value;
    redrawSetup();
  },
  async onCorpus(mode) {
    try {
      const result = await api.json(
        "setup-corpus",
        "/api/v1/setup/corpus",
        jsonBody({ draft_revision: setupUi.draft.revision, mode }),
      );
      setupUi.corpusJob = { message: "Building the bounded corpus…" };
      redrawSetup();
      await pollCorpusJob(result.job_id);
    } catch (error) {
      showSetupFailure(error);
    }
  },
  async onCorpusAccept(corpusHash) {
    try {
      await api.json(
        "setup-corpus-accept",
        "/api/v1/setup/corpus/accept",
        jsonBody({
          draft_revision: setupUi.draft.revision,
          corpus_hash: corpusHash,
        }),
      );
      setupUi.corpusJob = null;
      await refreshSetup();
    } catch (error) {
      showSetupFailure(error);
    }
  },
  onDestinationChoice(choice) {
    setupUi.destinationChoice = choice;
    setupUi.testedDestinationToken = null;
    setupUi.pickerState = "available";
    redrawSetup();
  },
  async onPickDestination() {
    try {
      const result = await api.json(
        "setup-folder-pick",
        "/api/v1/setup/folder/pick",
        jsonBody({ draft_revision: setupUi.draft.revision }),
      );
      if (result.cancelled) setupUi.pickerState = "cancelled";
      else if (result.unavailable) setupUi.pickerState = "unavailable";
      else if (result.destination_choice || result.picker_result_id) {
        setupUi.destinationChoice = result.destination_choice ?? result.picker_result_id;
        setupUi.testedDestinationToken = null;
        setupUi.pickerState = "available";
      }
      redrawSetup();
    } catch (error) {
      setupUi.pickerState = error.code === "picker_unavailable" ? "unavailable" : "unwritable";
      redrawSetup();
    }
  },
  async onTestDestination(choice) {
    try {
      const result = await api.json(
        "setup-folder-test",
        "/api/v1/setup/folder/test",
        jsonBody({ draft_revision: setupUi.draft.revision, destination_choice: choice }),
      );
      setupUi.testedDestinationToken =
        result.tested_destination_token ?? result.destination_token;
      setupUi.pickerState = "tested";
      redrawSetup();
    } catch {
      setupUi.pickerState = "unwritable";
      redrawSetup();
    }
  },
  onLauncherChoice(choice) {
    setupUi.launcherChoice = choice;
    redrawSetup();
  },
  onSubmit(step) {
    submitSetupStep(step).catch(showSetupFailure);
  },
};

async function requestSetup() {
  await refreshSetup();
}

async function renderCurrent() {
  const view = state.snapshot.view;
  markNavigation(view);
  statusText("Loading local data…");
  try {
    if (view === "setup") await requestSetup();
    else if (view === "review") await requestReview(
      state.snapshot.reviewDate ? { date: state.snapshot.reviewDate } : {},
    );
    else if (view === "calendar") await requestCalendar();
    else if (view === "library") await requestLibrary();
    else if (view === "interests") await requestInterests();
    else if (view === "settings") await requestSettings();
    statusText("");
    content.focus();
  } catch (error) {
    if (error instanceof StaleResponseError || error?.name === "AbortError") return;
    const retry = () => renderCurrent();
    if (view === "setup") renderSetupError(document, content, error.message, retry);
    else renderReviewError(document, content, error.message, retry);
    statusText("The last operation did not complete.");
  }
}

function navigate(requestedView, values = {}) {
  const view = requestedView === "calendar" ? "calendar" : validatedView(requestedView);
  const stateValues = view === "calendar" ? { view: "calendar" } : values;
  state.setView(view === "calendar" ? "review" : view, stateValues);
  history.pushState(
    { view, ...(state.snapshot.reviewDate ? { reviewDate: state.snapshot.reviewDate } : {}) },
    "",
    tokenFreeViewUrl(view),
  );
  renderCurrent();
}

navigation.addEventListener("click", (event) => {
  const control = event.target.closest?.("[data-view]");
  if (control) navigate(control.dataset.view);
});

const tabId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
api.json("tab-connect", "/api/v1/tabs/connect", jsonBody({ tab_id: tabId })).catch(() => {});
const heartbeat = setInterval(() => {
  api.json("tab-heartbeat", "/api/v1/tabs/heartbeat", jsonBody({ tab_id: tabId })).catch(() => {});
}, 20_000);

async function quitApplication() {
  clearInterval(heartbeat);
  try {
    await api.json("application-quit", "/api/v1/application/quit", jsonBody({}));
  } finally {
    clearSession(sessionStorage);
    content.replaceChildren();
    const message = document.createElement("h1");
    message.textContent = "arXiv Digest is closed";
    content.append(message);
    statusText("Reopen it with the arxiv-digest command or your desktop launcher.");
  }
}

document.querySelector("#quit").addEventListener("click", quitApplication);

addEventListener("pagehide", () => {
  clearInterval(heartbeat);
  api.json("tab-disconnect", "/api/v1/tabs/disconnect", jsonBody({ tab_id: tabId })).catch(() => {});
});

addEventListener("popstate", (event) => {
  const queryView = new URLSearchParams(location.search).get("view");
  const view = queryView === "calendar" ? "calendar" : validatedView(event.state?.view ?? queryView);
  const reviewDate = typeof event.state?.reviewDate === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(event.state.reviewDate)
    ? event.state.reviewDate
    : null;
  state.setView(
    view === "calendar" ? "review" : view,
    view === "calendar"
      ? { view: "calendar" }
      : reviewDate && view === "review"
        ? { reviewDate }
        : {},
  );
  renderCurrent();
});

renderCurrent();
