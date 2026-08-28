"""Whole-plan execution: scheduling, policy, objectives, and the app-server bridge."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import loomcraft as lc
from loomcraft.plan_executor import build_plan_graph, resolve_retry


def plan(steps, **extra):
    return {"goal": "investigate", "revision": 1, "steps": steps, **extra}


class PlanCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = lc.SessionStore(Path(self._tmp.name) / "sessions", in_memory_events=True)
        self.session = self.store.create("test")
        self.registry = lc.Registry()
        self.engine = lc.Engine(self.registry, self.session)
        self.broker = lc.ToolBroker(self.session, self.registry, engine=self.engine)
        self.broker.begin_turn()

    async def asyncTearDown(self):
        await self.broker.close()
        await self.engine.cancel_all()
        self._tmp.cleanup()

    def capability(self, cid, runner, *, outputs=("out",), **kwargs):
        cap = lc.Capability(
            id=cid,
            name=cid,
            description=cid,
            runner=cid,
            outputs=tuple(lc.Port(name=name, artifact_type="json") for name in outputs),
            **kwargs,
        )
        self.registry.register_capability(cap)
        self.registry.register_runner(cid, runner)
        return cap

    async def publish(self, payload):
        response = await self.broker.dispatch("publish_plan", {"plan": payload})
        self.assertTrue(response.ok, response.error)
        return response

    async def execute(self, **payload):
        return await self.broker.dispatch("execute_plan", payload)


class TestScheduling(PlanCase):
    async def test_independent_steps_run_concurrently(self):
        active = {"now": 0, "peak": 0}

        async def slow(ctx: lc.NodeContext) -> lc.NodeResult:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
            await asyncio.sleep(0.05)
            active["now"] -= 1
            return lc.NodeResult.ok()

        self.capability("c.root", slow)
        self.capability("c.leaf", slow)
        await self.publish(
            plan(
                [
                    {"id": "root", "title": "Root", "kind": "capability", "capability": "c.root"},
                    {"id": "a", "title": "A", "kind": "capability", "capability": "c.leaf", "depends_on": ["root"]},
                    {"id": "b", "title": "B", "kind": "capability", "capability": "c.leaf", "depends_on": ["root"]},
                ]
            )
        )
        started = time.monotonic()
        response = await self.execute()
        elapsed = time.monotonic() - started

        self.assertTrue(response.ok, response.error)
        self.assertEqual(active["peak"], 2, "siblings were serialised")
        self.assertLess(elapsed, 0.2, "two 50ms siblings should overlap")

    async def test_plan_state_is_projected_back_onto_the_steps(self):
        async def ok(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(summary="did the thing")

        self.capability("c.ok", ok)
        await self.publish(
            plan([{"id": "s", "title": "S", "kind": "capability", "capability": "c.ok"}])
        )
        await self.execute()

        step = lc.get_step(self.session.current_plan(), "s")
        self.assertEqual(step["status"], "succeeded")
        self.assertEqual(step["summary"], "did the thing")
        self.assertEqual(step["attempts"], 1)
        self.assertEqual(step["execution"]["status"], "succeeded")

    async def test_a_second_execution_while_busy_is_refused(self):
        release = asyncio.Event()

        async def blocked(ctx: lc.NodeContext) -> lc.NodeResult:
            await release.wait()
            return lc.NodeResult.ok()

        self.capability("c.block", blocked)
        await self.publish(
            plan([{"id": "s", "title": "S", "kind": "capability", "capability": "c.block"}])
        )
        first = asyncio.create_task(self.execute())
        await asyncio.sleep(0.05)
        second = await self.execute()
        self.assertFalse(second.ok)
        self.assertEqual(second.error_code, "BROKER_EXECUTION_BUSY")
        release.set()
        await first


class TestFailurePolicy(PlanCase):
    async def _two_branch_plan(self, policy):
        async def fails(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.fail("branch came back empty")

        async def ok(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok()

        self.capability("c.fail", fails)
        self.capability("c.ok", ok)
        await self.publish(
            plan(
                [
                    {"id": "risky", "title": "Risky", "kind": "capability", "capability": "c.fail", "on_failure": policy},
                    {"id": "after", "title": "After", "kind": "capability", "capability": "c.ok", "depends_on": ["risky"]},
                ]
            )
        )
        return await self.execute()

    async def test_stop_skips_the_dependent(self):
        response = await self._two_branch_plan("stop")
        self.assertFalse(response.ok)
        current = self.session.current_plan()
        self.assertEqual(lc.get_step(current, "risky")["status"], "failed")
        self.assertEqual(lc.get_step(current, "after")["status"], "skipped")

    async def test_continue_runs_the_dependent_and_still_reports_the_failure(self):
        response = await self._two_branch_plan("continue")
        self.assertTrue(response.ok, "a tolerated failure is not a failed run")
        current = self.session.current_plan()
        self.assertEqual(lc.get_step(current, "risky")["status"], "failed")
        self.assertEqual(lc.get_step(current, "after")["status"], "succeeded")

        tolerated = [row for row in response.result["failed_nodes"] if row["tolerated"]]
        self.assertEqual([row["node_id"] for row in tolerated], ["risky"])

    async def test_require_approval_parks_instead_of_failing(self):
        async def fails(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.fail("needs a human")

        self.capability("c.fail", fails)
        await self.publish(
            plan(
                [
                    {
                        "id": "s",
                        "title": "S",
                        "kind": "capability",
                        "capability": "c.fail",
                        "on_failure": "require_approval",
                    }
                ]
            )
        )
        response = await self.execute()
        self.assertTrue(response.ok, "a pause is not a failure")
        self.assertEqual(response.result["status"], "paused_approval")
        self.assertEqual(
            lc.get_step(self.session.current_plan(), "s")["status"], "waiting_approval"
        )


class TestRetryPolicy(PlanCase):
    async def test_plan_retry_drives_the_attempts(self):
        attempts = {"n": 0}

        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            attempts["n"] = ctx.attempt
            if ctx.attempt < 3:
                return lc.NodeResult.retry("transient")
            return lc.NodeResult.ok()

        self.capability("c.flaky", flaky)
        await self.publish(
            plan(
                [
                    {
                        "id": "s",
                        "title": "S",
                        "kind": "capability",
                        "capability": "c.flaky",
                        "retry": {"max_attempts": 3, "backoff_seconds": 0.0},
                    }
                ]
            )
        )
        response = await self.execute()
        self.assertTrue(response.ok, response.error)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(lc.get_step(self.session.current_plan(), "s")["attempts"], 3)

    async def test_an_omitted_plan_policy_inherits_the_capability(self):
        attempts = {"n": 0}

        async def flaky(ctx: lc.NodeContext) -> lc.NodeResult:
            attempts["n"] = ctx.attempt
            if ctx.attempt < 2:
                return lc.NodeResult.retry("transient")
            return lc.NodeResult.ok()

        self.capability("c.flaky", flaky, max_attempts=2, retry_backoff_seconds=0.0)
        await self.publish(
            plan([{"id": "s", "title": "S", "kind": "capability", "capability": "c.flaky"}])
        )
        response = await self.execute()
        self.assertTrue(response.ok, "publishing a plan downgraded the retry budget")
        self.assertEqual(attempts["n"], 2)

    def test_resolve_retry_prefers_an_explicit_plan_policy(self):
        default = lc.RetryPolicy()
        explicit = lc.RetryPolicy(max_attempts=5, backoff_seconds=1.0)
        self.assertEqual(
            resolve_retry(default, attempts=3, backoff=2.0), (3, 2.0, 2.0, None)
        )
        self.assertEqual(
            resolve_retry(explicit, attempts=3, backoff=2.0), (5, 1.0, 2.0, 60.0)
        )

    def test_backoff_is_capped(self):
        policy = lc.RetryPolicy(
            max_attempts=10,
            backoff_seconds=1.0,
            backoff_multiplier=10.0,
            max_backoff_seconds=30.0,
        )
        self.assertEqual(policy.delay_for(1), 1.0)
        self.assertEqual(policy.delay_for(2), 10.0)
        self.assertEqual(policy.delay_for(3), 30.0, "cap was not applied")
        self.assertEqual(policy.delay_for(9), 30.0)


class TestArtifactFlow(PlanCase):
    async def test_an_upstream_port_maps_onto_a_different_input_key(self):
        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("cleaned", "clean.csv", "a,b\n1,2\n")
            return lc.NodeResult.ok()

        async def consume(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(read=ctx.input("table").filename)

        self.capability("c.produce", produce, outputs=("cleaned",))
        consumer = lc.Capability(
            id="c.consume",
            name="c.consume",
            description="d",
            runner="c.consume",
            # Declares `table`, but upstream emits on the port `cleaned`.
            inputs=(
                lc.CapabilityInput(
                    key="table",
                    name="Table",
                    description="A table",
                    port_name="cleaned",
                    allowed_extensions=(".csv",),
                ),
            ),
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(consumer)
        self.registry.register_runner("c.consume", consume)

        await self.publish(
            plan(
                [
                    {"id": "produce", "title": "P", "kind": "capability", "capability": "c.produce"},
                    {"id": "consume", "title": "C", "kind": "capability", "capability": "c.consume", "depends_on": ["produce"]},
                ]
            )
        )
        response = await self.execute()
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.result["nodes"]["consume"]["detail"]["read"], "clean.csv")

    async def test_a_declared_extension_is_enforced_on_upstream_files(self):
        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("cleaned", "clean.txt", "not a csv")
            return lc.NodeResult.ok()

        async def consume(ctx: lc.NodeContext) -> lc.NodeResult:  # pragma: no cover
            return lc.NodeResult.ok()

        self.capability("c.produce", produce, outputs=("cleaned",))
        consumer = lc.Capability(
            id="c.consume",
            name="c.consume",
            description="d",
            runner="c.consume",
            inputs=(
                lc.CapabilityInput(
                    key="table",
                    name="Table",
                    description="A table",
                    port_name="cleaned",
                    allowed_extensions=(".csv",),
                ),
            ),
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(consumer)
        self.registry.register_runner("c.consume", consume)

        await self.publish(
            plan(
                [
                    {"id": "produce", "title": "P", "kind": "capability", "capability": "c.produce"},
                    {"id": "consume", "title": "C", "kind": "capability", "capability": "c.consume", "depends_on": ["produce"]},
                ]
            )
        )
        response = await self.execute()
        self.assertFalse(response.ok)
        self.assertEqual(
            lc.get_step(self.session.current_plan(), "consume")["status"], "failed"
        )

    async def test_step_scoped_inputs_do_not_leak_into_other_steps(self):
        """A step with no entry in a step-keyed map gets nothing, not everything."""

        async def root(ctx: lc.NodeContext) -> lc.NodeResult:
            ctx.emit("out", "root.json", "{}")
            return lc.NodeResult.ok()

        async def leaf(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(keys=sorted(ctx.inputs))

        capability = lc.Capability(
            id="c.root",
            name="c.root",
            description="d",
            runner="c.root",
            inputs=(lc.CapabilityInput(key="table", name="T", description="d"),),
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(capability)
        self.registry.register_runner("c.root", root)
        self.capability("c.leaf", leaf)

        upload = self.session.save_upload("t.csv", b"a,b\n1,2\n")
        await self.publish(
            plan(
                [
                    {"id": "root", "title": "R", "kind": "capability", "capability": "c.root"},
                    {"id": "leaf", "title": "L", "kind": "capability", "capability": "c.leaf", "depends_on": ["root"]},
                ]
            )
        )
        response = await self.execute(
            inputs={"root": {"inputs": {"table": upload["source_ref"]}}}
        )
        self.assertTrue(response.ok, response.error)
        # `leaf` sees the upstream artifact only — not the map meant for `root`.
        self.assertEqual(response.result["nodes"]["leaf"]["detail"]["keys"], ["out"])

    async def test_a_flat_binding_still_works_for_a_single_step_plan(self):
        async def only(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(read=ctx.input("table").filename)

        capability = lc.Capability(
            id="c.only",
            name="c.only",
            description="d",
            runner="c.only",
            inputs=(lc.CapabilityInput(key="table", name="T", description="d"),),
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(capability)
        self.registry.register_runner("c.only", only)

        upload = self.session.save_upload("t.csv", b"a,b\n1,2\n")
        await self.publish(
            plan([{"id": "s", "title": "S", "kind": "capability", "capability": "c.only"}])
        )
        response = await self.execute(inputs={"table": upload["source_ref"]})
        self.assertTrue(response.ok, response.error)
        self.assertEqual(response.result["nodes"]["s"]["detail"]["read"], "t.csv")

    async def test_an_unknown_source_fails_before_anything_runs(self):
        executed: list[str] = []

        async def produce(ctx: lc.NodeContext) -> lc.NodeResult:  # pragma: no cover
            executed.append("ran")
            return lc.NodeResult.ok()

        capability = lc.Capability(
            id="c.needs_file",
            name="c.needs_file",
            description="d",
            runner="c.needs_file",
            inputs=(
                lc.CapabilityInput(key="table", name="Table", description="A table"),
            ),
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(capability)
        self.registry.register_runner("c.needs_file", produce)

        await self.publish(
            plan(
                [
                    {"id": "s", "title": "S", "kind": "capability", "capability": "c.needs_file"}
                ]
            )
        )
        response = await self.execute(inputs={"s": {"inputs": {"table": "upload:nope"}}})
        self.assertFalse(response.ok)
        self.assertEqual(executed, [], "work started despite a bad input")


class TestAgentOwnedSteps(PlanCase):
    async def test_an_unhandled_dynamic_step_is_refused_with_guidance(self):
        await self.publish(
            plan([{"id": "d", "title": "D", "kind": "dynamic"}])
        )
        response = await self.execute()
        self.assertFalse(response.ok)
        self.assertIn("handler", response.error)

    async def test_a_registered_handler_runs_the_dynamic_step(self):
        async def handler(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(summary=f"handled {ctx.config['plan_step']}")

        self.registry.register_step_handler("dynamic", handler)
        await self.publish(plan([{"id": "d", "title": "D", "kind": "dynamic"}]))
        response = await self.execute()
        self.assertTrue(response.ok, response.error)
        self.assertEqual(
            lc.get_step(self.session.current_plan(), "d")["summary"], "handled d"
        )

    async def test_an_unbound_review_waits_for_a_person(self):
        await self.publish(plan([{"id": "r", "title": "R", "kind": "review"}]))
        response = await self.execute()
        self.assertTrue(response.ok)
        self.assertEqual(response.result["status"], "paused_approval")
        self.assertEqual(
            lc.get_step(self.session.current_plan(), "r")["status"], "waiting_approval"
        )

    async def test_an_answer_step_needs_no_handler(self):
        await self.publish(
            plan([{"id": "a", "title": "Answer", "kind": "answer", "description": "final"}])
        )
        response = await self.execute()
        self.assertTrue(response.ok, response.error)


class TestReviewBoundCapability(PlanCase):
    async def _register_review(self, cid="review.lambda", tags=()):
        async def check(ctx: lc.NodeContext) -> lc.NodeResult:
            return lc.NodeResult.ok(summary="lambda within range")

        cap = lc.Capability(
            id=cid,
            name=cid,
            description="d",
            runner=cid,
            tags=tags,
            outputs=(lc.Port(name="out", artifact_type="json"),),
        )
        self.registry.register_capability(cap)
        self.registry.register_runner(cid, check)

    async def test_a_review_runner_qualifies_a_review_step(self):
        await self._register_review()
        await self.publish(
            plan(
                [
                    {"id": "r", "title": "R", "kind": "review", "capability": "review.lambda"}
                ]
            )
        )
        response = await self.execute()
        self.assertTrue(response.ok, response.error)

    async def test_a_review_tag_also_qualifies(self):
        await self._register_review(cid="qc.check", tags=("review",))
        await self.publish(
            plan([{"id": "r", "title": "R", "kind": "review", "capability": "qc.check"}])
        )
        response = await self.execute()
        self.assertTrue(response.ok, response.error)

    async def test_an_ordinary_capability_cannot_be_bound_to_a_review(self):
        async def work(ctx: lc.NodeContext) -> lc.NodeResult:  # pragma: no cover
            return lc.NodeResult.ok()

        self.capability("data.transform", work)
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": plan(
                    [
                        {
                            "id": "r",
                            "title": "R",
                            "kind": "review",
                            "capability": "data.transform",
                        }
                    ]
                )
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("review", response.error)

    async def test_a_bound_review_cannot_be_self_reported(self):
        await self._register_review()
        await self.publish(
            plan([{"id": "r", "title": "R", "kind": "review", "capability": "review.lambda"}])
        )
        response = await self.broker.dispatch(
            "update_step", {"step_id": "r", "status": "succeeded"}
        )
        self.assertFalse(response.ok)
        self.assertIn("review", response.error)


class TestObjectiveLedger(PlanCase):
    def _plan_with_objectives(self, coverage, revision=1, **extra):
        return {
            "goal": "find the loci",
            "revision": revision,
            "objectives": [
                {"id": "q1", "question": "Which loci associate with yield?"},
            ],
            "analysis_coverage": coverage,
            "steps": [{"id": "a", "title": "Answer", "kind": "answer"}],
            **extra,
        }

    async def test_executed_coverage_needs_evidence(self):
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": self._plan_with_objectives(
                    [{"objective_id": "q1", "status": "executed", "reason": "done"}]
                )
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("evidence", response.error)

    async def test_evidence_makes_executed_coverage_valid(self):
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": self._plan_with_objectives(
                    [
                        {
                            "objective_id": "q1",
                            "status": "executed",
                            "reason": "scan complete",
                            "step_ids": ["a"],
                        }
                    ]
                )
            },
        )
        self.assertTrue(response.ok, response.error)

    async def test_an_unresolved_objective_must_state_a_next_action(self):
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": self._plan_with_objectives(
                    [{"objective_id": "q1", "status": "blocked", "reason": "no data"}]
                )
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("next_action", response.error)

    async def test_coverage_cannot_cite_an_unknown_step(self):
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": self._plan_with_objectives(
                    [
                        {
                            "objective_id": "q1",
                            "status": "executed",
                            "reason": "done",
                            "step_ids": ["nonexistent"],
                        }
                    ]
                )
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("unknown steps", response.error)

    async def test_every_objective_needs_coverage(self):
        response = await self.broker.dispatch(
            "publish_plan", {"plan": self._plan_with_objectives([])}
        )
        self.assertFalse(response.ok)
        self.assertIn("cover", response.error)

    async def test_a_revision_cannot_drop_an_objective(self):
        await self.publish(
            self._plan_with_objectives(
                [{"objective_id": "q1", "status": "planned", "reason": "queued"}]
            )
        )
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": {
                    "goal": "find the loci",
                    "revision": 2,
                    "reason": "narrowing scope",
                    "steps": [{"id": "a", "title": "Answer", "kind": "answer"}],
                }
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("q1", response.error)

    async def test_a_revision_may_mark_an_objective_not_estimable(self):
        await self.publish(
            self._plan_with_objectives(
                [{"objective_id": "q1", "status": "planned", "reason": "queued"}]
            )
        )
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": self._plan_with_objectives(
                    [
                        {
                            "objective_id": "q1",
                            "status": "not_estimable",
                            "reason": "the design has no replication",
                            "next_action": "collect replicated plots",
                        }
                    ],
                    revision=2,
                    reason="learned the design is unreplicated",
                )
            },
        )
        self.assertTrue(response.ok, response.error)
        parsed = lc.parse_plan(self.session.current_plan())
        self.assertEqual(
            [item.objective_id for item in parsed.unresolved_objectives], ["q1"]
        )

    async def test_a_profile_requires_objectives(self):
        response = await self.broker.dispatch(
            "publish_plan",
            {
                "plan": {
                    "goal": "g",
                    "revision": 1,
                    "analysis_profile": "association-scan",
                    "steps": [{"id": "a", "title": "A", "kind": "answer"}],
                }
            },
        )
        self.assertFalse(response.ok)
        self.assertIn("objective", response.error)


class TestGraphConstruction(PlanCase):
    async def test_a_plan_graph_mirrors_the_plan_dag(self):
        async def ok(ctx: lc.NodeContext) -> lc.NodeResult:  # pragma: no cover
            return lc.NodeResult.ok()

        self.capability("c.ok", ok)
        parsed = lc.parse_plan(
            plan(
                [
                    {"id": "a", "title": "A", "kind": "capability", "capability": "c.ok"},
                    {"id": "b", "title": "B", "kind": "capability", "capability": "c.ok", "depends_on": ["a"]},
                ]
            )
        )
        graph = build_plan_graph(parsed, self.registry, self.engine)
        self.assertEqual(graph.kind, "plan")
        self.assertEqual([node.id for node in graph.nodes], ["a", "b"])
        self.assertEqual(graph.node("b").depends_on, ("a",))
        self.assertEqual(graph.layers, [["a"], ["b"]])


class TestAppServerBridge(PlanCase):
    async def test_initialize_advertises_the_protocol(self):
        bridge = lc.AppServerBridge(self.broker)
        reply = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(reply["result"]["protocolVersion"], "loomcraft-v1")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "loomcraft")

    async def test_tools_list_matches_the_canonical_surface(self):
        bridge = lc.AppServerBridge(self.broker)
        reply = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {item["name"] for item in reply["result"]["tools"]}
        self.assertIn("publish_plan", names)
        self.assertIn("execute_plan", names)
        self.assertEqual(names, {spec.name for spec in lc.tool_specs()})

    async def test_a_codex_tool_call_goes_through_the_broker(self):
        bridge = lc.AppServerBridge(self.broker)
        reply = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "item/tool/call",
                "params": {"name": "session_context", "arguments": "{}"},
            }
        )
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(
            reply["result"]["structuredContent"]["result"]["session_id"],
            self.session.id,
        )

    async def test_a_rejected_call_is_reported_as_an_error_result(self):
        bridge = lc.AppServerBridge(self.broker)
        reply = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "run_capability", "arguments": {}},
            }
        )
        self.assertTrue(reply["result"]["isError"])

    async def test_unknown_methods_and_notifications(self):
        bridge = lc.AppServerBridge(self.broker)
        unknown = await bridge.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})
        self.assertEqual(unknown["error"]["code"], -32601)
        note = await bridge.handle({"jsonrpc": "2.0", "method": "notify"})
        self.assertEqual(note, {}, "a notification takes no reply")

    async def test_malformed_arguments_are_refused_not_crashed(self):
        bridge = lc.AppServerBridge(self.broker)
        reply = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "session_context", "arguments": "not json"},
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
