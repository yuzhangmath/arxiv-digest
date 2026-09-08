const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const RELEASES_URL = "https://github.com/yuzhangmath/arxiv-digest/releases";
const renderedNotices = new WeakMap();


export function renderUpdateNotice(document, container, update, {onStart} = {}) {
  container.hidden = false;
  let text = "";
  let href = "";
  if (update?.status === "manual_fallback") {
    text = "Could not check for updates. ";
    href = RELEASES_URL;
  } else if (
    ["available_manual", "available_automatic"].includes(update?.status) &&
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
  const automatic = update?.status === "available_automatic" && update.automatic_update === true && typeof onStart === "function" && href;
  const key = JSON.stringify([text, Boolean(automatic)]);
  if (renderedNotices.get(container) === key) return;
  renderedNotices.set(container, key);
  container.replaceChildren();
  if (!href) return;

  const link = document.createElement("a");
  link.textContent = automatic ? "View release notes" : "View update instructions";
  link.setAttribute("href", href);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  link.setAttribute(
    "aria-label",
    `${link.textContent} (opens in a new tab)`,
  );
  container.append(document.createTextNode(text));
  if (automatic) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Update and restart";
    button.addEventListener("click", () => onStart(update.available_version));
    container.append(button, document.createTextNode(" "));
  }
  container.append(link);
}

const renderedStates = new WeakMap();
const TROUBLESHOOTING_URL = "https://github.com/yuzhangmath/arxiv-digest/blob/main/docs/troubleshooting.md";
export const RESTART_GUIDANCE = "Updating and restarting… A new dashboard tab will open automatically. Once it appears, this tab is safe to close. If no new tab appears after a few minutes, launch arXiv Digest again to see the final outcome.";
export const PREPARATION_GUIDANCE = "Update preparation is finishing; actions are temporarily unavailable.";

function receiptText(receipt) {
  const version = receipt?.installed_version;
  if (!VERSION.test(version ?? "")) return "";
  return {
    updated: `Updated successfully to ${version}. You can safely close the previous update tab.`,
    restored: `The update failed; arXiv Digest ${version} was restored. You can retry or view update instructions.`,
    handoff_failed: "The restart handoff did not complete, so no update was installed. Fully quit any remaining arXiv Digest process, then reopen it to try again.",
    external_change_detected: "The installation changed outside arXiv Digest, so automatic update stopped without overwriting it. Fully quit and review the update instructions.",
    recovery_failed: "Automatic recovery did not complete. Follow the recovery instructions; your application data was not deleted.",
  }[receipt?.outcome] ?? "";
}

export function renderUpdateState(document, container, state, onStart) {
  const key = JSON.stringify(state);
  if (renderedStates.get(container) === key) return;
  renderedStates.set(container, key);
  container.hidden = false;
  container.replaceChildren();
  if (state.receipt) {
    const notice = document.createElement("p");
    notice.textContent = receiptText(state.receipt);
    notice.className = "update-receipt";
    if (["external_change_detected", "recovery_failed"].includes(state.receipt.outcome)) {
      notice.setAttribute("role", "alert");
    }
    container.append(notice);
  }
  if (state.mode === "restarting" || state.mode === "guarded") {
    const text = document.createElement("p");
    text.textContent = state.mode === "restarting" ? RESTART_GUIDANCE :
      "The update handoff could not be resolved safely. Fully quit any remaining arXiv Digest process, then reopen it for recovery guidance.";
    container.append(text);
    if (state.mode === "guarded") {
      const link = document.createElement("a");
      link.textContent = "View recovery instructions";
      link.setAttribute("href", TROUBLESHOOTING_URL);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.setAttribute("aria-label", "View recovery instructions (opens in a new tab)");
      container.append(link);
    }
    return;
  }
  if (state.mode === "preparing") {
    const text = document.createElement("p");
    const version = state.discovery?.available_version;
    text.textContent = state.blocked ? PREPARATION_GUIDANCE :
      state.phase === "snapshotting_environment" ? "Creating a recovery snapshot…" :
        VERSION.test(version ?? "") ? `Downloading and verifying ${version}…` : "Preparing update…";
    const button = document.createElement("button");
    button.type = "button";
    button.disabled = true;
    button.textContent = "Preparing update…";
    container.append(text, button);
    return;
  }
  if (state.mode === "failed") {
    const text = document.createElement("p");
    text.textContent = "The update did not complete. Canceled PDF and corpus jobs must be retried manually; ordinary synchronization resumes when safe.";
    container.append(text);
  }
  const notice = document.createElement("div");
  renderUpdateNotice(document, notice, state.discovery, {onStart: state.receipt_pending ? undefined : onStart});
  if (notice.textContent) container.append(notice);
}
