"""T8 验证：跨进程（≈跨会话）长期记忆。

必须分两次运行，才是真的"换了个进程还记得"：

    python tools/session_probe.py 1     # 写入情感线并压缩久记忆，然后退出
    python tools/session_probe.py 2     # 全新进程读同一个库，问它还记得什么
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.memory import MemoryManager  # noqa: E402
from agent.store import Store  # noqa: E402

DB = ROOT / "data" / "session_probe.db"
PEER = 100000001

SCRIPT = [
    ("user", "小柚", "以后叫我小柚，不许叫全名"),
    ("assistant", "小喵", "哼，小柚就小柚。"),
    ("user", "小柚", "说好了周六一起去那家猫咖，你别放我鸽子"),
    ("assistant", "小喵", "本喵才不放鸽子。"),
    ("user", "小柚", "今天天气不错"),
    ("assistant", "小喵", "嗯。"),
    ("user", "小柚", "我晚上睡觉必须开着灯，别问为什么"),
    ("assistant", "小喵", "……灯给你留着。"),
    ("user", "小柚", "我在看剧"),
    ("assistant", "小喵", "看吧。"),
    ("user", "小柚", "我最喜欢橘子味的东西"),
    ("assistant", "小喵", "橘子味……记住了喵。"),
    ("user", "小柚", "今天喝了两杯咖啡"),
    ("assistant", "小喵", "少喝点。"),
    ("user", "小柚", "有件事下次再跟你说，现在还不是时候"),
    ("assistant", "小喵", "……那本喵等着。"),
    ("user", "小柚", "在吗"),
    ("assistant", "小喵", "在呢喵。"),
]

RECALL_KEYWORDS = ["小柚", "猫咖", "周六", "灯", "橘子", "下次"]


class CaptureOneBot:
    self_id = 100000002
    nickname = "小喵"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_segments(self, chat_type: str, chat_id: int, segments: list[dict]) -> dict:
        self.sent.append("".join(s["data"].get("text", "") for s in segments if s["type"] == "text"))
        return {"message_id": len(self.sent)}

    async def send_text(self, chat_type: str, chat_id: int, text: str) -> dict:
        return await self.send_segments(chat_type, chat_id, [{"type": "text", "data": {"text": text}}])


async def stage1() -> int:
    cfg = Config.load(project_root=ROOT)
    cfg["memory"].update({"keep_recent": 4, "max_summary_chars": 500})
    DB.unlink(missing_ok=True)
    store = Store(DB)
    llm = LLMClient(cfg["llm"])
    conv = store.get_or_create_conversation("private", PEER)
    for role, who, text in SCRIPT:
        store.add_message(conv, role, text, sender_name=who)

    mgr = MemoryManager(cfg["memory"], store, llm)
    wrote = await mgr.compress(conv, force=True)
    summary, _ = store.get_summary(conv)
    print("=" * 72)
    print("[阶段1] 已写入 %d 条对话并压缩成长期记忆" % len(SCRIPT))
    print("=" * 72)
    print(f"压缩成功: {wrote}，摘要 {len(summary)} 字：")
    for line in summary.split("\n"):
        if line.strip():
            print("   " + line)
    print()
    print("现在**退出这个进程**，由另一个全新进程来测验它是否还记得。")
    store.close()
    return 0 if wrote else 1


async def stage2() -> int:
    cfg = Config.load(project_root=ROOT)
    cfg["behaviour"]["active_hours"] = ""
    store = Store(DB)
    llm = LLMClient(cfg["llm"])
    onebot = CaptureOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    print("=" * 72)
    print("[阶段2] 全新进程，读同一个数据库")
    print("=" * 72)
    summary, upto = store.get_summary(store.get_or_create_conversation("private", PEER))
    print(f"从库里读出的长期记忆（{len(summary)} 字，截止 {upto} 号消息）")
    if not summary:
        print("[x] 没读到长期记忆——跨会话记忆没有落盘")
        store.close()
        return 1

    question = "你还记得我之前跟你说过的事吗？"
    batch = [{
        "segments": [{"type": "text", "data": {"text": question}}],
        "text": question, "sender_id": PEER, "sender_name": "小柚",
        "message_id": 999, "ts": 1759000000,
    }]
    conv_id, ids = agent._store_batch("private", PEER, batch)
    messages, _ = await agent._build_messages("private", PEER, batch, ids)
    user_block = messages[-1]["content"]
    print(f"问: {question}")
    print(f"（上下文里已注入【长期记忆】: {'【长期记忆】' in user_block}）")

    reply = await llm.chat(messages)
    print(f"答: {reply}")
    print()

    hits = [k for k in RECALL_KEYWORDS if k in reply]
    print("-" * 72)
    print(f"答案里复现的专属信息: {len(hits)}/{len(RECALL_KEYWORDS)} -> {hits}")
    print(f"未复现: {[k for k in RECALL_KEYWORDS if k not in reply]}")
    ok = len(hits) >= 2
    print(f"结论: {'通过' if ok else '未通过'}（要求复现 >=2 项专属信息）")

    await agent.shutdown()
    store.close()
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("1", "2"):
        print(__doc__)
        return 2
    return asyncio.run(stage1() if sys.argv[1] == "1" else stage2())


if __name__ == "__main__":
    raise SystemExit(main())
