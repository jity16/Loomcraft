import copy
import unittest

from loomcraft import (
    Registry,
    allocate_input_uploads,
    topological_layers,
    topological_order,
    update_step,
    validate_input_fulfillment,
    validate_input_request,
    validate_plan,
)
from loomcraft.models import InputRequestValidationError, PlanValidationError


class ModelsTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.registry.register_capability(id="demo.cap", name="Demo capability")

    def plan(self, revision=1, reason=None):
        return {
            "goal": "demo",
            "revision": revision,
            "reason": reason,
            "steps": [
                {"id": "a", "title": "A", "kind": "capability", "capability": "demo.cap"},
                {"id": "b", "title": "B", "kind": "dynamic", "depends_on": ["a"]},
            ],
        }

    def test_publish_normalizes_state_and_checks_registry(self):
        value = validate_plan(self.plan(), registry=self.registry)
        self.assertEqual([step["status"] for step in value["steps"]], ["pending", "pending"])
        bad = self.plan()
        bad["steps"][0]["capability"] = "missing"
        with self.assertRaises(PlanValidationError):
            validate_plan(bad, registry=self.registry)

    def test_graph_and_replan_guards(self):
        value = validate_plan(self.plan(), registry=self.registry)
        self.assertEqual(topological_order(value), ["a", "b"])
        self.assertEqual(topological_layers(value), [["a"], ["b"]])
        with self.assertRaises(PlanValidationError):
            validate_plan(self.plan(), current=value, registry=self.registry)
        with self.assertRaises(PlanValidationError):
            validate_plan(self.plan(2), current=value, registry=self.registry)
        revised = validate_plan(self.plan(2, "add a follow-up"), current=value, registry=self.registry)
        self.assertEqual(revised["revision"], 2)

    def test_cycles_and_unknown_fields_are_rejected(self):
        raw = self.plan()
        raw["steps"][0]["depends_on"] = ["b"]
        with self.assertRaises(PlanValidationError):
            validate_plan(raw, registry=self.registry)
        raw = self.plan()
        raw["unexpected"] = True
        with self.assertRaises(PlanValidationError):
            validate_plan(raw, registry=self.registry)

    def test_transition_guard(self):
        value = validate_plan(self.plan(), registry=self.registry)
        running = update_step(value, "a", "running")
        done = update_step(running, "a", "succeeded", summary="ok")
        self.assertEqual(done["steps"][0]["summary"], "ok")
        with self.assertRaises(PlanValidationError):
            update_step(done, "a", "failed")

    def test_plan_json_round_trip(self):
        value = validate_plan(self.plan(), registry=self.registry)
        from loomcraft import Plan
        parsed = Plan.from_json(Plan.from_raw(value).to_json())
        self.assertEqual(parsed.goal, "demo")

    def test_revision_diff_is_stable(self):
        from loomcraft import diff_plans
        first = validate_plan(self.plan(), registry=self.registry)
        second = validate_plan({"goal": "demo", "revision": 2, "reason": "add review", "steps": [
            {"id": "a", "title": "A changed", "kind": "capability", "capability": "demo.cap"},
            {"id": "b", "title": "B", "kind": "dynamic", "depends_on": ["a"]},
            {"id": "c", "title": "C", "kind": "answer", "depends_on": ["b"]},
        ]}, current=first, registry=self.registry)
        diff = diff_plans(first, second)
        self.assertEqual(diff["added_steps"], ["c"])
        self.assertEqual(diff["changed_steps"], ["a"])

    def test_input_allocation_is_distinct_and_extension_aware(self):
        request = validate_input_request({
            "title": "Files",
            "message": "Need two files",
            "requirements": [
                {"key": "table", "label": "Table", "description": "CSV", "required": True, "min_files": 1, "max_files": 2, "allowed_extensions": [".CSV"]},
                {"key": "index", "label": "Index", "description": "Index", "required": True, "min_files": 1, "max_files": 1, "allowed_extensions": [".idx"]},
            ],
            "continue_prompt": "continue",
        })
        uploads = [
            {"id": "a", "filename": "data.csv", "checksum": "same"},
            {"id": "duplicate", "filename": "copy.csv", "checksum": "same"},
            {"id": "b", "filename": "data.idx", "checksum": "different"},
        ]
        with self.assertRaises(InputRequestValidationError):
            validate_input_fulfillment(request, uploads[:2])
        allocation = allocate_input_uploads(request, uploads)
        self.assertEqual(allocation["table"], ["a"])
        self.assertEqual(allocation["index"], ["b"])


if __name__ == "__main__":
    unittest.main()
