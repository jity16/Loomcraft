# Architecture

The design decisions, and why they were made that way. Read this before changing
anything structural — most of these choices exist because the obvious alternative
fails in a specific way.

- [The shape of the problem](#the-shape-of-the-problem)
- [Layers](#layers)
- [Decision: the agent authors the graph](#decision-the-agent-authors-the-graph)
- [Decision: kind determines ownership](#decision-kind-determines-ownership)
- [Decision: parallelism from graph shape](#decision-parallelism-from-graph-shape)
- [Decision: source refs, not paths](#decision-source-refs-not-paths)
- [Decision: events are the only output](#decision-events-are-the-only-output)
- [Decision: turns run in the background](#decision-turns-run-in-the-background)
- [Decision: one engine for everything](#decision-one-engine-for-everything)
- [Decision: artifacts are promoted, not streamed](#decision-artifacts-are-promoted-not-streamed)
- [Decision: fail closed](#decision-fail-closed)
- [Decision: value-free errors](#decision-value-free-errors)
- [The dependency budget](#the-dependency-budget)
- [Concurrency model](#concurrency-model)
- [Security boundaries](#security-boundaries)
- [Known limits](#known-limits)

---

## The shape of the problem

An agent doing multi-step work has to satisfy two requirements that pull against
each other:

**Flexibility.** Real tasks do not fit a template. The right graph for "assess
this cohort" depends on what is in the cohort, and a fixed pipeline either does
not fit or fits by being so generic it does nothing useful.

**Trustworthiness.** If a model can declare its own work finished, nothing
downstream — including the final answer — rests on anything. And if the work is
unobservable until it ends, nobody can tell a stalled run from a slow one.

Visual workflow builders pick trustworthiness and lose flexibility: a human draws
the graph, the model fills in a node. Free-running agent loops pick flexibility
and lose trustworthiness.

LoomCraft's answer: **the model authors the graph, but only through a validated
tool surface, and the server owns every result.** The graph is as flexible as the
model is, and as trustworthy as the validation is.

---

## Layers

```
┌──────────────────────────────────────────────────────────────┐
│  agent.py       The loop: model ↔ tool calls                 │
│                 Provider-specific. Swappable.                │
├──────────────────────────────────────────────────────────────┤
│  broker.py      THE BOUNDARY. Every tool call is validated,  │
│                 authorised against the plan, and dispatched. │
│                 Nothing reaches the engine without passing.  │
├──────────────────────────────────────────────────────────────┤
│  plan.py        What may run and in what order               │
│  registry.py    What exists and what it accepts              │
│  engine.py      Running it: parallel, retry, timeout, cancel │
│  store.py       Where state and bytes live                   │
│  events.py      What happened, durably and in order          │
├──────────────────────────────────────────────────────────────┤
│  server.py      One way to expose it. Optional.              │
└──────────────────────────────────────────────────────────────┘
```

Dependencies point downward only. `plan.py` does not import `engine.py`;
`graph.py` imports nothing from LoomCraft at all. That is why the DAG algorithms
are reusable on their own, and why the plan model can be validated in a context
with no engine present.

---

## Decision: the agent authors the graph

**Alternative rejected: a human-drawn graph the model fills in.**

That works when the task shape is stable — ETL, CI, approvals. It fails when the
right shape depends on the data. You end up with either a template that does not
fit the case, or a template so generic that every node is "call the model".

**Alternative rejected: no graph, just a loop.**

Fast to build, but the run is unobservable until it terminates, failures have no
blast radius (nothing knows what depended on the failed thing), and progress is
whatever the model says it is.

**What makes model-authored graphs safe:**

1. `publish_plan` validates structure before anything runs.
2. `depends_on` is an execution precondition, so the graph is enforced.
3. Revisions are monotonic and explained, so replanning is auditable rather than
   an invisible change of story.
4. The registry bounds what can appear as a `capability` step — the model can
   only compose from what you registered.

The cost is that a bad plan is possible; the mitigation is that a bad plan is
*visible* and *correctable* rather than silently executed.

---

## Decision: kind determines ownership

The single most important rule in the codebase: **`update_step` refuses
`capability` and `workflow` steps.**

Without it, a model that decided a step "basically worked" could mark it
succeeded, unblocking the rest of the graph and the final answer. With it, a
`succeeded` capability step always corresponds to a real run that produced real
artifacts — which is what lets a reviewer trust the plan view at all.

`dynamic`, `review`, and `answer` steps *are* agent-reported, because the agent
genuinely is the executor. The honest framing: those steps carry the same
epistemic weight as anything else the model says, and the way to raise it is to
have the agent register artifacts so the claim has evidence attached.

---

## Decision: parallelism from graph shape

There is no `parallel: true`, no fan-out node, no `Promise.all` equivalent in
the plan schema.

The engine layers the graph by longest path and runs each layer concurrently.
Two steps run at the same time exactly when neither transitively depends on the
other — which is the true condition. Any explicit parallelism syntax is a second,
redundant source of truth that can contradict the first.

The renderer uses the *same* longest-path assignment for its rows, so the picture
and the execution agree by construction rather than by convention.

The practical consequence is that plan quality shows up as an edge problem: an
agent that chains independent steps out of narrative habit serialises its own
work. That is worth a line in your system prompt (see
[Agent integration](03-agent-integration.md#prompting)).

---

## Decision: source refs, not paths

A capability input is `upload:abc`, `artifact:def`, or `scratch:notes/out.tsv` —
never `/var/data/sessions/s1/uploads/abc/cohort.vcf`.

Three problems this solves:

**Containment.** `resolve_source` resolves symlinks first, then proves the result
is inside the session directory. A path string would need this check at every
call site; a ref makes it structural.

**Integrity.** Manifest-backed sources are re-checked against their recorded size
and SHA-256 on *every* resolution. Not paranoia about the model — a session can
be long-lived, and a run that silently consumed different bytes than the ones the
agent inspected produces a result nobody can reproduce.

**Provenance.** A ref says where a file came from. `artifact:` means execution
produced it; `scratch:` means the agent wrote it. That distinction is exactly
what a reviewer needs and a path erases.

The four zones (`uploads/`, `artifacts/`, `scratch/`, `control/`) exist so those
categories have different trust levels. `control/` holds the plan the agent is
being held to — if the agent could write there, none of the validation would mean
anything.

---

## Decision: events are the only output

The engine and broker never return state to a caller and separately notify a UI.
They emit events, and everything else is derived.

That is what allows the reducer to be one function used for both live streaming
and history replay. If live updates went through a different code path from
reload, the two would drift, and the bug would be "the UI is wrong after a
refresh" — reported rarely, reproduced never.

**The hash chain** exists because the event log *is* the audit record. Each line
commits to the previous one; a sidecar cursor pins the log's identity (device,
inode, size, mtime) plus the running digest. Normal appends stay O(1) — read a
small cursor, verify, write one line. If the sidecar is missing or disagrees, the
log is re-scanned once. If the log itself is inconsistent, the writer **fails
closed** rather than allocating a duplicate sequence number over a corrupt tail.

**Dense monotonic sequence numbers** let a client resume with `after_seq` and
*prove* it missed nothing, which a timestamp cursor cannot do.

---

## Decision: turns run in the background

In `server.py`, `POST /turn` starts the work as a background task and the SSE
response only *subscribes* to it.

The alternative — running the turn inside the request handler — ties a
ten-minute analysis to a browser tab. The user switches tabs, a proxy times out
an idle connection, wifi drops, and the work dies for no reason.

With the split: disconnecting unsubscribes and nothing else. Reconnecting with
`after_seq` replays what was missed from the durable log. Cancellation is an
explicit `POST /cancel`, which is the only thing that *should* stop the work.

This is the design decision most worth preserving if you write a custom
transport.

---

## Decision: one engine for everything

A single capability runs through the same driver as a four-node workflow —
`graph_from_capability` just builds a one-node graph.

The tempting alternative is a fast path: "one node, no dependencies, just await
the runner". It would be maybe thirty lines shorter and would immediately drift.
Retry, cancellation, artifact registration, progress events, and timeout handling
would exist twice, and the second copy would lag.

`execute_plan` follows the same rule. It would have been easy to give a whole
plan its own scheduler — it has different concerns, after all: step kinds,
plan-level policy, projecting state back onto the plan. Instead
`plan_executor.py` *compiles* a plan into one `ExecutionGraph` and hands it to
the existing engine. It decides what the nodes are and nothing else. Every
guarantee a single capability gets, a fifteen-step plan gets for free, and there
is exactly one place where retry semantics live.

A registered workflow inside a plan runs as one node wrapping a nested run,
rather than being inlined. Inlining would let a plan step depend on a workflow's
internals, which is precisely what registering it as a unit was meant to prevent.

Same reasoning for cancellation: `Run.cancel()` awaits every node task before
returning. Returning early would be simpler and would let a node keep writing
artifacts after the caller believed the run was over.

---

## Decision: artifacts are promoted, not streamed

A runner emits artifacts through `ctx.emit`, but they are not registered when it
calls it. They are buffered and registered only once the attempt succeeds.

Registering immediately is the obvious implementation and it is wrong in a
specific, quiet way: a runner that writes a partial file and *then* discovers a
transient failure has already published it. The retry succeeds, and now the
session holds two artifacts on the same port — one complete, one truncated —
with a downstream node free to bind either. The user sees both in their
deliverables.

Deferring costs a list and a loop. It buys the property that a registered
artifact always came from an attempt that finished.

The same reasoning applies to approval. `requires_approval` originally parked a
node *after* its runner returned `needs_approval`, which meant the gate could
only ever confirm a result — by then the side effect had happened. It now parks
before the runner is invoked. A gate that runs after the action is not a gate.

---

## Decision: fail closed

Wherever a check cannot be completed, LoomCraft refuses rather than assumes.

| Situation | Behaviour |
| --- | --- |
| Event log inconsistent with itself | Refuse to append |
| Source checksum mismatch | Refuse to resolve |
| Nothing runnable, nothing in flight, not all terminal | Fail the run |
| Driver crashes | Mark every non-terminal node failed |
| Registry references a missing runner | Refuse to submit the graph |
| Artifact batch has one bad path | Register none of them |

The stalled-graph case deserves a note. A DAG that reaches "nothing can move and
nothing is running" but is not complete indicates an inconsistency. Treating that
as success would be the worst outcome available — a run reporting done having
executed nothing.

---

## Decision: value-free errors

`LoomCraftError` carries two messages: `str(exc)` with full detail for your logs,
and `.public_message` for the model.

The public one names the field and the problem but never echoes the rejected
value:

```
✗  invalid plan: steps[2].title: Input should be a valid string [input=<8kb of genotype matrix>]
✓  steps[2].title: Input should be a valid string
```

Two reasons. A model handed its own bad payload back tends to resend it, where
one told which field is wrong fixes it. And a rejected value can be file content
— echoing it into a transcript is an accidental data path.

---

## The dependency budget

| Package | Required | Optional |
| --- | --- | --- |
| `loomcraft` | `pydantic` | `fastapi`, `anthropic`, `openai` |
| `@loomcraft/renderer` | `react` (peer) | — |

Deliberate, and it costs real work: the DAG layout, the pan/zoom canvas, and the
SSE reader are all first-party rather than pulled from dagre, React Flow, and a
streaming library.

The reasoning is that a *library* has a different dependency calculus from an
application. Adding LoomCraft to an existing product should not force a
graph-layout engine into their bundle or a web framework into their service. The
engine runs in a Django view, a Celery worker, or a Lambda without dragging
FastAPI along.

The layout is ~200 lines because plan graphs are small — a general graph library
solves a much harder problem than the one we have.

---

## Concurrency model

### Inside a run

One driver coroutine per run. It scans for nodes whose dependencies have all
succeeded, spawns **all of them at once** (bounded by a semaphore), then sleeps
on an event that node tasks set on completion.

```
driver: scan → spawn ready nodes → wait(event) → repeat
node:   run → set(event)
```

Level-triggered rather than edge-triggered: the driver re-derives the ready set
from persisted status every wake-up, so a missed notification delays a scan
rather than losing a node.

### Across runs

The broker refuses a second `run_capability` while one is in flight, rather than
queueing it. Queueing would create a second concurrency model competing with the
DAG — and the DAG is supposed to be the only one. An agent that wants parallelism
expresses it as graph shape, which means a workflow.

### Cancellation

```
cancel() → set flag → cancel driver + node tasks
        → await every task
        → mark non-terminal nodes cancelled
        → finalise the run
```

Awaiting is the point. A runner's `finally` block gets to run, and the caller
knows nothing is still writing when `cancel()` returns.

---

## Security boundaries

Four, in order of how much they carry:

**1 · The registry is the authorisation boundary.** An agent can only run what is
registered. Per-tenant registries are a real permission model, not a prompt
instruction — nothing the model says can add a capability.

**2 · The plan is the execution boundary.** `run_capability` requires a matching
`capability` step with satisfied dependencies that has not already run. Even a
registered capability cannot run outside an authorised step.

**3 · The session is the filesystem boundary.** Every source ref is resolved with
symlink-aware containment and integrity checks. `control/` is unreachable from
any ref kind.

**4 · The broker is the rate boundary.** Per-turn budgets, repeat detection, and
the awaiting-inputs gate bound how much a confused or adversarial loop can do.

### What is not a boundary

**The system prompt.** Prompt instructions are guidance; they are not enforcement
and should not be treated as such. Anything that must not happen belongs in the
registry or the broker.

**`continue_prompt`.** Model-authored text in a file request. Hosts must not
forward it as if the user typed it — verify the request id and send your own
fixed continuation string.

**Runner sandboxing.** LoomCraft validates *what* runs and with which inputs; it
does not sandbox your runner code. A runner that shells out with untrusted input
is your problem to contain (containers, seccomp, whatever fits).

---

## Known limits

Honest list.

**Single-process engine.** Runs live in one asyncio loop. Multi-worker
deployments need session affinity, or an engine backed by a distributed queue —
the `Engine` interface is small enough to reimplement, but LoomCraft does not
ship that.

**Plan size.** The contract accepts 256 steps, but 24 is the size a reviewer can
read at a glance, and a plan approaching it usually wants a workflow for the
fixed parts. The ceiling exists for generated graphs; treat the smaller number
as the design target and expect the renderer to need scrolling past it.

**Lexical capability search.** Fine for tens of capabilities, weak at hundreds.
Subclass `Registry.search` for embeddings — see
[Extending](05-extending.md#custom-capability-search).

**No cross-session memory.** Each session is independent. Persisting learnings
across sessions is a host concern.

**No built-in cost accounting.** `TurnResult.usage` carries token counts; budget
enforcement is yours to add.

**Artifacts are files.** No streaming or chunked artifacts. Large outputs should
be written to your own object store by the runner, with a small manifest emitted
as the artifact.

**One approval model.** Node-level approve/reject. Multi-party approval, or
approval with an edit, needs a custom runner that models it.

Next: [API reference](07-api-reference.md).
