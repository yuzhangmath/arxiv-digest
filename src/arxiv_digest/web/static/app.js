import { ApiClient, StaleResponseError } from "./api.mjs";
import { renderCalendar } from "./calendar_view.mjs";
import {
  InterestsController,
  InterestsDraft,
  renderInterestsView,
} from "./interests_view.mjs";
import { LibraryController, renderLibraryView } from "./library_view.mjs";
import {
  renderReviewError,
  renderReviewHome,
  renderReviewView,
} from "./review_view.mjs";
import {
  SettingsController,
  renderRestoreError,
  renderRestoreInspection,
  renderSettingsView,
} from "./settings_view.mjs";
import {
  categorySetupActionLabel,
  optionalSetupActionLabel,
  renderSetupError,
  renderSetupView,
} from "./setup_view.mjs";
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

function statusTextIfChanged(message) {
  if (status.textContent !== message) status.textContent = message;
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
let applicationClosing = false;
let applicationQuitRequested = false;
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
  const setupActive = view === "setup";
  navigation.hidden = setupActive;
  for (const control of navigation.querySelectorAll("[data-view]")) {
    if (!setupActive && control.dataset.view === view) {
      control.setAttribute("aria-current", "page");
    }
    else control.removeAttribute("aria-current");
  }
}

function tokenFreeViewUrl(view) {
  return `${location.pathname}?view=${encodeURIComponent(view)}`;
}

let reviewRequestSequence = 0;
let reviewPollTimer = null;
let reviewPollGeneration = 0;

function clearReviewPoll() {
  if (reviewPollTimer !== null) clearTimeout(reviewPollTimer);
  reviewPollTimer = null;
  reviewPollGeneration += 1;
}

function reviewRequestIsCurrent(requestSequence) {
  return requestSequence === reviewRequestSequence && state.snapshot.view === "review";
}

function reviewPollIsCurrent(requestSequence, pollGeneration) {
  return pollGeneration === reviewPollGeneration &&
    reviewRequestIsCurrent(requestSequence);
}

function scheduleReviewPoll(delay = 1_000) {
  clearReviewPoll();
  const expectedSequence = reviewRequestSequence;
  const expectedPollGeneration = reviewPollGeneration;
  reviewPollTimer = setTimeout(async () => {
    reviewPollTimer = null;
    if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
    let refreshSequence = expectedSequence;
    try {
      const refresh = requestReview({});
      refreshSequence = reviewRequestSequence;
      await refresh;
    } catch (error) {
      if (error instanceof StaleResponseError || error?.name === "AbortError") return;
      if (!reviewRequestIsCurrent(refreshSequence)) return;
      showActionFailure(error);
      scheduleReviewPoll(1_500);
    }
  }, delay);
}

function scheduleReviewDateSyncRefresh(
  destination,
  expectedSequence = reviewRequestSequence,
  delay = 1_000,
) {
  clearReviewPoll();
  const expectedPollGeneration = reviewPollGeneration;
  const trackedDestination = Object.freeze({
    date: String(destination.date),
    anchor_event_id: destination.anchor_event_id ?? null,
  });
  reviewPollTimer = setTimeout(async () => {
    reviewPollTimer = null;
    if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
    try {
      const serviceStatus = await api.json("review-status", "/api/v1/status");
      if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
      if (serviceStatus?.sync?.status === "running") {
        scheduleReviewDateSyncRefresh(
          trackedDestination,
          expectedSequence,
        );
        return;
      }
      await navigateReviewDate(trackedDestination, {
        synchronizationRefresh: true,
      });
    } catch (error) {
      if (error instanceof StaleResponseError || error?.name === "AbortError") return;
      if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
      statusText("Synchronization status could not be refreshed yet. Retrying…");
      scheduleReviewDateSyncRefresh(
        trackedDestination,
        expectedSequence,
        1_500,
      );
    }
  }, delay);
}

function replaceTrackedReviewDate(date) {
  if (!state.snapshot.reviewDate) return;
  const reviewDate = String(date);
  state.setView("review", { reviewDate });
  history.replaceState(
    { view: "review", reviewDate },
    "",
    tokenFreeViewUrl("review"),
  );
}

function clearTrackedReviewDate() {
  state.setView("review");
  history.replaceState({ view: "review" }, "", tokenFreeViewUrl("review"));
}

async function waitForDownloadJob(jobId) {
  while (true) {
    if (applicationClosing) throw new StaleResponseError();
    const result = await libraryController.downloadStatus(jobId);
    if (result?.failed || result?.status === "failed") {
      const error = new Error("PDF download did not complete.");
      error.code = result?.error_code ?? "download_failed";
      throw error;
    }
    if (result?.complete || result?.status === "completed") return result;
    await new Promise((resolve) => setTimeout(resolve, 350));
  }
}

async function startReviewPdfDownload(
  arxivId,
  version,
  { saveFirst, saveVersion },
) {
  const result = await api.json(
    `paper-pdf:${arxivId}:${saveFirst ? "save" : "download"}`,
    "/api/v1/library/pdf",
    jsonBody({
      arxiv_id: arxivId,
      version,
      save_first: saveFirst,
      save_version: saveVersion,
    }),
  );
  return waitForDownloadJob(result?.job_id);
}

async function navigateReviewDate(
  destination,
  { synchronizationRefresh = false } = {},
) {
  statusText(
    synchronizationRefresh
      ? "Refreshing review after synchronization…"
      : "Loading review date…",
  );
  try {
    const rendered = await requestReview(destination);
    if (!rendered || state.snapshot.view !== "review") return false;
    replaceTrackedReviewDate(destination.date);
    const heading = content.querySelector(".review-view h1");
    heading?.scrollIntoView?.({ block: "start" });
    heading?.focus();
    statusText(
      synchronizationRefresh
        ? "Review updated after synchronization finished."
        : `Opened review for ${destination.date}.`,
    );
    return true;
  } catch (error) {
    if (error instanceof StaleResponseError || error?.name === "AbortError") return false;
    if (error?.code === "domain_not_found") {
      clearTrackedReviewDate();
      try {
        const rendered = await requestReview({});
        if (!rendered || state.snapshot.view !== "review") return false;
        content.focus();
        statusText(
          "Review dates changed while synchronization was finishing. The overview has been refreshed.",
        );
      } catch (refreshError) {
        if (
          refreshError instanceof StaleResponseError ||
          refreshError?.name === "AbortError" ||
          state.snapshot.view !== "review"
        ) return false;
        const message =
          "Review dates changed, but the overview could not be refreshed.";
        const notice = renderReviewError(document, content, message, () => {
          requestReview({}).catch(showActionFailure);
        });
        notice.querySelector("button")?.focus();
        statusText(`${message} Retry when ready.`);
      }
      return false;
    }
    const message = error?.message || "The review date could not be opened.";
    const notice = renderReviewError(document, content, message, () => {
      navigateReviewDate(destination).catch(showActionFailure);
    });
    notice.querySelector("button")?.focus();
    statusText("The review date could not be opened. Retry when ready.");
    return false;
  }
}

function renderFinishedReviewLoading(openedDay) {
  content.replaceChildren();
  const heading = document.createElement("h1");
  heading.textContent = "Review";
  const message = document.createElement("p");
  message.textContent = `Finished review for ${openedDay}. Loading the overview…`;
  content.append(heading, message);
  content.focus();
}

function showFinishedReviewRefreshFailure(openedDay) {
  if (state.snapshot.view !== "review") return;
  const message =
    `Finished review for ${openedDay}, but the Review overview could not be refreshed.`;
  const notice = renderReviewError(document, content, message, () => {
    refreshFinishedReviewOverview(openedDay).catch(showActionFailure);
  });
  notice.querySelector("button")?.focus();
  statusText(`${message} Retry when ready.`);
}

async function refreshFinishedReviewOverview(openedDay) {
  try {
    const rendered = await requestReview({});
    if (!rendered || state.snapshot.view !== "review") return false;
    content.focus();
    statusText(`Finished review for ${openedDay}.`);
    return true;
  } catch (error) {
    if (error instanceof StaleResponseError || error?.name === "AbortError") return false;
    showFinishedReviewRefreshFailure(openedDay);
    return false;
  }
}

async function requestReview(destination = {}) {
  clearReviewPoll();
  const requestSequence = ++reviewRequestSequence;
  let date = destination.date;
  if (!date) {
    const serviceStatus = await api.json("review-status", "/api/v1/status");
    if (!reviewRequestIsCurrent(requestSequence)) return false;
    const summary = await api.json("review-summary", "/api/v1/review/summary");
    if (!reviewRequestIsCurrent(requestSequence)) return false;
    const synchronizing = serviceStatus?.sync?.status === "running";
    const synchronizationPhase =
      synchronizing && serviceStatus?.sync?.phase === "enrichment"
        ? "enrichment"
        : "daily_list";
    const dailyListRetry = serviceStatus?.daily_list_retry;
    renderReviewHome(document, content, summary, {
      synchronizing,
      synchronizationPhase,
      dailyListRetry,
      dailyListProgress: serviceStatus?.daily_list_progress,
      start: (oldest) => navigateReviewDate({ date: oldest }),
      retryFailed: async () => {
        await api.json(
          "review-sync-start",
          "/api/v1/sync/start",
          jsonBody({ retry_failed_dates: true }),
        );
        statusText("Retrying failed daily-list dates…");
        return requestReview({});
      },
      pending: () => statusText("Marking all unreviewed papers as reviewed…"),
      finishAll: async (
        snapshotRevision,
        profileRevision,
        projectionRevision,
      ) => {
        const finishedHome = content.querySelector(".review-home");
        clearReviewPoll();
        const finishSequence = ++reviewRequestSequence;
        let result;
        try {
          result = await api.json(
            "review-finish-all",
            "/api/v1/review/finish",
            jsonBody({
              snapshot_revision: snapshotRevision,
              profile_revision: profileRevision,
              projection_revision: projectionRevision,
            }),
          );
        } catch (error) {
          const finishIsCurrent = reviewRequestIsCurrent(finishSequence) &&
            content.querySelector(".review-home") === finishedHome;
          if (!finishIsCurrent) return null;
          if (synchronizing) scheduleReviewPoll();
          throw error;
        }
        if (
          !reviewRequestIsCurrent(finishSequence) ||
          content.querySelector(".review-home") !== finishedHome
        ) return result;
        const reviewed = Number(result?.reviewed_count ?? 0);
        statusText(
          `Marked ${reviewed} ${reviewed === 1 ? "paper" : "papers"} as reviewed.`,
        );
        requestReview({})
          .then((rendered) => {
            if (rendered) content.focus();
          })
          .catch((error) => {
            if (error instanceof StaleResponseError || error?.name === "AbortError") return;
            if (
              state.snapshot.view !== "review" ||
              content.querySelector(".review-home") !== finishedHome
            ) return;
            const message =
              "The papers were marked as reviewed, but the Review summary could not be refreshed.";
            const notice = renderReviewError(document, content, message, () => {
              requestReview({}).catch(showActionFailure);
            });
            notice.querySelector("button")?.focus();
            statusText(message);
          });
        return result;
      },
      failure: showActionFailure,
    });
    if (synchronizing) scheduleReviewPoll();
    return true;
  }
  const parameters = new URLSearchParams({ date });
  if (destination.anchor_event_id != null) {
    parameters.set("anchor_event_id", String(destination.anchor_event_id));
  }
  if (destination.from_start === true) {
    parameters.set("from_start", "true");
  }
  const [serviceStatus, page] = await Promise.all([
    api.json("review-status", "/api/v1/status"),
    api.json("review-page", `/api/v1/review/date?${parameters}`),
  ]);
  if (!reviewRequestIsCurrent(requestSequence)) return false;
  const synchronizing = serviceStatus?.sync?.status === "running";
  const openedDestination = Object.freeze({
    date: String(page.day),
    anchor_event_id: page.anchor_event_id ?? null,
  });
  renderReviewView(document, content, page, {
    navigate: navigateReviewDate,
    overview: () => navigate("review"),
    failure: showActionFailure,
    finish: async (
      openedDay,
      snapshotRevision,
      profileRevision,
      projectionRevision,
    ) => {
      const openedView = content.querySelector(".review-view");
      clearReviewPoll();
      const finishSequence = ++reviewRequestSequence;
      let result;
      try {
        result = await api.json(
          "review-finish",
          "/api/v1/review/date/finish",
          jsonBody({
            date: openedDay,
            snapshot_revision: snapshotRevision,
            profile_revision: profileRevision,
            projection_revision: projectionRevision,
          }),
        );
      } catch (error) {
        const finishIsCurrent = reviewRequestIsCurrent(finishSequence) &&
          content.querySelector(".review-view") === openedView;
        if (!finishIsCurrent) return null;
        if (synchronizing) {
          scheduleReviewDateSyncRefresh(openedDestination, finishSequence);
        }
        throw error;
      }
      if (
        !reviewRequestIsCurrent(finishSequence) ||
        content.querySelector(".review-view") !== openedView
      ) return result;
      clearTrackedReviewDate();
      const nextDate = result?.next_later_unreviewed_date;
      if (typeof nextDate === "string" && nextDate) {
        const opened = await navigateReviewDate({
          date: nextDate,
          from_start: true,
        });
        if (opened) {
          statusText(`Finished review for ${openedDay}. Opening ${nextDate}.`);
        }
      } else {
        renderFinishedReviewLoading(openedDay);
        await refreshFinishedReviewOverview(openedDay);
      }
      return result;
    },
    paperActions: {
      save: (arxivId, version) => api.json(
        `paper-save:${arxivId}:${String(version ?? "latest")}`,
        "/api/v1/library/save",
        jsonBody({ arxiv_id: arxivId, version: version || null }),
      ),
      download: (arxivId, version) => startReviewPdfDownload(
        arxivId,
        version,
        { saveFirst: false, saveVersion: null },
      ),
      saveAndDownload: (
        arxivId,
        saveVersion,
        downloadVersion = saveVersion,
      ) => startReviewPdfDownload(
        arxivId,
        downloadVersion,
        { saveFirst: true, saveVersion },
      ),
      failure: showActionFailure,
    },
  });
  if (page.anchor_event_id != null) {
    api.json(
      "review-position",
      "/api/v1/review/date/position",
      jsonRequest("PUT", {
        date: String(page.day),
        snapshot_revision: Number(page.snapshot_revision),
        profile_revision: Number(page.profile_revision),
        projection_revision: Number(page.projection_revision),
        anchor_event_id: Number(page.anchor_event_id),
      }),
    ).catch(() => {});
  }
  if (synchronizing) {
    scheduleReviewDateSyncRefresh(openedDestination, requestSequence);
  }
  return true;
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
  if (applicationClosing) return;
  const arxivId = libraryJobPaper.get(jobId);
  const result = await libraryController.downloadStatus(jobId);
  if (applicationClosing) return;
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
  if (applicationClosing) return;
  const result = await libraryController.download(arxivId, version);
  if (applicationClosing) return;
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
    seed_paper_details: profile?.seed_paper_details ?? [],
    suggestions: payload?.suggestions ?? {},
    suggestions_generated_at: payload?.suggestions_generated_at,
    coverage_min: profile?.coverage_min,
    coverage_max: profile?.coverage_max,
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
      statusText("Suggestions refreshed. Nothing changes until you update interests.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async save(draft) {
    try {
      const payload = await interestsController.save(draft);
      interestsModel = interestsPayloadModel(payload);
      redrawInterests();
      statusText("Interests updated.");
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
let settingsPickerDisplayName = null;
let settingsPickerUnavailable = false;
let settingsSyncPollTimer = null;
let settingsSyncPollGeneration = 0;

function clearSettingsSyncPoll() {
  if (settingsSyncPollTimer !== null) clearTimeout(settingsSyncPollTimer);
  settingsSyncPollTimer = null;
  settingsSyncPollGeneration += 1;
}

function settingsSyncPollIsCurrent(generation) {
  return generation === settingsSyncPollGeneration &&
    state.snapshot.view === "settings" &&
    !applicationClosing;
}

function scheduleSettingsSyncPoll(delay = 1_000) {
  if (settingsSyncPollTimer !== null) clearTimeout(settingsSyncPollTimer);
  const generation = settingsSyncPollGeneration;
  settingsSyncPollTimer = setTimeout(async () => {
    settingsSyncPollTimer = null;
    if (!settingsSyncPollIsCurrent(generation)) return;
    try {
      const serviceStatus = await api.json(
        "settings-sync-status",
        "/api/v1/status",
      );
      if (!settingsSyncPollIsCurrent(generation)) return;
      if (serviceStatus?.sync?.status === "running") {
        const retry = serviceStatus?.daily_list_retry;
        if (settingsModel && retry?.status === "running") {
          settingsModel = {
            ...settingsModel,
            synchronizing: true,
            daily_list_retry: retry,
          };
          redrawSettings();
          statusText(
            `Retrying failed daily-list dates… ${Number(retry.completed ?? 0)} of ${Number(retry.total ?? 0)} completed`,
          );
        }
        scheduleSettingsSyncPoll();
        return;
      }
      await requestSettings();
      if (settingsSyncPollIsCurrent(generation)) {
        statusText("Synchronization finished.");
      }
    } catch (error) {
      if (error instanceof StaleResponseError || error?.name === "AbortError") return;
      if (!settingsSyncPollIsCurrent(generation)) return;
      statusText("Synchronization status could not be refreshed yet. Retrying…");
      scheduleSettingsSyncPoll(1_500);
    }
  }, delay);
}

function redrawSettings() {
  if (!settingsModel) return;
  renderSettingsView(
    document,
    content,
    {
      ...settingsModel,
      picker_choice: settingsPickerChoice,
      picker_display_name: settingsPickerDisplayName,
      picker_unavailable: settingsPickerUnavailable,
    },
    settingsActions,
  );
}

async function requestSettings() {
  const generation = settingsSyncPollGeneration;
  const [settings, doctor, launcher] = await Promise.all([
    api.json("settings-load", "/api/v1/settings"),
    settingsController.doctor(),
    settingsController.launcherStatus(),
  ]);
  if (!settingsSyncPollIsCurrent(generation)) return;
  settingsModel = { ...settings, doctor, launcher };
  redrawSettings();
  if (settings.synchronizing === true) scheduleSettingsSyncPoll();
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
          showRestoreInspection({
            ...inspection,
            picker_choice: choice,
            picker_display_name: result?.display_name ?? null,
            picker_unavailable: false,
          });
        } else if (result?.unavailable === true) {
          showRestoreInspection({
            ...inspection,
            picker_choice: null,
            picker_display_name: null,
            picker_unavailable: true,
          });
          statusText("The native folder picker is unavailable. Choose a fallback folder.");
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
  async retrySynchronization(count = 0) {
    if (!settingsModel || settingsModel.synchronizing === true) return;
    settingsModel = {
      ...settingsModel,
      synchronizing: true,
      daily_list_retry: {
        status: "running",
        completed: 0,
        total: Number.isSafeInteger(count) && count > 0 ? count : 0,
      },
    };
    redrawSettings();
    statusText("Retrying failed daily-list dates…");
    try {
      await settingsController.retrySynchronization();
      scheduleSettingsSyncPoll(0);
    } catch (error) {
      settingsModel = {
        ...settingsModel,
        synchronizing: false,
        daily_list_retry: {
          status: "idle",
          completed: 0,
          total: Number.isSafeInteger(count) && count > 0 ? count : 0,
        },
      };
      redrawSettings();
      showActionFailure(error);
    }
  },
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
        settingsPickerDisplayName = result?.display_name ?? null;
        settingsPickerUnavailable = false;
        redrawSettings();
        statusText("Folder selected. Test it before saving.");
      } else if (result?.unavailable === true) {
        settingsPickerUnavailable = true;
        redrawSettings();
        statusText("The native folder picker is unavailable. Choose a fallback folder.");
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
      settingsPickerDisplayName = null;
      settingsPickerUnavailable = false;
      await requestSettings();
      statusText("PDF destination saved.");
    } catch (error) {
      const pickerChoice = /^picker_[A-Za-z0-9_-]{8,120}$/.test(String(choice));
      if (pickerChoice) {
        settingsPickerChoice = null;
        settingsPickerDisplayName = null;
      }
      try {
        await requestSettings();
      } catch {
        redrawSettings();
      }
      if (pickerChoice) {
        statusText(`${error?.message || "The folder could not be used."} Choose the folder again.`);
      } else showActionFailure(error);
    }
  },
  async extendCoverage(category, newStart) {
    try {
      await settingsController.extendCoverage(
        category,
        newStart,
        Number(settingsModel?.revision),
      );
      await requestSettings();
      statusText(`Historical coverage for ${category} was extended.`);
    } catch (error) {
      showActionFailure(error);
    }
  },
  async clearCache() {
    try {
      await settingsController.clearCache(true);
      statusText(
        "Suggestion cache deleted. Interests, synchronization checkpoints, review progress, Library papers, and downloaded PDFs were kept.",
      );
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
  categoryQuery: "",
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
    terms: [],
    authors: [],
  },
  coverageStart: "",
  destinationChoice: null,
  destinationDisplayName: null,
  testedDestinationToken: null,
  pickerState: "available",
  corpusJob: null,
  corpusPollJobId: null,
  corpusPollGeneration: 0,
  lifecycleGeneration: 0,
  launcherChoice: null,
};

function setupLifecycleIsActive(generation) {
  return !applicationClosing &&
    state.snapshot.view === "setup" &&
    setupUi.lifecycleGeneration === generation;
}

function invalidateSetupLifecycle() {
  setupUi.lifecycleGeneration += 1;
  invalidateCorpusPolling();
}

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
  const observedCorpusJob = setupUi.corpusJob ?? draft.corpus_job;
  const corpusJob = observedCorpusJob
    ? {
        ...observedCorpusJob,
        can_resume:
          observedCorpusJob.can_resume ?? draft.corpus_can_resume === true,
      }
    : {
        complete: draft.corpus_complete === true,
        corpus_hash: draft.corpus_hash,
        reduced_breadth: draft.corpus_reduced_breadth === true,
        can_resume: draft.corpus_can_resume === true,
      };
  return {
    ...draft,
    categorySearchQuery: setupUi.categoryQuery,
    categorySelectionCount: setupUi.categorySelections.size,
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
    corpusJob,
    paperOptions: setupUi.paperOptions.map((option) => optionWithChecked("seed_papers", option)),
    keywordOptions: setupUi.keywordOptions.map((option) => optionWithChecked("keywords", option)),
    phraseOptions: setupUi.phraseOptions.map((option) => optionWithChecked("phrases", option)),
    authorOptions: setupUi.authorOptions.map((option) => optionWithChecked("authors", option)),
    acceptedSelectionCounts: {
      seed_papers: setupUi.accepted.seed_papers.size,
      terms: setupUi.accepted.keywords.size + setupUi.accepted.phrases.size,
      authors: setupUi.accepted.authors.size,
    },
    customPaperIds: setupUi.custom.paper_ids,
    customTerms: setupUi.custom.terms,
    customAuthors: setupUi.custom.authors,
    destinationChoice: setupUi.destinationChoice,
    destinationDisplayName: setupUi.destinationDisplayName,
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

function focusCustomEntry(index) {
  const inputs = [...content.querySelectorAll(".custom-entries input")];
  const target = inputs[index] ?? content.querySelector("[data-custom-add]");
  target?.focus();
}

function syncSetupContinueLabel() {
  const label = optionalSetupActionLabel(setupModel());
  const control = content.querySelector("[data-setup-continue]");
  if (label && control) control.textContent = label;
}

function syncSetupCategoryAction() {
  const control = content.querySelector("[data-setup-continue]");
  if (!control) return;
  const count = setupUi.categorySelections.size;
  control.textContent = categorySetupActionLabel(count);
  control.disabled = count === 0;
  const requirement = content.querySelector(".required-guidance");
  if (requirement) requirement.hidden = count > 0;
}

function replaceOptions(kind, payload) {
  const items = Array.isArray(payload)
    ? payload
    : payload?.items ?? payload?.suggestions ?? payload?.categories ?? [];
  if (kind === "categories") setupUi.categoryOptions = items;
  else if (kind === "seed_papers") setupUi.paperOptions = items;
  else if (kind === "authors") setupUi.authorOptions = items;
}

async function loadSetupOptions(
  query = "",
  lifecycleGeneration = setupUi.lifecycleGeneration,
) {
  if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
  const step = setupStep();
  if (step === "categories") {
    const payload = await api.json(
      "setup-categories",
      `/api/v1/categories?q=${encodeURIComponent(query)}`,
    );
    if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
    setupUi.categoryQuery = String(query);
    replaceOptions("categories", payload);
  } else if (step === "seed_papers") {
    const payload = await api.json(
      "setup-papers",
      `/api/v1/setup/candidates/papers?q=${encodeURIComponent(query)}&offset=0`,
    );
    if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
    replaceOptions("seed_papers", payload);
  } else if (step === "keywords_and_phrases" || step === "terms") {
    const payload = await api.json("setup-terms", "/api/v1/setup/candidates/terms");
    if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
    setupUi.keywordOptions = payload?.keywords ?? [];
    setupUi.phraseOptions = payload?.phrases ?? [];
  } else if (step === "authors") {
    const payload = await api.json(
      "setup-authors",
      `/api/v1/setup/candidates/authors?q=${encodeURIComponent(query)}`,
    );
    if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
    replaceOptions("authors", payload);
  }
  return setupLifecycleIsActive(lifecycleGeneration);
}

async function refreshSetup({
  loadOptions = true,
  lifecycleGeneration = setupUi.lifecycleGeneration,
} = {}) {
  const draft = await api.json("setup-draft", "/api/v1/setup/draft");
  if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
  setupUi.draft = draft;
  setupUi.corpusJob = setupUi.draft?.corpus_job ?? null;
  setupUi.coverageStart ||= setupUi.draft.coverage_start ?? "";
  if (loadOptions && !await loadSetupOptions("", lifecycleGeneration)) return false;
  if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
  redrawSetup();
  const recoveredJob = setupUi.draft?.corpus_job;
  if (
    recoveredJob?.status === "running" &&
    typeof recoveredJob.job_id === "string"
  ) {
    startCorpusPolling(recoveredJob.job_id);
  }
  return true;
}

function submittedCustomValues(kind) {
  const values = setupUi.custom[kind];
  return Array.isArray(values)
    ? values
        .filter((value) => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean)
    : [];
}

function customTermWordCount(value) {
  const normalized = String(value)
    .normalize("NFKC")
    .replace(/[^\p{Letter}\p{Number}\p{Mark}]+/gu, " ")
    .trim();
  return normalized === "" ? 0 : normalized.split(/\s+/u).length;
}

async function submitSetupStep(step) {
  const lifecycleGeneration = setupUi.lifecycleGeneration;
  if (!setupLifecycleIsActive(lifecycleGeneration)) return;
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
      custom_arxiv_ids: submittedCustomValues("paper_ids"),
    };
  } else if (step === "terms") {
    const customTerms = submittedCustomValues("terms");
    payload = {
      revision,
      step,
      accepted_keyword_suggestion_ids: [...setupUi.accepted.keywords],
      accepted_phrase_suggestion_ids: [...setupUi.accepted.phrases],
      custom_keywords: customTerms.filter((value) => customTermWordCount(value) === 1),
      custom_phrases: customTerms.filter((value) => customTermWordCount(value) !== 1),
    };
  } else if (step === "authors") {
    payload = {
      revision,
      step,
      accepted_suggestion_ids: [...setupUi.accepted.authors],
      custom_authors: submittedCustomValues("authors"),
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
      if (setupLifecycleIsActive(lifecycleGeneration)) {
        statusText("Setup is complete. Synchronization can be retried from Settings.");
      }
    }
    if (setupLifecycleIsActive(lifecycleGeneration)) navigate("review");
    return;
  } else {
    return;
  }
  const updated = await api.json(
    "setup-update",
    "/api/v1/setup/draft",
    jsonRequest("PUT", payload),
  );
  if (!setupLifecycleIsActive(lifecycleGeneration)) return;
  setupUi.draft = updated?.revision == null ? null : updated;
  await refreshSetup({ lifecycleGeneration });
}

function corpusPollIsActive(jobId, generation, lifecycleGeneration) {
  return setupUi.corpusPollJobId === jobId &&
    setupUi.corpusPollGeneration === generation &&
    setupLifecycleIsActive(lifecycleGeneration);
}

function invalidateCorpusPolling() {
  setupUi.corpusPollGeneration += 1;
  setupUi.corpusPollJobId = null;
}

function corpusJobPresentationKey(job) {
  return JSON.stringify([
    job?.status,
    job?.complete,
    job?.failed,
    job?.message,
    job?.error_code,
    job?.corpus_complete,
    job?.minimum_met,
    job?.setup_ready,
    job?.can_resume,
    job?.corpus_hash,
    job?.reduced_breadth,
    job?.pages_fetched,
    job?.progress,
  ]);
}

async function pollCorpusJob(jobId, generation, lifecycleGeneration) {
  if (!corpusPollIsActive(jobId, generation, lifecycleGeneration)) return;
  const job = await api.json("setup-job", `/api/v1/setup/jobs/${encodeURIComponent(jobId)}`);
  if (!corpusPollIsActive(jobId, generation, lifecycleGeneration)) return;
  if (!job.complete && !job.failed) {
    const presentationChanged =
      corpusJobPresentationKey(setupUi.corpusJob) !== corpusJobPresentationKey(job);
    setupUi.corpusJob = job;
    if (presentationChanged) redrawSetup();
    statusTextIfChanged("Generating candidate corpus…");
    setTimeout(() => pollCorpusJob(jobId, generation, lifecycleGeneration).catch((error) =>
      handleCorpusPollFailure(jobId, generation, lifecycleGeneration, error),
    ), 300);
  } else {
    invalidateCorpusPolling();
    statusText("Checking corpus status…");
    try {
      const refreshed = await refreshSetup({
        loadOptions: false,
        lifecycleGeneration,
      });
      if (!refreshed) return;
      const current = setupUi.corpusJob;
      if (current?.status === "running") {
        statusTextIfChanged("Generating candidate corpus…");
      } else if (current?.failed) {
        statusText("Corpus generation did not complete.");
      } else {
        statusText("Corpus generation finished.");
      }
    } catch (error) {
      if (setupLifecycleIsActive(lifecycleGeneration)) showSetupFailure(error);
    }
  }
}

function handleCorpusPollFailure(jobId, generation, lifecycleGeneration, error) {
  if (!corpusPollIsActive(jobId, generation, lifecycleGeneration)) return;
  invalidateCorpusPolling();
  if (error instanceof StaleResponseError || error?.name === "AbortError") return;
  if (!setupLifecycleIsActive(lifecycleGeneration)) return;
  showSetupFailure(error);
}

function startCorpusPolling(jobId) {
  const lifecycleGeneration = setupUi.lifecycleGeneration;
  if (!setupLifecycleIsActive(lifecycleGeneration)) return;
  if (setupUi.corpusPollJobId === jobId) return;
  invalidateCorpusPolling();
  setupUi.corpusPollJobId = jobId;
  const generation = setupUi.corpusPollGeneration;
  pollCorpusJob(jobId, generation, lifecycleGeneration).catch((error) =>
    handleCorpusPollFailure(jobId, generation, lifecycleGeneration, error),
  );
}

function showSetupFailure(error) {
  if (error instanceof StaleResponseError || error?.name === "AbortError") return;
  if (state.snapshot.view !== "setup") return;
  if (error?.code === "already_configured") {
    replaceCurrentView("interests").catch(showActionFailure);
    return;
  }
  renderSetupError(document, content, error.message, () => {
    refreshSetup().catch(showSetupFailure);
  });
}

const setupActions = {
  async onSearch(kind, query) {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    try {
      if (kind === "categories") {
        const payload = await api.json(
          "setup-categories",
          `/api/v1/categories?q=${encodeURIComponent(query)}`,
        );
        if (!setupLifecycleIsActive(lifecycleGeneration)) return;
        setupUi.categoryQuery = String(query);
        replaceOptions(kind, payload);
      } else if (kind === "seed_papers") {
        const payload = await api.json(
          "setup-papers",
          `/api/v1/setup/candidates/papers?q=${encodeURIComponent(query)}&offset=0`,
        );
        if (!setupLifecycleIsActive(lifecycleGeneration)) return;
        replaceOptions(kind, payload);
      } else if (kind === "authors") {
        const payload = await api.json(
          "setup-authors",
          `/api/v1/setup/candidates/authors?q=${encodeURIComponent(query)}`,
        );
        if (!setupLifecycleIsActive(lifecycleGeneration)) return;
        replaceOptions(kind, payload);
      }
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      redrawSetup();
    } catch (error) {
      if (setupLifecycleIsActive(lifecycleGeneration)) showSetupFailure(error);
    }
  },
  onSuggestion(kind, stable, checked) {
    if (kind === "categories") {
      const key = `${stable.category}\u0000${stable.set_spec}`;
      if (checked) setupUi.categorySelections.set(key, stable);
      else setupUi.categorySelections.delete(key);
      syncSetupCategoryAction();
    } else {
      if (checked) setupUi.accepted[kind].add(stable);
      else setupUi.accepted[kind].delete(stable);
      syncSetupContinueLabel();
    }
  },
  onCustomAdd(kind) {
    setupUi.custom[kind].push("");
    redrawSetup();
    focusCustomEntry(setupUi.custom[kind].length - 1);
  },
  onCustomChange(kind, index, value) {
    setupUi.custom[kind][index] = value;
    syncSetupContinueLabel();
  },
  onCustomRemove(kind, index) {
    setupUi.custom[kind].splice(index, 1);
    redrawSetup();
    focusCustomEntry(Math.min(index, setupUi.custom[kind].length - 1));
  },
  onCoverageChange(value) {
    setupUi.coverageStart = value;
    redrawSetup();
  },
  async onCorpus(mode) {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    if (!setupLifecycleIsActive(lifecycleGeneration)) return;
    if (["starting", "running"].includes(setupUi.corpusJob?.status)) return;
    setupUi.corpusJob = {
      status: "starting",
      complete: false,
      failed: false,
    };
    statusText("Starting candidate corpus generation…");
    redrawSetup();
    try {
      const result = await api.json(
        "setup-corpus",
        "/api/v1/setup/corpus",
        jsonBody({ draft_revision: setupUi.draft.revision, mode }),
      );
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      setupUi.corpusJob = {
        status: "running",
        complete: false,
        failed: false,
      };
      statusText("Generating candidate corpus…");
      redrawSetup();
      startCorpusPolling(result.job_id);
    } catch (error) {
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      setupUi.corpusJob = {
        status: "failed",
        complete: false,
        failed: true,
        can_resume: setupUi.draft?.corpus_can_resume === true,
        message: error?.message || "The background operation did not complete.",
      };
      statusText("Corpus generation did not complete.");
      redrawSetup();
    }
  },
  async onCorpusAccept(corpusHash) {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    try {
      await api.json(
        "setup-corpus-accept",
        "/api/v1/setup/corpus/accept",
        jsonBody({
          draft_revision: setupUi.draft.revision,
          corpus_hash: corpusHash,
        }),
      );
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      setupUi.corpusJob = null;
      await refreshSetup({ lifecycleGeneration });
    } catch (error) {
      if (setupLifecycleIsActive(lifecycleGeneration)) showSetupFailure(error);
    }
  },
  async onPickDestination() {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    try {
      const result = await api.json(
        "setup-folder-pick",
        "/api/v1/setup/folder/pick",
        jsonBody({ draft_revision: setupUi.draft.revision }),
      );
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      if (result.cancelled) setupUi.pickerState = "cancelled";
      else if (result.unavailable) setupUi.pickerState = "unavailable";
      else if (result.destination_choice || result.picker_result_id) {
        setupUi.destinationChoice = result.destination_choice ?? result.picker_result_id;
        setupUi.destinationDisplayName =
          typeof result.display_name === "string" ? result.display_name : null;
        setupUi.testedDestinationToken = null;
        setupUi.pickerState = "available";
      }
      redrawSetup();
    } catch (error) {
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      setupUi.pickerState = error.code === "picker_unavailable" ? "unavailable" : "unwritable";
      redrawSetup();
    }
  },
  async onTestDestination(choice) {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    try {
      const result = await api.json(
        "setup-folder-test",
        "/api/v1/setup/folder/test",
        jsonBody({ draft_revision: setupUi.draft.revision, destination_choice: choice }),
      );
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      const token = result.tested_destination_token ?? result.destination_token;
      if (
        typeof token !== "string" ||
        !/^destination_[A-Za-z0-9_-]{8,112}$/.test(token)
      ) {
        throw new TypeError("The folder test returned an invalid confirmation");
      }
      setupUi.testedDestinationToken = token;
      setupUi.pickerState = "tested";
      redrawSetup();
    } catch {
      if (!setupLifecycleIsActive(lifecycleGeneration)) return;
      // Custom picker choices are one-use server-side, including failed tests.
      const customPickerChoice = /^picker_[A-Za-z0-9_-]{8,120}$/.test(String(choice));
      if (customPickerChoice) {
        setupUi.destinationChoice = null;
        setupUi.destinationDisplayName = null;
      }
      setupUi.testedDestinationToken = null;
      setupUi.pickerState = customPickerChoice ? "unwritable" : "unavailable";
      redrawSetup();
    }
  },
  onLauncherChoice(choice) {
    setupUi.launcherChoice = choice;
    redrawSetup();
  },
  onSubmit(step) {
    const lifecycleGeneration = setupUi.lifecycleGeneration;
    submitSetupStep(step).catch((error) => {
      if (setupLifecycleIsActive(lifecycleGeneration)) showSetupFailure(error);
    });
  },
};

async function requestSetup(lifecycleGeneration) {
  await refreshSetup({ lifecycleGeneration });
}

let renderCurrentSequence = 0;

async function renderCurrent() {
  if (applicationClosing) return;
  const renderSequence = ++renderCurrentSequence;
  const view = state.snapshot.view;
  if (view !== "review") {
    clearReviewPoll();
    reviewRequestSequence += 1;
  }
  if (view !== "settings") clearSettingsSyncPoll();
  if (view !== "setup") invalidateSetupLifecycle();
  const setupGeneration = setupUi.lifecycleGeneration;
  markNavigation(view);
  statusText("Loading local data…");
  try {
    if (view === "setup") await requestSetup(setupGeneration);
    else if (view === "review") await requestReview(
      state.snapshot.reviewDate ? { date: state.snapshot.reviewDate } : {},
    );
    else if (view === "calendar") await requestCalendar();
    else if (view === "library") await requestLibrary();
    else if (view === "interests") await requestInterests();
    else if (view === "settings") await requestSettings();
    if (
      renderSequence !== renderCurrentSequence ||
      state.snapshot.view !== view
    ) return;
    if (view !== "setup" || setupLifecycleIsActive(setupGeneration)) {
      if (view === "setup" && setupUi.corpusJob?.status === "running") {
        statusTextIfChanged("Generating candidate corpus…");
      } else {
        statusText("");
      }
      content.focus();
    }
  } catch (error) {
    if (error instanceof StaleResponseError || error?.name === "AbortError") return;
    if (
      renderSequence !== renderCurrentSequence ||
      state.snapshot.view !== view
    ) return;
    if (view === "setup" && !setupLifecycleIsActive(setupGeneration)) return;
    if (view === "setup" && error?.code === "already_configured") {
      await replaceCurrentView("interests");
      return;
    }
    const retry = () => renderCurrent();
    if (view === "setup") renderSetupError(document, content, error.message, retry);
    else {
      const notice = renderReviewError(document, content, error.message, retry);
      notice.querySelector("button")?.focus();
    }
    statusText("The last operation did not complete.");
  }
}

async function replaceCurrentView(requestedView, values = {}) {
  if (applicationClosing) return;
  const view = validatedView(requestedView);
  state.setView(view, values);
  history.replaceState({ view }, "", tokenFreeViewUrl(view));
  await renderCurrent();
}

function navigate(requestedView, values = {}) {
  if (applicationClosing) return;
  const view = requestedView === "calendar" ? "calendar" : validatedView(requestedView);
  const stateValues = view === "calendar" ? { view: "calendar" } : values;
  state.setView(view === "calendar" ? "review" : view, stateValues);
  history.pushState(
    { view, ...(state.snapshot.reviewDate ? { reviewDate: state.snapshot.reviewDate } : {}) },
    "",
    tokenFreeViewUrl(view),
  );
  return renderCurrent();
}

navigation.addEventListener("click", (event) => {
  if (state.snapshot.view === "setup") return;
  const control = event.target.closest?.("[data-view]");
  if (control) navigate(control.dataset.view);
});

const tabId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
let heartbeat = null;
let tabDisconnect = Promise.resolve();

function connectTab() {
  api.json("tab-connect", "/api/v1/tabs/connect", jsonBody({ tab_id: tabId })).catch(() => {});
  if (heartbeat === null) {
    heartbeat = setInterval(() => {
      api.json("tab-heartbeat", "/api/v1/tabs/heartbeat", jsonBody({ tab_id: tabId })).catch(() => {});
    }, 20_000);
  }
}

function stopHeartbeat() {
  if (heartbeat !== null) clearInterval(heartbeat);
  heartbeat = null;
}

connectTab();

async function quitApplication() {
  if (applicationClosing) return;
  applicationClosing = true;
  applicationQuitRequested = true;
  stopHeartbeat();
  clearSettingsSyncPoll();
  invalidateSetupLifecycle();
  api.abortAll();
  for (const control of document.querySelectorAll("button")) {
    control.disabled = true;
  }
  statusText("Closing arXiv Digest…");
  try {
    await api.json(
      "application-quit",
      "/api/v1/application/quit",
      { ...jsonBody({}), keepalive: true },
    );
  } finally {
    api.abortAll();
    invalidateSetupLifecycle();
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
  clearSettingsSyncPoll();
  if (applicationQuitRequested) return;
  applicationClosing = true;
  stopHeartbeat();
  invalidateSetupLifecycle();
  api.abortAll();
  tabDisconnect = api.json(
    "tab-disconnect",
    "/api/v1/tabs/disconnect",
    jsonBody({ tab_id: tabId }),
  ).catch(() => {});
});

addEventListener("pageshow", async (event) => {
  if (!event.persisted || applicationQuitRequested) return;
  applicationClosing = false;
  connectTab();
  renderCurrent();
  await tabDisconnect;
  if (!applicationClosing && !applicationQuitRequested) connectTab();
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
