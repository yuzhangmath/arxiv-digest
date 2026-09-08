export const MARKER_KEY = "arxiv-digest.update-transition.v1";
export const MARKER_LIFETIME_MS = 24 * 60 * 60 * 1000;
export const validJobId = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
export const validNonce = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{16,128}$/.test(value);
export const exactFields = (value, fields) => value !== null &&
  typeof value === "object" && !Array.isArray(value) &&
  Object.keys(value).length === fields.length && fields.every((field) => Object.hasOwn(value, field));

export function validMarker(value, now = Date.now()) {
  return exactFields(value, ["schema_version", "startup_nonce", "job_id", "expires_at"]) &&
    value.schema_version === 1 && validNonce(value.startup_nonce) && validJobId(value.job_id) &&
    Number.isSafeInteger(value.expires_at) && value.expires_at > now &&
    value.expires_at <= now + MARKER_LIFETIME_MS;
}

export function validBroadcast(value) {
  return exactFields(value, ["schema_version", "type", "startup_nonce", "job_id"]) &&
    value.schema_version === 1 && validNonce(value.startup_nonce) && validJobId(value.job_id) &&
    ["update_preparing", "update_restarting", "update_canceled"].includes(value.type);
}

export class TransitionStore {
  constructor({storage, now = () => Date.now()} = {}) {
    this.storage = storage;
    this.now = now;
  }
  read() {
    try {
      const text = this.storage?.getItem(MARKER_KEY);
      if (typeof text !== "string" || text.length > 2048) return null;
      const value = JSON.parse(text);
      return validMarker(value, this.now()) ? value : null;
    } catch { return null; }
  }
  write(startup_nonce, job_id) {
    const value = {schema_version: 1, startup_nonce, job_id, expires_at: this.now() + MARKER_LIFETIME_MS};
    if (!validMarker(value, this.now())) return false;
    try {
      this.storage?.setItem(MARKER_KEY, JSON.stringify(value));
      return Boolean(this.storage);
    } catch { return false; }
  }
  clear(startup_nonce, job_id) {
    if (!validNonce(startup_nonce) || !validJobId(job_id)) return false;
    const value = this.read();
    if (value?.startup_nonce !== startup_nonce || value?.job_id !== job_id) return false;
    try {
      this.storage.removeItem(MARKER_KEY);
      return true;
    } catch { return false; }
  }
}
