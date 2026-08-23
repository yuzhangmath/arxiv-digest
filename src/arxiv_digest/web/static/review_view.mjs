import { renderPaperCard } from "./paper_view.mjs";

const TIERS = Object.freeze([
  ["top", "Top"],
  ["possible", "Possible"],
  ["other", "Other"],
]);

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

function dayOf(page) {
  return String(page?.day ?? page?.date ?? "");
}

function cardsOf(page) {
  return Array.isArray(page?.cards)
    ? page.cards
    : Array.isArray(page?.papers)
      ? page.papers
      : [];
}

function tierOf(card) {
  const tier = card?.tier;
  return typeof tier === "string" ? tier : String(tier?.value ?? "other");
}

export function reviewDestination(page, action) {
  const day = dayOf(page);
  const destinations = {
    "previous-page": { date: day, anchor_event_id: page?.previous_anchor_event_id ?? null },
    "next-page": { date: day, anchor_event_id: page?.next_anchor_event_id ?? null },
    "previous-date": { date: page?.previous_date ?? null, anchor_event_id: null },
    "next-date": { date: page?.next_date ?? null, anchor_event_id: null },
    "next-unreviewed": { date: page?.next_unreviewed_date ?? null, anchor_event_id: null },
  };
  if (!Object.hasOwn(destinations, action) || !destinations[action].date) return null;
  return Object.freeze(destinations[action]);
}

export function renderReviewError(document, container, message, retry) {
  const notice = element(document, "section", undefined, "error-banner");
  notice.setAttribute("role", "alert");
  notice.append(element(document, "p", String(message)));
  notice.append(button(document, "Retry", retry));
  container.append(notice);
  return notice;
}

function navigationButton(document, label, destination, navigate) {
  const control = button(document, label, () => {
    if (destination) navigate?.(destination);
  });
  control.disabled = destination === null;
  return control;
}

export function renderReviewView(document, container, page, actions = {}) {
  const cards = cardsOf(page);
  if (cards.length > 20) throw new RangeError("Review pages cannot exceed 20 cards");
  const day = dayOf(page);
  container.replaceChildren();
  const view = element(document, "section", undefined, "review-view");
  view.append(element(document, "h1", day ? `Review ${day}` : "Review"));
  view.append(
    element(
      document,
      "p",
      `Page ${Number(page?.page_number ?? 1)} of ${Number(page?.page_count ?? 1)}`,
      "page-count",
    ),
  );

  const dateNavigation = element(document, "nav", undefined, "date-navigation");
  dateNavigation.setAttribute("aria-label", "Review dates");
  dateNavigation.append(
    navigationButton(document, "Previous date", reviewDestination(page, "previous-date"), actions.navigate),
    navigationButton(document, "Next date", reviewDestination(page, "next-date"), actions.navigate),
    navigationButton(document, "Next unreviewed", reviewDestination(page, "next-unreviewed"), actions.navigate),
  );
  view.append(dateNavigation);

  const pageNavigation = element(document, "nav", undefined, "page-navigation");
  pageNavigation.setAttribute("aria-label", "Pages for this date");
  pageNavigation.append(
    navigationButton(document, "Previous page", reviewDestination(page, "previous-page"), actions.navigate),
    navigationButton(document, "Next page", reviewDestination(page, "next-page"), actions.navigate),
  );
  view.append(pageNavigation);

  for (const [tier, label] of TIERS) {
    const section = element(document, "section", undefined, `ranking-tier tier-${tier}`);
    section.append(element(document, "h2", label));
    for (const card of cards.filter((item) => tierOf(item) === tier)) {
      renderPaperCard(document, section, card, actions.paperActions ?? {});
    }
    view.append(section);
  }

  const finish = button(document, "Finish date", () => {
    finish.disabled = true;
    const confirmation = element(document, "div", undefined, "finish-confirmation");
    confirmation.append(
      element(document, "p", "Mark every paper in the opened snapshot as reviewed?"),
      button(document, "Confirm finish", () =>
        actions.finish?.(day, Number(page?.snapshot_revision)),
      ),
    );
    finish.parentNode?.append(confirmation);
    if (!finish.parentNode) view.append(confirmation);
  });
  view.append(finish);
  container.append(view);
  return view;
}
