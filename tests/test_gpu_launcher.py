import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from scripts.start_gpu import acquire_lock, main
from scripts.start_browser_collection import _lock, ensure_gpu


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        ready = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(200 if ready else 503, json={"execution_target": "remote_gpu"},
                              request=httpx.Request("GET", url))


class Clock:
    def __init__(self): self.value = 0
    def now(self): return self.value
    def sleep(self, value): self.value += value


class GPUStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
    def tearDown(self): self.tmp.cleanup()

    def test_incumbent_lock_can_be_reused_without_reading_locked_byte(self):
        for acquire in (acquire_lock, _lock):
            path = self.root / (acquire.__name__ + ".lock")
            first = acquire(path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire(path))
            finally:
                first.close()
            reopened = acquire(path)
            self.assertIsNotNone(reopened)
            reopened.close()

    def test_existing_bridge_main_authenticates_and_never_starts_duplicate(self):
        for name in ("gpu_sync_ed25519", "gpu_known_hosts", "gpu-access-token.txt"):
            (self.root / name).write_text("fixture-token-or-path-only", encoding="utf-8")
        client = FakeClient([True])
        output = io.StringIO()
        with patch("scripts.start_gpu.environment", return_value={"CUTFLOW_DATA": str(self.root)}), \
             patch("scripts.start_gpu.shutil.which", return_value="ssh"), \
             patch("scripts.start_gpu.acquire_lock", return_value=None), \
             patch("httpx.Client", return_value=client), \
             patch("scripts.start_gpu.subprocess.Popen") as popen, redirect_stdout(output):
            self.assertEqual(0, main(["--no-browser"]))
        popen.assert_not_called()
        self.assertNotIn("fixture-token", output.getvalue())
        self.assertIn("已复用", output.getvalue())

    def test_exited_competing_launcher_does_not_hide_recovered_tunnel(self):
        client = FakeClient([False, False, True])
        process = SimpleNamespace(poll=Mock(return_value=1))
        clock = Clock()
        with patch("httpx.Client", return_value=client) as factory, \
             patch("scripts.start_browser_collection.subprocess.Popen", return_value=process), \
             patch("scripts.start_browser_collection.time.monotonic", side_effect=clock.now), \
             patch("scripts.start_browser_collection.time.sleep", side_effect=clock.sleep):
            self.assertTrue(ensure_gpu({"CUTFLOW_DATA": str(self.root)}, "not-printed-token"))
        process.poll.assert_called_once()
        self.assertEqual(8, factory.call_args.kwargs["timeout"])
        self.assertEqual(3, len(client.calls))

    def test_wait_budget_remains_bounded_when_no_tunnel_recovers(self):
        client = FakeClient([False])
        clock = Clock()
        with patch("httpx.Client", return_value=client), \
             patch("scripts.start_browser_collection.subprocess.Popen", return_value=SimpleNamespace(poll=lambda: 1)), \
             patch("scripts.start_browser_collection.time.monotonic", side_effect=clock.now), \
             patch("scripts.start_browser_collection.time.sleep", side_effect=clock.sleep):
            self.assertFalse(ensure_gpu({"CUTFLOW_DATA": str(self.root)}, "not-printed-token", wait_seconds=3))
        self.assertEqual(3, clock.value)
        self.assertTrue(all(call[1]["timeout"] <= 3 for call in client.calls))


if __name__ == "__main__": unittest.main()
