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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def file_to_data_url(path: str, max_bytes: int) -> str | None:
    p = Path(path)
    try:
        if not p.is_file():
            return None
        size = p.stat().st_size
        if size <= 0 or size > max_bytes:
            log.warning("图片跳过（大小 %d 字节，上限 %d）: %s", size, max_bytes, p)
            return None
        payload = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError as e:
        log.warning("读取图片失败 %s: %s", p, e)
        return None
    return f"data:{_guess_mime(path)};base64,{payload}"


def build_image_part(url: str, detail: str = "auto") -> dict[str, Any] | None:
    if not url:
        return None
    if not (url.startswith(REMOTE_SCHEMES) or url.startswith("data:image/")):
        return None
    part: dict[str, Any] = {"type": "image_url", "image_url": {"url": url}}
    if detail and detail != "auto":
        part["image_url"]["detail"] = detail
    return part


def download_to_data_url(url: str, max_bytes: int, timeout: float = 20.0) -> str | None:
    """把远端图片下回来转成 data URL。用于本地模型或模型拉不到图床的情况。"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "qq-agent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        log.warning("下载图片失败 %s: %s", url[:80], e)
        return None
    if not mime.startswith("image/"):
        mime = _guess_mime(url)
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime};base64,{encoded}"
