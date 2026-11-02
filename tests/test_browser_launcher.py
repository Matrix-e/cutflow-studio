import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.start_browser_collection import API_URL, browser_launch, gpu_ready, main


class BrowserLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.token = "test-gpu-token-which-must-not-be-in-cli"
        (self.root / "gpu-access-token.txt").write_text(self.token)
        self.env = {"CUTFLOW_DATA": str(self.root), "CUTFLOW_INBOX": str(self.root.parent / "separate-inbox")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_secret_only_in_environment_and_profile_is_isolated(self):
        command, env, profile, output = browser_launch(self.env)
        self.assertNotIn(self.token, " ".join(command))
        self.assertNotIn("--token-file", command)
        self.assertEqual(self.token, env["CUTFLOW_BROWSER_TOKEN"])
        self.assertEqual(API_URL, env["CUTFLOW_BROWSER_API_URL"])
        self.assertEqual(self.root / "douyin-browser", profile)
        self.assertEqual(self.root / "collected" / "douyin", output)
        self.assertNotIn("--headless", command)

    def test_downloads_inside_auto_edit_inbox_are_rejected(self):
        with self.assertRaises(ValueError):
            browser_launch({**self.env, "CUTFLOW_INBOX": str(self.root)})

    def test_gpu_readiness_requires_remote_identity(self):
        class Client:
            def __init__(self, identity): self.identity = identity
            def get(self, *args, **kwargs):
                return SimpleNamespace(status_code=200, json=lambda: {"execution_target": self.identity})
        self.assertTrue(gpu_ready(Client("remote_gpu"), self.token))
        self.assertFalse(gpu_ready(Client("local"), self.token))

    def test_missing_dependency_does_not_launch_or_install(self):
        output = io.StringIO()
        with patch("scripts.start_browser_collection.importlib.util.find_spec", return_value=None), \
             patch("scripts.start_browser_collection.subprocess.run") as run, \
             patch("scripts.start_browser_collection.subprocess.Popen") as popen, redirect_stdout(output):
            self.assertEqual(1, main([]))
        run.assert_not_called()
        popen.assert_not_called()
        self.assertIn("pip install -r requirements-browser.txt", output.getvalue())

    def test_mocked_launcher_prints_no_token_and_never_controls_real_browser(self):
        output = io.StringIO()
        with patch("scripts.start_browser_collection.importlib.util.find_spec", return_value=object()), \
             patch("scripts.start_browser_collection.environment", return_value=self.env), \
             patch("scripts.start_browser_collection.ensure_gpu", return_value=True), \
             patch("scripts.start_browser_collection.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run, redirect_stdout(output):
            self.assertEqual(0, main(["--once"]))
        self.assertNotIn(self.token, output.getvalue())
        command = run.call_args.args[0]
        self.assertIn("--once", command)
        self.assertNotIn(self.token, " ".join(command))
        self.assertEqual(self.token, run.call_args.kwargs["env"]["CUTFLOW_BROWSER_TOKEN"])


if __name__ == "__main__":
    unittest.main()
