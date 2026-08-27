<div align="center">

# LoomCraft

**An AI-native DAG planning and execution engine.**

The agent writes the graph. The server proves it is safe. The engine runs it.
The UI draws it — live.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-38bdf8?style=flat-square&logo=python&logoColor=white&labelColor=0b1120)](packages/core/pyproject.toml)
[![React 18+](https://img.shields.io/badge/react-18+-a78bfa?style=flat-square&logo=react&logoColor=white&labelColor=0b1120)](packages/renderer/package.json)
[![core deps: pydantic only](https://img.shields.io/badge/core%20deps-pydantic%20only-fbbf24?style=flat-square&labelColor=0b1120)](packages/core/pyproject.toml)
[![tests: 232 passing](https://img.shields.io/badge/tests-232%20passing-34d399?style=flat-square&labelColor=0b1120)](#testing)
[![license: MIT](https://img.shields.io/badge/license-MIT-f472b6?style=flat-square&labelColor=0b1120)](LICENSE)

[Quick start](#quick-start) · [Core concepts](#core-concepts) · [Architecture](#architecture) · [Docs](docs/) · [Examples](examples/)

<br>

<img src="assets/plan-execution.svg" width="820"
     alt="The LoomCraft workbench rendering revision 2 of an association study: quality control succeeds, ancestry axes and the relatedness matrix are dispatched together because neither depends on the other, the structure-aware scan waits for both, and multiple-testing correction follows.">

<sub>The workbench, drawn with the tokens <code>@loomcraft/renderer</code> ships — and a real plan,
from <a href="examples/01-gwas-discovery/">example 1</a>.<br>
Revision <b>2</b>, because revision 1 was confounded and the agent noticed: λ = 2.80 → 0.95, eight hits → three.</sub>

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

<div align="center">
<img src="assets/architecture.svg" width="820"
     alt="LoomCraft request path: the agent calls tools, the broker validates and authorizes every call, the engine runs the DAG, both write to an append-only hash-chained event log, and the renderer subscribes to that log over SSE. User uploads and approvals flow from the renderer back to the agent.">
</div>

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
    id="gwas.kinship",
    name="Genomic relatedness matrix",
    description="Realised kinship between every pair of samples, from genotypes.",
    runner="gwas.kinship",
    inputs=(CapabilityInput(
        key="cohort", name="Cohort", description="A QC'd genotype matrix.",
        allowed_extensions=(".tsv",),
    ),),
    outputs=(Port(name="grm", artifact_type="json"),),
    max_attempts=3,              # retry with exponential backoff
    timeout_seconds=120,
    tags=("gwas", "kinship", "relatedness", "mixed-model"),
))
async def kinship(ctx: NodeContext) -> NodeResult:
    cohort = parse(ctx.input("cohort").read_text())
    ctx.progress(0.5, "standardising markers")
    ctx.emit("grm", "kinship.json", relatedness(cohort))
    return NodeResult.ok(samples=len(cohort.samples))
```

That is the whole extension point. LoomCraft never imports your domain code —
you register a contract and an async callable.

**2 · Give an agent the tools and a session.**

```python
from loomcraft import AnthropicAgent, SessionStore, ToolBroker

session = SessionStore("./data").create()
session.save_upload("cohort.vcf", open("cohort.vcf", "rb"))

broker = ToolBroker(session, registry)
result = await AnthropicAgent().run_turn(
    broker, "Which markers are associated with salt tolerance in this cohort?"
)
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
python examples/01-gwas-discovery/run_scripted.py
python examples/02-literature-meta/run_scripted.py
```

---

## Core concepts

### Plan

A versioned DAG the agent publishes through `publish_plan`. Each step has an
`id`, a `title`, a `kind`, and `depends_on` edges.

```json
{
  "goal": "Find markers associated with salt tolerance in the uploaded cohort",
  "revision": 2,
  "reason": "λ = 2.80 in revision 1 — inflated genome-wide, which is population structure rather than signal",
  "steps": [
    { "id": "qc",      "kind": "capability", "capability": "gwas.qc",        "title": "Quality control" },
    { "id": "pca",     "kind": "capability", "capability": "gwas.pca",       "title": "Ancestry axes",         "depends_on": ["qc"] },
    { "id": "kinship", "kind": "capability", "capability": "gwas.kinship",   "title": "Relatedness matrix",    "depends_on": ["qc"] },
    { "id": "assoc",   "kind": "capability", "capability": "gwas.associate", "title": "Structure-aware scan",  "depends_on": ["qc", "pca", "kinship"] },
    { "id": "correct", "kind": "capability", "capability": "gwas.correct",   "title": "Multiple testing",      "depends_on": ["assoc"] },
    { "id": "review",  "kind": "review",     "title": "Check the model is calibrated", "depends_on": ["correct"] },
    { "id": "answer",  "kind": "answer",     "title": "Report the associated loci",    "depends_on": ["review"] }
  ]
}
```

`pca` and `kinship` both depend only on `qc` and not on each other, so the engine
runs them **concurrently** — that is the layer lighting up twice at once in the
animation at the top of this page. Parallelism is a property of the graph, never
a keyword the plan author has to remember.

Note the `reason`. Revision 1 of this plan had no `pca` or `kinship` step at all;
it went straight from `qc` to a plain per-marker scan. The agent added them
because its own `review` step read a genomic inflation factor of 2.80 off the
artifact and concluded the model — not the arithmetic — was wrong.

### Step kinds

Kind decides *who is allowed to complete the step* — the important half.

| Kind | What it is | Completed by |
| --- | --- | --- |
| `capability` | One registered, typed unit of work | `run_capability` **only** |
| `workflow` | A registered multi-step SOP | `run_workflow` **only** |
| `dynamic` | Work the agent does itself in its sandbox | the agent, via `update_step` |
| `review` | Explicit verification of produced artifacts | the agent, via `update_step` |
| `answer` | Composing the final reply | the agent, via `update_step` |

```mermaid
flowchart LR
    A(["Agent"]):::agent

    A -- "update_step" --> AK["<b>answer</b> · <b>dynamic</b> · <b>review</b><br/><i>work the agent did itself</i>"]:::selfwrite
    A -- "run_capability<br/>run_workflow" --> SK["<b>capability</b> · <b>workflow</b><br/><i>registered units of work</i>"]:::servwrite
    A -. "update_step — refused" .-> SK

    SK --> ENG(["Engine"]):::engine
    ENG -- "status + artifacts" --> LOG[("Event log")]:::log
    AK -- "status" --> LOG

    classDef agent     fill:#f6f3fb,stroke:#6d5bb5,stroke-width:2px,color:#1a2332
    classDef selfwrite fill:#ffffff,stroke:#6d5bb5,stroke-width:1.5px,color:#1a2332
    classDef servwrite fill:#eef4fa,stroke:#1661ab,stroke-width:1.5px,color:#1a2332
    classDef engine    fill:#fbf6e9,stroke:#a8864b,stroke-width:2px,color:#1a2332
    classDef log       fill:#f0f6ef,stroke:#4a7d5b,stroke-width:2px,color:#1a2332

    linkStyle 2 stroke:#c03030,color:#c03030
```

The dotted edge is the whole point: an agent can *ask* to mark a `capability`
step done, and the broker refuses. A `capability` step reading `succeeded`
therefore always corresponds to a run that really happened.

### Step lifecycle

Statuses are not free-form strings — every write goes through a transition table,
so the log can never contain a step that went backwards.

```mermaid
stateDiagram-v2
    direction LR

    [*] --> pending
    pending   --> running   : all deps succeeded
    pending   --> skipped   : a dep failed
    running   --> succeeded : the owner wrote a result
    running   --> failed    : raised or timed out
    failed    --> running   : retry, with backoff
    skipped   --> running   : a replan unblocked it
    succeeded --> [*]

    classDef pend fill:#ffffff,stroke:#9aa2af,stroke-width:1.5px,color:#1a2332
    classDef run  fill:#eef4fa,stroke:#1661ab,stroke-width:2px,color:#1a2332
    classDef ok   fill:#f2f4e9,stroke:#6b7a3a,stroke-width:2px,color:#1a2332
    classDef bad  fill:#faeceb,stroke:#c03030,stroke-width:2px,color:#1a2332
    classDef skip fill:#f7f4ec,stroke:#b9b0a0,stroke-width:1.5px,color:#1a2332

    class pending pend
    class running run
    class succeeded ok
    class failed bad
    class skipped skip
```

`succeeded` is terminal — nothing can un-succeed a step, including a replan.
`failed` and `skipped` are not: a retry or a higher revision may put them back
in flight, which is exactly how recovery works without rewriting history.

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

```mermaid
flowchart LR
    USER(["User"]):::user
    ENG(["Engine"]):::engine
    AG(["Agent"]):::agent

    USER -- "uploads a file" --> UP
    ENG  -- "registers output" --> ART
    AG   -- "reads" --> UP
    AG   -- "reads" --> ART
    AG   -- "reads + writes" --> SCR
    AG   -. "no ref can name it" .-> CTL

    subgraph SESSION["one session on disk"]
        direction TB
        UP["<b>uploads/</b><br/><code>upload:id</code>"]:::up
        ART["<b>artifacts/</b><br/><code>artifact:id</code>"]:::art
        SCR["<b>scratch/</b><br/><code>scratch:path</code>"]:::scr
        CTL["<b>control/</b><br/>plan · event log · cursor"]:::ctl
    end

    classDef user   fill:#f0f6ef,stroke:#4a7d5b,stroke-width:2px,color:#1a2332
    classDef agent  fill:#f6f3fb,stroke:#6d5bb5,stroke-width:2px,color:#1a2332
    classDef engine fill:#3a2c0a,stroke:#fbbf24,stroke-width:2px,color:#fef3c7
    classDef up     fill:#f0f6ef,stroke:#4a7d5b,stroke-width:1.5px,color:#1a2332
    classDef art    fill:#eef4fa,stroke:#1661ab,stroke-width:1.5px,color:#1a2332
    classDef scr    fill:#f6f3fb,stroke:#6d5bb5,stroke-width:1.5px,color:#1a2332
    classDef ctl    fill:#faeceb,stroke:#c03030,stroke-width:2px,color:#1a2332

    linkStyle 5 stroke:#c03030,color:#c03030
```

Every arrow above is checked on **every** resolution, not once at registration:
the path is re-confined to the session (symlinks included) and the recorded
SHA-256 is re-verified, so a file swapped underneath a ref is caught rather than
consumed.

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
pip install -e packages/core   # or: export PYTHONPATH=packages/core/src

cd packages/core     && python -m unittest discover -s tests   # 187 tests
cd packages/renderer && npm ci && npm test                     # 45 tests
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
