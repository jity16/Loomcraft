"""Pure DAG algorithms: validation, ordering, layering, and analysis."""

from __future__ import annotations

import unittest

from loomcraft import graph


class TestValidation(unittest.TestCase):
    def test_empty_graph_is_a_dag(self):
        self.assertTrue(graph.is_dag({}))

    def test_linear_chain_is_a_dag(self):
        self.assertTrue(graph.is_dag({"a": [], "b": ["a"], "c": ["b"]}))

    def test_detects_two_node_cycle(self):
        cycle = graph.find_cycle({"a": ["b"], "b": ["a"]})
        self.assertEqual(set(cycle), {"a", "b"})

    def test_detects_long_cycle(self):
        adjacency = {"a": ["d"], "b": ["a"], "c": ["b"], "d": ["c"]}
        self.assertEqual(len(graph.find_cycle(adjacency)), 4)

    def test_detects_self_loop(self):
        issues = graph.validate({"a": ["a"]})
        self.assertTrue(any(issue.kind == "self_dependency" for issue in issues))

    def test_detects_duplicate_dependency(self):
        issues = graph.validate({"a": [], "b": ["a", "a"]})
        self.assertTrue(any(issue.kind == "duplicate_dependency" for issue in issues))

    def test_detects_unknown_dependency(self):
        issues = graph.validate({"a": ["ghost"]})
        self.assertTrue(any(issue.kind == "unknown_dependency" for issue in issues))

    def test_deep_chain_does_not_blow_the_stack(self):
        # Iterative DFS: 5k nodes would exceed the default recursion limit.
        adjacency = {"n0": []}
        for index in range(1, 5000):
            adjacency[f"n{index}"] = [f"n{index - 1}"]
        self.assertTrue(graph.is_dag(adjacency))
        self.assertEqual(len(graph.topological_order(adjacency)), 5000)


class TestOrdering(unittest.TestCase):
    def test_topological_order_respects_dependencies(self):
        adjacency = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
        order = graph.topological_order(adjacency)
        self.assertLess(order.index("a"), order.index("b"))
        self.assertLess(order.index("b"), order.index("d"))
        self.assertLess(order.index("c"), order.index("d"))

    def test_topological_order_is_deterministic(self):
        adjacency = {"z": [], "y": [], "x": [], "w": ["x", "y", "z"]}
        self.assertEqual(
            graph.topological_order(adjacency),
            graph.topological_order(adjacency),
        )

    def test_topological_order_rejects_cycles(self):
        with self.assertRaises(ValueError):
            graph.topological_order({"a": ["b"], "b": ["a"]})

    def test_layers_group_concurrent_work(self):
        adjacency = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "e": []}
        self.assertEqual(graph.layers(adjacency), [["a", "e"], ["b", "c"], ["d"]])

    def test_wide_fan_out_is_a_single_layer(self):
        adjacency = {"root": []}
        for index in range(20):
            adjacency[f"leaf{index}"] = ["root"]
        computed = graph.layers(adjacency)
        self.assertEqual(len(computed), 2)
        self.assertEqual(len(computed[1]), 20)


class TestAnalysis(unittest.TestCase):
    def setUp(self):
        self.adjacency = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"], "e": ["d"]}

    def test_roots_and_leaves(self):
        self.assertEqual(graph.roots(self.adjacency), ["a"])
        self.assertEqual(graph.leaves(self.adjacency), ["e"])

    def test_descendants(self):
        self.assertEqual(graph.descendants(self.adjacency, "b"), {"d", "e"})

    def test_ancestors(self):
        self.assertEqual(graph.ancestors(self.adjacency, "d"), {"a", "b", "c"})

    def test_critical_path_unweighted(self):
        path = graph.critical_path(self.adjacency)
        self.assertEqual(path[0], "a")
        self.assertEqual(path[-1], "e")
        self.assertEqual(len(path), 4)

    def test_critical_path_follows_weights(self):
        weights = {"a": 1, "b": 1, "c": 50, "d": 1, "e": 1}
        self.assertIn("c", graph.critical_path(self.adjacency, weights))

    def test_dot_export_contains_every_edge(self):
        dot = graph.to_dot(self.adjacency)
        self.assertIn('"a" -> "b"', dot)
        self.assertIn('"d" -> "e"', dot)

    def test_adjacency_from_dict_nodes(self):
        nodes = [{"id": "a", "depends_on": []}, {"id": "b", "depends_on": ["a"]}]
        self.assertEqual(graph.adjacency_from(nodes), {"a": [], "b": ["a"]})


if __name__ == "__main__":
    unittest.main()
