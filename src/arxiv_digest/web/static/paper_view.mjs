import { renderMathText } from "./math_view.mjs";

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
  const evidence = Array.isArray(event.evidence) ? event.evidence : [];
  const observations =
    source?.category_observations ??
    source?.observations ??
    evidence.map((item) => item?.category);
  const rawVersion = Object.hasOwn(source ?? {}, "announced_version")
    ? source.announced_version
    : source?.version ?? event.announced_version ?? null;
  const downloadVersion = source?.download_version ?? rawVersion;
  const actionVersion =
    Number.isInteger(downloadVersion) && downloadVersion > 0
      ? downloadVersion
      : storedText(downloadVersion);
  const linkVersion = Number.isInteger(actionVersion)
    ? `v${actionVersion}`
    : actionVersion;
  return {
    eventId: source?.event_id ?? event.event_id ?? null,
    arxivId: storedText(source?.arxiv_id ?? paper.arxiv_id),
    version: linkVersion,
    actionVersion,
    title: storedText(source?.title ?? paper.title, "Untitled paper"),
    authors: storedStrings(source?.authors ?? paper.authors),
    abstract: storedText(source?.abstract ?? paper.abstract),
    comments: storedText(source?.comments ?? paper.comments),
    journalRef: storedText(source?.journal_ref ?? paper.journal_ref),
    doi: storedText(source?.doi ?? paper.doi),
    rankingText: storedText(source?.ranking_text) || reasons.join("; "),
    tier: storedText(source?.tier, "other"),
    observations: storedStrings(observations),
    dateLabel: storedText(source?.date_label),
    confidenceLabel: storedText(source?.confidence_label),
    newlyDiscovered: source?.newly_discovered === true,
    announcedVersion:
      Number.isInteger(rawVersion) && rawVersion > 0
        ? rawVersion
        : typeof rawVersion === "string" && VERSION.test(rawVersion)
          ? Number(rawVersion.slice(1))
          : null,
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

function actionButton(document, label, callback, arxivId, version) {
  const button = element(document, "button", label);
  button.setAttribute("type", "button");
  button.addEventListener("click", () => callback?.(arxivId, version));
  return button;
}

export function renderPaperCard(document, container, source, actions = {}) {
  const paper = normalizedPaper(source);
  const arxivId = paper.arxivId;
  const version = paper.version;
  const actionVersion = paper.actionVersion;
  const links = arxivLinks(arxivId, version);
  const card = element(document, "article", undefined, "paper-card");
  if (paper.eventId !== null) card.dataset.eventId = String(paper.eventId);
  card.dataset.arxivId = arxivId;
  card.dataset.version = version;

  card.append(element(document, "h3", paper.title));
  card.append(element(document, "p", paper.authors.join(", "), "paper-authors"));
  const labels = [
    paper.announcedVersion === null
      ? "Announcement version unavailable"
      : `Announced v${paper.announcedVersion}`,
    paper.dateLabel,
    paper.confidenceLabel,
    paper.newlyDiscovered ? "Newly discovered" : "",
    ...paper.observations,
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

  const explanation = element(document, "section", undefined, "ranking-explanation");
  explanation.append(element(document, "h4", "Why this ranking"));
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
    safeExternalLink(document, "Abstract on arXiv", links.abstract),
    document.createTextNode(" "),
    safeExternalLink(document, "PDF on arXiv", links.pdf),
  );
  card.append(linksRow);

  const controls = element(document, "div", undefined, "paper-actions");
  controls.append(
    actionButton(document, "Save", actions.save, arxivId, actionVersion),
    actionButton(document, "Download PDF", actions.download, arxivId, actionVersion),
    actionButton(document, "Save + PDF", actions.saveAndDownload, arxivId, actionVersion),
  );
  card.append(controls);
  container.append(card);
  return card;
}
