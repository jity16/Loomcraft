# loomcraft

The Python engine: plan validation, DAG execution, the agent tool surface, and
session state.

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

Full documentation: <https://github.com/jity16/Loomcraft/tree/main/docs>

## Tests

```bash
python -m pytest -q packages/core/tests
```

The core contract suite currently contains 257 tests. Provider and FastAPI
dependencies remain optional, so the engine can be embedded without a web
framework or a model SDK.
