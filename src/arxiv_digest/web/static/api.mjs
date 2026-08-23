export class StaleResponseError extends Error {
  constructor() {
    super("A newer request replaced this response");
    this.name = "StaleResponseError";
  }
}

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message || "The local service could not complete the request");
    this.name = "ApiError";
    this.status = status;
    this.code = code || "invalid_response";
  }

  static async fromResponse(response) {
    try {
      const payload = await response.json();
      if (
        payload &&
        Object.getPrototypeOf(payload) === Object.prototype &&
        payload.api_version === "v1" &&
        payload.ok === false &&
        Object.keys(payload).length === 3 &&
        payload.error &&
        Object.getPrototypeOf(payload.error) === Object.prototype &&
        Object.keys(payload.error).every((key) => key === "code" || key === "message") &&
        typeof payload.error.code === "string" &&
        typeof payload.error.message === "string"
      ) {
        return new ApiError(response.status, payload.error.code, payload.error.message);
      }
    } catch {
      // Keep malformed server data out of the UI.
    }
    return new ApiError(
      response.status,
      "invalid_response",
      "The local service returned an invalid response",
    );
  }
}

export function validateEnvelope(payload) {
  if (
    !payload ||
    Object.getPrototypeOf(payload) !== Object.prototype ||
    Object.keys(payload).length !== 3 ||
    payload.api_version !== "v1" ||
    payload.ok !== true ||
    !Object.hasOwn(payload, "data")
  ) {
    throw new TypeError("Invalid API response envelope");
  }
  return payload.data;
}

export class ApiClient {
  constructor(origin, token, fetchImpl = fetch, onAuthenticationRejected = () => {}) {
    this.origin = new URL(origin).origin;
    this.token = token;
    this.fetchImpl = fetchImpl;
    this.onAuthenticationRejected = onAuthenticationRejected;
    this.controllers = new Map();
  }

  json(key, path, options = {}) {
    const url = new URL(path, this.origin);
    if (url.origin !== this.origin || !url.pathname.startsWith("/api/v1/")) {
      throw new TypeError("API path escaped the local origin");
    }
    this.controllers.get(key)?.abort();
    const controller = new AbortController();
    this.controllers.set(key, controller);
    const headers = {
      ...(options.headers ?? {}),
      Authorization: `Bearer ${this.token}`,
    };
    const request = this.fetchImpl.call(globalThis, url, {
      ...options,
      headers,
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    return (async () => {
      try {
        const response = await request;
        if (this.controllers.get(key) !== controller) {
          throw new StaleResponseError();
        }
        if (!response.ok) {
          const error = await ApiError.fromResponse(response);
          if (response.status === 401) this.onAuthenticationRejected();
          throw error;
        }
        const payload = await response.json();
        if (this.controllers.get(key) !== controller) {
          throw new StaleResponseError();
        }
        return validateEnvelope(payload);
      } finally {
        if (this.controllers.get(key) === controller) {
          this.controllers.delete(key);
        }
      }
    })();
  }
}
