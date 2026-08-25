import { renderInlineMathText, renderMathText } from "./math_view.mjs";

const MODERN_ID = /^\d{4}\.\d{4,5}$/;
const LEGACY_ID = /^[a-z][a-z0-9.-]*\/[0-9]{7}$/i;
const VERSION = /^v[1-9][0-9]*$/;

function storedText(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function storedStrings(value) {
  return Array.isArray(value)
    ? value.filter((item) => typeof item === "string")
    : [];
}

function normalizedPaper(source) {
  const paper = source?.paper ?? source ?? {};
  const event = source?.event ?? {};
  const reasons = Array.isArray(source?.reasons)
    ? source.reasons
        .map((reason) =>
          typeof reason === "string" ? reason : storedText(reason?.label),
        )
        .filter(Boolean)
    : [];
  const categories = source?.support_categories;
  const rawVersion = source?.resolved_announcement_version ?? null;
  const resolvedVersion = Number.isInteger(rawVersion) && rawVersion > 0
    ? rawVersion
    : typeof rawVersion === "string" && VERSION.test(rawVersion)
      ? Number(rawVersion.slice(1))
      : null;
  const latestValue = source?.latest_known_version ?? resolvedVersion;
  const latestVersion = Number.isInteger(latestValue) && latestValue > 0
    ? latestValue
    : typeof latestValue === "string" && VERSION.test(latestValue)
      ? Number(latestValue.slice(1))
      : null;
  const linkVersion = resolvedVersion === null ? "" : `v${resolvedVersion}`;
  const resolution = storedText(source?.version_resolution);
  const versionLabel = storedText(source?.version_label) ||
    (resolution === "atom_confirmed" && resolvedVersion !== null
      ? `Announced v${resolvedVersion} — Atom-confirmed`
      : resolution === "chronology_matched" && resolvedVersion !== null
        ? `Version v${resolvedVersion} — matched by chronology`
        : resolution === "unconfirmed"
          ? "Version not confirmed"
          : "");
  return {
    eventId: source?.event_id ?? event.event_id ?? null,
    arxivId: storedText(source?.arxiv_id ?? paper.arxiv_id),
    version: linkVersion,
    saveVersion: resolvedVersion,
    downloadVersion: resolvedVersion ?? latestVersion,
    latestVersion,
    title: storedText(source?.title ?? paper.title, "Untitled paper"),
    authors: storedStrings(source?.authors ?? paper.authors),
    abstract: storedText(source?.abstract ?? paper.abstract),
    comments: storedText(source?.comments ?? paper.comments),
    journalRef: storedText(source?.journal_ref ?? paper.journal_ref),
    doi: storedText(source?.doi ?? paper.doi),
    rankingText: storedText(source?.ranking_text) || reasons.join("; "),
    tier: storedText(source?.tier, "other"),
    categories: storedStrings(categories),
    dailyListDate: storedText(source?.daily_list_date),
    eventLabel: storedText(source?.event_label),
    versionLabel,
    versionResolution: resolution,
    newlyDiscovered: source?.newly_discovered === true,
    announcedVersion: resolvedVersion,
  };
}

export function arxivLinks(arxivId, version = "") {
  if (typeof arxivId !== "string" || !(MODERN_ID.test(arxivId) || LEGACY_ID.test(arxivId))) {
    throw new TypeError("Invalid stored arXiv ID");
  }
  if (version !== "" && (typeof version !== "string" || !VERSION.test(version))) {
    throw new TypeError("Invalid stored arXiv version");
  }
  const identifier = `${arxivId}${version}`;
  return Object.freeze({
    abstract: `https://arxiv.org/abs/${identifier}`,
    pdf: `https://arxiv.org/pdf/${identifier}.pdf`,
  });
}

function element(document, tag, text, className = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeExternalLink(document, label, href) {
  const link = element(document, "a", label);
  const parsed = new URL(href);
  if (parsed.protocol !== "https:" || parsed.hostname !== "arxiv.org") {
    throw new TypeError("Paper link escaped the arXiv allowlist");
  }
  link.setAttribute("href", parsed.href);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  return link;
}

function actionButton(document, label, callback, ...values) {
  const button = element(document, "button", label);
  button.setAttribute("type", "button");
  button.addEventListener("click", () => callback?.(...values));
  return button;
}

function saveButton(document, callback, failure, arxivId, version, status) {
  const button = element(document, "button", "Save");
  button.setAttribute("type", "button");
  button.addEventListener("click", async () => {
    if (typeof callback !== "function") return;
    button.disabled = true;
    button.textContent = "Saving…";
    status.textContent = "Saving paper…";
    try {
      await callback(arxivId, version);
      button.textContent = "Saved";
      status.textContent = "Paper saved to Library.";
    } catch (error) {
      button.disabled = false;
      button.textContent = "Save";
      status.textContent = "Paper was not saved. Try again.";
      failure?.(error);
    }
  });
  return button;
}

export function renderPaperCard(document, container, source, actions = {}) {
  const paper = normalizedPaper(source);
  const arxivId = paper.arxivId;
  const version = paper.version;
  const saveVersion = paper.saveVersion;
  const downloadVersion = paper.downloadVersion;
  const unconfirmed = paper.announcedVersion === null;
  const links = arxivLinks(arxivId, version);
  const card = element(document, "article", undefined, "paper-card");
  if (paper.eventId !== null) card.dataset.eventId = String(paper.eventId);
  card.dataset.arxivId = arxivId;
  card.dataset.version = version;

  const title = element(document, "h3", paper.title);
  if (globalThis.katex?.render) {
    renderInlineMathText(title, paper.title, globalThis.katex, document);
  }
  card.append(title);
  card.append(element(document, "p", paper.authors.join(", "), "paper-authors"));
  const labels = [
    paper.versionLabel,
    paper.dailyListDate
      ? `arXiv daily-list date: ${paper.dailyListDate}`
      : "",
    paper.eventLabel,
    paper.newlyDiscovered ? "Newly discovered" : "",
    paper.categories.length
      ? `Recovered under: ${paper.categories.join(" · ")}`
      : "",
  ].filter(Boolean);
  if (labels.length) card.append(element(document, "p", labels.join(" · "), "paper-labels"));
  if (paper.comments) card.append(element(document, "p", paper.comments, "paper-comments"));
  if (paper.journalRef) {
    card.append(element(document, "p", `Journal reference: ${paper.journalRef}`, "paper-journal"));
  }
  if (paper.doi) card.append(element(document, "p", `DOI: ${paper.doi}`, "paper-doi"));

  const details = element(document, "details");
  details.append(element(document, "summary", "Read abstract"));
  const abstract = element(document, "p", paper.abstract, "paper-abstract");
  if (globalThis.katex?.render) {
    renderMathText(abstract, paper.abstract, globalThis.katex, document);
  }
  details.append(abstract);
  card.append(details);

  const explanation = element(document, "details", undefined, "ranking-explanation");
  explanation.append(element(document, "summary", "Why this ranking"));
  explanation.append(
    element(
      document,
      "p",
      paper.rankingText || "No selected interest changed this paper's order.",
    ),
  );
  card.append(explanation);

  const linksRow = element(document, "p", undefined, "paper-links");
  linksRow.append(
    safeExternalLink(
      document,
      unconfirmed
        ? "Abstract on arXiv (latest version)"
        : "Abstract on arXiv",
      links.abstract,
    ),
    document.createTextNode(" "),
    safeExternalLink(
      document,
      unconfirmed ? "PDF on arXiv (latest version)" : "PDF on arXiv",
      links.pdf,
    ),
  );
  card.append(linksRow);

  const controls = element(document, "div", undefined, "paper-actions");
  const actionStatus = element(document, "p", "", "paper-action-status");
  actionStatus.setAttribute("role", "status");
  actionStatus.setAttribute("aria-live", "polite");
  controls.append(
    saveButton(
      document,
      actions.save,
      actions.failure,
      arxivId,
      saveVersion,
      actionStatus,
    ),
    actionButton(
      document,
      unconfirmed && downloadVersion !== null
        ? `Download latest v${downloadVersion} — announcement version unconfirmed`
        : "Download PDF",
      actions.download,
      arxivId,
      downloadVersion,
    ),
    actionButton(
      document,
      unconfirmed && downloadVersion !== null
        ? `Save unpinned + download latest v${downloadVersion} — announcement version unconfirmed`
        : "Save + PDF",
      actions.saveAndDownload,
      ...(unconfirmed
        ? [arxivId, null, downloadVersion]
        : [arxivId, downloadVersion]),
    ),
  );
  if (unconfirmed && downloadVersion === null) {
    const [download, saveAndDownload] = Array.from(controls.children).slice(-2);
    download.disabled = true;
    saveAndDownload.disabled = true;
    download.textContent = "PDF unavailable — announcement version unconfirmed";
    saveAndDownload.textContent = "Save unpinned (PDF unavailable)";
  }
  card.append(controls, actionStatus);
  container.append(card);
  return card;
}
