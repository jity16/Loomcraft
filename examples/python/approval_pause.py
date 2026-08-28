"""Human approval boundary example."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from loomcraft import ApprovalRequired, DAGExecutor, InMemoryStore, Registry, StepResult, ToolBroker  # noqa: E402


async def main() -> None:
    registry = Registry()
    approved = {"value": False}

    async def review(context):
        if not approved["value"]:
            raise ApprovalRequired("A reviewer must confirm the generated mapping", {"fields": 3})
        return StepResult(summary="mapping approved")

    async def publish(context):
        return StepResult(summary="published")

    registry.register_handler("review", review)
    registry.register_handler("dynamic", publish)
    store = InMemoryStore()
    store.create_session("approval-demo")
    executor = DAGExecutor(registry, store=store)
    broker = ToolBroker("approval-demo", registry, store=store, executor=executor)
    await broker.dispatch_dynamic_tool("publish_plan", {"plan": {"goal": "approve mapping", "revision": 1, "steps": [
        {"id": "review", "title": "Review mapping", "kind": "review"},
        {"id": "publish", "title": "Publish", "kind": "dynamic", "depends_on": ["review"]},
    ]}})
    first = await broker.dispatch_dynamic_tool("execute_plan")
    print("Paused:", first["result"]["status"])
    # A host UI normally asks a person, then updates the plan and resumes.
    approved["value"] = True
    current = store.get_current_plan("approval-demo")
    current["steps"][0]["status"] = "pending"
    store.update_current_plan("approval-demo", current)
    resumed = await executor.execute(current, session_id="approval-demo")
    print("After approval boundary is resolved:", resumed.status)


if __name__ == "__main__":
    asyncio.run(main())
