# Frontend integration

Rendering a live plan: the reducer, the transport, the components, and how to
build your own UI on the same protocol.

- [Three layers](#three-layers)
- [Fastest path](#fastest-path)
- [The hook](#the-hook)
- [Components](#components)
- [The reducer](#the-reducer)
- [The client](#the-client)
- [Layout](#layout)
- [Theming](#theming)
- [Accessibility](#accessibility)
- [Building a custom UI](#building-a-custom-ui)
- [Non-React frameworks](#non-react-frameworks)
- [The protocol](#the-protocol)

---

## Three layers

```
┌─────────────────────────────────────────────────────────┐
│  LoomWorkbench          a complete drop-in task UI      │
├─────────────────────────────────────────────────────────┤
│  PlanGraph, panels      individual components           │
│  useLoomSession         state + transport + actions     │
├─────────────────────────────────────────────────────────┤
│  reduceLoomEvent        pure state, framework-agnostic  │
│  layoutPlan             pure layout, zero dependencies  │
│  LoomClient             HTTP + SSE                      │
└─────────────────────────────────────────────────────────┘
```

Take as many layers as you want. The bottom one has no React dependency at all,
so a Vue or Svelte host still gets the reducer and the layout.

```bash
# Not on npm, and npm cannot install from a git subdirectory — build once,
# then install by path.
git clone https://github.com/jity16/Loomcraft.git
cd Loomcraft/packages/renderer && npm install && npm run build
cd /your/app && npm install /path/to/Loomcraft/packages/renderer
```

The only peer dependency is React. The DAG layout, the pan/zoom canvas, and the
SSE reader are first-party — adding LoomCraft does not pull in a chart library or
a graph-layout engine.

---

## Fastest path

```tsx
import { LoomWorkbench } from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

export function TaskPage({ sessionId }: { sessionId: string }) {
  return <LoomWorkbench sessionId={sessionId} baseUrl="/api/v1/loomcraft" />;
}
```

That gives you the plan graph, revision switcher, progress bar, conversation
timeline, execution list, artifact downloads, file-request panels, approval
prompts, and a composer.

---

## The hook

`useLoomSession` owns state, the SSE subscription, and the actions:

```tsx
import { useLoomSession } from "@loomcraft/renderer";

const session = useLoomSession({
  sessionId,
  baseUrl: "/api/v1/loomcraft",
  headers: { Authorization: `Bearer ${token}` },
  hydrate: true,            // load /history on mount
  reattachOnDetach: true,   // rejoin a turn whose stream dropped
});
```

| Field | What it is |
| --- | --- |
| `state` | The full reducer state |
| `busy` | A turn is streaming |
| `phase` | `idle` / `orienting` / `planned` / `executing` / `completed` |
| `visiblePlan` | The revision being viewed (live unless the user picked another) |
| `selectRevision`, `selectStep` | View selection |
| `orientation` | Grouped pre-plan activity |
| `pendingRequests` | Unresolved file requests |
| `send`, `cancel`, `upload`, `deleteUpload` | Turn and file actions |
| `fulfillRequest`, `cancelRequest`, `approve`, `download` | Interrupt handling |
| `refresh` | Re-hydrate from `/history` |

### Reconnect behaviour

If a turn's stream drops — tab slept, wifi blipped, proxy timed out — **the turn
keeps running on the server**. The hook re-hydrates and re-attaches from
`lastSeq`, so the user sees the run continue rather than a stalled UI or a
duplicated task.

This is the single most important thing to preserve if you write your own
transport. Tying the work's lifetime to the HTTP connection is how long-running
agent tasks die for no reason.

---

## Components

Every piece the workbench composes is exported on its own.

### `PlanGraph`

```tsx
<PlanGraph
  plan={session.visiblePlan}
  plans={session.state.plans}
  selectedStepId={session.selectedStepId}
  onSelectStep={session.selectStep}
  onSelectRevision={(revision) => session.selectRevision(revision)}
  layout={{ direction: "horizontal", nodeWidth: 200, rowGap: 48 }}
  controls
/>
```

Pan by dragging, zoom with the wheel (or ⌘/Ctrl + wheel), fit with the control.
The view auto-fits when the graph changes shape — but never after the user has
panned or zoomed deliberately.

### The panels

| Component | Renders |
| --- | --- |
| `PlanProgress` | A progress bar plus per-status counts |
| `StepDetail` | Detail card for a selected node |
| `Timeline` | Conversation, tool calls, executions, interrupts |
| `ArtifactList` | Deliverables with download buttons |
| `InputRequestPanel` | Typed file slots with hints, upload, confirm/skip |
| `ApprovalPanel` | Approve/reject for parked nodes |
| `ExecutionList` | Runs with per-node status and attempt counts |
| `OrientationPanel` | What the agent is doing before a plan exists |

---

## The reducer

`reduceLoomEvent(state, event)` is a pure function. `hydrateLoomState(history)`
replays a persisted snapshot **through the same function** — which is why a live
stream and a mid-run refresh cannot disagree.

```ts
import { initialLoomState, reduceLoomEvent, hydrateLoomState } from "@loomcraft/renderer";

let state = initialLoomState;
state = reduceLoomEvent(state, { event: "plan_published", data: { plan } });

const restored = hydrateLoomState(await client.getHistory(sessionId));
```

### State shape

```ts
interface LoomState {
  plans: Plan[];                        // every revision, ascending
  currentPlan: Plan | null;
  timeline: TimelineItem[];
  executions: Execution[];              // with per-node progress
  artifacts: Artifact[];
  uploads: Upload[];
  fulfilledInputRequestIds: string[];
  cancelledInputRequestIds: string[];
  pendingApprovals: Record<string, string[]>;
  lastSeq: number;                      // resume cursor
  error: string | null;
  done: boolean;
}
```

### Defensive by design

The reducer never throws and never blanks the UI:

- Unknown event names are ignored, so a server one version ahead is safe.
- Malformed payloads are coerced or dropped, not propagated.
- Streamed deltas coalesce into one message, and the final `message` replaces its
  own placeholder rather than duplicating the reply.
- Duplicate artifact ids are not double-counted.
- `lastSeq` only advances, so out-of-order frames cannot rewind the cursor.

### Derived selectors

```ts
deriveTaskPhase(state, busy);        // idle|orienting|planned|executing|completed
planProgress(state.currentPlan);     // counts + fraction
readySteps(state.currentPlan);       // the executable frontier
orientationActivities(state.timeline);
pendingInputRequests(state);
```

---

## The client

```ts
import { LoomClient } from "@loomcraft/renderer";

const client = new LoomClient({
  baseUrl: "/api/v1/loomcraft",
  headers: { Authorization: `Bearer ${token}` },
});

const session = await client.createSession();
await client.uploadFile(session.session_id, file);

const outcome = await client.runTurn(session.session_id, "Analyse it.", (event) => {
  state = reduceLoomEvent(state, event);
  render();
});
```

`runTurn` distinguishes three outcomes, and the distinction matters:

| Outcome | Meaning | What to do |
| --- | --- | --- |
| `{state: "terminal", ok}` | The server sent `done` | Show the result |
| `{state: "detached"}` | The stream ended without `done` | **The turn is still running** — re-attach |
| `{state: "aborted"}` | The caller cancelled | Nothing |

```ts
if (outcome.state === "detached") {
  await client.streamEvents(sessionId, apply, { afterSeq: state.lastSeq });
}
```

---

## Layout

`layoutPlan` is a deterministic layered layout with no dependencies:

```ts
import { layoutPlan, fitToViewport, assignLayers } from "@loomcraft/renderer";

const layout = layoutPlan(plan, {
  direction: "vertical",   // or "horizontal"
  nodeWidth: 224,
  nodeHeight: 92,
  columnGap: 32,
  rowGap: 64,
  sweeps: 6,               // barycenter passes for crossing reduction
});
```

Three passes: longest-path layering, barycenter crossing reduction, then
positioning. Ties break on node id, so the same plan always draws identically —
a layout that jitters between renders reads as motion the user has to re-parse.

**A layer is exactly what the engine will run in parallel.** The picture and the
execution model agree by construction, because both come from the same longest-
path assignment.

```ts
assignLayers(plan.steps);   // [["clean"], ["profile", "outliers"], ["report"]]
```

---

## Theming

### The default theme, and why it looks like that

Warm paper for the chrome, cool grey for the DAG pane, one green for identity
and one blue that means *running* and nothing else:

```
--lc-canvas       #fdfcf8   paper — the page behind the chrome
--lc-surface      #ffffff   cards, nodes, panels
--lc-sunken       #f7f4ec   inset wells, the glyph chip on a node
--lc-line         #e8e4d9   default rule

--lc-graph-canvas #f7f8fa   the DAG pane — cool, so it reads as inset
--lc-graph-dot    #d8dce4   the 20px dot grid

--lc-accent       #4a7d5b   identity, selection, replan notices
--lc-run          #1661ab   in flight — this hue means nothing else
--lc-ok           #6b7a3a
--lc-err          #c03030
```

Keeping "brand" and "in flight" on different hues is what lets a glance at a
crowded canvas answer *is anything moving right now?* without reading a word.
The dot grid and the cool pane are load-bearing too: they mark where the
zoomable surface starts, so a half-scrolled graph never looks like a broken
page.

### Overriding it

Every colour is a custom property. Override on any ancestor:

```css
.my-app .lc-workbench {
  --lc-accent: #7c3aed;
  --lc-ok: #059669;
  --lc-run: #0284c7;
  --lc-err: #dc2626;
  --lc-canvas: #ffffff;
  --lc-graph-canvas: var(--lc-canvas);   /* flush instead of inset */
  --lc-edge-active: var(--lc-run);       /* one blue instead of two */
  --lc-radius: 12px;
  --lc-font: "Inter", system-ui, sans-serif;
  --lc-mono: "JetBrains Mono", ui-monospace, monospace;
}
```

Node fills are not tokens — each is `color-mix` of its status colour at 2.5–3.5%
over `--lc-surface`, so retinting `--lc-run` retints the running node with it and
you never have to keep a fill and a border in sync by hand.

Dark mode follows `prefers-color-scheme` and can be forced with
`data-lc-theme="dark"` on any ancestor.

Class names are stable and prefixed `lc-`: `lc-node`, `lc-node--running`,
`lc-edge--active`, `lc-timeline__item--tool`, and so on. Restyle them directly if
custom properties are not enough.

---

## Accessibility

- Each graph node is a keyboard-focusable native `<button>` with an accessible
  label containing its kind, status, and dependencies; decorative edges are
  hidden from assistive technology.
- The progress bar is a real `role="progressbar"` with correct value attributes.
- Running indicators and animated edges respect `prefers-reduced-motion`.
- Every control is a real `<button>` with an accessible name.
- Errors use `role="alert"`.

If you build custom node rendering, keep an equivalent keyboard-accessible list
or table of steps and dependencies.

---

## Building a custom UI

The reducer and the layout are the reusable parts; the components are one
opinion. A minimal custom renderer:

```tsx
import { useEffect, useReducer } from "react";
import { LoomClient, initialLoomState, reduceLoomEvent, layoutPlan } from "@loomcraft/renderer";

function MyPlanView({ sessionId }: { sessionId: string }) {
  const [state, dispatch] = useReducer(reduceLoomEvent, initialLoomState);
  const client = new LoomClient({ baseUrl: "/api/v1/loomcraft" });

  useEffect(() => {
    const controller = new AbortController();
    void client.streamEvents(sessionId, dispatch, { signal: controller.signal });
    return () => controller.abort();
  }, [sessionId]);

  const layout = layoutPlan(state.currentPlan);

  return (
    <svg width={layout.width} height={layout.height}>
      {layout.edges.map((edge) => (
        <path key={edge.id} d={edge.path} stroke="#ccc" fill="none" />
      ))}
      {layout.nodes.map((node) => (
        <foreignObject key={node.id} x={node.x} y={node.y} width={node.width} height={node.height}>
          <div className={`my-node my-node--${node.step.status}`}>{node.step.title}</div>
        </foreignObject>
      ))}
    </svg>
  );
}
```

`reduceLoomEvent` has the exact `(state, action) => state` shape `useReducer`
wants, so it drops straight in.

---

## Non-React frameworks

`state.ts`, `layout.ts`, and `client.ts` import nothing from React.

```ts
// Svelte
import { writable } from "svelte/store";
import { LoomClient, initialLoomState, reduceLoomEvent } from "@loomcraft/renderer";

export const loom = writable(initialLoomState);
const client = new LoomClient();
client.streamEvents(sessionId, (event) =>
  loom.update((state) => reduceLoomEvent(state, event)),
);
```

```ts
// Vue
import { ref } from "vue";
const state = ref(initialLoomState);
client.streamEvents(sessionId, (event) => {
  state.value = reduceLoomEvent(state.value, event);
});
```

For a plain-JavaScript port with no build step at all, see
[`examples/01-gwas-discovery/web/index.html`](../examples/01-gwas-discovery/web/index.html)
— a single file that reimplements the reducer and layout in ~150 lines, which is
a useful measure of how small the front-end contract is.

---

## The protocol

Implementing against the HTTP API directly:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/sessions` | Create a task |
| `GET` | `/sessions/{id}` | Session metadata |
| `GET` | `/sessions/{id}/history?after_seq=` | Full state for a reload |
| `DELETE` | `/sessions/{id}` | Delete a task |
| `GET` | `/catalog` | Registered capabilities and workflows |
| `POST` | `/sessions/{id}/uploads` | Upload a file (multipart) |
| `DELETE` | `/sessions/{id}/uploads/{uid}` | Delete a file; returns invalidated requests |
| `GET` | `/tools` | Discover the extended tool schemas |
| `POST` | `/sessions/{id}/tools/{name}` | Dispatch one tool call directly |
| `POST` | `/sessions/{id}/turn` | Start a turn; responds `text/event-stream` |
| `GET` | `/sessions/{id}/events?after_seq=` | Observe without starting a turn |
| `POST` | `/sessions/{id}/cancel` | Stop the running turn |
| `POST` | `/sessions/{id}/input-requests/{rid}/fulfill` | Confirm uploaded files |
| `POST` | `/sessions/{id}/input-requests/{rid}/cancel` | Decline a request |
| `POST` | `/sessions/{id}/executions/{rid}/approve` | Resolve an approval gate |
| `GET` | `/sessions/{id}/artifacts` | List deliverables |
| `GET` | `/sessions/{id}/artifacts/{aid}` | Download one |

### SSE frames

```
event: plan_published
data: {"seq":3,"event":"plan_published","data":{"plan":{...}},"ts":"2026-..."}

: heartbeat
```

Comment lines (`:` prefix) are keep-alives — ignore them. Every persisted event
carries a `seq`; transient `message_delta`, `tool_call`, and `tool_result` frames
may use `seq: -1` when a host chooses not to persist them.

Next: [Extending](05-extending.md).
