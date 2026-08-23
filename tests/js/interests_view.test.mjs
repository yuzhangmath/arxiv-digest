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
  assert.equal(calls.length, 1);
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
  assert.equal(calls.some((call) => call.path.includes("cache/clear")), false);
  assert.equal(draft.dirty, false);
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

test("interests renders every preference kind, fresh suggestions, custom additions, and explicit Save", () => {
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

  for (const label of ["Categories", "Seed papers", "Keywords", "Phrases", "Authors"]) {
    assert.match(root.textContent, new RegExp(label));
  }
  assert.match(root.textContent, /only checked or typed values/i);
  assert.match(root.textContent, /2026-08-22/);
  const keyword = descendants(root, "input").find(
    (node) => node.getAttribute("value") === "derived geometry",
  );
  keyword.checked = true;
  keyword.dispatchEvent({ type: "change" });
  findButton(root, "Get fresh suggestions").click();
  assert.deepEqual(calls, ["fresh"]);
  findButton(root, "Save interests").click();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1][1].keywords, ["derived geometry"]);
});
