const PREFERENCE_FIELDS = new Set([
  "keywords",
  "phrases",
  "authors",
  "seed_papers",
]);

function normalizedText(value) {
  if (typeof value !== "string") throw new TypeError("Interest values must be text");
  const text = value.trim().replace(/\s+/g, " ");
  if (!text || text.length > 200) throw new TypeError("Invalid interest value");
  return text;
}

function uniqueText(values) {
  const result = [];
  const seen = new Set();
  for (const value of Array.isArray(values) ? values : []) {
    const text = normalizedText(value);
    const key = text.toLocaleLowerCase("en-US");
    if (!seen.has(key)) {
      seen.add(key);
      result.push(text);
    }
  }
  return result;
}

function customTermWordCount(value) {
  const normalized = value
    .normalize("NFKC")
    .replace(/[^\p{Letter}\p{Number}\p{Mark}]+/gu, " ")
    .trim();
  return normalized === "" ? 0 : normalized.split(/\s+/u).length;
}

function categorySelection(value) {
  if (
    !value ||
    typeof value.category !== "string" ||
    typeof value.set_spec !== "string" ||
    !value.category.trim() ||
    !value.set_spec.trim()
  ) {
    throw new TypeError("Categories require a server-provided set specification");
  }
  return Object.freeze({
    category: value.category.trim(),
    set_spec: value.set_spec.trim(),
  });
}

export class InterestsDraft {
  constructor(source) {
    if (!Number.isSafeInteger(source?.revision) || source.revision < 1) {
      throw new TypeError("Interests require a positive profile revision");
    }
    this.revision = source.revision;
    this.categories = (source.categories ?? []).map(categorySelection);
    this.categoryConfigs = [];
    this.values = Object.create(null);
    for (const field of PREFERENCE_FIELDS) {
      this.values[field] = uniqueText(source[field]);
    }
    this.dirty = false;
  }

  noteSearch(_query) {}
  noteSuggestionViewed(_suggestionId) {}
  notePage(_page) {}

  setSuggested(field, value, selected) {
    this.#assertField(field);
    const text = normalizedText(value);
    const key = text.toLocaleLowerCase("en-US");
    const index = this.values[field].findIndex(
      (item) => item.toLocaleLowerCase("en-US") === key,
    );
    if (selected && index < 0) {
      this.values[field].push(text);
      this.dirty = true;
    } else if (!selected && index >= 0) {
      this.values[field].splice(index, 1);
      this.dirty = true;
    }
  }

  addCustom(field, value) {
    this.#assertField(field);
    const before = this.values[field].length;
    this.values[field] = uniqueText([...this.values[field], value]);
    if (this.values[field].length !== before) this.dirty = true;
  }

  addTerm(value) {
    const text = normalizedText(value);
    const wordCount = customTermWordCount(text);
    if (wordCount < 1) throw new TypeError("Invalid term value");
    this.addCustom(wordCount === 1 ? "keywords" : "phrases", text);
  }

  addCategory(serverSelection, coverageStart) {
    if (typeof serverSelection === "string") {
      throw new TypeError("A server-provided category and set specification are required");
    }
    const selection = categorySelection(serverSelection);
    assertIsoDate(coverageStart);
    if (this.categories.some((item) => item.category === selection.category)) {
      throw new TypeError("Category is already selected");
    }
    this.categories.push(selection);
    this.categoryConfigs.push(
      Object.freeze({
        ...selection,
        coverage_start: coverageStart,
      }),
    );
    this.dirty = true;
  }

  removeCategory(category) {
    if (typeof category !== "string") throw new TypeError("Invalid category");
    const index = this.categories.findIndex((item) => item.category === category);
    if (index < 0) throw new TypeError("Category is not selected");
    if (this.categories.length === 1) {
      throw new TypeError("At least one category must remain selected");
    }
    this.categories.splice(index, 1);
    this.categoryConfigs = this.categoryConfigs.filter(
      (item) => item.category !== category,
    );
    this.dirty = true;
  }

  snapshot() {
    return {
      revision: this.revision,
      categories: this.categories.map((value) => ({ ...value })),
      category_configs: this.categoryConfigs.map((value) => ({ ...value })),
      keywords: [...this.values.keywords],
      phrases: [...this.values.phrases],
      authors: [...this.values.authors],
      seed_papers: [...this.values.seed_papers],
    };
  }

  #assertField(field) {
    if (!PREFERENCE_FIELDS.has(field)) throw new TypeError("Unknown interest field");
  }
}

function assertIsoDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    throw new TypeError("Coverage start must be an ISO date");
  }
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value) {
    throw new TypeError("Coverage start must be an ISO date");
  }
}

function putJson(value) {
  return {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

export class InterestsController {
  constructor(api) {
    if (!api || typeof api.json !== "function") {
      throw new TypeError("InterestsController requires an API client");
    }
    this.api = api;
  }

  load() {
    return this.api.json("interests-load", "/api/v1/interests", undefined);
  }

  freshSuggestions() {
    return this.api.json(
      "interests-load",
      "/api/v1/interests?refresh=1",
      undefined,
    );
  }

  async save(draft) {
    if (!(draft instanceof InterestsDraft)) throw new TypeError("Invalid interests draft");
    const snapshot = draft.snapshot();
    const payload = {
      expected_revision: snapshot.revision,
      categories: snapshot.categories,
      category_configs: snapshot.category_configs,
      keywords: snapshot.keywords,
      phrases: snapshot.phrases,
      authors: snapshot.authors,
      seed_papers: snapshot.seed_papers,
    };
    const result = await this.api.json(
      "interests-save",
      "/api/v1/interests",
      putJson(payload),
    );
    if (Number.isSafeInteger(result?.revision) && result.revision > 0) {
      draft.revision = result.revision;
    }
    draft.categoryConfigs = [];
    draft.dirty = false;
    return this.load();
  }
}

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(document, label, action) {
  const node = element(document, "button", label);
  node.setAttribute("type", "button");
  node.addEventListener("click", action);
  return node;
}

function formattedUtcDate(value) {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.valueOf())) return null;
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(parsed);
}

const SECTION_LABELS = Object.freeze({
  seed_papers: "Seed papers",
  keywords: "Keywords",
  phrases: "Phrases",
  authors: "Authors",
});

function suggestionValue(field, suggestion) {
  if (typeof suggestion?.value === "string") return suggestion.value;
  if (field === "seed_papers" && typeof suggestion?.arxiv_id === "string") {
    return suggestion.arxiv_id;
  }
  if (field === "authors" && typeof suggestion?.name === "string") {
    return suggestion.name;
  }
  if (typeof suggestion?.term === "string") return suggestion.term;
  throw new TypeError("Suggestion is missing its stored value");
}

function currentValuesList(
  document,
  field,
  draft,
  enableSave,
  seedPaperDetails = [],
) {
  const snapshot = draft.snapshot();
  const seedDetails = new Map(
    (Array.isArray(seedPaperDetails) ? seedPaperDetails : [])
      .filter((item) => typeof item?.arxiv_id === "string")
      .map((item) => [item.arxiv_id, item]),
  );
  const currentList = element(document, "ul", undefined, "interest-current-values");
  for (const value of snapshot[field]) {
    const item = element(document, "li");
    const detail = field === "seed_papers" ? seedDetails.get(value) : null;
    const title = typeof detail?.title === "string" ? detail.title.trim() : "";
    item.append(
      document.createTextNode(title ? `${title} (${value}) ` : `${value} `),
      actionButton(document, `Remove ${value}`, () => {
        draft.setSuggested(field, value, false);
        item.hidden = true;
        enableSave();
      }),
    );
    currentList.append(item);
  }
  return currentList;
}

function preferenceAdditions(
  document,
  field,
  draft,
  suggestions,
  enableSave,
  includeCustom = true,
) {
  const additions = element(document, "div", undefined, "interest-preference-additions");
  const snapshot = draft.snapshot();
  const selected = new Set(
    snapshot[field].map((value) => value.toLocaleLowerCase("en-US")),
  );
  for (const [index, suggestion] of suggestions.entries()) {
    const value = suggestionValue(field, suggestion);
    const row = element(document, "label", undefined, "interest-suggestion");
    const checkbox = element(document, "input");
    checkbox.setAttribute("type", "checkbox");
    checkbox.setAttribute("value", value);
    checkbox.setAttribute("id", `${field}-suggestion-${index}`);
    checkbox.checked = selected.has(value.toLocaleLowerCase("en-US"));
    checkbox.addEventListener("change", () => {
      draft.setSuggested(field, value, checkbox.checked);
      enableSave();
    });
    const title = field === "seed_papers" && typeof suggestion?.title === "string"
      ? suggestion.title.trim()
      : "";
    row.append(
      checkbox,
      document.createTextNode(` ${title ? `${title} (${value})` : value}`),
    );
    additions.append(row);
  }

  if (!includeCustom) return additions;
  const customLabel = element(document, "label", `Add custom ${SECTION_LABELS[field].toLocaleLowerCase("en-US")}`);
  const customInput = element(document, "input");
  const inputId = `${field}-custom`;
  customLabel.setAttribute("for", inputId);
  customInput.setAttribute("id", inputId);
  customInput.setAttribute("type", "text");
  const add = actionButton(document, `Add custom ${field.replace("_", " ").replace(/s$/, "")}`, () => {
    draft.addCustom(field, customInput.value);
    customInput.value = "";
    enableSave();
  });
  additions.append(customLabel, customInput, add);
  return additions;
}

function appendAdditionPanel(document, section, label, key, panel) {
  panel.classList.add("interest-addition-panel");
  panel.dataset.interestAdd = key;
  panel.hidden = true;
  let control;
  control = actionButton(document, label, () => {
    panel.hidden = !panel.hidden;
    control.setAttribute("aria-expanded", String(!panel.hidden));
  });
  control.setAttribute("aria-expanded", "false");
  section.append(control, panel);
}

function renderPreferenceSection(
  document,
  field,
  draft,
  suggestions,
  enableSave,
  addLabel,
  seedPaperDetails = [],
) {
  const section = element(document, "section", undefined, "interest-section");
  section.append(
    element(document, "h2", SECTION_LABELS[field]),
    currentValuesList(document, field, draft, enableSave, seedPaperDetails),
  );
  appendAdditionPanel(
    document,
    section,
    addLabel,
    field,
    preferenceAdditions(document, field, draft, suggestions, enableSave),
  );
  return section;
}

function renderTermsSection(document, draft, suggestions, enableSave) {
  const section = element(document, "section", undefined, "interest-section");
  section.append(
    element(document, "h2", "Terms"),
    currentValuesList(document, "keywords", draft, enableSave),
    currentValuesList(document, "phrases", draft, enableSave),
  );
  const additions = element(document, "div");
  additions.append(
    preferenceAdditions(
      document,
      "keywords",
      draft,
      Array.isArray(suggestions.keywords) ? suggestions.keywords : [],
      enableSave,
      false,
    ),
    preferenceAdditions(
      document,
      "phrases",
      draft,
      Array.isArray(suggestions.phrases) ? suggestions.phrases : [],
      enableSave,
      false,
    ),
  );
  const customLabel = element(document, "label", "Add custom term");
  const customInput = element(document, "input");
  customLabel.setAttribute("for", "terms-custom");
  customInput.setAttribute("id", "terms-custom");
  customInput.setAttribute("type", "text");
  additions.append(
    customLabel,
    customInput,
    actionButton(document, "Add custom term", () => {
      draft.addTerm(customInput.value);
      customInput.value = "";
      enableSave();
    }),
  );
  appendAdditionPanel(document, section, "Add terms", "terms", additions);
  return section;
}

export function renderInterestsView(document, container, model, actions = {}) {
  const draft = model?.draft;
  if (!(draft instanceof InterestsDraft)) throw new TypeError("Interests view requires a draft");
  const suggestions = model?.suggestions ?? {};
  const coverageMin = String(model?.coverage_min ?? model?.coverageMin ?? "");
  const coverageMax = String(model?.coverage_max ?? model?.coverageMax ?? "");
  container.replaceChildren();
  container.append(
    element(document, "h1", "Interests"),
    element(
      document,
      "p",
      "Selections, additions, and removals change your profile only after you choose Update interests. Browsing suggestions does not change it. Candidate papers do not populate Review, Calendar, or Library, and selecting a seed paper does not save it.",
    ),
  );
  const suggestionDate = formattedUtcDate(model?.suggestions_generated_at);
  if (suggestionDate) {
    container.append(
      element(document, "p", `Suggestion pool created ${suggestionDate}`),
    );
  }

  let saveButton;
  const enableSave = () => {
    if (saveButton) saveButton.disabled = !draft.dirty;
  };

  const categorySection = element(document, "section", undefined, "interest-section");
  categorySection.append(element(document, "h2", "Categories"));
  const categoryList = element(document, "ul");
  const categoryItems = new Map();
  const categorySuggestionRows = new Map();
  const refreshCategoryControls = () => {
    const selected = new Set(
      draft.snapshot().categories.map((selection) => selection.category),
    );
    for (const [category, { item, remove }] of categoryItems) {
      const active = selected.has(category);
      item.hidden = !active;
      remove.disabled = !active || selected.size <= 1;
      remove.setAttribute(
        "title",
        active && selected.size <= 1 ? "At least one category must remain selected" : "",
      );
    }
    for (const [category, row] of categorySuggestionRows) {
      row.hidden = selected.has(category);
    }
  };
  const appendCurrentCategory = (selection) => {
    const existing = categoryItems.get(selection.category);
    if (existing) {
      existing.item.hidden = false;
      return;
    }
    const item = element(document, "li");
    let remove;
    remove = actionButton(document, `Remove ${selection.category}`, () => {
      if (remove.disabled) return;
      draft.removeCategory(selection.category);
      refreshCategoryControls();
      enableSave();
    });
    item.append(
      document.createTextNode(`${selection.category} (${selection.set_spec}) `),
      remove,
    );
    categoryList.append(item);
    categoryItems.set(selection.category, { item, remove });
  };
  for (const selection of draft.snapshot().categories) {
    appendCurrentCategory(selection);
  }
  categorySection.append(categoryList);
  const categoryAdditions = element(document, "div");
  for (const [index, suggestion] of (suggestions.categories ?? []).entries()) {
    const selection = categorySelection(suggestion);
    const row = element(document, "div", undefined, "category-suggestion");
    const label = element(document, "label", `Coverage start for ${selection.category}`);
    const dateInput = element(document, "input");
    const id = `category-coverage-${index}`;
    label.setAttribute("for", id);
    dateInput.setAttribute("id", id);
    dateInput.setAttribute("type", "date");
    dateInput.setAttribute("required", "");
    if (coverageMin) dateInput.setAttribute("min", coverageMin);
    if (coverageMax) dateInput.setAttribute("max", coverageMax);
    let add;
    const validCoverageStart = () => {
      try {
        assertIsoDate(dateInput.value);
        return (
          (!coverageMin || dateInput.value >= coverageMin) &&
          (!coverageMax || dateInput.value <= coverageMax)
        );
      } catch {
        return false;
      }
    };
    add = actionButton(document, `Add ${selection.category}`, () => {
      if (row.hidden || add.disabled) return;
      draft.addCategory(selection, dateInput.value);
      appendCurrentCategory(selection);
      refreshCategoryControls();
      enableSave();
    });
    add.disabled = true;
    dateInput.addEventListener("input", () => {
      add.disabled = !validCoverageStart();
    });
    row.append(
      label,
      dateInput,
      add,
    );
    categoryAdditions.append(row);
    categorySuggestionRows.set(selection.category, row);
  }
  refreshCategoryControls();
  appendAdditionPanel(
    document,
    categorySection,
    "Add category",
    "categories",
    categoryAdditions,
  );
  container.append(categorySection);

  container.append(
    renderPreferenceSection(
      document,
      "seed_papers",
      draft,
      Array.isArray(suggestions.seed_papers) ? suggestions.seed_papers : [],
      enableSave,
      "Add seed paper",
      model?.seed_paper_details,
    ),
    renderTermsSection(document, draft, suggestions, enableSave),
    renderPreferenceSection(
      document,
      "authors",
      draft,
      Array.isArray(suggestions.authors) ? suggestions.authors : [],
      enableSave,
      "Add author",
    ),
  );

  const controls = element(document, "div", undefined, "interest-actions");
  controls.append(
    element(document, "p", "Previously shown suggestions may reappear."),
  );
  controls.append(
    actionButton(document, "Refresh suggestions", () => actions.refreshSuggestions?.()),
  );
  saveButton = actionButton(document, "Update interests", () => actions.save?.(draft));
  saveButton.disabled = !draft.dirty;
  controls.append(saveButton);
  container.append(controls);
  return container;
}
