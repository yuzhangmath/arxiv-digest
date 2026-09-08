// Matches update_contract.DISCOVERY_DEADLINE_SECONDS; the browser integration
// checks the shared boundary with the backend's real discovery payloads.
export const DISCOVERY_DEADLINE_MS = 60_000;
const FINAL_OBSERVATION_DELAY_MS = 1_000;
const RESPONSE_TIMEOUT_MS = 5_000;
const POLL_INTERVAL_MS = 250;
const PENDING = new Set(["idle", "checking"]);
const TERMINAL = new Set([
  "current", "manual_fallback", "available_manual", "available_automatic", "preparing",
]);

// Discovery observation only. The update flow owns this observer
// without duplicating timers or resetting the budget when a tab resumes.
export class UpdatePoller {
  constructor({
    request, cancelRequest, render,
    now = () => performance.now(),
    setTimer = (callback, delay) => setTimeout(callback, delay),
    clearTimer = (timer) => clearTimeout(timer),
  }) {
    this.request = request;
    this.cancelRequest = cancelRequest;
    this.render = render;
    this.now = now;
    this.setTimer = setTimer;
    this.clearTimer = clearTimer;
    this.startedAt = null;
    this.active = false;
    this.stopped = false;
    this.generation = 0;
    this.timer = null;
  }

  start() {
    if (this.stopped || this.active) return;
    this.startedAt ??= this.now();
    this.active = true;
    this.observe(this.generation);
  }

  suspend() {
    this.active = false;
    this.generation++;
    this.clearPendingTimer();
    this.cancelRequest();
  }

  resume() {
    this.start();
  }

  stop() {
    this.stopped = true;
    this.suspend();
  }

  clearPendingTimer() {
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  async observe(generation) {
    if (generation !== this.generation || !this.active) return;
    const finalAt = this.startedAt + DISCOVERY_DEADLINE_MS + FINAL_OBSERVATION_DELAY_MS;
    const final = this.now() >= finalAt;
    const timeout = final ? RESPONSE_TIMEOUT_MS : Math.min(RESPONSE_TIMEOUT_MS, finalAt - this.now());
    let result;
    try {
      result = await Promise.race([
        Promise.resolve(this.request()),
        new Promise((resolve) => {
          this.timer = this.setTimer(() => {
            this.cancelRequest();
            resolve(null);
          }, timeout);
        }),
      ]);
    } catch (error) {
      if (generation !== this.generation || !this.active) return;
      if (error?.status === 401) {
        this.stop();
        return;
      }
      result = null;
    }
    if (generation !== this.generation || !this.active) return;
    this.clearPendingTimer();
    if (TERMINAL.has(result?.status)) {
      this.render(result);
      return;
    }
    if (final || (result !== null && !PENDING.has(result?.status))) {
      this.render({status: "manual_fallback", automatic_update: false});
      return;
    }
    if (result !== null) this.render(result);
    const remaining = finalAt - this.now();
    const delay = this.now() >= this.startedAt + DISCOVERY_DEADLINE_MS
      ? Math.max(0, remaining)
      : Math.max(0, Math.min(POLL_INTERVAL_MS, remaining));
    this.timer = this.setTimer(() => {
      this.timer = null;
      this.observe(generation);
    }, delay);
  }
}
