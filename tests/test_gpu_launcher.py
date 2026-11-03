import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from scripts.start_gpu import acquire_lock, authenticated_gpu, main, wait_for_gpu
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


class FakeProcess:
    def __init__(self, pid, returncode=None):
        self.pid, self.returncode = pid, returncode
        self.terminated = False
    def poll(self): return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = 0
    def kill(self): self.returncode = -1
    def wait(self, **kwargs): return self.returncode


class GPUStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
    def tearDown(self): self.tmp.cleanup()

    def configured_environment(self):
        for name in ('gpu_sync_ed25519', 'gpu_known_hosts', 'gpu-access-token.txt'):
            (self.root / name).write_text('fixture-token-or-path-only', encoding='utf-8')
        return {'CUTFLOW_DATA': str(self.root), 'CUTFLOW_INBOX': str(self.root / 'inbox')}

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

    def test_initial_tunnel_failure_then_recovery_starts_only_one_sync(self):
        env = self.configured_environment()
        clock, client, launched = Clock(), FakeClient([False, True]), []
        first, second, sync = FakeProcess(101, 1), FakeProcess(102), FakeProcess(103)
        processes = iter((first, second, sync))
        def launch(command, **kwargs):
            launched.append(command)
            return next(processes)
        def sleep(seconds):
            clock.sleep(seconds)
            if clock.value >= 17:
                (self.root / 'gpu-client.stop').touch()
        output = io.StringIO()
        with patch('scripts.start_gpu.environment', return_value=env), \
             patch('scripts.start_gpu.shutil.which', return_value='ssh'), \
             patch('httpx.Client', return_value=client), \
             patch('scripts.start_gpu.subprocess.Popen', side_effect=launch), \
             patch('scripts.start_gpu.time.monotonic', side_effect=clock.now), \
             patch('scripts.start_gpu.time.sleep', side_effect=sleep), redirect_stdout(output):
            self.assertEqual(main(['--no-browser']), 0)
        self.assertEqual(len(launched), 3)
        self.assertEqual(sum('studio.sync' in command for command in launched), 1)
        for command in launched[:2]:
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('BatchMode=yes', command)
            self.assertIn('ConnectTimeout=10', command)
            self.assertIn(str(self.root / 'gpu_sync_ed25519'), command)
        self.assertLess(output.getvalue().index('GPU 暂未连接'), output.getvalue().index('GPU 工作台已连接'))
        self.assertEqual(output.getvalue().count('GPU 工作台已连接'), 1)
        self.assertNotIn('fixture-token', output.getvalue())
        self.assertTrue(second.terminated)
        self.assertTrue(sync.terminated)
        self.assertFalse((self.root / 'gpu-client.stop').exists())
        released = acquire_lock(self.root / 'gpu-client.lock')
        self.assertIsNotNone(released)
        released.close()

    def test_stop_during_initial_api_wait_starts_no_sync(self):
        env, clock, client = self.configured_environment(), Clock(), FakeClient([False])
        tunnel = FakeProcess(201)
        def sleep(seconds):
            clock.sleep(seconds)
            (self.root / 'gpu-client.stop').touch()
        output = io.StringIO()
        with patch('scripts.start_gpu.environment', return_value=env), \
             patch('scripts.start_gpu.shutil.which', return_value='ssh'), \
             patch('httpx.Client', return_value=client), \
             patch('scripts.start_gpu.subprocess.Popen', return_value=tunnel) as launch, \
             patch('scripts.start_gpu.time.monotonic', side_effect=clock.now), \
             patch('scripts.start_gpu.time.sleep', side_effect=sleep), redirect_stdout(output):
            self.assertEqual(main(['--no-browser']), 0)
        self.assertEqual(launch.call_count, 1)
        self.assertNotIn('studio.sync', launch.call_args.args[0])
        self.assertNotIn('GPU 工作台已连接', output.getvalue())
        self.assertTrue(tunnel.terminated)
        self.assertLessEqual(clock.value, 0.5)

    def test_initial_api_timeout_keeps_lock_and_reconnects_before_sync(self):
        env, clock = self.configured_environment(), Clock()
        processes = [FakeProcess(401), FakeProcess(402), FakeProcess(403)]
        outcomes = iter(('timeout', 'ready'))
        def wait(*args, **kwargs):
            # Neither a failed authentication window nor a retry releases
            # ownership to a second launcher.
            self.assertIsNone(acquire_lock(self.root / 'gpu-client.lock'))
            return next(outcomes)
        def sleep(seconds):
            clock.sleep(seconds)
            if clock.value >= 13:
                (self.root / 'gpu-client.stop').touch()
        output = io.StringIO()
        with patch('scripts.start_gpu.environment', return_value=env), \
             patch('scripts.start_gpu.shutil.which', return_value='ssh'), \
             patch('httpx.Client', return_value=FakeClient([True])), \
             patch('scripts.start_gpu.wait_for_gpu', side_effect=wait) as authentication, \
             patch('scripts.start_gpu.subprocess.Popen', side_effect=processes) as launch, \
             patch('scripts.start_gpu.time.monotonic', side_effect=clock.now), \
             patch('scripts.start_gpu.time.sleep', side_effect=sleep), redirect_stdout(output):
            self.assertEqual(main(['--no-browser']), 0)
        self.assertEqual(authentication.call_count, 2)
        self.assertTrue(processes[0].terminated)
        self.assertEqual(launch.call_count, 3)
        self.assertEqual(sum('studio.sync' in call.args[0] for call in launch.call_args_list), 1)
        self.assertLess(output.getvalue().index('GPU 暂未连接'), output.getvalue().index('GPU 工作台已连接'))

    def test_duplicate_launcher_during_reconnect_never_starts_another_process(self):
        env, client = self.configured_environment(), FakeClient([False])
        incumbent = acquire_lock(self.root / 'gpu-client.lock')
        self.assertIsNotNone(incumbent)
        try:
            output = io.StringIO()
            with patch('scripts.start_gpu.environment', return_value=env), \
                 patch('scripts.start_gpu.shutil.which', return_value='ssh'), \
                 patch('httpx.Client', return_value=client), \
                 patch('scripts.start_gpu.subprocess.Popen') as launch, redirect_stdout(output):
                self.assertEqual(main(['--no-browser']), 0)
            launch.assert_not_called()
            self.assertIn('后台等待连接', output.getvalue())
            self.assertIsNone(acquire_lock(self.root / 'gpu-client.lock'))
        finally:
            incumbent.close()

    def test_api_wait_is_bounded_and_rejects_non_gpu_200_response(self):
        clock, client = Clock(), FakeClient([False])
        with patch('scripts.start_gpu.time.monotonic', side_effect=clock.now), \
             patch('scripts.start_gpu.time.sleep', side_effect=clock.sleep):
            result = wait_for_gpu(client, 'http://localhost', {}, FakeProcess(301),
                                  self.root / 'gpu-client.stop', timeout=3)
        self.assertEqual(result, 'timeout')
        self.assertEqual(clock.value, 3)
        self.assertTrue(all(call[1]['timeout'] <= 3 for call in client.calls))
        wrong_service = Mock()
        wrong_service.get.return_value = httpx.Response(200, json={'execution_target': 'local'})
        self.assertFalse(authenticated_gpu(wrong_service, 'http://localhost', {}))

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
