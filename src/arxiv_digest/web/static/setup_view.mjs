import { renderInlineMathText } from "./math_view.mjs";

function copiedStrings(values) {
  return Array.isArray(values)
    ? values.filter((value) => typeof value === "string").map(String)
    : [];
}

function reviewSeedPapers(summary) {
  const details = Array.isArray(summary?.seed_paper_details)
    ? summary.seed_paper_details
    : [];
  const papers = details
    .map((source) => ({
      arxivId: String(source?.arxiv_id ?? "").trim(),
      title: String(source?.title ?? "").trim(),
    }))
    .filter((paper) => paper.arxivId !== "");
  if (papers.length > 0) return papers;
  return copiedStrings(summary?.seed_papers).map((arxivId) => ({
    arxivId,
    title: "",
  }));
}

function seedPaperSummaryDetail(document, summary) {
  const detail = element(document, "dd");
  const papers = reviewSeedPapers(summary);
  if (papers.length === 0) {
    detail.textContent = "None";
    return detail;
  }
  const list = element(document, "ul", undefined, "setup-summary-seed-papers");
  for (const paper of papers) {
    const row = element(document, "li", undefined, "setup-summary-seed-paper");
    row.append(element(document, "span", paper.arxivId, "setup-summary-seed-id"));
    if (paper.title) {
      row.append(element(document, "span", "—", "setup-summary-seed-separator"));
      const title = element(document, "span", undefined, "setup-summary-seed-title");
      if (globalThis.katex?.render) {
        renderInlineMathText(title, paper.title, globalThis.katex, document);
      } else {
        title.textContent = paper.title;
      }
      row.append(title);
    }
    list.append(row);
  }
  detail.append(list);
  return detail;
}

function pdfDestinationSummaryDetail(document, summary) {
  const kind = String(summary?.pdf_destination_kind ?? "");
  const projectedPath = typeof summary?.pdf_destination_display_path === "string"
    ? summary.pdf_destination_display_path.trim()
    : "";
  const fallbackPath = kind === "downloads"
    ? "Downloads / Arxiv Digest"
    : kind === "documents"
      ? "Documents / Arxiv Digest"
      : kind === "custom"
        ? "Chosen folder"
        : "No folder selected";
  const detail = element(document, "dd", undefined, "setup-summary-destination");
  detail.append(
    element(
      document,
      "code",
      projectedPath || fallbackPath,
      "setup-summary-destination-path",
    ),
  );
  if (kind) {
    detail.append(
      element(
        document,
        "span",
        "Tested and ready for PDF downloads.",
        "setup-summary-destination-status",
      ),
    );
  }
  return detail;
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

function hasOptionalSelections(options, customValues) {
  return (
    (Array.isArray(options) && options.some((option) => option?.checked === true)) ||
    copiedStrings(customValues).some((value) => value.trim() !== "")
  );
}

export function categorySetupActionLabel(count) {
  const total = Number.isInteger(count) && count > 0 ? count : 0;
  if (total === 0) return "Continue";
  return `Continue with ${total} selected ${total === 1 ? "category" : "categories"}`;
}

export function optionalSetupActionLabel(model) {
  const step = canonicalStep(String(model?.current_step ?? model?.step ?? ""));
  const acceptedCount = Number(model?.acceptedSelectionCounts?.[step] ?? 0);
  if (step === "seed_papers") {
    return acceptedCount > 0 || hasOptionalSelections(model?.paperOptions, model?.customPaperIds)
      ? "Continue with selected seed papers"
      : "Continue without seed papers";
  }
  if (step === "terms") {
    const termOptions = [
      ...(Array.isArray(model?.keywordOptions) ? model.keywordOptions : []),
      ...(Array.isArray(model?.phraseOptions) ? model.phraseOptions : []),
    ];
    return acceptedCount > 0 || hasOptionalSelections(termOptions, model?.customTerms)
      ? "Continue with selected terms"
      : "Continue without terms";
  }
  if (step === "authors") {
    return acceptedCount > 0 || hasOptionalSelections(model?.authorOptions, model?.customAuthors)
      ? "Continue with selected authors"
      : "Continue without author preferences";
  }
  return null;
}

export function isMathematicsCategory(source) {
  const category = String(source?.category ?? "");
  return category === "math" || category.startsWith("math.");
}

function suggestionChecklist(document, kind, options, actions, target = null) {
  const list = target ?? element(document, "div", undefined, "suggestion-list");
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
    const label = element(document, "span", undefined, "suggestion-label");
    const labelText = optionLabel(source);
    if (kind === "categories") {
      const category = String(source?.category ?? "");
      label.textContent = category && category !== labelText
        ? `${labelText} · ${category}`
        : labelText;
    } else if (kind === "seed_papers" && globalThis.katex?.render) {
      renderInlineMathText(label, labelText, globalThis.katex, document);
    } else {
      label.textContent = labelText;
    }
    row.append(input, label);
    list.append(row);
  }
  return list;
}

function categoryChoices(document, model, actions) {
  const options = Array.isArray(model?.categoryOptions) ? model.categoryOptions : [];
  const mathematics = options.filter(isMathematicsCategory);
  const moreCategories = options.filter((option) => !isMathematicsCategory(option));
  const result = element(document, "div", undefined, "category-groups");

  if (mathematics.length > 0) {
    const section = element(
      document,
      "section",
      undefined,
      "category-group category-group-mathematics",
    );
    section.append(
      element(document, "h3", "Mathematics"),
      suggestionChecklist(document, "categories", mathematics, actions),
    );
    result.append(section);
  }
  if (moreCategories.length > 0) {
    const details = element(
      document,
      "details",
      undefined,
      "category-group category-group-more",
    );
    details.open = String(model?.categorySearchQuery ?? "").trim() !== "";
    details.append(
      element(document, "summary", "More categories"),
      suggestionChecklist(document, "categories", moreCategories, actions),
    );
    result.append(details);
  }
  const query = String(model?.categorySearchQuery ?? "").trim();
  if (options.length === 0 && query !== "") {
    result.append(
      element(
        document,
        "p",
        "No categories match this search. Try a category name or code.",
        "empty-state",
      ),
    );
  }
  return result;
}

function searchControl(
  document,
  kind,
  actions,
  labelText = "Search",
  initialValue = "",
) {
  const group = element(document, "div", undefined, "search-control");
  const { label, input } = inputWithLabel(document, labelText);
  input.setAttribute("autocomplete", "off");
  input.value = String(initialValue);
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
    const row = element(document, "div", undefined, "custom-entry");
    const { label, input } = inputWithLabel(document, `${labelText} ${index + 1}`);
    input.value = value;
    input.addEventListener("input", () =>
      actions.onCustomChange?.(kind, index, input.value),
    );
    const remove = button(
      document,
      "Remove",
      () => actions.onCustomRemove?.(kind, index),
    );
    remove.setAttribute(
      "aria-label",
      `Remove ${labelText.toLocaleLowerCase("en-US")} ${index + 1}`,
    );
    row.append(label, remove);
    section.append(row);
  }
  const add = button(
    document,
    `Add ${labelText.toLocaleLowerCase("en-US")}`,
    () => actions.onCustomAdd?.(kind),
  );
  add.setAttribute("data-custom-add", kind);
  section.append(add);
  return section;
}

export function renderSetupError(document, container, message, retry) {
  let notice = container.querySelector(".error-banner");
  if (!notice) {
    notice = element(document, "section", undefined, "error-banner");
    container.append(notice);
  }
  notice.setAttribute("role", "alert");
  notice.replaceChildren(
    element(document, "p", String(message)),
    button(document, "Retry", retry),
  );
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
  const heading = step === "categories"
    ? "Choose categories to monitor"
    : step.replaceAll("_", " ");
  view.append(element(document, "h2", heading));

  let canContinue = true;
  let continueLabel = "Continue";
  if (step === "categories") {
    view.append(
      element(
        document,
        "p",
        "Choose one or more arXiv categories to monitor. arXiv Digest uses these choices to find new papers. Search by category name or code. You can change them later in Interests.",
        "step-guidance",
      ),
      searchControl(
        document,
        "categories",
        actions,
        "Search by category name or code",
        model?.categorySearchQuery,
      ),
      categoryChoices(document, model, actions),
    );
    const selectedCount = Number(model?.categorySelectionCount ?? 0);
    canContinue = selectedCount > 0;
    continueLabel = categorySetupActionLabel(selectedCount);
    const requirement = element(
      document,
      "p",
      "Select at least one category to continue.",
      "required-guidance",
    );
    requirement.hidden = canContinue;
    view.append(requirement);
  } else if (step === "coverage") {
    const note = element(
      document,
      "p",
      "The chosen start controls confirmed historical daily lists in Review and Calendar. Choose a date within the recoverable window shown by this picker.",
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
    const coverageMin = String(model?.coverageMin ?? model?.coverage_min ?? "");
    const coverageMax = String(model?.coverageMax ?? model?.coverage_max ?? "");
    if (coverageMin) input.setAttribute("min", coverageMin);
    if (coverageMax) input.setAttribute("max", coverageMax);
    input.addEventListener("change", () => actions.onCoverageChange?.(input.value));
    view.append(label);
    canContinue = Boolean(
      input.value &&
      (!coverageMin || input.value >= coverageMin) &&
      (!coverageMax || input.value <= coverageMax),
    );
  } else if (step === "corpus") {
    const job = model?.corpusJob ?? {};
    const working = job.status === "starting" || job.status === "running";
    const workingMessage = job.status === "starting"
      ? "Starting corpus generation… This can take up to five minutes."
      : "Generating corpus… This can take up to five minutes.";
    const defaultMessage = working
      ? workingMessage
      : job.failed
        ? "Corpus generation did not complete."
        : job.complete && job.corpus_complete === true
          ? "Corpus ready."
          : job.complete && job.minimum_met === true
            ? job.can_resume
              ? "This corpus pass finished. The required minimum is ready; resume for broader coverage or accept reduced breadth."
              : "This corpus pass finished. The required minimum is ready; accept reduced breadth to continue or restart generation."
            : job.complete
              ? job.can_resume
                ? "This corpus pass finished, but more papers are needed."
                : "This corpus pass finished, but more papers are needed. Restart generation to try again."
          : "arXiv Digest will gather a bounded sample of recent papers from your selected categories. It uses this sample to suggest seed papers, terms, and authors in the next steps. Candidate papers do not populate Review, Calendar, or Library.";
    const progress = element(
      document,
      "p",
      String(job.message ?? defaultMessage),
    );
    if (job.failed) progress.setAttribute("role", "alert");
    view.append(progress);
    if (working) {
      view.setAttribute("aria-busy", "true");
      if (job.message) {
        view.append(
          element(
            document,
            "p",
            workingMessage,
            "working-detail",
          ),
        );
      }
      const activity = element(document, "progress", undefined, "corpus-progress");
      activity.setAttribute("aria-label", "Corpus generation in progress");
      view.append(activity);
      canContinue = false;
    } else if (!job.complete) {
      const startLabel = job.can_resume
        ? "Resume corpus"
        : job.failed
          ? "Retry corpus"
          : "Generate corpus";
      view.append(
        button(document, startLabel, () =>
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
        continueLabel = "Use this corpus and continue";
      } else {
        canContinue = false;
        if (job.can_resume) {
          view.append(button(document, "Resume corpus", () => actions.onCorpus?.("resume")));
        }
        view.append(button(document, "Restart corpus", () => actions.onCorpus?.("restart")));
        if (!job.minimum_met) {
          view.append(
            element(
              document,
              "p",
              job.can_resume
                ? "The candidate corpus is below the required minimum. Resume or restart generation before continuing."
                : "The candidate corpus is below the required minimum. Restart generation before continuing.",
              "warning",
            ),
          );
        } else {
          view.append(
            element(
              document,
              "p",
              job.can_resume
                ? "The required minimum is available. You can resume for broader suggestions or explicitly accept this reduced breadth."
                : "The required minimum is available. Explicitly accept this reduced breadth to continue, or restart generation.",
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
    view.append(
      element(
        document,
        "p",
        "Choose papers that represent what you want to read. arXiv Digest uses selected seed papers to boost textually similar papers in future reviews and tailor the term and author suggestions that follow. This step is optional. Selecting a seed paper does not populate Review, Calendar, or Library; it does not save the paper or download its PDF. You can add or change these later in Interests.",
        "step-guidance",
      ),
    );
    view.append(
      searchControl(
        document,
        "seed_papers",
        actions,
        "Search by title, author, or arXiv ID",
      ),
    );
    view.append(suggestionChecklist(document, "seed_papers", model?.paperOptions, actions));
    view.append(customEntries(document, "paper_ids", model?.customPaperIds, actions, "Custom paper ID"));
    continueLabel = optionalSetupActionLabel(model);
  } else if (step === "terms") {
    view.append(
      element(
        document,
        "p",
        "Optional. Choose or add terms that describe work you want to prioritize. You can change them later in Interests.",
        "step-guidance",
      ),
    );
    view.append(element(document, "h3", "Suggested terms"));
    const termSuggestions = suggestionChecklist(
      document,
      "keywords",
      model?.keywordOptions,
      actions,
    );
    suggestionChecklist(
      document,
      "phrases",
      model?.phraseOptions,
      actions,
      termSuggestions,
    );
    view.append(termSuggestions);
    view.append(customEntries(document, "terms", model?.customTerms, actions, "Custom term"));
    continueLabel = optionalSetupActionLabel(model);
  } else if (step === "authors") {
    view.append(
      element(
        document,
        "p",
        "Optional. Choose authors whose work you want to prioritize. You can add or change these later in Interests.",
        "step-guidance",
      ),
    );
    view.append(searchControl(document, "authors", actions, "Search authors"));
    view.append(suggestionChecklist(document, "authors", model?.authorOptions, actions));
    view.append(customEntries(document, "authors", model?.customAuthors, actions, "Custom author"));
    continueLabel = optionalSetupActionLabel(model);
  } else if (step === "pdf_destination") {
    const selected = String(model?.destinationChoice ?? "");
    const hasSelectedFolder = /^picker_[A-Za-z0-9_-]{8,120}$/.test(selected);
    const selectedFolderName = String(model?.destinationDisplayName ?? "").trim();
    view.append(
      element(
        document,
        "p",
        "Choose the folder where arXiv Digest will place paper PDFs if you download them. Setup will not download any PDFs. After choosing a folder, test it to confirm the app can create and remove files there.",
        "step-guidance",
      ),
    );
    const choices = element(document, "div", undefined, "destination-choices");
    choices.append(
      button(
        document,
        hasSelectedFolder ? "Choose another folder" : "Choose PDF folder",
        () => actions.onPickDestination?.(),
      ),
    );
    view.append(choices);
    if (hasSelectedFolder) {
      view.append(
        element(
          document,
          "p",
          selectedFolderName
            ? `Selected folder: ${selectedFolderName}`
            : "A folder has been selected.",
          "selected-destination",
        ),
      );
    }
    const pickerMessages = {
      cancelled: hasSelectedFolder
        ? "Folder selection was cancelled; the previously selected folder is unchanged."
        : "Folder selection was cancelled; no folder was selected.",
      unwritable: "The folder could not be used. Choose a folder and try again.",
      unavailable: "The native folder picker is unavailable on this system. You can use an app-managed fallback folder instead.",
      tested: "The selected folder is writable. The temporary test file was removed.",
    };
    if (pickerMessages[model?.pickerState]) {
      view.append(element(document, "p", pickerMessages[model.pickerState], "picker-status"));
    }
    if (model?.pickerState === "unavailable") {
      const fallbacks = element(document, "div", undefined, "destination-fallbacks");
      fallbacks.append(
        button(document, "Test and use Downloads fallback", () =>
          actions.onTestDestination?.("downloads")),
        button(document, "Test and use Documents fallback", () =>
          actions.onTestDestination?.("documents")),
      );
      view.append(fallbacks);
    }
    if (hasSelectedFolder && !model?.testedDestinationToken) {
      view.append(
        button(document, "Test selected destination", () =>
          actions.onTestDestination?.(selected),
        ),
      );
    }
    canContinue = typeof model?.testedDestinationToken === "string";
  } else if (step === "review") {
    const summary = model?.profileSummary ?? model?.review_summary ?? {};
    const terms = [
      ...copiedStrings(summary.keywords),
      ...copiedStrings(summary.phrases),
    ];
    const categories = copiedStrings(summary.categories);
    const authors = copiedStrings(summary.authors);
    const coverageStart = typeof summary.coverage_start === "string"
      ? summary.coverage_start
      : "";
    const list = element(document, "dl", undefined, "setup-summary");
    list.append(
      element(document, "dt", "Categories"),
      element(document, "dd", categories.length > 0 ? categories.join(", ") : "None"),
      element(document, "dt", "Initial review history"),
      element(document, "dd", coverageStart ? `Starts ${coverageStart}` : "Not set"),
      element(document, "dt", "Seed papers"),
      seedPaperSummaryDetail(document, summary),
      element(document, "dt", "Terms"),
      element(document, "dd", terms.length > 0 ? terms.join(", ") : "None"),
      element(document, "dt", "Authors"),
      element(document, "dd", authors.length > 0 ? authors.join(", ") : "None"),
      element(document, "dt", "PDF download folder"),
      pdfDestinationSummaryDetail(document, summary),
    );
    view.append(list);
    canContinue = typeof model?.profileSummarySha256 === "string";
    continueLabel = "Confirm profile and continue";
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
    continueLabel = "Finish setup";
  }

  const continueButton = button(document, continueLabel, () => {
    if (step === "corpus") actions.onCorpusAccept?.(model?.corpusJob?.corpus_hash);
    else actions.onSubmit?.(step);
    actions.onContinue?.();
  });
  continueButton.dataset.setupContinue = "";
  continueButton.disabled = !canContinue;
  view.append(continueButton);
  container.append(view);
  return view;
}
