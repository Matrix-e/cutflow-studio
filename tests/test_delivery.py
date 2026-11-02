import os
import json
import io
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from studio.delivery import DeliveryError, _begin_send, _db, _setting, _webhook_key, configuration_status, publish_video
from scripts.configure_gpu_delivery import upload_config


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return httpx.Response(200, json=result)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.video = Path(self.tmp.name) / "demo.mp4"
        self.video.write_bytes(b"fake mp4 fixture")
        self.env = patch.dict(os.environ, {"CUTFLOW_DATA": self.tmp.name, "CUTFLOW_PUBLISH_ENABLED": "1",
                    "CUTFLOW_WECOM_MODE": "webhook", "CUTFLOW_WECOM_WEBHOOK": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-123"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_review_gate(self):
        with self.assertRaises(DeliveryError) as raised:
            publish_video(self.video, "demo", "job1")
        self.assertEqual("review_required", raised.exception.code)

    def test_external_webhook_rejected(self):
        for url in ["http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-123", "https://qyapi.weixin.qq.com.evil.test/cgi-bin/webhook/send?key=test-key-123", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-123&url=evil"]:
            with self.assertRaises(DeliveryError):
                _webhook_key(url)

    def test_upload_then_send_and_persistent_idempotency(self):
        client = FakeClient([{"errcode": 0, "media_id": "fake-media"}, {"errcode": 0, "errmsg": "ok"}])
        with patch("studio.delivery.httpx.Client", return_value=client):
            result = publish_video(self.video, "测试成片", "job1", approved=True)
            again = publish_video(self.video, "测试成片", "job1", approved=True)
        self.assertEqual("published", result["status"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(2, len(client.calls))
        self.assertIn("media", client.calls[0][1]["files"])
        self.assertEqual({"msgtype": "file", "file": {"media_id": "fake-media"}}, client.calls[1][1]["json"])
        self.assertNotIn("test-key", str(result))

    def test_uncertain_send_never_auto_retries(self):
        client = FakeClient([{"errcode": 0, "media_id": "fake-media"}, httpx.ReadTimeout("secret in URL must not leak")])
        with patch("studio.delivery.httpx.Client", return_value=client):
            with self.assertRaises(DeliveryError) as raised:
                publish_video(self.video, "demo", "job2", approved=True)
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn("secret", str(raised.exception))
            with self.assertRaises(DeliveryError) as retry:
                publish_video(self.video, "demo", "job2", approved=True)
            self.assertTrue(retry.exception.uncertain)
        self.assertEqual(2, len(client.calls))

    def test_provider_reject_does_not_claim_success(self):
        client = FakeClient([{"errcode": 0, "media_id": "fake-media"}, {"errcode": 40014, "errmsg": "invalid token"}])
        with patch("studio.delivery.httpx.Client", return_value=client):
            with self.assertRaises(DeliveryError) as raised:
                publish_video(self.video, "demo", "job3", approved=True)
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual("wecom_rejected", raised.exception.code)

    def test_durable_per_destination_rate_limit(self):
        with closing(_db()) as database:
            for index in range(20):
                _begin_send(database, f"rate-{index}", "same-destination")
            with self.assertRaises(DeliveryError) as raised:
                _begin_send(database, "rate-21", "same-destination")
            self.assertEqual("rate_limited", raised.exception.code)
            _begin_send(database, "another-target", "other-destination")

    def test_runtime_file_live_reload_allowlist_and_environment_priority(self):
        path = Path(self.tmp.name) / "wecom-config.json"
        minimal = {"CUTFLOW_DATA": self.tmp.name}
        with patch.dict(os.environ, minimal, clear=True):
            self.assertFalse(configuration_status()["enabled"])
            config = {"CUTFLOW_WECOM_MODE": "webhook", "CUTFLOW_WECOM_WEBHOOK": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=file-key-123",
                      "CUTFLOW_PUBLISH_ENABLED": "1", "CUTFLOW_AUTO_PUBLISH": "1", "CUTFLOW_TOKEN": "never-import"}
            path.write_text(json.dumps(config), encoding="utf-8")
            status = configuration_status()
            self.assertTrue(status["enabled"])
            self.assertTrue(status["configured"])
            self.assertNotIn("file-key", str(status))
            self.assertEqual("", _setting("CUTFLOW_AUTO_PUBLISH"))
            self.assertEqual("", _setting("CUTFLOW_TOKEN"))
            config["CUTFLOW_PUBLISH_ENABLED"] = "0"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertFalse(configuration_status()["enabled"])
            with patch.dict(os.environ, {"CUTFLOW_PUBLISH_ENABLED": "1"}):
                self.assertTrue(configuration_status()["enabled"])
            path.write_text("invalid-json", encoding="utf-8")
            self.assertFalse(configuration_status()["enabled"])

    def test_gpu_configuration_is_protected_before_write_and_has_no_auto_flag(self):
        class FakeSFTP:
            def __init__(self):
                self.files, self.modes, self.events = {}, {}, []

            def open(self, path, mode):
                if mode == "rb":
                    return io.BytesIO(self.files[path])
                owner = self
                class Writer(io.BytesIO):
                    def write(self, data):
                        self.assert_protected = owner.modes.get(path) == 0o600
                        if not self.assert_protected:
                            raise AssertionError("secret was written before chmod")
                        return super().write(data)
                    def close(self):
                        owner.files[path] = self.getvalue()
                        super().close()
                self.events.append(("create", path, mode))
                return Writer()

            def chmod(self, path, mode):
                self.modes[path] = mode

            def posix_rename(self, source, target):
                self.files[target] = self.files.pop(source)
                self.modes[target] = self.modes.pop(source)

            def stat(self, path):
                return SimpleNamespace(st_mode=self.modes[path])

            def remove(self, path):
                if path not in self.files:
                    raise FileNotFoundError()
                del self.files[path]

        sftp = FakeSFTP()
        upload_config(sftp, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=upload-key-123")
        target = "/root/autodl-tmp/cutflow-studio/data/wecom-config.json"
        self.assertEqual([target], list(sftp.files))
        config = json.loads(sftp.files[target])
        self.assertEqual({"CUTFLOW_WECOM_MODE", "CUTFLOW_WECOM_WEBHOOK", "CUTFLOW_PUBLISH_ENABLED"}, set(config))
        self.assertEqual("1", config["CUTFLOW_PUBLISH_ENABLED"])
        self.assertEqual(0o600, sftp.modes[target])


if __name__ == "__main__":
    unittest.main()
