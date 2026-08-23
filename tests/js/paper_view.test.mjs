import assert from "node:assert/strict";
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

function paper() {
  return {
    arxiv_id: "2608.01234",
    version: "v2",
    title: `A safe title ${hostile}`,
    authors: [`Ada ${hostile}`, "Grace Hopper"],
    abstract: `Abstract ${hostile}`,
    comments: `Comments ${hostile}`,
    ranking_text: `Why: ${hostile}`,
    tier: "top",
    observations: ["math.AG", "math.CO"],
    date_label: "Recovered",
  };
}

test("paper metadata and ranking prose are inserted only as literal text", () => {
  const root = new FakeNode("div");
  renderPaperCard(new FakeDocument(), root, paper(), {});
  assert.match(root.textContent, /<img src=x/);
  assert.match(root.textContent, /<script>bad\(\)<\/script>/);
  assert.equal(descendants(root, "img").length, 0);
  assert.equal(descendants(root, "script").length, 0);
  assert.equal(descendants(root, "details").length, 1);
  assert.match(descendants(root, "summary")[0].textContent, /abstract/i);
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
  source.version = "v99";
  findButton(root, "Save").click();
  findButton(root, "Download PDF").click();
  findButton(root, "Save + PDF").click();
  assert.deepEqual(calls, [
    ["save", "2608.01234", "v2"],
    ["download", "2608.01234", "v2"],
    ["both", "2608.01234", "v2"],
  ]);
});

test("explicit API card projection shows confidence, observations, and discovery labels", () => {
  const calls = [];
  const root = new FakeNode("div");
  renderPaperCard(
    new FakeDocument(),
    root,
    {
      event_id: 42,
      arxiv_id: "2608.04200",
      announced_version: 3,
      title: "Projected paper",
      authors: ["Safe Author"],
      abstract: "Safe abstract",
      category_observations: ["math.AG", "math.CO"],
      date_label: "arXiv mailing date",
      confidence_label: "Recovered announcement",
      newly_discovered: true,
      tier: "possible",
      reasons: [{ kind: "keyword", label: "Matched selected keyword", location: "title" }],
    },
    { download: (...values) => calls.push(values) },
  );
  assert.match(root.textContent, /Recovered announcement/);
  assert.match(root.textContent, /arXiv mailing date/);
  assert.match(root.textContent, /Newly discovered/);
  assert.match(root.textContent, /math\.AG · math\.CO/);
  findButton(root, "Download PDF").click();
  assert.deepEqual(calls, [["2608.04200", 3]]);
  const pdf = descendants(root, "a").find((link) => /PDF/.test(link.textContent));
  assert.equal(pdf.getAttribute("href"), "https://arxiv.org/pdf/2608.04200v3.pdf");
});

test("versionless events use only the server-resolved download version and render full metadata", () => {
  const calls = [];
  const root = new FakeNode("div");
  renderPaperCard(
    new FakeDocument(),
    root,
    {
      event_id: 77,
      arxiv_id: "2608.07700",
      announced_version: null,
      download_version: 4,
      title: "Versionless recovered event",
      authors: ["Safe Author"],
      abstract: "Abstract",
      comments: `18 pages ${hostile}`,
      journal_ref: "Synthetic Journal 1 (2026)",
      doi: "10.0000/synthetic-doi",
      category_observations: ["math.AG"],
      confidence_label: "Inferred update",
      tier: "other",
      reasons: [],
    },
    { download: (...values) => calls.push(values) },
  );
  assert.match(root.textContent, /Announcement version unavailable/);
  assert.match(root.textContent, /18 pages <img/);
  assert.match(root.textContent, /Synthetic Journal/);
  assert.match(root.textContent, /10\.0000\/synthetic-doi/);
  assert.equal(descendants(root, "img").length, 0);
  findButton(root, "Download PDF").click();
  assert.deepEqual(calls, [["2608.07700", 4]]);
  const pdf = descendants(root, "a").find((link) => /PDF/.test(link.textContent));
  assert.equal(pdf.getAttribute("href"), "https://arxiv.org/pdf/2608.07700v4.pdf");
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
