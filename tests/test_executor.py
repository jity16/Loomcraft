import asyncio
import time
import unittest

from loomcraft import ApprovalRequired, DAGExecutor, ExecutionError, InMemoryStore, Registry, StepResult


class ExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_branches_and_retry(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("s")
        started = {}
        attempts = {"flaky": 0}

        async def branch(context):
            started[context.step["id"]] = time.monotonic()
            await asyncio.sleep(0.05)
            return StepResult(output={context.step["id"]: True})

        async def flaky(context):
            attempts["flaky"] += 1
            if attempts["flaky"] < 2:
                raise RuntimeError("temporary")
            return StepResult(output={"ok": True})

        async def join(context):
            return StepResult(summary="joined", output=context.dependencies)

        for identifier, handler in (("a", branch), ("b", branch), ("flaky", flaky)):
            registry.register_capability(id=identifier, name=identifier, handler=handler)
        registry.register_capability(id="join", name="join", handler=join)
        executor = DAGExecutor(registry, store=store, max_concurrency=2)
        plan = {
            "goal": "test",
            "revision": 1,
            "steps": [
                {"id": "a", "title": "A", "kind": "capability", "capability": "a"},
                {"id": "b", "title": "B", "kind": "capability", "capability": "b"},
                {"id": "flaky", "title": "Flaky", "kind": "capability", "capability": "flaky", "retry": {"max_attempts": 2}},
                {"id": "join", "title": "Join", "kind": "capability", "capability": "join", "depends_on": ["a", "b", "flaky"]},
            ],
        }
        result = await executor.execute(plan, session_id="s", reset=True)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.steps["flaky"].attempts, 2)
        self.assertLess(abs(started["a"] - started["b"]), 0.04)
        self.assertTrue(any(event.event == "step_retry" for event in store.read_events("s")))
        self.assertEqual(store.read_events("s")[0].event, "plan_published")

    async def test_failure_blocks_downstream_and_continue_allows_it(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("s")

        async def fail(context):
            raise RuntimeError("nope")

        async def pass_step(context):
            return StepResult(summary="ran")

        registry.register_capability(id="fail", name="fail", handler=fail)
        registry.register_capability(id="pass", name="pass", handler=pass_step)
        executor = DAGExecutor(registry, store=store)
        plan = {
            "goal": "failure",
            "revision": 1,
            "steps": [
                {"id": "bad", "title": "bad", "kind": "capability", "capability": "fail", "on_failure": "continue"},
                {"id": "next", "title": "next", "kind": "capability", "capability": "pass", "depends_on": ["bad"]},
            ],
        }
        result = await executor.execute(plan, session_id="s", reset=True)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps["next"].status, "succeeded")

    async def test_cancel_marks_running_and_pending(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("s")

        async def slow(context):
            await asyncio.sleep(10)
            return StepResult()

        registry.register_capability(id="slow", name="slow", handler=slow)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "cancel", "revision": 1, "steps": [{"id": "slow", "title": "slow", "kind": "capability", "capability": "slow"}]}
        task = asyncio.create_task(executor.execute(plan, session_id="s", run_id="cancel-me", reset=True))
        await asyncio.sleep(0.03)
        self.assertTrue(await executor.cancel("cancel-me"))
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(store.get_current_plan("s")["steps"][0]["status"], "cancelled")

    async def test_concurrent_reset_cannot_mutate_an_active_plan(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("busy")
        async def slow(context):
            await asyncio.sleep(1)
            return StepResult()
        registry.register_capability(id="slow", name="slow", handler=slow)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "busy", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "slow"}]}
        task = asyncio.create_task(executor.execute(plan, session_id="busy", reset=True))
        await asyncio.sleep(0.03)
        with self.assertRaises(ExecutionError):
            await executor.execute(plan, session_id="busy", reset=True)
        self.assertEqual(store.get_current_plan("busy")["steps"][0]["status"], "running")
        run_id = executor.active_runs("busy")[0]
        await executor.cancel(run_id)
        await task

    async def test_run_timeout_is_observable(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("timeout")

        async def slow(context):
            await asyncio.sleep(1)
            return StepResult()

        registry.register_capability(id="slow", name="slow", handler=slow)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "timeout", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "slow"}]}
        result = await executor.execute(plan, session_id="timeout", reset=True, timeout_seconds=0.03)
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(any(event.event == "run_timeout" for event in store.read_events("timeout")))

    async def test_stale_running_node_fails_closed_on_resume(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("stale")
        async def done(context):
            return StepResult()
        registry.register_capability(id="x", name="x", handler=done)
        plan = {"goal": "stale", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "x", "status": "running"}]}
        store.update_current_plan("stale", plan)
        result = await DAGExecutor(registry, store=store).execute(plan, session_id="stale")
        self.assertEqual(result.status, "failed")
        self.assertEqual(store.get_current_plan("stale")["steps"][0]["status"], "failed")

    async def test_approval_pause_preserves_downstream_and_resumes(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("approval")
        approved = {"value": False}

        async def review(context):
            if not approved["value"]:
                raise ApprovalRequired("human review", {"confidence": 0.7})
            return StepResult(output={"approved": True})

        async def downstream(context):
            return StepResult(output=context.dependencies)

        registry.register_handler("review", review)
        registry.register_handler("dynamic", downstream)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "approval", "revision": 1, "steps": [
            {"id": "review", "title": "review", "kind": "review"},
            {"id": "next", "title": "next", "kind": "dynamic", "depends_on": ["review"]},
        ]}
        paused = await executor.execute(plan, session_id="approval", reset=True)
        self.assertEqual(paused.status, "waiting_approval")
        self.assertEqual(store.get_current_plan("approval")["steps"][1]["status"], "pending")
        approved["value"] = True
        resumed = await executor.approve(paused.run_id, "review")
        self.assertEqual(resumed.status, "succeeded")

    async def test_unregistered_review_uses_safe_approval_boundary(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("default-review")
        executor = DAGExecutor(registry, store=store)
        result = await executor.execute({"goal": "review", "revision": 1, "steps": [{"id": "review", "title": "Review", "kind": "review"}]}, session_id="default-review", reset=True)
        self.assertEqual(result.status, "waiting_approval")

    async def test_require_approval_policy_pauses_after_final_failure(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("policy")

        async def fail(context):
            raise RuntimeError("needs a human decision")

        registry.register_capability(id="fail", name="fail", handler=fail)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "policy", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "fail", "on_failure": "require_approval"}]}
        result = await executor.execute(plan, session_id="policy", reset=True)
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(store.get_current_plan("policy")["steps"][0]["status"], "waiting_approval")

    async def test_handler_can_return_structured_failure_for_retry(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("structured")
        seen = {"count": 0}

        async def handler(context):
            seen["count"] += 1
            if seen["count"] == 1:
                return {"status": "failed", "error": "temporary result", "summary": "retry me"}
            return {"status": "succeeded", "output": {"ok": True}}

        registry.register_capability(id="x", name="x", handler=handler)
        result = await DAGExecutor(registry, store=store).execute({"goal": "structured", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "x", "retry": {"max_attempts": 2}}]}, session_id="structured", reset=True)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.steps["x"].attempts, 2)

    async def test_cancel_approval_pause(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("approval-cancel")

        async def review(context):
            raise ApprovalRequired("review")

        registry.register_handler("review", review)
        executor = DAGExecutor(registry, store=store)
        plan = {"goal": "cancel approval", "revision": 1, "steps": [{"id": "r", "title": "r", "kind": "review"}]}
        paused = await executor.execute(plan, session_id="approval-cancel", reset=True)
        self.assertTrue(await executor.cancel(paused.run_id))
        self.assertEqual(store.get_current_plan("approval-cancel")["steps"][0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
