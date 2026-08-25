import assert from "node:assert/strict";
import test from "node:test";

import {
  SetupSelectionState,
  categorySetupActionLabel,
  isMathematicsCategory,
  optionalSetupActionLabel,
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

test("only checked suggestions and custom entries alter setup selections", () => {
  const state = new SetupSelectionState({
    suggestions: ["s1"],
    custom: ["initial"],
  });
  const before = state.snapshot;
  state.noteSearch("graph theory");
  state.noteDetailsOpened("s2");
  state.notePage(4);
  assert.deepEqual(state.snapshot, before);

  state.setSuggestion("s2", true);
  state.setCustom(0, "edited");
  state.addCustom("new value");
  assert.deepEqual(state.snapshot, {
    suggestions: ["s1", "s2"],
    custom: ["edited", "new value"],
  });
  state.setSuggestion("s1", false);
  state.removeCustom(0);
  assert.deepEqual(state.snapshot, {
    suggestions: ["s2"],
    custom: ["new value"],
  });
});

test("paper IDs, terms, and authors remain editable", () => {
  for (const kind of ["paper ID", "term", "author"]) {
    const state = new SetupSelectionState({ custom: [`old ${kind}`] });
    state.setCustom(0, `new ${kind}`);
    assert.deepEqual(state.snapshot.custom, [`new ${kind}`]);
  }
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
  assert.match(root.textContent, /only checked suggestions and custom entries/i);
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

test("a terminal candidate invocation below the minimum offers resume and restart but cannot continue", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "candidate_corpus",
      corpusJob: {
        status: "completed",
        complete: true,
        corpus_complete: false,
        minimum_met: false,
        setup_ready: false,
        can_resume: true,
        corpus_hash: "a".repeat(64),
      },
    },
    {
      onCorpus: (mode) => calls.push(mode),
      onCorpusAccept: () => calls.push("accept"),
    },
  );

  findButton(root, "Resume corpus").click();
  findButton(root, "Restart corpus").click();
  assert.deepEqual(calls, ["resume", "restart"]);
  assert.equal(findButton(root, "Continue").disabled, true);
  assert.match(root.textContent, /pass finished/i);
  assert.match(root.textContent, /more papers are needed/i);
  assert.match(root.textContent, /minimum/i);
});

test("an incomplete corpus above the minimum visibly requires reduced-breadth acceptance", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "candidate_corpus",
      corpusJob: {
        status: "completed",
        complete: true,
        corpus_complete: false,
        minimum_met: true,
        setup_ready: false,
        reduced_breadth: false,
        can_resume: true,
        corpus_hash: "b".repeat(64),
      },
    },
    {
      onCorpusAccept: (corpusHash) => calls.push(corpusHash),
    },
  );

  assert.match(root.textContent, /reduced breadth/i);
  assert.match(root.textContent, /required minimum is ready/i);
  const accept = findButton(root, "Accept reduced breadth and continue");
  assert.equal(accept.disabled, false);
  accept.click();
  assert.deepEqual(calls, ["b".repeat(64)]);
});

test("a terminal corpus without resumable work only instructs the available restart action", () => {
  for (const minimumMet of [false, true]) {
    const root = new FakeNode("main");
    renderSetupView(new FakeDocument(), root, {
      current_step: "candidate_corpus",
      corpusJob: {
        status: "completed",
        complete: true,
        corpus_complete: false,
        minimum_met: minimumMet,
        can_resume: false,
        corpus_hash: "b".repeat(64),
      },
    });

    assert.doesNotMatch(root.textContent, /resume/i);
    findButton(root, "Restart corpus");
    assert.equal(
      descendants(root, "button").some(
        (candidate) => candidate.textContent === "Resume corpus",
      ),
      false,
    );
  }
});

test("an active corpus job is visibly busy and cannot be started twice", () => {
  for (const status of ["starting", "running"]) {
    const root = new FakeNode("main");
    renderSetupView(new FakeDocument(), root, {
      current_step: "candidate_corpus",
      corpusJob: {
        status,
        complete: false,
        failed: false,
      },
    });

    const view = root.children[0];
    assert.equal(view.getAttribute("aria-busy"), "true");
    assert.match(
      root.textContent,
      status === "starting" ? /starting corpus generation/i : /generating corpus/i,
    );
    assert.match(root.textContent, /up to five minutes/i);
    assert.equal(descendants(root, "progress").length, 1);
    assert.equal(
      descendants(root, "p").some(
        (candidate) => candidate.getAttribute("role") === "status",
      ),
      false,
    );
    assert.equal(
      descendants(root, "button").some((candidate) =>
        /^(generate|resume|restart) corpus$/i.test(candidate.textContent),
      ),
      false,
    );
    assert.equal(findButton(root, "Continue").disabled, true);
  }
});

test("the corpus step explains what it gathers and how the sample is used", () => {
  const root = new FakeNode("main");
  renderSetupView(new FakeDocument(), root, {
    current_step: "candidate_corpus",
    corpusJob: { complete: false, failed: false, can_resume: false },
  });

  assert.match(root.textContent, /sample of recent papers/i);
  assert.match(root.textContent, /suggest seed papers, terms, and authors/i);
  assert.match(
    root.textContent,
    /candidate papers do not populate Review, Calendar, or Library/i,
  );
  findButton(root, "Generate corpus");
});

test("a failed corpus job is announced and offers an explicit retry", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "candidate_corpus",
      corpusJob: {
        status: "failed",
        complete: false,
        failed: true,
        message: "The background operation did not complete.",
      },
    },
    { onCorpus: (mode) => calls.push(mode) },
  );

  const alert = descendants(root, "p").find(
    (candidate) => candidate.getAttribute("role") === "alert",
  );
  assert.ok(alert);
  assert.match(alert.textContent, /did not complete/i);
  findButton(root, "Retry corpus").click();
  assert.deepEqual(calls, ["restart"]);
  assert.equal(findButton(root, "Continue").disabled, true);
});

test("a failed corpus job preserves resumable cached work", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "candidate_corpus",
      corpusJob: {
        status: "failed",
        complete: false,
        failed: true,
        can_resume: true,
      },
    },
    { onCorpus: (mode) => calls.push(mode) },
  );

  findButton(root, "Resume corpus").click();
  findButton(root, "Restart corpus").click();
  assert.deepEqual(calls, ["resume", "restart"]);
});

test("the seed-paper step explains its effect and that selection is optional", () => {
  const root = new FakeNode("main");

  renderSetupView(new FakeDocument(), root, {
    current_step: "seed_papers",
    paperOptions: [],
    customPaperIds: [],
  });

  assert.match(root.textContent, /boost textually similar papers/i);
  assert.match(root.textContent, /term and author suggestions/i);
  assert.match(root.textContent, /this step is optional/i);
  assert.match(root.textContent, /change these later in interests/i);
  assert.match(
    root.textContent,
    /does not populate Review, Calendar, or Library/i,
  );
  assert.match(root.textContent, /does not save the paper or download its pdf/i);
  assert.match(root.textContent, /search by title, author, or arxiv id/i);
});

test("the seed-paper action states whether the optional step is empty", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();

  renderSetupView(document, root, {
    current_step: "seed_papers",
    paperOptions: [],
    customPaperIds: [],
  });
  findButton(root, "Continue without seed papers");

  renderSetupView(document, root, {
    current_step: "seed_papers",
    paperOptions: [{ suggestion_id: "seed-1", title: "Selected", checked: true }],
    customPaperIds: [],
  });
  findButton(root, "Continue with selected seed papers");
});

test("optional actions count accepted selections hidden by search results", () => {
  assert.equal(
    optionalSetupActionLabel({
      current_step: "seed_papers",
      paperOptions: [],
      customPaperIds: [],
      acceptedSelectionCounts: { seed_papers: 1 },
    }),
    "Continue with selected seed papers",
  );
  assert.equal(
    optionalSetupActionLabel({
      current_step: "authors",
      authorOptions: [],
      customAuthors: [],
      acceptedSelectionCounts: { authors: 2 },
    }),
    "Continue with selected authors",
  );
});

test("terms and authors explain optionality and name empty or selected submissions", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();

  renderSetupView(document, root, {
    current_step: "keywords_and_phrases",
    phraseOptions: [],
    customTerms: [],
  });
  assert.match(root.textContent, /optional/i);
  assert.match(root.textContent, /change them later in interests/i);
  findButton(root, "Continue without terms");

  renderSetupView(document, root, {
    current_step: "authors",
    authorOptions: [{ suggestion_id: "author-1", label: "Ada", checked: true }],
    customAuthors: [],
  });
  assert.match(root.textContent, /optional/i);
  assert.match(root.textContent, /change these later in interests/i);
  findButton(root, "Continue with selected authors");
});

test("terms setup presents keyword and phrase suggestions through one unified terms control", () => {
  const root = new FakeNode("main");
  const document = new FakeDocument();
  const calls = [];

  renderSetupView(document, root, {
    current_step: "keywords_and_phrases",
    keywordOptions: [{ suggestion_id: "keyword-1", label: "topology" }],
    phraseOptions: [
      { suggestion_id: "phrase-1", label: "spectral sequence" },
    ],
    customTerms: [],
  }, {
    onSuggestion: (...values) => calls.push(values),
  });

  assert.deepEqual(
    descendants(root, "h3").map((heading) => heading.textContent),
    ["Suggested terms", "Custom term"],
  );
  assert.match(root.textContent, /spectral sequence/i);
  assert.match(root.textContent, /topology/i);
  const topology = descendants(root, "input").find(
    (input) => input.value === "keyword-1",
  );
  const spectralSequence = descendants(root, "input").find(
    (input) => input.value === "phrase-1",
  );
  topology.checked = true;
  topology.dispatchEvent({ type: "change" });
  spectralSequence.checked = true;
  spectralSequence.dispatchEvent({ type: "change" });
  assert.deepEqual(calls, [
    ["keywords", "keyword-1", true],
    ["phrases", "phrase-1", true],
  ]);
  findButton(root, "Add custom term");
  findButton(root, "Continue without terms");
});

test("setup review shows titled seed-paper rows and a clear PDF folder label", () => {
  const root = new FakeNode("main");

  renderSetupView(new FakeDocument(), root, {
    current_step: "review",
    profileSummary: {
      categories: ["math.AT"],
      coverage_start: "2026-07-24",
      seed_papers: ["2205.13427", "2206.01234"],
      seed_paper_details: [
        { arxiv_id: "2205.13427", title: "First title" },
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
  assert.match(seedRows[0].textContent, /2205\.13427.*First title/);
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
    current_step: "candidate_corpus",
    corpusJob: {
      status: "completed",
      complete: true,
      corpus_complete: true,
      corpus_hash: "c".repeat(64),
    },
  });
  findButton(root, "Use this corpus and continue");

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

test("seed-paper titles render only explicitly delimited math", () => {
  const previousKatex = globalThis.katex;
  const renderCalls = [];
  const selectionCalls = [];
  globalThis.katex = {
    render(source, output, options) {
      renderCalls.push({ source, options });
      output.textContent = `rendered:${source}`;
    },
  };

  try {
    const root = new FakeNode("main");
    renderSetupView(
      new FakeDocument(),
      root,
      {
        current_step: "seed_papers",
        paperOptions: [
          {
            suggestion_id: "paper_1",
            title: "On the $m$-dimensional $\\mathbb{Z}/p$-action \\(y\\) and $$z$$ <script>bad()</script>",
          },
        ],
        customPaperIds: [],
      },
      { onSuggestion: (...args) => selectionCalls.push(args) },
    );

    assert.deepEqual(
      renderCalls.map(({ source }) => source),
      ["m", "\\mathbb{Z}/p", "y", "z"],
    );
    assert.equal(renderCalls.every(({ options }) => options.trust === false), true);
    assert.equal(
      renderCalls.every(({ options }) => options.displayMode === false),
      true,
    );
    assert.equal(descendants(root, "script").length, 0);
    assert.match(root.textContent, /<script>bad\(\)<\/script>/);
    const suggestionLabel = root.querySelector(".suggestion-label");
    assert.equal(descendants(suggestionLabel, "div").length, 0);

    const checkbox = descendants(root, "input").find(
      (node) => node.getAttribute("type") === "checkbox",
    );
    assert.equal(checkbox.value, "paper_1");
    checkbox.checked = true;
    checkbox.dispatchEvent({ type: "change" });
    assert.deepEqual(selectionCalls, [["seed_papers", "paper_1", true]]);
  } finally {
    if (previousKatex === undefined) delete globalThis.katex;
    else globalThis.katex = previousKatex;
  }
});
