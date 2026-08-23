import assert from "node:assert/strict";
import test from "node:test";

import {
  reviewDestination,
  renderReviewError,
  renderReviewView,
} from "../../src/arxiv_digest/web/static/review_view.mjs";
import { FakeDocument, FakeNode, descendants, findButton } from "./dom_test_helper.mjs";

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

test("review uses server-provided dates, tiers, anchors, and a maximum of 20 cards", () => {
  const root = new FakeNode("main");
  const navigated = [];
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
  });

  assert.equal(descendants(root, "article").length, 20);
  assert.match(root.textContent, /Top/);
  assert.match(root.textContent, /Possible/);
  assert.match(root.textContent, /Other/);
  assert.match(root.textContent, /Page 2 of 7/);
  findButton(root, "Previous date").click();
  findButton(root, "Next date").click();
  findButton(root, "Next unreviewed").click();
  assert.deepEqual(navigated, [
    { date: "2026-07-31", anchor_event_id: null },
    { date: "2026-09-01", anchor_event_id: null },
    { date: "2026-09-04", anchor_event_id: null },
  ]);
  assert.deepEqual(reviewDestination(page, "next-page"), {
    date: "2026-08-21",
    anchor_event_id: 40,
  });
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

test("finish submits the opened snapshot revision after explicit confirmation", () => {
  const root = new FakeNode("main");
  const finished = [];
  renderReviewView(
    new FakeDocument(),
    root,
    {
      day: "2026-08-21",
      snapshot_revision: 37,
      page_number: 1,
      page_count: 1,
      cards: [item(1, "top")],
    },
    { finish: (...args) => finished.push(args) },
  );
  findButton(root, "Finish date").click();
  assert.equal(finished.length, 0);
  findButton(root, "Confirm finish").click();
  assert.deepEqual(finished, [["2026-08-21", 37]]);
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
