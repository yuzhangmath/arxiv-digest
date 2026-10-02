import assert from "node:assert/strict";
import test from "node:test";

import {
  categorySetupActionLabel,
  isMathematicsCategory,
  renderSetupError,
  renderSetupView,
} from "../../src/arxiv_digest/web/static/setup_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

test("category action reports the total with correct grammar", () => {
  assert.equal(categorySetupActionLabel(0), "Continue");
  assert.equal(categorySetupActionLabel(1), "Continue with 1 selected category");
  assert.equal(categorySetupActionLabel(2), "Continue with 2 selected categories");
});

test("category setup prioritizes Mathematics and identifies every choice by code", () => {
  assert.equal(isMathematicsCategory({ category: "math" }), true);
  assert.equal(isMathematicsCategory({ category: "math.AG" }), true);
  assert.equal(isMathematicsCategory({ category: "math-ph" }), false);
  assert.equal(isMathematicsCategory({ category: "stat.ML" }), false);

  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "categories",
    categorySearchQuery: "",
    categorySelectionCount: 0,
    categoryOptions: [
      {
        category: "stat.ML",
        set_spec: "arXiv:stat.ML",
        label: "Machine Learning",
        checked: false,
      },
      {
        category: "math.AG",
        set_spec: "arXiv:math.AG",
        label: "Algebraic Geometry",
        checked: false,
      },
    ],
  });

  assert.match(root.textContent, /choose categories to monitor/i);
  assert.match(root.textContent, /uses these choices to find new papers/i);
  assert.match(root.textContent, /search by category name or code/i);
  assert.match(root.textContent, /Algebraic Geometry · math\.AG/);
  assert.match(root.textContent, /Machine Learning · stat\.ML/);
  const headings = descendants(root, "h2");
  assert.equal(headings.length, 1);
  assert.equal(headings[0].textContent, "Choose categories to monitor");
  const mathematicsIndex = root.textContent.indexOf("Mathematics");
  const moreIndex = root.textContent.indexOf("More categories");
  assert.ok(mathematicsIndex >= 0);
  assert.ok(moreIndex >= 0);
  assert.ok(mathematicsIndex < moreIndex);
  const more = descendants(root, "details")[0];
  assert.ok(more);
  assert.equal(Boolean(more.open), false);
  assert.equal(findButton(root, "Continue").disabled, true);
  assert.match(root.textContent, /select at least one category to continue/i);
});

test("category search opens matching More categories", () => {
  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "categories",
    categorySearchQuery: "ML",
    categorySelectionCount: 1,
    categoryOptions: [
      {
        category: "stat.ML",
        set_spec: "arXiv:stat.ML",
        label: "Machine Learning",
        checked: true,
      },
    ],
  });
  assert.equal(descendants(root, "details")[0].open, true);
  assert.equal(descendants(root, "input")[0].value, "ML");
});

test("category search explains when no category matches", () => {
  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "categories",
    categorySearchQuery: "not-a-category",
    categorySelectionCount: 0,
    categoryOptions: [],
  });
  assert.match(root.textContent, /no categories match this search/i);
  assert.match(root.textContent, /try a category name or code/i);
});

test("setup renders explicit selection guidance and launcher choice is required", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      step: "desktop_launcher",
      revision: 8,
      selections: { suggestions: [], custom: [] },
      launcherChoice: null,
    },
    {
      onLauncherChoice: (choice) => calls.push(choice),
      onContinue: () => calls.push("continue"),
    },
  );
  assert.match(root.textContent, /After setup, open Interests/i);
  const continueButton = findButton(root, "Finish setup");
  assert.equal(continueButton.disabled, true);
  findButton(root, "Not now").click();
  assert.deepEqual(calls, ["not_now"]);
});

test("an error keeps the draft controls and provides retry", () => {
  const root = new FakeNode("main");
  const draft = new FakeNode("input");
  draft.value = "unsaved custom author";
  root.append(draft);
  let retried = false;
  renderSetupError(new FakeDocument(), root, "Could not save. Try again.", () => {
    retried = true;
  });
  assert.equal(root.children[0], draft);
  assert.equal(draft.value, "unsaved custom author");
  findButton(root, "Retry").click();
  assert.equal(retried, true);
});

test("a repeated setup failure replaces the existing retry banner", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();
  let retried = "";

  renderSetupError(document, root, "First failure", () => {
    retried = "first";
  });
  renderSetupError(document, root, "Updated failure", () => {
    retried = "updated";
  });

  assert.equal(root.querySelectorAll(".error-banner").length, 1);
  assert.doesNotMatch(root.textContent, /First failure/);
  assert.match(root.textContent, /Updated failure/);
  findButton(root, "Retry").click();
  assert.equal(retried, "updated");
});

test("category browsing mutates nothing until a returned pair is checked", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "categories",
      revision: 0,
      categoryOptions: [
        {
          category: "math.AG",
          set_spec: "arXiv:math.AG",
          label: "Algebraic Geometry",
          checked: false,
        },
      ],
    },
    {
      onSearch: (...args) => calls.push(["search", ...args]),
      onSuggestion: (...args) => calls.push(["suggestion", ...args]),
      onSubmit: (...args) => calls.push(["submit", ...args]),
    },
  );
  const search = descendants(root, "input")[0];
  search.value = "geometry";
  findButton(root, "Search").click();
  assert.deepEqual(calls, [["search", "categories", "geometry"]]);

  const choice = descendants(root, "input")[1];
  choice.checked = true;
  choice.dispatchEvent({ type: "change" });
  assert.equal(calls[1][0], "suggestion");
  assert.equal(calls[1][1], "categories");
  assert.deepEqual(calls[1][2], {
    category: "math.AG",
    set_spec: "arXiv:math.AG",
  });
  assert.equal(calls[1][3], true);
});

test("coverage is bounded to the server-issued confirmed daily-list window", () => {
  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "initial_coverage",
    recommendedCoverageStart: "2026-07-23",
    coverageStart: "2026-07-23",
    coverageMin: "2026-05-28",
    coverageMax: "2026-08-25",
  });

  const input = descendants(root, "input").find(
    (candidate) => candidate.getAttribute("type") === "date",
  );
  assert.equal(input.getAttribute("min"), "2026-05-28");
  assert.equal(input.getAttribute("max"), "2026-08-25");
  assert.match(root.textContent, /confirmed historical daily lists/i);
  assert.match(root.textContent, /recoverable window/i);
  assert.doesNotMatch(root.textContent, /inferred|bulk-update|fallback date/i);
});

test("PDF setup uses the folder picker as its only destination method", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "pdf_destination",
      revision: 7,
      destinationChoice: null,
      testedDestinationToken: null,
      pickerState: "available",
    },
    {
      onPickDestination: () => calls.push(["pick"]),
      onTestDestination: (choice) => calls.push(["test", choice]),
    },
  );
  const inputs = descendants(root, "input");
  assert.equal(inputs.filter((input) => input.getAttribute("type") === "text").length, 0);
  assert.equal(inputs.filter((input) => input.getAttribute("type") === "radio").length, 0);
  assert.match(root.textContent, /where arXiv Digest will place paper PDFs/i);
  assert.match(root.textContent, /setup will not download any PDFs/i);
  assert.doesNotMatch(root.textContent, /Use Downloads|Use Documents/);
  assert.equal(
    descendants(root, "button").some(
      (candidate) => candidate.textContent === "Test selected destination",
    ),
    false,
  );
  assert.equal(findButton(root, "Continue").disabled, true);
  findButton(root, "Choose PDF folder").click();
  assert.deepEqual(calls, [["pick"]]);

  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "pdf_destination",
      revision: 7,
      destinationChoice: "picker_abcd1234",
      destinationDisplayName: "Research PDFs",
      testedDestinationToken: null,
      pickerState: "available",
    },
    {
      onPickDestination: () => calls.push(["pick"]),
      onTestDestination: (choice) => calls.push(["test", choice]),
    },
  );
  assert.match(root.textContent, /Selected folder: Research PDFs/);
  findButton(root, "Choose another folder");
  findButton(root, "Test selected destination").click();
  assert.deepEqual(calls, [["pick"], ["test", "picker_abcd1234"]]);
});

test("folder picker feedback hides standard destinations unless the native picker is unavailable", () => {
  for (const pickerState of ["cancelled", "unwritable"]) {
    const root = new FakeNode("main");
    renderSetupView(new FakeDocument(), root, {
      current_step: "pdf_destination",
      destinationChoice: null,
      pickerState,
    });
    assert.doesNotMatch(root.textContent, /Downloads|Documents/);
    assert.equal(findButton(root, "Continue").disabled, true);
  }

  const fallbackRoot = new FakeNode("main");
  const fallbackCalls = [];
  renderSetupView(
    new FakeDocument(),
    fallbackRoot,
    {
      current_step: "pdf_destination",
      destinationChoice: null,
      pickerState: "unavailable",
    },
    {
      onTestDestination: (choice) => fallbackCalls.push(choice),
    },
  );
  assert.match(fallbackRoot.textContent, /native folder picker is unavailable/i);
  findButton(fallbackRoot, "Test and use Downloads fallback").click();
  findButton(fallbackRoot, "Test and use Documents fallback").click();
  assert.deepEqual(fallbackCalls, ["downloads", "documents"]);
  assert.equal(findButton(fallbackRoot, "Continue").disabled, true);

  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "pdf_destination",
    destinationChoice: "picker_abcd1234",
    destinationDisplayName: "Existing choice",
    pickerState: "cancelled",
  });
  assert.match(root.textContent, /previously selected folder is unchanged/i);
  assert.match(root.textContent, /Selected folder: Existing choice/);
  findButton(root, "Test selected destination");
});

test("setup review shows titled seed-paper rows and a clear PDF folder label", () => {
  const root = new FakeNode("main");

  renderSetupView(new FakeDocument(), root, {
    current_step: "review",
    profileSummary: {
      categories: ["math.AT"],
      coverage_start: "2026-07-24",
      seed_papers: ["2608.49003", "2206.01234"],
      seed_paper_details: [
        { arxiv_id: "2608.49003", title: "First title" },
        {
          arxiv_id: "2206.01234",
          title: "Second <script>not markup</script> title",
        },
      ],
      keywords: ["topology"],
      phrases: ["spectral sequence"],
      authors: [],
      pdf_destination_kind: "custom",
      pdf_destination_display_path: "~/Documents/Research PDFs",
    },
    profileSummarySha256: "d".repeat(64),
  });

  const labels = descendants(root, "dt").map((label) => label.textContent);
  assert.deepEqual(labels, [
    "Categories",
    "Initial review history",
    "Seed papers",
    "Terms",
    "Authors",
    "PDF download folder",
  ]);
  assert.match(root.textContent, /Starts 2026-07-24/);
  const seedRows = descendants(root, "li");
  assert.equal(seedRows.length, 2);
  assert.match(seedRows[0].textContent, /2608\.49003.*First title/);
  assert.match(
    seedRows[1].textContent,
    /2206\.01234.*Second <script>not markup<\/script> title/,
  );
  assert.equal(descendants(root, "script").length, 0);
  assert.match(root.textContent, /topology, spectral sequence/i);
  assert.equal(descendants(root, "code")[0].textContent, "~/Documents/Research PDFs");
  assert.match(root.textContent, /Tested and ready for PDF downloads\./);
  assert.equal(descendants(root, "input").length, 0);
  assert.equal(
    descendants(root, "dd").some((detail) => detail.textContent === "custom"),
    false,
  );
});

test("required final setup actions say what they will commit", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();

  renderSetupView(document, root, {
    current_step: "review",
    profileSummary: {},
    profileSummarySha256: "d".repeat(64),
  });
  findButton(root, "Confirm profile and continue");

  renderSetupView(document, root, {
    current_step: "desktop_launcher",
    launcherChoice: "not_now",
  });
  findButton(root, "Finish setup");
});
