export const TOKEN_STORAGE_KEY = "arxiv-digest.session-token";
export const ALLOWED_VIEWS = Object.freeze([
  "setup",
  "review",
  "calendar",
  "library",
  "interests",
  "settings",
]);

const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const VIEW_SET = new Set(ALLOWED_VIEWS);

function checkedToken(value) {
  if (typeof value !== "string" || !TOKEN_PATTERN.test(value)) {
    throw new TypeError("Invalid dashboard session token");
  }
  return value;
}

export function validatedView(value, fallback = "review") {
  const safeFallback = VIEW_SET.has(fallback) ? fallback : "review";
  return typeof value === "string" && VIEW_SET.has(value)
    ? value
    : safeFallback;
}

export function clearSession(sessionStorage) {
  sessionStorage.removeItem(TOKEN_STORAGE_KEY);
}

export function bootstrapSession({
  location,
  history,
  sessionStorage,
  defaultView = "review",
}) {
  const fragment = new URLSearchParams(String(location.hash || "").replace(/^#/, ""));
  const hasFragment = fragment.has("token") || fragment.has("view");
  let token;
  let view;

  if (hasFragment) {
    try {
      if (
        fragment.getAll("token").length !== 1 ||
        fragment.getAll("view").length > 1
      ) {
        throw new TypeError("Invalid dashboard session token");
      }
      token = checkedToken(fragment.get("token"));
    } catch (error) {
      clearSession(sessionStorage);
      throw error;
    }
    view = validatedView(fragment.get("view"), defaultView);
    sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    token = checkedToken(sessionStorage.getItem(TOKEN_STORAGE_KEY));
    const query = new URLSearchParams(location.search || "");
    const queryView = query.getAll("view").length === 1 ? query.get("view") : null;
    view = validatedView(queryView ?? history.state?.view, defaultView);
  }

  const pathname = location.pathname || "/";
  const tokenFreeUrl = `${pathname}?view=${encodeURIComponent(view)}`;
  history.replaceState({ view }, "", tokenFreeUrl);
  return Object.freeze({ token, view });
}

function deeplyFrozenRecord(value) {
  if (Array.isArray(value)) {
    return Object.freeze(value.map(deeplyFrozenRecord));
  }
  if (value && typeof value === "object") {
    return Object.freeze(
      Object.fromEntries(
        Object.entries(value).map(([key, item]) => [key, deeplyFrozenRecord(item)]),
      ),
    );
  }
  return value;
}

export class ViewState {
  constructor(view, initial = {}) {
    this.sequence = 0;
    this.latest = new Map();
    this.current = deeplyFrozenRecord({
      view: validatedView(view),
      ...initial,
    });
  }

  get snapshot() {
    return this.current;
  }

  setView(view, values = {}) {
    this.current = deeplyFrozenRecord({ view: validatedView(view), ...values });
    return this.current;
  }

  begin(key) {
    const request = Object.freeze({ key: String(key), id: ++this.sequence });
    this.latest.set(request.key, request.id);
    return request;
  }

  commit(request, values) {
    if (!request || this.latest.get(request.key) !== request.id) return false;
    this.current = deeplyFrozenRecord({ ...this.current, ...values });
    return true;
  }
}
