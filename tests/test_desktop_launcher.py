import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from scripts.start_desktop import DesktopError, LOCAL_URL, main, prepare_desktop, spawn_gateway


class FakeClient:
    def __init__(self, *, first_absent=False, foreign=False, bad_nonce=False):
        self.first_absent, self.foreign, self.bad_nonce = first_absent, foreign, bad_nonce
        self.calls = []

    def __enter__(self): return self
    def __exit__(self, *args): pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/api/health"):
            if self.first_absent:
                self.first_absent = False
                raise httpx.ConnectError("not listening")
            return httpx.Response(200, json={"service": "other-app" if self.foreign else "cutflow-local-gateway", "project_id": "cutflow-studio"})
        if url.endswith("/local-session"):
            return httpx.Response(200, json={"token": "gateway-only-nonce", "local_gateway": not self.bad_nonce})
        return httpx.Response(200, json={"execution_target": "remote_gpu"})


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.token = "gpu-token-that-is-never-printed-123456"
        (self.root / "gpu-access-token.txt").write_text(self.token, encoding="utf-8")
        self.env = {"CUTFLOW_DATA": str(self.root)}

    def tearDown(self): self.tmp.cleanup()

    def test_owned_gateway_is_reused_and_authenticated_with_its_nonce(self):
        client = FakeClient()
        with patch("scripts.start_desktop.httpx.Client", return_value=client), \
             patch("scripts.start_desktop.ensure_gpu", return_value=True), \
             patch("scripts.start_desktop.spawn_gateway") as spawn:
            result = prepare_desktop(self.env)
        self.assertEqual({"url": LOCAL_URL + "/", "reused": True}, result)
        spawn.assert_not_called()
        self.assertNotIn(self.token, str(client.calls))
        self.assertEqual("Bearer gateway-only-nonce", client.calls[-1][1]["headers"]["Authorization"])

    def test_foreign_port_is_not_reused_or_modified(self):
        client = FakeClient(foreign=True)
        with patch("scripts.start_desktop.httpx.Client", return_value=client), \
             patch("scripts.start_desktop.ensure_gpu") as gpu, \
             patch("scripts.start_desktop.spawn_gateway") as spawn:
            with self.assertRaises(DesktopError): prepare_desktop(self.env)
        gpu.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(1, len(client.calls))
        self.assertNotIn("headers", client.calls[0][1])

    def test_new_gateway_secret_is_only_in_process_environment(self):
        with patch("scripts.start_desktop.subprocess.Popen", return_value=object()) as popen:
            spawn_gateway({**self.env, "CUTFLOW_TOKEN": "irrelevant-local-token"}, self.token)
        arguments, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertNotIn(self.token, " ".join(arguments))
        self.assertEqual(self.token, options["env"]["CUTFLOW_GPU_TOKEN"])
        self.assertNotIn("CUTFLOW_TOKEN", options["env"])
        self.assertIn("studio.local_gateway", arguments)

    def test_absent_gateway_starts_then_verifies_and_no_browser_option_is_quiet(self):
        client = FakeClient(first_absent=True)
        output = io.StringIO()
        with patch("scripts.start_desktop.environment", return_value=self.env), \
             patch("scripts.start_desktop.httpx.Client", return_value=client), \
             patch("scripts.start_desktop.ensure_gpu", return_value=True), \
             patch("scripts.start_desktop.spawn_gateway", return_value=SimpleNamespace(poll=lambda: None)) as spawn, \
             patch("scripts.start_desktop.webbrowser.open") as opened, redirect_stdout(output):
            self.assertEqual(0, main(["--no-browser"]))
        spawn.assert_called_once()
        opened.assert_not_called()
        self.assertIn(LOCAL_URL, output.getvalue())
        self.assertNotIn(self.token, output.getvalue())
        self.assertNotIn("gateway-only-nonce", output.getvalue())

    def test_browser_opens_bare_url_without_any_token(self):
        with patch("scripts.start_desktop.environment", return_value=self.env), \
             patch("scripts.start_desktop.prepare_desktop", return_value={"url": LOCAL_URL + "/", "reused": True}), \
             patch("scripts.start_desktop.webbrowser.open") as opened, redirect_stdout(io.StringIO()):
            self.assertEqual(0, main([]))
        opened.assert_called_once_with(LOCAL_URL + "/")


if __name__ == "__main__": unittest.main()
