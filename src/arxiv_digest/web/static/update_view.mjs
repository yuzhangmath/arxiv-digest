const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const RELEASES_URL = "https://github.com/yuzhangmath/arxiv-digest/releases";
const renderedNotices = new WeakMap();


export function renderUpdateNotice(document, container, update) {
  container.hidden = false;
  let text = "";
  let href = "";
  if (update?.status === "manual_fallback") {
    text = "Could not check for updates. ";
    href = RELEASES_URL;
  } else if (
    update?.status === "available_manual" &&
    typeof update.installed_version === "string" &&
    typeof update.available_version === "string" &&
    VERSION.test(update.installed_version) &&
    VERSION.test(update.available_version)
  ) {
    text = `arXiv Digest ${update.available_version} is available ` +
      `(installed: ${update.installed_version}). `;
    href = `${RELEASES_URL}/tag/v${update.available_version}`;
  }
  // Preserve the polite live region and keyboard focus on unchanged results.
  const key = text;
  if (renderedNotices.get(container) === key) return;
  renderedNotices.set(container, key);
  container.replaceChildren();
  if (!href) return;

  const link = document.createElement("a");
  link.textContent = "View update instructions";
  link.setAttribute("href", href);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  link.setAttribute(
    "aria-label",
    `${link.textContent} (opens in a new tab)`,
  );
  container.append(document.createTextNode(text));
  container.append(link);
}
