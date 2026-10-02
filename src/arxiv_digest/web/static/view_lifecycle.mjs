import { StaleResponseError } from "./api.mjs";

// Tracks responses and timers owned by a view or a running application task.
export class ViewLifecycle {
  constructor(isActive = () => true) {
    this.isActive = isActive;
    this.generation = 0;
    this.timers = new Set();
  }

  capture() {
    return this.generation;
  }

  isCurrent(generation) {
    return generation === this.generation && this.isActive();
  }

  async wait(promise, generation = this.capture()) {
    try {
      return await promise;
    } finally {
      if (!this.isCurrent(generation)) throw new StaleResponseError();
    }
  }

  cancelScheduled() {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers.clear();
  }

  invalidate() {
    this.cancelScheduled();
    return ++this.generation;
  }

  schedule(callback, delay) {
    const generation = this.capture();
    const timer = setTimeout(() => {
      this.timers.delete(timer);
      if (this.isCurrent(generation)) callback();
    }, delay);
    this.timers.add(timer);
  }
}
