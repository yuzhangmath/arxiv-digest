import assert from "node:assert/strict";
import test from "node:test";

import {
  InterestsController,
  InterestsDraft,
  renderInterestsView,
} from "../../src/arxiv_digest/web/static/interests_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

test("only selected suggestions and typed custom values change interests", () => {
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: ["existing keyword"],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  const before = draft.snapshot();
  draft.noteSearch("spectral");
  draft.noteSuggestionViewed("suggestion_term_1");
  draft.notePage(3);
  assert.deepEqual(draft.snapshot(), before);

  draft.setSuggested("keywords", "new keyword", true);
  draft.addCustom("phrases", "custom phrase");
  draft.addCustom("authors", "Example Author");
  draft.addCustom("seed_papers", "2608.01234");
  draft.setSuggested("keywords", "new keyword", false);

  assert.deepEqual(draft.snapshot(), {
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    category_configs: [],
    keywords: ["existing keyword"],
    phrases: ["custom phrase"],
    authors: ["Example Author"],
    seed_papers: ["2608.01234"],
  });
  assert.equal(draft.dirty, true);
});

test("saved interests render as removable entries", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: ["topology"],
    phrases: ["derived geometry"],
    authors: ["Ada Example"],
    seed_papers: ["2608.01234"],
  });

  renderInterestsView(new FakeDocument(), root, {
    draft,
    seed_paper_details: [
      {
        arxiv_id: "2608.01234",
        title: "A geometric seed paper",
        authors: ["Ada Example"],
      },
    ],
    suggestions: {},
  });

  for (const value of [
    "math.AG",
    "A geometric seed paper",
    "2608.01234",
    "topology",
    "derived geometry",
    "Ada Example",
  ]) {
    assert.match(root.textContent, new RegExp(value.replace(".", "\\.")));
  }

  findButton(root, "Remove 2608.01234").click();
  findButton(root, "Remove topology").click();
  findButton(root, "Remove derived geometry").click();
  findButton(root, "Remove Ada Example").click();

  assert.deepEqual(draft.snapshot(), {
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    category_configs: [],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  assert.equal(draft.dirty, true);
});

test("Interests explains that candidate suggestions and seeds are non-durable", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });

  renderInterestsView(new FakeDocument(), root, { draft, suggestions: {} });

  assert.match(
    root.textContent,
    /candidate papers do not populate Review, Calendar, or Library/i,
  );
  assert.match(root.textContent, /selecting a seed paper does not save it/i);
});

test("interest additions start behind four explicit add controls", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });

  renderInterestsView(new FakeDocument(), root, {
    draft,
    coverage_min: "2026-06-01",
    coverage_max: "2026-08-22",
    suggestions: {
      categories: [{ category: "math.NT", set_spec: "arXiv:math.NT" }],
      seed_papers: [
        {
          arxiv_id: "2608.09999",
          title: "A suggested arithmetic paper",
        },
      ],
      keywords: [{ value: "topology" }],
      phrases: [{ value: "derived geometry" }],
      authors: [{ name: "Ada Example" }],
    },
  });

  for (const label of ["Add category", "Add seed paper", "Add terms", "Add author"]) {
    assert.equal(
      descendants(root, "button").filter((button) => button.textContent === label).length,
      1,
    );
  }
  const panels = root.querySelectorAll(".interest-addition-panel");
  assert.equal(panels.length, 4);
  assert.ok(panels.every((panel) => panel.hidden));
  const seedPanel = panels.find(
    (panel) => panel.dataset.interestAdd === "seed_papers",
  );
  assert.match(seedPanel.textContent, /A suggested arithmetic paper/);
  assert.match(seedPanel.textContent, /2608\.09999/);

  findButton(root, "Add seed paper").click();
  assert.equal(seedPanel.hidden, false);
});

test("category controls keep the visible draft in sync and protect the final category", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });

  renderInterestsView(new FakeDocument(), root, {
    draft,
    coverage_min: "2026-06-01",
    coverage_max: "2026-08-22",
    suggestions: {
      categories: [{ category: "math.NT", set_spec: "arXiv:math.NT" }],
    },
  });

  const removeAg = findButton(root, "Remove math.AG");
  assert.equal(removeAg.disabled, true);
  assert.match(removeAg.getAttribute("title"), /at least one category/i);

  findButton(root, "Add category").click();
  const addNt = findButton(root, "Add math.NT");
  assert.equal(addNt.disabled, true);
  addNt.click();
  assert.deepEqual(
    draft.snapshot().categories.map((item) => item.category),
    ["math.AG"],
  );
  const coverage = descendants(root, "input").find(
    (node) => node.getAttribute("id") === "category-coverage-0",
  );
  assert.equal(coverage.getAttribute("min"), "2026-06-01");
  assert.equal(coverage.getAttribute("max"), "2026-08-22");
  coverage.value = "2026-05-01";
  coverage.dispatchEvent({ type: "input" });
  assert.equal(addNt.disabled, true);
  coverage.value = "2026-07-01";
  coverage.dispatchEvent({ type: "input" });
  assert.equal(addNt.disabled, false);
  addNt.click();

  assert.deepEqual(
    draft.snapshot().categories.map((item) => item.category),
    ["math.AG", "math.NT"],
  );
  const removeNt = findButton(root, "Remove math.NT");
  assert.equal(removeAg.disabled, false);
  assert.equal(removeNt.disabled, false);

  removeAg.click();
  assert.deepEqual(
    draft.snapshot().categories.map((item) => item.category),
    ["math.NT"],
  );
  assert.equal(
    descendants(root, "li").find((item) => /math\.AG/.test(item.textContent)).hidden,
    true,
  );
  assert.equal(removeNt.disabled, true);
});

test("terms use one visual section while preserving backend classification", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  renderInterestsView(new FakeDocument(), root, {
    draft,
    suggestions: {
      keywords: [{ value: "topology" }],
      phrases: [{ value: "derived geometry" }],
    },
  });

  const headings = [
    ...descendants(root, "h2"),
    ...descendants(root, "h3"),
  ].map((heading) => heading.textContent);
  assert.equal(headings.filter((heading) => heading === "Terms").length, 1);
  assert.equal(headings.includes("Keywords"), false);
  assert.equal(headings.includes("Phrases"), false);
  assert.equal(
    descendants(root, "button").filter(
      (button) => button.textContent === "Add custom term",
    ).length,
    1,
  );

  findButton(root, "Add terms").click();
  const input = descendants(root, "input").find(
    (node) => node.getAttribute("id") === "terms-custom",
  );
  input.value = "topology";
  findButton(root, "Add custom term").click();
  input.value = "derived geometry";
  findButton(root, "Add custom term").click();

  assert.deepEqual(draft.snapshot().keywords, ["topology"]);
  assert.deepEqual(draft.snapshot().phrases, ["derived geometry"]);
});

test("suggestion refresh copy is concise and explicit about repeats", () => {
  const root = new FakeNode("main");
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });

  renderInterestsView(new FakeDocument(), root, {
    draft,
    suggestions: {},
    suggestions_generated_at: "2026-08-23T12:01:52.895646+00:00",
  });

  assert.match(root.textContent, /Suggestion pool created Aug 23, 2026/);
  assert.doesNotMatch(root.textContent, /T12:01|\+00:00/);
  assert.match(root.textContent, /Previously shown suggestions may reappear\./);
  assert.match(root.textContent, /only after you choose Update interests/i);
  assert.equal(findButton(root, "Update interests").disabled, true);
  assert.equal(
    descendants(root, "button").some(
      (button) => button.textContent === "Save interests",
    ),
    false,
  );
});

test("category changes carry the server set spec and explicit coverage without broad cache clearing", async () => {
  const calls = [];
  const api = {
    async json(key, path, options) {
      calls.push({ key, path, options });
      return { revision: 5, invalidated_categories: ["math.NT"] };
    },
  };
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  assert.throws(() => draft.addCategory("math.NT", "2026-07-01"), /server/i);

  draft.addCategory(
    { category: "math.NT", set_spec: "arXiv:math.NT" },
    "2026-07-01",
  );
  draft.removeCategory("math.AG");
  assert.equal(calls.length, 0, "editing a draft must not publish it");

  const result = await new InterestsController(api).save(draft);
  assert.deepEqual(result, {
    revision: 5,
    invalidated_categories: ["math.NT"],
  });
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, "/api/v1/interests");
  assert.equal(calls[0].options.method, "PUT");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    expected_revision: 4,
    categories: [{ category: "math.NT", set_spec: "arXiv:math.NT" }],
    category_configs: [
      {
        category: "math.NT",
        set_spec: "arXiv:math.NT",
        coverage_start: "2026-07-01",
      },
    ],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  assert.equal(calls[1].path, "/api/v1/interests");
  assert.equal(calls[1].options, undefined);
  assert.equal(calls.some((call) => call.path.includes("cache/clear")), false);
  assert.equal(draft.dirty, false);
});

test("updating interests reloads the server-reconciled profile", async () => {
  const calls = [];
  const refreshed = {
    revision: 5,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: ["topology"],
    phrases: [],
    authors: [],
    seed_papers: ["2608.01234"],
    seed_paper_details: [
      {
        arxiv_id: "2608.01234",
        title: "A reconciled seed title",
        authors: ["Ada Example"],
      },
    ],
    suggestions: { seed_papers: [] },
  };
  const api = {
    async json(key, path, options) {
      calls.push({ key, path, options });
      return options?.method === "PUT" ? { revision: 5 } : refreshed;
    },
  };
  const draft = new InterestsDraft({
    revision: 4,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: [],
    authors: [],
    seed_papers: [],
  });
  draft.addCustom("keywords", "topology");

  const result = await new InterestsController(api).save(draft);

  assert.equal(calls.length, 2);
  assert.deepEqual(
    calls.map(({ key, path }) => ({ key, path })),
    [
      { key: "interests-save", path: "/api/v1/interests" },
      { key: "interests-load", path: "/api/v1/interests" },
    ],
  );
  assert.equal(draft.dirty, false);
  assert.deepEqual(result, refreshed);
});

test("fresh suggestions explicitly ask the server to resume the candidate corpus", async () => {
  const calls = [];
  const api = {
    async json(key, path, options) {
      calls.push({ key, path, options });
      return { suggestions: {} };
    },
  };

  await new InterestsController(api).freshSuggestions();

  assert.deepEqual(calls, [
    {
      key: "interests-load",
      path: "/api/v1/interests?refresh=1",
      options: undefined,
    },
  ]);
});

test("interests renders every preference kind, fresh suggestions, custom additions, and explicit Update", () => {
  const root = new FakeNode("main");
  const calls = [];
  const draft = new InterestsDraft({
    revision: 2,
    categories: [{ category: "math.AG", set_spec: "arXiv:math.AG" }],
    keywords: [],
    phrases: ["existing phrase"],
    authors: [],
    seed_papers: [],
  });
  renderInterestsView(
    new FakeDocument(),
    root,
    {
      draft,
      suggestions: {
        keywords: [{ suggestion_id: "keyword_123", value: "derived geometry" }],
        phrases: [],
        authors: [],
        seed_papers: [],
        categories: [],
      },
      suggestions_generated_at: "2026-08-22T12:00:00Z",
    },
    {
      refreshSuggestions: () => calls.push("fresh"),
      save: (current) => calls.push(["save", current.snapshot()]),
    },
  );

  for (const label of ["Categories", "Seed papers", "Terms", "Authors"]) {
    assert.match(root.textContent, new RegExp(label));
  }
  assert.match(root.textContent, /only after you choose Update interests/i);
  assert.match(root.textContent, /Aug 22, 2026/);
  findButton(root, "Add terms").click();
  const keyword = descendants(root, "input").find(
    (node) => node.getAttribute("value") === "derived geometry",
  );
  keyword.checked = true;
  keyword.dispatchEvent({ type: "change" });
  findButton(root, "Refresh suggestions").click();
  assert.deepEqual(calls, ["fresh"]);
  findButton(root, "Update interests").click();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1][1].keywords, ["derived geometry"]);
});
