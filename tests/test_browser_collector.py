"""Fixtures only: never opens a live browser or sends a live download request."""
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from studio.browser_collector import (CollectionError, MAX_BYTES, canonical_video_url,
    collect_run, discover_links, download_video, existing_download, exposed_video_url,
    profile_lock, report_items, safe_output_dir, validate_cdn_url, visible_download_url)


class Response:
    def __init__(self, status, headers, body=b""):
        self.status, self.headers, self._body = status, headers, body
        self.disposed = False
    def body(self):
        return self._body
    def dispose(self):
        self.disposed = True


class BrowserMetadataTests(unittest.TestCase):
    def test_canonical_links_strip_tracking_and_reject_other_origins(self):
        self.assertEqual(canonical_video_url("https://www.douyin.com/video/123?tracking=secret"), "https://www.douyin.com/video/123")
        self.assertIsNone(canonical_video_url("https://www.douyin.com.evil.test/video/123"))
        self.assertIsNone(canonical_video_url("https://user:secret@www.douyin.com/video/123"))
        self.assertIsNone(canonical_video_url("javascript:alert(1)"))

    def test_cdn_rejects_nonpublic_dns_and_unapproved_hosts(self):
        private = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        public = lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with self.assertRaises(CollectionError):
            validate_cdn_url("https://v.example.com/file.mp4", public)
        with self.assertRaises(CollectionError):
            validate_cdn_url("https://v.douyinvod.com/file.mp4", private)
        self.assertEqual(validate_cdn_url("https://v.douyinvod.com/file.mp4", public), "https://v.douyinvod.com/file.mp4")

    def test_discovery_uses_only_rendered_anchors_and_deduplicates(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "搜索结果"
        page.locator.return_value.evaluate_all.return_value = [
            {"href": "https://www.douyin.com/video/123", "title": "口播"},
            {"href": "https://www.douyin.com/video/123?x=1", "title": "duplicate"},
            {"href": "https://evil.test/video/456", "title": "ignored"},
            {"href": "https://www.douyin.com/video/789", "title": "课程"}]
        with patch("studio.browser_collector._pause"):
            found = discover_links(page, "口播教程", 2, time.monotonic()+100)
        self.assertEqual([item["item_id"] for item in found], ["123", "789"])
        self.assertIn("type=video", page.goto.call_args.args[0])
        self.assertTrue(all(item["rights_status"] == "unknown" for item in found))

    def test_login_or_verification_is_handoff_not_automated(self):
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "请完成验证"
        with patch("studio.browser_collector._pause"):
            with self.assertRaises(CollectionError) as caught:
                discover_links(page, "口播", 1, time.monotonic()+100)
        self.assertEqual(caught.exception.code, "captcha_required")
        page.locator.return_value.click.assert_not_called()

    def test_blob_player_is_not_treated_as_downloadable_url(self):
        page = MagicMock()
        page.locator.return_value.evaluate_all.return_value = ["blob:https://www.douyin.com/test"]
        self.assertIsNone(exposed_video_url(page))

    def test_visible_download_link_is_selected_without_private_api(self):
        page = MagicMock()
        page.locator.return_value.evaluate_all.return_value = ["https://v.douyinvod.com/file.mp4"]
        with patch("studio.browser_collector.validate_cdn_url", side_effect=lambda url: url):
            self.assertEqual(visible_download_url(page), "https://v.douyinvod.com/file.mp4")


class BoundedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cutflow-browser-test-")
        self.output = Path(self.temp.name)/"123.mp4"
    def tearDown(self):
        self.temp.cleanup()

    def test_verified_ranges_publish_file_and_hash(self):
        payload = b"abcdefghij"
        replies = [Response(200, {"content-length": "10", "content-type": "video/mp4"}),
            Response(206, {"content-length": "4", "content-range": "bytes 0-3/10"}, payload[:4]),
            Response(206, {"content-length": "4", "content-range": "bytes 4-7/10"}, payload[4:8]),
            Response(206, {"content-length": "2", "content-range": "bytes 8-9/10"}, payload[8:])]
        with patch("studio.browser_collector.CHUNK_BYTES", 4), \
             patch("studio.browser_collector._request", side_effect=replies) as request, \
             patch("studio.browser_collector.inspect_media", return_value={"video_codec": "h264", "duration": 2}):
            result = download_video(object(), "https://v.douyinvod.com/file.mp4", self.output, time.monotonic()+100)
        self.assertEqual(self.output.read_bytes(), payload)
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["download_status"], "downloaded")
        self.assertEqual(request.call_count, 4)
        self.assertEqual(list(self.output.parent.glob("*.part")), [])

    def test_oversized_declaration_stops_before_get(self):
        head = Response(200, {"content-length": str(MAX_BYTES+1), "content-type": "video/mp4"})
        with patch("studio.browser_collector._request", return_value=head) as request:
            with self.assertRaises(CollectionError):
                download_video(object(), "https://v.douyinvod.com/file.mp4", self.output, time.monotonic()+100)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_changed_content_range_does_not_publish_partial(self):
        replies = [Response(200, {"content-length": "4", "content-type": "video/mp4"}),
                   Response(206, {"content-length": "4", "content-range": "bytes 0-3/5"}, b"data")]
        with patch("studio.browser_collector._request", side_effect=replies):
            with self.assertRaises(CollectionError):
                download_video(object(), "https://v.douyinvod.com/file.mp4", self.output, time.monotonic()+100)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob("*.part")), [])

    def test_unavailable_player_returns_reference_not_fake_success(self):
        item = {"item_id":"123", "source_url":"https://www.douyin.com/video/123", "title":"课程", "rights_status":"unknown", "download_status":"not_requested"}
        page = MagicMock()
        with patch("studio.browser_collector.discover_links", return_value=[item]), \
             patch("studio.browser_collector._pause"), patch("studio.browser_collector._check_page"), \
             patch("studio.browser_collector.visible_download_url", return_value=None), \
             patch("studio.browser_collector.exposed_video_url", return_value=None):
            results = collect_run(object(), page, {"keyword":"课程", "max_items":1}, self.output.parent)
        self.assertEqual(results[0]["download_status"], "not_available")
        self.assertNotIn("sha256", results[0])

    def _cached_file(self):
        self.output.write_bytes(b"verified-video")
        self.output.with_suffix(".provenance.json").write_text(json.dumps({
            "item_id":"123", "source_url":"https://www.douyin.com/video/123",
            "size_bytes":14, "sha256":hashlib.sha256(b"verified-video").hexdigest()}), encoding="utf-8")

    def test_existing_file_reused_after_hash_verification_without_navigation(self):
        self._cached_file()
        item = {"item_id":"123", "source_url":"https://www.douyin.com/video/123", "title":"课程", "download_status":"not_requested"}
        page = MagicMock()
        with patch("studio.browser_collector.inspect_media", return_value={"video_codec":"h264", "duration":2}), \
             patch("studio.browser_collector.discover_links", return_value=[item]), \
             patch("studio.browser_collector.download_video") as downloader:
            results = collect_run(object(), page, {"keyword":"other keyword", "max_items":1}, self.output.parent)
        page.goto.assert_not_called()
        downloader.assert_not_called()
        self.assertEqual(results[0]["download_status"], "downloaded")
        self.assertEqual(self.output.read_bytes(), b"verified-video")

    def test_tampered_cached_file_is_not_overwritten(self):
        self._cached_file()
        self.output.write_bytes(b"tampered-video")
        with self.assertRaises(CollectionError):
            existing_download(self.output)
        self.assertEqual(self.output.read_bytes(), b"tampered-video")

    def test_direct_module_output_cannot_target_watched_inbox(self):
        inbox = self.output.parent / "watched"
        with patch.dict("os.environ", {"CUTFLOW_INBOX":str(inbox)}):
            with self.assertRaises(CollectionError):
                safe_output_dir(inbox / "incoming")

    def test_profile_lock_rejects_second_owner_and_releases_cleanly(self):
        profile = self.output.parent / "douyin-browser"
        with profile_lock(profile):
            with self.assertRaises(CollectionError):
                with profile_lock(profile):
                    self.fail("second owner must not acquire a profile")
        with profile_lock(profile):
            pass

    def test_later_verification_failure_preserves_processed_records(self):
        items = [{"item_id":str(i), "source_url":f"https://www.douyin.com/video/{i}", "title":"课程", "download_status":"not_requested"} for i in (123,456)]
        cached = {"local_filename":"123.mp4", "sha256":"a"*64, "size_bytes":10, "download_status":"downloaded"}
        with patch("studio.browser_collector.discover_links", return_value=items), \
             patch("studio.browser_collector.existing_download", side_effect=[cached, None]), \
             patch("studio.browser_collector._pause"), \
             patch("studio.browser_collector._check_page", side_effect=CollectionError("captcha_required", "请完成验证")):
            with self.assertRaises(CollectionError) as caught:
                collect_run(object(), MagicMock(), {"keyword":"课程", "max_items":2}, self.output.parent)
        self.assertEqual(caught.exception.code, "captcha_required")
        self.assertEqual(len(caught.exception.items), 1)
        self.assertEqual(caught.exception.items[0]["download_status"], "downloaded")
        payload = report_items([{**caught.exception.items[0], "secret":"never forward", "absolute_path":"C:/private"}])
        self.assertNotIn("secret", payload[0])
        self.assertNotIn("absolute_path", payload[0])


if __name__ == "__main__":
    unittest.main()
