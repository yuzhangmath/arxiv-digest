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
    return result;
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

function renderPreferenceSection(document, field, draft, suggestions, enableSave) {
  const section = element(document, "section", undefined, "interest-section");
  section.append(element(document, "h2", SECTION_LABELS[field]));
  const selected = new Set(
    draft.snapshot()[field].map((value) => value.toLocaleLowerCase("en-US")),
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
    row.append(checkbox, document.createTextNode(` ${value}`));
    section.append(row);
  }

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
  section.append(customLabel, customInput, add);
  return section;
}

export function renderInterestsView(document, container, model, actions = {}) {
  const draft = model?.draft;
  if (!(draft instanceof InterestsDraft)) throw new TypeError("Interests view requires a draft");
  const suggestions = model?.suggestions ?? {};
  container.replaceChildren();
  container.append(
    element(document, "h1", "Interests"),
    element(
      document,
      "p",
      "Only checked or typed values become preferences. Browsing and searching suggestions does not change your profile.",
    ),
  );
  if (typeof model?.suggestions_generated_at === "string") {
    container.append(
      element(document, "p", `Suggestions generated ${model.suggestions_generated_at}`),
    );
  }

  let saveButton;
  const enableSave = () => {
    if (saveButton) saveButton.disabled = !draft.dirty;
  };

  const categorySection = element(document, "section", undefined, "interest-section");
  categorySection.append(element(document, "h2", "Categories"));
  const categoryList = element(document, "ul");
  for (const selection of draft.snapshot().categories) {
    const item = element(document, "li");
    item.append(
      document.createTextNode(`${selection.category} (${selection.set_spec}) `),
      actionButton(document, `Remove ${selection.category}`, () => {
        draft.removeCategory(selection.category);
        enableSave();
      }),
    );
    categoryList.append(item);
  }
  categorySection.append(categoryList);
  for (const [index, suggestion] of (suggestions.categories ?? []).entries()) {
    const selection = categorySelection(suggestion);
    const row = element(document, "div", undefined, "category-suggestion");
    const label = element(document, "label", `Coverage start for ${selection.category}`);
    const dateInput = element(document, "input");
    const id = `category-coverage-${index}`;
    label.setAttribute("for", id);
    dateInput.setAttribute("id", id);
    dateInput.setAttribute("type", "date");
    row.append(
      label,
      dateInput,
      actionButton(document, `Add ${selection.category}`, () => {
        draft.addCategory(selection, dateInput.value);
        enableSave();
      }),
    );
    categorySection.append(row);
  }
  container.append(categorySection);

  for (const field of Object.keys(SECTION_LABELS)) {
    container.append(
      renderPreferenceSection(
        document,
        field,
        draft,
        Array.isArray(suggestions[field]) ? suggestions[field] : [],
        enableSave,
      ),
    );
  }

  const controls = element(document, "div", undefined, "interest-actions");
  controls.append(
    actionButton(document, "Get fresh suggestions", () => actions.refreshSuggestions?.()),
  );
  saveButton = actionButton(document, "Save interests", () => actions.save?.(draft));
  saveButton.disabled = !draft.dirty;
  controls.append(saveButton);
  container.append(controls);
  return container;
}
