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
  updateReviewAbstractStatus,
} from "./review_view.mjs";
import {
  SettingsController,
  arxivAccessPauseText,
  renderSettingsView,
} from "./settings_view.mjs";
import {
  categorySetupActionLabel,
  renderSetupError,
  renderSetupView,
} from "./setup_view.mjs";
import {
  ViewState,
  bootstrapSession,
  clearSession,
  validatedView,
} from "./state.mjs";
import { renderUpdateNotice } from "./update_view.mjs";
import { ViewLifecycle } from "./view_lifecycle.mjs";

const content = document.querySelector("#content");
const status = document.querySelector("#status");
const navigation = document.querySelector(".primary-navigation");
const updateNotice = document.querySelector("#update-notice");

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
let applicationClosing = false;
let applicationQuitRequested = false;
const viewLifecycle = new ViewLifecycle(() => !applicationClosing);
const downloadLifecycle = new ViewLifecycle(() => !applicationClosing);
const api = new ApiClient(location.origin, session.token, fetch, () => {
  clearSession(sessionStorage);
  statusText("Your local session expired. Reopen arXiv Digest to continue.");
});
const libraryController = new LibraryController(api);
const interestsController = new InterestsController(api);
const settingsController = new SettingsController(api);

// Observe the background release check without delaying dashboard startup.
const updateDeadline = performance.now() + 65_000;
let updateTimer = null;

function stopUpdateNotice() {
  clearTimeout(updateTimer);
  updateTimer = null;
  api.abort("release-update");
}

async function refreshUpdateNotice() {
  if (applicationClosing) return;
  stopUpdateNotice();
  const fallback = { status: "manual_fallback" };
  const timeout = setTimeout(() => {
    api.abort("release-update");
    renderUpdateNotice(document, updateNotice, fallback);
  }, 5_000);
  updateTimer = timeout;
  let update;
  try {
    update = await api.json("release-update", "/api/v1/update");
  } catch (error) {
    if (applicationClosing || error instanceof StaleResponseError ||
        error?.name === "AbortError" || error?.status === 401) return;
    update = fallback;
  } finally {
    clearTimeout(timeout);
  }
  if (applicationClosing) return;
  if (["idle", "checking"].includes(update?.status)) {
    if (performance.now() < updateDeadline) {
      updateTimer = setTimeout(refreshUpdateNotice, 1_000);
      return;
    }
    update = fallback;
  }
  renderUpdateNotice(document, updateNotice, update);
}

function jsonBody(value) {
  return jsonRequest("POST", value);
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

const reviewRequests = new ViewLifecycle(
  () => !applicationClosing && state.snapshot.view === "review",
);
const reviewPolling = new ViewLifecycle(
  () => !applicationClosing && state.snapshot.view === "review",
);

function clearReviewPoll() {
  reviewPolling.invalidate();
}

function reviewRequestIsCurrent(requestSequence) {
  return reviewRequests.isCurrent(requestSequence);
}

function reviewPollIsCurrent(requestSequence, pollGeneration) {
  return reviewPolling.isCurrent(pollGeneration) &&
    reviewRequestIsCurrent(requestSequence);
}

function scheduleReviewPoll(delay = 1_000) {
  clearReviewPoll();
  const expectedSequence = reviewRequests.capture();
  const expectedPollGeneration = reviewPolling.capture();
  reviewPolling.schedule(async () => {
    if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
    let refreshSequence = expectedSequence;
    try {
      const refresh = requestReview({});
      refreshSequence = reviewRequests.capture();
      await refresh;
    } catch (error) {
      if (error instanceof StaleResponseError || error?.name === "AbortError") return;
      if (!reviewRequestIsCurrent(refreshSequence)) return;
      showActionFailure(error);
      scheduleReviewPoll(1_500);
    }
  }, delay);
}

function arxivAccessRefreshDelay(access) {
  const retryAt = typeof access?.retry_at === "string" ? Date.parse(access.retry_at) : NaN;
  return Number.isFinite(retryAt)
    ? Math.max(1_000, Math.min(60_000, retryAt - Date.now() + 100))
    : 60_000;
}

function scheduleReviewDateSyncRefresh(
  destination,
  expectedSequence = reviewRequests.capture(),
  delay = 1_000,
  updateStatus = null,
) {
  clearReviewPoll();
  const expectedPollGeneration = reviewPolling.capture();
  const trackedDestination = Object.freeze({
    date: String(destination.date),
    anchor_event_id: destination.anchor_event_id ?? null,
  });
  reviewPolling.schedule(async () => {
    if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
    try {
      const serviceStatus = await api.json("review-status", "/api/v1/status");
      if (!reviewPollIsCurrent(expectedSequence, expectedPollGeneration)) return;
      updateStatus?.(serviceStatus);
      if (serviceStatus?.sync?.status === "running") {
        scheduleReviewDateSyncRefresh(
          trackedDestination,
          expectedSequence,
          1_000,
          updateStatus,
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
        updateStatus,
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
  const requestSequence = reviewRequests.invalidate();
  let date = destination.date;
  if (!date) {
    const serviceStatus = await reviewRequests.wait(
      api.json("review-status", "/api/v1/status"), requestSequence,
    );
    if (!reviewRequestIsCurrent(requestSequence)) return false;
    const summary = await reviewRequests.wait(
      api.json("review-summary", "/api/v1/review/summary"), requestSequence,
    );
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
      arxivAccess: serviceStatus?.arxiv_access,
      start: (oldest) => navigateReviewDate({ date: oldest }),
      retryFailed: async () => {
        await reviewRequests.wait(api.json(
          "review-sync-start",
          "/api/v1/sync/start",
          jsonBody({ retry_failed_dates: true }),
        ));
        statusText("Retrying failed daily-list dates…");
        return requestReview({});
      },
      pending: () => statusText("Marking all unreviewed papers as reviewed…"),
      finishAll: async (
        snapshotRevision,
        profileRevision,
        projectionRevision,
        throughDate,
      ) => {
        const finishedHome = content.querySelector(".review-home");
        clearReviewPoll();
        const finishSequence = reviewRequests.invalidate();
        let result;
        try {
          result = await api.json(
            "review-finish-all",
            "/api/v1/review/finish",
            jsonBody({
              snapshot_revision: snapshotRevision,
              profile_revision: profileRevision,
              projection_revision: projectionRevision,
              ...(throughDate === undefined ? {} : { through_date: throughDate }),
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
    else if (serviceStatus?.arxiv_access?.paused === true) {
      scheduleReviewPoll(arxivAccessRefreshDelay(serviceStatus.arxiv_access));
    }
    return true;
  }
  const parameters = new URLSearchParams({ date });
  if (destination.anchor_event_id != null) {
    parameters.set("anchor_event_id", String(destination.anchor_event_id));
  }
  if (destination.from_start === true) {
    parameters.set("from_start", "true");
  }
  const [serviceStatus, page] = await reviewRequests.wait(Promise.all([
    api.json("review-status", "/api/v1/status"),
    api.json("review-page", `/api/v1/review/date?${parameters}`),
  ]), requestSequence);
  if (!reviewRequestIsCurrent(requestSequence)) return false;
  const synchronizing = serviceStatus?.sync?.status === "running";
  const openedDestination = Object.freeze({
    date: String(page.day),
    anchor_event_id: page.anchor_event_id ?? null,
  });
  const reviewActions = {
    synchronizing,
    arxivAccess: serviceStatus?.arxiv_access,
    abstractRetry: serviceStatus?.abstract_retry,
    navigate: navigateReviewDate,
    overview: () => navigate("review"),
    failure: showActionFailure,
    retryAbstracts: async (openedDay) => {
      const openedView = content.querySelector(".review-view");
      const retrySequence = reviewRequests.capture();
      const isCurrent = () => reviewRequestIsCurrent(retrySequence) &&
        content.querySelector(".review-view") === openedView;
      try {
        await api.json(
          "review-sync-start",
          "/api/v1/sync/start",
          jsonBody({ retry_missing_abstracts: true, retry_date: openedDay }),
        );
      } catch (error) {
        if (isCurrent()) throw error;
        return false;
      }
      if (!isCurrent()) return false;
      statusText(`Retrying missing abstracts for ${openedDay}…`);
      return requestReview(openedDestination);
    },
    finish: async (
      openedDay,
      snapshotRevision,
      profileRevision,
      projectionRevision,
    ) => {
      const openedView = content.querySelector(".review-view");
      clearReviewPoll();
      const finishSequence = reviewRequests.invalidate();
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
          scheduleReviewDateSyncRefresh(openedDestination, finishSequence, 1_000, updateAbstractStatus);
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
  };
  const openedView = renderReviewView(document, content, page, reviewActions);
  const updateAbstractStatus = (status) => updateReviewAbstractStatus(
    document,
    openedView,
    page,
    {
      ...reviewActions,
      synchronizing: status?.sync?.status === "running",
      arxivAccess: status?.arxiv_access,
      abstractRetry: status?.abstract_retry,
    },
  );
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
    scheduleReviewDateSyncRefresh(openedDestination, requestSequence, 1_000, updateAbstractStatus);
  } else if (serviceStatus?.arxiv_access?.paused === true) {
    scheduleReviewDateSyncRefresh(
      openedDestination,
      requestSequence,
      arxivAccessRefreshDelay(serviceStatus.arxiv_access),
      updateAbstractStatus,
    );
  }
  return true;
}

async function requestCalendar() {
  const now = new Date();
  const endDate = new Date(Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
  ));
  const startDate = new Date(endDate);
  startDate.setUTCDate(startDate.getUTCDate() - 30);
  const start = startDate.toISOString().slice(0, 10);
  const end = endDate.toISOString().slice(0, 10);
  const entries = await viewLifecycle.wait(
    api.json("calendar", `/api/v1/review/calendar?start=${start}&end=${end}`),
  );
  content.replaceChildren();
  const heading = document.createElement("h1");
  heading.textContent = "Calendar";
  content.append(heading);
  if (entries.some((entry) => entry.abstracts_pending)) {
    const guidance = document.createElement("p");
    guidance.className = "calendar-guidance";
    guidance.textContent = "Missing abstracts do not prevent review. Open a date to retry its missing abstracts if you want them.";
    content.append(guidance);
  }
  if (entries.some((entry) => entry.retrieval_failed)) {
    const guidance = document.createElement("p");
    guidance.className = "calendar-guidance";
    guidance.textContent = "Failed daily lists have not been retrieved. Counts and review status cover confirmed papers only. See Settings for recovery status.";
    content.append(guidance);
  }
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
  if (!libraryPage || applicationClosing || state.snapshot.view !== "library") return;
  renderLibraryView(document, content, projectedLibraryPage(libraryPage), libraryActions);
}

function libraryStatus(message) {
  if (!applicationClosing && state.snapshot.view === "library") statusText(message);
}

function showLibraryFailure(error) {
  if (!applicationClosing && state.snapshot.view === "library") showActionFailure(error);
}

async function pollLibraryDownload(jobId) {
  const generation = downloadLifecycle.capture();
  if (!downloadLifecycle.isCurrent(generation)) return;
  const arxivId = libraryJobPaper.get(jobId);
  const result = await downloadLifecycle.wait(libraryController.downloadStatus(jobId), generation);
  if (!downloadLifecycle.isCurrent(generation)) return;
  if (arxivId) libraryDownloadState.set(arxivId, { ...result, job_id: jobId });
  redrawLibrary();
  if (result.failed || result.status === "failed") {
    libraryStatus("PDF download did not complete. You can retry it from the paper.");
    return;
  }
  if (result.complete || result.status === "completed") {
    libraryStatus("PDF download complete.");
    return;
  }
  libraryStatus("Downloading PDF…");
  downloadLifecycle.schedule(() => pollLibraryDownload(jobId).catch(showLibraryFailure), 350);
}

async function startLibraryDownload(arxivId, version) {
  const generation = downloadLifecycle.capture();
  if (!downloadLifecycle.isCurrent(generation)) return;
  const result = await downloadLifecycle.wait(libraryController.download(arxivId, version), generation);
  if (!downloadLifecycle.isCurrent(generation)) return;
  libraryJobPaper.set(result.job_id, arxivId);
  libraryDownloadState.set(arxivId, { job_id: result.job_id, status: "running" });
  redrawLibrary();
  libraryStatus("Downloading PDF…");
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
    const generation = viewLifecycle.capture();
    try {
      await libraryController.remove(arxivId);
      libraryDownloadState.delete(arxivId);
      if (!viewLifecycle.isCurrent(generation)) return;
      await requestLibrary(libraryPage?.query ?? "", libraryPage?.offset ?? 0);
      if (!viewLifecycle.isCurrent(generation)) return;
      statusText("Paper removed from the library.");
    } catch (error) {
      if (viewLifecycle.isCurrent(generation)) showActionFailure(error);
    }
  },
  downloadPdf(arxivId, version) {
    startLibraryDownload(arxivId, version).catch(showLibraryFailure);
  },
  async retryPdf(jobId) {
    const generation = downloadLifecycle.capture();
    try {
      const arxivId = libraryJobPaper.get(jobId);
      const result = await libraryController.retryDownload(jobId);
      if (!downloadLifecycle.isCurrent(generation)) return;
      if (arxivId) {
        libraryJobPaper.set(result.job_id, arxivId);
        libraryDownloadState.set(arxivId, { job_id: result.job_id, status: "running" });
      }
      redrawLibrary();
      await pollLibraryDownload(result.job_id);
    } catch (error) {
      showLibraryFailure(error);
    }
  },
};

async function requestLibrary(query = "", offset = 0) {
  libraryPage = await viewLifecycle.wait(libraryController.search(query, offset));
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
  if (!interestsModel || applicationClosing || state.snapshot.view !== "interests") return;
  renderInterestsView(document, content, interestsModel, interestsActions);
}

const interestsActions = {
  async refreshSuggestions() {
    try {
      const payload = await viewLifecycle.wait(interestsController.freshSuggestions());
      interestsModel = interestsPayloadModel(payload, interestsModel?.draft);
      redrawInterests();
      statusText("Suggestions refreshed. Nothing changes until you update interests.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async save(draft) {
    try {
      const payload = await viewLifecycle.wait(interestsController.save(draft));
      interestsModel = interestsPayloadModel(payload);
      redrawInterests();
      statusText("Interests updated.");
    } catch (error) {
      showActionFailure(error);
    }
  },
};

async function requestInterests() {
  const payload = await viewLifecycle.wait(interestsController.load());
  interestsModel = interestsPayloadModel(payload);
  redrawInterests();
}

let settingsModel = null;
let settingsPickerChoice = null;
let settingsPickerDisplayName = null;
let settingsPickerUnavailable = false;
const settingsLifecycle = new ViewLifecycle(
  () => !applicationClosing && state.snapshot.view === "settings",
);

function clearSettingsSyncPoll() {
  settingsLifecycle.invalidate();
}

function settingsSyncPollIsCurrent(generation) {
  return settingsLifecycle.isCurrent(generation);
}

function scheduleSettingsSyncPoll(delay = 1_000) {
  settingsLifecycle.cancelScheduled();
  const generation = settingsLifecycle.capture();
  settingsLifecycle.schedule(async () => {
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
            arxiv_access: serviceStatus?.arxiv_access,
          };
          redrawSettings();
          statusText(
            `Retrying failed daily-list dates… ${Number(retry.completed ?? 0)} of ${Number(retry.total ?? 0)} completed`,
          );
        }
        scheduleSettingsSyncPoll();
        return;
      }
      const wasSynchronizing = settingsModel?.synchronizing === true;
      const wasPaused = settingsModel?.arxiv_access?.paused === true;
      await requestSettings();
      if (settingsSyncPollIsCurrent(generation)) {
        if (settingsModel?.arxiv_access?.paused === true) {
          statusText(arxivAccessPauseText(settingsModel.arxiv_access));
        } else if (wasSynchronizing) {
          statusText("Synchronization finished.");
        } else if (wasPaused) {
          statusText("Retries are available again.");
        }
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
  if (!settingsModel || !settingsLifecycle.isCurrent(settingsLifecycle.capture())) return;
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
  const [settings, doctor, launcher] = await settingsLifecycle.wait(Promise.all([
    api.json("settings-load", "/api/v1/settings"),
    settingsController.doctor(),
    settingsController.launcherStatus(),
  ]));
  settingsModel = { ...settings, doctor, launcher };
  redrawSettings();
  if (settings.synchronizing === true) scheduleSettingsSyncPoll();
  else if (settings.arxiv_access?.paused === true) {
    scheduleSettingsSyncPoll(arxivAccessRefreshDelay(settings.arxiv_access));
  }
}

async function startSettingsRetry(count, request) {
  if (!settingsModel || settingsModel.synchronizing === true || settingsModel.arxiv_access?.paused === true) return;
  const generation = settingsLifecycle.capture();
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
    await request();
    if (settingsSyncPollIsCurrent(generation)) scheduleSettingsSyncPoll(0);
  } catch (error) {
    if (!settingsSyncPollIsCurrent(generation)) return;
    settingsModel = {
      ...settingsModel,
      synchronizing: false,
      daily_list_retry: {
        status: "idle",
        completed: 0,
        total: Number.isSafeInteger(count) && count > 0 ? count : 0,
      },
    };
    try {
      await requestSettings();
    } catch {
      redrawSettings();
    }
    if (!settingsLifecycle.isCurrent(generation)) return;
    showActionFailure(error);
  }
}

const settingsActions = {
  retrySynchronization(count = 0) {
    return startSettingsRetry(count, () => settingsController.retrySynchronization());
  },
  retryFailedDate(category, day) {
    return startSettingsRetry(1, () => settingsController.retryFailedDate(category, day));
  },
  async openFolder() {
    try {
      await settingsLifecycle.wait(settingsController.openFolder());
      statusText("PDF folder opened.");
    } catch (error) {
      showActionFailure(error);
    }
  },
  async pickFolder() {
    try {
      const result = await settingsLifecycle.wait(settingsController.pickFolder());
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
    const generation = settingsLifecycle.capture();
    try {
      await settingsLifecycle.wait(settingsController.testFolder(choice));
      await settingsLifecycle.wait(settingsController.saveTestedFolder(Number(settingsModel?.revision)));
      settingsPickerChoice = null;
      settingsPickerDisplayName = null;
      settingsPickerUnavailable = false;
      await requestSettings();
      statusText("PDF destination saved.");
    } catch (error) {
      if (error instanceof StaleResponseError || error?.name === "AbortError") return;
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
      if (!settingsLifecycle.isCurrent(generation)) return;
      if (pickerChoice) {
        statusText(`${error?.message || "The folder could not be used."} Choose the folder again.`);
      } else showActionFailure(error);
    }
  },
  async extendCoverage(category, newStart) {
    try {
      await settingsLifecycle.wait(settingsController.extendCoverage(
        category,
        newStart,
        Number(settingsModel?.revision),
      ));
      await requestSettings();
      statusText(`Historical coverage for ${category} was extended.`);
    } catch (error) {
      showActionFailure(error);
    }
  },
  async clearCache() {
    try {
      await settingsLifecycle.wait(settingsController.clearCache(true));
      statusText(
        "Suggestion cache deleted. Interests, synchronization checkpoints, review progress, Library papers, and downloaded PDFs were kept.",
      );
    } catch (error) {
      showActionFailure(error);
    }
  },
  createLauncher() {
    settingsLifecycle.wait(settingsController.createLauncher())
      .then(requestSettings)
      .then(() => statusText("Desktop launcher created."))
      .catch(showActionFailure);
  },
  retryLauncher() {
    this.createLauncher();
  },
  notNowLauncher() {
    settingsLifecycle.wait(settingsController.notNowLauncher())
      .then(requestSettings)
      .then(() => statusText("Desktop launcher deferred."))
      .catch(showActionFailure);
  },
  removeLauncher() {
    settingsLifecycle.wait(settingsController.removeLauncher())
      .then(requestSettings)
      .then(() => statusText("Desktop launcher removed."))
      .catch(showActionFailure);
  },
  exportBackup() {
    settingsLifecycle.wait(settingsController.downloadBackup())
      .then(() => statusText("Portable backup downloaded."))
      .catch(showActionFailure);
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
  coverageStart: "",
  destinationChoice: null,
  destinationDisplayName: null,
  testedDestinationToken: null,
  pickerState: "available",
  lifecycleGeneration: 0,
  launcherChoice: null,
};

function setupLifecycleIsActive(generation) {
  return viewLifecycle.isCurrent(generation) && state.snapshot.view === "setup";
}

function setupStep(draft = setupUi.draft) {
  return String(draft?.current_step ?? draft?.step ?? "categories");
}

function setupModel() {
  const draft = setupUi.draft ?? {};
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
  setupUi.coverageStart ||= setupUi.draft.coverage_start ?? "";
  if (loadOptions && !await loadSetupOptions("", lifecycleGeneration)) return false;
  if (!setupLifecycleIsActive(lifecycleGeneration)) return false;
  redrawSetup();
  return true;
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
    }
  },
  onCoverageChange(value) {
    setupUi.coverageStart = value;
    redrawSetup();
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
  setupUi.lifecycleGeneration = lifecycleGeneration;
  await refreshSetup({ lifecycleGeneration });
}

async function renderCurrent() {
  if (applicationClosing) return;
  const generation = viewLifecycle.invalidate();
  const view = state.snapshot.view;
  if (view !== "review") {
    clearReviewPoll();
    reviewRequests.invalidate();
  }
  clearSettingsSyncPoll();
  markNavigation(view);
  statusText("Loading local data…");
  try {
    if (view === "setup") await requestSetup(generation);
    else if (view === "review") await requestReview(
      state.snapshot.reviewDate ? { date: state.snapshot.reviewDate } : {},
    );
    else if (view === "calendar") await requestCalendar();
    else if (view === "library") await requestLibrary();
    else if (view === "interests") await requestInterests();
    else if (view === "settings") await requestSettings();
    if (!viewLifecycle.isCurrent(generation)) return;
    statusText("");
    content.focus();
  } catch (error) {
    if (error instanceof StaleResponseError || error?.name === "AbortError") return;
    if (!viewLifecycle.isCurrent(generation)) return;
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
  if (applicationClosing) return;
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

async function quitApplication() {
  if (applicationClosing) return;
  applicationClosing = true;
  applicationQuitRequested = true;
  stopHeartbeat();
  stopUpdateNotice();
  clearSettingsSyncPoll();
  clearReviewPoll();
  viewLifecycle.invalidate();
  downloadLifecycle.invalidate();
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
    viewLifecycle.invalidate();
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
  stopUpdateNotice();
  if (applicationQuitRequested) return;
  applicationClosing = true;
  stopHeartbeat();
  clearReviewPoll();
  viewLifecycle.invalidate();
  downloadLifecycle.invalidate();
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
  refreshUpdateNotice();
  connectTab();
  renderCurrent();
  for (const download of libraryDownloadState.values()) {
    if (download.status === "running") {
      pollLibraryDownload(download.job_id).catch(showLibraryFailure);
    }
  }
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

refreshUpdateNotice();
connectTab();
renderCurrent();
