"""The AI tool loop with a deterministic provider (no API key required)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))

from loomcraft import (  # noqa: E402
    InMemoryStore,
    PlannerAgent,
    Registry,
    ScriptedProvider,
    StepResult,
    ToolBroker,
    ToolCall,
)


async def main() -> None:
    registry = Registry()

    async def inspect(context):
        return StepResult(output={"columns": ["id", "value"]}, summary="table inspected")

    registry.register_capability(id="table.inspect", name="Inspect table", handler=inspect)
    store = InMemoryStore()
    session_id = "ai-demo"
    store.create_session(session_id)
    async def inspect_table(source_ref, options):
        return {"source": {"requested": source_ref}, "shape": {"sample_rows": 2, "columns": 2}, "columns": [{"name": "id"}, {"name": "value"}]}

    broker = ToolBroker(session_id, registry, store=store, table_inspector=inspect_table)

    # A real provider would be OpenAICompatibleProvider or a local JSONL
    # adapter. ScriptedProvider makes the protocol inspectable and reproducible.
    provider = ScriptedProvider([
        {"tool_calls": [{"id": "call-context", "name": "session_context", "arguments": {}}]},
        {"tool_calls": [{"id": "call-publish", "name": "publish_plan", "arguments": {
            "plan": {"goal": "Inspect the uploaded table", "revision": 1, "steps": [
                {"id": "inspect", "title": "Inspect table", "kind": "capability", "capability": "table.inspect"}
            ]}
        }}]},
        {"tool_calls": [{"id": "call-run", "name": "execute_plan", "arguments": {}}]},
        {"text": "The table was inspected and the plan completed."},
    ])
    agent = PlannerAgent(provider, broker, max_rounds=5)
    result = await agent.run("Please inspect my table and report when done.")
    print("Agent status:", result.status)
    print("Agent text:", result.text)
    print("Tool calls:", [item["tool"] for item in result.tool_results])
    print("Plan:", store.get_current_plan(session_id))


if __name__ == "__main__":
    asyncio.run(main())
