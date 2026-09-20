import { renderPaperCard } from "./paper_view.mjs";
import { arxivAccessPauseText } from "./settings_view.mjs";

const TIERS = Object.freeze([
  ["top", "Top"],
  ["possible", "Possible"],
  ["other", "Other"],
]);

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(document, label, handler) {
  const node = element(document, "button", label);
  node.setAttribute("type", "button");
  node.addEventListener("click", handler);
  return node;
}

function dayOf(page) {
  return String(page?.day ?? page?.date ?? "");
}

function cardsOf(page) {
  return Array.isArray(page?.cards)
    ? page.cards
    : Array.isArray(page?.papers)
      ? page.papers
      : [];
}

function tierOf(card) {
  const tier = card?.tier;
  return typeof tier === "string" ? tier : String(tier?.value ?? "other");
}

function normalizedRetry(value) {
  const total = Number.isSafeInteger(value?.total) && value.total > 0
    ? value.total
    : 0;
  const completed = Number.isSafeInteger(value?.completed) && value.completed > 0
    ? Math.min(value.completed, total)
    : 0;
  return Object.freeze({
    status: value?.status === "running" && total > 0 ? "running" : "idle",
    completed,
    total,
  });
}

function nonnegativeCount(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

export function dailyListProgressText(value) {
  const total = nonnegativeCount(value?.target_dates);
  if (total === 0) return "";
  const checked = Math.min(nonnegativeCount(value?.checked_dates), total);
  const withPapers = nonnegativeCount(value?.dates_with_papers);
  const empty = nonnegativeCount(value?.empty_dates);
  const failed = nonnegativeCount(value?.failed_dates);
  const remaining = Math.min(
    nonnegativeCount(value?.pending_dates),
    total,
  );
  return `Checking historical daily lists: ${checked} of ${total} dates checked · ${withPapers} with papers · ${empty} empty · ${failed} failed · ${remaining} remaining.`;
}

function updateStartButton(control) {
  const busy = control.dataset.busy === "true";
  control.disabled = busy;
  control.setAttribute("aria-busy", busy ? "true" : "false");
  control.textContent = busy ? "Loading…" : "Start review";
}

function updateRetryButton(control, retry, synchronizing, paused) {
  const busy = control.dataset.busy === "true";
  const running = retry.status === "running";
  control.hidden = retry.total === 0;
  control.disabled = busy || running || synchronizing || paused;
  control.setAttribute("aria-busy", busy || running ? "true" : "false");
  if (running) {
    control.textContent = `Retrying failed daily-list dates… ${retry.completed} of ${retry.total} completed`;
  } else if (busy) {
    control.textContent = "Starting failed-date retry…";
  } else {
    const noun = retry.total === 1 ? "date" : "dates";
    control.textContent = `Retry ${retry.total} failed daily-list ${noun}`;
  }
}

export function reviewSummaryText(summary) {
  const papers = Number(summary?.unreviewed_papers ?? 0);
  const dates = Number(summary?.unreviewed_dates ?? 0);
  const paperState = papers === 1
    ? "paper announcement is"
    : "paper announcements are";
  const dateNoun = dates === 1 ? "date" : "dates";
  let message = `${papers} unreviewed ${paperState} ready across ${dates} ${dateNoun}. Review starts with the oldest date.`;
  const added = Number(summary?.newly_discovered ?? 0);
  if (added === 1) {
    message += " 1 paper announcement was added to a previously finished date.";
  } else if (added > 1) {
    message += ` ${added} paper announcements were added to previously finished dates.`;
  }
  return message;
}

export function reviewHomeText(
  summary,
  { synchronizing = false, coverageIncomplete = false } = {},
) {
  const waitingDates = nonnegativeCount(summary?.waiting_abstract_dates);
  const missing = nonnegativeCount(summary?.missing_abstracts);
  const waiting = missing > 0
    ? `${missing} unreviewed ${missing === 1 ? "paper is missing an abstract" : "papers are missing abstracts"}${waitingDates > 0 ? ` across ${waitingDates} ${waitingDates === 1 ? "date" : "dates"}` : ""}. You can still review and finish these dates.`
    : "";
  if (synchronizing && !summary?.oldest_unreviewed_date) {
    return `Historical daily-list recovery is in progress. Dates will appear as their daily lists are recovered, and this page will update automatically.${waiting ? ` ${waiting}` : ""}`;
  }
  if (summary?.oldest_unreviewed_date) {
    const backlog = `${reviewSummaryText(summary)}${waiting ? ` ${waiting}` : ""}`;
    if (synchronizing) {
      return `${backlog} Synchronization is still in progress, so this count may increase. This page will update automatically.`;
    }
    return coverageIncomplete
      ? `${backlog} Historical daily-list coverage is incomplete, so unresolved gaps may hide additional paper announcements.`
      : backlog;
  }
  if (coverageIncomplete) {
    return `Historical daily-list coverage is incomplete. Dates with recovered announcements remain available; unresolved gaps may hide additional paper announcements.${waiting ? ` ${waiting}` : ""}`;
  }
  return `You are caught up. New papers from future synchronizations, including papers added to finished dates, will appear here.${waiting ? ` ${waiting}` : ""}`;
}

function finishAllConfirmationText(summary) {
  const papers = Number(summary?.unreviewed_papers ?? 0);
  const dates = Number(summary?.unreviewed_dates ?? 0);
  const paperNoun = papers === 1 ? "paper" : "papers";
  const dateNoun = dates === 1 ? "date" : "dates";
  const prefix = papers === 1 ? "Mark 1" : `Mark all ${papers}`;
  return `${prefix} currently unreviewed ${paperNoun} across ${dates} ${dateNoun} as reviewed? Papers imported later will remain unreviewed.`;
}

export function renderReviewHome(
  document,
  container,
  summary,
  {
    synchronizing = false,
    synchronizationPhase = "daily_list",
    dailyListRetry,
    dailyListProgress,
    arxivAccess,
    start,
    retryFailed,
    finishAll,
    pending,
    failure,
  } = {},
) {
  const retry = normalizedRetry(dailyListRetry);
  const enrichment = synchronizing && synchronizationPhase === "enrichment";
  const recovering = synchronizing && !enrichment;
  let view = container.querySelector(".review-home");
  if (!view) {
    container.replaceChildren();
    view = element(document, "section", undefined, "review-home");
    const heading = element(document, "h1", "Review");
    const paragraph = element(document, "p", undefined, "review-home-status");
    const accessStatus = element(document, "p", undefined, "arxiv-access-status");
    accessStatus.setAttribute("role", "status");
    accessStatus.setAttribute("aria-live", "polite");
    accessStatus.hidden = true;
    const activity = element(
      document,
      "div",
      undefined,
      "review-sync-activity",
    );
    activity.hidden = true;
    const progress = element(
      document,
      "progress",
      undefined,
      "review-sync-progress",
    );
    progress.setAttribute("aria-label", "Synchronization in progress");
    const progressText = element(
      document,
      "p",
      undefined,
      "review-sync-progress-text",
    );
    const phaseStatus = element(
      document,
      "p",
      undefined,
      "review-sync-phase-status",
    );
    phaseStatus.setAttribute("role", "status");
    phaseStatus.setAttribute("aria-live", "polite");
    activity.append(progress, progressText, phaseStatus);
    view.append(heading, paragraph, accessStatus, activity);
    container.append(view);
  }

  const paragraph = view.querySelector(".review-home-status");
  const coverageIncomplete =
    nonnegativeCount(dailyListProgress?.failed_dates) > 0 ||
    nonnegativeCount(dailyListProgress?.pending_dates) > 0 ||
    nonnegativeCount(dailyListProgress?.unavailable_dates) > 0;
  const message = reviewHomeText(summary, {
    synchronizing: recovering,
    coverageIncomplete,
  });
  if (paragraph.textContent !== message) paragraph.textContent = message;
  const accessStatus = view.querySelector(".arxiv-access-status");
  const accessMessage = arxivAccessPauseText(arxivAccess);
  accessStatus.hidden = !accessMessage;
  if (accessStatus.textContent !== accessMessage) accessStatus.textContent = accessMessage;
  const activity = view.querySelector(".review-sync-activity");
  const progress = activity.querySelector("progress");
  const progressText = activity.querySelector(".review-sync-progress-text");
  const phaseStatus = activity.querySelector(".review-sync-phase-status");
  const retrying = retry.status === "running";
  const coverageText = dailyListProgressText(dailyListProgress);
  activity.hidden = !synchronizing && !retrying;
  view.setAttribute("aria-busy", recovering || retrying ? "true" : "false");
  if (enrichment) {
    progress.removeAttribute?.("value");
    progress.removeAttribute?.("max");
    progress.setAttribute("aria-label", "Syncing recent paper data…");
    progressText.textContent = "";
    if (phaseStatus.textContent !== "Syncing recent paper data…") {
      phaseStatus.textContent = "Syncing recent paper data…";
    }
  } else if (coverageText) {
    if (phaseStatus.textContent !== "") phaseStatus.textContent = "";
    const total = nonnegativeCount(dailyListProgress?.target_dates);
    const checked = Math.min(
      nonnegativeCount(dailyListProgress?.checked_dates),
      total,
    );
    progress.setAttribute("value", checked);
    progress.setAttribute("max", total);
    progress.setAttribute("aria-label", coverageText);
    progressText.textContent = coverageText;
  } else if (retrying) {
    if (phaseStatus.textContent !== "") phaseStatus.textContent = "";
    progress.setAttribute("value", retry.completed);
    progress.setAttribute("max", retry.total);
    progress.setAttribute(
      "aria-label",
      `Retrying failed daily-list dates: ${retry.completed} of ${retry.total} completed`,
    );
    progressText.textContent = "";
  } else {
    if (phaseStatus.textContent !== "") phaseStatus.textContent = "";
    progress.removeAttribute?.("value");
    progress.removeAttribute?.("max");
    progress.setAttribute("aria-label", "Synchronization in progress");
    progressText.textContent = "";
  }

  const papers = Number(summary?.unreviewed_papers ?? 0);
  const oldest = summary?.oldest_unreviewed_date
    ? String(summary.oldest_unreviewed_date)
    : "";
  const hasReview = Boolean(oldest) && papers > 0;
  let controls = view.querySelector(".review-home-actions");
  if (!hasReview && retry.total === 0) {
    controls?.remove();
    return view;
  }

  if (!controls) {
    controls = element(document, "div", undefined, "review-home-actions");
    const startButton = button(document, "Start review", async () => {
      if (startButton.dataset.busy === "true") return;
      const reviewActions = controls.reviewActions;
      const selectedDate = startButton.dataset.date;
      startButton.dataset.busy = "true";
      updateStartButton(startButton);
      let failed = false;
      try {
        await reviewActions?.start?.(selectedDate);
      } catch (error) {
        failed = true;
        reviewActions?.failure?.(error);
      } finally {
        startButton.dataset.busy = "false";
        updateStartButton(startButton);
        if (failed) startButton.focus?.();
      }
    });
    startButton.className = "review-start";
    startButton.setAttribute("aria-busy", "false");

    const retryButton = button(document, "Retry failed daily-list dates", async () => {
      if (retryButton.disabled || retryButton.dataset.busy === "true") return;
      retryButton.dataset.busy = "true";
      updateRetryButton(
        retryButton,
        controls.reviewRetry,
        controls.reviewSynchronizing,
        controls.reviewPaused,
      );
      let failed = false;
      try {
        await controls.reviewActions?.retryFailed?.();
      } catch (error) {
        failed = true;
        controls.reviewActions?.failure?.(error);
      } finally {
        retryButton.dataset.busy = "false";
        updateRetryButton(
          retryButton,
          controls.reviewRetry,
          controls.reviewSynchronizing,
          controls.reviewPaused,
        );
        if (failed) retryButton.focus?.();
      }
    });
    retryButton.className = "review-retry-failed";
    retryButton.setAttribute("aria-busy", "false");

    const markAll = button(document, "Mark all as reviewed", () => {
      if (confirmation.dataset.busy === "true") return;
      const confirmed = controls.reviewSummary;
      controls.confirmedSnapshot = Object.freeze({
        snapshotRevision: Number(confirmed?.snapshot_revision),
        profileRevision: Number(confirmed?.profile_revision),
        projectionRevision: Number(confirmed?.projection_revision),
        throughDate: confirmed?.through_date,
      });
      confirmationText.textContent = finishAllConfirmationText(confirmed);
      markAll.disabled = true;
      markAll.setAttribute("aria-expanded", "true");
      confirm.disabled = false;
      cancel.disabled = false;
      confirm.textContent = "Confirm mark all as reviewed";
      confirmation.hidden = false;
      confirm.focus?.();
    });
    markAll.className = "review-finish-all";
    markAll.setAttribute("aria-expanded", "false");
    markAll.setAttribute("aria-controls", "review-finish-all-confirmation");

    const confirmation = element(
      document,
      "div",
      undefined,
      "review-finish-all-confirmation",
    );
    confirmation.setAttribute("id", "review-finish-all-confirmation");
    confirmation.hidden = true;
    const confirmationText = element(document, "p");
    confirmationText.setAttribute("id", "review-finish-all-description");
    confirmation.append(confirmationText);

    const confirm = button(
      document,
      "Confirm mark all as reviewed",
      async () => {
        if (confirmation.dataset.busy === "true") return;
        confirmation.dataset.busy = "true";
        confirmation.setAttribute("aria-busy", "true");
        confirm.disabled = true;
        cancel.disabled = true;
        confirm.textContent = "Marking…";
        controls.reviewActions?.pending?.();
        try {
          await controls.reviewActions?.finishAll?.(
            controls.confirmedSnapshot.snapshotRevision,
            controls.confirmedSnapshot.profileRevision,
            controls.confirmedSnapshot.projectionRevision,
            controls.confirmedSnapshot.throughDate,
          );
          confirmation.dataset.busy = "false";
          confirmation.setAttribute("aria-busy", "false");
          confirm.textContent = "Marked as reviewed";
        } catch (error) {
          confirmation.dataset.busy = "false";
          confirmation.setAttribute("aria-busy", "false");
          confirm.disabled = false;
          cancel.disabled = false;
          confirm.textContent = "Confirm mark all as reviewed";
          controls.reviewActions?.failure?.(error);
          confirm.focus?.();
        }
        if (confirmation.hidden) markAll.disabled = false;
      },
    );
    confirm.setAttribute("aria-describedby", "review-finish-all-description");
    const cancel = button(document, "Cancel", () => {
      confirmation.hidden = true;
      markAll.disabled = false;
      markAll.setAttribute("aria-expanded", "false");
      markAll.focus?.();
    });
    confirmation.append(confirm, cancel);
    controls.append(startButton, retryButton, markAll, confirmation);
    view.append(controls);
  }

  controls.reviewActions = { start, retryFailed, finishAll, pending, failure };
  controls.reviewSummary = summary;
  controls.reviewRetry = retry;
  controls.reviewSynchronizing = synchronizing;
  controls.reviewPaused = arxivAccess?.paused === true;
  const startButton = controls.querySelector(".review-start");
  startButton.hidden = !hasReview;
  startButton.dataset.date = oldest;
  updateStartButton(startButton);
  updateRetryButton(
    controls.querySelector(".review-retry-failed"),
    retry,
    synchronizing,
    controls.reviewPaused,
  );
  const confirmation = controls.querySelector(
    ".review-finish-all-confirmation",
  );
  const markAll = controls.querySelector(".review-finish-all");
  markAll.hidden = !hasReview;
  if (!hasReview) {
    confirmation.hidden = true;
    markAll.setAttribute("aria-expanded", "false");
  }
  markAll.disabled = !confirmation.hidden || confirmation.dataset.busy === "true";
  if (confirmation.hidden) {
    confirmation.querySelector("p").textContent =
      finishAllConfirmationText(summary);
  }
  return view;
}

export function reviewDestination(page, action) {
  const day = dayOf(page);
  const destinations = {
    "previous-page": { date: day, anchor_event_id: page?.previous_anchor_event_id ?? null },
    "next-page": { date: day, anchor_event_id: page?.next_anchor_event_id ?? null },
    "previous-date": { date: page?.previous_date ?? null, anchor_event_id: null },
    "next-date": { date: page?.next_date ?? null, anchor_event_id: null },
  };
  if (!Object.hasOwn(destinations, action) || !destinations[action].date) return null;
  if (
    (action === "previous-page" || action === "next-page") &&
    destinations[action].anchor_event_id === null
  ) {
    return null;
  }
  return Object.freeze(destinations[action]);
}

export function renderReviewError(document, container, message, retry) {
  let notice = container.querySelector(".error-banner");
  if (!notice) {
    notice = element(document, "section", undefined, "error-banner");
    container.append(notice);
  }
  notice.setAttribute("role", "alert");
  notice.replaceChildren(
    element(document, "p", String(message)),
    button(document, "Retry", retry),
  );
  return notice;
}

function pendingButton(document, label, action, failure, disabled = false) {
  const control = button(document, label, async () => {
    if (control.disabled || control.dataset.busy === "true") return;
    control.dataset.busy = "true";
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    control.textContent = "Loading…";
    let failed = false;
    try {
      await action?.();
    } catch (error) {
      failed = true;
      failure?.(error);
    } finally {
      control.dataset.busy = "false";
      control.disabled = false;
      control.setAttribute("aria-busy", "false");
      control.textContent = label;
      if (failed) control.focus?.();
    }
  });
  control.disabled = disabled;
  control.setAttribute("aria-busy", "false");
  return control;
}

function navigationButton(document, label, destination, navigate, failure) {
  return pendingButton(
    document,
    label,
    () => navigate?.(destination),
    failure,
    destination === null,
  );
}

function abstractRetryResultText(retry) {
  const recovered = nonnegativeCount(retry?.recovered);
  const remaining = nonnegativeCount(retry?.remaining);
  let text = `${retry?.status === "interrupted" ? "Retry interrupted. " : ""}Recovered ${recovered} ${recovered === 1 ? "abstract" : "abstracts"}; ${remaining} still unavailable.`;
  const codes = Array.isArray(retry?.error_codes) ? retry.error_codes : [];
  const statuses = [...new Set(codes.map((code) =>
    /^arxiv_http_(\d{3})$/.exec(String(code))?.[1],
  ).filter(Boolean))];
  if (statuses.length) {
    text += ` arXiv requests failed (${statuses.map((code) => `HTTP ${code}`).join(", ")}).`;
  } else if (codes.some((code) => code !== "cancelled")) {
    text += " Some arXiv requests failed. You can retry later.";
  }
  return text;
}

export function updateReviewAbstractStatus(document, view, page, actions = {}) {
  const day = dayOf(page);
  const missing = nonnegativeCount(page?.missing_abstracts);
  const retry = actions.abstractRetry?.retry_date === day ? actions.abstractRetry : null;
  const running = retry?.status === "running";
  const terminal = retry?.status === "completed" || retry?.status === "interrupted";
  let notice = view.querySelector(".review-abstract-notice");
  if (!notice) {
    notice = element(document, "div", undefined, "review-abstract-notice");
    view.append(notice);
  }
  notice.abstractActions = actions;
  notice.abstractPage = page;
  notice.hidden = missing === 0 && !terminal && !running;
  let description = notice.querySelector(".review-abstract-availability");
  let control = notice.querySelector(".review-retry-abstracts");
  if (missing > 0 || running) {
    if (!description) {
      description = element(document, "p", undefined, "review-abstract-availability");
      notice.append(description);
    }
    description.hidden = false;
    const ready = nonnegativeCount(page?.abstracts_ready);
    description.textContent = `${ready} of ${ready + missing} abstracts available. You can still review and finish this date.`;
    if (!control) {
      control = button(document, "Retry missing abstracts", async () => {
        if (control.disabled || control.dataset.busy === "true") return;
        control.dataset.busy = "true";
        const refresh = () => updateReviewAbstractStatus(document, view, notice.abstractPage, notice.abstractActions);
        refresh();
        let failed = false;
        try {
          await notice.abstractActions.retryAbstracts?.(day);
        } catch (error) {
          failed = true;
          notice.abstractActions.failure?.(error);
        } finally {
          control.dataset.busy = "false";
          refresh();
          if (failed) control.focus?.();
        }
      });
      control.className = "review-retry-abstracts";
      notice.append(control);
    }
    const progress = normalizedRetry(retry);
    const busy = control.dataset.busy === "true";
    control.hidden = false;
    control.textContent = running
      ? `Retrying abstracts… ${progress.completed} of ${progress.total} checked`
      : busy ? "Starting abstract retry…" : "Retry missing abstracts";
    control.disabled = busy || running || actions.synchronizing === true || actions.arxivAccess?.paused === true;
    control.setAttribute("aria-busy", busy || running ? "true" : "false");
  } else {
    if (description) description.hidden = true;
    if (control) control.hidden = true;
  }
  let result = notice.querySelector(".review-abstract-retry-result");
  if (terminal) {
    if (!result) {
      result = element(document, "p", undefined, "review-abstract-retry-result");
      result.setAttribute("role", "status");
      result.setAttribute("aria-live", "polite");
      notice.append(result);
    }
    result.hidden = false;
    const message = abstractRetryResultText(retry);
    if (result.textContent !== message) result.textContent = message;
  } else if (result) {
    result.hidden = true;
  }
  const accessMessage = arxivAccessPauseText(actions.arxivAccess);
  let accessStatus = notice.querySelector(".arxiv-access-status");
  if (missing > 0 && accessMessage) {
    if (!accessStatus) {
      accessStatus = element(document, "p", undefined, "arxiv-access-status");
      accessStatus.setAttribute("role", "status");
      notice.append(accessStatus);
    }
    accessStatus.hidden = false;
    if (accessStatus.textContent !== accessMessage) accessStatus.textContent = accessMessage;
  } else if (accessStatus) {
    accessStatus.hidden = true;
  }
}

export function renderReviewView(document, container, page, actions = {}) {
  const cards = cardsOf(page);
  if (cards.length > 20) throw new RangeError("Review pages cannot exceed 20 cards");
  const day = dayOf(page);
  const totalCards = Number(page?.total_cards ?? cards.length);
  const pageNumber = Number(page?.page_number ?? 1);
  const pageCount = Number(page?.page_count ?? 1);
  const isLastPage = page?.next_anchor_event_id == null && pageNumber >= pageCount;
  const totalNoun = totalCards === 1
    ? "paper announcement"
    : "paper announcements";
  const reviewState = cards.length > 0 && cards.every((card) => card?.reviewed === true)
    ? "reviewed"
    : "unreviewed";
  container.replaceChildren();
  const view = element(document, "section", undefined, "review-view");
  const heading = element(document, "h1", day ? `Review ${day}` : "Review");
  heading.setAttribute("tabindex", "-1");
  view.append(heading);
  view.append(
    element(
      document,
      "p",
      `${totalCards} ${reviewState} ${totalNoun} for this date · Page ${pageNumber} of ${pageCount}`,
      "page-count",
    ),
  );
  updateReviewAbstractStatus(document, view, page, actions);

  const dateNavigation = element(document, "nav", undefined, "date-navigation");
  dateNavigation.setAttribute("aria-label", "Review dates");
  dateNavigation.append(
    pendingButton(
      document,
      "Back to Review overview",
      () => actions.overview?.(),
      actions.failure,
    ),
    navigationButton(document, "Previous date", reviewDestination(page, "previous-date"), actions.navigate, actions.failure),
    navigationButton(document, "Next date", reviewDestination(page, "next-date"), actions.navigate, actions.failure),
  );
  view.append(dateNavigation);

  const pageNavigation = element(document, "nav", undefined, "page-navigation");
  pageNavigation.setAttribute("aria-label", "Pages for this date");
  pageNavigation.append(
    navigationButton(document, "Previous page", reviewDestination(page, "previous-page"), actions.navigate, actions.failure),
    navigationButton(document, "Next page", reviewDestination(page, "next-page"), actions.navigate, actions.failure),
  );

  for (const [tier, label] of TIERS) {
    const tierCards = cards.filter((item) => tierOf(item) === tier);
    if (tierCards.length === 0) continue;
    const section = element(document, "section", undefined, `ranking-tier tier-${tier}`);
    section.append(element(document, "h2", label));
    for (const card of tierCards) {
      renderPaperCard(document, section, card, actions.paperActions ?? {});
    }
    view.append(section);
  }
  view.append(pageNavigation);

  if (reviewState !== "reviewed" && isLastPage) {
    const finishStatus = element(document, "p", "", "finish-status");
    finishStatus.setAttribute("role", "status");
    finishStatus.setAttribute("aria-live", "polite");
    const confirmation = element(
      document,
      "div",
      undefined,
      "finish-confirmation",
    );
    confirmation.setAttribute("id", "review-finish-confirmation");
    confirmation.hidden = true;
    confirmation.setAttribute("aria-busy", "false");
    const confirm = button(document, "Confirm finish", async () => {
      if (confirmation.dataset.busy === "true") return;
      confirmation.dataset.busy = "true";
      confirmation.setAttribute("aria-busy", "true");
      confirm.disabled = true;
      cancel.disabled = true;
      confirm.textContent = "Finishing…";
      finishStatus.textContent = "Finishing this review date…";
      try {
        await actions.finish?.(
          day,
          Number(page?.snapshot_revision),
          Number(page?.profile_revision),
          Number(page?.projection_revision),
        );
        confirmation.dataset.busy = "false";
        confirmation.setAttribute("aria-busy", "false");
        confirm.textContent = "Finished";
        finishStatus.textContent = "Date marked as reviewed.";
      } catch (error) {
        confirmation.dataset.busy = "false";
        confirmation.setAttribute("aria-busy", "false");
        confirm.disabled = false;
        cancel.disabled = false;
        confirm.textContent = "Confirm finish";
        finish.disabled = false;
        finishStatus.textContent = "The date was not finished. Try again.";
        actions.failure?.(error);
        confirm.focus?.();
      }
    });
    const cancel = button(document, "Cancel", () => {
      confirmation.hidden = true;
      finish.disabled = false;
      finish.setAttribute("aria-expanded", "false");
      finish.focus?.();
    });
    confirmation.append(
      element(document, "p", "Mark every paper in the opened snapshot as reviewed?"),
      confirm,
      cancel,
    );
    const finish = button(document, "Finish date", () => {
      finish.disabled = true;
      finish.setAttribute("aria-expanded", "true");
      confirmation.hidden = false;
      finishStatus.textContent = "";
      confirm.focus?.();
    });
    finish.setAttribute("aria-expanded", "false");
    finish.setAttribute("aria-controls", "review-finish-confirmation");
    view.append(finish, confirmation, finishStatus);
  }
  const exitNavigation = element(
    document,
    "nav",
    undefined,
    "review-exit-navigation",
  );
  exitNavigation.setAttribute("aria-label", "Leave this review date");
  exitNavigation.append(
    pendingButton(
      document,
      "Back to Review overview",
      () => actions.overview?.(),
      actions.failure,
    ),
  );
  view.append(exitNavigation);
  container.append(view);
  return view;
}
