"""图片 → 多模态消息段。

两条路：
- url   : 把图片链接直接交给模型（云端模型需要能访问该链接，QQ 图床链接通常可访问）。
- base64: 本机读文件转 data URL（本地模型、或图床链接模型拉不到时用）。
- auto  : 端点在本机时用 base64，否则用 url。
任何一步失败都返回 None —— 图片看不懂，不等于对话要中断。
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

log = logging.getLogger("qqagent.vision")

REMOTE_SCHEMES = ("http://", "https://")


def _is_local_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("image/"):
        return mime
    ext = Path(path).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def resolve_transport(cfg: dict[str, Any], model_base_url: str) -> str:
    mode = str(cfg.get("image_transport", "auto")).lower()
    if mode == "auto":
        return "base64" if _is_local_endpoint(model_base_url) else "url"
    return mode if mode in ("url", "base64") else "url"


def image_ref_kind(ref: str) -> str:
    """把图片引用分成 data / remote / local / reject。不信任消息里的任意路径或协议。"""
    text = (ref or "").strip()
    if not text or "\x00" in text:
        return "reject"
    lower = text.lower()
    if lower.startswith("data:"):
        return "data" if lower.startswith("data:image/") else "reject"
    if lower.startswith(("http://", "https://")):
        return "remote"
    if lower.startswith("file:"):
        return "local"
    if "://" in text:
        return "reject"
    # javascript: 一类没有 // 的协议。Windows 盘符（C:\、C:/）除外。
    if ":" in text and not (len(text) >= 3 and text[0].isalpha() and text[1] == ":" and text[2] in "\\/"):
        return "reject"
    return "local"


def file_url_to_path(url: str) -> str | None:
    """只接受本机 file URL。带其它主机名的 file:// 直接拒绝。"""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "file":
        return None
    host = (parsed.netloc or "").lower()
    if host not in ("", "localhost", "127.0.0.1"):
        return None
    return url2pathname(unquote(parsed.path))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child_s = os.path.normcase(os.path.abspath(child.resolve()))
        parent_s = os.path.normcase(os.path.abspath(parent.resolve()))
    except OSError:
        return False
    if child_s == parent_s:
        return False
    try:
        return os.path.commonpath([child_s, parent_s]) == parent_s
    except ValueError:
        return False


def file_to_data_url(
    path: str,
    max_bytes: int,
    allowed_roots: list[str | Path] | None = None,
) -> str | None:
    """只读取 allowed_roots 之内的本地图片。目录为空、路径穿越、异常协议都返回 None。"""
    kind = image_ref_kind(path)
    local = path.strip()
    if kind == "local" and local.lower().startswith("file:"):
        converted = file_url_to_path(local)
        if not converted:
            log.warning("拒绝不受信任的 file URL")
            return None
        local = converted
    elif kind != "local":
        log.warning("拒绝把非本地引用当成本地文件读取")
        return None
    roots = [Path(item) for item in (allowed_roots or []) if str(item).strip()]
    if not roots:
        log.warning("未配置 vision.allowed_dirs，拒绝读取本地图片")
        return None
    candidate = Path(local)
    if not any(_is_within(candidate, root) for root in roots):
        log.warning("拒绝读取允许目录之外的本地图片")
        return None
    try:
        p = candidate.resolve()
    except OSError:
        log.warning("本地图片路径无法解析")
        return None
    try:
        if not p.is_file():
            return None
        size = p.stat().st_size
        if size <= 0 or size > max_bytes:
            log.warning("图片跳过（大小 %d 字节，上限 %d）", size, max_bytes)
            return None
        payload = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        log.warning("读取允许目录内的图片失败")
        return None
    return f"data:{_guess_mime(str(p))};base64,{payload}"


def build_image_part(url: str, detail: str = "auto") -> dict[str, Any] | None:
    if not url:
        return None
    lower = url.lower()
    if lower.startswith(REMOTE_SCHEMES):
        scheme, sep, rest = url.partition(":")
        url = scheme.lower() + sep + rest
    elif not lower.startswith("data:image/"):
        return None
    part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
    if detail and detail != "auto":
        part["image_url"]["detail"] = detail
    return part


class _HttpOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """拒绝跳转到 file:// 或其它非 http(s) 协议，避免远端链接变成本地文件读取。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        scheme = (urlparse(newurl).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise urllib.error.URLError("refused non-http redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_to_data_url(url: str, max_bytes: int, timeout: float = 20.0) -> str | None:
    """把远端图片下回来转成 data URL。只接受 http/https，不跟随到其它协议。"""
    if image_ref_kind(url) != "remote":
        log.warning("拒绝下载非 http(s) 图片地址")
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "qq-agent/0.1"})
    opener = urllib.request.build_opener(_HttpOnlyRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                log.warning("远端图片过大（%s 字节，上限 %d），跳过", declared, max_bytes)
                return None
            payload = resp.read(max_bytes + 1)
            if len(payload) > max_bytes:
                log.warning("远端图片超过上限 %d 字节，跳过", max_bytes)
                return None
            mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        host = urlparse(url).hostname or ""
        log.warning("下载图片失败 host=%s (%s)", host, type(e).__name__)
        return None
    if not mime.startswith("image/"):
        mime = _guess_mime(url)
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"
