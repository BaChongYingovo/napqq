"""验证人设是否真的复现了风格档案里的硬指标。

对照基准来自 qq-style/风格档案-八重櫻ovo.txt 的「主私聊（川渝奶龙）」一档：
    单条字数中位数 4 字 · ≤4 字的消息 53.6% · 表情码 8.2% · 连续发言串 3.68 条/串
    ≤15 字 96.2% · 含标点 7.3%

用法：
    python tools/style_probe.py            # 跑全部
    python tools/style_probe.py --dry      # 只看配置，不调模型
"""
from __future__ import annotations

import asyncio
import statistics
import time
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import emoji, message as msg  # noqa: E402
from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.store import Store  # noqa: E402

PARTNER = 100000004

# 她（川渝奶龙）会说的话 —— 取自档案里描述的场景类型
INPUTS = [
    "你在干嘛呀",
    "我今天好累",
    "切 不理你了",
    "你今晚几点睡",
    "我明天要早起",
    "你想我了吗",
    "我今天吃了火锅/呲牙",
    "刚刚被老师骂了 好烦",
]

PUNCT = set("。，、；：？！,.;:?!")


class CaptureOneBot:
    """只记录，不真发。接口与 agent/onebot.py 的 OneBot 保持一致。"""

    def __init__(self) -> None:
        self.self_id = 100000002
        self.nickname = "探针"
        self.raw: list[list[dict]] = []

    async def send_segments(self, chat_type: str, chat_id: int, segments: list[dict]) -> dict:
        self.raw.append(list(segments))
        return {"message_id": len(self.raw)}

    async def send_text(self, chat_type: str, chat_id: int, text: str) -> dict:
        return await self.send_segments(chat_type, chat_id, [{"type": "text", "data": {"text": text}}])

    async def get_image(self, file: str):
        return None

    async def call(self, action: str, params: dict | None = None, timeout: float = 20.0) -> Any:
        return {"status": "ok", "retcode": 0, "data": {}}


def render(segs: list[dict]) -> str:
    out = []
    for s in segs:
        if s["type"] == "text":
            out.append(s["data"]["text"])
        elif s["type"] == "face":
            out.append(f"[表情:{emoji.face_name(s['data']['id']) or s['data']['id']}]")
        elif s["type"] == "at":
            out.append(f"@{s['data']['qq']}")
    return "".join(out)


async def main() -> int:
    dry = "--dry" in sys.argv
    cfg = Config.load(project_root=ROOT)
    ts = cfg["persona"]["typing_style"]

    print("=" * 74)
    print("风格验证：八重櫻ovo × 川渝奶龙")
    print("=" * 74)
    print(f"  人设      : {cfg['persona']['name']}（{len(cfg['persona']['system_prompt'])} 字）")
    print(f"  单条上限  : {ts['max_chars_per_message']} 字   每题最多切 {ts['max_messages_per_reply']} 条")
    print(f"  私聊白名单: {cfg['behaviour']['private_whitelist']}")
    print(f"  群聊策略  : {cfg['behaviour']['group_reply_mode']}")
    if dry:
        return 0

    store = Store(cfg["storage"]["db_path"])
    llm = LLMClient(cfg["llm"])
    onebot = CaptureOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    per_msg: list[str] = []
    face_msgs = 0
    punct_msgs = 0
    bursts: list[int] = []
    replies: list[list[str]] = []

    for i, text in enumerate(INPUTS, 1):
        conv = store.get_or_create_conversation("private", PARTNER)
        onebot.raw.clear()
        batch = [{
            "segments": msg.parse(f"[CQ:face,id=14]{text}") if text.startswith("[") else
                        [{"type": "text", "data": {"text": text}}],
            "text": text, "sender_id": PARTNER, "sender_name": "川渝奶龙",
            "message_id": 1000 + i, "ts": int(time.time()) - 120,
        }]
        print(f"\n[{i:2}] 她: {text}")
        await agent._reply(("private", PARTNER), batch)

        pieces = [render(s) for s in onebot.raw]
        pieces = [p for p in pieces if p]
        replies.append(pieces)
        bursts.append(len(pieces))
        for p in pieces:
            per_msg.append(p)
            if "/" in p or "[表情:" in p:
                face_msgs += 1
            if any(ch in PUNCT for ch in p):
                punct_msgs += 1
            print(f"       {p}")

    lens = [len(p) for p in per_msg]
    n = len(per_msg) or 1
    med = statistics.median(lens) if lens else 0
    short4 = sum(1 for L in lens if L <= 4) / n
    short15 = sum(1 for L in lens if L <= 15) / n

    print()
    print("-" * 74)
    print(f"{'指标':<16}{'档案基准':>12}{'实测':>12}{'判定':>8}")
    rows = [
        ("单条字数中位数", "4 字", f"{med:.0f} 字", med <= 8),
        ("≤4 字占比", "53.6%", f"{short4:.1%}", short4 >= 0.25),
        ("≤15 字占比", "96.2%", f"{short15:.1%}", short15 >= 0.85),
        ("带标点占比", "7.3%", f"{punct_msgs / n:.1%}", punct_msgs / n <= 0.30),
        ("表情码占比", "8.2%", f"{face_msgs / n:.1%}", face_msgs >= 1),
        ("回复条数/轮", "1~2 条", f"{statistics.mean(bursts):.2f} 条", statistics.mean(bursts) <= 2.0),
        ("单轮最多条数", "≤2", f"{max(bursts)} 条", max(bursts) <= 2),
    ]
    ok = True
    for name, base, got, passed in rows:
        ok = ok and passed
        print(f"{name:<16}{base:>12}{got:>12}{'  通过' if passed else '  未过':>8}")
    print("-" * 74)
    print(f"结论: {'通过' if ok else '未通过'}   （共 {len(per_msg)} 条消息 / {len(INPUTS)} 轮）")

    # ---- 主动消息预演：看它会说什么，还是会识趣地 SKIP ----
    from agent.proactive import ProactiveMessenger
    print()
    print("=" * 74)
    print("主动消息预演（对方没发消息时，她会收到什么）")
    print("=" * 74)
    pm = ProactiveMessenger(agent)
    conv = store.get_or_create_conversation("private", PARTNER)
    spoken = 0
    for i in range(4):
        text = await pm._compose(PARTNER, conv)
        if not text or "SKIP" in text.upper():
            print(f"  [{i + 1}] SKIP（识趣地不发）")
        else:
            spoken += 1
            first = text.split("\n")[0].strip()
            print(f"  [{i + 1}] {first}   ({len(first)} 字)")
    print(f"  -> 4 次里说了 {spoken} 次，跳过 {4 - spoken} 次")

    await agent.shutdown()
    store.close()

    out = ROOT / "out" / "style_probe.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(f"她: {i}\n   " + "\n   ".join(r) for i, r in zip(INPUTS, replies)),
                   encoding="utf-8")
    print(f"存档: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
