"""撤回指定的消息。

    python tools/recall.py 123456 123457 123458
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.config import Config  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402


async def main() -> int:
    ids = [int(a) for a in sys.argv[1:] if a.isdigit()]
    if not ids:
        print(__doc__)
        return 2
    cfg = Config.load(project_root=ROOT)
    client = OneBotClient(cfg["onebot"]["ws_url"], cfg["onebot"].get("access_token", ""), 1, 2)

    async def noop(event):
        return None

    await client.start(noop)
    try:
        await asyncio.wait_for(client.ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("[x] OneBot 未就绪")
        await client.stop()
        return 1

    failed = 0
    for mid in ids:
        try:
            await client.call("delete_msg", {"message_id": mid}, timeout=15)
            print(f"[ok] 已撤回 {mid}")
        except Exception as e:
            failed += 1
            print(f"[x] 撤回 {mid} 失败: {e}")
    await client.stop()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
