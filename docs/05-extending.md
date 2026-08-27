# Extending LoomCraft

Adding your domain, and replacing the parts you need to replace.

- [Writing runners](#writing-runners)
- [Declaring capabilities](#declaring-capabilities)
- [Composing workflows](#composing-workflows)
- [Organising a large catalog](#organising-a-large-catalog)
- [Custom capability search](#custom-capability-search)
- [Long-running and blocking work](#long-running-and-blocking-work)
- [Human-in-the-loop](#human-in-the-loop)
- [Custom storage](#custom-storage)
- [Custom transports](#custom-transports)
- [Custom agents](#custom-agents)
- [Custom tools](#custom-tools)
- [Observability](#observability)
- [Testing your extensions](#testing-your-extensions)
- [Production checklist](#production-checklist)

---

## Writing runners

A runner is any `async def run(ctx: NodeContext) -> NodeResult`.

```python
from loomcraft import NodeContext, NodeResult

async def summarise(ctx: NodeContext) -> NodeResult:
    document = ctx.input("document")           # declared, resolved, verified
    limit = ctx.parameters["max_sentences"]    # validated against the contract

    ctx.log(f"summarising {document.filename}", "info")
    ctx.progress(0.3, "reading")

    text = document.read_text()
    ctx.raise_if_cancelled()                   # cheap cancellation checkpoint

    summary = do_work(text, limit)
    ctx.emit("summary", "summary.md", summary)
    return NodeResult.ok(sentences=len(summary.splitlines()))
```

### The context

| Member | Purpose |
| --- | --- |
| `ctx.input(key)` | The single file bound to `key`; raises if absent or multi-valued |
| `ctx.input_list(key)` | All files for a multi-file input |
| `ctx.optional_input(key)` / `ctx.has_input(key)` | Optional inputs |
| `ctx.parameters` | Validated parameters with defaults applied |
| `ctx.config` | Fixed, non-model-writable runner configuration |
| `ctx.workdir` | Private per-attempt scratch directory |
| `ctx.attempt` | 1 on the first try, 2 on the first retry, … |
| `ctx.log(msg, level)` | Streamed when the engine has `stream_logs=True` |
| `ctx.progress(fraction, msg)` | Streamed as `execution_progress` |
| `ctx.emit(port, filename, data)` | Write bytes as an artifact |
| `ctx.emit_path(port, path)` | Register an already-written file |
| `ctx.cancelled` / `ctx.raise_if_cancelled()` | Cancellation checkpoints |

The context deliberately cannot reach the plan or other nodes. A runner reads its
inputs, writes artifacts, and returns; the engine assigns status.

### Choosing a result

```python
NodeResult.ok(rows=120)                    # succeeded
NodeResult.fail("no header row")           # permanent — do NOT retry
NodeResult.retry("upstream returned 503")  # transient — retry if budget remains
NodeResult.needs_approval("about to send") # park for a human
```

Getting `fail` vs `retry` right matters more than it looks. A malformed input is
`fail` — re-running it cannot help, and retrying wastes the budget while delaying
the agent's replan. A rate limit is `retry`.

Anything a runner raises becomes a non-retryable failure with the exception type
in the message, so an unexpected bug does not silently retry three times.

### Cancellation

Cancellation arrives as `asyncio.CancelledError` at the next `await`. For
CPU-bound loops with no natural await point, poll:

```python
for index, chunk in enumerate(chunks):
    if ctx.cancelled:
        return NodeResult.fail("cancelled")
    process(chunk)
    if index % 100 == 0:
        ctx.progress(index / len(chunks))
        await asyncio.sleep(0)      # yield so cancellation can land
```

---

## Declaring capabilities

```python
from loomcraft import Capability, CapabilityInput, Parameter, Port, Registry

registry = Registry()

SUMMARISE = Capability(
    id="lit.harmonise",                    # ^[a-z][a-z0-9_.-]{1,159}$
    name="Summarise a document",
    version="1",
    description="Extractive summary by sentence scoring.",   # the agent reads this
    runner="lit.harmonise",
    inputs=(CapabilityInput(
        key="document",
        name="Document",
        description="A text or Markdown document.",
        allowed_extensions=(".txt", ".md"),
        max_files=1,
    ),),
    outputs=(Port(name="summary", artifact_type="md"),),
    parameters={"max_sentences": Parameter(
        type="integer", description="Sentences to keep.",
        minimum=1, maximum=20, default=5,
    )},
    max_attempts=3,
    retry_backoff_seconds=1.0,
    timeout_seconds=300,
    requires_approval=False,
    tags=("summary", "documents", "nlp"),
)

registry.register_runner("lit.harmonise", summarise)
registry.register_capability(SUMMARISE)
```

Or both at once:

```python
@registry.capability_runner(SUMMARISE)
async def summarise(ctx: NodeContext) -> NodeResult:
    ...
```

### Descriptions are the agent's UI

`description` and `tags` are how a model finds your capability and decides
whether it fits. Write them for a competent colleague who has not seen your
codebase.

| ✗ | ✓ |
| --- | --- |
| "Runs the summariser" | "Extractive summary by sentence scoring. Deterministic and auditable — does not paraphrase." |
| "Processes data" | "Drops blank rows, trims whitespace, normalises headers to snake_case, and optionally de-duplicates." |

State what it does **not** do too. "Does not paraphrase" saves the agent from
using it where abstraction was wanted.

### Input variants

```python
inputs=(
    CapabilityInput(key="bed", ..., allowed_extensions=(".bed",)),
    CapabilityInput(key="bim", ..., allowed_extensions=(".bim",)),
    CapabilityInput(key="fam", ..., allowed_extensions=(".fam",)),
    CapabilityInput(key="vcf", ..., allowed_extensions=(".vcf",)),
),
input_variants=(("bed", "bim", "fam"), ("vcf",)),
```

A PLINK triple *or* a VCF. Half a variant is refused; mixing variants is refused.
Inputs outside the matched variant are still accepted as optional context — which
is how `gwas.summarise` takes an optional `components` file while requiring
`qc_report` + `annotated`.

### Parameters

| `type` | Extra validation |
| --- | --- |
| `integer` / `number` | `minimum`, `maximum` |
| `string` | non-empty, ≤2000 chars, `enum` |
| `boolean` | strict bool |
| `array` | ≤256 entries |
| `object` | ≤64 KiB when serialised |

Anything the model must not set belongs in `config`, not `parameters`.

### Validate at startup

```python
problems = registry.validate()
if problems:
    raise SystemExit("\n".join(problems))
```

Fail fast. A capability pointing at a missing runner is a bug the agent would
otherwise discover mid-plan, after the user has waited.

---

## Composing workflows

Use a workflow when a sequence must not vary, or when you want the engine's
internal parallelism — the broker refuses two overlapping agent-initiated
executions, so concurrent execution lives inside a single graph.

```python
from loomcraft import Workflow, WorkflowNode

registry.register_workflow(Workflow(
    id="docs.full_review",
    name="Full document review",
    description="Extract, then summarise and theme in parallel, then brief.",
    inputs=(CapabilityInput(key="documents", name="Documents",
                            description="1–6 documents.", max_files=6),),
    nodes=(
        WorkflowNode(id="extract", name="Extract", runner="lit.extract",
                     inputs=("documents",),
                     outputs=(Port(name="corpus", artifact_type="json"),)),
        # Same single dependency ⇒ the engine runs these two concurrently.
        WorkflowNode(id="summary", name="Summarise", runner="lit.harmonise",
                     depends_on=("extract",),
                     outputs=(Port(name="summary", artifact_type="md"),)),
        WorkflowNode(id="themes", name="Themes", runner="lit.influence",
                     depends_on=("extract",),
                     outputs=(Port(name="themes", artifact_type="json"),)),
        WorkflowNode(id="brief", name="Brief", runner="lit.brief",
                     depends_on=("summary", "themes")),
    ),
))
```

A node receives its dependencies' artifacts keyed by the emitting **port name**,
so `brief` reads `ctx.input("summary")` without knowing which node produced it.
Name ports after what they contain, not after the node.

---

## Organising a large catalog

Split by domain and merge:

```python
# capabilities/genomics.py
registry = Registry()
@registry.capability_runner(...)
async def plink_qc(ctx): ...

# capabilities/__init__.py
from loomcraft import merge_registries
from . import genomics, phenotype, reporting

registry = merge_registries(genomics.registry, phenotype.registry, reporting.registry)
assert not registry.validate()
```

**Give different users different catalogs.** The registry is the authorisation
boundary: an agent cannot run what is not registered, so per-tenant registries
are a real permission model rather than a prompt instruction.

```python
def registry_for(user) -> Registry:
    parts = [base.registry]
    if user.can_write:
        parts.append(mutating.registry)
    return merge_registries(*parts)
```

---

## Custom capability search

The built-in search is lexical. For a large catalog, subclass:

```python
class EmbeddingRegistry(Registry):
    def __init__(self, embedder):
        super().__init__()
        self._embedder = embedder
        self._vectors: dict[str, list[float]] = {}

    def register_capability(self, capability, *, replace=False):
        result = super().register_capability(capability, replace=replace)
        self._vectors[capability.id] = self._embedder.embed(
            f"{capability.name}. {capability.description} {' '.join(capability.tags)}"
        )
        return result

    def search(self, query, *, scope="all", limit=5):
        query_vector = self._embedder.embed(query)
        ranked = sorted(
            self.capabilities.values(),
            key=lambda item: -cosine(query_vector, self._vectors[item.id]),
        )
        return [item.contract() for item in ranked[:limit]]
```

The broker only needs a ranked list of contracts.

---

## Long-running and blocking work

The engine is asyncio. **Never block the event loop** — a synchronous 30-second
call stalls every other node in the process.

```python
import asyncio

async def heavy(ctx: NodeContext) -> NodeResult:
    result = await asyncio.to_thread(cpu_bound_analysis, ctx.input("table").path)
    ctx.emit("result", "result.json", result)
    return NodeResult.ok()
```

For subprocesses, use the async API and kill the whole process group on
cancellation — otherwise a cancelled run leaves orphans:

```python
import os, signal

async def external_tool(ctx: NodeContext) -> NodeResult:
    process = await asyncio.create_subprocess_exec(
        "plink", "--bfile", str(ctx.input("bed").path.with_suffix("")),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=ctx.workdir, start_new_session=True,
    )
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        raise
    if process.returncode != 0:
        return NodeResult.fail(f"plink exited {process.returncode}: {stderr.decode()[:500]}")
    ctx.emit_path("output", ctx.workdir / "plink.out")
    return NodeResult.ok()
```

For work that outlives a turn, have the runner poll an external job and return
`retry` while it is pending — but set `timeout_seconds` so it cannot poll forever.

---

## Human-in-the-loop

Declare `requires_approval=True` and return early **before the side effect**:

```python
PUBLISH = Capability(id="report.publish", ..., requires_approval=True)

@registry.capability_runner(PUBLISH)
async def publish(ctx: NodeContext) -> NodeResult:
    if ctx.attempt == 1 and not ctx.config.get("approved"):
        return NodeResult.needs_approval(
            f"about to publish {ctx.input('report').filename} to the shared space"
        )
    do_the_irreversible_thing()
    return NodeResult.ok()
```

The run parks in `paused_approval` and emits `approval_required`. Resolve it:

```python
run.approve("execute", True)     # or False to reject
```

Rejection fails the node and skips everything downstream, which is usually what
you want — the plan reflects that the outcome did not happen.

Gate on **reversibility**, not importance: outward-facing sends, deletions,
production writes, anything with a cost.

---

## Custom storage

`Session` is a class, not an interface — subclass it to move bytes elsewhere
while keeping the manifest logic:

```python
class S3Session(Session):
    def save_upload(self, filename, data, *, content_type=None):
        record = super().save_upload(filename, data, content_type=content_type)
        s3.upload_file(str(self.uploads_dir / record["id"] / record["filename"]),
                       BUCKET, f"{self.id}/{record['id']}")
        return record

class S3SessionStore(SessionStore):
    def _build(self, session_id):
        session = S3Session(session_id, self.root / session_id)
        self._cache[session_id] = session
        return session
```

For a fully custom backend, the surface a `ToolBroker` and `Engine` actually
require is small:

```python
class MySession:
    id: str
    root: Path
    events: EventLog
    def meta(self) -> dict: ...
    def update_meta(self, **fields) -> dict: ...
    def list_uploads(self) -> list[dict]: ...
    def save_upload(self, filename, data, *, content_type=None) -> dict: ...
    def delete_upload(self, upload_id) -> dict | None: ...
    def current_plan(self) -> dict | None: ...
    def plan_history(self) -> list[dict]: ...
    def publish_plan(self, plan) -> dict: ...
    def update_current_plan(self, plan) -> dict: ...
    def list_executions(self) -> list[dict]: ...
    def record_execution(self, execution) -> dict: ...
    def list_artifacts(self) -> list[dict]: ...
    def get_artifact(self, artifact_id) -> tuple[dict, Path] | None: ...
    def add_artifact(self, source, **kwargs) -> dict: ...
    def register_scratch_artifacts(self, entries, *, step_id=None) -> list[dict]: ...
    def resolve_source(self, source_ref) -> ResolvedSource: ...
    def run_dir(self, run_id) -> Path: ...
    def emit(self, event, data) -> Event: ...
    def history(self, *, after_seq=0) -> dict: ...
```

**Keep the containment and integrity checks in `resolve_source`.** They are the
security boundary, not an optimisation.

### Custom event log

Subclass `EventLog` to mirror events into Kafka, Postgres, or a tracing system:

```python
class MirroredEventLog(EventLog):
    def append(self, event, data=None):
        record = super().append(event, data)     # durable first
        kafka.produce("loomcraft.events", record.to_dict())
        return record
```

Keep the local durable write. It is what `after_seq` resume reads from.

---

## Custom transports

`create_router` is a reference implementation, not a requirement. To host
LoomCraft elsewhere, wire the same three things:

```python
# 1 · Start the turn in the BACKGROUND
task = asyncio.create_task(agent.run_turn(broker, message, on_event=sink))

# 2 · Subscribe to events; unsubscribing must NOT cancel the task
unsubscribe = session.events.subscribe(lambda event: queue.put_nowait(event))

# 3 · Let clients resume from a cursor
session.events.read(after_seq=client_cursor)
```

The background/subscribe split is the important part. If the turn's lifetime is
tied to the HTTP connection, a user switching tabs kills a ten-minute analysis.

WebSocket instead of SSE:

```python
@app.websocket("/ws/{session_id}")
async def websocket(ws: WebSocket, session_id: str):
    await ws.accept()
    session = store.get(session_id)
    queue: asyncio.Queue = asyncio.Queue()
    unsubscribe = session.events.subscribe(queue.put_nowait)
    try:
        for event in session.events.read(after_seq=0):
            await ws.send_json(event.to_dict())
        while True:
            await ws.send_json((await queue.get()).to_dict())
    finally:
        unsubscribe()
```

---

## Custom agents

Anything satisfying the `Agent` protocol works:

```python
class RoutingAgent:
    """Cheap model for simple asks, strong model for planning."""

    def __init__(self, fast, strong):
        self.fast, self.strong = fast, strong

    async def run_turn(self, broker, message, *, history=None, on_event=None):
        agent = self.strong if len(message) > 200 or "compare" in message else self.fast
        return await agent.run_turn(broker, message, history=history, on_event=on_event)
```

Or wrap for policy — this is where an approval gate on *tool calls* belongs:

```python
class GatedAgent:
    def __init__(self, inner, allowed: set[str]):
        self.inner, self.allowed = inner, allowed

    async def run_turn(self, broker, message, **kwargs):
        original = broker.dispatch

        async def gated(name, payload=None):
            if name == "run_capability" and payload.get("capability_id") not in self.allowed:
                from loomcraft.broker import ToolResponse
                return ToolResponse(ok=False, error="not permitted for this user",
                                    error_code="CAPABILITY_FORBIDDEN")
            return await original(name, payload)

        broker.dispatch = gated                      # type: ignore[method-assign]
        try:
            return await self.inner.run_turn(broker, message, **kwargs)
        finally:
            broker.dispatch = original               # type: ignore[method-assign]
```

Prefer a scoped registry where you can — it is declarative and cannot be bypassed.

---

## Custom tools

Add domain tools by extending the specs and the broker together:

```python
from loomcraft import ToolSpec, tool_specs
from loomcraft.broker import ToolBroker, ToolResponse

QUERY_WAREHOUSE = ToolSpec(
    name="query_warehouse",
    description="Run one read-only, parameterised query against the warehouse.",
    parameters={
        "type": "object",
        "properties": {
            "query_id": {"type": "string", "enum": ["daily_sales", "inventory"]},
            "parameters": {"type": "object"},
        },
        "required": ["query_id"],
        "additionalProperties": False,
    },
)

class WarehouseBroker(ToolBroker):
    async def _route(self, name, payload):
        if name == "query_warehouse":
            return self._query(payload)
        return await super()._route(name, payload)

    def _query(self, payload) -> ToolResponse:
        # Named, pre-written queries — never model-authored SQL.
        rows = WAREHOUSE_QUERIES[payload["query_id"]](payload.get("parameters", {}))
        return ToolResponse(ok=True, result={"rows": rows[:100]})

agent = AnthropicAgent(tools=[*tool_specs(), QUERY_WAREHOUSE])
```

Two rules that keep a custom tool from becoming the hole in the boundary:
enumerate what it can do (an `enum`, not free text), and bound what it returns
(the result goes into the model's context).

---

## Observability

```python
def instrumented_emit(name: str, data: dict) -> None:
    metrics.increment(f"loomcraft.event.{name}")
    if name == "execution_finished":
        execution = data["execution"]
        metrics.timing("loomcraft.execution.duration",
                       execution["duration_seconds"],
                       tags=[f"capability:{execution['capability']}",
                             f"status:{execution['status']}"])
    return session.emit(name, data)

engine = Engine(registry, session, emit=instrumented_emit, stream_logs=True)
broker = ToolBroker(session, registry, engine=engine)
```

Worth alerting on:

| Signal | Why |
| --- | --- |
| `BROKER_ACTION_LIMIT_EXCEEDED` rate | Agents looping — usually a prompt or contract problem |
| Plan revisions per session | High counts mean the first plan is consistently wrong |
| `retryable` failures per capability | A flaky dependency |
| Time from `plan_published` to `done` | End-to-end latency |
| `session.events.verify()` failures | Storage corruption or tampering |

---

## Testing your extensions

```python
import unittest, tempfile
from pathlib import Path
import loomcraft as lc

class TestSummarise(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = lc.SessionStore(
            Path(self._tmp.name), in_memory_events=True,
        ).create("t")
        self.engine = lc.Engine(registry, self.session, emit=lambda *_: None)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_produces_a_summary(self):
        upload = self.session.save_upload("doc.md", b"One. Two. Three.")
        run = await self.engine.execute(lc.graph_from_capability(
            registry.capability("lit.harmonise"),
            sources={"document": (upload["source_ref"],)},
            parameters={"max_sentences": 2},
        ))
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(len(run.artifacts), 1)
```

Test the *contract* too — it is what the agent sees:

```python
def test_rejects_a_partial_variant(self):
    with self.assertRaises(lc.ContractError):
        registry.capability("geno.qc").validate_inputs({"bed": "upload:1"})
```

And test the whole path with `ScriptedAgent`, which needs no model or network.

---

## Production checklist

- [ ] `registry.validate()` at startup, and fail on problems
- [ ] `timeout_seconds` on every capability that touches the network or a subprocess
- [ ] `max_attempts` set deliberately; `fail` vs `retry` chosen correctly in runners
- [ ] `requires_approval` on everything hard to reverse
- [ ] Blocking work moved off the event loop with `asyncio.to_thread`
- [ ] Subprocesses started with `start_new_session=True` and killed by process group
- [ ] `BrokerLimits` tuned; alerting on budget-exceeded rates
- [ ] Per-tenant registries where users should see different capabilities
- [ ] Session storage on a volume with a retention and cleanup policy
- [ ] Upload limits (`max_upload_bytes`, `max_session_bytes`) sized for your data
- [ ] Event logs mirrored somewhere durable if you need long-term audit
- [ ] `session.events.verify()` in a periodic audit job
- [ ] Turns started in the background, never tied to the HTTP connection
- [ ] `continue_prompt` never forwarded as user-authored input

Next: [Architecture](06-architecture.md).
