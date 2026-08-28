"""Regressions for defects that let unsafe or untrue state through.

Each test here corresponds to a behaviour that was once wrong in a way the
engine reported as success. They are grouped by what the failure would have
cost: a side effect nobody authorised, a deliverable nobody produced, a
contract nobody agreed to, or host detail nobody should see.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import loomcraft as lc
from loomcraft.engine import ExecutionGraph, ExecutionNode, graph_from_capability


class Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = lc.SessionStore(Path(self._tmp.name) / "sessions", in_memory_events=True)
        self.session = self.store.create("test")
        self.registry = lc.Registry()
        self.engine = lc.Engine(self.registry, self.session)

    async def asyncTearDown(self):
        await self.engine.cancel_all()
        self._tmp.cleanup()

    def capability(self, cid, runner, **kwargs):
        kwargs.setdefault("outputs", (lc.Port(name="declared", artifact_type="json"),))
        cap = lc.Capability(id=cid, name=cid, description=cid, runner=cid, **kwargs)
        self.registry.register_capability(cap)
        self.registry.register_runner(cid, runner)
        return cap

    async def run_one(self, cid, **kwargs):
        graph = graph_from_capability(
            self.registry.capability(cid), sources={}, parameters={}, **kwargs
        )
        return await self.engine.execute(graph)


class TestApprovalGatesTheSideEffect(Harness):
    """``requires_approval`` used to be declared and then ignored."""

    async def test_runner_does_not_execute_before_approval(self):
        executed: list[str] = []

        async def destructive(ctx: lc.NodeContext) -> lc.NodeResult:
            executed.append("ran")
            return lc.NodeResult.ok()

        self.capability("danger.drop", destructive, requires_approval=True)
        run = await self.run_one("danger.drop")

        self.assertEqual(run.status, "paused_approval")
        self.assertEqual(executed, [], "runner ran before anyone approved it")
        self.assertEqual(run.pending_approvals, ["execute"])

    async def test_approval_then_runs_and_tells_the_runner(self):
        seen: list[bool] = []

        async def destructive(ctx: lc.NodeContext) -> lc.NodeResult:
            seen.append(bool(ctx.config.get("approved")))
            return lc.NodeResult.ok()

        self.capability("danger.drop", destructive, requires_approval=True)
        run = await self.run_one("danger.drop")
        self.assertTrue(run.approve("execute", True))
        await run.wait()

        self.assertEqual(run.status, "succeeded")
        self.assertEqual(seen, [True], "the runner should know it was approved")

    async def test_rejection_never_runs_the_work(self):
        executed: list[str] = []

        async def destructive(ctx: lc.NodeContext) -> lc.NodeResult:
            executed.append("ran")
            return lc.NodeResult.ok()

        self.capability("danger.drop", destructive, requires_approval=True)
        run = await self.run_one("danger.drop")
        run.approve("execute", False)
        await run.wait()

        self.assertEqual(run.status, "failed")
        self.assertEqual(executed, [])

    async def test_execute_does_not_deadlock_on_a_gate(self):
        """``execute`` returns at the gate instead of waiting for itself."""

        async def gated(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.capability("gated.thing", gated, requires_approval=True)
        run = await self.run_one("gated.thing")
        self.assertEqual(run.status, "paused_approval")


class TestArtifactsFromFailedAttempts(Harness):
    """A retry's partial output must not become a deliverable."""

    async def test_failed_attempt_artifacts_are_discarded(self):
        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", f"attempt-{ctx.attempt}.json", "{}")
            if ctx.attempt < 2:
                return lc.NodeResult.retry("transient")
            return lc.NodeResult.ok()

        self.capability(
            "flaky.thing", flaky, max_attempts=2, retry_backoff_seconds=0.0
        )
        run = await self.run_one("flaky.thing")

        self.assertEqual(run.status, "succeeded")
        names = [item["filename"] for item in self.session.list_artifacts()]
        self.assertEqual(names, ["attempt-2.json"])
        self.assertNotIn("attempt-1.json", names)

    async def test_a_permanently_failing_node_registers_nothing(self):
        async def doomed(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "partial.json", "{}")
            return lc.NodeResult.fail("no good")

        self.capability("doomed.thing", doomed)
        run = await self.run_one("doomed.thing")

        self.assertEqual(run.status, "failed")
        self.assertEqual(self.session.list_artifacts(), [])

    async def test_downstream_never_sees_a_discarded_artifact(self):
        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("out", f"a-{ctx.attempt}.json", "{}")
            if ctx.attempt < 2:
                return lc.NodeResult.retry("transient")
            return lc.NodeResult.ok()

        async def consumer(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(
                seen=sorted(item.filename for item in ctx.input_list("out"))
            )

        self.registry.register_runner("r.flaky", flaky)
        self.registry.register_runner("r.consume", consumer)
        graph = ExecutionGraph(
            id="g",
            name="g",
            nodes=(
                ExecutionNode(
                    id="produce",
                    name="produce",
                    runner="r.flaky",
                    outputs=("out",),
                    max_attempts=2,
                    retry_backoff_seconds=0.0,
                ),
                ExecutionNode(
                    id="consume",
                    name="consume",
                    runner="r.consume",
                    depends_on=("produce",),
                ),
            ),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.nodes["consume"].detail["seen"], ["a-2.json"])


class TestArtifactContract(Harness):
    """Emitted files stay inside the workdir and inside the declared ports."""

    async def test_filename_cannot_escape_the_workdir(self):
        async def escaping(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "../../escaped.json", "{}")
            return lc.NodeResult.ok()

        self.capability("escape.thing", escaping)
        run = await self.run_one("escape.thing")

        self.assertEqual(run.status, "failed")
        self.assertIn("inside the node workdir", run.nodes["execute"].error)
        self.assertEqual(self.session.list_artifacts(), [])

    async def test_absolute_filename_is_refused(self):
        async def absolute(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "/tmp/loomcraft-escape.json", "{}")
            return lc.NodeResult.ok()

        self.capability("absolute.thing", absolute)
        run = await self.run_one("absolute.thing")
        self.assertEqual(run.status, "failed")

    async def test_undeclared_port_is_refused(self):
        async def wrong_port(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("not_declared", "x.json", "{}")
            return lc.NodeResult.ok()

        self.capability("port.thing", wrong_port)
        run = await self.run_one("port.thing")

        self.assertEqual(run.status, "failed")
        self.assertIn("not declared", run.nodes["execute"].error)

    async def test_a_declared_port_still_works(self):
        async def fine(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "nested/ok.json", "{}")
            return lc.NodeResult.ok()

        self.capability("fine.thing", fine)
        run = await self.run_one("fine.thing")
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(
            [item["filename"] for item in self.session.list_artifacts()], ["ok.json"]
        )


class TestResultDetailIsBounded(Harness):
    async def test_non_serializable_detail_fails_the_node(self):
        async def bad(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(handle=object())

        self.capability("bad.detail", bad)
        run = await self.run_one("bad.detail")
        self.assertEqual(run.status, "failed")
        self.assertIn("not JSON-serializable", run.nodes["execute"].error)

    async def test_oversized_detail_fails_the_node(self):
        async def huge(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(blob="x" * (300 * 1024))

        self.capability("huge.detail", huge)
        run = await self.run_one("huge.detail")
        self.assertEqual(run.status, "failed")
        self.assertIn("KiB limit", run.nodes["execute"].error)


class TestHostDetailStaysOnTheHost(Harness):
    async def test_history_does_not_leak_the_artifact_path(self):
        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "result.json", "{}")
            return lc.NodeResult.ok()

        self.capability("leaky.thing", produce)
        await self.run_one("leaky.thing")

        stored = self.session.list_artifacts()[0]
        self.assertIn("relpath", stored, "the host still needs it internally")

        published = self.session.history()["artifacts"][0]
        self.assertNotIn("relpath", published)
        self.assertIn("source_ref", published)
        self.assertIn("checksum", published)

    def test_public_execution_drops_command_lines(self):
        scrubbed = lc.public_execution(
            {
                "id": "run-1",
                "status": "succeeded",
                "command": ["/usr/bin/secret-tool", "--key", "hunter2"],
                "workspace_path": "/srv/loomcraft/sessions/abc",
                "artifacts": [{"id": "a", "filename": "x", "relpath": "artifacts/a/x"}],
            }
        )
        self.assertNotIn("command", scrubbed)
        self.assertNotIn("workspace_path", scrubbed)
        self.assertNotIn("relpath", scrubbed["artifacts"][0])
        self.assertEqual(scrubbed["status"], "succeeded")


class TestNumericContracts(Harness):
    def test_nan_parameter_is_refused(self):
        capability = lc.Capability(
            id="p.x",
            name="p",
            description="d",
            runner="p.x",
            parameters={"threshold": lc.Parameter(type="number", description="t")},
            outputs=(lc.Port(name="o", artifact_type="json"),),
        )
        with self.assertRaises(lc.ContractError):
            capability.validate_parameters({"threshold": float("nan")})
        with self.assertRaises(lc.ContractError):
            capability.validate_parameters({"threshold": float("inf")})
        self.assertEqual(
            capability.validate_parameters({"threshold": 0.05}), {"threshold": 0.05}
        )


class TestRunRetention(Harness):
    async def test_terminal_runs_are_pruned_to_the_retention_bound(self):
        async def quick(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.capability("quick.thing", quick)
        engine = lc.Engine(self.registry, self.session, max_retained_runs=3)
        for _ in range(8):
            graph = graph_from_capability(
                self.registry.capability("quick.thing"), sources={}, parameters={}
            )
            await engine.execute(graph)
        self.assertLessEqual(len(engine._runs), 3)

    async def test_a_live_run_is_never_pruned(self):
        import asyncio

        release = asyncio.Event()

        async def blocked(ctx: lc.NodeContext) -> lc.NodeResult:
            await release.wait()
            return lc.NodeResult.ok()

        async def quick(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.capability("blocked.thing", blocked)
        self.capability("quick.thing", quick)
        engine = lc.Engine(self.registry, self.session, max_retained_runs=2)
        held = engine.submit(
            graph_from_capability(
                self.registry.capability("blocked.thing"), sources={}, parameters={}
            )
        )
        for _ in range(6):
            await engine.execute(
                graph_from_capability(
                    self.registry.capability("quick.thing"), sources={}, parameters={}
                )
            )
        self.assertIsNotNone(engine.get(held.id), "a running handle was pruned")
        release.set()
        await held.wait()


class TestSessionIdentity(Harness):
    def test_a_hostile_session_id_is_refused_not_rewritten(self):
        for bad in ("../escape", "a/b", "", "x" * 200):
            with self.assertRaises(lc.SourceError):
                self.store.create(bad)

    def test_duplicate_creation_is_refused(self):
        self.store.create("dup")
        with self.assertRaises(lc.SourceError):
            self.store.create("dup")

    def test_get_rejects_a_malformed_id(self):
        self.assertIsNone(self.store.get("../escape"))


class TestValidationDoesNotEchoInput(Harness):
    def test_rejected_extension_is_not_quoted_back(self):
        secret = ".verysecretinternalextension"
        with self.assertRaises(lc.InputRequestError) as caught:
            lc.validate_input_request(
                {
                    "title": "Files",
                    "message": "Upload",
                    "requirements": [
                        {
                            "key": "table",
                            "label": "Table",
                            "description": "A table",
                            "required": True,
                            "min_files": 1,
                            "max_files": 1,
                            "allowed_extensions": [secret, "bad"],
                        }
                    ],
                    "continue_prompt": "go",
                }
            )
        self.assertNotIn(secret, caught.exception.public_message)
        self.assertIn("extension", caught.exception.public_message)


class TestDeterministicOrdering(Harness):
    def test_topological_order_is_lexicographically_smallest(self):
        forward = lc.topological_order({"b": [], "a": [], "c": ["a", "b"]})
        reverse = lc.topological_order({"c": ["b", "a"], "a": [], "b": []})
        self.assertEqual(forward, ["a", "b", "c"])
        self.assertEqual(forward, reverse, "declaration order changed the result")


class TestSerializedRunIsPublishable(Harness):
    async def test_run_to_dict_is_json_safe_and_scrubbed(self):
        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("declared", "out.json", "{}")
            return lc.NodeResult.ok(summary="done")

        self.capability("pub.thing", produce)
        run = await self.run_one("pub.thing")
        payload = run.to_dict()
        json.dumps(payload, allow_nan=False)
        self.assertNotIn("relpath", payload["artifacts"][0])
        self.assertNotIn("relpath", payload["nodes"]["execute"]["artifacts"][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
