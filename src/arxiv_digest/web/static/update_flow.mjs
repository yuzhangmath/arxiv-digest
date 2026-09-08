import { UpdatePoller } from "./update_poll.mjs";
import { TransitionStore, exactFields, validBroadcast, validJobId, validNonce } from "./update_transition.mjs";

export const COMMIT_MESSAGE = "Updating arXiv Digest. A new dashboard will open automatically. You can close this tab.";
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;
const PHASES = new Set(["downloading", "verifying", "snapshotting_environment", "stopping_work", "backing_up", "preparing_recovery", "ready_to_restart", "restarting"]);
const QUIESCENT = new Set(["stopping_work", "backing_up", "preparing_recovery", "ready_to_restart", "restarting"]);
const GUARDED = new Set(["helper_cancel_failed", "helper_commit_failed", "external_change_detected"]);
const ERRORS = new Set(["eligibility_changed", "download_failed", "verification_failed", "snapshot_failed", "work_did_not_stop", "backup_failed", "recovery_preparation_failed", "helper_failed", "handoff_not_acknowledged", "helper_cancel_failed", "helper_commit_aborted", "helper_commit_failed", "external_change_detected", "application_closing", "pending_update_receipt"]);
const RECEIPT_CODES = {updated: "update_succeeded", restored: "update_failed_restored", handoff_failed: "handoff_failed", external_change_detected: "external_change_detected", recovery_failed: "recovery_failed"};
const REQUEST_TIMEOUT_MS = 5000;
const PREPARATION_OBSERVATION_MS = 15 * 60 * 1000;
const REQUEST_KEYS = ["update-health", "release-update", "update-receipt", "update-receipt-ack", "update-start", "update-job", "update-commit", "update-handoff"];
const jsonPost = (value) => ({method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(value)});
const validVersion = (value) => typeof value === "string" && value.length <= 64 && VERSION.test(value);

export function validUpdateJob(value, jobId) {
  const terminal = ["failed", "canceled"].includes(value?.state);
  return exactFields(value, terminal
    ? ["job_id", "state", "phase", "complete", "error_code", "message"]
    : ["job_id", "state", "phase", "complete"]) &&
    value.job_id === jobId && validJobId(value.job_id) && PHASES.has(value.phase) &&
    ["running", "ready_to_restart", "restarting", "failed", "canceled"].includes(value.state) &&
    (value.state !== "ready_to_restart" || value.phase === "ready_to_restart") &&
    (value.state !== "restarting" || value.phase === "restarting") &&
    typeof value.complete === "boolean" && value.complete === terminal &&
    (!terminal || ERRORS.has(value.error_code) && typeof value.message === "string" && value.message.length <= 16384);
}

export function validReceipt(value) {
  return exactFields(value, ["receipt_id", "outcome", "installed_version", "attempted_version", "message_code"]) &&
    validJobId(value.receipt_id) && Object.hasOwn(RECEIPT_CODES, value.outcome) &&
    RECEIPT_CODES[value.outcome] === value.message_code &&
    validVersion(value.installed_version) && validVersion(value.attempted_version);
}

// One owner for discovery, preparation, handoff, receipts and reconciliation.
// UI callbacks render synchronously; no callback performs protocol requests.
export class UpdateFlow {
  constructor({api, render, storage, broadcast = () => {},
    now = () => performance.now(), wallNow = () => Date.now(),
    setTimer = (callback, delay) => setTimeout(callback, delay),
    clearTimer = (timer) => clearTimeout(timer),
  }) {
    Object.assign(this, {api, render, broadcast, now, setTimer, clearTimer});
    this.markers = new TransitionStore({storage, now: wallNow});
    this.nonce = null;
    this.jobId = null;
    this.generation = 0;
    this.stopped = false;
    this.suspended = false;
    this.busy = false;
    this.initiator = false;
    this.mode = "idle";
    this.phase = null;
    this.blocked = false;
    this.receipt = null;
    this.discovery = null;
    this.errorCode = null;
    this.retired = new Set();
    this.timer = null;
    this.startedAt = null;
    this.reconciling = null;
    this.receiptsSeen = new Set();
    this.receiptAckPending = null;
    this.receiptAckRequest = null;
    this.poller = new UpdatePoller({
      request: () => this.api.json("release-update", "/api/v1/update"),
      cancelRequest: () => this.api.abort("release-update"),
      render: (value) => this.observeDiscovery(value), now, setTimer, clearTimer,
    });
  }

  get blocksOrdinaryWork() { return this.blocked; }
  get handoff() { return ["restarting", "guarded"].includes(this.mode); }
  get allowQuit() { return this.mode === "guarded"; }
  current(generation) { return !this.stopped && !this.suspended && generation === this.generation; }

  emit() {
    this.render({discovery: this.discovery, mode: this.mode, phase: this.phase,
      job_id: this.jobId, blocked: this.blocked, busy: this.busy,
      allowQuit: this.allowQuit, error_code: this.errorCode, receipt: this.receipt,
      receipt_pending: this.receiptAckPending !== null});
  }

  async request(key, path, options) {
    let timer;
    try {
      return await Promise.race([
        this.api.json(key, path, options),
        new Promise((_, reject) => {
          timer = this.setTimer(() => {this.api.abort(key); reject(new Error("Update response timeout"));}, REQUEST_TIMEOUT_MS);
        }),
      ]);
    } finally { this.clearTimer(timer); }
  }

  async start() {
    if (this.stopped || this.starting) return;
    this.starting = true;
    const generation = this.generation;
    try {
      const status = await this.request("update-health", "/api/v1/status");
      if (!this.current(generation) || !validNonce(status?.startup_nonce)) return;
      this.nonce = status.startup_nonce;
      this.checkReceipt(generation);
      if (this.checkMarker()) { this.reconcile(); return; }
      this.poller.start();
    } catch { /* A later focus/resume can retry local health without a marker guess. */ }
    finally { this.starting = false; }
  }

  observeDiscovery(value) {
    if (this.stopped || this.suspended || this.handoff) return;
    this.discovery = value;
    if (value?.status === "preparing" && validJobId(value.job_id) && PHASES.has(value.phase)) {
      if (this.retired.has(value.job_id)) return;
      this.jobId = value.job_id;
      this.busy = true;
      this.startedAt ??= this.now();
      this.showPreparation(value.phase);
      this.observeJob(this.generation);
      return;
    }
    if (!this.jobId && this.mode === "preparing") {
      this.blocked = false;
      this.busy = false;
      this.mode = "idle";
    }
    this.emit();
  }

  showPreparation(phase, {notify = true} = {}) {
    const changed = this.phase !== phase || !this.blocked;
    this.phase = phase;
    this.mode = "preparing";
    this.blocked ||= QUIESCENT.has(phase);
    this.emit();
    if (this.blocked && changed && notify) this.send("update_preparing");
  }

  async startUpdate(version) {
    if (this.busy || this.receiptAckPending !== null || this.stopped || this.suspended || !this.nonce || !validVersion(version) ||
      this.discovery?.status !== "available_automatic" || this.discovery.automatic_update !== true ||
      this.discovery.available_version !== version) return;
    this.busy = true;
    this.initiator = true;
    this.errorCode = null;
    this.jobId = null;
    this.startedAt = this.now();
    const generation = ++this.generation;
    this.poller.suspend();
    this.showPreparation("downloading");
    try {
      const value = await this.request("update-start", "/api/v1/update/start", jsonPost({target_version: version}));
      if (!this.current(generation)) return;
      if (!exactFields(value, ["job_id", "state", "phase"]) || !validJobId(value.job_id) ||
        value.state !== "running" || value.phase !== "downloading") throw new Error("Invalid update start");
      this.jobId = value.job_id;
      await this.observeJob(generation);
    } catch (error) {
      if (!this.current(generation)) return;
      // A lost start response can still have started the server-owned attempt.
      // Observe its authoritative state before offering a second start.
      if (error?.code && ERRORS.has(error.code)) this.finishFailure(error.code);
      else { this.mode = "preparing"; this.emit(); this.poller.resume(); }
    }
  }

  clearJobTimer() {
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  async observeJob(generation) {
    if (!this.current(generation) || !this.jobId || this.handoff) return;
    this.clearJobTimer();
    const job = this.jobId;
    try {
      const value = await this.request("update-job", `/api/v1/update/jobs/${job}`);
      if (!this.current(generation) || this.jobId !== job) return;
      if (!validUpdateJob(value, job)) throw new Error("Invalid update job");
      if (value.complete) { this.finishFailure(value.error_code); return; }
      this.showPreparation(value.phase);
      if (value.state === "ready_to_restart" && this.initiator) {
        await this.commit(generation, job);
        return;
      }
    } catch (error) {
      if (!this.current(generation) || this.jobId !== job) return;
      if (error?.status === 401) { this.stop(); return; }
    }
    if (this.current(generation) && !this.handoff && this.now() - this.startedAt < PREPARATION_OBSERVATION_MS) {
      this.timer = this.setTimer(() => {this.timer = null; this.observeJob(generation);}, 500);
    }
  }

  async commit(generation, job) {
    try {
      const value = await this.request("update-commit", `/api/v1/update/jobs/${job}/commit`, jsonPost({}));
      if (!this.current(generation) || this.jobId !== job) return;
      if (!exactFields(value, ["job_id", "state", "phase", "message"]) || value.job_id !== job ||
        value.state !== "restarting" || value.phase !== "restarting" || value.message !== COMMIT_MESSAGE) {
        throw new Error("Invalid update commit response");
      }
      this.enterHandoff(); // synchronous visible render comes first
      this.markers.write(this.nonce, job);
      this.send("update_restarting");
      const acknowledged = await this.request("update-handoff", `/api/v1/update/jobs/${job}/handoff-ack`, jsonPost({}));
      if (!exactFields(acknowledged, ["job_id", "state", "phase"]) || acknowledged.job_id !== job ||
        acknowledged.state !== "restarting" || acknowledged.phase !== "restarting") throw new Error("Invalid handoff acknowledgement");
    } catch {
      if (!this.current(generation) || this.jobId !== job) return;
      // One bounded reconciliation handles a lost acknowledgement. There is
      // no repeating timer probing an old server after a commit response.
      await this.reconcile();
      if (this.current(generation) && this.jobId === job && !this.handoff) {
        this.initiator = false;
        this.timer = this.setTimer(() => {this.timer = null; this.observeJob(generation);}, 500);
      }
    }
  }

  enterHandoff() {
    this.mode = "restarting";
    this.phase = "restarting";
    this.busy = true;
    this.blocked = true;
    this.clearJobTimer();
    this.poller.suspend();
    this.emit();
  }

  finishFailure(code) {
    this.clearJobTimer();
    this.errorCode = ERRORS.has(code) ? code : "helper_failed";
    if (GUARDED.has(code)) {
      this.mode = "guarded";
      this.blocked = true;
      this.busy = true;
      this.poller.suspend();
      this.emit();
      return;
    }
    if (this.jobId) {
      this.markers.clear(this.nonce, this.jobId);
      this.send("update_canceled");
      this.retired.add(this.jobId);
      if (this.retired.size > 128) this.retired.delete(this.retired.values().next().value);
    }
    this.jobId = null;
    this.busy = false;
    this.initiator = false;
    this.blocked = false;
    this.mode = "failed";
    this.phase = null;
    this.generation++;
    this.emit();
  }

  handleApiError(error) {
    if (this.stopped || this.handoff || error?.status !== 409 || error.code !== "update_in_progress") return;
    this.blocked = true;
    this.busy = true;
    this.mode = "preparing";
    this.emit();
    if (!this.jobId) { this.poller.suspend(); this.poller.resume(); }
  }

  send(type) {
    const value = {schema_version: 1, type, startup_nonce: this.nonce, job_id: this.jobId};
    if (!validBroadcast(value)) return;
    try { this.broadcast(value); } catch { /* Cross-tab delivery is best effort. */ }
  }

  receive(value) {
    if (this.stopped || !this.nonce || !validBroadcast(value) || value.startup_nonce !== this.nonce ||
      this.retired.has(value.job_id) || this.jobId && value.job_id !== this.jobId) return;
    this.jobId = value.job_id;
    if (value.type === "update_restarting") this.enterHandoff();
    else if (value.type === "update_canceled") this.reconcile();
    else if (!this.handoff) {
      this.busy = true;
      this.startedAt ??= this.now();
      this.showPreparation("stopping_work", {notify: false});
      if (!this.suspended) this.observeJob(this.generation);
    }
  }

  checkMarker() {
    if (!this.nonce || this.stopped) return false;
    const value = this.markers.read();
    if (value?.startup_nonce !== this.nonce || this.retired.has(value.job_id) ||
      this.jobId && this.jobId !== value.job_id) return false;
    this.jobId = value.job_id;
    if (this.mode !== "guarded") this.enterHandoff();
    return true;
  }

  async reconcile() {
    if (this.stopped || this.suspended || !this.nonce || !this.jobId || this.reconciling) return;
    const generation = this.generation;
    const job = this.jobId;
    this.reconciling = job;
    try {
      const value = await this.request("update-job", `/api/v1/update/jobs/${job}`);
      if (!this.current(generation) || this.jobId !== job || !validUpdateJob(value, job)) return;
      if (value.complete) this.finishFailure(value.error_code);
    } catch { /* An unreachable old server cannot prove cancellation. */ }
    finally { if (this.reconciling === job) this.reconciling = null; }
  }

  async checkReceipt(generation) {
    try {
      const value = await this.request("update-receipt", "/api/v1/update/receipt");
      if (!this.current(generation) || !exactFields(value, ["receipt"])) return;
      if (value.receipt === null) {
        if (this.receiptAckPending !== null) { this.receiptAckPending = null; this.emit(); }
        return;
      }
      if (!validReceipt(value.receipt)) return;
      this.receipt = value.receipt;
      this.receiptsSeen.add(value.receipt.receipt_id);
      this.receiptAckPending = value.receipt.receipt_id;
      this.emit();
      await this.acknowledgeReceipt(generation);
    } catch (error) { if (this.current(generation) && error?.status === 401) this.stop(); }
  }

  async acknowledgeReceipt(generation) {
    const identifier = this.receiptAckPending;
    if (!identifier || !this.current(generation) || this.receiptAckRequest?.generation === generation) return;
    const request = {identifier, generation};
    this.receiptAckRequest = request;
    this.emit(); // Preserve the visible receipt before every bounded retry.
    try {
      const value = await this.request("update-receipt-ack", `/api/v1/update/receipt/${identifier}/ack`, jsonPost({}));
      if (!this.current(generation) || this.receiptAckPending !== identifier ||
        this.receipt?.receipt_id !== identifier || !exactFields(value, ["acknowledged"]) ||
        typeof value.acknowledged !== "boolean") return;
      if (value.acknowledged) { this.receiptAckPending = null; this.emit(); }
      else await this.checkReceipt(generation);
    } catch (error) {
      if (this.current(generation) && error?.status === 401) this.stop();
    } finally { if (this.receiptAckRequest === request) this.receiptAckRequest = null; }
  }

  suspend() {
    this.suspended = true;
    this.generation++;
    this.clearJobTimer();
    this.poller.suspend();
    for (const key of REQUEST_KEYS) this.api.abort(key);
  }

  resume() {
    if (this.stopped) return;
    this.suspended = false;
    if (this.checkMarker() || this.handoff) { this.reconcile(); return; }
    if (!this.nonce) { this.start(); return; }
    if (this.receiptAckPending !== null) this.acknowledgeReceipt(this.generation);
    if (this.jobId) this.observeJob(this.generation);
    else this.poller.resume();
  }

  stop() {
    this.stopped = true;
    this.suspend();
    this.poller.stop();
  }
}
