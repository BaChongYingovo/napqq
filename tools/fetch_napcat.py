"""下载 NapCat 与 QQ 安装包到 <NAPCAT_DIR>\\downloads。

只依赖标准库。支持断点续传、重试、大小校验、sha256。
GitHub 主站在本机被墙，NapCat 资产走 gh-proxy.com；QQ 安装包走腾讯官方 CDN。
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TARGET_DIR = Path(r"<NAPCAT_DIR>\downloads")

NAP_VER = "v4.18.33"
NAPCAT_URL = (
    "https://gh-proxy.com/https://github.com/NapNeko/NapCatQQ/releases/download/"
    f"{NAP_VER}/NapCat.Shell.zip"
)
NAPCAT_EXPECT = 29500155

QQ_URL = "https://qqdl.gtimg.cn/qqfile/QQNT/9.9.33/release/497e2f1f/QQ_9.9.33_260813_x64_01.exe"
QQ_EXPECT = 314581680

CHUNK = 1 << 20  # 1MB


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path, expect_size: int, label: str, tries: int = 6) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == expect_size:
        print(f"[跳过] {label} 已存在且大小正确: {dest}")
        return True

    for attempt in range(1, tries + 1):
        have = dest.stat().st_size if dest.exists() else 0
        if have > expect_size:
            print(f"[重置] {label} 本地文件超出预期大小，重新下载")
            dest.unlink()
            have = 0
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        if have:
            headers["Range"] = f"bytes={have}-"
            print(f"[续传] {label} 从 {human(have)} 继续 (第 {attempt}/{tries} 次)")
        else:
            print(f"[开始] {label} {human(expect_size)} (第 {attempt}/{tries} 次)")
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                mode = "ab" if have and resp.status == 206 else "wb"
                if mode == "wb":
                    have = 0
                done = have
                last_report = 0.0
                started = time.time()
                with dest.open(mode) as f:
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        f.write(block)
                        done += len(block)
                        now = time.time()
                        if now - last_report > 3 or done >= expect_size:
                            last_report = now
                            speed = done / max(now - started, 0.001)
                            pct = done / expect_size * 100
                            print(f"       {label} {pct:5.1f}%  {human(done)} / {human(expect_size)}  {human(speed)}/s",
                                  flush=True)
            size = dest.stat().st_size
            if size == expect_size:
                print(f"[完成] {label} {human(size)}")
                return True
            print(f"[警告] {label} 大小不符: 实际 {size} 期望 {expect_size}，准备重试")
        except (urllib.error.URLError, TimeoutError, OSError, ConnectionError) as e:
            print(f"[重试] {label} 第 {attempt} 次失败: {type(e).__name__}: {str(e)[:100]}")
            time.sleep(min(3 * attempt, 15))
    print(f"[失败] {label} 重试 {tries} 次仍未完成")
    return False


def main() -> int:
    print(f"目标目录: {TARGET_DIR}")
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    ok = True

    napcat_zip = TARGET_DIR / f"NapCat.Shell.{NAP_VER}.zip"
    if download(NAPCAT_URL, napcat_zip, NAPCAT_EXPECT, "NapCat.Shell.zip"):
        print(f"       sha256 = {sha256_of(napcat_zip)}")
    else:
        ok = False

    print()
    qq_exe = TARGET_DIR / "QQ_9.9.33_x64.exe"
    if download(QQ_URL, qq_exe, QQ_EXPECT, "QQ_9.9.33_x64.exe"):
        print(f"       sha256 = {sha256_of(qq_exe)}")
    else:
        ok = False

    print()
    print("=== 结果 ===")
    for f in (napcat_zip, qq_exe):
        if f.exists():
            print(f"  {f.name:34s} {human(f.stat().st_size)}")
        else:
            print(f"  {f.name:34s} 缺失")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
