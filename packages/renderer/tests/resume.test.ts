/**
 * Reducer behaviour around resume, revisions, and whole-plan runs.
 *
 * These cover the cases where a reconnecting client sees the same event twice,
 * or sees history out of order — situations the reducer has to survive without
 * showing the reader something that never happened.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  hydrateLoomState,
  initialLoomState,
  parsePlan,
  reduceLoomEvent,
} from "../src/state.ts";
import type { LoomEvent } from "../src/types.ts";

const PLAN_V1 = {
  goal: "Find the loci",
  summary: "",
  revision: 1,
  reason: null,
  objectives: [{ id: "q1", question: "Which loci associate with yield?" }],
  analysis_coverage: [
    { objective_id: "q1", status: "planned", reason: "scan queued", step_ids: ["scan"] },
  ],
  steps: [
    {
      id: "scan",
      title: "Association scan",
      kind: "capability",
      capability: "c.scan",
      depends_on: [],
      description: "",
      status: "pending",
      summary: null,
      execution: null,
      retry: { max_attempts: 3, backoff_seconds: 2 },
      timeout_seconds: 900,
      on_failure: "continue",
      attempts: 0,
    },
  ],
};

const PLAN_V2 = { ...PLAN_V1, revision: 2, reason: "added a covariate" };

function event(name: string, data: unknown, seq: number): LoomEvent {
  return { seq, event: name, data, ts: "2026-08-28T00:00:00.000Z" };
}

test("a replayed event is not applied twice", () => {
  const published = event("plan_published", { plan: PLAN_V1 }, 1);
  const updated = event(
    "step_updated",
    { revision: 1, step: { ...PLAN_V1.steps[0], status: "succeeded" } },
    2,
  );

  let state = reduceLoomEvent(initialLoomState, published);
  state = reduceLoomEvent(state, updated);
  const once = state;

  // An SSE reconnect replays the backlog the client already folded in.
  state = reduceLoomEvent(state, published);
  state = reduceLoomEvent(state, updated);

  assert.deepEqual(state, once);
  assert.equal(state.lastSeq, 2);
});

test("an out-of-order older revision does not become current", () => {
  let state = reduceLoomEvent(initialLoomState, event("plan_published", { plan: PLAN_V2 }, 1));
  assert.equal(state.currentPlan?.revision, 2);

  // History replay can deliver revision 1 after revision 2.
  state = reduceLoomEvent(state, event("plan_published", { plan: PLAN_V1 }, 2));

  assert.equal(state.currentPlan?.revision, 2, "the view jumped back a revision");
  assert.deepEqual(
    state.plans.map((item) => item.revision),
    [1, 2],
    "both revisions are still available to the switcher",
  );
});

test("step policy and objectives survive parsing", () => {
  const plan = parsePlan(PLAN_V1);
  assert.ok(plan);
  assert.equal(plan.steps[0].on_failure, "continue");
  assert.equal(plan.steps[0].timeout_seconds, 900);
  assert.equal(plan.steps[0].retry?.max_attempts, 3);
  assert.equal(plan.objectives?.length, 1);
  assert.equal(plan.analysis_coverage?.[0].status, "planned");
});

test("on_failure defaults to stop when absent", () => {
  const plan = parsePlan({
    ...PLAN_V1,
    steps: [{ ...PLAN_V1.steps[0], on_failure: undefined }],
  });
  assert.equal(plan?.steps[0].on_failure, "stop");
});

test("the new step statuses are accepted", () => {
  for (const status of ["ready", "waiting_approval", "cancelled"]) {
    const plan = parsePlan({ ...PLAN_V1, steps: [{ ...PLAN_V1.steps[0], status }] });
    assert.equal(plan?.steps[0].status, status, `${status} was coerced away`);
  }
});

test("an unknown status still falls back to pending", () => {
  const plan = parsePlan({
    ...PLAN_V1,
    steps: [{ ...PLAN_V1.steps[0], status: "invented" }],
  });
  assert.equal(plan?.steps[0].status, "pending");
});

test("a whole-plan execution hydrates with its per-node state", () => {
  const state = hydrateLoomState({
    plans: [PLAN_V1],
    executions: [
      {
        id: "run-1",
        kind: "plan",
        capability: "",
        status: "succeeded",
        revision: 1,
        artifacts: [],
        nodes: {
          scan: { node_id: "scan", status: "succeeded", attempts: 3, duration_seconds: 1.5 },
        },
      },
    ],
  });

  const execution = state.executions[0];
  assert.equal(execution.kind, "plan");
  assert.equal(execution.revision, 1);
  assert.equal(execution.nodes?.scan.status, "succeeded");
  assert.equal(execution.nodes?.scan.attempt, 3, "attempts should populate attempt");
});

test("a node map given as bare status strings still parses", () => {
  const state = hydrateLoomState({
    executions: [
      {
        id: "run-2",
        kind: "plan",
        capability: "",
        status: "running",
        artifacts: [],
        nodes: { qc: "succeeded", scan: "running" },
      },
    ],
  });
  assert.equal(state.executions[0].nodes?.qc.status, "succeeded");
  assert.equal(state.executions[0].nodes?.scan.status, "running");
});

test("hydrating then replaying the tail matches folding everything live", () => {
  const events: LoomEvent[] = [
    event("plan_published", { plan: PLAN_V1 }, 1),
    event(
      "step_updated",
      { revision: 1, step: { ...PLAN_V1.steps[0], status: "running", attempts: 1 } },
      2,
    ),
    event(
      "step_updated",
      { revision: 1, step: { ...PLAN_V1.steps[0], status: "succeeded", attempts: 3 } },
      3,
    ),
  ];

  const live = events.reduce(reduceLoomEvent, initialLoomState);
  const resumed = events
    .slice(1)
    .reduce(reduceLoomEvent, hydrateLoomState({ plans: [PLAN_V1], events: [] }));

  assert.equal(live.currentPlan?.steps[0].status, resumed.currentPlan?.steps[0].status);
  assert.equal(live.currentPlan?.steps[0].attempts, 3);
});
