import { useState } from "react";
import { LoomcraftWorkbench, initialState, reduceEvent, type LoomcraftEvent } from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

const demoPlan = {
  goal: "Profile sources and publish a validated report",
  revision: 2,
  reason: "The first pass found a transient quality service failure; retry policy was added.",
  steps: [
    { id: "source-a", title: "Profile source A", kind: "dynamic", depends_on: [], capability: null, status: "succeeded", summary: "120 rows", execution: null },
    { id: "source-b", title: "Profile source B", kind: "dynamic", depends_on: [], capability: null, status: "running", summary: null, execution: { attempts: 2 } },
    { id: "quality", title: "Quality gate", kind: "review", depends_on: ["source-a", "source-b"], capability: null, status: "pending", summary: null, execution: null },
    { id: "report", title: "Build report", kind: "dynamic", depends_on: ["quality"], capability: null, status: "pending", summary: null, execution: null },
  ],
};

export default function Demo() {
  const [state, setState] = useState(() => reduceEvent(initialState, { event: "plan_published", data: { plan: demoPlan } } as LoomcraftEvent));
  return <LoomcraftWorkbench state={state} />;
}
