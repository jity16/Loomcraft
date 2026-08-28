"""A complete Loomcraft run: parallel branches, retry, join, and live events."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core"))

from loomcraft import (  # noqa: E402
    DAGExecutor,
    InMemoryStore,
    Registry,
    StepResult,
    ToolBroker,
    WorkflowSpec,
    CapabilitySpec,
)


async def main() -> None:
    registry = Registry()
    store = InMemoryStore()
    attempts = {"flaky": 0}

    async def source_a(context):
        await asyncio.sleep(0.12)
        return StepResult(output={"rows": 12}, summary="A loaded", artifacts=[{"filename": "a.json", "size": 42}])

    async def source_b(context):
        await asyncio.sleep(0.12)
        return StepResult(output={"rows": 8}, summary="B loaded", artifacts=[{"filename": "b.json", "size": 31}])

    async def flaky_check(context):
        attempts["flaky"] += 1
        if attempts["flaky"] < 3:
            raise RuntimeError("transient upstream timeout")
        return StepResult(output={"quality": "pass"}, summary="quality check passed on retry")

    async def normalize(context):
        total = sum((value or {}).get("rows", 0) for value in context.dependencies.values())
        return StepResult(output={"rows": total}, summary="normalized %d rows" % total)

    async def report(context):
        return StepResult(
            output={"rows": context.dependencies["normalize"]["rows"], "quality": context.dependencies["quality"]["quality"]},
            summary="report assembled",
            artifacts=[{"filename": "report.json", "size": 128, "content_type": "application/json"}],
        )

    registry.register_capability(CapabilitySpec("source.a", "Source A", handler=source_a))
    registry.register_capability(CapabilitySpec("source.b", "Source B", handler=source_b))
    registry.register_capability(CapabilitySpec("quality.check", "Quality check", handler=flaky_check))
    registry.register_capability(CapabilitySpec("data.normalize", "Normalize", handler=normalize))
    registry.register_workflow(WorkflowSpec("report.build", "Build report", handler=report))

    session_id = "parallel-retry-demo"
    store.create_session(session_id)
    executor = DAGExecutor(registry, store=store, max_concurrency=2)
    broker = ToolBroker(session_id, registry, store=store, executor=executor)
    plan = {
        "goal": "Merge two feeds and publish a quality report",
        "revision": 1,
        "summary": "Two independent feeds converge on a retried quality check and report.",
        "steps": [
            {"id": "source-a", "title": "Load source A", "kind": "capability", "capability": "source.a"},
            {"id": "source-b", "title": "Load source B", "kind": "capability", "capability": "source.b"},
            {"id": "quality", "title": "Quality check", "kind": "capability", "capability": "quality.check", "depends_on": ["source-a"], "retry": {"max_attempts": 3, "backoff_seconds": 0.03}},
            {"id": "normalize", "title": "Normalize merged rows", "kind": "capability", "capability": "data.normalize", "depends_on": ["source-a", "source-b"]},
            {"id": "report", "title": "Build report", "kind": "workflow", "capability": "report.build", "depends_on": ["normalize", "quality"]},
        ],
    }
    published = await broker.dispatch_dynamic_tool("publish_plan", {"plan": plan})
    assert published["ok"], published

    print("Published revision", published["result"]["plan"]["revision"])
    result = await broker.dispatch_dynamic_tool("execute_plan")
    print("Run status:", result["result"]["status"])
    print("Attempts:", result["result"]["steps"]["quality"]["attempts"])
    print("Parallel evidence (events):")
    for event in store.read_events(session_id):
        if event.event in {"step_attempt", "step_retry", "execution_progress", "execution_finished"}:
            print("  %-22s %s" % (event.event, event.data))


if __name__ == "__main__":
    asyncio.run(main())
