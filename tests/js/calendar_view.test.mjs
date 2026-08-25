import assert from "node:assert/strict";
import test from "node:test";

import { renderCalendar } from "../../src/arxiv_digest/web/static/calendar_view.mjs";
import { FakeDocument, FakeNode, descendants } from "./dom_test_helper.mjs";

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
    "2026-08-01: 3 paper announcements, unreviewed",
  );
  buttons[0].click();
  assert.deepEqual(selected, ["2026-08-01"]);
});

test("calendar visibly labels singular and plural paper-announcement counts", () => {
  const root = new FakeNode("div");
  renderCalendar(new FakeDocument(), root, [
    { date: "2026-08-01", count: 1, status: "unreviewed" },
    { date: "2026-08-02", count: 3, status: "unreviewed" },
  ]);

  const buttons = descendants(root, "button");
  assert.equal(buttons[0].textContent, "2026-08-01\n1 paper announcement");
  assert.equal(buttons[1].textContent, "2026-08-02\n3 paper announcements");
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
