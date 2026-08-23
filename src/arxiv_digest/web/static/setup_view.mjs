function copiedStrings(values) {
  return Array.isArray(values)
    ? values.filter((value) => typeof value === "string").map(String)
    : [];
}

function frozenSnapshot(suggestions, custom) {
  return Object.freeze({
    suggestions: Object.freeze([...suggestions]),
    custom: Object.freeze([...custom]),
  });
}

export class SetupSelectionState {
  constructor({ suggestions = [], custom = [] } = {}) {
    this.suggestions = new Set(copiedStrings(suggestions));
    this.custom = copiedStrings(custom);
  }

  get snapshot() {
    return frozenSnapshot(this.suggestions, this.custom);
  }

  setSuggestion(suggestionId, checked) {
    if (checked) this.suggestions.add(String(suggestionId));
    else this.suggestions.delete(String(suggestionId));
  }

  setCustom(index, value) {
    if (!Number.isInteger(index) || index < 0 || index >= this.custom.length) {
      throw new RangeError("Unknown custom entry");
    }
    this.custom[index] = String(value);
  }

  addCustom(value = "") {
    this.custom.push(String(value));
  }

  removeCustom(index) {
    if (!Number.isInteger(index) || index < 0 || index >= this.custom.length) {
      throw new RangeError("Unknown custom entry");
    }
    this.custom.splice(index, 1);
  }

  noteSearch(_query) {}
  noteDetailsOpened(_suggestionId) {}
  notePage(_page) {}
}

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(document, label, handler) {
  const node = element(document, "button", label);
  node.setAttribute("type", "button");
  node.addEventListener("click", handler);
  return node;
}

function inputWithLabel(document, labelText, type = "text") {
  const label = element(document, "label", undefined, "field");
  label.append(element(document, "span", labelText));
  const input = element(document, "input");
  input.setAttribute("type", type);
  label.append(input);
  return { label, input };
}

function canonicalStep(value) {
  return {
    initial_coverage: "coverage",
    candidate_corpus: "corpus",
    keywords_and_phrases: "terms",
    desktop_launcher: "launcher",
  }[value] ?? value;
}

function optionIdentity(kind, source) {
  if (kind === "categories") {
    return `${String(source?.category ?? "")}\u0000${String(source?.set_spec ?? "")}`;
  }
  return String(source?.suggestion_id ?? source?.id ?? source?.value ?? source ?? "");
}

function optionLabel(source) {
  if (typeof source === "string") return source;
  return String(
    source?.label ??
      source?.display_name ??
      source?.title ??
      source?.name ??
      source?.value ??
      source?.category ??
      "",
  );
}

function suggestionChecklist(document, kind, options, actions) {
  const list = element(document, "div", undefined, "suggestion-list");
  for (const source of Array.isArray(options) ? options : []) {
    const stable =
      kind === "categories"
        ? Object.freeze({
            category: String(source?.category ?? ""),
            set_spec: String(source?.set_spec ?? ""),
          })
        : optionIdentity(kind, source);
    const row = element(document, "label", undefined, "suggestion");
    const input = element(document, "input");
    input.setAttribute("type", "checkbox");
    input.value = optionIdentity(kind, source);
    input.checked = source?.checked === true;
    input.addEventListener("change", () =>
      actions.onSuggestion?.(kind, stable, input.checked),
    );
    row.append(input, document.createTextNode(` ${optionLabel(source)}`));
    list.append(row);
  }
  return list;
}

function searchControl(document, kind, actions, labelText = "Search") {
  const group = element(document, "div", undefined, "search-control");
  const { label, input } = inputWithLabel(document, labelText);
  input.setAttribute("autocomplete", "off");
  group.append(
    label,
    button(document, "Search", () => actions.onSearch?.(kind, input.value)),
  );
  return group;
}

function customEntries(document, kind, values, actions, labelText) {
  const section = element(document, "section", undefined, "custom-entries");
  section.append(element(document, "h3", labelText));
  for (const [index, value] of copiedStrings(values).entries()) {
    const { label, input } = inputWithLabel(document, `${labelText} ${index + 1}`);
    input.value = value;
    input.addEventListener("input", () =>
      actions.onCustomChange?.(kind, index, input.value),
    );
    label.append(
      button(document, "Remove", () => actions.onCustomRemove?.(kind, index)),
    );
    section.append(label);
  }
  section.append(button(document, `Add ${labelText.toLowerCase()}`, () => actions.onCustomAdd?.(kind)));
  return section;
}

function destinationRadio(document, labelText, value, selected, actions) {
  const label = element(document, "label", undefined, "destination-choice");
  const input = element(document, "input");
  input.setAttribute("type", "radio");
  input.setAttribute("name", "pdf-destination");
  input.value = value;
  input.checked = selected === value;
  input.addEventListener("change", () => {
    if (input.checked) actions.onDestinationChoice?.(value);
  });
  const choose = button(document, labelText, () => actions.onDestinationChoice?.(value));
  label.append(input, choose);
  return label;
}

export function renderSetupError(document, container, message, retry) {
  const notice = element(document, "section", undefined, "error-banner");
  notice.setAttribute("role", "alert");
  notice.append(element(document, "p", String(message)));
  notice.append(button(document, "Retry", retry));
  container.append(notice);
  return notice;
}

export function renderSetupView(document, container, model, actions = {}) {
  container.replaceChildren();
  const view = element(document, "section", undefined, "setup-view");
  view.append(element(document, "h1", "Set up your arXiv digest"));
  view.append(
    element(
      document,
      "p",
      "Only checked suggestions and custom entries become interests. Searching, opening details, navigating, and changing pages never change your profile.",
      "selection-guidance",
    ),
  );
  const sourceStep = String(model?.current_step ?? model?.step ?? "categories");
  const step = canonicalStep(sourceStep);
  view.dataset.step = step;
  view.append(element(document, "h2", step.replaceAll("_", " ")));

  let canContinue = true;
  let continueLabel = "Continue";
  if (step === "categories") {
    view.append(searchControl(document, "categories", actions, "Search categories"));
    view.append(
      suggestionChecklist(document, "categories", model?.categoryOptions, actions),
    );
    const selected = Array.isArray(model?.categorySelections)
      ? model.categorySelections
      : Array.isArray(model?.categories)
        ? model.categories
        : [];
    canContinue = selected.length > 0;
  } else if (step === "coverage") {
    const note = element(
      document,
      "p",
      "The recommended initial history is 30 days. Older history may take longer and can be resumed.",
    );
    view.append(note);
    const recommended = String(model?.recommendedCoverageStart ?? "");
    view.append(
      button(document, "Use recommended 30 days", () =>
        actions.onCoverageChange?.(recommended),
      ),
    );
    const { label, input } = inputWithLabel(document, "Coverage start", "date");
    input.value = String(model?.coverageStart ?? model?.coverage_start ?? recommended);
    input.addEventListener("change", () => actions.onCoverageChange?.(input.value));
    view.append(label);
    if (model?.coverageWarning || (recommended && input.value && input.value < recommended)) {
      const warning = element(
        document,
        "p",
        String(
          model?.coverageWarning ??
            "Older results use inferred metadata/version dates and may include bulk-update dates.",
        ),
        "warning",
      );
      warning.setAttribute("role", "note");
      view.append(warning);
    }
    canContinue = Boolean(input.value);
  } else if (step === "corpus") {
    const job = model?.corpusJob ?? {};
    const progress = element(
      document,
      "p",
      String(job.message ?? "Build a bounded local candidate corpus from the selected categories."),
    );
    progress.setAttribute("role", "status");
    progress.setAttribute("aria-live", "polite");
    view.append(progress);
    if (!job.complete) {
      view.append(
        button(document, job.can_resume ? "Resume corpus" : "Generate corpus", () =>
          actions.onCorpus?.(job.can_resume ? "resume" : "restart"),
        ),
      );
      if (job.can_resume) {
        view.append(button(document, "Restart corpus", () => actions.onCorpus?.("restart")));
      }
      canContinue = false;
    } else {
      if (job.corpus_complete === true) {
        canContinue = typeof job.corpus_hash === "string";
      } else {
        canContinue = false;
        if (job.can_resume) {
          view.append(button(document, "Resume", () => actions.onCorpus?.("resume")));
        }
        view.append(button(document, "Retry", () => actions.onCorpus?.("restart")));
        if (!job.minimum_met) {
          view.append(
            element(
              document,
              "p",
              "The candidate corpus is below the required minimum. Resume or retry generation before continuing.",
              "warning",
            ),
          );
        } else {
          view.append(
            element(
              document,
              "p",
              "The required minimum is available. You can resume for broader suggestions or explicitly accept this reduced breadth.",
              "warning",
            ),
          );
          continueLabel = "Accept reduced breadth and continue";
          canContinue = typeof job.corpus_hash === "string";
        }
      }
      if (job.reduced_breadth) {
        view.append(element(document, "p", "The bounded corpus used reduced breadth.", "warning"));
      }
    }
  } else if (step === "seed_papers") {
    view.append(searchControl(document, "seed_papers", actions, "Search by title or author"));
    view.append(suggestionChecklist(document, "seed_papers", model?.paperOptions, actions));
    view.append(customEntries(document, "paper_ids", model?.customPaperIds, actions, "Custom paper ID"));
  } else if (step === "terms") {
    view.append(element(document, "h3", "Suggested keywords"));
    view.append(suggestionChecklist(document, "keywords", model?.keywordOptions, actions));
    view.append(customEntries(document, "keywords", model?.customKeywords, actions, "Custom keyword"));
    view.append(element(document, "h3", "Suggested phrases"));
    view.append(suggestionChecklist(document, "phrases", model?.phraseOptions, actions));
    view.append(customEntries(document, "phrases", model?.customPhrases, actions, "Custom phrase"));
  } else if (step === "authors") {
    view.append(searchControl(document, "authors", actions, "Search authors"));
    view.append(suggestionChecklist(document, "authors", model?.authorOptions, actions));
    view.append(customEntries(document, "authors", model?.customAuthors, actions, "Custom author"));
  } else if (step === "pdf_destination") {
    const selected = String(model?.destinationChoice ?? "downloads");
    const choices = element(document, "div", undefined, "destination-choices");
    choices.append(
      destinationRadio(document, "Use Downloads", "downloads", selected, actions),
      destinationRadio(document, "Use Documents", "documents", selected, actions),
      button(document, "Choose another folder", () => actions.onPickDestination?.()),
    );
    view.append(choices);
    const pickerMessages = {
      cancelled: "Folder selection was cancelled; the previous destination is unchanged.",
      unwritable: "The selected folder is unwritable; the previous destination is unchanged.",
      unavailable: "The native folder picker is unavailable. Use Downloads or Documents.",
      tested: "The selected destination is writable.",
    };
    if (pickerMessages[model?.pickerState]) {
      view.append(element(document, "p", pickerMessages[model.pickerState], "picker-status"));
    }
    if (!model?.testedDestinationToken) {
      view.append(
        button(document, "Test selected destination", () =>
          actions.onTestDestination?.(selected),
        ),
      );
    }
    canContinue = typeof model?.testedDestinationToken === "string";
  } else if (step === "review") {
    const summary = model?.profileSummary ?? model?.review_summary ?? {};
    const list = element(document, "dl", undefined, "setup-summary");
    for (const [label, value] of [
      ["Categories", summary.categories],
      ["Seed papers", summary.seed_papers],
      ["Keywords", summary.keywords],
      ["Phrases", summary.phrases],
      ["Authors", summary.authors],
      ["PDF destination", summary.pdf_destination_kind],
    ]) {
      list.append(
        element(document, "dt", label),
        element(document, "dd", Array.isArray(value) ? value.join(", ") : String(value ?? "None")),
      );
    }
    view.append(list);
    canContinue = typeof model?.profileSummarySha256 === "string";
  } else if (step === "launcher") {
    view.append(
      element(
        document,
        "p",
        "Choose whether to create a desktop launcher. A choice is required; neither option is selected initially.",
      ),
    );
    const choices = element(document, "div", undefined, "launcher-choices");
    choices.append(
      button(document, "Create desktop launcher", () => actions.onLauncherChoice?.("create")),
      button(document, "Not now", () => actions.onLauncherChoice?.("not_now")),
    );
    view.append(choices);
    canContinue = model?.launcherChoice === "create" || model?.launcherChoice === "not_now";
  }

  const continueButton = button(document, continueLabel, () => {
    if (step === "corpus") actions.onCorpusAccept?.(model?.corpusJob?.corpus_hash);
    else actions.onSubmit?.(step);
    actions.onContinue?.();
  });
  continueButton.disabled = !canContinue;
  view.append(continueButton);
  container.append(view);
  return view;
}
