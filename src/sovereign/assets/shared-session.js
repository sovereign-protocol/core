/* Optimistic browser view of one authoritative Sovereign Session.
 *
 * Confirmed snapshots and pending human intentions are deliberately separate:
 * visible state is rebuilt by replaying pending projections over confirmed
 * state. A timeout is uncertain, never a rejection or an automatic rollback.
 */
(function installSovereignSessionView(global) {
  "use strict";

  const clone = (value) => {
    if (typeof structuredClone === "function") return structuredClone(value);
    return JSON.parse(JSON.stringify(value));
  };

  const delay = (milliseconds) =>
    new Promise((resolve) => setTimeout(resolve, milliseconds));

  const randomId = () => {
    if (global.crypto?.randomUUID) return global.crypto.randomUUID();
    return `mutation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  };

  class SessionView {
    constructor({
      request = global.SovereignApi?.request,
      mutationStatus = null,
      timeoutMs = 10000,
      retryDelays = [250, 1000, 3000, 5000],
    } = {}) {
      if (typeof request !== "function") {
        throw new Error("SessionView requires a request function");
      }
      this.request = request;
      this.mutationStatus = mutationStatus || ((mutationId) =>
        request(`/api/core/mutations/${encodeURIComponent(mutationId)}`));
      this.timeoutMs = timeoutMs;
      this.retryDelays = retryDelays;
      this.confirmed = {};
      this.sessionRevision = 0;
      this.views = new Map();
      this.pending = [];
      this.listeners = new Set();
      this.keyTails = new Map();
      this.pollTimer = null;
      this.pollPromise = null;
      this.batchDepth = 0;
      this.batchedChange = null;
      this.destroyed = false;
    }

    defineView(name, {load, merge = null}) {
      if (!name || typeof load !== "function") {
        throw new Error("A SessionView view requires a name and loader");
      }
      this.views.set(name, {
        load,
        merge: merge || ((state, payload) => ({...state, ...payload})),
        revision: -1,
        requested: false,
        promise: null,
      });
      return this;
    }

    subscribe(listener) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    }

    state() {
      let visible = clone(this.confirmed);
      for (const intention of this.pending) {
        if (!intention.project) continue;
        const projected = intention.project(visible, intention.arguments);
        if (projected !== undefined) visible = projected;
      }
      return visible;
    }

    pendingState() {
      return this.pending.map(({id, key, status}) => ({id, key, status}));
    }

    _emit(change = "state") {
      if (this.batchDepth) {
        if (!this.batchedChange) {
          this.batchedChange = change;
        } else if (this.batchedChange !== change) {
          // More than one part of the visible state changed. Preserve that
          // information instead of letting the last notification hide an
          // earlier refreshed view.
          this.batchedChange = "state";
        }
        return;
      }
      const visible = this.state();
      const pending = this.pendingState();
      for (const listener of this.listeners) {
        listener(visible, pending, {change});
      }
    }

    _beginBatch() {
      this.batchDepth += 1;
    }

    _endBatch(change = "state") {
      this.batchDepth = Math.max(0, this.batchDepth - 1);
      if (this.batchDepth) return;
      const batched = this.batchedChange;
      this.batchedChange = null;
      this._emit(batched || change);
    }

    async refresh(names = [...this.views.keys()]) {
      const requested = Array.isArray(names) ? names : [names];
      await Promise.all(requested.map((name) => this._refreshOne(name)));
      return this.state();
    }

    async _refreshOne(name) {
      const view = this.views.get(name);
      if (!view) throw new Error(`Unknown SessionView view: ${name}`);
      view.requested = true;
      if (view.promise) return view.promise;
      view.promise = (async () => {
        while (view.requested && !this.destroyed) {
          view.requested = false;
          const payload = await view.load();
          const revision = Number.isInteger(payload?.revision)
            ? payload.revision : view.revision;
          if (revision >= view.revision) {
            view.revision = revision;
            this.sessionRevision = Math.max(this.sessionRevision, revision);
            this.confirmed = view.merge(this.confirmed, payload);
            this._emit(name);
          }
        }
      })().finally(() => {
        view.promise = null;
        if (view.requested && !this.destroyed) {
          queueMicrotask(() => this._refreshOne(name));
        }
      });
      return view.promise;
    }

    mutate({
      key,
      command,
      arguments: args = {},
      project = null,
      action,
      invalidates = [],
      mutationId = randomId(),
    }) {
      if (
        !key || typeof action !== "function"
        || (project !== null && typeof project !== "function")
      ) {
        return Promise.reject(
          new Error("A mutation requires key, action and project"),
        );
      }
      const intention = {
        id: mutationId,
        key,
        command: command || key,
        arguments: clone(args),
        project,
        status: "pending",
      };
      this.pending.push(intention);
      this._emit(project ? "state" : "pending");

      const predecessor = this.keyTails.get(key) || Promise.resolve();
      const execution = predecessor
        .catch(() => undefined)
        .then(() => this._execute(intention, action, invalidates));
      let safeTail;
      const tail = execution.finally(() => {
        if (this.keyTails.get(key) === safeTail) this.keyTails.delete(key);
      });
      // The stored tail must never become an unhandled rejection merely
      // because the application chooses not to await an interaction.
      safeTail = tail.catch(() => undefined);
      this.keyTails.set(key, safeTail);
      return execution;
    }

    async _execute(intention, action, invalidates) {
      let attempt = 0;
      while (!this.destroyed) {
        try {
          const response = await this._attempt(intention, action);
          return await this._confirm(intention, response, invalidates);
        } catch (error) {
          if (error?.definitive) {
            this._remove(intention);
            throw error;
          }
          intention.status = "uncertain";
          this._emit("pending");

          try {
            const known = await this.mutationStatus(intention.id);
            if (known?.status === "ok") {
              return await this._confirm(intention, known, invalidates);
            }
            if (known?.status === "error") {
              const rejected = new Error(known.reason || "Mutation rejected");
              rejected.definitive = true;
              rejected.payload = known;
              this._remove(intention);
              throw rejected;
            }
          } catch (statusError) {
            if (statusError?.definitive) {
              this._remove(intention);
              throw statusError;
            }
            // Losing the status request is still uncertainty, not rejection.
          }

          const wait = this.retryDelays[
            Math.min(attempt, this.retryDelays.length - 1)
          ];
          attempt += 1;
          await delay(wait);
          intention.status = "pending";
          this._emit("pending");
          // Retry the same mutation ID. The Session ledger makes this safe
          // when the first response was lost after the local commit.
        }
      }
      throw new Error("SessionView was destroyed");
    }

    async _attempt(intention, action) {
      const controller = new AbortController();
      let timer;
      const timeout = new Promise((_resolve, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new DOMException("Mutation timed out", "AbortError"));
        }, this.timeoutMs);
      });
      try {
        return await Promise.race([
          Promise.resolve(action({
            mutationId: intention.id,
            signal: controller.signal,
            command: intention.command,
            arguments: clone(intention.arguments),
          })),
          timeout,
        ]);
      } finally {
        clearTimeout(timer);
      }
    }

    async _confirm(intention, response, fallbackInvalidates) {
      intention.status = "confirming";
      this.sessionRevision = Math.max(
        this.sessionRevision,
        Number.isInteger(response?.revision) ? response.revision : 0,
      );
      const invalidates = response?.invalidates?.length
        ? response.invalidates : fallbackInvalidates;
      // Replacing an optimistic projection with its confirmed snapshot is one
      // visible transition. A refresh may already contain the real created
      // object while the temporary projection is still pending; emitting
      // between refresh and removal would briefly render both.
      this._beginBatch();
      try {
        let attempt = 0;
        while (invalidates.length && !this.destroyed) {
          try {
            await this.refresh(invalidates);
            break;
          } catch (_error) {
            intention.status = "uncertain";
            this._emit("pending");
            const wait = this.retryDelays[
              Math.min(attempt, this.retryDelays.length - 1)
            ];
            attempt += 1;
            await delay(wait);
          }
        }
        if (!invalidates.length && intention.project) {
          const projected = intention.project(
            clone(this.confirmed), intention.arguments,
          );
          if (projected !== undefined) this.confirmed = projected;
        }
        this._remove(intention);
      } finally {
        this._endBatch(intention.project ? "state" : "pending");
      }
      return response;
    }

    _remove(intention) {
      const index = this.pending.indexOf(intention);
      if (index >= 0) this.pending.splice(index, 1);
      this._emit(intention.project ? "state" : "pending");
    }

    startPolling({
      views = [...this.views.keys()],
      intervalMs = 1500,
      when = () => true,
    } = {}) {
      this.stopPolling();
      this.pollTimer = setInterval(() => {
        if (this.pollPromise || this.destroyed || !when()) return;
        this.pollPromise = this.request("/api/core/revision")
          .then((payload) => {
            if (!Number.isInteger(payload?.revision)) return undefined;
            const staleViews = views.filter(
              (name) => this.views.get(name)?.revision !== payload.revision,
            );
            if (staleViews.length) return this.refresh(staleViews);
            return undefined;
          })
          .catch(() => undefined)
          .finally(() => { this.pollPromise = null; });
      }, intervalMs);
      return this;
    }

    stopPolling() {
      if (this.pollTimer) clearInterval(this.pollTimer);
      this.pollTimer = null;
    }

    destroy() {
      this.destroyed = true;
      this.stopPolling();
      this.listeners.clear();
    }
  }

  global.SovereignSessionView = Object.freeze({
    create(options) {
      return new SessionView(options);
    },
  });
})(typeof window === "undefined" ? globalThis : window);
