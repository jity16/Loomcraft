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
    id="csv.profile",
    name="Profile a CSV",
    description="Column types, null counts, and basic statistics.",
    runner="csv.profile",
    inputs=(CapabilityInput(key="table", name="Table",
                            description="A CSV with a header row.",
                            allowed_extensions=(".csv",)),),
    outputs=(Port(name="profile", artifact_type="json"),),
))
async def profile(ctx: NodeContext) -> NodeResult:
    ctx.emit("profile", "profile.json", analyse(ctx.input("table").read_text()))
    return NodeResult.ok()

session = SessionStore("./data").create()
broker = ToolBroker(session, registry)
await AnthropicAgent().run_turn(broker, "Profile the uploaded table.")
```

Full documentation: <https://github.com/jity16/Loomcraft/tree/main/docs>

## Tests

```bash
python -m unittest discover -s tests
```

187 tests on the standard library — no pytest required.
