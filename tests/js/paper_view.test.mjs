import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  arxivLinks,
  renderPaperCard,
} from "../../src/arxiv_digest/web/static/paper_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

const hostile = '<img src=x onerror="steal()"><script>bad()</script>';
const stylesheet = readFileSync(
  new URL("../../src/arxiv_digest/web/static/styles.css", import.meta.url),
  "utf8",
);
const paperViewSource = readFileSync(
  new URL("../../src/arxiv_digest/web/static/paper_view.mjs", import.meta.url),
  "utf8",
);

function paper() {
  return {
    arxiv_id: "2608.01234",
    resolved_announcement_version: 2,
    latest_known_version: 2,
    version_resolution: "atom_confirmed",
    version_label: "Announced v2 — Atom-confirmed",
    daily_list_date: "2026-08-21",
    event_label: "New submission",
    support_categories: ["math.AG", "math.CO"],
    title: `A safe title ${hostile}`,
    authors: [`Ada ${hostile}`, "Grace Hopper"],
    abstract: `Abstract ${hostile}`,
    comments: `Comments ${hostile}`,
    ranking_text: `Why: ${hostile}`,
    tier: "top",
  };
}

test("paper metadata and ranking prose are inserted only as literal text", () => {
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {});
  assert.match(root.textContent, /<img src=x/);
  assert.match(root.textContent, /<script>bad\(\)<\/script>/);
  assert.equal(descendants(root, "img").length, 0);
  assert.equal(descendants(root, "script").length, 0);
  assert.ok(
    descendants(root, "summary").some((summary) => /abstract/i.test(summary.textContent)),
  );
});

test("paper ranking rationale is initially hidden in a disclosure", () => {
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {});

  const explanation = descendants(root, "details").find((node) =>
    node.className === "ranking-explanation"
  );
  assert.ok(explanation, "ranking disclosure was not rendered");
  assert.equal(explanation.getAttribute("open"), null);
  assert.equal(descendants(explanation, "summary")[0].textContent, "Why this ranking");
  assert.match(explanation.textContent, /Why: <img src=x/);
});

test("review paper titles do not impose a fixed reading-width cap", () => {
  const titleRule = stylesheet.match(/\.paper-card h3\s*\{([^}]*)\}/)?.[1] ?? "";
  assert.doesNotMatch(titleRule, /max-width\s*:/);
});

test("links are derived from stored IDs and restricted to HTTPS arXiv hosts", () => {
  assert.deepEqual(arxivLinks("2608.01234", "v2"), {
    abstract: "https://arxiv.org/abs/2608.01234v2",
    pdf: "https://arxiv.org/pdf/2608.01234v2.pdf",
  });
  assert.throws(() => arxivLinks("https://evil.test/x", "v1"), /arXiv ID/);
  assert.throws(() => arxivLinks("2608.01234", "../../x"), /version/);

  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {});
  for (const link of descendants(root, "a")) {
    assert.equal(new URL(link.getAttribute("href")).protocol, "https:");
    assert.equal(new URL(link.getAttribute("href")).hostname, "arxiv.org");
    assert.equal(link.getAttribute("rel"), "noopener noreferrer");
  }
});

test("card actions close over immutable stored IDs and versions", () => {
  const calls = [];
  const source = paper();
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, source, {
    save: (...args) => calls.push(["save", ...args]),
    download: (...args) => calls.push(["download", ...args]),
    saveAndDownload: (...args) => calls.push(["both", ...args]),
  });
  source.arxiv_id = "9999.99999";
  source.resolved_announcement_version = 99;
  findButton(root, "Save").click();
  findButton(root, "Download PDF").click();
  findButton(root, "Save + PDF").click();
  assert.deepEqual(calls, [
    ["save", "2608.01234", 2],
    ["download", "2608.01234", 2],
    ["both", "2608.01234", 2],
  ]);
});

test("save shows pending and successful Library feedback", async () => {
  let resolveSave;
  const pendingSave = new Promise((resolve) => {
    resolveSave = resolve;
  });
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {
    save: () => pendingSave,
  });

  const save = findButton(root, "Save");
  save.click();

  assert.equal(save.disabled, true);
  assert.equal(save.textContent, "Saving…");
  const actionStatus = descendants(root, "p").find((node) =>
    node.className === "paper-action-status"
  );
  assert.equal(actionStatus.getAttribute("role"), "status");
  assert.equal(actionStatus.getAttribute("aria-live"), "polite");
  assert.equal(actionStatus.textContent, "Saving paper…");

  resolveSave({ saved: true });
  await pendingSave;
  await Promise.resolve();

  assert.equal(save.disabled, true);
  assert.equal(save.textContent, "Saved");
  assert.equal(actionStatus.textContent, "Paper saved to Library.");
});

test("failed saves restore the action and report the error", async () => {
  let rejectSave;
  const pendingSave = new Promise((_resolve, reject) => {
    rejectSave = reject;
  });
  const failures = [];
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {
    save: () => pendingSave,
    failure: (error) => failures.push(error),
  });

  const save = findButton(root, "Save");
  save.click();
  const error = new Error("synthetic save failure");
  rejectSave(error);
  await pendingSave.catch(() => {});
  await Promise.resolve();

  assert.equal(save.disabled, false);
  assert.equal(save.textContent, "Save");
  const actionStatus = descendants(root, "p").find((node) =>
    node.className === "paper-action-status"
  );
  assert.equal(actionStatus.textContent, "Paper was not saved. Try again.");
  assert.deepEqual(failures, [error]);
});

test("confirmed card projection shows only active daily-list support categories", () => {
  const calls = [];
  const root = new FakeNode("div");
  renderPaperCard(
    new FakeDocument(),
    root,
    {
      event_id: 42,
      arxiv_id: "2608.04200",
      resolved_announcement_version: 3,
      latest_known_version: 4,
      version_resolution: "chronology_matched",
      version_label: "Version v3 — matched by chronology",
      daily_list_date: "2026-08-20",
      title: "Projected paper",
      authors: ["Safe Author"],
      abstract: "Safe abstract",
      support_categories: ["math.AT"],
      event_label: "Replacement",
      newly_discovered: true,
      tier: "possible",
      reasons: [{ kind: "keyword", label: "Matched selected keyword", location: "title" }],
    },
    { download: (...values) => calls.push(values) },
  );
  assert.match(root.textContent, /arXiv daily-list date: 2026-08-20/);
  assert.match(root.textContent, /Replacement/);
  assert.match(root.textContent, /Version v3 — matched by chronology/);
  assert.doesNotMatch(root.textContent, /Announced v3/);
  assert.doesNotMatch(root.textContent, /Current feed|Inferred from version history/);
  assert.match(root.textContent, /Newly discovered/);
  const labels = descendants(root, "p").find((node) =>
    node.className === "paper-labels"
  );
  assert.match(labels.textContent, /Recovered under: math\.AT(?: ·|$)/);
  assert.doesNotMatch(labels.textContent, /math\.AC|math\.RT/);
  findButton(root, "Download PDF").click();
  assert.deepEqual(calls, [["2608.04200", 3]]);
  const pdf = descendants(root, "a").find((link) => /PDF/.test(link.textContent));
  assert.equal(pdf.getAttribute("href"), "https://arxiv.org/pdf/2608.04200v3.pdf");
});

test("unconfirmed events stay unpinned while latest-version downloads are explicit", () => {
  const calls = [];
  const root = new FakeNode("div");
  renderPaperCard(
    new FakeDocument(),
    root,
    {
      event_id: 78,
      arxiv_id: "2608.07800",
      resolved_announcement_version: null,
      latest_known_version: 4,
      version_resolution: "unconfirmed",
      version_label: "Version not confirmed",
      daily_list_date: "2026-08-21",
      event_label: "Replacement",
      support_categories: ["math.AG"],
      title: "Unconfirmed daily-list event",
      authors: ["Safe Author"],
      abstract: "Abstract",
      tier: "other",
      reasons: [],
    },
    {
      save: (...values) => calls.push(["save", ...values]),
      download: (...values) => calls.push(["download", ...values]),
      saveAndDownload: (...values) => calls.push(["both", ...values]),
    },
  );

  assert.match(root.textContent, /Version not confirmed/);
  assert.match(root.textContent, /arXiv daily-list date: 2026-08-21/);
  assert.match(root.textContent, /Recovered under: math\.AG/);
  findButton(root, "Save").click();
  findButton(
    root,
    "Download latest v4 — announcement version unconfirmed",
  ).click();
  findButton(
    root,
    "Save unpinned + download latest v4 — announcement version unconfirmed",
  ).click();
  assert.deepEqual(calls, [
    ["save", "2608.07800", null],
    ["download", "2608.07800", 4],
    ["both", "2608.07800", null, 4],
  ]);
  const abstract = descendants(root, "a").find((link) =>
    /Abstract/.test(link.textContent)
  );
  const pdf = descendants(root, "a").find((link) => /PDF/.test(link.textContent));
  assert.equal(abstract.textContent, "Abstract on arXiv (latest version)");
  assert.equal(pdf.textContent, "PDF on arXiv (latest version)");
  assert.equal(abstract.getAttribute("href"), "https://arxiv.org/abs/2608.07800");
  assert.equal(pdf.getAttribute("href"), "https://arxiv.org/pdf/2608.07800.pdf");
});

test("missing latest-version controls support the browser HTMLCollection contract", () => {
  assert.doesNotMatch(paperViewSource, /controls\.children\.slice\(/);

  const root = new FakeNode("div");
  renderPaperCard(
    new FakeDocument(),
    root,
    {
      event_id: 48,
      arxiv_id: "2608.04800",
      resolved_announcement_version: null,
      latest_known_version: null,
      version_resolution: "unconfirmed",
      version_label: "Version not confirmed",
      daily_list_date: "2026-08-24",
      support_categories: ["math.AT"],
      title: "No known PDF version",
      authors: ["Safe Author"],
      abstract: "Safe abstract",
    },
    {},
  );

  assert.equal(
    findButton(root, "PDF unavailable — announcement version unconfirmed").disabled,
    true,
  );
  assert.equal(
    findButton(root, "Save unpinned (PDF unavailable)").disabled,
    true,
  );
});

test("paper titles render delimited math inline through the audited KaTeX runtime", () => {
  const calls = [];
  const previous = globalThis.katex;
  globalThis.katex = {
    render(source, output, options) {
      calls.push({ source, options });
      output.textContent = `rendered:${source}`;
    },
  };
  try {
    const root = new FakeNode("div");
    renderPaperCard(
      new FakeDocument(),
      root,
      { ...paper(), title: String.raw`Towards $\mathbb{A}^1$-homotopy` },
      {},
    );

    const title = descendants(root, "h3")[0];
    assert.equal(title.textContent, String.raw`Towards rendered:\mathbb{A}^1-homotopy`);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].source, String.raw`\mathbb{A}^1`);
    assert.equal(calls[0].options.displayMode, false);
    assert.equal(calls[0].options.trust, false);
  } finally {
    if (previous === undefined) delete globalThis.katex;
    else globalThis.katex = previous;
  }
});

test("paper abstracts render only delimited math through the audited KaTeX runtime", () => {
  const calls = [];
  const previous = globalThis.katex;
  globalThis.katex = {
    render(source, output, options) {
      calls.push({ source, options });
      output.textContent = `rendered:${source}`;
    },
  };
  try {
    const root = new FakeNode("div");
    renderPaperCard(
      new FakeDocument(),
      root,
      { ...paper(), abstract: "Energy is $E=mc^2$ in this model." },
      {},
    );
    const abstract = descendants(root, "p").find((node) =>
      node.className === "paper-abstract"
    );
    assert.equal(abstract.textContent, "Energy is rendered:E=mc^2 in this model.");
    assert.equal(calls.length, 1);
    assert.equal(calls[0].source, "E=mc^2");
    assert.equal(calls[0].options.trust, false);
  } finally {
    if (previous === undefined) delete globalThis.katex;
    else globalThis.katex = previous;
  }
});
