# loomcraft

The Python engine: plan validation, DAG execution, the agent tool surface, and
session state.

```bash
pip install loomcraft                        # engine only (pydantic)
pip install 'loomcraft[server]'              # + FastAPI router with SSE
pip install 'loomcraft[anthropic]'           # + Claude agent loop
pip install 'loomcraft[openai]'              # + OpenAI-compatible agent loop
pip install 'loomcraft[all]'                 # everything
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
