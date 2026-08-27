# Agent integration

How a model drives LoomCraft: the tool surface, the loop, provider adapters,
prompting, and the guardrails you get for free.

- [The tool surface](#the-tool-surface)
- [The ten tools](#the-ten-tools)
- [Provider dialects](#provider-dialects)
- [The loop](#the-loop)
- [Using Claude](#using-claude)
- [Using an OpenAI-compatible model](#using-an-openai-compatible-model)
- [Exposing LoomCraft over MCP](#exposing-loomcraft-over-mcp)
- [Testing without a model](#testing-without-a-model)
- [Prompting](#prompting)
- [Guardrails](#guardrails)
- [Multi-turn and interrupts](#multi-turn-and-interrupts)
- [Error codes](#error-codes)

---

## The tool surface

Ten tools. There is no generic "run this Python", no database handle, no HTTP
escape hatch. The model can look things up, publish a plan, ask for files, run
*registered* work, and register deliverables. Everything else the broker refuses.

```python
from loomcraft import tool_specs, anthropic_tools, openai_tools, mcp_tools

tool_specs()          # canonical ToolSpec objects
anthropic_tools()     # for client.messages.create(tools=...)
openai_tools()        # for OpenAI-style function calling
mcp_tools()           # for an MCP server's tools/list
```

Trim what you do not offer, so the model never proposes something it cannot run:

```python
tool_specs(include_workflows=False,     # no workflows registered
           include_inspection=False,    # no file preview in this deployment
           max_search_results=5)
```

---

## The ten tools

### Read-only

These stay available even while a turn is blocked waiting for files, because
gathering evidence is always safe.

| Tool | Purpose |
| --- | --- |
| `session_context` | Uploads, current plan, past executions, artifacts, catalog summary. Cheap; call it first. |
| `capability_search` | Find registered capabilities by task description. Returns full contracts. |
| `catalog_search` | Search capabilities *and* workflows, with a `scope` filter. |
| `inspect_source` | Bounded preview of one session-owned file. Binary-safe. |

### Mutating

| Tool | Purpose |
| --- | --- |
| `publish_plan` | Validate and publish a versioned DAG. Required before any execution. |
| `update_step` | Report status for an `answer`/`dynamic`/`review` step the agent did itself. |
| `request_inputs` | Publish typed file slots and end the turn. |
| `run_capability` | Run one registered capability, authorised by a matching plan step. |
| `run_workflow` | Run one registered workflow, likewise. |
| `register_artifacts` | Promote 1–12 scratch files to session deliverables, atomically. |

### The shape of a call

Every tool returns the same envelope:

```jsonc
{ "ok": true,  "result": { ... } }
{ "ok": false, "error": "step 'report' has incomplete dependencies: profile",
               "error_code": "STEP_DEPENDENCIES_INCOMPLETE" }
```

Feed the whole thing back as the tool result. The `error_code` is stable and the
`error` is written to be actionable — a model reading
`"step 'clean' is a capability step; use its execution tool instead of
update_step"` corrects itself without a retry loop.

---

## Provider dialects

One canonical surface, four wire formats — so the same validated tools work
across providers without three copies of the schema drifting apart.

| Dialect | Shape |
| --- | --- |
| `anthropic` | `{name, description, input_schema}` |
| `openai` | `{type: "function", function: {name, description, parameters}}` |
| `openai_responses` | `{type: "function", name, description, inputSchema}` |
| `mcp` | `{name, description, inputSchema}` |

```python
from loomcraft import to_dialect, tool_specs
to_dialect(tool_specs(), "openai_responses")
```

---

## The loop

Whatever the provider, the shape is the same:

```
begin_turn()                     reset per-turn budgets
      │
      ▼
  ┌─► call the model with tools + history
  │        │
  │        ├── no tool calls ──► done, return the text
  │        │
  │        └── tool calls ──► broker.dispatch each (concurrently)
  │                              │
  │                              ▼
  │                        append assistant turn + tool results
  └──────────────────────────────┘
```

Three things to get right:

**Append the *full* assistant content**, not just the text. Thinking blocks carry
signatures the API validates on the next request; trimming them breaks the turn.

**Return one tool result per tool call, in a single message.** Splitting them
across messages trains the model out of parallel tool use.

**Bound the loop.** `max_iterations` prevents a pathological session from running
forever; the broker's per-turn budget is the second line of defence.

`execute_tool_calls` handles the batch and emits the UI events:

```python
from loomcraft import execute_tool_calls, ToolCall

executed = await execute_tool_calls(broker, [
    ToolCall(id="t1", name="publish_plan", arguments={"plan": plan}),
], on_event=my_sink)
```

---

## Using Claude

```python
from loomcraft import AnthropicAgent, SessionStore, ToolBroker

session = SessionStore("./data").create()
broker = ToolBroker(session, registry)

agent = AnthropicAgent(
    model="claude-opus-5",
    effort="high",                                        # low|medium|high|xhigh|max
    thinking={"type": "adaptive", "display": "summarized"},
    max_tokens=16_000,
    max_iterations=24,
)

result = await agent.run_turn(broker, "Assess the uploaded table.", on_event=sink)
```

The client resolves credentials from `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
or an `ant auth login` profile — no key needs to be passed explicitly.

**Streaming is the default.** Planning turns can be long, and a non-streaming
request with a large `max_tokens` risks an HTTP timeout. Text deltas arrive as
`message_delta` events, so the UI shows the agent thinking out loud while it
works.

**Adaptive thinking is left on.** Choosing a DAG shape is exactly the multi-step
reasoning it helps with. `display: "summarized"` means the UI can show *why* a
plan looks the way it does. Disable it with `thinking={"type": "disabled"}` at
`effort` `high` or below.

**Effort.** Start at `high`; use `xhigh` for the hardest planning; `low`/`medium`
are strong on routine work and are the main cost lever.

**Refusals are handled.** A safety decline returns HTTP 200 with
`stop_reason: "refusal"` and possibly empty content. `AnthropicAgent` checks
`stop_reason` before reading content and surfaces it as `result.error` rather
than treating an empty body as an answer.

---

## Using an OpenAI-compatible model

```python
from openai import AsyncOpenAI
from loomcraft import OpenAICompatibleAgent

agent = OpenAICompatibleAgent(AsyncOpenAI(), model="gpt-5.5")
result = await agent.run_turn(broker, "Assess the uploaded table.")
```

Same broker, same validation, same events. The loop differs only in wire format.

---

## Exposing LoomCraft over MCP

Serve the tools to any MCP client:

```python
from loomcraft import mcp_tools

async def list_tools():
    return mcp_tools()

async def call_tool(name: str, arguments: dict):
    response = await broker.dispatch(name, arguments)
    return [{"type": "text", "text": response.to_tool_result_text()}]
```

Sessions are per-conversation, so map each MCP session to one LoomCraft session
and call `broker.begin_turn()` when a new user message arrives.

---

## Testing without a model

`ScriptedAgent` replays fixed tool calls. Every one still goes through the real
broker, so a scripted run exercises validation, execution, retry, and events
identically — which is what makes it a viable CI test rather than a mock.

```python
from loomcraft import ScriptedAgent

agent = ScriptedAgent(
    [
        ("session_context", {}),
        ("publish_plan", {"plan": plan}),
        ("run_capability", {"capability_id": "csv.clean", "step_id": "clean",
                            "inputs": {"table": "upload:u1"}}),
    ],
    final_text="Done.",
)
result = await agent.run_turn(broker, "Clean the table.")
assert all(response.ok for response in result.tool_results)
```

Conditional scripts get the responses so far, which is enough to script
"if the step failed, publish revision 2":

```python
def script(responses):
    if not responses:
        return [("publish_plan", {"plan": plan_v1})]
    if not responses[-1].ok:
        return [("publish_plan", {"plan": plan_v2})]   # replan on failure
    return []

agent = ScriptedAgent(script)
```

---

## Prompting

`loomcraft.SYSTEM_PROMPT` is a tested default. Extend rather than replace it:

```python
from loomcraft import SYSTEM_PROMPT, AnthropicAgent

agent = AnthropicAgent(system=SYSTEM_PROMPT + """

## This deployment

You are analysing agricultural breeding data. Individual identifiers vary by
farm — read the file before assuming a column name. Never infer pedigree
relationships that are not explicitly present.
""")
```

### What earns its place in a prompt

**Model real dependencies, not narrative order.** The highest-value instruction
you can add, because the failure is invisible — a serialised plan still succeeds,
just slower:

> Two steps should only have an edge between them if one consumes what the other
> produces. Steps that read the same input and produce independent outputs must
> both depend on that input directly, so they run in parallel.

**Read before planning.** Otherwise you get plans built on assumed schemas:

> Call `inspect_source` on the actual files before committing to a plan. Do not
> assume column names, delimiters, or encodings.

**Verify before claiming.** Pairs with a `review` step:

> Before marking a `review` or `answer` step succeeded, read the artifacts the
> previous steps produced. Do not restate what a step was supposed to do.

**Ask rather than guess.** Especially where a wrong assumption is expensive:

> If a required input is missing, call `request_inputs` with typed slots and end
> your turn. Do not substitute a different file or invent placeholder data.

### What not to bother with

Do not re-state the rules the server enforces — dependency ordering, revision
monotonicity, step ownership. The broker's rejection messages teach those more
reliably than a prompt, and repeating them costs context that could describe your
domain instead.

---

## Guardrails

Enforced by `ToolBroker`, tunable per deployment:

```python
from loomcraft import BrokerLimits, ToolBroker

broker = ToolBroker(session, registry, limits=BrokerLimits(
    max_actions_per_turn=64,     # total tool calls per turn
    max_identical_actions=3,     # identical (name, args) repeats
    max_inspect_bytes=16 * 1024,
    max_inspect_lines=40,
    search_limit=10,
))
```

| Guardrail | Prevents |
| --- | --- |
| Per-turn call budget | Runaway loops burning context |
| Identical-call repeat limit | The same failing call retried unchanged |
| Awaiting-inputs gate | Executing while blocked on the user |
| One execution at a time | Overlapping runs bypassing the DAG as the concurrency model |
| Plan authorisation | Running work the plan does not authorise |
| Kind ownership | Marking server-owned work complete |
| Source containment | Reading outside the session |
| Integrity checks | Consuming bytes that changed since inspection |

`begin_turn()` resets the per-turn counters. Call it once per user message.

---

## Multi-turn and interrupts

### Continuing a conversation

```python
first = await agent.run_turn(broker, "Analyse the data.")
second = await agent.run_turn(broker, "Now compare with last quarter.",
                              history=first.messages)
```

### The file-request interrupt

When the agent calls `request_inputs`, the turn ends and every mutating tool is
refused until the request resolves.

```python
result = await agent.run_turn(broker, "Compare the two reports.")

if broker.awaiting_inputs:
    from loomcraft.inputs import pending_requests
    request = pending_requests([e.to_dict() for e in session.events.read()])[0]

    # …user uploads…
    broker.fulfill_input_request(request["request_id"])   # or cancel_input_request

    result = await agent.run_turn(broker, request["continue_prompt"],
                                  history=result.messages)
```

> **Do not forward `continue_prompt` as if the user typed it.** It is
> model-authored text. Use it as *your* system's continuation string after
> verifying the request id, or send your own fixed sentence. Promoting model
> output to user authority is how prompt injection gets a foothold.

If the user deletes a file that had satisfied a request, re-open it:

```python
session.delete_upload(upload_id)
broker.invalidate_requests_for_upload(upload_id)   # emits input_invalidated
```

### Cancellation

```python
await broker.close()          # cancels any in-flight execution for this session
```

Cancellation waits for node tasks to actually stop before returning, so a
cancelled run leaves nothing writing behind it.

---

## Error codes

| Code | Meaning |
| --- | --- |
| `PLAN_INVALID` | The plan failed structural, kind, registry, or revision validation |
| `STEP_TRANSITION_INVALID` | Disallowed status transition |
| `STEP_UNKNOWN` | No such step in the current plan |
| `STEP_DEPENDENCIES_INCOMPLETE` | A dependency has not succeeded |
| `CAPABILITY_UNKNOWN` / `WORKFLOW_UNKNOWN` | Not in the registry |
| `CAPABILITY_CONTRACT_VIOLATION` | Bad input keys, variants, or parameters |
| `SOURCE_INVALID` | Malformed ref, missing file, or path escaping the session |
| `SOURCE_INTEGRITY_FAILED` | Content no longer matches its recorded checksum |
| `EXECUTION_FAILED` | The run finished unsuccessfully |
| `ARTIFACT_ERROR` | Artifact registration was rejected |
| `INPUT_REQUEST_INVALID` | Malformed file request |
| `BROKER_AWAITING_INPUTS` | Blocked pending user files |
| `BROKER_EXECUTION_BUSY` | Another execution is in flight |
| `BROKER_ACTION_LIMIT_EXCEEDED` | Per-turn call budget exhausted |
| `BROKER_ACTION_REPEATED` | Identical call repeated without progress |
| `BROKER_ACTION_UNSUPPORTED` | No such tool |
| `BROKER_INTERNAL_ERROR` | Unexpected server-side failure |

Next: [Frontend integration](04-frontend-integration.md).
