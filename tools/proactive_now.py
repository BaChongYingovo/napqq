"""立刻让机器人主动开口一次（测试用）。

平时主动消息受三道闸限制（静默时长 / 概率 / 免打扰）。这个工具**临时绕过那三道闸**，
但**保留 SKIP 判断**——模型觉得没话可说就不发，不会硬凑一句废话。

    python tools/proactive_now.py --target 100000003
    python tools/proactive_now.py --target 100000003 --dry     # 只看会说什么，不发

走的是和线上完全一样的生成路径（真实人设、真实记忆、真实模型），
所以语气和自动主动消息一致。**不改磁盘上的配置。**
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402
from agent.proactive import SKIP_MARK, ProactiveMessenger  # noqa: E402
from agent.store import Store  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="立刻主动开口一次")
    ap.add_argument("--target", type=int, required=True, help="对谁主动开口")
    ap.add_argument("--dry", action="store_true", help="只生成不发送")
    ap.add_argument("--times", type=int, default=1, help="生成几个候选（dry 时有用）")
    ap.add_argument("--text", default="", help="直接发这句（走正规路径、会入库），不再让模型生成")
    args = ap.parse_args()

    cfg = Config.load(project_root=ROOT)
    store = Store(cfg.resolve(str(cfg["storage"]["db_path"])))
    llm = LLMClient(cfg["llm"])
    onebot = OneBotClient(cfg["onebot"]["ws_url"], cfg["onebot"].get("access_token", ""), 1, 2)
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    # 临时放开三道闸（只改内存，不写磁盘）
    pr = cfg["behaviour"].setdefault("proactive", {})
    pr["enabled"] = True
    pr["targets"] = [args.target]
    pr["min_silence_minutes"] = 0
    pr["quiet_hours"] = ""
    pr["chance"] = 1.0
    pr["max_per_day"] = 99

    async def noop(event):  # 只发不收
        return None

    await onebot.start(noop)
    try:
        await asyncio.wait_for(onebot.ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("[x] OneBot 没连上（NapCat 在跑吗？）")
        await onebot.stop()
        store.close()
        return 1

    print(f"[ok] 机器人 QQ {onebot.self_id} | 人设 {cfg['persona']['name']} | 目标 {args.target}")

    # --text 分支必须在 --dry 之前：否则 --dry --text 会走到"生成候选"那条路去
    if args.text:
        from agent import message as msg

        style = cfg["persona"]["typing_style"]
        pieces = msg.split_reply(
            args.text, int(style.get("max_chars_per_message", 30) or 30), True,
            int(style.get("max_messages_per_reply", 3) or 3),
        )
        print(f"[i] 指定内容（会切成 {len(pieces)} 条）:")
        for p in pieces:
            print(f"    [{len(p):>2} 字] {p}")
        if args.dry:
            print("[dry] 不发送、不入库")
            await onebot.stop()
            store.close()
            return 0
        conv = store.get_or_create_conversation("private", args.target)
        await agent._send_reply("private", args.target, conv, args.text)
        print("[ok] 已发送（已入库）")
        await agent.shutdown()
        await onebot.stop()
        store.close()
        return 0

    if args.dry:
        pm = ProactiveMessenger(agent)
        conv = store.get_or_create_conversation("private", args.target)
        for i in range(max(1, args.times)):
            text = await pm._compose(args.target, conv)
            if not text or SKIP_MARK in text.upper():
                print(f"  [{i+1}] SKIP —— 它觉得现在没什么可说")
            else:
                first = text.split("\n")[0].strip()
                print(f"  [{i+1}] {first}   ({len(first)} 字)")
        await onebot.stop()
        store.close()
        return 0

    pm = ProactiveMessenger(agent)
    if False:
        pass
    else:
        sent = await pm.tick()
        if not sent:
            print("  它选择了不发（SKIP 或条件不满足）——这是设计行为，不是故障")
    await agent.shutdown()
    await onebot.stop()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
