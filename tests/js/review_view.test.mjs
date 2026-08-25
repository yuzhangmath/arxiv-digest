import assert from "node:assert/strict";
import test from "node:test";

import * as reviewView from "../../src/arxiv_digest/web/static/review_view.mjs";
import { FakeDocument, FakeNode, descendants, findButton } from "./dom_test_helper.mjs";

const {
  dailyListProgressText,
  reviewDestination,
  reviewHomeText,
  reviewSummaryText,
  renderReviewError,
  renderReviewHome,
  renderReviewView,
} = reviewView;

function item(index, tier = "other") {
  return {
    event_id: index,
    arxiv_id: `2608.${String(index).padStart(5, "0")}`,
    version: "v1",
    title: `Paper ${index}`,
    authors: ["A. Author"],
    abstract: "Abstract",
    comments: "",
    ranking_text: "Why this ranking",
    tier,
    observations: ["math.AG"],
    date_label: "Current",
  };
}

test("review summary identifies the unreviewed oldest-first backlog without a zero discovery sentence", () => {
  assert.equal(
    reviewSummaryText({
      unreviewed_papers: 283,
      unreviewed_dates: 29,
      newly_discovered: 0,
      oldest_unreviewed_date: "2026-07-24",
    }),
    "283 unreviewed paper announcements are ready across 29 dates. Review starts with the oldest date.",
  );
});

test("caught-up review summary includes ordinary future synchronization", () => {
  assert.equal(
    reviewHomeText({
      unreviewed_papers: 0,
      unreviewed_dates: 0,
      newly_discovered: 0,
      oldest_unreviewed_date: null,
    }),
    "You are caught up. New papers from future synchronizations, including papers added to finished dates, will appear here.",
  );
});

test("coverage gaps never describe an empty queue as caught up", () => {
  assert.equal(
    reviewHomeText(
      {
        unreviewed_papers: 0,
        unreviewed_dates: 0,
        oldest_unreviewed_date: null,
      },
      { coverageIncomplete: true },
    ),
    "Historical daily-list coverage is incomplete. Confirmed announcements from recovered dates remain available; unresolved gaps may hide additional paper announcements.",
  );
});

test("an idle pending-only daily-list gap is not described as caught up", () => {
  const root = new FakeNode("main");
  renderReviewHome(
    new FakeDocument(),
    root,
    {
      unreviewed_papers: 0,
      unreviewed_dates: 0,
      oldest_unreviewed_date: null,
    },
    {
      synchronizing: false,
      dailyListProgress: {
        target_dates: 1,
        checked_dates: 0,
        dates_with_papers: 0,
        empty_dates: 0,
        failed_dates: 0,
        pending_dates: 1,
        unavailable_dates: 0,
      },
    },
  );

  assert.match(root.textContent, /coverage is incomplete/i);
  assert.doesNotMatch(root.textContent, /caught up/i);
});

test("an empty review stays in loading state while synchronization is active", () => {
  assert.equal(
    reviewHomeText(
      {
        unreviewed_papers: 0,
        unreviewed_dates: 0,
        newly_discovered: 0,
        oldest_unreviewed_date: null,
      },
      { synchronizing: true },
    ),
    "Historical daily-list recovery is in progress. Confirmed daily-list announcements will appear as dates are recovered, and this page will update automatically.",
  );
});

test("an available backlog says its count may grow during synchronization", () => {
  assert.equal(
    reviewHomeText(
      {
        unreviewed_papers: 3,
        unreviewed_dates: 2,
        newly_discovered: 0,
        oldest_unreviewed_date: "2026-08-01",
      },
      { synchronizing: true },
    ),
    "3 unreviewed paper announcements are ready across 2 dates. Review starts with the oldest date. Synchronization is still in progress, so this count may increase. This page will update automatically.",
  );
});

test("review home shows indeterminate synchronization activity until sync stops", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();
  const summary = {
    unreviewed_papers: 3,
    unreviewed_dates: 2,
    oldest_unreviewed_date: "2026-08-01",
  };

  const active = renderReviewHome(document, root, summary, {
    synchronizing: true,
  });
  const activity = active.querySelector(".review-sync-activity");
  const progress = descendants(activity, "progress")[0];
  assert.equal(active.getAttribute("aria-busy"), "true");
  assert.equal(activity.hidden, false);
  assert.equal(
    progress.getAttribute("aria-label"),
    "Synchronization in progress",
  );
  assert.equal(progress.getAttribute("value"), null);

  const settled = renderReviewHome(document, root, summary, {
    synchronizing: false,
  });
  assert.equal(settled, active);
  assert.equal(settled.getAttribute("aria-busy"), "false");
  assert.equal(activity.hidden, true);
});

test("review home renders disjoint daily-list coverage progress numerically", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();
  const summary = {
    unreviewed_papers: 3,
    unreviewed_dates: 2,
    oldest_unreviewed_date: "2026-08-01",
  };
  const dailyListProgress = {
    target_dates: 32,
    checked_dates: 18,
    dates_with_papers: 10,
    empty_dates: 8,
    failed_dates: 2,
    pending_dates: 12,
  };

  assert.equal(
    dailyListProgressText(dailyListProgress),
    "Checking historical daily lists: 18 of 32 dates checked · 10 with papers · 8 empty · 2 failed · 12 remaining.",
  );

  const active = renderReviewHome(document, root, summary, {
    synchronizing: true,
    dailyListProgress,
  });
  const activity = active.querySelector(".review-sync-activity");
  const progress = descendants(activity, "progress")[0];
  assert.match(activity.textContent, /18 of 32 dates checked/);
  assert.equal(progress.getAttribute("value"), "18");
  assert.equal(progress.getAttribute("max"), "32");
  assert.equal(
    progress.getAttribute("aria-label"),
    "Checking historical daily lists: 18 of 32 dates checked · 10 with papers · 8 empty · 2 failed · 12 remaining.",
  );
});

test("review summary describes one late paper as added to a previously finished date", () => {
  assert.equal(
    reviewSummaryText({
      unreviewed_papers: 1,
      unreviewed_dates: 1,
      newly_discovered: 1,
      oldest_unreviewed_date: "2026-07-24",
    }),
    "1 unreviewed paper announcement is ready across 1 date. Review starts with the oldest date. 1 paper announcement was added to a previously finished date.",
  );
});

test("review summary describes multiple late papers as added to previously finished dates", () => {
  assert.equal(
    reviewSummaryText({
      unreviewed_papers: 5,
      unreviewed_dates: 2,
      newly_discovered: 3,
      oldest_unreviewed_date: "2026-07-24",
    }),
    "5 unreviewed paper announcements are ready across 2 dates. Review starts with the oldest date. 3 paper announcements were added to previously finished dates.",
  );
});

test("review home requires confirmation before marking the whole backlog reviewed", async () => {
  const root = new FakeNode("main");
  const finishes = [];
  renderReviewHome(
    new FakeDocument(),
    root,
    {
      unreviewed_papers: 3,
      unreviewed_dates: 2,
      newly_discovered: 0,
      oldest_unreviewed_date: "2026-08-01",
      snapshot_revision: 8,
      profile_revision: 4,
      projection_revision: 9,
    },
    { finishAll: async (...revisions) => finishes.push(revisions) },
  );

  findButton(root, "Mark all as reviewed").click();
  assert.deepEqual(finishes, []);
  assert.match(
    root.textContent,
    /Mark all 3 currently unreviewed papers across 2 dates as reviewed\?/,
  );

  const confirm = findButton(root, "Confirm mark all as reviewed");
  assert.equal(
    confirm.getAttribute("aria-describedby"),
    "review-finish-all-description",
  );
  confirm.click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(finishes, [[8, 4, 9]]);
});

test("a failed whole-backlog finish restores and focuses confirmation", async () => {
  const root = new FakeNode("main");
  const failures = [];
  renderReviewHome(
    new FakeDocument(),
    root,
    {
      unreviewed_papers: 3,
      unreviewed_dates: 2,
      oldest_unreviewed_date: "2026-08-01",
      snapshot_revision: 8,
    },
    {
      finishAll: async () => {
        throw new Error("Offline");
      },
      failure: (error) => failures.push(error.message),
    },
  );

  findButton(root, "Mark all as reviewed").click();
  const confirm = findButton(root, "Confirm mark all as reviewed");
  let focused = false;
  confirm.focus = () => {
    focused = true;
  };
  confirm.click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(failures, ["Offline"]);
  assert.equal(confirm.disabled, false);
  assert.equal(confirm.textContent, "Confirm mark all as reviewed");
  assert.equal(focused, true);
});

test("start review reports pending work and restores focus after failure", async () => {
  const root = new FakeNode("main");
  let rejectStart;
  const failures = [];
  renderReviewHome(
    new FakeDocument(),
    root,
    {
      unreviewed_papers: 3,
      unreviewed_dates: 2,
      oldest_unreviewed_date: "2026-08-01",
    },
    {
      start: () =>
        new Promise((_resolve, reject) => {
          rejectStart = reject;
        }),
      failure: (error) => failures.push(error.message),
    },
  );

  const start = findButton(root, "Start review");
  let focused = false;
  start.focus = () => {
    focused = true;
  };
  start.click();
  const pending = {
    disabled: start.disabled,
    label: start.textContent,
    busy: start.getAttribute("aria-busy"),
  };

  rejectStart(new Error("Date changed"));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(pending, {
    disabled: true,
    label: "Loading…",
    busy: "true",
  });
  assert.deepEqual(failures, ["Date changed"]);
  assert.equal(start.disabled, false);
  assert.equal(start.textContent, "Start review");
  assert.equal(start.getAttribute("aria-busy"), "false");
  assert.equal(focused, true);
});

test("review starts immediately while failed-date retry remains independent", async () => {
  const root = new FakeNode("main");
  const starts = [];
  let retries = 0;
  const document = new FakeDocument();
  const summary = {
    unreviewed_papers: 3,
    unreviewed_dates: 2,
    oldest_unreviewed_date: "2026-08-01",
  };

  renderReviewHome(document, root, summary, {
    dailyListRetry: { status: "idle", completed: 0, total: 32 },
    start: async (...args) => starts.push(args),
    retryFailed: async () => retries++,
  });

  findButton(root, "Start review").click();
  findButton(root, "Retry 32 failed daily-list dates").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(starts, [["2026-08-01"]]);
  assert.equal(retries, 1);

  const active = renderReviewHome(document, root, summary, {
    synchronizing: true,
    dailyListRetry: { status: "running", completed: 20, total: 32 },
  });
  const retrying = findButton(
    root,
    "Retrying failed daily-list dates… 20 of 32 completed",
  );
  assert.equal(findButton(root, "Start review").disabled, false);
  const progress = descendants(
    active.querySelector(".review-sync-activity"),
    "progress",
  )[0];
  assert.equal(retrying.disabled, true);
  assert.equal(progress.getAttribute("value"), "20");
  assert.equal(progress.getAttribute("max"), "32");
  assert.equal(
    progress.getAttribute("aria-label"),
    "Retrying failed daily-list dates: 20 of 32 completed",
  );
});

test("review page status states how many papers belong to the date", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-07-24",
    total_cards: 2,
    page_number: 1,
    page_count: 1,
    cards: [item(1), item(2)],
  });

  const status = descendants(root, "p").find((node) =>
    node.className === "page-count"
  );
  assert.equal(
    status.textContent,
    "2 unreviewed paper announcements for this date · Page 1 of 1",
  );
});

test("review page status uses singular paper grammar", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-07-24",
    total_cards: 1,
    page_number: 1,
    page_count: 1,
    cards: [item(1)],
  });

  const status = descendants(root, "p").find((node) =>
    node.className === "page-count"
  );
  assert.equal(
    status.textContent,
    "1 unreviewed paper announcement for this date · Page 1 of 1",
  );
});

test("review page status identifies a fully reviewed date", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-07-24",
    total_cards: 1,
    page_number: 1,
    page_count: 1,
    cards: [{ ...item(1), reviewed: true }],
  });

  const status = descendants(root, "p").find((node) =>
    node.className === "page-count"
  );
  assert.equal(
    status.textContent,
    "1 reviewed paper announcement for this date · Page 1 of 1",
  );
  assert.equal(
    descendants(root, "button").some((node) => node.textContent === "Finish date"),
    false,
  );
});

test("review omits ranking tier sections that contain no papers", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-07-24",
    total_cards: 2,
    page_number: 1,
    page_count: 1,
    cards: [item(1), item(2)],
  });

  assert.deepEqual(
    descendants(root, "h2").map((heading) => heading.textContent),
    ["Other"],
  );
});

test("review uses server-provided dates, tiers, anchors, and a maximum of 20 cards", () => {
  const root = new FakeNode("main");
  const navigated = [];
  let overviews = 0;
  const page = {
    day: "2026-08-21",
    snapshot_revision: 11,
    page_number: 2,
    page_count: 7,
    previous_date: "2026-07-31",
    next_date: "2026-09-01",
    next_unreviewed_date: "2026-09-04",
    previous_anchor_event_id: 19,
    next_anchor_event_id: 40,
    cards: [item(1, "top"), item(2, "possible"), ...Array.from({ length: 18 }, (_, i) => item(i + 3))],
  };
  renderReviewView(new FakeDocument(), root, page, {
    navigate: (destination) => navigated.push(destination),
    overview: () => overviews++,
  });

  assert.equal(descendants(root, "article").length, 20);
  assert.match(root.textContent, /Top/);
  assert.match(root.textContent, /Possible/);
  assert.match(root.textContent, /Other/);
  assert.match(root.textContent, /Page 2 of 7/);
  findButton(root, "Back to Review overview").click();
  findButton(root, "Previous date").click();
  findButton(root, "Next date").click();
  findButton(root, "Next unreviewed").click();
  assert.deepEqual(navigated, [
    { date: "2026-07-31", anchor_event_id: null },
    { date: "2026-09-01", anchor_event_id: null },
    { date: "2026-09-04", anchor_event_id: null },
  ]);
  assert.equal(overviews, 1);
  assert.deepEqual(reviewDestination(page, "next-page"), {
    date: "2026-08-21",
    anchor_event_id: 40,
  });
});

test("review offers a bottom exit back to the overview", () => {
  const root = new FakeNode("main");
  let overviews = 0;
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      snapshot_revision: 11,
      cards: [item(1)],
    },
    { overview: () => overviews++ },
  );

  findButton(root, "Back to Review overview").click();

  assert.equal(overviews, 1);
});

test("review exposes matching Back to Review overview controls at both ends", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-08-21",
    snapshot_revision: 11,
    cards: [item(1)],
  });

  assert.equal(
    descendants(root, "button").filter(
      (control) => control.textContent === "Back to Review overview",
    ).length,
    2,
  );
});

test("overview exits show pending feedback and restore focus after failure", async () => {
  const root = new FakeNode("main");
  let rejectOverview;
  const failures = [];
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      snapshot_revision: 11,
      cards: [item(1)],
    },
    {
      overview: () =>
        new Promise((_resolve, reject) => {
          rejectOverview = reject;
        }),
      failure: (error) => failures.push(error.message),
    },
  );

  const exit = findButton(root, "Back to Review overview");
  let focused = false;
  exit.focus = () => {
    focused = true;
  };
  exit.click();
  assert.equal(exit.disabled, true);
  assert.equal(exit.textContent, "Loading…");
  assert.equal(exit.getAttribute("aria-busy"), "true");

  rejectOverview(new Error("Summary unavailable"));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(failures, ["Summary unavailable"]);
  assert.equal(exit.disabled, false);
  assert.equal(exit.textContent, "Back to Review overview");
  assert.equal(exit.getAttribute("aria-busy"), "false");
  assert.equal(focused, true);
});

test("previous page is disabled when there is no previous anchor", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-08-21",
    previous_anchor_event_id: null,
    next_anchor_event_id: 21,
    cards: [item(1)],
  });

  assert.equal(findButton(root, "Previous page").disabled, true);
  assert.equal(findButton(root, "Next page").disabled, false);
});

test("next page is disabled when there is no next anchor", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-08-21",
    previous_anchor_event_id: 1,
    next_anchor_event_id: null,
    cards: [item(21)],
  });

  assert.equal(findButton(root, "Previous page").disabled, false);
  assert.equal(findButton(root, "Next page").disabled, true);
});

test("date navigation shows pending feedback and restores a failed control", async () => {
  const root = new FakeNode("main");
  let rejectNavigation;
  const failures = [];
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      next_date: "2026-08-22",
      cards: [item(1)],
    },
    {
      navigate: () =>
        new Promise((_resolve, reject) => {
          rejectNavigation = reject;
        }),
      failure: (error) => failures.push(error.message),
    },
  );

  const next = findButton(root, "Next date");
  next.click();
  assert.equal(next.disabled, true);
  assert.equal(next.textContent, "Loading…");
  assert.equal(next.getAttribute("aria-busy"), "true");

  rejectNavigation(new Error("Date changed"));
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(failures, ["Date changed"]);
  assert.equal(next.disabled, false);
  assert.equal(next.textContent, "Next date");
  assert.equal(next.getAttribute("aria-busy"), "false");
});

test("review rejects oversized pages rather than hiding cards", () => {
  const root = new FakeNode("main");
  assert.throws(
    () =>
      renderReviewView(new FakeDocument(), root, {
        day: "2026-08-21",
        page_number: 1,
        page_count: 2,
        cards: Array.from({ length: 21 }, (_, i) => item(i + 1)),
      }),
    /20/,
  );
});

test("next unreviewed is disabled when the backlog still points to this date", () => {
  const page = {
    day: "2026-08-21",
    next_unreviewed_date: "2026-08-21",
  };

  assert.equal(reviewDestination(page, "next-unreviewed"), null);
});

test("finish submits the opened snapshot revision after explicit confirmation", async () => {
  const root = new FakeNode("main");
  const finished = [];
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      snapshot_revision: 37,
      profile_revision: 4,
      projection_revision: 9,
      page_number: 1,
      page_count: 1,
      cards: [item(1, "top")],
    },
    { finish: (...args) => finished.push(args) },
  );
  findButton(root, "Finish date").click();
  assert.equal(finished.length, 0);
  findButton(root, "Confirm finish").click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(finished, [["2026-08-21", 37, 4, 9]]);
  assert.equal(findButton(root, "Finished").disabled, true);
});

test("finish date identifies the confirmation it controls", () => {
  const root = new FakeNode("main");
  renderReviewView(new FakeDocument(), root, {
    day: "2026-08-21",
    snapshot_revision: 37,
    cards: [item(1, "top")],
  });

  const finish = findButton(root, "Finish date");
  const confirmationId = finish.getAttribute("aria-controls");
  assert.ok(confirmationId, "Finish date must identify its confirmation");
  const confirmation = descendants(root, "div").find(
    (node) => node.getAttribute("id") === confirmationId,
  );
  assert.ok(confirmation, "The controlled finish confirmation must be rendered");
});

test("a failed finish remains retryable and reports the failure", async () => {
  const root = new FakeNode("main");
  const failures = [];
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      snapshot_revision: 37,
      cards: [item(1, "top")],
    },
    {
      finish: async () => {
        throw new Error("Offline");
      },
      failure: (error) => failures.push(error.message),
    },
  );

  const finish = findButton(root, "Finish date");
  finish.click();
  findButton(root, "Confirm finish").click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(failures, ["Offline"]);
  assert.equal(finish.disabled, false);
  assert.equal(findButton(root, "Confirm finish").disabled, false);
  assert.match(root.textContent, /The date was not finished\. Try again\./);
});

test("review errors preserve the open page and expose retry", () => {
  const root = new FakeNode("main");
  const current = new FakeNode("article");
  current.textContent = "Current draft page";
  root.append(current);
  let retries = 0;
  renderReviewError(new FakeDocument(), root, "Offline", () => retries++);
  assert.match(root.textContent, /Current draft page/);
  findButton(root, "Retry").click();
  assert.equal(retries, 1);
});
