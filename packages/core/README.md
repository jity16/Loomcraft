# loomcraft

The Python engine: validated Plans, DAG execution, provider-neutral agent tools,
secure session state, and compatibility adapters for earlier APIs.

```bash
# Not on PyPI — install from the repository.
pip install "git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"                       # engine only (pydantic)
pip install "loomcraft[server] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"   # + FastAPI router with SSE
pip install "loomcraft[anthropic] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"  # + Claude agent loop
pip install "loomcraft[openai] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"   # + OpenAI-compatible agent loop
pip install "loomcraft[all] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"      # everything
```

```python
from loomcraft import (
    AnthropicAgent, Capability, CapabilityInput, NodeContext, NodeResult,
    Port, Registry, SessionStore, ToolBroker,
)

registry = Registry()

@registry.capability_runner(Capability(
    id="gwas.pca",
    name="Principal components of ancestry",
    description="Project samples onto the leading axes of genotype variation.",
    runner="gwas.pca",
    inputs=(CapabilityInput(key="cohort", name="Cohort",
                            description="A QC'd genotype matrix.",
                            allowed_extensions=(".tsv",)),),
    outputs=(Port(name="components", artifact_type="json"),),
))
async def pca(ctx: NodeContext) -> NodeResult:
    ctx.emit("components", "pca.json", decompose(ctx.input("cohort").read_text()))
    return NodeResult.ok()

session = SessionStore("./data").create()
broker = ToolBroker(session, registry)
await AnthropicAgent().run_turn(broker, "Find markers associated with salt tolerance.")
```

For a normalized provider (OpenAI Chat/Responses, a JSONL subprocess, or a
scripted test model), use `AIProvider` + `PlannerAgent`. The optional
`execute_plan` tool schedules a complete published Plan through the same
`Engine` driver used by `run_capability` and `run_workflow`; it is not a second
execution implementation.

Full documentation: <https://github.com/jity16/Loomcraft/tree/main/docs>

## Tests

```bash
python -m pytest -q packages/core/tests
```

The typed contract suite runs in `packages/core/tests`; the repository-level
compatibility suite is in `tests/`. Provider and FastAPI dependencies remain
optional.
