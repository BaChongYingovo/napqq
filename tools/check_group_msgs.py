"""核实最近发到某个群的消息（只看机器人自己发的），并打印 message_id 以便撤回。

    python tools/check_group_msgs.py 999999999
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import message as msg  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    group_id = int(sys.argv[1])
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

    print(f"[i] 机器人 QQ {client.self_id}，群 {group_id}")
    data = None
    for action, params in (
        ("get_group_msg_history", {"group_id": group_id, "count": 20}),
        ("get_group_msg_history", {"group_id": group_id, "message_seq": 0, "count": 20}),
    ):
        try:
            data = await client.call(action, params, timeout=15)
            if data:
                break
        except Exception as e:
            print(f"[!] {action} 失败: {e}")

    if not data:
        print("[x] 拿不到群消息历史（NapCat 版本可能不支持该接口）")
        await client.stop()
        return 1

    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        print(f"[x] 返回结构不认识: {str(data)[:200]}")
        await client.stop()
        return 1

    print(f"[i] 拿到 {len(messages)} 条历史消息，下面只列机器人自己发的：")
    found = []
    raw_wanted = "--raw" in sys.argv
    for m in messages:
        sender = m.get("sender") or {}
        if int(sender.get("user_id") or m.get("user_id") or 0) != int(client.self_id):
            continue
        text = msg.render_segments(msg.normalize_message(m.get("message")))
        ts = int(m.get("time") or 0)
        clock = time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "?"
        found.append((int(m.get("message_id") or 0), clock, text))
        if raw_wanted:
            segs = msg.normalize_message(m.get("message"))
            ats = [s["data"].get("qq") for s in segs if s.get("type") == "at"]
            print(f"  raw: id={found[-1][0]} 段类型={[s['type'] for s in segs]}"
                  f"{'  真 at -> ' + str(ats) if ats else '  无 at 段（是纯文本）'}")
    for mid, clock, text in found:
        print(f"  message_id={mid:<12} [{clock}] {len(text):>4} 字  {text[:46]}...")

    if not found:
        print("  （没找到机器人发的消息）")
    else:
        ids = " ".join(str(mid) for mid, _, _ in found)
        print()
        print(f"[i] 想撤回的话：python tools/recall.py {ids}")

    await client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
