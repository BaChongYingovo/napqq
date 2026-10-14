"""独立探针：连真实 NapCat 的 OneBot v11 WebSocket，验证握手/echo/get_login_info。

不涉及大模型，不需要 API key。
    python tools/probe_onebot.py [ws_url]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.onebot import OneBotClient  # noqa: E402


async def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:3001"
    events: list[str] = []
    client = OneBotClient(url, "", reconnect_min=1, reconnect_max=2)

    async def handler(event):
        events.append(str(event.get("post_type")))

    print(f"连接 {url} ...")
    await client.start(handler)
    try:
        await asyncio.wait_for(client.ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("[x] 15 秒内未就绪")
        await client.stop()
        return 1

    print(f"[ok] 握手成功，self_id = {client.self_id}，昵称 = {client.nickname}")

    for action in ("get_login_info", "get_status", "get_version_info"):
        try:
            data = await client.call(action, timeout=10)
            print(f"[ok] {action} -> {data}")
        except Exception as e:
            print(f"[!] {action} 失败: {type(e).__name__}: {e}")

    try:
        friends = await client.call("get_friend_list", timeout=15)
        if isinstance(friends, list):
            print(f"[ok] get_friend_list -> {len(friends)} 个好友")
    except Exception as e:
        print(f"[!] get_friend_list 失败: {e}")

    try:
        groups = await client.call("get_group_list", timeout=15)
        if isinstance(groups, list):
            names = [str(g.get("group_name")) for g in groups[:5]]
            print(f"[ok] get_group_list -> {len(groups)} 个群: {names}")
    except Exception as e:
        print(f"[!] get_group_list 失败: {e}")

    await asyncio.sleep(1.0)
    print(f"[i] 期间收到的非响应事件: {events[:10]}")
    await client.stop()
    print("[ok] 连接已正常关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
