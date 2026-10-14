"""T7 验证：记忆压缩是否真的"情感类优先"。

造一段混着**情感线/专属互动**和**流水账**的对话，用真实模型压缩一次，
检查摘要留住了什么、丢掉了什么。

    python tools/memory_probe.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.memory import MemoryManager  # noqa: E402
from agent.store import Store  # noqa: E402

# (角色, 说话人, 内容)  标记 [K] 的必须留住，[F] 的是流水账、允许被淡忘
SCRIPT: list[tuple[str, str, str]] = [
    ("user", "小柚", "以后叫我小柚，不许叫全名"),
    ("assistant", "小喵", "哼，小柚就小柚。……谁要叫你叫得那么亲"),
    ("user", "小柚", "今天天气不错"),
    ("assistant", "小喵", "嗯，喵。"),
    ("user", "小柚", "说好了周六一起去那家猫咖，你别放我鸽子"),
    ("assistant", "小喵", "本喵才不放鸽子，是你别迟到。"),
    ("user", "小柚", "我刚吃了碗米饭"),
    ("assistant", "小喵", "哦。"),
    ("user", "小柚", "我晚上睡觉必须开着灯，别问为什么"),
    ("assistant", "小喵", "……知道了。灯给你留着。"),
    ("user", "小柚", "外面下雨了"),
    ("assistant", "小喵", "带伞。"),
    ("user", "小柚", "我最讨厌别人敷衍我，说'哦''嗯'的时候我特别烦"),
    ("assistant", "小喵", "那本喵以后好好说话。"),
    ("user", "小柚", "我在看剧"),
    ("assistant", "小喵", "看吧。"),
    ("user", "小柚", "我最喜欢橘子味的东西，糖、汽水、护手霜都是橘子味"),
    ("assistant", "小喵", "橘子味……记住了喵。"),
    ("user", "小柚", "今天喝了两杯咖啡"),
    ("assistant", "小喵", "少喝点。"),
    ("user", "小柚", "有件事下次再跟你说，现在还不是时候"),
    ("assistant", "小喵", "……那本喵等着。"),
    ("user", "小柚", "我躺在沙发上"),
    ("assistant", "小喵", "懒猫。"),
    ("user", "小柚", "明天不用上班"),
    ("assistant", "小喵", "那早点睡。"),
    # 以下是 keep_recent 保留区，不参与压缩（模拟"最近还在聊"）
    ("user", "小柚", "在吗"),
    ("assistant", "小喵", "在呢喵。"),
    ("user", "小柚", "陪我聊会儿"),
    ("assistant", "小喵", "……就一会儿哦。"),
]

KEEP_KEYWORDS = ["小柚", "猫咖", "周六", "灯", "橘子", "敷衍", "下次"]
DROP_KEYWORDS = ["米饭", "下雨", "两杯咖啡", "沙发", "看剧"]


async def main() -> int:
    cfg = Config.load(project_root=ROOT)
    # 用小 chunk 强制把情感线也压进去；否则前面 24 条不够触发
    cfg["memory"].update({"keep_recent": 6, "max_summary_chars": 500})

    db = ROOT / "data" / "memory_probe.db"
    db.unlink(missing_ok=True)
    store = Store(db)
    llm = LLMClient(cfg["llm"])
    conv = store.get_or_create_conversation("private", 100000001)
    for role, who, text in SCRIPT:
        store.add_message(conv, role, text, sender_name=who)

    mgr = MemoryManager(cfg["memory"], store, llm)
    print("=" * 72)
    print("T7 记忆权重验证：造 %d 条对话（含情感线 + 流水账），压缩后看留住了什么" % len(SCRIPT))
    print("=" * 72)

    wrote = await mgr.compress(conv, force=True)
    summary, upto = store.get_summary(conv)
    if not wrote or not summary:
        print("[x] 压缩没有产出摘要")
        store.close(); db.unlink(missing_ok=True)
        return 1

    print(f"[i] 压缩至 {upto} 号消息，摘要 {len(summary)} 字：")
    print()
    for line in summary.split("\n"):
        print("   " + line)
    print()

    kept = [k for k in KEEP_KEYWORDS if k in summary]
    dropped = [k for k in DROP_KEYWORDS if k in summary]
    print("-" * 72)
    print(f"情感/专属互动关键词留住了: {len(kept)}/{len(KEEP_KEYWORDS)}  -> {kept}")
    print(f"未留住的: {[k for k in KEEP_KEYWORDS if k not in summary]}")
    print(f"流水账被写入摘要: {len(dropped)} 处  -> {dropped if dropped else '无'}")
    ok = len(kept) >= 5 and len(dropped) <= 1
    print(f"结论: {'通过' if ok else '未通过'}（要求：情感线 >=5/7 且 流水账 <=1）")
    print(f"     {('[x] 漏掉的: ' + str([k for k in KEEP_KEYWORDS if k not in summary])) if len(kept) < 5 else ''}")

    store.close()
    db.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
