# loomcraft

The Python engine: plan validation, DAG execution, the agent tool surface, and
session state.

```bash
# Not on PyPI — install from the repository.
pip install "git+git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"                       # engine only (pydantic)
pip install "loomcraft[server] @ git+git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"   # + FastAPI router with SSE
pip install "loomcraft[anthropic] @ git+git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"  # + Claude agent loop
pip install "loomcraft[openai] @ git+git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"   # + OpenAI-compatible agent loop
pip install "loomcraft[all] @ git+git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"      # everything
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
python -m unittest discover -s tests
```

187 tests on the standard library — no pytest required.
