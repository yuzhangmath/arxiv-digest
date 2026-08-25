import { renderPaperCard } from "./paper_view.mjs";

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

function normalizedDailyListRetry(value) {
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

function updateRetryButton(control, retry, synchronizing) {
  const busy = control.dataset.busy === "true";
  const running = retry.status === "running";
  control.hidden = retry.total === 0;
  control.disabled = busy || running || synchronizing;
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
  if (synchronizing && !summary?.oldest_unreviewed_date) {
    return "Historical daily-list recovery is in progress. Confirmed daily-list announcements will appear as dates are recovered, and this page will update automatically.";
  }
  if (summary?.oldest_unreviewed_date) {
    const backlog = reviewSummaryText(summary);
    if (synchronizing) {
      return `${backlog} Synchronization is still in progress, so this count may increase. This page will update automatically.`;
    }
    return coverageIncomplete
      ? `${backlog} Historical daily-list coverage is incomplete, so unresolved gaps may hide additional paper announcements.`
      : backlog;
  }
  if (coverageIncomplete) {
    return "Historical daily-list coverage is incomplete. Confirmed announcements from recovered dates remain available; unresolved gaps may hide additional paper announcements.";
  }
  return "You are caught up. New papers from future synchronizations, including papers added to finished dates, will appear here.";
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
    dailyListRetry,
    dailyListProgress,
    start,
    retryFailed,
    finishAll,
    pending,
    failure,
  } = {},
) {
  const retry = normalizedDailyListRetry(dailyListRetry);
  let view = container.querySelector(".review-home");
  if (!view) {
    container.replaceChildren();
    view = element(document, "section", undefined, "review-home");
    const heading = element(document, "h1", "Review");
    const paragraph = element(document, "p", undefined, "review-home-status");
    paragraph.setAttribute("role", "status");
    paragraph.setAttribute("aria-live", "polite");
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
    activity.append(progress, progressText);
    view.append(heading, paragraph, activity);
    container.append(view);
  }

  const paragraph = view.querySelector(".review-home-status");
  const coverageIncomplete =
    nonnegativeCount(dailyListProgress?.failed_dates) > 0 ||
    nonnegativeCount(dailyListProgress?.pending_dates) > 0 ||
    nonnegativeCount(dailyListProgress?.unavailable_dates) > 0;
  const message = reviewHomeText(summary, {
    synchronizing,
    coverageIncomplete,
  });
  if (paragraph.textContent !== message) paragraph.textContent = message;
  const activity = view.querySelector(".review-sync-activity");
  const progress = activity.querySelector("progress");
  const progressText = activity.querySelector(".review-sync-progress-text");
  const retrying = retry.status === "running";
  const coverageText = dailyListProgressText(dailyListProgress);
  activity.hidden = !synchronizing && !retrying;
  view.setAttribute("aria-busy", synchronizing || retrying ? "true" : "false");
  if (coverageText) {
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
    progress.setAttribute("value", retry.completed);
    progress.setAttribute("max", retry.total);
    progress.setAttribute(
      "aria-label",
      `Retrying failed daily-list dates: ${retry.completed} of ${retry.total} completed`,
    );
    progressText.textContent = "";
  } else {
    progress.removeAttribute?.("value");
    progress.removeAttribute?.("max");
    progress.setAttribute("aria-label", "Synchronization in progress");
    progressText.textContent = "";
  }

  const papers = Number(summary?.unreviewed_papers ?? 0);
  const oldest = summary?.oldest_unreviewed_date
    ? String(summary.oldest_unreviewed_date)
    : "";
  let controls = view.querySelector(".review-home-actions");
  if (!oldest || papers <= 0) {
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
        );
        if (failed) retryButton.focus?.();
      }
    });
    retryButton.className = "review-retry-failed";
    retryButton.setAttribute("aria-busy", "false");

    const markAll = button(document, "Mark all as reviewed", () => {
      const confirmed = controls.reviewSummary;
      controls.confirmedSnapshot = Object.freeze({
        snapshotRevision: Number(confirmed?.snapshot_revision),
        profileRevision: Number(confirmed?.profile_revision),
        projectionRevision: Number(confirmed?.projection_revision),
      });
      confirmationText.textContent = finishAllConfirmationText(confirmed);
      markAll.disabled = true;
      markAll.setAttribute("aria-expanded", "true");
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
  const startButton = controls.querySelector(".review-start");
  startButton.dataset.date = oldest;
  updateStartButton(startButton);
  updateRetryButton(
    controls.querySelector(".review-retry-failed"),
    retry,
    synchronizing,
  );
  const confirmation = controls.querySelector(
    ".review-finish-all-confirmation",
  );
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
    "next-unreviewed": { date: page?.next_unreviewed_date ?? null, anchor_event_id: null },
  };
  if (!Object.hasOwn(destinations, action) || !destinations[action].date) return null;
  if (
    (action === "previous-page" || action === "next-page") &&
    destinations[action].anchor_event_id === null
  ) {
    return null;
  }
  if (action === "next-unreviewed" && destinations[action].date === day) {
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

export function renderReviewView(document, container, page, actions = {}) {
  const cards = cardsOf(page);
  if (cards.length > 20) throw new RangeError("Review pages cannot exceed 20 cards");
  const day = dayOf(page);
  const totalCards = Number(page?.total_cards ?? cards.length);
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
      `${totalCards} ${reviewState} ${totalNoun} for this date · Page ${Number(page?.page_number ?? 1)} of ${Number(page?.page_count ?? 1)}`,
      "page-count",
    ),
  );

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
    navigationButton(document, "Next unreviewed", reviewDestination(page, "next-unreviewed"), actions.navigate, actions.failure),
  );
  view.append(dateNavigation);

  const pageNavigation = element(document, "nav", undefined, "page-navigation");
  pageNavigation.setAttribute("aria-label", "Pages for this date");
  pageNavigation.append(
    navigationButton(document, "Previous page", reviewDestination(page, "previous-page"), actions.navigate, actions.failure),
    navigationButton(document, "Next page", reviewDestination(page, "next-page"), actions.navigate, actions.failure),
  );
  view.append(pageNavigation);

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

  if (reviewState !== "reviewed") {
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
