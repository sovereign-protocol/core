"use strict";

const assert = require("node:assert/strict");
require("../src/sovereign/assets/shared-api.js");
require("../src/sovereign/assets/shared-session.js");

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return {promise, resolve, reject};
};

async function sameKeyRebasesAfterEarlierRejection() {
  let confirmed = {value: 0};
  const first = deferred();
  const second = deferred();
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: 0}),
    mutationStatus: async () => ({status: "unknown"}),
  });
  view.defineView("value", {
    load: async () => ({...confirmed, revision: confirmed.value}),
  });
  await view.refresh("value");

  const firstRun = view.mutate({
    key: "value",
    arguments: {value: 1},
    project: (state, change) => ({...state, value: change.value}),
    action: async () => first.promise,
    invalidates: ["value"],
  });
  const secondRun = view.mutate({
    key: "value",
    arguments: {value: 2},
    project: (state, change) => ({...state, value: change.value}),
    action: async () => second.promise,
    invalidates: ["value"],
  });
  assert.equal(view.state().value, 2);

  const rejected = new Error("rejected");
  rejected.definitive = true;
  first.reject(rejected);
  await assert.rejects(firstRun, /rejected/);
  assert.equal(
    view.state().value,
    2,
    "removing the earlier intention must rebase the later one",
  );

  confirmed = {value: 2};
  second.resolve({
    status: "ok", revision: 2, invalidates: ["value"],
  });
  await secondRun;
  assert.equal(view.state().value, 2);
}

async function onlyDefinitiveHttpFailuresRollback() {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => ({
      ok: false,
      status: 500,
      json: async () => ({reason: "server failed"}),
    });
    await assert.rejects(
      globalThis.SovereignApi.request("/mutation", {}),
      (error) => error.definitive === false,
    );

    globalThis.fetch = async () => ({
      ok: false,
      status: 409,
      json: async () => ({status: "error", reason: "conflict"}),
    });
    await assert.rejects(
      globalThis.SovereignApi.request("/mutation", {}),
      (error) => error.definitive === true,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
}

async function timeoutNeverFlipsBack() {
  let confirmed = {value: 0};
  const observed = [];
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: confirmed.value}),
    timeoutMs: 5,
    retryDelays: [1],
    mutationStatus: async (mutationId) => {
      confirmed = {value: 1};
      return {
        status: "ok",
        mutation_id: mutationId,
        revision: 1,
        invalidates: ["value"],
      };
    },
  });
  view.defineView("value", {
    load: async () => ({...confirmed, revision: confirmed.value}),
  });
  view.subscribe((state) => observed.push(state.value));
  await view.refresh("value");

  await view.mutate({
    key: "value",
    arguments: {value: 1},
    project: (state, change) => ({...state, value: change.value}),
    action: ({signal}) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(
        new DOMException("timed out", "AbortError"),
      ));
    }),
    invalidates: ["value"],
  });

  const firstOptimistic = observed.indexOf(1);
  assert.notEqual(firstOptimistic, -1);
  assert.ok(
    observed.slice(firstOptimistic).every((value) => value === 1),
    `the optimistic value flipped back: ${observed.join(",")}`,
  );
}

async function independentKeysDoNotBlock() {
  const started = [];
  const left = deferred();
  const right = deferred();
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: 0}),
  });

  const one = view.mutate({
    key: "left",
    action: async () => {
      started.push("left");
      return left.promise;
    },
  });
  const two = view.mutate({
    key: "right",
    action: async () => {
      started.push("right");
      return right.promise;
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(started.sort(), ["left", "right"]);
  left.resolve({status: "ok", revision: 1});
  right.resolve({status: "ok", revision: 2});
  await Promise.all([one, two]);
}

async function pollingRepairsAViewBehindTheCurrentRevision() {
  let leftLoads = 0;
  let rightLoads = 0;
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: 2}),
  });
  view.defineView("left", {
    load: async () => {
      leftLoads += 1;
      return {left: true, revision: leftLoads === 1 ? 1 : 2};
    },
  });
  view.defineView("right", {
    load: async () => {
      rightLoads += 1;
      return {right: true, revision: 2};
    },
  });
  await view.refresh(["left", "right"]);
  view.startPolling({views: ["left", "right"], intervalMs: 2});
  await new Promise((resolve) => setTimeout(resolve, 12));
  view.stopPolling();

  assert.ok(leftLoads >= 2, "the lagging view was not refreshed");
  assert.equal(rightLoads, 1, "the current view was refreshed unnecessarily");
}

async function confirmationDoesNotRenderConfirmedAndTemporaryObjectsTogether() {
  let confirmed = {items: [], revision: 0};
  const observed = [];
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: confirmed.revision}),
  });
  view.defineView("items", {
    load: async () => confirmed,
  });
  view.subscribe((state) => {
    observed.push((state.items || []).map((item) => item.id));
  });
  await view.refresh("items");

  await view.mutate({
    key: "items",
    arguments: {item: {id: "temporary"}},
    project: (state, change) => ({
      ...state,
      items: [...(state.items || []), change.item],
    }),
    action: async () => {
      confirmed = {items: [{id: "confirmed"}], revision: 1};
      return {status: "ok", revision: 1, invalidates: ["items"]};
    },
    invalidates: ["items"],
  });

  assert.equal(
    observed.some((items) =>
      items.includes("temporary") && items.includes("confirmed")),
    false,
    `confirmation exposed both objects: ${JSON.stringify(observed)}`,
  );
  assert.deepEqual(observed.at(-1), ["confirmed"]);
}

async function confirmationNotifiesWhenRefreshedViewsReplacePendingState() {
  let confirmed = {
    tiles: {activeColumn: ""},
    context: {selectedTopic: "board"},
    revision: 0,
  };
  const changes = [];
  const view = globalThis.SovereignSessionView.create({
    request: async () => ({revision: confirmed.revision}),
  });
  view
    .defineView("tiles", {
      load: async () => ({
        ...confirmed.tiles,
        revision: confirmed.revision,
      }),
    })
    .defineView("context", {
      load: async () => ({
        ...confirmed.context,
        revision: confirmed.revision,
      }),
    });
  view.subscribe((_state, _pending, event) => changes.push(event.change));
  await view.refresh(["tiles", "context"]);
  changes.length = 0;

  await view.mutate({
    key: "board-settings",
    action: async () => {
      confirmed = {
        ...confirmed,
        tiles: {activeColumn: "doing"},
        revision: 1,
      };
      return {
        status: "ok",
        revision: 1,
        invalidates: ["tiles", "context"],
      };
    },
    invalidates: ["tiles", "context"],
  });

  assert.equal(view.state().activeColumn, "doing");
  assert.deepEqual(
    changes,
    ["pending", "state"],
    `refreshed views did not request a redraw: ${JSON.stringify(changes)}`,
  );
}

async function main() {
  await onlyDefinitiveHttpFailuresRollback();
  await sameKeyRebasesAfterEarlierRejection();
  await timeoutNeverFlipsBack();
  await independentKeysDoNotBlock();
  await pollingRepairsAViewBehindTheCurrentRevision();
  await confirmationDoesNotRenderConfirmedAndTemporaryObjectsTogether();
  await confirmationNotifiesWhenRefreshedViewsReplacePendingState();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
