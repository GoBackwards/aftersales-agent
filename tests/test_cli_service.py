"""CLI checks in a fresh process and in this process; credentials and state stay in temporary directories."""

from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from support_agent import cli
from support_agent.models import LazyModel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
NO_LIVE_CONFIGURATION = """
import sys
from unittest.mock import patch
from support_agent.cli import main
with patch('support_agent.cli.load_dotenv', side_effect=AssertionError('dotenv must stay lazy')), \\
     patch('support_agent.cli.APIConfig.from_env', side_effect=AssertionError('config must stay lazy')), \\
     patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network allowed')):
    raise SystemExit(main(sys.argv[1:]))
"""


class AppCLITests(unittest.TestCase):
    def test_app_rejects_invalid_runtime_limits_before_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            process = subprocess.run([sys.executable, "-X", "utf8", "-c", NO_LIVE_CONFIGURATION, "app",
                                      "--data-dir", str(data), "--port", "0", "--max-model-attempts", "9",
                                      "--knowledge-store", str(data / "missing-store")],
                                     cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertFalse((data / "app.sqlite3").exists())
            if GRAPH_AVAILABLE:
                # Without the graph runtime the command already stops at its demo-model import.
                self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                self.assertIn("启动失败", process.stderr)
                self.assertNotIn("Traceback", process.stderr)

    @unittest.skipUnless(GRAPH_AVAILABLE, "app imports the offline graph demo model")
    def test_app_main_in_process_rejects_each_invalid_limit_before_database(self):
        # Same check as above inside this process, so the argument parser and limit validation are traced too.
        for option, value, field in (("--max-model-attempts", "9", "max_model_attempts"),
                                     ("--max-tool-calls", "0", "max_tool_calls"),
                                     ("--run-timeout", "0", "run_timeout"),
                                     ("--model-timeout", "46", "model_timeout")):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                data = Path(temporary)
                errors = io.StringIO()
                with patch("support_agent.cli.load_dotenv", side_effect=AssertionError("dotenv must stay lazy")), \
                        patch("support_agent.cli.APIConfig.from_env", side_effect=AssertionError("config must stay lazy")), \
                        redirect_stderr(errors):
                    status = cli.main(["app", "--data-dir", str(data), "--port", "0", option, value,
                                       "--knowledge-store", str(data / "missing-store")])
                self.assertEqual(status, 2)
                self.assertIn("启动失败", errors.getvalue())
                self.assertIn(field, errors.getvalue())
                self.assertFalse((data / "app.sqlite3").exists())
                self.assertFalse((data / "requests.jsonl").exists())

    def test_live_session_model_reads_no_configuration_until_first_request(self):
        with patch("support_agent.cli.load_dotenv", side_effect=AssertionError("dotenv must stay lazy")), \
                patch("support_agent.cli.APIConfig.from_env", side_effect=AssertionError("config must stay lazy")):
            model = LazyModel(cli._live_model)
            self.assertEqual(model.mode, "live_agent")
            self.assertIsNone(model.model)
            # The first request is what reads the configuration, through the names these guards patch.
            with self.assertRaisesRegex(AssertionError, "dotenv must stay lazy"):
                model.complete([], [])
        self.assertIsNone(model.model)
        self.assertEqual(model.mode, "live_agent")
        with patch("support_agent.cli.load_dotenv") as dotenv, \
                patch("support_agent.cli.APIConfig.from_env", side_effect=AssertionError("config must stay lazy")):
            with self.assertRaisesRegex(AssertionError, "config must stay lazy"):
                LazyModel(cli._live_model).complete_with_timeout([], [], timeout=1)
        dotenv.assert_called_once_with(cli.PROJECT_ROOT / ".env")


if __name__ == "__main__":
    unittest.main()
