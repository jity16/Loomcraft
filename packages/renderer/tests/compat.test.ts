import { strict as assert } from "node:assert";
import test from "node:test";
import { layoutPlan } from "../src/layout.ts";
import { renderPlanSvg } from "../src/svg.ts";
import { hydrateState, initialState, reduceEvent } from "../src/state.ts";
import type { Plan } from "../src/types.ts";

const plan: Plan = {
  goal: "demo",
  revision: 1,
  steps: [
    { id: "a", title: "A", kind: "dynamic", depends_on: [], capability: null, status: "succeeded", summary: null, execution: null },
    { id: "b", title: "B", kind: "dynamic", depends_on: [], capability: null, status: "running", summary: null, execution: null },
    { id: "join", title: "Join", kind: "review", depends_on: ["a", "b"], capability: null, status: "pending", summary: null, execution: null },
  ],
};

test("layout keeps independent nodes in one layer and dependencies below", () => {
  const graph = layoutPlan(plan);
  assert.equal(graph.nodes.find((node) => node.id === "a")?.layer, 0);
  assert.equal(graph.nodes.find((node) => node.id === "b")?.layer, 0);
  assert.equal(graph.nodes.find((node) => node.id === "join")?.layer, 1);
  assert.equal(graph.edges.length, 2);
});

test("reducer is idempotent for repeated plan and artifact events", () => {
  let state = reduceEvent(initialState, { event: "plan_published", data: { plan } });
  state = reduceEvent(state, { event: "plan_published", data: { plan } });
  assert.equal(state.plans.length, 1);
  state = reduceEvent(state, { event: "artifact_registered", data: { artifact: { id: "a1", filename: "out.json" } } });
  state = reduceEvent(state, { event: "artifact_registered", data: { artifact: { id: "a1", filename: "out.json" } } });
  assert.equal(state.artifacts.length, 1);
});

test("step and execution events project into the current plan", () => {
  let state = reduceEvent(initialState, { event: "plan_published", data: { plan } });
  state = reduceEvent(state, { event: "step_updated", data: { revision: 1, step: { id: "join", status: "succeeded", summary: "done" } } });
  assert.equal(state.currentPlan?.steps.find((step) => step.id === "join")?.status, "succeeded");
  state = reduceEvent(state, { event: "execution_finished", data: { step_id: "join", execution: { kind: "dag", id: "r1", status: "succeeded", artifacts: [] } } });
  assert.equal(state.executions.length, 1);
});

test("input lifecycle is replayable", () => {
  let state = reduceEvent(initialState, { event: "input_required", data: { request: { request_id: "input-1", title: "Need a file", requirements: [] } } });
  state = reduceEvent(state, { event: "input_fulfilled", data: { request_id: "input-1" } });
  assert.deepEqual(state.fulfilledInputRequestIds, ["input-1"]);
  state = reduceEvent(state, { event: "input_invalidated", data: { request_id: "input-1" } });
  assert.deepEqual(state.fulfilledInputRequestIds, []);
});

test("SVG export escapes user text", () => {
  const svg = renderPlanSvg({ ...plan, goal: "<unsafe>" });
  assert.match(svg, /&lt;unsafe&gt;/);
  assert.match(svg, /<svg/);
});

test("stream deltas collapse into one final assistant message", () => {
  let state = reduceEvent(initialState, { event: "message_delta", data: { item_id: "m1", delta: "hel" } });
  state = reduceEvent(state, { event: "message_delta", data: { item_id: "m1", delta: "lo" } });
  state = reduceEvent(state, { event: "message", data: { item_id: "m1", text: "hello" } });
  assert.equal(state.timeline.length, 1);
  assert.equal(state.timeline[0].kind === "assistant" ? state.timeline[0].text : "", "hello");
});

test("sequence cursors suppress replayed events", () => {
  let state = reduceEvent(initialState, { seq: 1, event: "notice", data: { message: "one" } });
  state = reduceEvent(state, { seq: 1, event: "notice", data: { message: "duplicate" } });
  assert.equal(state.timeline.length, 1);
  assert.equal(state.lastSeq, 1);
});

test("attempt events expose retry progress on the node", () => {
  let state = reduceEvent(initialState, { event: "plan_published", data: { plan } });
  state = reduceEvent(state, { event: "step_attempt", data: { step_id: "join", attempt: 2 } });
  assert.equal(state.currentPlan?.steps.find((step) => step.id === "join")?.attempts, 2);
});

test("late events for an older revision do not regress current revision", () => {
  let state = reduceEvent(initialState, { event: "plan_published", data: { plan } });
  state = reduceEvent(state, { event: "plan_published", data: { plan: { ...plan, revision: 2, reason: "replan" } } });
  state = reduceEvent(state, { event: "step_updated", data: { revision: 1, step: { id: "a", status: "failed" } } });
  assert.equal(state.currentPlan?.revision, 2);
  assert.equal(state.plans.find((item) => item.revision === 1)?.steps.find((step) => step.id === "a")?.status, "failed");
});

test("hydration accepts the original wrapped plan history shape", () => {
  const state = hydrateState({
    plans: [{ plan: { ...plan, revision: 3 }, events: [{ event: "step_updated", data: { revision: 3, step: { id: "a", status: "failed" } } }] }],
  });
  assert.equal(state.currentPlan?.revision, 3);
  assert.equal(state.currentPlan?.steps.find((step) => step.id === "a")?.status, "failed");
});
