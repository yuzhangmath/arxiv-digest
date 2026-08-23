function readableCount(count) {
  return `${count} ${count === 1 ? "paper" : "papers"}`;
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
    const control = document.createElement("button");
    control.setAttribute("type", "button");
    control.setAttribute("role", "listitem");
    control.setAttribute("aria-label", `${date}: ${readableCount(count)}, ${status}`);
    control.textContent = `${date}\n${count}`;
    control.dataset.date = date;
    control.dataset.status = status;
    control.addEventListener("click", () => selectDate?.(date));
    grid.append(control);
  }
  container.append(grid);
  return grid;
}
