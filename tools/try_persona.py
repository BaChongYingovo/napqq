"""直接试人设：用真实 Agent 的上下文组装逻辑 + 真实模型跑一次，不碰 QQ。

    python tools/try_persona.py "用喵语背一下出师表"
    python tools/try_persona.py "在吗" --who 100000004

会打印：
  1) 实际发给模型的完整提示词（前若干行）
  2) 模型的原始回复
  3) 这条回复会被切成几条 QQ 消息发出去（含每条的字符数）

用途：调人设/调参数时不用真发 QQ 消息，也不怕把话说错到群里。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import message as msg  # noqa: E402
from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.store import Store  # noqa: E402

TRY_DB = ROOT / "data" / "try_persona.db"


class NullOneBot:
    """不发任何东西，只记录。"""

    self_id = 100000002
    nickname = "小助"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_segments(self, chat_type: str, chat_id: int, segments: list[dict]) -> dict:
        text = "".join(s["data"].get("text", "") for s in segments if s["type"] == "text")
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def send_text(self, chat_type: str, chat_id: int, text: str) -> dict:
        return await self.send_segments(chat_type, chat_id, [{"type": "text", "data": {"text": text}}])


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    text = args[0]
    who = 100000001
    if "--who" in sys.argv:
        who = int(sys.argv[sys.argv.index("--who") + 1])

    cfg = Config.load(project_root=ROOT)
    problems = cfg.validate()
    if problems:
        print("配置有问题:", problems)
        return 2

    TRY_DB.unlink(missing_ok=True)
    store = Store(TRY_DB)
    llm = LLMClient(cfg["llm"])
    agent = Agent(cfg, store, llm, NullOneBot())  # type: ignore[arg-type]

    batch = [{
        "segments": [{"type": "text", "data": {"text": text}}],
        "text": text,
        "sender_id": who,
        "sender_name": "对方",
        "message_id": 1,
        "ts": 1759000000,
    }]
    conv_id, ids = agent._store_batch("private", who, batch)
    messages, used_images = await agent._build_messages("private", who, batch, ids)

    print("=" * 70)
    print("发给模型的 system（截断显示前 60 行）")
    print("=" * 70)
    system = messages[0]["content"]
    for line in system.split("\n")[:60]:
        print("  " + line)
    print(f"  ...（system 共 {len(system)} 字）")

    print()
    print("=" * 70)
    print(f"用户消息: {text}    （会话 private/{who}，对方身份={'已登记' if agent.contacts.get(str(who)) else '未登记'}）")
    print("=" * 70)
    content = messages[-1]["content"]
    user_text = content if isinstance(content, str) else "(多模态)"
    print("  " + user_text.replace("\n", "\n  ")[:1200])

    print()
    print("=" * 70)
    print("调用真实模型 ...")
    print("=" * 70)
    import time
    started = time.time()
    try:
        reply = await llm.chat(messages)
    except Exception as e:
        print(f"[x] 模型调用失败: {type(e).__name__}: {e}")
        store.close()
        TRY_DB.unlink(missing_ok=True)
        return 1
    elapsed = time.time() - started

    print(f"[ok] {elapsed:.1f}s，{len(reply)} 字")
    print()
    print("-" * 70)
    print(reply)
    print("-" * 70)

    style = cfg["persona"].get("typing_style") or {}
    limit = int(style.get("max_chars_per_message", 180) or 180)
    threshold = int(style.get("long_reply_threshold", 0) or 0)
    switched = threshold > 0 and len(reply) > threshold
    if switched:
        limit = int(style.get("long_reply_max_chars_per_message", limit) or limit)
    pieces = msg.split_reply(reply, limit, bool(style.get("split_on_newline", True)),
                             int(style.get("max_messages_per_reply", 0) or 0))

    print()
    print("=" * 70)
    print(f"实际发出方式：{len(pieces)} 条（单条上限 {limit} 字"
          f"{'，已启用长回复模式' if switched else ''}）")
    print("=" * 70)
    for i, piece in enumerate(pieces, 1):
        preview = piece.replace("\n", " ")
        print(f"  第{i}条 ({len(piece):>4} 字): {preview[:70]}{'...' if len(preview) > 70 else ''}")

    store.close()
    TRY_DB.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
