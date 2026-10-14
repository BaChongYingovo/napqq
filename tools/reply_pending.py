"""补回"机器人停机期间漏掉的消息"，并用真实人设生成回复发出去。

**为什么需要它**：机器人被关闭的那几分钟里，NapCat 收到了对方的消息，
但 agent 不在线，那些消息既没进库也没被回复。重启后它们不会自动补上
（OneBot 不会重放历史事件）。

用法：
    python tools/reply_pending.py --private 100000004 --messages "我去吃饭了" "下大雨" "我淋雨淋回来了"
    python tools/reply_pending.py --private 100000004 --dry --messages "在吗"

它走的是**和线上完全一样的代码路径**（真实人设、真实记忆、真实模型），
所以发出的语气与自动回复一致。不修改任何配置。
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
from agent.store import Store  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description="补发漏掉的消息并回复")
    ap.add_argument("--private", type=int, required=True, help="对方 QQ")
    ap.add_argument("--messages", nargs="+", required=True, help="漏掉的消息，按时间顺序")
    ap.add_argument("--dry", action="store_true", help="只生成不发送")
    args = ap.parse_args()

    cfg = Config.load(project_root=ROOT)
    problems = cfg.validate()
    if problems:
        print("配置有问题:", problems)
        return 2

    store = Store(cfg.resolve(str(cfg["storage"]["db_path"])))
    llm = LLMClient(cfg["llm"])
    onebot = OneBotClient(cfg["onebot"]["ws_url"], cfg["onebot"].get("access_token", ""), 1, 2)
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    async def noop(event):  # 本脚本只发不收
        return None

    await onebot.start(noop)
    try:
        await asyncio.wait_for(onebot.ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("[x] OneBot 没连上（NapCat 在跑吗？）")
        await onebot.stop()
        store.close()
        return 1
    print(f"[ok] 已连接，机器人 QQ {onebot.self_id}")
    print(f"[i] 白名单: {cfg['behaviour'].get('private_whitelist')}")
    print(f"[i] 人设: {cfg['persona']['name']}")
    print()

    # 把漏掉的消息补进会话（模拟它们正常到达的样子）
    conv = store.get_or_create_conversation("private", args.private)
    base_ts = int(time.time()) - len(args.messages) * 30
    batch = []
    for i, text in enumerate(args.messages):
        batch.append({
            "segments": [{"type": "text", "data": {"text": text}}],
            "text": text,
            "sender_id": args.private,
            "sender_name": str(args.private),
            "message_id": 900000 + i,
            "ts": base_ts + i * 30,
        })
    print("待回复的消息：")
    for m in batch:
        print(f"   她 | {m['text']}")
    print()

    if args.dry:
        print("[dry] 不发送、不入库")
        await onebot.stop()
        store.close()
        return 0

    # 注意：不要在这里手动存消息——_reply 内部已经把 batch 存进会话了
    # （agent/bot.py 的 add_message）。手动再存一次会导致对方每条消息重复出现。
    await agent._reply(("private", args.private), batch)
    after = store.recent_messages(conv, 20)
    sent = [m["content"] for m in after if m["role"] == "assistant"]
    print(f"[ok] 已回复（库里消息 {before} -> 增加）")
    if sent:
        print(f"    发出: {sent[-1]}")

    await agent.shutdown()
    await onebot.stop()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
