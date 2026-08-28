# Examples

Every example uses the public LoomCraft contracts. The scripted scenarios run
offline, so they are suitable for CI and for learning the event protocol before
connecting a real model.

## Coverage map

| Example | What it demonstrates |
| --- | --- |
| [`01-gwas-discovery`](01-gwas-discovery/) | DAG validation, independent branches running in parallel, typed capability contracts, artifacts, review, re-plan and live SSE/renderer output. |
| [`02-literature-meta`](02-literature-meta/) | Structured file requests, extension/cardinality matching, execution gating, failure/skip propagation and a real domain-level re-plan. |
| [`python/retry_parallel.py`](python/retry_parallel.py) | Generic parallel scheduling, exponential retry, timeout and artifact events without domain code. |
| [`python/ai_planning.py`](python/ai_planning.py) | Provider-neutral tool calls, `publish_plan`/`execute_plan`, scripted AI loop and message history. |
| [`python/approval_pause.py`](python/approval_pause.py) | Human approval pause/resume and downstream preservation. |
| [`python/input_request.py`](python/input_request.py) | Input requests, allocation and fulfillment/invalidation events. |
| [`web/`](web/) | React state reduction, SSE consumption and the reusable workbench. |

## Run the offline examples

```bash
python examples/01-gwas-discovery/run_scripted.py
python examples/02-literature-meta/run_scripted.py
python examples/python/retry_parallel.py
python examples/python/ai_planning.py
python examples/python/approval_pause.py
python examples/python/input_request.py
```

The examples do not import the original business application. Domain runners
are registered through `Registry`, while the engine owns validation, scheduling,
retry, state transitions and event emission.

For the HTTP demo, install the optional server extra and run the `serve.py` file
inside either scientific example. The browser client can reconnect with the last
event sequence number and rebuild state from the same event history.
