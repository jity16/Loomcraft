<div align="center">

# LoomCraft

**An AI-native DAG planning and execution engine.**

The agent writes the graph. The server proves it is safe. The engine runs it.
The UI draws it — live.

**English** · [简体中文](README.zh-CN.md)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-38bdf8?style=flat-square&logo=python&logoColor=white&labelColor=0b1120)](packages/core/pyproject.toml)
[![React 18+](https://img.shields.io/badge/react-18+-a78bfa?style=flat-square&logo=react&logoColor=white&labelColor=0b1120)](packages/renderer/package.json)
[![core deps: pydantic only](https://img.shields.io/badge/core%20deps-pydantic%20only-fbbf24?style=flat-square&labelColor=0b1120)](packages/core/pyproject.toml)
[![tests: 309 passing](https://img.shields.io/badge/tests-309%20passing-34d399?style=flat-square&labelColor=0b1120)](#testing)
[![license: MIT](https://img.shields.io/badge/license-MIT-f472b6?style=flat-square&labelColor=0b1120)](LICENSE)

[Quick start](#quick-start) · [Investigative work](#built-for-investigative-work) · [Core concepts](#core-concepts) · [Architecture](#architecture) · [Docs](docs/) · [Examples](examples/)

<br>

<img src="assets/plan-execution.svg" width="820"
     alt="The LoomCraft workbench rendering revision 2 of an association study: quality control succeeds, ancestry axes and the relatedness matrix are dispatched together because neither depends on the other, the structure-aware scan waits for both, and multiple-testing correction follows.">

<sub>The workbench, drawn with the tokens <code>@loomcraft/renderer</code> ships — and a real plan,
from <a href="examples/01-gwas-discovery/">example 1</a>.<br>
Revision <b>1</b> runs, its own <code>review</code> step reads λ = 2.80 off the artifact, and the agent replaces
the plan.<br>Revision <b>2</b> adds the two steps it was missing — and they have no edge between them, so the
engine runs both at once.</sub>

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

### What that buys you

Enough abstraction. Here is real output from
[example 1](examples/01-gwas-discovery/) — no API key, no network, under three
seconds end to end:

```
 6 · The naive scan, and the diagnostic that condemns it
   genomic inflation λ              2.8024   ← a calibrated scan sits near 1.0
   survive FDR                      8
   …of which are real               3 of 8
   false positives                  rs9701, rs5543, rs11703, rs2001, rs10317

 7 · Agent-reported steps: the review that changes the plan
   faking a capability step         refused — step 'assoc' is a capability step
   rerun 'assoc' in place           refused — cannot start from 'succeeded'
   what mlm says for itself         model='mlm' needs a kinship matrix on 'grm'
   conclusion                       the plan needs a step it does not have

 8 · Replan discipline: revision 2 has to explain itself
   revision 2 accepted              True
   layer 1                          kinship, pca   ← the engine may run these at once

11 · The corrected scan
   genomic inflation λ              2.8024  →  0.9461
   survive correction               8  →  3
   recovered                        rs1385, rs2309, rs3233
   markers with a real effect       rs1385, rs2309, rs3233
```

Not one of those numbers is a fixture. The cohort in `data.py` is built so that
ancestry moves the phenotype *and* most allele frequencies at once, which makes
λ = 2.80 an arithmetic consequence of the design; the fall to 0.9461 and the
three survivors are computed the same way. Nobody told the agent its first
answer was wrong. Its own `review` step read the number off the artifact and
rewrote the plan.

A fixed pipeline cannot do that — it has no way to change what it does next. An
unconstrained agent loop cannot be trusted to, because it can simply declare
itself finished. Here it cannot: `assoc` is a `capability` step, so the broker
refused both the attempt to mark it done and the attempt to quietly re-run it
with better settings. The only thing the agent was allowed to change was the
plan — and every change to the plan left a revision number and a reason behind
it.

### What the server guarantees

A plan is not a suggestion the UI decorates. These are enforced, with tests:

- **The graph is a DAG.** Cycles, self-dependencies, unknown dependency targets,
  duplicate ids, and oversized plans are rejected at publish time.
- **A step runs only when its dependencies are satisfied.** The edges are an
  execution precondition, not documentation.
- **The model cannot mark its own work done.** `capability` and `workflow` steps
  — and a `review` step bound to a capability — are written *only* by their
  execution tools, so a step reading `succeeded` corresponds to a real run that
  produced real artifacts.
- **Replans are monotonic and explained.** A new revision must increase and must
  carry a `reason`; the old revision is kept for audit.
- **Declared questions cannot be quietly abandoned.** A revision may reclassify
  an objective — including as *unanswerable* — but may not drop one.
- **Approval gates sit in front of the work.** A capability marked
  `requires_approval` parks *before* its runner is invoked, so the decision
  precedes the side effect rather than blessing it afterwards.
- **Files are references, never paths.** Inputs are `upload:`/`artifact:`/`scratch:`
  refs, re-resolved and checksum-verified on every use, confined to the session.
- **Loops are bounded.** Per-turn call budgets and repeat detection stop a
  confused model from burning context without progress.

### What you get for free

Parallel scheduling, retry with capped exponential backoff, timeouts,
cancellation that actually waits, pre-execution approval gates, skip
propagation, a hash-chained audit log, SSE streaming with resume, and a React
renderer that reads the same events.

---

## Built for investigative work

The GWAS run above is not a demo dressed up as science. It is the shape of the
problem LoomCraft was built for, and the three things that make investigation
different from automation are all in it.

**The next step depends on what the last one found.** A pipeline is a good fit
when the steps are known in advance. Investigation is the case where they are
not: λ = 2.80 is *why* revision 2 has a kinship step. LoomCraft lets the agent
author that graph at runtime and still refuses to let it fake a result.

**The questions must outlive the answers.** A plan may declare `objectives` —
what the work is actually meant to establish — and an evidence ledger binding
each one to the steps and artifacts that discharge it:

```json
{
  "objectives": [
    { "id": "q1", "question": "Which loci associate with yield?",
      "estimand": "per-allele effect", "independent_unit": "plot" },
    { "id": "q2", "question": "Is there a maternal effect?",
      "independent_unit": "dam" }
  ],
  "analysis_coverage": [
    { "objective_id": "q1", "status": "executed",
      "reason": "structure-aware scan, λ = 0.95",
      "step_ids": ["scan"], "artifact_refs": ["artifact:art-9f3c"] },
    { "objective_id": "q2", "status": "not_estimable",
      "reason": "the pedigree has no dam column, so the maternal component is not identifiable",
      "next_action": "request a pedigree export including dam ids" }
  ]
}
```

The server enforces the part that matters. `executed` **requires** a step or an
artifact to point at — you cannot claim a question was answered without naming
the evidence. `not_estimable`, `blocked` and `deferred_by_scope` **require** a
`next_action`. And a later revision cannot make `q2` disappear; the easiest way
to finish an investigation is to stop asking the part that did not work, and
that route is closed.

This is the difference between a report that says *"we found three loci"* and
one that says *"we found three loci; the maternal question was not identifiable
from this design, and here is what would make it so."* The second is worth more,
and only the second is reproducible from the record.

**Getting through a settled plan should not cost model turns.** Once the graph
is decided, `execute_plan` hands the whole thing to the scheduler in one audited
run — independent branches concurrent, per-step retry and timeout, approval
gates honoured:

```python
await broker.dispatch("execute_plan", {})
```

An exploratory branch that comes back empty is a finding, not a crash:
`on_failure: "continue"` lets its independent dependents proceed while the
failure stays recorded on the step and in `failed_nodes`.

---

## Quick start

```bash
# Engine only — one dependency (pydantic)
pip install "git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"

# …with the optional extras: FastAPI/SSE server, Claude agent, OpenAI agent
pip install "loomcraft[server,anthropic] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"
```

> Not on PyPI or npm — install from the repository. For the React renderer,
> `npm` cannot install from a git subdirectory, so build it once and install by
> path:
>
> ```bash
> git clone https://github.com/jity16/Loomcraft.git
> cd Loomcraft/packages/renderer && npm install && npm run build
> cd /your/app && npm install /path/to/Loomcraft/packages/renderer
> ```

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
| `review` | Explicit verification of produced artifacts | the agent, via `update_step` — *unless* it binds a review-scoped capability, which makes it server-owned |
| `answer` | Composing the final reply | the agent, via `update_step` |

A `review` step may name a capability whose runner starts with `review.` or
which is tagged `review`. That turns verification from something the agent
asserts into something the server ran — useful exactly where it matters, on the
check that decides whether a result is trustworthy.

<div align="center">
<img src="assets/step-kinds.svg" width="820"
     alt="The agent completes answer, dynamic and review steps itself through update_step. Capability and workflow steps are written only by run_capability and run_workflow, which dispatch to the engine; update_step against those kinds is refused by the broker.">
</div>

The red dashed arrow is the whole point: an agent can *ask* to mark a
`capability` step done, and the broker refuses. A `capability` step reading
`succeeded` therefore always corresponds to a run that really happened.

### Step lifecycle

Statuses are not free-form strings — every write goes through a transition table,
so the log can never contain a step that went backwards.

<div align="center">
<img src="assets/step-lifecycle.svg" width="820"
     alt="A step goes from pending to running when every dependency has succeeded, or to skipped when one failed. Running goes to succeeded when the owner writes a result, or to failed when the runner raises or times out. Failed can return to running via a bounded retry and skipped via a replan. Succeeded is terminal.">
</div>

`succeeded` is terminal — nothing can un-succeed a step, including a replan.
`failed`, `skipped` and `cancelled` are not: a retry or a higher revision may put
them back in flight, which is exactly how recovery works without rewriting
history. `waiting_approval` is where a step sits while a person decides, and
`ready` marks a step the scheduler could dispatch but has not yet — a
distinction that matters once a whole plan is in flight at once.

Each step may carry its own execution policy, which the reader sees next to the
work it governs:

```json
{
  "id": "scan", "kind": "capability", "capability": "gwas.associate",
  "depends_on": ["qc", "pca", "kinship"],
  "retry": { "max_attempts": 3, "backoff_seconds": 2, "max_backoff_seconds": 60 },
  "timeout_seconds": 900,
  "on_failure": "stop"
}
```

Omitting `retry` inherits whatever the capability declared, so publishing a plan
never silently downgrades a capability that asked for three attempts.

### Capability

A typed contract: declared inputs (with extensions and cardinality), declared
parameters (with types and ranges), declared outputs, one runner. Because the
contract is data, the *same* declaration produces the agent-facing JSON Schema,
the server-side validation, and the execution graph — they cannot drift apart.

Input **variants** let one capability accept alternatives without accepting
nonsense: `input_variants=(("bed", "bim", "fam"), ("vcf",))` means a PLINK
triple *or* a VCF, never half of each.

### Source refs

An input is never a filesystem path. It is `upload:<id>`, `artifact:<id>`, or
`scratch:<relative-path>`, resolved through the session with containment and
integrity checks on every call. A session has four zones with different trust:
`uploads/` (the user's), `artifacts/` (execution output), `scratch/` (the agent's
own workspace), `control/` (server-owned, unreachable by the agent).

<div align="center">
<img src="assets/session-zones.svg" width="820"
     alt="A session has four directories with different trust. Uploads belong to the user, artifacts are written by the engine, scratch is the agent's own workspace, and control holds the plan, the event log and the cursor — no source ref can name it.">
</div>

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
├── plan.py           Plan/Step models, DAG validation, revisions, objectives
├── graph.py          Pure DAG algorithms (layering, cycles, critical path)
├── registry.py       Capabilities, workflows, runners — where your domain plugs in
├── context.py        The runner contract: NodeContext / NodeResult
├── engine.py         Async driver: parallel, retry, timeout, approval, cancellation
├── plan_executor.py  Compiles a published plan into one graph for execute_plan
├── store.py          Sessions, the four zones, source-ref resolution, artifacts
├── events.py         Append-only hash-chained event log + subscriptions
├── inputs.py         Typed file requests + upload→slot allocation
├── tools.py          The 11 agent tools as JSON Schema, in 4 provider dialects
├── broker.py         The only door: validates and dispatches every tool call
├── agent.py          Agent loops — Anthropic, OpenAI-compatible, subprocess, scripted
├── protocol.py       JSON-RPC bridge for Codex / app-server hosts
└── server.py         Optional FastAPI router: sessions, uploads, SSE, downloads

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

## Bringing your own model runtime

Four ways in, all landing on the same broker and therefore the same guarantees:

| Runtime | How |
| --- | --- |
| Claude | `AnthropicAgent()` |
| Any OpenAI-compatible endpoint | `OpenAICompatibleAgent(client, model=…, stream=True)` |
| A model runner in another process | `SubprocessAgent(["my-runner", "--serve"])` — JSONL over stdio |
| Codex / an app-server host | `AppServerBridge(broker)` — JSON-RPC |

The last one is the case where the model runtime owns its own process and calls
*back* for tools. Advertise the catalog at turn start and route each inbound
message through the bridge:

```python
from loomcraft import AppServerBridge, dynamic_tool_specs

bridge = AppServerBridge(broker)

tools = dynamic_tool_specs()          # hand these to the runtime

async def on_message(message: dict) -> dict:
    return await bridge.handle(message)   # {} means it was a notification
```

`initialize`, `tools/list`, `tools/call` and Codex's `item/tool/call` all
resolve to `broker.dispatch`. A tool call arriving over JSON-RPC is authorised
against the published plan exactly like one from an in-process loop — the
transport does not become a second door.

---

## Testing

```bash
make install
make check        # lint, python tests, renderer typecheck/tests/build, docs
```

Or directly:

```bash
python -m pytest -q packages/core/tests    # 255 tests
npm test --prefix packages/renderer        # 54 tests
```

The suites cover DAG validation, revision discipline, the transition state
machine, the objective ledger, concurrency, retry and backoff caps, timeouts,
approval gating, cancellation, skip and failure policy, path traversal,
port contracts, integrity checks, event-log tampering, host-detail scrubbing,
the JSON-RPC bridge, both new agent providers, and every broker guardrail.

`packages/core/tests/test_hardening.py` is worth reading on its own: each test
there corresponds to a defect that once let the engine report success for
something that had not safely happened.

---

## Documentation

| Guide | What's in it |
| --- | --- |
| [Concepts](docs/01-concepts.md) | The model: plans, kinds, capabilities, sessions, events |
| [Defining plans](docs/02-defining-plans.md) | Plan schema, validation rules, transitions, objectives, replanning |
| [Agent integration](docs/03-agent-integration.md) | Tool surface, prompting, Claude/OpenAI/subprocess/Codex, loop design |
| [Frontend integration](docs/04-frontend-integration.md) | Reducer, SSE, components, theming, custom UIs |
| [Extending](docs/05-extending.md) | Runners, capabilities, workflows, storage, transports |
| [Architecture](docs/06-architecture.md) | Design decisions and why they were made that way |
| [API reference](docs/07-api-reference.md) | Every public symbol, tool, event, and endpoint |

Machine-readable contracts live in [`packages/core/schema/`](packages/core/schema/)
and are generated from the code, so they cannot drift from the validators that
actually run.

---

## License

MIT
