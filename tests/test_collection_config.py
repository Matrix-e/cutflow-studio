import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.configure_douyin import MAX_INT64, KEYS, main, save_local, upload_config, validate_config


class FakeSFTP:
    def __init__(self, corrupt=False):
        self.files, self.modes, self.events, self.corrupt = {}, {}, [], corrupt

    def open(self, path, mode):
        if mode == "rb":
            return io.BytesIO(self.files[path] + (b"bad" if self.corrupt else b""))
        owner = self
        class Writer(io.BytesIO):
            def write(self, data):
                if owner.modes.get(path) != 0o600:
                    raise AssertionError("secret written before chmod")
                owner.events.append("write")
                return super().write(data)
            def close(self):
                if not self.closed:
                    owner.files[path] = self.getvalue()
                super().close()
        self.events.append("create")
        return Writer()

    def chmod(self, path, mode):
        self.events.append("chmod")
        self.modes[path] = mode

    def posix_rename(self, source, target):
        self.events.append("rename")
        self.files[target], self.modes[target] = self.files.pop(source), self.modes.pop(source)

    def stat(self, path):
        return SimpleNamespace(st_mode=self.modes[path])

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError()
        del self.files[path]


class CollectionConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = validate_config("fake-client-key", "fake-client-secret", "1234567890123456789")

    def test_int64_boundaries_and_canonical_string(self):
        self.assertEqual("1", validate_config("key", "secret", "0001")["CUTFLOW_DOUYIN_DEVICE_ID"])
        self.assertEqual(str(MAX_INT64), validate_config("key", "secret", str(MAX_INT64))["CUTFLOW_DOUYIN_DEVICE_ID"])
        for value in ["0", "-1", "+1", "1.0", "1e3", str(MAX_INT64 + 1), "", "123\n456"]:
            with self.assertRaises(ValueError):
                validate_config("key", "secret", value)

    def test_upload_exact_fields_protects_before_write(self):
        sftp = FakeSFTP()
        upload_config(sftp, self.config)
        target = "/root/autodl-tmp/cutflow-studio/data/douyin-config.json"
        self.assertEqual([target], list(sftp.files))
        self.assertEqual(KEYS, set(json.loads(sftp.files[target])))
        self.assertEqual(0o600, sftp.modes[target])
        self.assertEqual(["create", "chmod", "write", "rename"], sftp.events)
        with self.assertRaises(ValueError):
            upload_config(sftp, {**self.config, "CUTFLOW_AUTO_PUBLISH": "1"})

    def test_bad_remote_copy_never_replaces_previous_config(self):
        sftp = FakeSFTP(corrupt=True)
        target = "/root/autodl-tmp/cutflow-studio/data/douyin-config.json"
        sftp.files[target] = b"previous-config"
        with self.assertRaises(RuntimeError):
            upload_config(sftp, self.config)
        self.assertEqual({target: b"previous-config"}, sftp.files)
        self.assertNotIn("rename", sftp.events)

    def test_local_write_is_protected_before_secret_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "douyin-config.json"
            target.write_text("previous", encoding="utf-8")
            def protect(path):
                self.assertEqual(b"", path.read_bytes())
                self.assertEqual("previous", target.read_text())
            with patch("scripts.configure_douyin._restrict_local_file", side_effect=protect) as restricted:
                self.assertEqual(target.resolve(), save_local(self.config, root))
            restricted.assert_called_once()
            self.assertEqual(self.config, json.loads(target.read_text()))
            self.assertEqual([target], list(root.iterdir()))

    def test_local_permission_failure_keeps_old_config_and_no_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "douyin-config.json"
            target.write_text("previous", encoding="utf-8")
            with patch("scripts.configure_douyin._restrict_local_file", side_effect=PermissionError("private")):
                with self.assertRaises(PermissionError):
                    save_local(self.config, root)
            self.assertEqual("previous", target.read_text())
            self.assertEqual([target], list(root.iterdir()))

    def test_configurator_never_prints_credentials_or_calls_search(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("scripts.configure_douyin.environment", return_value={"CUTFLOW_DATA": directory}), \
                 patch("scripts.configure_douyin.getpass.getpass", side_effect=["fake-client-key", "fake-client-secret", "1234567890123456789"]), \
                 patch("scripts.configure_douyin._restrict_local_file"), \
                 patch("scripts.configure_douyin.SFTPTransport") as transport, redirect_stdout(output):
                result = main(["--local"])
            self.assertEqual(0, result)
            transport.assert_not_called()
            for value in self.config.values():
                self.assertNotIn(value, output.getvalue())
            self.assertIn("aweme.dy.video_search", output.getvalue())


if __name__ == "__main__":
    unittest.main()
