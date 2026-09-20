import assert from "node:assert/strict";
import test from "node:test";

import { renderCalendar } from "../../src/arxiv_digest/web/static/calendar_view.mjs";
import { FakeDocument, FakeNode, descendants } from "./dom_test_helper.mjs";

test("calendar exposes review states through labeled native buttons", () => {
  const root = new FakeNode("div");
  const grid = renderCalendar(new FakeDocument(), root, [
    {
      day: "2026-08-01",
      total_papers: 14,
      unreviewed_papers: 0,
      finished: true,
    },
    {
      day: "2026-08-02",
      total_papers: 16,
      unreviewed_papers: 16,
      finished: false,
    },
    {
      day: "2026-08-03",
      total_papers: 12,
      unreviewed_papers: 4,
      finished: false,
    },
  ]);

  assert.equal(grid.getAttribute("role"), "list");
  assert.equal(grid.children.length, 3);
  const buttons = grid.children.map((item) => {
    assert.equal(item.getAttribute("role"), "listitem");
    assert.equal(item.children.length, 1);
    assert.equal(item.children[0].tagName, "BUTTON");
    assert.equal(item.children[0].getAttribute("role"), null);
    return item.children[0];
  });
  assert.deepEqual(
    buttons.map((button) => button.textContent),
    [
      "2026-08-01\n14 papers\n✓ Reviewed",
      "2026-08-02\n16 papers\nUnreviewed",
      "2026-08-03\n12 papers\nPartial",
    ],
  );
  assert.deepEqual(
    buttons.map((button) => button.getAttribute("aria-label")),
    [
      "2026-08-01: 14 papers, reviewed",
      "2026-08-02: 16 papers, unreviewed",
      "2026-08-03: 12 papers, partial",
    ],
  );
});

test("calendar abbreviates announcement counts to papers", () => {
  const root = new FakeNode("div");
  renderCalendar(new FakeDocument(), root, [
    { date: "2026-08-03", count: 12, status: "unreviewed" },
  ]);

  const button = descendants(root, "button")[0];
  assert.equal(button.textContent, "2026-08-03\n12 papers\nUnreviewed");
  assert.equal(
    button.getAttribute("aria-label"),
    "2026-08-03: 12 papers, unreviewed",
  );
});

test("calendar renders accessible server-date buttons with count and status", () => {
  const root = new FakeNode("div");
  const selected = [];
  renderCalendar(
    new FakeDocument(),
    root,
    [
      { date: "2026-08-01", count: 3, status: "unreviewed" },
      { date: "2026-08-02", count: 0, status: "reviewed" },
    ],
    (date) => selected.push(date),
  );
  const buttons = descendants(root, "button");
  assert.equal(buttons.length, 2);
  assert.equal(
    buttons[0].getAttribute("aria-label"),
    "2026-08-01: 3 papers, unreviewed",
  );
  buttons[0].click();
  assert.deepEqual(selected, ["2026-08-01"]);
});

test("calendar visibly labels singular and plural paper counts", () => {
  const root = new FakeNode("div");
  renderCalendar(new FakeDocument(), root, [
    { date: "2026-08-01", count: 1, status: "unreviewed" },
    { date: "2026-08-02", count: 3, status: "unreviewed" },
  ]);

  const buttons = descendants(root, "button");
  assert.equal(buttons[0].textContent, "2026-08-01\n1 paper\nUnreviewed");
  assert.equal(buttons[1].textContent, "2026-08-02\n3 papers\nUnreviewed");
});

test("calendar dispatches only the stored date even after caller mutation", () => {
  const entry = { date: "2026-08-03", count: 1, status: "partial" };
  const root = new FakeNode("div");
  const selected = [];
  renderCalendar(new FakeDocument(), root, [entry], (date) => selected.push(date));
  entry.date = "2099-01-01";
  descendants(root, "button")[0].click();
  assert.deepEqual(selected, ["2026-08-03"]);
});

test("domain calendar summaries expose partial and reviewed status", () => {
  const root = new FakeNode("div");
  renderCalendar(new FakeDocument(), root, [
    {
      day: "2026-08-04",
      total_papers: 10,
      unreviewed_papers: 2,
      newly_discovered: 1,
      finished: false,
    },
    {
      day: "2026-08-05",
      total_papers: 4,
      unreviewed_papers: 0,
      newly_discovered: 0,
      finished: true,
    },
  ]);
  const buttons = descendants(root, "button");
  assert.match(buttons[0].getAttribute("aria-label"), /partial/);
  assert.match(buttons[1].getAttribute("aria-label"), /reviewed/);
});

test("failed-only dates are visible without a paper count or review action", () => {
  const root = new FakeNode("div");
  const selected = [];
  const grid = renderCalendar(new FakeDocument(), root, [{
    day: "2026-08-04",
    total_papers: null,
    unreviewed_papers: null,
    newly_discovered: null,
    finished: null,
    retrieval_failed: true,
  }], (date) => selected.push(date));

  assert.equal(grid.children.length, 1);
  const item = grid.children[0];
  assert.equal(item.getAttribute("role"), "listitem");
  assert.equal(item.getAttribute("aria-label"), "2026-08-04: Retrieval failed");
  assert.equal(item.textContent, "2026-08-04\nRetrieval failed");
  assert.equal(descendants(root, "button").length, 0);
  item.children[0].click();
  assert.deepEqual(selected, []);
  assert.equal(item.children[0].getAttribute("tabindex"), null);
  assert.equal(item.children[0].dataset.status, undefined);
});

test("partly recovered dates label confirmed counts and retain review navigation", () => {
  const root = new FakeNode("div");
  const selected = [];
  renderCalendar(new FakeDocument(), root, [
    {
      day: "2026-08-03",
      total_papers: 4,
      unreviewed_papers: 2,
      newly_discovered: 1,
      finished: false,
      retrieval_failed: true,
    },
    {
      day: "2026-08-04",
      total_papers: 1,
      unreviewed_papers: 0,
      newly_discovered: 0,
      finished: true,
      retrieval_failed: true,
    },
  ], (date) => selected.push(date));

  const buttons = descendants(root, "button");
  assert.equal(buttons[0].textContent, "2026-08-03\n4 confirmed papers\nPartial\nSome retrievals failed");
  assert.equal(buttons[0].getAttribute("aria-label"), "2026-08-03: 4 confirmed papers, partial, some retrievals failed");
  assert.equal(buttons[1].textContent, "2026-08-04\n1 confirmed paper\n✓ Reviewed\nSome retrievals failed");
  buttons[0].click();
  assert.deepEqual(selected, ["2026-08-03"]);
});

test("refreshing a recovered date replaces its failed placeholder with a review button", () => {
  const root = new FakeNode("div");
  const document = new FakeDocument();
  renderCalendar(document, root, [{
    day: "2026-08-04", total_papers: null, finished: null, retrieval_failed: true,
  }]);
  const selected = [];
  renderCalendar(document, root, [{
    day: "2026-08-04",
    total_papers: 3,
    unreviewed_papers: 3,
    finished: false,
    retrieval_failed: false,
  }], (date) => selected.push(date));

  assert.equal(root.children.length, 1);
  assert.doesNotMatch(root.textContent, /failed/i);
  const buttons = descendants(root, "button");
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].getAttribute("aria-label"), "2026-08-04: 3 papers, unreviewed");
  buttons[0].click();
  assert.deepEqual(selected, ["2026-08-04"]);
});

test("dates with missing abstracts retain review status and date navigation", () => {
  const root = new FakeNode("div");
  const selected = [];
  const grid = renderCalendar(new FakeDocument(), root, [{
    day: "2026-08-04",
    total_papers: 3,
    unreviewed_papers: 3,
    newly_discovered: 0,
    finished: false,
    abstracts_pending: true,
    abstracts_ready: 2,
    missing_abstracts: 1,
    retrieval_failed: false,
  }], (date) => selected.push(date));
  const item = grid.children[0];
  assert.equal(item.textContent, "2026-08-04\n3 papers\nUnreviewed\n2 of 3 abstracts available");
  const button = descendants(root, "button")[0];
  assert.equal(button.getAttribute("aria-label"), "2026-08-04: 3 papers, unreviewed, 2 of 3 abstracts available");
  assert.equal(button.disabled, false);
  assert.equal(button.dataset.status, "unreviewed");
  button.click();
  assert.deepEqual(selected, ["2026-08-04"]);
});

test("finished dates hide missing abstract progress and preserve retrieval failures", () => {
  const root = new FakeNode("div");
  const document = new FakeDocument();
  const pending = {
    day: "2026-08-04", total_papers: 3, abstracts_ready: 0, missing_abstracts: 3,
    abstracts_pending: true, unreviewed_papers: 0, finished: true,
    retrieval_failed: true,
  };
  renderCalendar(document, root, [pending]);
  assert.equal(root.textContent, "2026-08-04\n3 confirmed papers\n✓ Reviewed\nSome retrievals failed");
  const button = descendants(root, "button")[0];
  assert.equal(button.dataset.status, "reviewed");
  assert.equal(button.getAttribute("aria-label"),
    "2026-08-04: 3 confirmed papers, reviewed, some retrievals failed");
  renderCalendar(document, root, [{
    ...pending, abstracts_pending: false, abstracts_ready: 3, missing_abstracts: 0,
    unreviewed_papers: 3, finished: false,
  }]);
  assert.doesNotMatch(root.textContent, /Waiting for abstracts/);
  assert.equal(descendants(root, "button")[0].getAttribute("aria-label"),
    "2026-08-04: 3 confirmed papers, unreviewed, some retrievals failed");
});

test("explicit review status hides abstract progress only for reviewed dates", () => {
  const root = new FakeNode("div");
  renderCalendar(new FakeDocument(), root, [
    {
      date: "2026-08-04", count: 3, status: "reviewed",
      abstracts_pending: true, abstracts_ready: 2,
    },
    {
      date: "2026-08-05", count: 3, status: "partial",
      abstracts_pending: true, abstracts_ready: 2,
    },
  ]);
  const buttons = descendants(root, "button");
  assert.equal(buttons[0].textContent, "2026-08-04\n3 papers\n✓ Reviewed");
  assert.equal(buttons[0].getAttribute("aria-label"), "2026-08-04: 3 papers, reviewed");
  assert.equal(buttons[1].textContent, "2026-08-05\n3 papers\nPartial\n2 of 3 abstracts available");
  assert.equal(buttons[1].getAttribute("aria-label"), "2026-08-05: 3 papers, partial, 2 of 3 abstracts available");
});
