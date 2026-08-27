# Examples

Two runnable scenarios. Both work with **no API key** — a scripted agent replays
the exact tool calls a model would make, and every one still goes through the
real broker, engine, and event log.

```bash
pip install -e packages/core          # or: pip install loomcraft

python examples/01-data-pipeline/run_scripted.py
python examples/02-research-assistant/run_scripted.py
```

## What each one covers

| Capability | Example 1 | Example 2 |
| --- | :---: | :---: |
| DAG validation (cycles, unknown deps, duplicate ids) | ✅ | |
| Dependency layering → parallel scheduling | ✅ | ✅ |
| Real concurrent execution inside one graph | ✅ | |
| Dependency gating (no jumping ahead) | ✅ | |
| Typed input contracts and input variants | ✅ | ✅ |
| Parameter validation (types, ranges, enums) | ✅ | |
| Retry with exponential backoff | ✅ | |
| Timeouts | ✅ | |
| Human approval before a hard-to-reverse step | ✅ | |
| Genuine step failure | ✅ | ✅ |
| Skip propagation to the downstream subtree | ✅ | ✅ |
| Replan discipline (increasing revision + reason) | ✅ | ✅ |
| Structured file requests + execution gating | | ✅ |
| Upload allocation across typed slots | | ✅ |
| Artifact reuse across a replan | | ✅ |
| Agent-reported `review` / `answer` steps | ✅ | ✅ |
| Hash-chained audit log | ✅ | ✅ |
| HTTP + SSE server | ✅ | |
| Browser UI | ✅ | |
| Live Claude agent | | ✅ |

## [01 · Data pipeline](01-data-pipeline/)

A CSV quality toolkit: clean → (profile ‖ outliers) → report, with a flaky
reference lookup and a publish step behind human approval.

```bash
python examples/01-data-pipeline/run_scripted.py      # 13 annotated sections

pip install 'loomcraft[server]'
python examples/01-data-pipeline/serve.py --scripted  # http://127.0.0.1:8000
```

The browser UI in `web/index.html` is a single dependency-free file that ports
the reducer and layout from `@loomcraft/renderer`. Diff it against
`packages/renderer/src/state.ts` to see how small the front-end contract is.

## [02 · Research assistant](02-research-assistant/)

A document research agent that has to ask for what it is missing, and replan
when a step legitimately cannot run.

```bash
python examples/02-research-assistant/run_scripted.py   # both branches

pip install 'loomcraft[anthropic]'
export ANTHROPIC_API_KEY=...                            # or: ant auth login
python examples/02-research-assistant/run_live.py
python examples/02-research-assistant/run_live.py --one-document --decline
```

`run_live.py` and `run_scripted.py` share the same capabilities, broker, and
session. The only difference is who chooses the next tool call — which is the
argument for developing against the scripted agent and switching one line for
production.
