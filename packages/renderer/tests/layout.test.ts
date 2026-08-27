/** Layout tests: layering matches execution semantics, output is deterministic. */

import assert from "node:assert/strict";
import { test } from "node:test";

import { assignLayers, countCrossings, fitToViewport, layoutPlan } from "../src/layout.ts";
import type { Plan, PlanStep, StepKind } from "../src/types.ts";

function step(id: string, dependsOn: string[] = [], kind: StepKind = "dynamic"): PlanStep {
  return {
    id,
    title: id.toUpperCase(),
    kind,
    depends_on: dependsOn,
    capability: null,
    description: "",
    status: "pending",
    summary: null,
    execution: null,
  };
}

function plan(steps: PlanStep[]): Plan {
  return { goal: "goal", summary: "", revision: 1, reason: null, steps };
}

const DIAMOND = plan([
  step("a"),
  step("b", ["a"]),
  step("c", ["a"]),
  step("d", ["b", "c"]),
]);

test("layering places each node below its deepest dependency", () => {
  const layers = assignLayers(DIAMOND.steps);
  assert.deepEqual(layers, [["a"], ["b", "c"], ["d"]]);
});

test("a layer is exactly what the engine may run in parallel", () => {
  // Independent siblings share a row; anything with a path between them cannot.
  const layers = assignLayers(DIAMOND.steps);
  assert.ok(layers[1].includes("b") && layers[1].includes("c"));
  assert.ok(!layers.some((layer) => layer.includes("a") && layer.includes("b")));
});

test("a skip edge lands the target below the deeper path", () => {
  const layers = assignLayers([
    step("a"),
    step("b", ["a"]),
    step("c", ["a", "b"]),
  ]);
  assert.deepEqual(layers, [["a"], ["b"], ["c"]]);
});

test("disconnected roots share the first layer", () => {
  const layers = assignLayers([step("a"), step("b"), step("c", ["a"])]);
  assert.deepEqual(layers[0], ["a", "b"]);
});

test("layout produces a node box per step and an edge per dependency", () => {
  const layout = layoutPlan(DIAMOND);
  assert.equal(layout.nodes.length, 4);
  assert.equal(layout.edges.length, 4);
  assert.ok(layout.width > 0 && layout.height > 0);
});

test("layout is deterministic across runs", () => {
  const first = layoutPlan(DIAMOND);
  const second = layoutPlan(DIAMOND);
  assert.deepEqual(
    first.nodes.map((node) => [node.id, node.x, node.y]),
    second.nodes.map((node) => [node.id, node.x, node.y]),
  );
});

test("dependencies are always drawn downward in vertical mode", () => {
  const layout = layoutPlan(DIAMOND);
  const byId = new Map(layout.nodes.map((node) => [node.id, node]));
  for (const edge of layout.edges) {
    assert.ok(
      byId.get(edge.source)!.y < byId.get(edge.target)!.y,
      `${edge.id} should point downward`,
    );
  }
});

test("horizontal mode flows left to right", () => {
  const layout = layoutPlan(DIAMOND, { direction: "horizontal" });
  const byId = new Map(layout.nodes.map((node) => [node.id, node]));
  for (const edge of layout.edges) {
    assert.ok(byId.get(edge.source)!.x < byId.get(edge.target)!.x);
  }
});

test("long edges are flagged for dashed rendering", () => {
  const layout = layoutPlan(plan([step("a"), step("b", ["a"]), step("c", ["a", "b"])]));
  const skip = layout.edges.find((edge) => edge.source === "a" && edge.target === "c");
  assert.equal(skip?.long, true);
});

test("nodes never overlap within a layer", () => {
  const wide = plan([step("root"), ...Array.from({ length: 8 }, (_, i) => step(`n${i}`, ["root"]))]);
  const layout = layoutPlan(wide);
  const row = layout.nodes.filter((node) => node.layer === 1).sort((a, b) => a.x - b.x);
  for (let index = 1; index < row.length; index += 1) {
    assert.ok(
      row[index].x >= row[index - 1].x + row[index - 1].width,
      "sibling nodes must not overlap",
    );
  }
});

test("crossing reduction improves on the adversarial ordering", () => {
  // Two parents feeding two children in reversed declaration order.
  const crossed = plan([
    step("p1"),
    step("p2"),
    step("c1", ["p2"]),
    step("c2", ["p1"]),
  ]);
  const layout = layoutPlan(crossed);
  assert.equal(countCrossings(layout), 0, "barycenter sweeps should untangle this");
});

test("an empty plan produces an empty layout", () => {
  const layout = layoutPlan(null);
  assert.deepEqual(layout.nodes, []);
  assert.equal(layout.width, 0);
});

test("a cyclic payload degrades instead of hanging", () => {
  // The server rejects cycles, but the renderer must not lock up if one arrives.
  const cyclic = plan([step("a", ["b"]), step("b", ["a"])]);
  const layout = layoutPlan(cyclic);
  assert.equal(layout.nodes.length, 2);
});

test("fitToViewport centres and never exceeds maxZoom", () => {
  const layout = layoutPlan(DIAMOND);
  const fit = fitToViewport(layout, { width: 4000, height: 4000 }, { maxZoom: 1 });
  assert.equal(fit.scale, 1);
  assert.ok(fit.translateX > 0 && fit.translateY > 0);
});

test("fitToViewport scales a large graph down", () => {
  const layout = layoutPlan(DIAMOND);
  const fit = fitToViewport(layout, { width: 200, height: 150 });
  assert.ok(fit.scale < 1);
});

test("fitToViewport tolerates a zero-sized viewport", () => {
  const fit = fitToViewport(layoutPlan(DIAMOND), { width: 0, height: 0 });
  assert.equal(fit.scale, 1);
});
