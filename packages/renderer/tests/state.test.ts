/**
 * Reducer tests — run with `node --test --experimental-strip-types`.
 *
 * The reducer is the piece both the live stream and the reload path depend on,
 * so these lean on equivalence ("live and replayed states agree") as much as on
 * individual transitions.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendUserMessage,
  deriveTaskPhase,
  hydrateLoomState,
  initialLoomState,
  orientationActivities,
  parsePlan,
  pendingInputRequests,
  planProgress,
  readySteps,
  reduceLoomEvent,
} from "../src/state.ts";
import type { LoomEvent, LoomState } from "../src/types.ts";

const PLAN = {
  goal: "Analyse the uploaded table",
  summary: "",
  revision: 1,
  reason: null,
  steps: [
    { id: "load", title: "Load", kind: "capability", depends_on: [], capability: "csv.load", description: "", status: "pending", summary: null, execution: null },
    { id: "stats", title: "Stats", kind: "capability", depends_on: ["load"], capability: "csv.stats", description: "", status: "pending", summary: null, execution: null },
    { id: "chart", title: "Chart", kind: "capability", depends_on: ["load"], capability: "csv.chart", description: "", status: "pending", summary: null, execution: null },
    { id: "answer", title: "Answer", kind: "answer", depends_on: ["stats", "chart"], capability: null, description: "", status: "pending", summary: null, execution: null },
  ],
};

function fold(events: LoomEvent[], from: LoomState = initialLoomState): LoomState {
  return events.reduce(reduceLoomEvent, from);
}

const published: LoomEvent = { seq: 1, event: "plan_published", data: { plan: PLAN } };

test("parsePlan accepts a well-formed plan", () => {
  const plan = parsePlan(PLAN);
  assert.ok(plan);
  assert.equal(plan.steps.length, 4);
  assert.equal(plan.revision, 1);
});

test("parsePlan rejects an unknown step kind", () => {
  const bad = { ...PLAN, steps: [{ ...PLAN.steps[0], kind: "teleport" }] };
  assert.equal(parsePlan(bad), null);
});

test("parsePlan rejects a payload with no steps", () => {
  assert.equal(parsePlan({ ...PLAN, steps: [] }), null);
});

test("parsePlan defaults an unknown status to pending", () => {
  const plan = parsePlan({ ...PLAN, steps: [{ ...PLAN.steps[0], status: "vibes" }] });
  assert.equal(plan?.steps[0].status, "pending");
});

test("plan_published stores the current plan and the revision list", () => {
  const state = fold([published]);
  assert.equal(state.currentPlan?.revision, 1);
  assert.equal(state.plans.length, 1);
});

test("a new revision is appended and becomes current", () => {
  const revised = { ...PLAN, revision: 2, reason: "the first source was wrong" };
  const state = fold([published, { seq: 2, event: "plan_published", data: { plan: revised } }]);
  assert.equal(state.currentPlan?.revision, 2);
  assert.deepEqual(state.plans.map((plan) => plan.revision), [1, 2]);
  assert.equal(state.currentPlan?.reason, "the first source was wrong");
});

test("step_updated mutates only the addressed step", () => {
  const state = fold([
    published,
    {
      seq: 2,
      event: "step_updated",
      data: { revision: 1, step: { id: "load", status: "succeeded", summary: "12 rows" } },
    },
  ]);
  const byId = new Map(state.currentPlan!.steps.map((step) => [step.id, step]));
  assert.equal(byId.get("load")?.status, "succeeded");
  assert.equal(byId.get("load")?.summary, "12 rows");
  assert.equal(byId.get("stats")?.status, "pending");
});

test("step_updated for an unknown revision is ignored", () => {
  const state = fold([
    published,
    { seq: 2, event: "step_updated", data: { revision: 1, step: { status: "succeeded" } } },
  ]);
  assert.equal(state.currentPlan?.steps[0].status, "pending");
});

test("execution progress is tracked per node", () => {
  const state = fold([
    published,
    {
      seq: 2,
      event: "execution_started",
      data: { execution_kind: "capability", execution_id: "run-1", capability: "csv.load", step_id: "load" },
    },
    {
      seq: 3,
      event: "execution_progress",
      data: { execution_id: "run-1", node_id: "execute", status: "running", attempt: 2, max_attempts: 3 },
    },
  ]);
  const execution = state.executions[0];
  assert.equal(execution.id, "run-1");
  assert.equal(execution.nodes?.execute.attempt, 2);
  assert.equal(execution.nodes?.execute.max_attempts, 3);
});

test("execution_finished merges its artifacts into the session list", () => {
  const state = fold([
    published,
    {
      seq: 2,
      event: "execution_finished",
      data: {
        step_id: "load",
        execution: {
          id: "run-1",
          kind: "capability",
          capability: "csv.load",
          status: "succeeded",
          artifacts: [{ id: "art-1", filename: "loaded.csv", size: 120 }],
        },
      },
    },
  ]);
  assert.equal(state.artifacts.length, 1);
  assert.equal(state.artifacts[0].filename, "loaded.csv");
});

test("persisted execution nodes hydrate their final attempts and status", () => {
  const state = hydrateLoomState({
    executions: [
      {
        id: "run-1",
        kind: "plan",
        status: "succeeded",
        artifacts: [],
        nodes: {
          profile: {
            node_id: "profile",
            status: "succeeded",
            attempts: 3,
            duration_seconds: 0.4,
          },
        },
      },
    ],
  });
  assert.equal(state.executions[0].nodes?.profile.status, "succeeded");
  assert.equal(state.executions[0].nodes?.profile.attempt, 3);
});

test("compatibility histories can hydrate transcript messages and string node maps", () => {
  const state = hydrateLoomState({
    messages: [
      { role: "user", text: "Inspect this" },
      { role: "assistant", text: "I will inspect it" },
    ],
    executions: [
      {
        id: "run-2",
        kind: "plan",
        status: "running",
        artifacts: [],
        nodes: { load: "succeeded" },
      },
    ],
  });
  assert.equal(state.timeline.filter((item) => item.kind === "user").length, 1);
  assert.equal(state.timeline.filter((item) => item.kind === "assistant").length, 1);
  assert.equal(state.executions[0].nodes?.load.status, "succeeded");
});

test("duplicate artifact ids are not double-counted", () => {
  const artifact = { artifact: { id: "art-1", filename: "a.csv", size: 10 } };
  const state = fold([
    { seq: 1, event: "artifact_registered", data: artifact },
    { seq: 2, event: "artifact_registered", data: artifact },
  ]);
  assert.equal(state.artifacts.length, 1);
});

test("streamed deltas coalesce into one assistant message", () => {
  const state = fold([
    { seq: 1, event: "message_delta", data: { item_id: "m1", delta: "Hel" } },
    { seq: 2, event: "message_delta", data: { item_id: "m1", delta: "lo" } },
  ]);
  const assistant = state.timeline.filter((item) => item.kind === "assistant");
  assert.equal(assistant.length, 1);
  assert.equal(assistant[0].kind === "assistant" && assistant[0].text, "Hello");
});

test("a final message replaces its own streamed placeholder", () => {
  const state = fold([
    { seq: 1, event: "message_delta", data: { item_id: "m1", delta: "Partial" } },
    { seq: 2, event: "message", data: { item_id: "m1", text: "Partial answer, completed." } },
  ]);
  const assistant = state.timeline.filter((item) => item.kind === "assistant");
  assert.equal(assistant.length, 1, "the completed message must not duplicate the stream");
  assert.equal(
    assistant[0].kind === "assistant" && assistant[0].text,
    "Partial answer, completed.",
  );
});

test("tool_call then tool_result closes out one timeline entry", () => {
  const state = fold([
    { seq: 1, event: "tool_call", data: { item_id: "t1", tool: "publish_plan" } },
    { seq: 2, event: "tool_result", data: { item_id: "t1", ok: true } },
  ]);
  const tools = state.timeline.filter((item) => item.kind === "tool");
  assert.equal(tools.length, 1);
  assert.equal(tools[0].kind === "tool" && tools[0].status, "done");
  assert.equal(tools[0].kind === "tool" && tools[0].ok, true);
});

test("a failed tool result carries its error code", () => {
  const state = fold([
    { seq: 1, event: "tool_call", data: { item_id: "t1", tool: "run_capability" } },
    {
      seq: 2,
      event: "tool_result",
      data: { item_id: "t1", ok: false, error: "nope", error_code: "PLAN_INVALID" },
    },
  ]);
  const tool = state.timeline.find((item) => item.kind === "tool");
  assert.equal(tool?.kind === "tool" && tool.errorCode, "PLAN_INVALID");
});

test("input request lifecycle: required, fulfilled, invalidated", () => {
  const request = {
    request: {
      request_id: "input-0123456789abcdef",
      title: "Need a table",
      message: "Upload a CSV",
      requirements: [
        {
          key: "table",
          label: "Table",
          description: "A CSV",
          required: true,
          min_files: 1,
          max_files: 1,
          allowed_extensions: [".csv"],
          field_hints: [],
        },
      ],
      continue_prompt: "continue",
    },
  };
  let state = fold([{ seq: 1, event: "input_required", data: request }]);
  assert.equal(pendingInputRequests(state).length, 1);

  state = reduceLoomEvent(state, {
    seq: 2,
    event: "input_fulfilled",
    data: { request_id: "input-0123456789abcdef" },
  });
  assert.equal(pendingInputRequests(state).length, 0);

  // Deleting a file that satisfied the request must re-open it.
  state = reduceLoomEvent(state, {
    seq: 3,
    event: "input_invalidated",
    data: { request_id: "input-0123456789abcdef" },
  });
  assert.equal(pendingInputRequests(state).length, 1);
});

test("approval events track and then clear pending nodes", () => {
  let state = fold([
    { seq: 1, event: "approval_required", data: { execution_id: "run-1", nodes: ["gate"] } },
  ]);
  assert.deepEqual(state.pendingApprovals["run-1"], ["gate"]);

  state = reduceLoomEvent(state, {
    seq: 2,
    event: "approval_resolved",
    data: { execution_id: "run-1", node_id: "gate", approved: true },
  });
  assert.equal(state.pendingApprovals["run-1"], undefined);
  const approval = state.timeline.find((item) => item.kind === "approval");
  assert.equal(approval?.kind === "approval" && approval.resolved?.approved, true);
});

test("unknown events are ignored rather than throwing", () => {
  const before = fold([published]);
  const after = reduceLoomEvent(before, {
    seq: 2,
    event: "quantum_entanglement_established",
    data: { spooky: true },
  });
  assert.equal(after.currentPlan?.revision, 1);
  assert.equal(after.timeline.length, before.timeline.length);
});

test("malformed payloads never throw", () => {
  for (const data of [null, undefined, 42, "string", [], { plan: "not-a-plan" }]) {
    assert.doesNotThrow(() =>
      reduceLoomEvent(initialLoomState, { event: "plan_published", data }),
    );
  }
});

test("lastSeq advances monotonically for resume", () => {
  const state = fold([
    { seq: 1, event: "notice", data: { message: "a" } },
    { seq: 5, event: "notice", data: { message: "b" } },
    { seq: 3, event: "notice", data: { message: "out of order" } },
  ]);
  assert.equal(state.lastSeq, 5);
});

test("hydrating a history matches folding the same events live", () => {
  const events = [
    published,
    {
      seq: 2,
      event: "step_updated",
      data: { revision: 1, step: { id: "load", status: "succeeded", summary: "done" } },
    },
    { seq: 3, event: "artifact_registered", data: { artifact: { id: "art-1", filename: "a.csv", size: 5 } } },
  ];
  const live = fold(events);
  const hydrated = hydrateLoomState({
    plans: [PLAN],
    current_plan: live.currentPlan,
    events: events.map((event) => ({ ...event })),
    artifacts: [{ id: "art-1", filename: "a.csv", size: 5 }],
    uploads: [],
    executions: [],
  });

  assert.deepEqual(hydrated.currentPlan, live.currentPlan);
  assert.deepEqual(
    hydrated.artifacts.map((item) => item.id),
    live.artifacts.map((item) => item.id),
  );
});

test("hydration parses uploads", () => {
  const state = hydrateLoomState({
    uploads: [{ id: "u1", filename: "a.csv", size: 10, source_ref: "upload:u1" }],
  });
  assert.equal(state.uploads.length, 1);
  assert.equal(state.uploads[0].source_ref, "upload:u1");
});

test("task phase follows plan state", () => {
  assert.equal(deriveTaskPhase(initialLoomState, false), "idle");
  assert.equal(deriveTaskPhase(initialLoomState, true), "orienting");

  const planned = fold([published]);
  assert.equal(deriveTaskPhase(planned, false), "planned");

  const running = reduceLoomEvent(planned, {
    seq: 2,
    event: "step_updated",
    data: { revision: 1, step: { id: "load", status: "running" } },
  });
  assert.equal(deriveTaskPhase(running, true), "executing");

  let complete = planned;
  for (const id of ["load", "stats", "chart", "answer"]) {
    complete = reduceLoomEvent(complete, {
      event: "step_updated",
      data: { revision: 1, step: { id, status: "succeeded" } },
    });
  }
  assert.equal(deriveTaskPhase(complete, false), "completed");
});

test("orientation groups pre-plan tool calls by intent", () => {
  const state = fold([
    { seq: 1, event: "tool_call", data: { item_id: "t1", tool: "session_context" } },
    { seq: 2, event: "tool_result", data: { item_id: "t1", ok: true } },
    { seq: 3, event: "tool_call", data: { item_id: "t2", tool: "capability_search" } },
    { seq: 4, event: "tool_result", data: { item_id: "t2", ok: true } },
    { seq: 5, event: "tool_call", data: { item_id: "t3", tool: "capability_search" } },
    { seq: 6, event: "tool_result", data: { item_id: "t3", ok: true } },
  ]);
  const activities = orientationActivities(state.timeline);
  const search = activities.find((item) => item.label.includes("capabilities"));
  assert.equal(search?.count, 2, "repeated searches collapse into one row");
});

test("orientation excludes tool calls bound to a step", () => {
  const state = fold([
    { seq: 1, event: "tool_call", data: { item_id: "t1", tool: "run_capability", step_id: "load" } },
    { seq: 2, event: "tool_result", data: { item_id: "t1", ok: true, step_id: "load" } },
  ]);
  assert.equal(orientationActivities(state.timeline).length, 0);
});

test("planProgress counts every status", () => {
  let state = fold([published]);
  state = reduceLoomEvent(state, {
    event: "step_updated",
    data: { revision: 1, step: { id: "load", status: "succeeded" } },
  });
  state = reduceLoomEvent(state, {
    event: "step_updated",
    data: { revision: 1, step: { id: "stats", status: "running" } },
  });
  const progress = planProgress(state.currentPlan);
  assert.equal(progress.total, 4);
  assert.equal(progress.succeeded, 1);
  assert.equal(progress.running, 1);
  assert.equal(progress.pending, 2);
  assert.equal(progress.fraction, 0.25);
});

test("readySteps reflects the executable frontier", () => {
  let state = fold([published]);
  assert.deepEqual(readySteps(state.currentPlan).map((step) => step.id), ["load"]);

  state = reduceLoomEvent(state, {
    event: "step_updated",
    data: { revision: 1, step: { id: "load", status: "succeeded" } },
  });
  assert.deepEqual(
    readySteps(state.currentPlan).map((step) => step.id).sort(),
    ["chart", "stats"],
  );
});

test("readySteps honors an upstream continue policy", () => {
  const plan = parsePlan({
    goal: "continue",
    revision: 1,
    steps: [
      { id: "failed", title: "Failed", kind: "dynamic", status: "failed", on_failure: "continue" },
      { id: "next", title: "Next", kind: "answer", status: "pending", depends_on: ["failed"] },
    ],
  });
  assert.deepEqual(readySteps(plan).map((step) => step.id), ["next"]);
});

test("appendUserMessage clears a prior terminal state", () => {
  const errored = reduceLoomEvent(initialLoomState, {
    event: "error",
    data: { message: "previous failure" },
  });
  const next = appendUserMessage(errored, "try again please");
  assert.equal(next.error, null);
  assert.equal(next.done, false);
  assert.equal(next.timeline.at(-1)?.kind, "user");
});

test("the reducer never mutates its input", () => {
  const before = fold([published]);
  const snapshot = JSON.stringify(before);
  reduceLoomEvent(before, {
    event: "step_updated",
    data: { revision: 1, step: { id: "load", status: "succeeded" } },
  });
  assert.equal(JSON.stringify(before), snapshot);
});
