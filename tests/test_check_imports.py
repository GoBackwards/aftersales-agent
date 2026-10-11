"""Module import sweep on a synthetic tree, run in a child process so no test module is imported twice."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts import check_imports

SCRIPT = Path(check_imports.__file__).resolve()


class CheckImportsTests(unittest.TestCase):
    def tree(self, root, *, broken):
        for package in ("support_agent", "scripts", "tests"):
            (root / package).mkdir()
        (root / "support_agent" / "__init__.py").write_text("", encoding="utf-8")
        (root / "support_agent" / "__main__.py").write_text("raise SystemExit('must not run')\n", encoding="utf-8")
        (root / "support_agent" / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
        (root / "scripts" / "tool.py").write_text(
            "from support_agent.good import VALUE\nif __name__ == '__main__':\n    raise SystemExit(1)\n", encoding="utf-8")
        (root / "tests" / "test_sibling.py").write_text("SIBLING = True\n", encoding="utf-8")
        (root / "tests" / "test_uses_sibling.py").write_text("from test_sibling import SIBLING\n", encoding="utf-8")
        if broken:
            (root / "scripts" / "broken.py").write_text("import module_that_does_not_exist_s0\n", encoding="utf-8")

    def run_tool(self, root):
        return subprocess.run([sys.executable, "-X", "utf8", str(SCRIPT), "--root", str(root)], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def test_names_skip_main_and_map_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.tree(root, broken=False)
            self.assertEqual(check_imports.module_names(root), [
                "support_agent", "support_agent.good", "scripts.tool", "tests.test_sibling", "tests.test_uses_sibling"])

    def test_clean_tree_prints_nothing_and_failures_are_listed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "clean"
            root.mkdir()
            self.tree(root, broken=False)
            result = self.run_tool(root)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            root = Path(temporary) / "broken"
            root.mkdir()
            self.tree(root, broken=True)
            result = self.run_tool(root)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.splitlines(), [
                "scripts.broken: ModuleNotFoundError: No module named 'module_that_does_not_exist_s0'"])


if __name__ == "__main__":
    unittest.main()
