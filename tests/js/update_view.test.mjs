import assert from "node:assert/strict";
import test from "node:test";

import { renderUpdateNotice } from "../../src/arxiv_digest/web/static/update_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
} from "./dom_test_helper.mjs";


test("an available release renders a safe GitHub update link", () => {
  const notice = new FakeNode("aside");

  renderUpdateNotice(new FakeDocument(), notice, {
    status: "available_manual",
    installed_version: "0.2.1",
    available_version: "0.3.0",
    url: "https://evil.test/ignore-this",
  });

  assert.equal(notice.hidden, false);
  assert.match(notice.textContent, /arXiv Digest 0\.3\.0 is available/);
  assert.match(notice.textContent, /installed: 0\.2\.1/);
  const links = descendants(notice, "a");
  assert.equal(links.length, 1);
  assert.equal(descendants(notice, "button").length, 0);
  assert.equal(links[0].textContent, "View update instructions");
  assert.equal(
    links[0].getAttribute("aria-label"),
    "View update instructions (opens in a new tab)",
  );
  assert.equal(
    links[0].getAttribute("href"),
    "https://github.com/yuzhangmath/arxiv-digest/releases/tag/v0.3.0",
  );
  assert.equal(links[0].getAttribute("target"), "_blank");
  assert.equal(links[0].getAttribute("rel"), "noopener noreferrer");
});


test("a missing update leaves the live region present and empty", () => {
  const notice = new FakeNode("aside");
  notice.hidden = true;

  renderUpdateNotice(new FakeDocument(), notice, {
    status: "available_manual",
    installed_version: "0.2.1",
    available_version: "0.3.0",
  });
  renderUpdateNotice(new FakeDocument(), notice, { status: "current" });

  assert.equal(notice.hidden, false);
  assert.equal(notice.textContent, "");
  assert.equal(descendants(notice, "a").length, 0);
});

test("inconclusive discovery uses only the canonical generic releases page", () => {
  const notice = new FakeNode("aside");
  renderUpdateNotice(new FakeDocument(), notice, {
    status: "manual_fallback",
    installed_version: "0.2.1", release_notes_url: "https://evil.test/",
  });
  assert.match(notice.textContent, /Could not check for updates/);
  assert.equal(descendants(notice, "a")[0].getAttribute("href"),
    "https://github.com/yuzhangmath/arxiv-digest/releases");
});

test("unchanged results preserve the live region and focused link nodes", () => {
  const notice = new FakeNode("aside");
  const document = new FakeDocument();
  const update = {
    status: "available_manual", installed_version: "0.2.1", available_version: "0.3.0",
  };
  renderUpdateNotice(document, notice, update);
  const link = descendants(notice, "a")[0];
  renderUpdateNotice(document, notice, {...update});
  assert.ok(link);
  assert.equal(descendants(notice, "a")[0], link);
});

test("pending/current states stay quiet and invalid versions never become links", () => {
  for (const update of [
    {status: "idle"}, {status: "checking"},
    {status: "current", installed_version: "0.2.1"},
    {status: "available_manual", installed_version: "0.2.1", available_version: "../bad"},
    {status: "available_manual", installed_version: "01.2.1", available_version: "0.3.0"},
    null, {},
  ]) {
    const notice = new FakeNode("aside");
    renderUpdateNotice(new FakeDocument(), notice, update);
    assert.equal(notice.hidden, false);
    assert.equal(notice.textContent, "");
    assert.equal(descendants(notice, "a").length, 0);
  }
});
