"""Plan validation, revision discipline, and the step state machine."""

from __future__ import annotations

import unittest

import loomcraft as lc
from loomcraft.errors import (
    DependencyError,
    PlanValidationError,
    StepTransitionError,
    UnknownStepError,
)


def plan(steps, revision=1, reason=None, goal="goal"):
    payload = {"goal": goal, "revision": revision, "steps": steps}
    if reason is not None:
        payload["reason"] = reason
    return payload


DIAMOND = [
    {"id": "load", "title": "Load", "kind": "dynamic"},
    {"id": "left", "title": "Left", "kind": "dynamic", "depends_on": ["load"]},
    {"id": "right", "title": "Right", "kind": "dynamic", "depends_on": ["load"]},
    {"id": "join", "title": "Join", "kind": "answer", "depends_on": ["left", "right"]},
]


class TestPlanStructure(unittest.TestCase):
    def test_accepts_a_valid_dag(self):
        validated = lc.validate_plan(plan(DIAMOND))
        self.assertEqual(validated["revision"], 1)
        self.assertEqual(len(validated["steps"]), 4)

    def test_publication_resets_execution_state(self):
        steps = [
            {
                "id": "a",
                "title": "A",
                "kind": "dynamic",
                "status": "succeeded",
                "summary": "already done, honest",
            }
        ]
        validated = lc.validate_plan(plan(steps))
        self.assertEqual(validated["steps"][0]["status"], "pending")
        self.assertIsNone(validated["steps"][0]["summary"])

    def test_rejects_duplicate_ids(self):
        steps = [
            {"id": "a", "title": "A", "kind": "dynamic"},
            {"id": "a", "title": "A again", "kind": "dynamic"},
        ]
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(steps))
        self.assertIn("unique", str(ctx.exception))

    def test_rejects_unknown_dependency(self):
        steps = [{"id": "a", "title": "A", "kind": "dynamic", "depends_on": ["ghost"]}]
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(steps))
        self.assertIn("unknown", ctx.exception.public_message)

    def test_rejects_self_dependency(self):
        steps = [{"id": "a", "title": "A", "kind": "dynamic", "depends_on": ["a"]}]
        with self.assertRaises(PlanValidationError):
            lc.validate_plan(plan(steps))

    def test_rejects_cycles(self):
        steps = [
            {"id": "a", "title": "A", "kind": "dynamic", "depends_on": ["c"]},
            {"id": "b", "title": "B", "kind": "dynamic", "depends_on": ["a"]},
            {"id": "c", "title": "C", "kind": "dynamic", "depends_on": ["b"]},
        ]
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(steps))
        self.assertIn("cycle", ctx.exception.public_message)

    def test_rejects_too_many_steps(self):
        steps = [
            {"id": f"s{i}", "title": f"S{i}", "kind": "dynamic"}
            for i in range(lc.plan.MAX_STEPS + 1)
        ]
        with self.assertRaises(PlanValidationError):
            lc.validate_plan(plan(steps))

    def test_capability_step_requires_a_capability_id(self):
        steps = [{"id": "a", "title": "A", "kind": "capability"}]
        with self.assertRaises(PlanValidationError):
            lc.validate_plan(plan(steps))

    def test_non_capability_step_may_not_declare_one(self):
        steps = [{"id": "a", "title": "A", "kind": "dynamic", "capability": "x.y"}]
        with self.assertRaises(PlanValidationError):
            lc.validate_plan(plan(steps))

    def test_registry_rejects_unknown_capability(self):
        registry = lc.Registry()
        steps = [
            {"id": "a", "title": "A", "kind": "capability", "capability": "nope.missing"}
        ]
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(steps), registry=registry)
        self.assertIn("unknown capability", ctx.exception.public_message)

    def test_error_message_does_not_echo_rejected_values(self):
        secret = "s3cret-token-value-do-not-echo"
        steps = [{"id": "a", "title": secret * 20, "kind": "dynamic"}]
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(steps))
        self.assertNotIn(secret, ctx.exception.public_message)


class TestRevisionDiscipline(unittest.TestCase):
    def test_revision_must_increase(self):
        first = lc.validate_plan(plan(DIAMOND, revision=2))
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(DIAMOND, revision=2, reason="again"), first)
        self.assertIn("must increase", str(ctx.exception))

    def test_replan_requires_a_reason(self):
        first = lc.validate_plan(plan(DIAMOND))
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(DIAMOND, revision=2), first)
        self.assertIn("reason", str(ctx.exception))

    def test_replan_with_reason_is_accepted(self):
        first = lc.validate_plan(plan(DIAMOND))
        second = lc.validate_plan(
            plan(DIAMOND, revision=2, reason="left branch needs a different source"),
            first,
        )
        self.assertEqual(second["revision"], 2)

    def test_cannot_replan_while_a_step_is_running(self):
        first = lc.validate_plan(plan(DIAMOND))
        running = lc.update_step(first, "load", "running")
        with self.assertRaises(PlanValidationError) as ctx:
            lc.validate_plan(plan(DIAMOND, revision=2, reason="mid-flight"), running)
        self.assertIn("running", str(ctx.exception))


class TestStepTransitions(unittest.TestCase):
    def setUp(self):
        self.plan = lc.validate_plan(plan(DIAMOND))

    def test_pending_to_running_to_succeeded(self):
        state = lc.update_step(self.plan, "load", "running")
        state = lc.update_step(state, "load", "succeeded", summary="loaded")
        self.assertEqual(lc.get_step(state, "load")["status"], "succeeded")
        self.assertEqual(lc.get_step(state, "load")["summary"], "loaded")

    def test_succeeded_is_terminal(self):
        state = lc.update_step(self.plan, "load", "succeeded")
        with self.assertRaises(StepTransitionError):
            lc.update_step(state, "load", "failed")

    def test_failed_may_retry(self):
        state = lc.update_step(self.plan, "load", "failed")
        state = lc.update_step(state, "load", "running")
        self.assertEqual(lc.get_step(state, "load")["status"], "running")

    def test_skipped_may_be_reactivated(self):
        state = lc.update_step(self.plan, "load", "skipped")
        state = lc.update_step(state, "load", "running")
        self.assertEqual(lc.get_step(state, "load")["status"], "running")

    def test_unknown_step_rejected(self):
        with self.assertRaises(UnknownStepError):
            lc.update_step(self.plan, "ghost", "running")

    def test_unsupported_status_rejected(self):
        with self.assertRaises(StepTransitionError):
            lc.update_step(self.plan, "load", "finished-ish")


class TestDependencyGating(unittest.TestCase):
    def setUp(self):
        self.plan = lc.validate_plan(plan(DIAMOND))

    def test_blocks_a_step_with_unmet_dependencies(self):
        with self.assertRaises(DependencyError):
            lc.plan.ensure_dependencies_succeeded(self.plan, "join")

    def test_allows_a_step_once_dependencies_succeed(self):
        state = lc.update_step(self.plan, "load", "succeeded")
        state = lc.update_step(state, "left", "succeeded")
        state = lc.update_step(state, "right", "succeeded")
        lc.plan.ensure_dependencies_succeeded(state, "join")

    def test_ready_steps_tracks_the_frontier(self):
        parsed = lc.parse_plan(self.plan)
        self.assertEqual([step.id for step in parsed.ready_steps()], ["load"])
        state = lc.update_step(self.plan, "load", "succeeded")
        parsed = lc.parse_plan(state)
        self.assertEqual(
            sorted(step.id for step in parsed.ready_steps()), ["left", "right"]
        )

    def test_startable_requires_matching_kind_and_capability(self):
        steps = [
            {"id": "run", "title": "Run", "kind": "capability", "capability": "a.b"}
        ]
        state = lc.validate_plan(plan(steps))
        with self.assertRaises(PlanValidationError):
            lc.plan.ensure_step_startable(state, "run", kind="workflow", capability="a.b")
        with self.assertRaises(PlanValidationError):
            lc.plan.ensure_step_startable(
                state, "run", kind="capability", capability="other.thing"
            )
        lc.plan.ensure_step_startable(state, "run", kind="capability", capability="a.b")

    def test_startable_rejects_a_step_that_already_ran(self):
        steps = [
            {"id": "run", "title": "Run", "kind": "capability", "capability": "a.b"}
        ]
        state = lc.update_step(lc.validate_plan(plan(steps)), "run", "succeeded")
        with self.assertRaises(StepTransitionError):
            lc.plan.ensure_step_startable(
                state, "run", kind="capability", capability="a.b"
            )


class TestSkipPropagation(unittest.TestCase):
    def test_continue_policy_keeps_downstream_startable(self):
        steps = [
            {
                "id": "load",
                "title": "Load",
                "kind": "dynamic",
                "on_failure": "continue",
            },
            {
                "id": "report",
                "title": "Report",
                "kind": "answer",
                "depends_on": ["load"],
            },
        ]
        state = lc.update_step(lc.validate_plan(plan(steps)), "load", "failed")
        state = lc.propagate_skips(state)
        self.assertEqual(lc.get_step(state, "report")["status"], "pending")
        lc.plan.ensure_dependencies_succeeded(state, "report")
        self.assertEqual([item.id for item in lc.parse_plan(state).ready_steps()], ["report"])

    def test_failure_closes_out_the_whole_downstream_subtree(self):
        state = lc.validate_plan(plan(DIAMOND))
        state = lc.update_step(state, "load", "failed")
        state = lc.propagate_skips(state)
        statuses = {step["id"]: step["status"] for step in state["steps"]}
        self.assertEqual(statuses["load"], "failed")
        self.assertEqual(statuses["left"], "skipped")
        self.assertEqual(statuses["right"], "skipped")
        self.assertEqual(statuses["join"], "skipped")

    def test_a_healthy_branch_survives_a_sibling_failure(self):
        state = lc.validate_plan(plan(DIAMOND))
        state = lc.update_step(state, "load", "succeeded")
        state = lc.update_step(state, "left", "failed")
        state = lc.propagate_skips(state)
        statuses = {step["id"]: step["status"] for step in state["steps"]}
        self.assertEqual(statuses["right"], "pending")
        self.assertEqual(statuses["join"], "skipped")


class TestPlanViews(unittest.TestCase):
    def test_layers_expose_parallelism(self):
        parsed = lc.parse_plan(lc.validate_plan(plan(DIAMOND)))
        self.assertEqual(parsed.layers, [["load"], ["left", "right"], ["join"]])

    def test_progress_and_completion(self):
        state = lc.validate_plan(plan(DIAMOND))
        for step_id in ("load", "left", "right", "join"):
            state = lc.update_step(state, step_id, "succeeded")
        parsed = lc.parse_plan(state)
        self.assertTrue(parsed.is_complete)
        self.assertEqual(parsed.progress["succeeded"], 4)


if __name__ == "__main__":
    unittest.main()
