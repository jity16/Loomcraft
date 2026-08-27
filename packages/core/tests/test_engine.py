"""Engine behaviour: parallelism, retry, timeout, skips, approval, cancellation."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import loomcraft as lc
from loomcraft.engine import ExecutionGraph, ExecutionNode


class EngineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = lc.SessionStore(Path(self._tmp.name) / "sessions", in_memory_events=True)
        self.session = self.store.create("test")
        self.registry = lc.Registry()
        self.events: list[str] = []
        self.engine = lc.Engine(
            self.registry,
            self.session,
            emit=lambda name, data: self.events.append(name),
        )

    async def asyncTearDown(self):
        await self.engine.cancel_all()
        self._tmp.cleanup()

    def node(self, node_id, runner, **kwargs):
        return ExecutionNode(id=node_id, name=node_id, runner=runner, **kwargs)


class TestParallelism(EngineCase):
    async def test_independent_nodes_run_concurrently(self):
        active = {"now": 0, "peak": 0}

        async def slow(ctx: lc.NodeContext) -> lc.NodeResult:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return lc.NodeResult.ok()

        self.registry.register_runner("slow", slow)
        graph = ExecutionGraph(
            id="g",
            name="fan-out",
            nodes=tuple(self.node(f"n{i}", "slow") for i in range(5)),
        )
        started = time.monotonic()
        run = await self.engine.execute(graph)
        elapsed = time.monotonic() - started

        self.assertEqual(run.status, "succeeded")
        self.assertEqual(active["peak"], 5, "all five nodes should overlap")
        self.assertLess(elapsed, 0.2, "concurrent nodes should not serialise")

    async def test_dependencies_serialise_execution(self):
        order: list[str] = []

        async def record(ctx: lc.NodeContext) -> lc.NodeResult:
            order.append(ctx.node_id)
            await asyncio.sleep(0.01)
            return lc.NodeResult.ok()

        self.registry.register_runner("record", record)
        graph = ExecutionGraph(
            id="g",
            name="chain",
            nodes=(
                self.node("a", "record"),
                self.node("b", "record", depends_on=("a",)),
                self.node("c", "record", depends_on=("b",)),
            ),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(order, ["a", "b", "c"])

    async def test_max_parallel_is_respected(self):
        active = {"now": 0, "peak": 0}

        async def slow(ctx: lc.NodeContext) -> lc.NodeResult:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.03)
            active["now"] -= 1
            return lc.NodeResult.ok()

        self.registry.register_runner("slow", slow)
        engine = lc.Engine(self.registry, self.session, max_parallel=2, emit=lambda *_: None)
        graph = ExecutionGraph(
            id="g", name="capped", nodes=tuple(self.node(f"n{i}", "slow") for i in range(6))
        )
        run = await engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertLessEqual(active["peak"], 2)


class TestRetry(EngineCase):
    async def test_retries_until_success(self):
        calls = {"n": 0}

        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            calls["n"] += 1
            if calls["n"] < 3:
                return lc.NodeResult.retry("transient")
            return lc.NodeResult.ok(attempt=ctx.attempt)

        self.registry.register_runner("flaky", flaky)
        graph = ExecutionGraph(
            id="g",
            name="retry",
            nodes=(self.node("n", "flaky", max_attempts=3, retry_backoff_seconds=0.001),),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.nodes["n"].attempts, 3)
        self.assertEqual(calls["n"], 3)

    async def test_exhausted_retries_fail_the_run(self):
        async def always(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.retry("still broken")

        self.registry.register_runner("always", always)
        graph = ExecutionGraph(
            id="g",
            name="retry",
            nodes=(self.node("n", "always", max_attempts=2, retry_backoff_seconds=0.001),),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.nodes["n"].attempts, 2)

    async def test_non_retryable_failure_is_not_retried(self):
        calls = {"n": 0}

        async def hard_fail(ctx: lc.NodeContext) -> lc.NodeResult:
            calls["n"] += 1
            return lc.NodeResult.fail("permanent")

        self.registry.register_runner("hard", hard_fail)
        graph = ExecutionGraph(
            id="g",
            name="retry",
            nodes=(self.node("n", "hard", max_attempts=5, retry_backoff_seconds=0.001),),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertEqual(calls["n"], 1, "a permanent failure should run once")

    async def test_raised_exception_becomes_a_failure_not_a_crash(self):
        async def explode(ctx: lc.NodeContext) -> lc.NodeResult:
            raise ValueError("boom")

        self.registry.register_runner("explode", explode)
        graph = ExecutionGraph(id="g", name="x", nodes=(self.node("n", "explode"),))
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertIn("ValueError", run.nodes["n"].error or "")

    async def test_attempt_number_is_visible_to_the_runner(self):
        seen: list[int] = []

        async def note(ctx: lc.NodeContext) -> lc.NodeResult:
            seen.append(ctx.attempt)
            return lc.NodeResult.retry("again") if ctx.attempt < 3 else lc.NodeResult.ok()

        self.registry.register_runner("note", note)
        graph = ExecutionGraph(
            id="g",
            name="a",
            nodes=(self.node("n", "note", max_attempts=3, retry_backoff_seconds=0.001),),
        )
        await self.engine.execute(graph)
        self.assertEqual(seen, [1, 2, 3])


class TestTimeout(EngineCase):
    async def test_timeout_fails_the_node(self):
        async def hang(ctx: lc.NodeContext) -> lc.NodeResult:
            await asyncio.sleep(5)
            return lc.NodeResult.ok()

        self.registry.register_runner("hang", hang)
        graph = ExecutionGraph(
            id="g", name="t", nodes=(self.node("n", "hang", timeout_seconds=0.05),)
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertIn("timed out", run.nodes["n"].error or "")

    async def test_timeout_is_retried_when_budget_remains(self):
        calls = {"n": 0}

        async def slow_then_fast(ctx: lc.NodeContext) -> lc.NodeResult:
            calls["n"] += 1
            await asyncio.sleep(0.5 if calls["n"] == 1 else 0.001)
            return lc.NodeResult.ok()

        self.registry.register_runner("mixed", slow_then_fast)
        graph = ExecutionGraph(
            id="g",
            name="t",
            nodes=(
                self.node(
                    "n",
                    "mixed",
                    timeout_seconds=0.05,
                    max_attempts=2,
                    retry_backoff_seconds=0.001,
                ),
            ),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(calls["n"], 2)


class TestSkipPropagation(EngineCase):
    async def test_downstream_of_failure_is_skipped_not_run(self):
        ran: list[str] = []

        async def ok(ctx: lc.NodeContext) -> lc.NodeResult:
            ran.append(ctx.node_id)
            return lc.NodeResult.ok()

        async def bad(ctx: lc.NodeContext) -> lc.NodeResult:
            ran.append(ctx.node_id)
            return lc.NodeResult.fail("nope")

        self.registry.register_runner("ok", ok)
        self.registry.register_runner("bad", bad)
        graph = ExecutionGraph(
            id="g",
            name="skip",
            nodes=(
                self.node("root", "bad"),
                self.node("mid", "ok", depends_on=("root",)),
                self.node("leaf", "ok", depends_on=("mid",)),
                self.node("island", "ok"),
            ),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.nodes["mid"].status, "skipped")
        self.assertEqual(run.nodes["leaf"].status, "skipped")
        self.assertEqual(run.nodes["island"].status, "succeeded")
        self.assertNotIn("mid", ran)
        self.assertIn("island", ran)


class TestApproval(EngineCase):
    async def test_run_pauses_then_resumes_on_approval(self):
        async def gate(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.needs_approval("please confirm")

        async def after(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.registry.register_runner("gate", gate)
        self.registry.register_runner("after", after)
        graph = ExecutionGraph(
            id="g",
            name="approval",
            nodes=(self.node("gate", "gate"), self.node("after", "after", depends_on=("gate",))),
        )
        run = self.engine.submit(graph)
        for _ in range(200):
            await asyncio.sleep(0.005)
            if run.pending_approvals:
                break
        self.assertEqual(run.pending_approvals, ["gate"])
        self.assertEqual(run.status, "paused_approval")
        self.assertIn("approval_required", self.events)

        self.assertTrue(run.approve("gate", True))
        await run.wait()
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.nodes["after"].status, "succeeded")

    async def test_rejection_fails_the_node_and_skips_downstream(self):
        async def gate(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.needs_approval()

        async def after(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.registry.register_runner("gate", gate)
        self.registry.register_runner("after", after)
        graph = ExecutionGraph(
            id="g",
            name="approval",
            nodes=(self.node("gate", "gate"), self.node("after", "after", depends_on=("gate",))),
        )
        run = self.engine.submit(graph)
        for _ in range(200):
            await asyncio.sleep(0.005)
            if run.pending_approvals:
                break
        run.approve("gate", False)
        await run.wait()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.nodes["after"].status, "skipped")


class TestCancellation(EngineCase):
    async def test_cancel_stops_the_run_and_awaits_node_tasks(self):
        finished = {"count": 0}

        async def slow(ctx: lc.NodeContext) -> lc.NodeResult:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            finished["count"] += 1
            return lc.NodeResult.ok()

        self.registry.register_runner("slow", slow)
        graph = ExecutionGraph(
            id="g", name="c", nodes=tuple(self.node(f"n{i}", "slow") for i in range(3))
        )
        run = self.engine.submit(graph)
        await asyncio.sleep(0.05)
        self.assertTrue(await run.cancel())
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(finished["count"], 0)
        self.assertTrue(all(task.done() for task in run._tasks))

    async def test_cancelling_a_finished_run_is_a_no_op(self):
        async def quick(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.registry.register_runner("quick", quick)
        graph = ExecutionGraph(id="g", name="c", nodes=(self.node("n", "quick"),))
        run = await self.engine.execute(graph)
        self.assertFalse(await run.cancel())
        self.assertEqual(run.status, "succeeded")

    async def test_runner_can_observe_cancellation(self):
        observed = {"cancelled": False}

        async def watcher(ctx: lc.NodeContext) -> lc.NodeResult:
            for _ in range(500):
                if ctx.cancelled:
                    observed["cancelled"] = True
                    return lc.NodeResult.fail("cancelled by request")
                await asyncio.sleep(0.005)
            return lc.NodeResult.ok()

        self.registry.register_runner("watch", watcher)
        graph = ExecutionGraph(id="g", name="c", nodes=(self.node("n", "watch"),))
        run = self.engine.submit(graph)
        await asyncio.sleep(0.05)
        run._cancel.set()
        await asyncio.sleep(0.05)
        self.assertTrue(observed["cancelled"])
        await run.cancel()


class TestArtifactsAndInputs(EngineCase):
    async def test_artifacts_flow_from_upstream_to_downstream_by_port(self):
        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("table", "out.csv", "id,value\n1,42\n")
            return lc.NodeResult.ok()

        async def consume(ctx: lc.NodeContext) -> lc.NodeResult:
            upstream = ctx.input("table")
            return lc.NodeResult.ok(rows=len(upstream.read_text().splitlines()))

        self.registry.register_runner("produce", produce)
        self.registry.register_runner("consume", consume)
        graph = ExecutionGraph(
            id="g",
            name="pipe",
            nodes=(
                self.node("produce", "produce", outputs=("table",)),
                self.node("consume", "consume", depends_on=("produce",)),
            ),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.nodes["consume"].detail["rows"], 2)
        self.assertEqual(len(run.artifacts), 1)

    async def test_uploads_resolve_through_source_refs(self):
        upload = self.session.save_upload("data.csv", b"a,b\n1,2\n")

        async def read(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(text=ctx.input("doc").read_text())

        self.registry.register_runner("read", read)
        graph = ExecutionGraph(
            id="g",
            name="read",
            nodes=(self.node("n", "read", inputs={"doc": (upload["source_ref"],)}),),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.nodes["n"].detail["text"], "a,b\n1,2\n")

    async def test_missing_source_fails_the_node_cleanly(self):
        async def read(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.registry.register_runner("read", read)
        graph = ExecutionGraph(
            id="g",
            name="read",
            nodes=(self.node("n", "read", inputs={"doc": ("upload:nope",)}),),
        )
        run = await self.engine.execute(graph)
        self.assertEqual(run.status, "failed")
        self.assertIn("input binding failed", run.nodes["n"].error or "")


class TestGraphGuards(EngineCase):
    async def test_unregistered_runner_is_rejected_at_submit(self):
        graph = ExecutionGraph(id="g", name="x", nodes=(self.node("n", "missing.runner"),))
        with self.assertRaises(lc.RegistryError):
            self.engine.submit(graph)

    async def test_cyclic_graph_cannot_be_constructed(self):
        with self.assertRaises(lc.RegistryError):
            ExecutionGraph(
                id="g",
                name="x",
                nodes=(
                    ExecutionNode(id="a", name="a", runner="r", depends_on=("b",)),
                    ExecutionNode(id="b", name="b", runner="r", depends_on=("a",)),
                ),
            )

    async def test_duplicate_node_ids_are_rejected(self):
        with self.assertRaises(lc.RegistryError):
            ExecutionGraph(
                id="g",
                name="x",
                nodes=(
                    ExecutionNode(id="a", name="a", runner="r"),
                    ExecutionNode(id="a", name="a2", runner="r"),
                ),
            )


if __name__ == "__main__":
    unittest.main()
