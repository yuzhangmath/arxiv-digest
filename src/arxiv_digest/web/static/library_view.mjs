export class LibraryController {
  constructor(api) {
    if (!api || typeof api.json !== "function") {
      throw new TypeError("LibraryController requires an API client");
    }
    this.api = api;
    this.downloads = new Map();
  }

  search(query = "", offset = 0) {
    if (typeof query !== "string" || !Number.isSafeInteger(offset) || offset < 0) {
      throw new TypeError("Invalid library search");
    }
    const parameters = new URLSearchParams({
      q: query.trim(),
      offset: String(offset),
    });
    return this.api.json(
      "library-search",
      `/api/v1/library?${parameters}`,
      undefined,
    );
  }

  remove(arxivId) {
    assertArxivId(arxivId);
    return this.api.json(
      "library-remove",
      "/api/v1/library/remove",
      jsonRequest({ arxiv_id: arxivId }),
    );
  }

  async download(arxivId, version) {
    assertArxivId(arxivId);
    if (!Number.isSafeInteger(version) || version < 1) {
      throw new TypeError("Invalid stored arXiv version");
    }
    const result = await this.api.json(
      "library-pdf",
      "/api/v1/library/pdf",
      jsonRequest({
        arxiv_id: arxivId,
        version,
        save_first: false,
        save_version: null,
      }),
    );
    assertJobId(result?.job_id);
    this.downloads.set(result.job_id, Object.freeze({ arxivId, version }));
    return result;
  }

  downloadStatus(jobId) {
    assertJobId(jobId);
    return this.api.json(
      `download-${jobId}`,
      `/api/v1/downloads/${encodeURIComponent(jobId)}`,
      undefined,
    );
  }

  retryDownload(jobId) {
    assertJobId(jobId);
    const stored = this.downloads.get(jobId);
    if (!stored) throw new TypeError("Unknown download job");
    return this.download(stored.arxivId, stored.version);
  }
}

const MODERN_ID = /^\d{4}\.\d{4,5}$/;
const LEGACY_ID = /^[a-z][a-z0-9.-]*\/[0-9]{7}$/i;
const JOB_ID = /^[A-Za-z0-9_-]{8,128}$/;

function assertArxivId(value) {
  if (typeof value !== "string" || !(MODERN_ID.test(value) || LEGACY_ID.test(value))) {
    throw new TypeError("Invalid stored arXiv ID");
  }
}

function assertJobId(value) {
  if (typeof value !== "string" || !JOB_ID.test(value)) {
    throw new TypeError("Invalid download job ID");
  }
}

function jsonRequest(value) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(document, label, action) {
  const node = element(document, "button", label);
  node.setAttribute("type", "button");
  node.addEventListener("click", action);
  return node;
}

export function renderLibraryView(document, container, page, actions = {}) {
  const entries = Array.isArray(page?.entries) ? page.entries : [];
  if (entries.length > 20) {
    throw new TypeError("Library pages may contain at most 20 papers");
  }
  container.replaceChildren();
  container.append(element(document, "h1", "Library"));

  const search = element(document, "section", undefined, "library-search");
  const label = element(document, "label", "Search saved papers");
  label.setAttribute("for", "library-query");
  const input = element(document, "input");
  input.setAttribute("id", "library-query");
  input.setAttribute("name", "q");
  input.setAttribute("type", "search");
  const query = typeof page?.query === "string" ? page.query : "";
  input.value = query;
  search.append(
    element(
      document,
      "p",
      "Leave the search blank to show all saved papers below.",
      "library-search-guidance",
    ),
    label,
    input,
    button(document, "Search", () => actions.search?.(input.value)),
  );
  container.append(search);

  const list = element(document, "section", undefined, "library-results");
  list.setAttribute("aria-label", "Saved papers");
  if (entries.length === 0) {
    list.append(element(
      document,
      "p",
      query.trim()
        ? "No saved papers match this search."
        : "No saved papers yet. Save a paper from Review to add it here.",
    ));
  }
  for (const entry of entries) {
    const metadata = entry?.metadata ?? {};
    const arxivId = metadata.arxiv_id;
    assertArxivId(arxivId);
    const card = element(document, "article", undefined, "library-paper");
    card.dataset.arxivId = arxivId;
    card.append(
      element(document, "h2", typeof metadata.title === "string" ? metadata.title : "Untitled paper"),
      element(
        document,
        "p",
        Array.isArray(metadata.authors)
          ? metadata.authors.filter((value) => typeof value === "string").join(", ")
          : "",
      ),
    );
    const versionLabels = [];
    if (Number.isSafeInteger(entry.saved_version)) {
      versionLabels.push(`Saved v${entry.saved_version}`);
    } else {
      versionLabels.push("Saved version not pinned");
    }
    if (Number.isSafeInteger(entry.latest_version)) {
      versionLabels.push(`Latest v${entry.latest_version}`);
    }
    if (
      entry.paper_available !== false &&
      entry.new_version_available === true
    ) versionLabels.push("New version available");
    card.append(element(document, "p", versionLabels.join(" · "), "library-version-state"));
    const completedDownloadVersion = entry?.download?.status === "completed"
      ? entry?.download?.value?.version
      : null;
    const reportedLocalVersions = Array.isArray(entry.local_pdf_versions)
      ? entry.local_pdf_versions
      : [];
    const localPdfVersions = [
      ...new Set([...reportedLocalVersions, completedDownloadVersion].filter(
        (value) => Number.isSafeInteger(value) && value > 0,
      )),
    ].sort((left, right) => left - right);
    if (entry.paper_available === false) {
      card.append(element(document, "p", "Paper unavailable from arXiv", "library-paper-state"));
    }
    card.append(element(
      document,
      "p",
      localPdfVersions.length
        ? `Local PDF available: ${localPdfVersions.map((value) => `v${value}`).join(", ")}`
        : "No local PDF in the current destination",
      "library-pdf-state",
    ));

    const controls = element(document, "div", undefined, "paper-actions");
    controls.append(
      button(document, "Remove", () => actions.remove?.(arxivId)),
    );
    const download = entry?.download;
    if (
      entry.paper_available !== false &&
      download?.status === "failed" &&
      typeof download.job_id === "string" &&
      JOB_ID.test(download.job_id)
    ) {
      const jobId = download.job_id;
      controls.append(button(document, "Retry PDF", () => actions.retryPdf?.(jobId)));
    } else if (entry.paper_available !== false && download?.status !== "completed") {
      const version = entry.latest_version ?? entry.saved_version;
      if (
        Number.isSafeInteger(version) &&
        version > 0 &&
        !localPdfVersions.includes(version)
      ) {
        controls.append(
          button(document, `Download v${version} PDF`, () => actions.downloadPdf?.(arxivId, version)),
        );
      }
    }
    card.append(controls);
    list.append(card);
  }
  container.append(list);

  const pagination = element(document, "nav", undefined, "page-navigation");
  pagination.setAttribute("aria-label", "Library pages");
  if (Number.isSafeInteger(page?.previous_offset) && page.previous_offset >= 0) {
    const offset = page.previous_offset;
    pagination.append(button(document, "Previous page", () => actions.page?.(offset)));
  }
  if (Number.isSafeInteger(page?.next_offset) && page.next_offset >= 0) {
    const offset = page.next_offset;
    pagination.append(button(document, "Next page", () => actions.page?.(offset)));
  }
  if (pagination.children.length) container.append(pagination);
  return container;
}
