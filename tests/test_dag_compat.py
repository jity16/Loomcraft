import unittest

from loomcraft import DAGValidationError, plan_from_dag, validate_dag


class DagCompatibilityTest(unittest.TestCase):
    def test_node_edge_definition_validates_and_converts(self):
        dag = {
            "id": "demo-dag",
            "name": "Demo",
            "version": "1.0",
            "nodes": [
                {"id": "input", "name": "Input", "type": "input.upload"},
                {"id": "profile", "name": "Profile", "type": "data.profile"},
            ],
            "edges": [{"from": "input", "to": "profile"}],
        }
        validated = validate_dag(dag)
        self.assertEqual(validated["topological_order"], ["input", "profile"])
        plan = plan_from_dag(dag)
        self.assertEqual(plan["steps"][1]["depends_on"], ["input"])

    def test_cycle_is_rejected(self):
        dag = {
            "id": "cycle",
            "name": "Cycle",
            "version": "1",
            "nodes": [
                {"id": "a", "name": "A", "type": "data.profile"},
                {"id": "b", "name": "B", "type": "data.profile"},
            ],
            "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        }
        with self.assertRaises(DAGValidationError):
            validate_dag(dag)

    def test_legacy_retry_zero_and_ports_are_normalized(self):
        dag = {
            "id": "legacy",
            "name": "Legacy",
            "version": "1",
            "nodes": [{"id": "n", "name": "N", "type": "tool.external", "inputs": [{"name": "in", "artifact_type": "file"}], "retry": {"max_attempts": 0}, "config": {"capability": "demo.cap"}}],
            "edges": [],
        }
        plan = plan_from_dag(dag)
        self.assertEqual(plan["steps"][0]["retry"]["max_attempts"], 1)


if __name__ == "__main__":
    unittest.main()
