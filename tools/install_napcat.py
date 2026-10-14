"""解压 NapCat.Shell 到 <NAPCAT_DIR>，并预写 OneBot v11 WebSocket 服务端配置。

对应 qq-agent 的 config.json: onebot.ws_url = ws://127.0.0.1:3001, access_token = ""
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

ZIP = Path(r"<NAPCAT_DIR>\downloads\NapCat.Shell.v4.18.33.zip")
DEST = Path(r"<NAPCAT_DIR>")

ONEBOT_CONFIG = {
    "network": {
        "httpServers": [],
        "httpClients": [],
        "websocketServers": [
            {
                "name": "qq-agent-ws",
                "enable": True,
                "host": "127.0.0.1",
                "port": 3001,
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "token": "",
                "enableForcePushEvent": True,
                "debug": False,
                "heartInterval": 30000,
            }
        ],
        "websocketClients": [],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
}


def main() -> int:
    if not ZIP.exists():
        print(f"[x] 找不到 {ZIP}")
        return 1
    if DEST.exists():
        print(f"[i] {DEST} 已存在，先清空重建")
        shutil.rmtree(DEST, ignore_errors=True)
    DEST.mkdir(parents=True, exist_ok=True)

    print(f"[1] 解压 {ZIP.name} -> {DEST}")
    with zipfile.ZipFile(ZIP) as z:
        z.extractall(DEST)
    total = sum(1 for _ in DEST.rglob("*") if _.is_file())
    print(f"    完成，共 {total} 个文件")

    print("[2] 写入 OneBot 配置 config/onebot11.json")
    cfg_dir = DEST / "config"
    cfg_dir.mkdir(exist_ok=True)
    target = cfg_dir / "onebot11.json"
    if target.exists():
        backup = cfg_dir / "onebot11.json.bak"
        shutil.copy2(target, backup)
        print(f"    已备份原配置到 {backup.name}")
    target.write_text(json.dumps(ONEBOT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    WS 服务端: 127.0.0.1:3001  格式=array  token=空（仅本机可连）")

    print("[3] 校验关键文件")
    for name in ("launcher.bat", "launcher-win10.bat", "NapCatWinBootMain.exe", "napcat.mjs",
                 "NapCatWinBootHook.dll", "package.json", "KillQQ.bat"):
        p = DEST / name
        print(f"    {'[ok]  ' if p.exists() else '[缺失]'} {name:26s} {p.stat().st_size if p.exists() else 0}")

    print("[4] config 目录内容")
    for p in sorted(cfg_dir.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(DEST)}  ({p.stat().st_size} B)")

    print("[5] launcher.bat 内容")
    lb = DEST / "launcher.bat"
    if lb.exists():
        print("    " + "-" * 60)
        for line in lb.read_text(encoding="utf-8", errors="replace").splitlines():
            print("    " + line)
        print("    " + "-" * 60)

    print("[6] package.json 版本")
    pj = DEST / "package.json"
    if pj.exists():
        data = json.loads(pj.read_text(encoding="utf-8"))
        print(f"    name={data.get('name')} version={data.get('version')} main={data.get('main')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
