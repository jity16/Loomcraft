"""Contract tests for the features brought over from the extracted runtime."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import loomcraft as lc


class ExtractedExtensionTests(unittest.IsolatedAsyncioTestCase):
    def test_in_memory_knowledge_version_commits_to_content(self) -> None:
        first = lc.InMemoryKnowledgeProvider({"guide.md": "first"})
        second = lc.InMemoryKnowledgeProvider({"guide.md": "second"})
        self.assertNotEqual(first.version, second.version)

    async def test_typed_broker_uses_the_injected_table_inspector(self) -> None:
        seen: dict[str, object] = {}

        async def inspect(source_ref: str, options: dict[str, object]) -> dict[str, object]:
            seen.update(options)
            return {"source_ref": source_ref, "columns": [{"name": "value"}]}

        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            upload = session.save_upload("table.csv", b"value\n1\n")
            broker = lc.ToolBroker(session, lc.Registry(), table_inspector=inspect)
            result = await broker.dispatch(
                "inspect_table", {"source_ref": upload["source_ref"], "max_rows": 5}
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result["columns"], [{"name": "value"}])
            self.assertEqual(Path(seen["resolved_path"]), session.resolve_source(upload["source_ref"]).path)

    async def test_catalog_search_merges_injected_host_metadata(self) -> None:
        async def catalog(query: str, scope: str, limit: int) -> list[dict[str, object]]:
            return [{"id": "host.operation", "name": query, "scope": scope, "limit": limit}]

        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, lc.Registry(), catalog_provider=catalog)
            result = await broker.dispatch(
                "catalog_search",
                {"query": "profile", "scope": "operations", "limit": 3},
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result["results"][0]["id"], "host.operation")

    async def test_knowledge_snapshot_is_pinned_for_the_session(self) -> None:
        class Knowledge:
            version = "v1"

            def list(self, payload: dict[str, object]) -> dict[str, object]:
                return {"version": self.version, "entries": []}

            def search(self, payload: dict[str, object]) -> dict[str, object]:
                return {"version": self.version, "results": []}

        knowledge = Knowledge()
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, lc.Registry(), knowledge_provider=knowledge)
            first = await broker.dispatch("knowledge_list", {})
            self.assertTrue(first.ok)
            self.assertEqual(session.meta()["knowledge_version"], "v1")
            knowledge.version = "v2"
            changed = await broker.dispatch("knowledge_search", {"query": "x"})
            self.assertFalse(changed.ok)
            self.assertEqual(changed.error_code, "BROKER_KNOWLEDGE_UNAVAILABLE")

    async def test_extended_plan_and_execute_plan_use_the_canonical_engine(self) -> None:
        registry = lc.Registry()

        async def work(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.progress(0.5, "halfway")
            return lc.NodeResult.ok(value=ctx.node_id)

        registry.register_runner("demo.work", work)
        registry.register_capability(
            lc.Capability(
                id="demo.work",
                name="Demo work",
                description="A test capability.",
                runner="demo.work",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            plan = {
                "goal": "extended",
                "revision": 1,
                "objectives": [{"id": "objective", "question": "Did it run?"}],
                "analysis_coverage": [
                    {"objective_id": "objective", "status": "planned", "reason": "test"}
                ],
                "steps": [
                    {
                        "id": "a",
                        "title": "A",
                        "kind": "capability",
                        "capability": "demo.work",
                        "retry": {"max_attempts": 2},
                    },
                    {"id": "answer", "title": "Answer", "kind": "answer", "depends_on": ["a"]},
                ],
            }
            published = await broker.dispatch("publish_plan", {"plan": plan})
            self.assertTrue(published.ok)
            executed = await broker.dispatch("execute_plan", {"inputs": {}})
            self.assertTrue(executed.ok)
            self.assertEqual(executed.result["status"], "succeeded")
            self.assertEqual(session.current_plan()["steps"][0]["status"], "succeeded")
            names = [event.event for event in session.events.read()]
            self.assertIn("execution_started", names)
            self.assertIn("execution_finished", names)

    async def test_execute_plan_validates_typed_parameters_before_running(self) -> None:
        registry = lc.Registry()
        called = False

        async def work(_: lc.NodeContext) -> lc.NodeResult:
            nonlocal called
            called = True
            return lc.NodeResult.ok()

        registry.register_runner("demo.work", work)
        registry.register_capability(
            lc.Capability(
                id="demo.work",
                name="Work",
                description="w",
                runner="demo.work",
                parameters={
                    "count": lc.Parameter(
                        type="integer",
                        description="count",
                        minimum=1,
                        maximum=3,
                    )
                },
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "validate",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "work",
                                "title": "Work",
                                "kind": "capability",
                                "capability": "demo.work",
                            }
                        ],
                    }
                },
            )
            result = await broker.dispatch(
                "execute_plan",
                {"inputs": {"work": {"parameters": {"count": 99}}}},
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "CAPABILITY_CONTRACT_VIOLATION")
            self.assertFalse(called)

    async def test_execute_plan_runs_typed_workflow_with_internal_artifact_flow(self) -> None:
        registry = lc.Registry()

        async def prepare(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("prepared", "prepared.txt", ctx.input("doc").read_text().upper())
            return lc.NodeResult.ok()

        async def finish(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("result", "result.txt", ctx.input("prepared").read_text() + "!")
            return lc.NodeResult.ok()

        registry.register_runner("demo.prepare", prepare)
        registry.register_runner("demo.finish", finish)
        registry.register_workflow(
            lc.Workflow(
                id="demo.flow",
                name="Flow",
                description="f",
                inputs=(
                    lc.CapabilityInput(
                        key="doc", name="Document", description="d"
                    ),
                ),
                nodes=(
                    lc.WorkflowNode(
                        id="prepare",
                        name="Prepare",
                        runner="demo.prepare",
                        inputs=("doc",),
                        outputs=(lc.Port(name="prepared", artifact_type="text"),),
                    ),
                    lc.WorkflowNode(
                        id="finish",
                        name="Finish",
                        runner="demo.finish",
                        depends_on=("prepare",),
                        outputs=(lc.Port(name="result", artifact_type="text"),),
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            upload = session.save_upload("doc.txt", b"hello")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "workflow",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "flow",
                                "title": "Flow",
                                "kind": "workflow",
                                "capability": "demo.flow",
                            }
                        ],
                    }
                },
            )
            result = await broker.dispatch(
                "execute_plan",
                {
                    "inputs": {
                        "flow": {"inputs": {"doc": upload["source_ref"]}}
                    }
                },
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result["status"], "succeeded")
            self.assertEqual(len(result.result["nodes"]["flow"]["artifacts"]), 2)
            artifact = next(
                item for item in session.list_artifacts() if item["port_name"] == "result"
            )
            self.assertEqual(artifact["step_id"], "flow")
            self.assertEqual(session.resolve_source(artifact["source_ref"]).path.read_text(), "HELLO!")

    def test_extended_tool_dialects_share_one_schema(self) -> None:
        names = {item.name for item in lc.extended_tool_specs()}
        self.assertIn("execute_plan", names)
        self.assertIn("knowledge_search", names)
        self.assertEqual(
            lc.to_dialect(lc.extended_tool_specs(), "openai")[0]["type"], "function"
        )
        self.assertIn("inputSchema", lc.dynamic_tool_specs()[0])

    def test_plan_and_event_compatibility_aliases_are_single_sourced(self) -> None:
        raw = {"goal": "g", "revision": 1, "steps": [{"id": "a", "title": "A", "kind": "answer"}]}
        normalized = lc.validate_plan(raw)
        self.assertEqual(lc.Plan.from_raw(normalized).to_dict()["goal"], "g")
        event = lc.EventLog().append_event("notice", {"message": "ok"})
        self.assertEqual(event.as_dict()["data"]["_event_seq"], 1)

    async def test_continue_failure_policy_runs_the_healthy_dependent(self) -> None:
        registry = lc.Registry()
        seen: list[str] = []

        async def fail(_: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.fail("expected failure")

        async def continue_step(_: lc.NodeContext) -> lc.NodeResult:
            seen.append("continue")
            return lc.NodeResult.ok()

        registry.register_runner("demo.fail", fail)
        registry.register_runner("demo.continue", continue_step)
        registry.register_capability(
            lc.Capability(id="demo.fail", name="Fail", description="f", runner="demo.fail")
        )
        registry.register_capability(
            lc.Capability(
                id="demo.continue",
                name="Continue",
                description="c",
                runner="demo.continue",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "failure policy",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "fail",
                                "title": "Fail",
                                "kind": "capability",
                                "capability": "demo.fail",
                                "on_failure": "continue",
                            },
                            {
                                "id": "next",
                                "title": "Next",
                                "kind": "capability",
                                "capability": "demo.continue",
                                "depends_on": ["fail"],
                            },
                        ],
                    }
                },
            )
            result = await broker.dispatch("execute_plan")
            self.assertEqual(result.result["status"], "failed")
            self.assertEqual(seen, ["continue"])

    async def test_runtime_adapts_the_normalized_provider_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = lc.ScriptedProvider([{"text": "done"}])
            runtime = lc.LoomcraftRuntime(
                provider=provider,
                store=lc.SessionStore(Path(directory)),
            )
            session_id = runtime.create_session("runtime")["session_id"]
            result = await runtime.run_turn(session_id, "hello")
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.text, "done")
            exposed = {
                item["function"]["name"]
                for item in provider.calls[0]["tools"]
            }
            self.assertIn("execute_plan", exposed)
            self.assertIn("knowledge_search", exposed)

    async def test_plan_adapter_exposes_dependency_outputs_to_downstream_handlers(self) -> None:
        registry = lc.Registry()

        async def source(_: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(output={"answer": 42})

        async def consume(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(output={"seen": ctx.dependencies["source"]["answer"]})

        registry.register_runner("demo.source", source)
        registry.register_runner("demo.consume", consume)
        registry.register_capability(
            lc.Capability(id="demo.source", name="Source", description="s", runner="demo.source")
        )
        registry.register_capability(
            lc.Capability(id="demo.consume", name="Consume", description="c", runner="demo.consume")
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "dependency output",
                        "revision": 1,
                        "steps": [
                            {"id": "source", "title": "Source", "kind": "capability", "capability": "demo.source"},
                            {"id": "consume", "title": "Consume", "kind": "capability", "capability": "demo.consume", "depends_on": ["source"]},
                        ],
                    }
                },
            )
            result = await broker.dispatch("execute_plan")
            self.assertTrue(result.ok)
            self.assertEqual(result.result["nodes"]["consume"]["detail"]["output"]["seen"], 42)

    async def test_direct_capability_approval_returns_without_hanging_and_resumes(self) -> None:
        registry = lc.Registry()
        approved = {"value": False}

        async def gated(_: lc.NodeContext) -> lc.NodeResult:
            if not approved["value"]:
                return lc.NodeResult.needs_approval("human decision")
            return lc.NodeResult.ok()

        registry.register_runner("demo.gated", gated)
        registry.register_capability(
            lc.Capability(id="demo.gated", name="Gated", description="g", runner="demo.gated")
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "approval",
                        "revision": 1,
                        "steps": [
                            {"id": "gate", "title": "Gate", "kind": "capability", "capability": "demo.gated"}
                        ],
                    }
                },
            )
            paused = await broker.dispatch(
                "run_capability",
                {"capability_id": "demo.gated", "step_id": "gate", "inputs": {}},
            )
            self.assertTrue(paused.ok)
            self.assertEqual(paused.result["status"], "paused_approval")
            approved["value"] = True
            resumed = await broker.approve_run(paused.result["id"], "gate")
            self.assertEqual(resumed["status"], "succeeded")
            self.assertEqual(session.current_plan()["steps"][0]["status"], "succeeded")

    async def test_turn_manager_retains_a_run_while_approval_is_pending(self) -> None:
        registry = lc.Registry()

        async def gated(_: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.needs_approval("human decision")

        registry.register_runner("demo.gated", gated)
        registry.register_capability(
            lc.Capability(
                id="demo.gated",
                name="Gated",
                description="g",
                runner="demo.gated",
            )
        )

        class Agent:
            async def run_turn(self, broker: lc.ToolBroker, message: str, **_: object):
                await broker.dispatch(
                    "publish_plan",
                    {
                        "plan": {
                            "goal": message,
                            "revision": 1,
                            "steps": [
                                {
                                    "id": "gate",
                                    "title": "Gate",
                                    "kind": "capability",
                                    "capability": "demo.gated",
                                }
                            ],
                        }
                    },
                )
                await broker.dispatch(
                    "run_capability",
                    {"capability_id": "demo.gated", "step_id": "gate", "inputs": {}},
                )
                return type("Result", (), {"error": None})()

        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            manager = lc.TurnManager()
            task = manager.start(
                session,
                broker,
                Agent(),
                "approval",
                on_event=lambda *_: None,
            )
            await task
            self.assertEqual(broker.active_run.status, "paused_approval")
            self.assertIs(manager.broker("s"), broker)
            self.assertTrue(manager.is_busy("s"))
            self.assertEqual(session.meta()["status"], "waiting_approval")
            resumed = await broker.approve_run(broker.active_run.id, "gate")
            self.assertEqual(resumed["status"], "succeeded")
            self.assertFalse(manager.is_busy("s"))
            self.assertEqual(session.meta()["status"], "idle")

    async def test_approval_returns_at_the_next_gate_instead_of_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, lc.Registry())
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "two reviews",
                        "revision": 1,
                        "steps": [
                            {"id": "first", "title": "First", "kind": "review"},
                            {
                                "id": "second",
                                "title": "Second",
                                "kind": "review",
                                "depends_on": ["first"],
                            },
                        ],
                    }
                },
            )
            paused = await broker.dispatch("execute_plan")
            self.assertEqual(paused.result["status"], "paused_approval")
            next_gate = await broker.approve_run(paused.result["id"], "first")
            self.assertEqual(next_gate["status"], "paused_approval")
            self.assertEqual(next_gate["nodes"]["second"]["status"], "waiting_approval")
            finished = await broker.approve_run(paused.result["id"], "second")
            self.assertEqual(finished["status"], "succeeded")

    async def test_plan_handler_can_raise_approval_required(self) -> None:
        registry = lc.Registry()

        async def review(_: object) -> lc.StepResult:
            raise lc.ApprovalRequired("check evidence", {"confidence": 0.7})

        registry.register_handler("review", review)
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "review",
                        "revision": 1,
                        "steps": [
                            {"id": "review", "title": "Review", "kind": "review"}
                        ],
                    }
                },
            )
            paused = await broker.dispatch("execute_plan")
            self.assertEqual(paused.result["status"], "paused_approval")
            self.assertEqual(
                paused.result["nodes"]["review"]["detail"]["confidence"], 0.7
            )

    async def test_direct_workflow_approval_projects_to_its_plan_step(self) -> None:
        registry = lc.Registry()
        calls: list[bool] = []

        async def publish(ctx: lc.NodeContext) -> lc.NodeResult:
            calls.append(bool(ctx.config.get("approved")))
            return lc.NodeResult.ok()

        registry.register_runner("demo.publish", publish)
        registry.register_workflow(
            lc.Workflow(
                id="demo.approval_flow",
                name="Approval flow",
                description="a",
                nodes=(
                    lc.WorkflowNode(
                        id="publish",
                        name="Publish",
                        runner="demo.publish",
                        requires_approval=True,
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "approval workflow",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "flow",
                                "title": "Flow",
                                "kind": "workflow",
                                "capability": "demo.approval_flow",
                            }
                        ],
                    }
                },
            )
            paused = await broker.dispatch(
                "run_workflow",
                {
                    "workflow_id": "demo.approval_flow",
                    "step_id": "flow",
                    "inputs": {},
                },
            )
            self.assertEqual(paused.result["status"], "paused_approval")
            self.assertEqual(calls, [])
            resumed = await broker.approve_run(paused.result["id"], "flow")
            self.assertEqual(resumed["status"], "succeeded")
            self.assertEqual(calls, [True])
            self.assertEqual(session.current_plan()["steps"][0]["status"], "succeeded")
            self.assertEqual(session.list_executions()[0]["status"], "succeeded")

    async def test_direct_capability_inherits_catalog_retry_policy(self) -> None:
        registry = lc.Registry()
        attempts: list[int] = []

        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            attempts.append(ctx.attempt)
            if ctx.attempt < 3:
                return lc.NodeResult.retry("temporary")
            return lc.NodeResult.ok()

        registry.register_runner("demo.flaky", flaky)
        registry.register_capability(
            lc.Capability(
                id="demo.flaky",
                name="Flaky",
                description="f",
                runner="demo.flaky",
                max_attempts=3,
                retry_backoff_seconds=0,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "retry",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "flaky",
                                "title": "Flaky",
                                "kind": "capability",
                                "capability": "demo.flaky",
                            }
                        ],
                    }
                },
            )
            result = await broker.dispatch(
                "run_capability",
                {"capability_id": "demo.flaky", "step_id": "flaky", "inputs": {}},
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result["attempts"], 3)
            self.assertEqual(attempts, [1, 2, 3])

    async def test_review_capability_uses_the_trusted_execution_path(self) -> None:
        registry = lc.Registry()

        async def review(_: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(verdict="accepted")

        registry.register_runner("review.check", review)
        registry.register_capability(
            lc.Capability(
                id="demo.review",
                name="Review",
                description="r",
                runner="review.check",
                tags=("review",),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            published = await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "review",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "review",
                                "title": "Review",
                                "kind": "review",
                                "capability": "demo.review",
                            }
                        ],
                    }
                },
            )
            self.assertTrue(published.ok)
            bypass = await broker.dispatch(
                "update_step", {"step_id": "review", "status": "succeeded"}
            )
            self.assertFalse(bypass.ok)
            executed = await broker.dispatch(
                "run_capability",
                {
                    "capability_id": "demo.review",
                    "step_id": "review",
                    "inputs": {},
                },
            )
            self.assertTrue(executed.ok)
            self.assertEqual(session.current_plan()["steps"][0]["status"], "succeeded")

    async def test_schema_capability_can_reference_a_registered_runner(self) -> None:
        registry = lc.Registry()

        async def runner(ctx: object) -> lc.StepResult:
            return lc.StepResult.ok(
                output={"value": ctx.input("value")}  # type: ignore[attr-defined]
            )

        registry.register_capability(
            id="demo.schema",
            name="Schema capability",
            handler=runner,
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            session = lc.SessionStore(Path(directory)).create("s")
            broker = lc.ToolBroker(session, registry)
            await broker.dispatch(
                "publish_plan",
                {
                    "plan": {
                        "goal": "runner",
                        "revision": 1,
                        "steps": [
                            {
                                "id": "run",
                                "title": "Run",
                                "kind": "capability",
                                "capability": "demo.schema",
                            }
                        ],
                    }
                },
            )
            result = await broker.dispatch(
                "run_capability",
                {
                    "capability_id": "demo.schema",
                    "step_id": "run",
                    "inputs": {"value": 42},
                },
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.result["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
