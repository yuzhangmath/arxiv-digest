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

export function categorySetupActionLabel(count) {
  const total = Number.isInteger(count) && count > 0 ? count : 0;
  if (total === 0) return "Continue";
  return `Continue with ${total} selected ${total === 1 ? "category" : "categories"}`;
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
      "Choose your categories, review history, and PDF folder. After setup, open Interests to add seed papers, terms, and authors or generate suggestions.",
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
    actions.onSubmit?.(step);
    actions.onContinue?.();
  });
  continueButton.dataset.setupContinue = "";
  continueButton.disabled = !canContinue;
  view.append(continueButton);
  container.append(view);
  return view;
}
