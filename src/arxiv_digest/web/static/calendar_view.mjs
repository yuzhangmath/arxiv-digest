function readableCount(count) {
  return `${count} ${count === 1 ? "paper" : "papers"}`;
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
    const count = Number(source?.count ?? source?.total_papers ?? 0);
    const total = Number(source?.total_papers ?? count);
    const unreviewed = Number(source?.unreviewed_papers ?? total);
    const derivedStatus = source?.finished
      ? "reviewed"
      : unreviewed > 0 && unreviewed < total
        ? "partial"
        : "unreviewed";
    const status = String(source?.status ?? derivedStatus);
    const item = document.createElement("div");
    item.className = "calendar-date-item";
    item.setAttribute("role", "listitem");
    const control = document.createElement("button");
    control.className = "calendar-date";
    control.setAttribute("type", "button");
    control.setAttribute("aria-label", `${date}: ${readableCount(count)}, ${status}`);
    control.textContent = `${date}\n${readableCount(count)}\n${readableStatus(status)}`;
    control.dataset.date = date;
    control.dataset.status = status;
    control.addEventListener("click", () => selectDate?.(date));
    item.append(control);
    grid.append(item);
  }
  container.append(grid);
  return grid;
}
