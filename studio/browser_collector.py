"""User-operated Chrome collection bridge. No login, CAPTCHA, or player bypass.

Only rendered public links and exposed HTML video URLs are inspected. Downloaded
files stay in a separate quarantine with unknown commercial rights. No personal
Chrome profiles/cookies are read or exported; no hidden Douyin APIs are called.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import socket
import time
from urllib.parse import quote, urljoin, urlsplit
import uuid

import httpx

from .media import inspect_media

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
CDN_SUFFIXES = ("douyinvod.com", "douyincdn.com", "bytecdn.com", "bytecdn.cn")
FAILURE_CODES = {"login_required", "captcha_required", "download_unavailable", "browser_unavailable", "collection_failed"}


class CollectionError(RuntimeError):
    def __init__(self, code: str, message: str, *, items: list[dict] | None = None):
        super().__init__(message)
        self.code = code
        self.items = items or []


def canonical_video_url(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r"/video/([0-9]{1,32})/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in {"www.douyin.com", "douyin.com"} or parsed.port not in (None, 443) or parsed.username or parsed.password or not match:
            return None
        return "https://www.douyin.com/video/" + match.group(1)
    except (TypeError, ValueError):
        return None


def validate_cdn_url(value: str, resolver=socket.getaddrinfo) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment or len(value) > 8192
                or not any(host == suffix or host.endswith("." + suffix) for suffix in CDN_SUFFIXES)):
            raise ValueError()
        addresses = resolver(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
            raise ValueError()
        return value
    except (ValueError, TypeError, OSError):
        raise CollectionError("download_unavailable", "播放器未提供受支持的公开 HTTPS 视频地址。") from None


def _deadline(limit: float):
    if time.monotonic() >= limit:
        raise CollectionError("collection_failed", "本次采集已达到时间上限；已完成的本地素材会保留。")


def _pause(limit: float):
    _deadline(limit)
    time.sleep(random.uniform(1.0, 3.0))


def blocking_reason(page) -> str | None:
    try:
        text = page.locator("body").inner_text(timeout=5000)[:100000]
    except Exception:
        return None
    if re.search(r"拖动下方滑块|请完成.{0,12}安全验证|请完成验证|验证后继续|点击完成验证", text):
        return "captcha_required"
    if re.search(r"请先登录|登录后继续|登录后查看", text):
        return "login_required"
    return None


def _check_page(page):
    reason = blocking_reason(page)
    if reason:
        message = "请在采集专用 Chrome 中手动完成验证，再重新运行任务。" if reason == "captcha_required" else "请在采集专用 Chrome 中手动登录，再重新运行任务。"
        raise CollectionError(reason, message)


def discover_links(page, keyword: str, maximum: int, limit: float) -> list[dict]:
    if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 120:
        raise CollectionError("collection_failed", "关键词应为 1–120 个字符。")
    page.goto("https://www.douyin.com/search/" + quote(keyword.strip(), safe="") + "?type=video", wait_until="domcontentloaded", timeout=30000)
    _pause(limit)
    found = {}
    for _ in range(7):
        _check_page(page)
        anchors = page.locator("a[href*='/video/']").evaluate_all("""els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && getComputedStyle(e).display !== 'none').map(e => ({href:e.href,title:e.getAttribute('title') || e.innerText || e.getAttribute('aria-label') || ''}))""")
        for entry in anchors[:500]:
            url = canonical_video_url(entry.get("href", ""))
            if url and url not in found:
                item_id = url.rsplit("/", 1)[1]
                found[url] = {"item_id": item_id, "source_url": url,
                              "title": str(entry.get("title", "")).strip()[:2000], "author": "",
                              "cover_url": None, "published_at": None, "likes": None,
                              "rights_status": "unknown", "download_status": "not_requested"}
            if len(found) >= maximum:
                return list(found.values())
        page.mouse.wheel(0, 800)
        _pause(limit)
    return list(found.values())


def exposed_video_url(page) -> str | None:
    values = page.locator("video").evaluate_all("""els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden').map(e => e.currentSrc || e.src || '')""")
    return next((url for url in values if isinstance(url, str) and url.startswith("https://")), None)


def visible_download_url(page) -> str | None:
    """Prefer a visible native download link; do not guess script-only controls."""
    links = page.locator("a[href]").evaluate_all("""els => els.filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' && (e.hasAttribute('download') || /^(下载|保存本地|下载视频)$/.test((e.innerText || '').trim()))).map(e => e.href)""")
    for url in links[:30]:
        try:
            return validate_cdn_url(url)
        except CollectionError:
            continue
    return None


def _request(context, method: str, url: str, *, headers=None):
    # Playwright preserves this dedicated browser session. No cookie export,
    # header spoofing, response interception, or private player API is used.
    for _ in range(4):
        validate_cdn_url(url)
        try:
            response = context.request.fetch(url, method=method, headers=headers or {},
                                             timeout=30000, max_redirects=0, fail_on_status_code=False)
        except Exception:
            raise CollectionError("download_unavailable", "视频文件请求未完成；可在原页面检查是否支持下载。") from None
        if response.status not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location", "")
        response.dispose()
        url = urljoin(url, location)
    raise CollectionError("download_unavailable", "视频地址重定向过多，已停止下载。")


def download_video(context, media_url: str, output: Path, limit: float) -> dict:
    """Range-download a size-declared public CDN resource, then verify the file.

    The browser request API buffers each response. HEAD bounds declared total
    size to 128 MiB; byte ranges normally bound responses to 4 MiB. Oversized,
    unbounded, changed, or non-range responses are rejected, never published.
    """
    _deadline(limit)
    response = _request(context, "HEAD", media_url)
    try:
        length_text = response.headers.get("content-length", "")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if response.status != 200 or not re.fullmatch(r"[0-9]{1,12}", length_text):
            raise CollectionError("download_unavailable", "视频未提供可验证的文件大小，已停止下载。")
        total = int(length_text)
        if not 1 <= total <= MAX_BYTES or content_type not in {"video/mp4", "video/webm", "application/octet-stream"}:
            raise CollectionError("download_unavailable", "视频格式不受支持或文件超过 128 MB。")
    finally:
        response.dispose()
    if output.exists():
        raise CollectionError("download_unavailable", "本地已有同名素材，已保留现有文件。")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + "." + uuid.uuid4().hex[:8] + ".part")
    digest = hashlib.sha256()
    try:
        with partial.open("xb") as stream:
            for start in range(0, total, CHUNK_BYTES):
                _deadline(limit)
                end = min(total-1, start+CHUNK_BYTES-1)
                result = _request(context, "GET", media_url, headers={"Range": f"bytes={start}-{end}"})
                try:
                    expected = end-start+1
                    content_range = result.headers.get("content-range", "")
                    if result.status == 206:
                        if content_range != f"bytes {start}-{end}/{total}":
                            raise CollectionError("download_unavailable", "视频分段信息发生变化，已停止下载。")
                    elif result.status != 200 or start != 0 or total > CHUNK_BYTES:
                        raise CollectionError("download_unavailable", "视频服务器不支持受限分段下载。")
                    declared = result.headers.get("content-length", "")
                    if not declared.isascii() or not declared.isdigit() or int(declared) != expected:
                        raise CollectionError("download_unavailable", "视频分段大小无法验证，已停止下载。")
                    block = result.body()
                    if len(block) != expected or len(block) > CHUNK_BYTES:
                        raise CollectionError("download_unavailable", "视频实际分段大小不匹配，已停止下载。")
                    stream.write(block)
                    digest.update(block)
                finally:
                    result.dispose()
        metadata = inspect_media(partial)
        if not metadata.get("video_codec") or metadata["duration"] <= 0:
            raise CollectionError("download_unavailable", "下载文件未通过视频检查。")
        # No overwrite race: same-directory hard link publishes verified bytes.
        os.link(partial, output)
        return {"local_filename": output.name, "sha256": digest.hexdigest(), "size_bytes": total,
                "download_status": "downloaded", "reason": "已保存至本地待确认素材区，商用授权尚未确认。"}
    except CollectionError:
        raise
    except Exception:
        raise CollectionError("download_unavailable", "下载文件未通过完整性检查或本地保存失败。") from None
    finally:
        if partial.exists():
            partial.unlink()


def existing_download(output: Path) -> dict | None:
    """Reuse only a matching provenance/size/hash pair; never silently overwrite."""
    sidecar = output.with_suffix(".provenance.json")
    if not output.exists() and not sidecar.exists():
        return None
    try:
        if not output.is_file() or not sidecar.is_file() or sidecar.stat().st_size > 32768:
            raise ValueError()
        provenance = json.loads(sidecar.read_text(encoding="utf-8"))
        expected_url = "https://www.douyin.com/video/" + output.stem
        size = output.stat().st_size
        expected_hash = provenance.get("sha256", "")
        if (not 0 < size <= MAX_BYTES or size != provenance.get("size_bytes")
                or provenance.get("item_id") != output.stem or provenance.get("source_url") != expected_url
                or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise ValueError()
        digest = hashlib.sha256()
        with output.open("rb") as stream:
            while block := stream.read(CHUNK_BYTES):
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError()
        metadata = inspect_media(output)
        if not metadata.get("video_codec") or metadata.get("duration", 0) <= 0:
            raise ValueError()
        return {"local_filename": output.name, "sha256": expected_hash, "size_bytes": size,
                "download_status": "downloaded", "reason": "已核对并复用本地素材，未重复下载；商用授权仍需确认。"}
    except Exception:
        raise CollectionError("download_unavailable", "已有同名素材或来源记录校验失败，已保留原文件并停止覆盖。") from None


def collect_run(context, page, run: dict, output_dir: Path) -> list[dict]:
    maximum = min(10, max(1, int(run.get("max_items", 10))))
    if run.get("publish_time", 0) or run.get("sort_type", 0):
        raise CollectionError("collection_failed", "浏览器采集当前支持默认排序和不限时间，请调整任务筛选条件。")
    limit = time.monotonic() + 12 * 60
    items = discover_links(page, run["keyword"], maximum, limit)
    if not items:
        raise CollectionError("download_unavailable", "页面未呈现可采集的视频链接；请检查登录状态和关键词。")
    for index, item in enumerate(items):
        try:
            _deadline(limit)
            output = output_dir / (item["item_id"] + ".mp4")
            cached = existing_download(output)
            if cached is not None:
                item.update(cached)
                continue
            page.goto(item["source_url"], wait_until="domcontentloaded", timeout=30000)
            _pause(limit)
            _check_page(page)
            media_url = visible_download_url(page) or exposed_video_url(page)
            if not media_url:
                raise CollectionError("download_unavailable", "页面播放器未公开可下载文件地址，已仅保存来源信息。")
            item.update(download_video(context, media_url, output, limit))
            provenance = {"version": 1, "source_url": item["source_url"], "keyword": run["keyword"],
                "item_id": item["item_id"], "title": item["title"], "sha256": item["sha256"],
                "size_bytes": item["size_bytes"], "rights_status": "unknown", "watermark_removed": False,
                "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            output.with_suffix(".provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
        except CollectionError as exc:
            if exc.code in {"login_required", "captcha_required", "collection_failed"}:
                retained = sum(entry.get("download_status") == "downloaded" for entry in items)
                raise CollectionError(exc.code, str(exc) + f" 已完成的 {retained} 条素材及来源记录保留在本地待确认区。", items=items[:index]) from None
            item.update(download_status="not_available", reason=str(exc))
        except Exception:
            item.update(download_status="failed", reason="该视频页面读取失败，已保留来源信息。")
    return items


def report_items(items: list[dict]) -> list[dict]:
    allowed = {"item_id", "source_url", "title", "author", "cover_url", "published_at", "likes",
               "rights_status", "download_status", "local_filename", "sha256", "size_bytes", "reason"}
    return [{key: value for key, value in item.items() if key in allowed}
            for item in items[:10] if isinstance(item, dict)]


@contextmanager
def profile_lock(profile: Path):
    profile.parent.mkdir(parents=True, exist_ok=True)
    handle = (profile.parent / ".douyin-browser.lock").open("a+b")
    locked = False
    try:
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise CollectionError("browser_unavailable", "采集浏览器已由另一个进程使用。") from None
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def safe_output_dir(value: str | Path) -> Path:
    output = Path(value).resolve()
    inboxes = {ROOT / "素材投递箱", ROOT / "inbox"}
    if os.environ.get("CUTFLOW_INBOX"):
        inboxes.add(Path(os.environ["CUTFLOW_INBOX"]))
    for config_path in (ROOT / "config" / "local.json", ROOT / "config" / "private.json"):
        try:
            if config_path.stat().st_size <= 32768:
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if isinstance(config, dict) and isinstance(config.get("CUTFLOW_INBOX"), str):
                    inboxes.add(Path(config["CUTFLOW_INBOX"]))
        except (OSError, ValueError):
            continue
    if any(output.is_relative_to(inbox.resolve()) for inbox in inboxes):
        raise CollectionError("collection_failed", "采集素材目录不能位于自动剪辑投递箱内；请使用独立待确认区。")
    return output


def _api_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or ".." in parsed.path.split("/")
            or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
        raise CollectionError("collection_failed", "服务地址必须为 HTTPS 或本机转发地址。")
    return value.rstrip("/")


def _post(client, api: str, route: str, body: dict) -> dict:
    try:
        response = client.post(api + route, json=body)
        if response.status_code != 200:
            raise ValueError()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise CollectionError("collection_failed", "无法与剪辑服务同步采集任务，请检查本地连接。") from None


def main():
    parser = argparse.ArgumentParser(description="在专用 Chrome 中采集已公开的可下载视频；登录和验证由本人完成")
    data = Path(os.environ.get("CUTFLOW_DATA", "") or ROOT / "data").resolve()
    parser.add_argument("--api-url", default=os.environ.get("CUTFLOW_BROWSER_API_URL", "http://127.0.0.1:8766"))
    parser.add_argument("--token-file", default=str(data / "gpu-access-token.txt"))
    parser.add_argument("--profile-dir", default=str(data / "douyin-browser"))
    parser.add_argument("--output-dir", default=str(data / "collected" / "douyin"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true")
    group.add_argument("--login-only", action="store_true")
    args = parser.parse_args()
    profile = Path(args.profile_dir).resolve()
    if profile != data / "douyin-browser":
        parser.exit(1, "必须使用项目专用 data/douyin-browser，不能读取个人 Chrome 配置。\n")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.exit(1, "尚未安装 playwright Python 依赖；安装后将使用已有 Chrome。\n")
    try:
        api = _api_url(args.api_url)
        output_dir = safe_output_dir(args.output_dir)
        token = os.environ.get("CUTFLOW_BROWSER_TOKEN", "").strip()
        if not args.login_only and not token:
            token = Path(args.token_file).read_text(encoding="utf-8-sig").strip()
        if not args.login_only and (not token or len(token) > 4096):
            raise CollectionError("collection_failed", "缺少有效的剪辑服务连接凭据。")
        with profile_lock(profile), sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), channel="chrome", headless=False,
                viewport={"width": 1280, "height": 900})
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
                print("专用采集浏览器已打开。登录和验证码请本人操作；素材只进入待确认区。", flush=True)
                if args.login_only:
                    input("完成登录后按回车关闭浏览器并保存该专用会话：")
                    return
                input("请先在专用 Chrome 中完成登录或确认页面可访问，再按回车开始领取采集任务：")
                agent_id = "desktop-" + uuid.uuid4().hex[:16]
                with httpx.Client(headers={"Authorization": "Bearer " + token}, timeout=20,
                                  trust_env=False, follow_redirects=False) as client:
                    while True:
                        claimed = _post(client, api, "/api/collections/browser/claim", {"agent_id": agent_id})
                        run = claimed.get("run")
                        if run:
                            run_id = str(run.get("id", ""))
                            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
                                raise CollectionError("collection_failed", "任务编号无效。")
                            route = "/api/collections/browser/runs/" + run_id
                            try:
                                items = collect_run(context, page, run, output_dir)
                            except CollectionError as exc:
                                _post(client, api, route + "/fail", {"lease_token": run["lease_token"],
                                    "code": exc.code if exc.code in FAILURE_CODES else "collection_failed",
                                    "message": str(exc), "items": report_items(exc.items)})
                                print(str(exc), flush=True)
                                if exc.code in {"login_required", "captcha_required"}:
                                    input("采集已暂停。请手动完成登录或验证，并在工作台重新运行失败任务；准备好后按回车继续等待：")
                            else:
                                _post(client, api, route + "/complete", {"lease_token": run["lease_token"], "items": report_items(items)})
                                print(f"本次记录 {len(items)} 条来源，其中 {sum(item['download_status'] == 'downloaded' for item in items)} 条已下载至待确认区。", flush=True)
                        if args.once:
                            break
                        time.sleep(60)
            finally:
                context.close()
    except KeyboardInterrupt:
        return
    except CollectionError as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception:
        parser.exit(1, "采集浏览器未能完成操作；请检查 Chrome、连接和目录权限后重新启动。\n")


if __name__ == "__main__":
    main()
