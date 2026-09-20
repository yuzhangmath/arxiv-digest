function readableCount(count, confirmed = false) {
  return `${count} ${confirmed ? "confirmed " : ""}${count === 1 ? "paper" : "papers"}`;
}

function readableStatus(status) {
  if (status === "reviewed") return "✓ Reviewed";
  if (status === "partial") return "Partial";
  return "Unreviewed";
}

export function renderCalendar(document, container, entries, selectDate) {
  container.replaceChildren();
  const grid = document.createElement("div");
  grid.className = "calendar-grid";
  grid.setAttribute("role", "list");
  for (const source of Array.isArray(entries) ? entries : []) {
    const date = String(source?.date ?? source?.day ?? "");
    const retrievalFailed = source?.retrieval_failed === true;
    const item = document.createElement("div");
    item.className = "calendar-date-item";
    item.setAttribute("role", "listitem");
    if (retrievalFailed && source?.total_papers == null && source?.count == null) {
      const placeholder = document.createElement("div");
      placeholder.className = "calendar-date calendar-date-unavailable";
      placeholder.textContent = `${date}\nRetrieval failed`;
      item.setAttribute("aria-label", `${date}: Retrieval failed`);
      item.append(placeholder);
      grid.append(item);
      continue;
    }
    const count = Number(source?.count ?? source?.total_papers ?? 0);
    const total = Number(source?.total_papers ?? count);
    const unreviewed = Number(source?.unreviewed_papers ?? total);
    const derivedStatus = source?.finished
      ? "reviewed"
      : unreviewed > 0 && unreviewed < total
        ? "partial"
        : "unreviewed";
    const status = String(source?.status ?? derivedStatus);
    const countLabel = readableCount(count, retrievalFailed);
    const abstractProgress = status !== "reviewed" && source?.abstracts_pending === true
      ? `${Number(source.abstracts_ready ?? 0)} of ${total} abstracts available`
      : "";
    const control = document.createElement("button");
    control.className = "calendar-date";
    control.setAttribute("type", "button");
    control.setAttribute("aria-label", `${date}: ${countLabel}, ${status}${abstractProgress ? `, ${abstractProgress}` : ""}${retrievalFailed ? ", some retrievals failed" : ""}`);
    control.textContent = `${date}\n${countLabel}\n${readableStatus(status)}${abstractProgress ? `\n${abstractProgress}` : ""}${retrievalFailed ? "\nSome retrievals failed" : ""}`;
    control.dataset.date = date;
    control.dataset.status = status;
    control.addEventListener("click", () => selectDate?.(date));
    item.append(control);
    grid.append(item);
  }
  container.append(grid);
  return grid;
}
