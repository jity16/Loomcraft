import ast
import unittest
from pathlib import Path


class IsolationTest(unittest.TestCase):
    def test_core_has_no_source_application_imports(self):
        root = Path(__file__).resolve().parents[1] / "core" / "loomcraft"
        forbidden = {"app", "backend", "frontend"}
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [item.name.split(".", 1)[0] for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".", 1)[0]]
                else:
                    continue
                self.assertTrue(forbidden.isdisjoint(names), "%s imports a source application" % path)


if __name__ == "__main__":
    unittest.main()
