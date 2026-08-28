# Concepts

The mental model, in the order you meet it.

- [Why a plan at all](#why-a-plan-at-all)
- [Plan](#plan)
- [Step kinds](#step-kinds)
- [Capability](#capability)
- [Workflow](#workflow)
- [Runner](#runner)
- [Session and the four zones](#session-and-the-four-zones)
- [Source refs](#source-refs)
- [Artifacts](#artifacts)
- [Events](#events)
- [Turn](#turn)
- [Replan](#replan)
- [How the pieces fit](#how-the-pieces-fit)

---

## Why a plan at all

An agent loop without a plan has a specific failure mode: it is *unobservable
until it finishes*. You cannot see what it intends, you cannot tell whether step
four is blocked or just slow, and when it announces success you have no way to
check that the work behind that claim happened.

Adding a plan does not fix this by itself — a plan the model narrates in prose is
still just prose. Three things make it real:

1. **It is structured and validated.** A DAG the server checked, not a list the
   model wrote in a message.
2. **It gates execution.** A step cannot run before its dependencies succeed, so
   the graph is a precondition rather than a picture.
3. **The server owns the outcomes.** The model can request work; only the
   execution tools can record that it happened.

Everything below follows from those three.

---

## Plan

A **plan** is a versioned DAG the agent publishes through the `publish_plan`
tool. It carries a `goal`, an optional `summary`, a `revision`, an optional
`reason`, and 1–256 steps — though 24 is the size a reviewer can actually read
at a glance, and what the examples and the renderer are tuned for.

```python
{
  "goal": "Assess the quality of the uploaded sales table",
  "summary": "Clean, then profile and scan in parallel, then report.",
  "revision": 1,
  "reason": None,
  "steps": [ ... ]
}
```

Each step has:

| Field | Meaning |
| --- | --- |
| `id` | Unique within the plan; the handle every tool uses |
| `title` | Human-readable, shown on the node |
| `kind` | Who is allowed to complete it — see below |
| `depends_on` | Steps that must be **satisfied** before this one may start |
| `capability` | Registered capability/workflow id (required for those kinds; optional on a review) |
| `description` | Optional detail for the reader |
| `retry` | Attempts and backoff for this step; omit to inherit the capability's |
| `timeout_seconds` | Wall-clock ceiling for one attempt |
| `on_failure` | `stop` (default), `continue`, or `require_approval` |
| `metadata` | Free-form annotations carried through to the UI |
| `status` | Server-owned: see the lifecycle below |
| `summary` | Server-owned: what happened |
| `execution` | Server-owned: which run produced this |
| `attempts` | Server-owned: how many tries it actually took |

The last four are **server-owned**. A model can send them; `validate_plan`
discards them and publishes with everything `pending`. Publishing a plan is a
proposal, never a claim of progress.

### Layers are parallelism

`Plan.layers` groups steps into dependency levels. Everything in a level is
independent of everything else in it, so the engine may run a level concurrently:

```python
parse_plan(plan).layers
# [['clean'], ['outliers', 'profile'], ['report'], ['answer']]
```

You never write "run these in parallel". You write the dependencies that are
actually true, and concurrency falls out. That is worth internalising, because
the most common plan-quality problem is an agent adding edges that do not
correspond to real data dependencies and accidentally serialising its own work.

---

## Step kinds

Kind answers "who is allowed to say this finished?" — which matters more than
what the step is about.

| Kind | Purpose | Completed by |
| --- | --- | --- |
| `capability` | One registered, typed unit of work | `run_capability` **only** |
| `workflow` | A registered multi-step SOP | `run_workflow` **only** |
| `dynamic` | Work the agent performs itself (a script it wrote) | agent, via `update_step` |
| `review` | Explicit verification of artifacts before relying on them | agent, via `update_step` — or `run_capability`, if it binds one |
| `answer` | Composing the final reply | agent, via `update_step` |

`update_step` refuses `capability` and `workflow` steps outright:

```python
await broker.dispatch("update_step", {"step_id": "clean", "status": "succeeded"})
# ok=False, "step 'clean' is a capability step; use its execution tool instead"
```

This is the load-bearing distinction in the whole design. Without it, a model
that decided a step "basically worked" could mark it succeeded and unblock the
rest of the graph. With it, a `succeeded` capability step always means a real run
with a real result — so downstream steps and the final answer rest on something.

`dynamic` and unbound `review` are honestly self-reported, and that is the right
trade: the agent *is* the executor there, so the alternative is not having those
steps at all. Make them observable by having the agent register artifacts.

### A review can be made server-owned

Self-reported verification is the weakest link in the chain, because it is
exactly the step whose job is to catch a bad result. When the check itself can be
codified, register it as a capability whose runner starts with `review.` or which
is tagged `review`, and bind it:

```python
{"id": "calibration", "kind": "review", "capability": "review.genomic_inflation",
 "depends_on": ["scan"]}
```

Publication verifies the capability really is review-scoped — an ordinary
transform cannot be bound to a step the model describes as verification — and
from then on the step follows the `run_capability` path. `update_step` refuses it
like any other server-owned step.

---

## Capability

A **capability** is one atomic, typed unit of work — the thing an agent composes
plans out of.

```python
Capability(
    id="gwas.pca",
    name="Principal components of ancestry",
    version="1",
    description="Project samples onto the leading axes of genotype variation.",
    runner="gwas.pca",
    inputs=(CapabilityInput(
        key="cohort", name="QC'd cohort", description="The genotype matrix to decompose.",
        allowed_extensions=(".tsv",), max_files=1,
    ),),
    outputs=(Port(name="components", artifact_type="json"),),
    parameters={"components": Parameter(
        type="integer", description="How many principal components to retain.",
        minimum=1, maximum=10, default=2,
    )},
    max_attempts=3,
    retry_backoff_seconds=1.0,
    timeout_seconds=120,
    requires_approval=False,
    tags=("gwas", "pca", "ancestry", "population-structure"),
)
```

Because the contract is data, one declaration produces three things that can
never disagree: the JSON Schema the agent reads via `capability_search`, the
server-side validation the broker runs, and the execution graph the engine runs.

### Input variants

`input_variants` declares which *combinations* are acceptable:

```python
input_variants=(("bed", "bim", "fam"), ("vcf",))
```

A PLINK triple, or a VCF. Supplying `bed` alone is refused; supplying a triple
*and* a VCF is refused. Without this, "optional inputs" degrade into an untyped bag and the
runner ends up re-validating by hand.

### Execution policy

`max_attempts`, `retry_backoff_seconds`, `timeout_seconds`, and
`requires_approval` are declared on the capability, so retry semantics live next
to the work rather than in the caller.

`requires_approval=True` is a **pre-execution** gate: the engine parks the node
before invoking the runner, and supplies `ctx.config["approved"] = True` once a
person says yes. The decision therefore precedes the side effect. This is the
right setting for anything that writes outside the session, costs real money, or
cannot be undone.

A plan step may override the retry and timeout for one particular use — see
[Defining plans](02-defining-plans.md#execution-policy). An omitted `retry` on
the step inherits whatever the capability declared, so publishing a plan never
silently downgrades a capability that asked for three attempts.

---

## Workflow

A **workflow** is a registered multi-node sub-DAG — a standard operating
procedure you want offered as one unit.

```python
Workflow(
    id="gwas.structured_scan",
    name="Structure-aware association scan",
    description="QC, then ancestry ‖ relatedness, then a kinship-corrected scan.",
    inputs=(CapabilityInput(key="vcf", name="VCF", description="The cohort to scan."),),
    nodes=(
        WorkflowNode(id="qc", name="Quality control", runner="gwas.qc", inputs=("vcf",),
                     outputs=(Port(name="cohort", artifact_type="tsv"),
                              Port(name="qc_report", artifact_type="json"))),
        # Same single dependency, no edge between them: the engine runs these
        # two concurrently. The SOP author never opts in to parallelism.
        WorkflowNode(id="pca", name="Ancestry axes", runner="gwas.pca",
                     depends_on=("qc",), outputs=(Port(name="components", artifact_type="json"),)),
        WorkflowNode(id="kinship", name="Relatedness", runner="gwas.kinship",
                     depends_on=("qc",), outputs=(Port(name="grm", artifact_type="json"),)),
        WorkflowNode(id="assoc", name="Association scan", runner="gwas.associate",
                     depends_on=("qc", "pca", "kinship"),
                     outputs=(Port(name="stats", artifact_type="json"),)),
    ),
)
```

**Capability vs workflow.** A capability is one node the agent schedules itself,
which lets it interleave your work with its own `dynamic` steps. A workflow is
several nodes the *engine* schedules as a unit — use it when the sequence must
not vary, or when you want the engine's internal parallelism (a workflow is where
concurrent execution inside a single run actually happens, since the broker
refuses two overlapping agent-initiated executions).

Inside a workflow, a node receives its dependencies' artifacts keyed by the
emitting **port name** — so `report` reads `ctx.input("profile")` without caring
which node produced it.

---

## Runner

A **runner** is any `async def run(ctx: NodeContext) -> NodeResult`. That is the
entire extension point.

```python
async def profile(ctx: NodeContext) -> NodeResult:
    table = ctx.input("table")            # declared input, already verified
    ctx.log("parsing", "info")
    ctx.progress(0.5, "halfway")          # streamed to the UI
    ctx.emit("profile", "profile.json", json.dumps(result))
    return NodeResult.ok(rows=len(rows))  # small structured detail
```

`NodeResult` has four shapes, and choosing correctly matters:

| Result | Meaning |
| --- | --- |
| `NodeResult.ok(**detail)` | Succeeded |
| `NodeResult.fail(msg)` | Failed permanently — **do not** retry |
| `NodeResult.retry(msg)` | Failed transiently — retry if budget remains |
| `NodeResult.needs_approval(msg)` | Park until a human decides |

`fail` vs `retry` is the one to get right. A malformed input file is `fail` —
running it again cannot help, and retrying wastes the budget and delays the
agent's replan. A 503 from an upstream service is `retry`.

The context deliberately cannot reach the plan or other nodes. A runner reads
its inputs, writes artifacts, reports progress, and returns; the engine assigns
status. That separation is what keeps "did this work?" answerable.

---

## Session and the four zones

A **session** is one task from first message to final deliverable. Its directory
has four zones with different trust levels:

| Zone | Contents | Written by | Readable by agent |
| --- | --- | --- | --- |
| `uploads/` | What the user provided | the host, on upload | via `upload:` refs |
| `artifacts/` | What execution produced | the engine only | via `artifact:` refs |
| `scratch/` | The agent's own workspace | the agent | via `scratch:` refs |
| `control/` | Plan, plan history, executions, event log | the server only | **never** |

The split is what makes the rest safe. `control/` holds the plan the agent is
being held to — if the agent could write there, none of the validation would
mean anything. And nothing in `scratch/` is a deliverable until
`register_artifacts` promotes it, which re-validates the path and copies the
bytes into `artifacts/`.

---

## Source refs

An input is never a path. It is a reference the session resolves:

| Ref | Points at |
| --- | --- |
| `upload:<id>` | A user-provided file |
| `artifact:<id>` | Something execution produced |
| `scratch:<relative-path>` | A file the agent wrote in its sandbox |

Every resolution re-checks containment (resolving symlinks first) and, for
manifest-backed sources, re-checks size and SHA-256 against what was recorded at
ingest:

```python
session.resolve_source("scratch:../control/plan.json")   # SourceError
session.resolve_source("upload:u1")                      # if bytes changed:
                                                         # SourceIntegrityError
```

The integrity check is not paranoia about the model — it is about *time*. A
session can be long-lived, files can be replaced, and a run that silently
consumed different bytes than the ones the agent inspected would produce a
result nobody could reproduce.

---

## Artifacts

Files produced by execution or promoted from scratch. Each is recorded with size,
SHA-256, content type, the port that emitted it, and the step and run it came
from — so a deliverable can always be traced back to the step that made it.

Two ways to create one:

```python
ctx.emit("profile", "profile.json", data)   # from a runner, bound to a port
```
```python
await broker.dispatch("register_artifacts", {                # from the agent
    "step_id": "analysis",
    "artifacts": [{"path": "findings.md"}],
})
```

`register_artifacts` is atomic across 1–12 files: every path is validated before
any is registered, so a batch with one bad entry registers nothing.

---

## Events

Everything observable is an event on an append-only, hash-chained log.

| Event | Fires when |
| --- | --- |
| `plan_published` | A revision is accepted |
| `step_updated` | Any step's status changes |
| `execution_started` / `_progress` / `_finished` | A run's lifecycle, including per-node retries |
| `artifact_registered` | A deliverable is recorded |
| `input_required` / `_fulfilled` / `_cancelled` / `_invalidated` | The file-request lifecycle |
| `approval_required` / `_resolved` | A human gate |
| `tool_call` / `tool_result` | Agent activity (stream-only, not persisted) |
| `message` / `message_delta` / `notice` / `error` / `done` | Conversation and turn lifecycle |

Each line commits to the previous one and a sidecar cursor pins the log's
identity. Normal appends stay O(1); tampering is detectable via
`session.events.verify()`.

Sequence numbers are dense and monotonic, so a client resumes with `after_seq`
and can prove it missed nothing.

---

## Turn

A **turn** is one user message plus everything the agent does in response. Turns
are the natural unit for budgets: each begins with `broker.begin_turn()`, which
resets the per-turn call budget and the repeat detector.

Crucially, in the reference server a turn runs **in the background** and the SSE
response only *subscribes* to it. A client that navigates away stops receiving
events; it does not cancel the work. Reconnecting with `after_seq` replays what
was missed. Getting this backwards — tying the work's lifetime to the HTTP
connection — is the most common way a long-running agent task dies for no reason.

---

## Replan

When something fails, the agent publishes a higher revision with a `reason`:

```python
{
  "goal": "…",
  "revision": 2,
  "reason": "Contradiction analysis needs two documents and the user declined "
            "to supply a second. Dropping the cross-check and delivering a "
            "single-document brief instead.",
  "steps": [ ... ]
}
```

Rules: revisions must increase; a revision replacing an earlier plan must carry a
`reason`; you cannot replace a plan while a step is `running` or
`waiting_approval`; and a revision may not drop a declared objective. Old
revisions are retained, and the renderer offers a revision switcher.

Artifacts survive a replan — completed work is not thrown away just because the
plan around it changed.

## Objectives and the evidence ledger

A plan can also state *what it is trying to find out*, separately from the steps
it will run to find out:

```python
{
  "objectives": [
    {"id": "q1", "question": "Which loci associate with yield?",
     "estimand": "per-allele effect", "independent_unit": "plot"}
  ],
  "analysis_coverage": [
    {"objective_id": "q1", "status": "executed",
     "reason": "structure-aware scan, λ = 0.95",
     "step_ids": ["scan"], "artifact_refs": ["artifact:art-9f3c"]}
  ]
}
```

Objectives are optional. Declaring them buys three enforced properties:

1. **Every objective must be covered.** A plan that declares a question and no
   `analysis_coverage` entry for it is rejected.
2. **"Executed" must cite evidence.** `status: "executed"` requires at least one
   `step_ids` or `artifact_refs` entry. You cannot claim a question was answered
   without naming what answers it.
3. **An unanswered question must leave a thread.** `not_estimable`, `blocked`
   and `deferred_by_scope` all require a `next_action`.

And across revisions: a later plan may *reclassify* an objective — including as
`not_estimable` — but may not remove it. The failure mode this closes is the
quiet one: an investigation that finishes by narrowing until only the parts that
worked are still being asked about.

`independent_unit` deserves the field it gets. It is the assumption most often
left implicit and most often responsible for a result that does not replicate —
writing it down next to the question is cheap, and reviewable.

---

## How the pieces fit

```
user message
      │
      ▼
   Agent ──── session_context ────► what do I have?
      │  ──── capability_search ──► what may I run?
      │  ──── inspect_source ─────► what is actually in the file?
      │
      ├── missing files? ── request_inputs ──► turn ends, broker blocks
      │                                        mutating tools until resolved
      │
      ├── publish_plan ──► Broker validates ──► DAG ok? ids known?
      │                                          revision increased?
      │                    ──► Session persists ──► `plan_published`
      │
      ├── run_capability ──► Broker authorizes: right kind? right capability?
      │                       dependencies succeeded? not already run?
      │                     ──► contracts validated, sources resolved
      │                     ──► Engine runs the graph (parallel, retry, timeout)
      │                     ──► artifacts recorded, step written by the server
      │                     ──► `execution_started/progress/finished`
      │
      ├── update_step ──► only answer/dynamic/review, deps enforced
      │
      └── register_artifacts ──► scratch validated, promoted, `artifact_registered`
                                    │
                                    ▼
                              Event log (hash-chained)
                                    │ SSE
                                    ▼
                              Renderer: live DAG, timeline, downloads
```

Next: [Defining plans](02-defining-plans.md) for the schema and rules in full.
