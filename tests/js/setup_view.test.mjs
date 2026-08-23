import assert from "node:assert/strict";
import test from "node:test";

import {
  SetupSelectionState,
  renderSetupError,
  renderSetupView,
} from "../../src/arxiv_digest/web/static/setup_view.mjs";
import {
  FakeDocument,
  FakeNode,
  descendants,
  findButton,
} from "./dom_test_helper.mjs";

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

test("paper IDs, keywords, phrases, and authors remain editable", () => {
  for (const kind of ["paper ID", "keyword", "phrase", "author"]) {
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
  const continueButton = findButton(root, "Continue");
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

test("PDF setup defaults to Downloads and never renders a path text field", () => {
  const root = new FakeNode("main");
  const calls = [];
  renderSetupView(
    new FakeDocument(),
    root,
    {
      current_step: "pdf_destination",
      revision: 7,
      destinationChoice: "downloads",
      testedDestinationToken: null,
      pickerState: "available",
    },
    {
      onDestinationChoice: (choice) => calls.push(["choice", choice]),
      onPickDestination: () => calls.push(["pick"]),
      onTestDestination: (choice) => calls.push(["test", choice]),
    },
  );
  const inputs = descendants(root, "input");
  assert.equal(inputs.filter((input) => input.getAttribute("type") === "text").length, 0);
  assert.equal(
    inputs.find((input) => input.value === "downloads").checked,
    true,
  );
  findButton(root, "Use Documents").click();
  findButton(root, "Choose another folder").click();
  assert.deepEqual(calls, [["choice", "documents"], ["pick"]]);
});

test("cancelled or unwritable picker feedback keeps the previous destination", () => {
  for (const pickerState of ["cancelled", "unwritable", "unavailable"]) {
    const root = new FakeNode("main");
    renderSetupView(new FakeDocument(), root, {
      current_step: "pdf_destination",
      destinationChoice: "downloads",
      pickerState,
    });
    assert.match(root.textContent, new RegExp(pickerState, "i"));
    const selected = descendants(root, "input").find((input) => input.checked);
    assert.equal(selected.value, "downloads");
  }
});

test("a terminal candidate invocation below the minimum offers Resume and Retry but cannot continue", () => {
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

  findButton(root, "Resume").click();
  findButton(root, "Retry").click();
  assert.deepEqual(calls, ["resume", "restart"]);
  assert.equal(findButton(root, "Continue").disabled, true);
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
  const accept = findButton(root, "Accept reduced breadth and continue");
  assert.equal(accept.disabled, false);
  accept.click();
  assert.deepEqual(calls, ["b".repeat(64)]);
});
