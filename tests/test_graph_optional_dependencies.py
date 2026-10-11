"""Product imports and offline startup remain independent of unused framework dependencies.
"""
import importlib.util
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[1]
WITHOUT_LANGGRAPH = """
import importlib.abc
import sys

sys.path.insert(0, sys.argv[1])

class MissingLangGraph(importlib.abc.MetaPathFinder):
    # langgraph and langsmith are no longer dependencies; neither may be imported.
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if top in {"langgraph", "langsmith"}:
            raise ModuleNotFoundError(f"No module named '{top}'", name=top)

sys.meta_path.insert(0, MissingLangGraph())
"""


class GraphOptionalDependencyTests(unittest.TestCase):
    def without_langgraph(self, source):
        # A fresh isolated interpreter prevents imports cached by other tests
        # from hiding the missing-package path in Graph-enabled CI as well.
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c",
             WITHOUT_LANGGRAPH + textwrap.dedent(source), str(PROJECT)],
            cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_graph_execution_needs_neither_langgraph_nor_pydantic(self):
        # S2 removed langgraph and langsmith; B7 removed the planner's pydantic model (plan_turn is checked by
        # understand.parse), so a chat turn loads without pydantic too. Only the knowledge base's AI drafting
        # (kb_ai) still imports it.
        if importlib.util.find_spec("pydantic") is not None:
            self.without_langgraph("""
                from pathlib import Path
                import tempfile
                from support_agent.service import create_server

                with tempfile.TemporaryDirectory() as temporary:
                    server = create_server(db_path=Path(temporary) / "graph.sqlite3",
                                           credentials={"test-owner": "alice"}, port=0)
                    try:
                        server.model_factory()
                    finally:
                        server.server_close()
                import support_agent.turn  # noqa: F401  the chat turn itself
                assert "langgraph" not in sys.modules and "langsmith" not in sys.modules
            """)
        self.without_langgraph("""
            class MissingPydantic(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "pydantic" or fullname.startswith("pydantic."):
                        raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")

            sys.meta_path.insert(0, MissingPydantic())
            from pathlib import Path
            import tempfile
            from support_agent.service import create_server

            with tempfile.TemporaryDirectory() as temporary:
                database = Path(temporary) / "graph.sqlite3"
                server = create_server(db_path=database, credentials={"test-owner": "alice"}, port=0)
                try:
                    # Without a knowledge release the graph runtime loads with the first model.
                    server.model_factory()
                finally:
                    server.server_close()
            import support_agent.turn  # noqa: F401  the chat turn itself
            assert "pydantic" not in sys.modules
        """)

    def test_product_routes_and_card_store_do_not_require_graph_or_pydantic(self):
        self.without_langgraph("""
            class MissingPydantic(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "pydantic" or fullname.startswith("pydantic."):
                        raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")

            sys.meta_path.insert(0, MissingPydantic())
            from pathlib import Path
            import tempfile
            from support_agent import cli, kb_sources, kb_store, product_api
            from support_agent.service import create_server

            assert "support_agent.turn" not in sys.modules
            assert "support_agent.kb_ai" not in sys.modules
            document = kb_sources.build_uploaded_document("title", "body text")
            assert kb_sources.supplementary_evidence([document])
            with tempfile.TemporaryDirectory() as temporary:
                server = create_server(db_path=Path(temporary) / "demo.sqlite3", credentials={"test-owner": "alice"},
                                       port=0, demo_login=True)
                try:
                    assert product_api.demo_identities(server)[1]["identities"][0]["identity"] == "alice"
                finally:
                    server.server_close()
            assert "pydantic" not in sys.modules and "langgraph" not in sys.modules
        """)


if __name__ == "__main__":
    unittest.main()
