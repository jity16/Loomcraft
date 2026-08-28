"""Broker guardrails: authorisation, gating, budgets, and error shape."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import loomcraft as lc


async def echo(ctx: lc.NodeContext) -> lc.NodeResult:
    ctx.emit("out", "out.txt", f"processed {len(ctx.input_list('doc'))} file(s)")
    return lc.NodeResult.ok()


async def boom(ctx: lc.NodeContext) -> lc.NodeResult:
    return lc.NodeResult.fail("runner refused")


def build_registry() -> lc.Registry:
    registry = lc.Registry()
    registry.register_runner("echo", echo)
    registry.register_runner("boom", boom)
    registry.register_capability(
        lc.Capability(
            id="text.echo",
            name="Echo",
            description="Echo a text document.",
            runner="echo",
            inputs=(
                lc.CapabilityInput(
                    key="doc",
                    name="Document",
                    description="A text document.",
                    allowed_extensions=(".txt",),
                ),
            ),
            outputs=(lc.Port(name="out", artifact_type="txt"),),
            parameters={
                "mode": lc.Parameter(
                    type="string", description="mode", enum=("fast", "slow"), default="fast"
                ),
                "rounds": lc.Parameter(
                    type="integer", description="rounds", minimum=1, maximum=5, default=1
                ),
            },
            tags=("text", "echo"),
        )
    )
    registry.register_capability(
        lc.Capability(
            id="text.boom",
            name="Boom",
            description="Always fails.",
            runner="boom",
        )
    )
    registry.register_workflow(
        lc.Workflow(
            id="text.pipeline",
            name="Text pipeline",
            description="Echo twice.",
            inputs=(
                lc.CapabilityInput(
                    key="doc", name="Document", description="A text document."
                ),
            ),
            nodes=(
                lc.WorkflowNode(id="first", name="First", runner="echo", inputs=("doc",),
                                outputs=(lc.Port(name="out", artifact_type="txt"),)),
                lc.WorkflowNode(id="second", name="Second", runner="echo",
                                depends_on=("first",)),
            ),
        )
    )
    return registry


class BrokerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = lc.SessionStore(Path(self._tmp.name) / "s", in_memory_events=True)
        self.session = self.store.create("t")
        self.registry = build_registry()
        self.broker = lc.ToolBroker(self.session, self.registry)
        self.upload = self.session.save_upload("input.txt", b"hello")

    async def asyncTearDown(self):
        await self.broker.close()
        self._tmp.cleanup()

    async def publish(self, steps, revision=1, reason=None):
        plan = {"goal": "goal", "revision": revision, "steps": steps}
        if reason:
            plan["reason"] = reason
        return await self.broker.dispatch("publish_plan", {"plan": plan})

    async def publish_echo_plan(self):
        return await self.publish(
            [
                {
                    "id": "run",
                    "title": "Echo",
                    "kind": "capability",
                    "capability": "text.echo",
                },
                {
                    "id": "reply",
                    "title": "Answer",
                    "kind": "answer",
                    "depends_on": ["run"],
                },
            ]
        )


class TestDiscovery(BrokerCase):
    async def test_session_context_reports_uploads_and_catalog(self):
        response = await self.broker.dispatch("session_context", {})
        self.assertTrue(response.ok)
        self.assertEqual(len(response.result["uploads"]), 1)
        self.assertIsNone(response.result["plan"])
        self.assertEqual(response.result["catalog"]["capability_count"], 2)

    async def test_capability_search_returns_full_contracts(self):
        response = await self.broker.dispatch("capability_search", {"query": "echo text"})
        self.assertTrue(response.ok)
        top = response.result["results"][0]
        self.assertEqual(top["id"], "text.echo")
        self.assertIn("mode", top["parameters"])
        self.assertEqual(top["execution_tool"], "run_capability")

    async def test_catalog_search_can_scope_to_workflows(self):
        response = await self.broker.dispatch(
            "catalog_search", {"query": "pipeline", "scope": "workflows"}
        )
        self.assertEqual(response.result["results"][0]["id"], "text.pipeline")

    async def test_inspect_source_previews_a_file(self):
        response = await self.broker.dispatch(
            "inspect_source", {"source_ref": self.upload["source_ref"]}
        )
        self.assertTrue(response.ok)
        self.assertEqual(response.result["preview_lines"], ["hello"])
        self.assertFalse(response.result["binary"])

    async def test_inspect_source_rejects_a_path_outside_the_session(self):
        response = await self.broker.dispatch(
            "inspect_source", {"source_ref": "scratch:../../etc/passwd"}
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "SOURCE_INVALID")


class TestPlanTools(BrokerCase):
    async def test_publish_then_read_back(self):
        self.assertTrue((await self.publish_echo_plan()).ok)
        context = await self.broker.dispatch("session_context", {})
        self.assertEqual(context.result["plan"]["revision"], 1)
        self.assertEqual(len(context.result["plan"]["steps"]), 2)

    async def test_publish_rejects_unknown_capability(self):
        response = await self.publish(
            [{"id": "x", "title": "X", "kind": "capability", "capability": "not.real"}]
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "PLAN_INVALID")
        self.assertIn("unknown capability", response.error)

    async def test_update_step_refuses_capability_steps(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "update_step", {"step_id": "run", "status": "succeeded"}
        )
        self.assertFalse(response.ok)
        self.assertIn("execution tool", response.error)

    async def test_update_step_enforces_dependencies(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "update_step", {"step_id": "reply", "status": "succeeded"}
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "STEP_DEPENDENCIES_INCOMPLETE")

    async def test_execution_before_publishing_is_refused(self):
        response = await self.broker.dispatch(
            "run_capability",
            {"capability_id": "text.echo", "step_id": "run", "inputs": {}},
        )
        self.assertFalse(response.ok)
        self.assertIn("publish a task plan", response.error)

    async def test_replan_bumps_revision_and_keeps_history(self):
        await self.publish_echo_plan()
        response = await self.publish(
            [{"id": "solo", "title": "Solo", "kind": "answer"}],
            revision=2,
            reason="the echo capability was the wrong tool",
        )
        self.assertTrue(response.ok)
        self.assertEqual(len(self.session.plan_history()), 2)
        self.assertEqual(self.session.current_plan()["revision"], 2)


class TestExecution(BrokerCase):
    async def test_direct_continue_policy_leaves_downstream_runnable(self):
        await self.publish(
            [
                {
                    "id": "run",
                    "title": "Boom",
                    "kind": "capability",
                    "capability": "text.boom",
                    "on_failure": "continue",
                },
                {
                    "id": "reply",
                    "title": "Answer",
                    "kind": "answer",
                    "depends_on": ["run"],
                },
            ]
        )
        failed = await self.broker.dispatch(
            "run_capability",
            {"capability_id": "text.boom", "step_id": "run", "inputs": {}},
        )
        self.assertFalse(failed.ok)
        self.assertEqual(lc.get_step(self.session.current_plan(), "reply")["status"], "pending")
        completed = await self.broker.dispatch(
            "update_step", {"step_id": "reply", "status": "succeeded"}
        )
        self.assertTrue(completed.ok)

    async def test_run_capability_end_to_end(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "run",
                "inputs": {"doc": self.upload["source_ref"]},
                "parameters": {"mode": "slow", "rounds": 2},
            },
        )
        self.assertTrue(response.ok)
        self.assertEqual(response.result["status"], "succeeded")
        self.assertEqual(len(response.result["artifacts"]), 1)
        self.assertEqual(lc.get_step(self.session.current_plan(), "run")["status"], "succeeded")

    async def test_capability_mismatch_is_refused(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "run_capability",
            {"capability_id": "text.boom", "step_id": "run", "inputs": {}},
        )
        self.assertFalse(response.ok)
        self.assertIn("does not authorize", response.error)

    async def test_unknown_input_key_is_refused(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "run",
                "inputs": {"wrong_key": self.upload["source_ref"]},
            },
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "CAPABILITY_CONTRACT_VIOLATION")

    async def test_out_of_range_parameter_is_refused(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "run",
                "inputs": {"doc": self.upload["source_ref"]},
                "parameters": {"rounds": 99},
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("maximum", response.error)

    async def test_unknown_source_ref_is_refused_before_the_step_moves(self):
        await self.publish_echo_plan()
        response = await self.broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "run",
                "inputs": {"doc": "upload:does-not-exist"},
            },
        )
        self.assertFalse(response.ok)
        self.assertEqual(
            lc.get_step(self.session.current_plan(), "run")["status"],
            "pending",
            "a rejected call must not leave the step stuck in running",
        )

    async def test_input_extension_is_enforced_by_the_engine_contract(self):
        await self.publish_echo_plan()
        wrong = self.session.save_upload("input.csv", b"hello")
        response = await self.broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "run",
                "inputs": {"doc": wrong["source_ref"]},
            },
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "CAPABILITY_CONTRACT_VIOLATION")
        self.assertEqual(lc.get_step(self.session.current_plan(), "run")["status"], "pending")

    async def test_failed_execution_marks_the_step_and_skips_downstream(self):
        await self.publish(
            [
                {"id": "run", "title": "Boom", "kind": "capability", "capability": "text.boom"},
                {"id": "reply", "title": "Answer", "kind": "answer", "depends_on": ["run"]},
            ]
        )
        response = await self.broker.dispatch(
            "run_capability",
            {"capability_id": "text.boom", "step_id": "run", "inputs": {}},
        )
        self.assertFalse(response.ok)
        current = self.session.current_plan()
        self.assertEqual(lc.get_step(current, "run")["status"], "failed")
        self.assertEqual(lc.get_step(current, "reply")["status"], "skipped")

    async def test_a_step_cannot_run_twice(self):
        await self.publish_echo_plan()
        payload = {
            "capability_id": "text.echo",
            "step_id": "run",
            "inputs": {"doc": self.upload["source_ref"]},
        }
        self.assertTrue((await self.broker.dispatch("run_capability", payload)).ok)
        second = await self.broker.dispatch("run_capability", payload)
        self.assertFalse(second.ok)

    async def test_run_workflow_executes_the_registered_dag(self):
        await self.publish(
            [
                {
                    "id": "pipe",
                    "title": "Pipeline",
                    "kind": "workflow",
                    "capability": "text.pipeline",
                }
            ]
        )
        response = await self.broker.dispatch(
            "run_workflow",
            {
                "workflow_id": "text.pipeline",
                "step_id": "pipe",
                "inputs": {"doc": self.upload["source_ref"]},
            },
        )
        self.assertTrue(response.ok)
        self.assertEqual(response.result["status"], "succeeded")


class TestInputGating(BrokerCase):
    def request_payload(self):
        return {
            "request": {
                "title": "Need a table",
                "message": "Upload the source table to continue.",
                "requirements": [
                    {
                        "key": "table",
                        "label": "Source table",
                        "description": "The CSV to analyse.",
                        "required": True,
                        "min_files": 1,
                        "max_files": 1,
                        "allowed_extensions": [".csv"],
                        "field_hints": ["id", "value"],
                    }
                ],
                "continue_prompt": "The table is uploaded, please continue.",
            }
        }

    async def test_request_inputs_blocks_mutating_tools(self):
        response = await self.broker.dispatch("request_inputs", self.request_payload())
        self.assertTrue(response.ok)
        self.assertTrue(self.broker.awaiting_inputs)

        blocked = await self.publish_echo_plan()
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.error_code, "BROKER_AWAITING_INPUTS")

        # Read-only evidence gathering stays available while blocked.
        self.assertTrue((await self.broker.dispatch("session_context", {})).ok)

    async def test_fulfilling_unblocks_the_broker(self):
        response = await self.broker.dispatch("request_inputs", self.request_payload())
        request_id = response.result["request"]["request_id"]
        self.session.save_upload("table.csv", b"id,value\n1,2\n")
        self.broker.fulfill_input_request(request_id)
        self.assertFalse(self.broker.awaiting_inputs)
        self.broker.begin_turn()
        self.assertTrue((await self.publish_echo_plan()).ok)

    async def test_fulfilment_fails_when_the_required_file_is_absent(self):
        response = await self.broker.dispatch("request_inputs", self.request_payload())
        request_id = response.result["request"]["request_id"]
        with self.assertRaises(lc.InputRequestError):
            self.broker.fulfill_input_request(request_id)

    async def test_cancelling_a_request_also_unblocks(self):
        response = await self.broker.dispatch("request_inputs", self.request_payload())
        self.broker.cancel_input_request(response.result["request"]["request_id"])
        self.assertFalse(self.broker.awaiting_inputs)

    async def test_deleting_a_used_upload_reopens_the_request(self):
        response = await self.broker.dispatch("request_inputs", self.request_payload())
        request_id = response.result["request"]["request_id"]
        upload = self.session.save_upload("table.csv", b"id,value\n1,2\n")
        self.broker.fulfill_input_request(request_id)
        self.session.delete_upload(upload["id"])
        affected = self.broker.invalidate_requests_for_upload(upload["id"])
        self.assertEqual(affected, [request_id])
        self.assertTrue(self.broker.awaiting_inputs)

    async def test_server_owns_the_request_id(self):
        payload = self.request_payload()
        payload["request"]["request_id"] = "input-aaaaaaaaaaaaaaaa"
        response = await self.broker.dispatch("request_inputs", payload)
        self.assertFalse(response.ok)
        self.assertIn("generated by the server", response.error)


class TestArtifactRegistration(BrokerCase):
    async def test_rejects_registration_before_dependencies_complete(self):
        await self.publish(
            [
                {"id": "prepare", "title": "Prepare", "kind": "dynamic"},
                {
                    "id": "report",
                    "title": "Report",
                    "kind": "dynamic",
                    "depends_on": ["prepare"],
                },
            ]
        )
        (self.session.scratch_dir / "report.md").write_text("report")
        response = await self.broker.dispatch(
            "register_artifacts",
            {"step_id": "report", "artifacts": [{"path": "report.md"}]},
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "STEP_DEPENDENCIES_INCOMPLETE")
        self.assertEqual(self.session.list_artifacts(), [])

    async def test_rejects_registration_after_step_is_terminal(self):
        await self.publish([{"id": "work", "title": "Work", "kind": "dynamic"}])
        await self.broker.dispatch(
            "update_step", {"step_id": "work", "status": "succeeded"}
        )
        (self.session.scratch_dir / "late.md").write_text("late")
        response = await self.broker.dispatch(
            "register_artifacts",
            {"step_id": "work", "artifacts": [{"path": "late.md"}]},
        )
        self.assertFalse(response.ok)
        self.assertEqual(self.session.list_artifacts(), [])

    async def test_registers_scratch_files(self):
        await self.publish([{"id": "work", "title": "Work", "kind": "dynamic"}])
        (self.session.scratch_dir / "report.md").write_text("# Findings\n")
        response = await self.broker.dispatch(
            "register_artifacts",
            {"step_id": "work", "artifacts": [{"path": "report.md"}]},
        )
        self.assertTrue(response.ok)
        self.assertEqual(len(self.session.list_artifacts()), 1)

    async def test_rejects_paths_outside_scratch(self):
        await self.publish([{"id": "work", "title": "Work", "kind": "dynamic"}])
        response = await self.broker.dispatch(
            "register_artifacts",
            {"step_id": "work", "artifacts": [{"path": "../control/plan.json"}]},
        )
        self.assertFalse(response.ok)
        self.assertEqual(len(self.session.list_artifacts()), 0)

    async def test_batch_is_atomic(self):
        await self.publish([{"id": "work", "title": "Work", "kind": "dynamic"}])
        (self.session.scratch_dir / "good.md").write_text("ok")
        response = await self.broker.dispatch(
            "register_artifacts",
            {
                "step_id": "work",
                "artifacts": [{"path": "good.md"}, {"path": "missing.md"}],
            },
        )
        self.assertFalse(response.ok)
        self.assertEqual(
            len(self.session.list_artifacts()),
            0,
            "a batch with one bad entry must register nothing",
        )

    async def test_rejects_registration_against_a_capability_step(self):
        await self.publish_echo_plan()
        (self.session.scratch_dir / "x.md").write_text("x")
        response = await self.broker.dispatch(
            "register_artifacts", {"step_id": "run", "artifacts": [{"path": "x.md"}]}
        )
        self.assertFalse(response.ok)


class TestGuardrails(BrokerCase):
    async def test_extra_handler_cannot_override_a_core_tool(self):
        with self.assertRaises(ValueError):
            lc.ToolBroker(
                self.session,
                self.registry,
                extra_tool_handlers={"publish_plan": lambda _: {}},
            )

    async def test_per_turn_call_budget(self):
        broker = lc.ToolBroker(
            self.session, self.registry, limits=lc.BrokerLimits(max_actions_per_turn=3)
        )
        broker.begin_turn()
        for _ in range(3):
            self.assertTrue((await broker.dispatch("session_context", {})).ok)
        response = await broker.dispatch("session_context", {})
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "BROKER_ACTION_LIMIT_EXCEEDED")

    async def test_identical_call_repeat_limit(self):
        broker = lc.ToolBroker(
            self.session, self.registry, limits=lc.BrokerLimits(max_identical_actions=2)
        )
        broker.begin_turn()
        payload = {"query": "echo"}
        self.assertTrue((await broker.dispatch("capability_search", payload)).ok)
        self.assertTrue((await broker.dispatch("capability_search", payload)).ok)
        response = await broker.dispatch("capability_search", payload)
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "BROKER_ACTION_REPEATED")

    async def test_begin_turn_resets_the_budget(self):
        broker = lc.ToolBroker(
            self.session, self.registry, limits=lc.BrokerLimits(max_actions_per_turn=1)
        )
        broker.begin_turn()
        self.assertTrue((await broker.dispatch("session_context", {})).ok)
        self.assertFalse((await broker.dispatch("session_context", {})).ok)
        broker.begin_turn()
        self.assertTrue((await broker.dispatch("session_context", {})).ok)

    async def test_unknown_tool_is_refused(self):
        response = await self.broker.dispatch("delete_everything", {})
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "BROKER_ACTION_UNSUPPORTED")

    async def test_non_object_and_non_json_payloads_fail_closed(self):
        non_object = await self.broker.dispatch("session_context", [])  # type: ignore[arg-type]
        self.assertFalse(non_object.ok)
        non_finite = await self.broker.dispatch("catalog_search", {"query": float("nan")})
        self.assertFalse(non_finite.ok)
        self.assertIn("JSON-serializable", non_finite.error or "")

    async def test_tool_schema_is_enforced_server_side(self):
        response = await self.broker.dispatch(
            "session_context", {"unexpected": True}
        )
        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "BROKER_INVALID_ARGUMENT")

    async def test_concurrent_executions_are_refused(self):
        registry = build_registry()

        async def slow(ctx: lc.NodeContext) -> lc.NodeResult:
            await asyncio.sleep(0.2)
            return lc.NodeResult.ok()

        registry.register_runner("echo", slow, replace=True)
        broker = lc.ToolBroker(self.session, registry)
        broker.begin_turn()
        plan = {
            "goal": "g",
            "revision": 1,
            "steps": [
                {"id": "a", "title": "A", "kind": "capability", "capability": "text.echo"},
                {"id": "b", "title": "B", "kind": "capability", "capability": "text.echo"},
            ],
        }
        self.assertTrue((await broker.dispatch("publish_plan", {"plan": plan})).ok)

        first = asyncio.create_task(
            broker.dispatch(
                "run_capability",
                {
                    "capability_id": "text.echo",
                    "step_id": "a",
                    "inputs": {"doc": self.upload["source_ref"]},
                },
            )
        )
        await asyncio.sleep(0.05)
        second = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "text.echo",
                "step_id": "b",
                "inputs": {"doc": self.upload["source_ref"]},
            },
        )
        self.assertFalse(second.ok)
        self.assertEqual(second.error_code, "BROKER_EXECUTION_BUSY")
        self.assertTrue((await first).ok)
        await broker.close()

    async def test_errors_expose_a_stable_code_and_bounded_message(self):
        response = await self.broker.dispatch("update_step", {"step_id": "ghost", "status": "x"})
        self.assertFalse(response.ok)
        self.assertIsInstance(response.error_code, str)
        self.assertLessEqual(len(response.to_dict()["error"]), 4000)


if __name__ == "__main__":
    unittest.main()
