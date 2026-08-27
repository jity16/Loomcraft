<div align="center">

# LoomCraft

**An AI-native DAG planning and execution engine.**

The agent writes the graph. The server proves it is safe. The engine runs it.
The UI draws it — live.

[Quick start](#quick-start) · [Core concepts](#core-concepts) · [Architecture](#architecture) · [Docs](docs/) · [Examples](examples/)

</div>

---

## What this is

Most "AI workflow" tools give you one of two things: a **visual builder** where a
human draws the DAG and the model just fills in a node, or an **agent loop** where
the model does whatever it wants and you find out afterwards.

LoomCraft is the third option. The agent *authors* the task graph at runtime —
shaped to the actual problem, not to a template — but it authors it through a
narrow, validated tool surface. The server checks the graph before anything runs,
owns every execution result, and streams each state change as an event. You get
model-authored flexibility with server-enforced safety, and a UI that shows the
real plan rather than a chat transcript pretending to be one.

```
                 publish_plan          run_capability
   ┌────────┐   ─────────────►  ┌──────────┐  ────────►  ┌────────┐
   │ Agent  │                   │  Broker  │             │ Engine │
   │ (any   │   ◄─────────────  │ validate │  ◄────────  │ DAG    │
   │ model) │   tool results    │ authorize│   results   │ runner │
   └────────┘                   └────┬─────┘             └───┬────┘
                                     │ events                │
                                     ▼                       │
                              ┌─────────────┐                │
                              │  Event log  │ ◄──────────────┘
                              │ (hash-chain)│
                              └──────┬──────┘
                                     │ SSE
                                     ▼
                              ┌─────────────┐
                              │  Renderer   │  live DAG, timeline, artifacts
                              └─────────────┘
```

### What the server guarantees

A plan is not a suggestion the UI decorates. These are enforced, with tests:

- **The graph is a DAG.** Cycles, self-dependencies, unknown dependency targets,
  duplicate ids, and oversized plans are rejected at publish time.
- **A step runs only when its dependencies have succeeded.** The edges are an
  execution precondition, not documentation.
- **The model cannot mark its own work done.** `capability` and `workflow` steps
  are written *only* by their execution tools, so a step reading `succeeded`
  corresponds to a real run that produced real artifacts.
- **Replans are monotonic and explained.** A new revision must increase and must
  carry a `reason`; the old revision is kept for audit.
- **Files are references, never paths.** Inputs are `upload:`/`artifact:`/`scratch:`
  refs, re-resolved and checksum-verified on every use, confined to the session.
- **Loops are bounded.** Per-turn call budgets and repeat detection stop a
  confused model from burning context without progress.

### What you get for free

Parallel scheduling, retry with backoff, timeouts, cancellation that actually
waits, human-approval gates, skip propagation, a hash-chained audit log,
SSE streaming with resume, and a React renderer that reads the same events.

---

## Quick start

```bash
pip install loomcraft            # engine only — one dependency (pydantic)
pip install 'loomcraft[server,anthropic]'   # + FastAPI/SSE + Claude agent
```

**1 · Register what your agent is allowed to do.**

```python
from loomcraft import (
    Capability, CapabilityInput, NodeContext, NodeResult, Port, Registry,
)

registry = Registry()

@registry.capability_runner(Capability(
    id="csv.profile",
    name="Profile a CSV",
    description="Column types, null counts, and basic statistics.",
    runner="csv.profile",
    inputs=(CapabilityInput(
        key="table", name="Table", description="A CSV with a header row.",
        allowed_extensions=(".csv",),
    ),),
    outputs=(Port(name="profile", artifact_type="json"),),
    max_attempts=3,              # retry with exponential backoff
    timeout_seconds=120,
    tags=("csv", "profile", "statistics"),
))
async def profile(ctx: NodeContext) -> NodeResult:
    text = ctx.input("table").read_text()
    ctx.progress(0.5, "parsing")
    ctx.emit("profile", "profile.json", analyse(text))
    return NodeResult.ok(rows=text.count("\n"))
```

That is the whole extension point. LoomCraft never imports your domain code —
you register a contract and an async callable.

**2 · Give an agent the tools and a session.**

```python
from loomcraft import AnthropicAgent, SessionStore, ToolBroker

session = SessionStore("./data").create()
session.save_upload("sales.csv", open("sales.csv", "rb"))

broker = ToolBroker(session, registry)
result = await AnthropicAgent().run_turn(broker, "Profile the uploaded table.")
```

**3 · Serve it and render it.**

```python
from loomcraft.server import create_app
app = create_app(SessionStore("./data"), registry, lambda _s: AnthropicAgent())
```

```tsx
import { LoomWorkbench } from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

<LoomWorkbench sessionId={sessionId} baseUrl="/api/v1/loomcraft" />
```

**4 · Or run the examples with no API key at all.**

```bash
python examples/01-data-pipeline/run_scripted.py
python examples/02-research-assistant/run_scripted.py
```

---

## Core concepts

### Plan

A versioned DAG the agent publishes through `publish_plan`. Each step has an
`id`, a `title`, a `kind`, and `depends_on` edges.

```json
{
  "goal": "Assess the quality of the uploaded sales table",
  "revision": 1,
  "steps": [
    { "id": "clean",    "kind": "capability", "capability": "csv.clean",    "title": "Clean the table" },
    { "id": "profile",  "kind": "capability", "capability": "csv.profile",  "title": "Profile columns",  "depends_on": ["clean"] },
    { "id": "outliers", "kind": "capability", "capability": "csv.outliers", "title": "Detect outliers",  "depends_on": ["clean"] },
    { "id": "report",   "kind": "capability", "capability": "csv.report",   "title": "Write the report", "depends_on": ["profile", "outliers"] },
    { "id": "answer",   "kind": "answer",     "title": "Answer the user",   "depends_on": ["report"] }
  ]
}
```

`profile` and `outliers` both depend only on `clean` and not on each other, so
the engine runs them **concurrently**. Parallelism is a property of the graph,
never a keyword the plan author has to remember.

### Step kinds

Kind decides *who is allowed to complete the step* — the important half.

| Kind | What it is | Completed by |
| --- | --- | --- |
| `capability` | One registered, typed unit of work | `run_capability` **only** |
| `workflow` | A registered multi-step SOP | `run_workflow` **only** |
| `dynamic` | Work the agent does itself in its sandbox | the agent, via `update_step` |
| `review` | Explicit verification of produced artifacts | the agent, via `update_step` |
| `answer` | Composing the final reply | the agent, via `update_step` |

### Capability

A typed contract: declared inputs (with extensions and cardinality), declared
parameters (with types and ranges), declared outputs, one runner. Because the
contract is data, the *same* declaration produces the agent-facing JSON Schema,
the server-side validation, and the execution graph — they cannot drift apart.

Input **variants** let one capability accept alternatives without accepting
nonsense: `input_variants=(("bed", "bim"), ("vcf",))` means a PLINK pair *or* a
VCF, never half of each.

### Source refs

An input is never a filesystem path. It is `upload:<id>`, `artifact:<id>`, or
`scratch:<relative-path>`, resolved through the session with containment and
integrity checks on every call. A session has four zones with different trust:
`uploads/` (the user's), `artifacts/` (execution output), `scratch/` (the agent's
own workspace), `control/` (server-owned, unreachable by the agent).

### Events

Everything observable is an event on an append-only, hash-chained log:
`plan_published`, `step_updated`, `execution_started/progress/finished`,
`artifact_registered`, `input_required/fulfilled/cancelled/invalidated`,
`approval_required/resolved`, `tool_call/result`, `message`, `error`, `done`.

The renderer folds these into state with one pure function — and folds a
persisted history with the *same* function, which is why a live stream and a
mid-run page refresh cannot disagree.

### Replan

When something fails, the agent publishes a higher revision with a `reason`. Old
revisions stay for audit, and the UI offers a revision switcher so a reviewer can
see what the agent believed before and after it learned something.

---

## Architecture

```
packages/core/src/loomcraft/
├── plan.py       Plan/Step models, DAG validation, revision + transition rules
├── graph.py      Pure DAG algorithms (layering, cycles, critical path) — no deps
├── registry.py   Capabilities, workflows, runners — where your domain plugs in
├── context.py    The runner contract: NodeContext / NodeResult
├── engine.py     Async driver: parallel, retry, timeout, approval, cancellation
├── store.py      Sessions, the four zones, source-ref resolution, artifacts
├── events.py     Append-only hash-chained event log + subscriptions
├── inputs.py     Typed file requests + upload→slot allocation
├── tools.py      The 10 agent tools as JSON Schema, in 4 provider dialects
├── broker.py     The only door: validates and dispatches every tool call
├── agent.py      Agent loops — Anthropic, OpenAI-compatible, and scripted
└── server.py     Optional FastAPI router: sessions, uploads, SSE, downloads

packages/renderer/src/
├── state.ts      The event reducer + history hydration (framework-agnostic)
├── layout.ts     Layered DAG layout with crossing reduction — zero dependencies
├── client.ts     HTTP + SSE client with detach/resume semantics
├── useLoomSession.ts   The one hook most hosts need
└── components/   PlanGraph, panels, and a ready-made LoomWorkbench
```

**Dependency budget.** The engine depends on `pydantic` and nothing else.
FastAPI, `anthropic`, and `openai` are optional extras. The renderer's only peer
dependency is React — the DAG layout, the pan/zoom canvas, and the SSE reader are
all first-party, so adding LoomCraft to a UI does not drag in a chart library or
a graph-layout engine.

**Provider neutrality.** `tools.py` emits one canonical tool surface and adapts
it to Anthropic, OpenAI chat, OpenAI Responses, and MCP dialects. The broker
validates calls identically whichever produced them, so swapping models is a
constructor change.

---

## Testing

```bash
cd packages/core     && python -m unittest discover -s tests   # 187 tests
cd packages/renderer && npm test                               # 45 tests
```

The core suite runs on the standard library — no pytest required — and covers
DAG validation, revision discipline, the transition state machine, concurrency,
retry, timeouts, approval, cancellation, skip propagation, path traversal,
integrity checks, event-log tampering, allocation, contracts, and every broker
guardrail.

---

## Documentation

| Guide | What's in it |
| --- | --- |
| [Concepts](docs/01-concepts.md) | The model: plans, kinds, capabilities, sessions, events |
| [Defining plans](docs/02-defining-plans.md) | Plan schema, validation rules, transitions, replanning |
| [Agent integration](docs/03-agent-integration.md) | Tool surface, prompting, Claude/OpenAI/MCP, loop design |
| [Frontend integration](docs/04-frontend-integration.md) | Reducer, SSE, components, theming, custom UIs |
| [Extending](docs/05-extending.md) | Runners, capabilities, workflows, storage, transports |
| [Architecture](docs/06-architecture.md) | Design decisions and why they were made that way |
| [API reference](docs/07-api-reference.md) | Every public symbol, tool, event, and endpoint |

---

## License

MIT
