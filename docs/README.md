# LoomCraft documentation

| Guide | Read it for |
| --- | --- |
| [01 · Concepts](01-concepts.md) | The model: plans, step kinds, capabilities, sessions, source refs, events |
| [02 · Defining plans](02-defining-plans.md) | Plan schema, every validation rule, the step state machine, replanning |
| [03 · Agent integration](03-agent-integration.md) | The ten tools, the loop, Claude/OpenAI/MCP, prompting, guardrails |
| [04 · Frontend integration](04-frontend-integration.md) | The reducer, SSE, components, theming, custom and non-React UIs |
| [05 · Extending](05-extending.md) | Runners, capabilities, workflows, storage, transports, agents, production |
| [06 · Architecture](06-architecture.md) | Design decisions and why the obvious alternatives fail |
| [07 · API reference](07-api-reference.md) | Every public symbol, tool, event, and endpoint |

## Reading paths

**"I want to try it."** → [README quick start](../README.md#quick-start), then
`python examples/01-data-pipeline/run_scripted.py`.

**"I'm adding my domain."** → [Concepts](01-concepts.md) §Capability →
[Extending](05-extending.md) §Writing runners → the
[production checklist](05-extending.md#production-checklist).

**"I'm wiring up a model."** → [Agent integration](03-agent-integration.md), then
[Defining plans](02-defining-plans.md) for what the server will accept.

**"I'm building the UI."** → [Frontend integration](04-frontend-integration.md),
and [`examples/01-data-pipeline/web/index.html`](../examples/01-data-pipeline/web/index.html)
for a dependency-free port of the protocol.

**"I want to know why it works this way."** → [Architecture](06-architecture.md).
