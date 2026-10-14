"""主动发消息。

**为什么必须有这个**：只会「被动回话」是最大的机器感来源——真人会自己找话说。
一个永远秒回、且只在被叫时才出声的账号，几天就会被看穿。

**设计原则（防刷屏是第一优先）**：
主动发消息是最容易变成骚扰的功能，所以硬限制写在代码里，不交给模型判断：

  1. 只对白名单里的私聊对象主动，群里永不主动
  2. 必须在活跃时段内、且不在免打扰时段（默认 01:00-09:00）
  3. 每天有次数上限（默认 4 次）
  4. 距上次任何人说话要静默够久（默认 45 分钟）
  5. 到点了还要过一道概率闸（默认 50%）——避免"每小时准点问候"这种机械规律
  6. **模型自己也要能说不**：提示词要求它想不出自然的话就输出 SKIP

第 6 条是关键：宁可这次不发，也不要发一句"在干嘛呀"这种没话找话的废话。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from . import message as msg

log = logging.getLogger("qqagent.proactive")

SKIP_MARK = "SKIP"

PROACTIVE_SYSTEM = """现在**对方没有发新消息**，是你要主动说一句话。

**这不是追问，是开一个新话头。** 不要接着刚才那个话题催对方回答
（刚在争论某件事，就别再发「人呢」「你答不上来」那种），那叫催债，不叫开口。

**你们上一轮争的那件事已经说够了——这次别提它。** 说点别的：你自己的事、你想起的人、
你正在算的账、一个你真心想知道的怪问题。让人觉得「这个人忽然想到我，想说点别的」，
而不是「他还在追着要我回答」。

按下面的顺序判断，想不出自然的就输出 `SKIP`（三个字母，别的什么都不要写）：

1. 看现在几点。凌晨她大概率睡了就别发；早上可以问问起没起；晚上可以聊两句。
2. **先想一件你自己的事**：你刚在干什么、看到什么、想起什么。
   主动开口最有意思的方式是**突然说一句你自己的事**，等对方来接。
3. 或者拉对方入伙：给他一个角色、一个任务、一个要算的账。
4. 或者抛一个你真的想知道的怪问题。
5. 最后才考虑接上一次没说完的话——而且**别催**，用陈述句带一下就行。

要求：
- 只输出 **1 条**，**至少 8 个字**，不要标点，不要解释你在做什么。
- **一两个字的催问一律不算**（「人呢」「在吗」「还在吗」直接重写）。
- 不要问候式开场（「在干嘛」「睡了吗」）。
- 不要复述记忆或总结你们聊过什么。
- 想不出自然的话，就老老实实输出 SKIP。
"""

# 主动开口时不该出现的开场（催问式，最没意思）
BANNED_OPENERS = ("人呢", "在吗", "还在吗", "睡了吗", "喂", "你说话", "干嘛呢", "在干嘛")


# 主动开口的"角度"。每次随机抽一个，强制换话头。
# 为什么必须强制：实测只写"别提刚才那件事"，模型还是被上一轮对话黏住，
# 连着生成「你那个上帝之国…」「你答不上…」。指定一个具体角度才真的换得过去。
ANGLES = [
    "用「突然想起一件你自己的事」开口——一个具体画面（地名、天气、吃的、身上的疼），"
    "说完就停，不解释，等对方来接",
    "用「拉他入伙」开口——给他一个角色，或者一件要一起算的账",
    "用「抛一个你真心想知道的怪问题」开口——不是考他，是你真的想知道人怎么想",
    "用「下一个小命令」开口——给他一件今天就能做的事，你之后会追问结果",
    "用「说一句你现在的状态」开口——你刚在干什么、睡没睡、屋里冷不冷、在想什么",
    "用「想起某个人或某个地方」开口——不解释为什么突然想起来",
]


def is_weak_opener(text: str, min_chars: int = 6) -> bool:
    """判断这条主动消息是不是"没话找话"。

    实测：不给硬门槛的话，模型会一直发「人呢」「那三个数呢」——它被上一轮对话黏住了，
    发出来的全是催问。宁可这次不发，也不要发这种。
    """
    t = (text or "").strip()
    if len(t) < min_chars:
        return True
    return any(t.startswith(b) for b in BANNED_OPENERS)


class ProactiveMessenger:
    """定期检查是否该主动说句话。"""

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.cfg = agent.cfg
        self.store = agent.store
        self.enabled = False
        self._sent_today: dict[str, int] = {}
        self._day = ""
        self._task: asyncio.Task | None = None
        self.sent_count = 0
        self.skipped_count = 0

    # ---------- 配置 ----------

    @property
    def conf(self) -> dict[str, Any]:
        return self.cfg["behaviour"].get("proactive") or {}

    def _targets(self) -> list[int]:
        raw = self.conf.get("targets")
        if raw:
            return [int(x) for x in raw]
        # 没配就跟随私聊白名单——语义一致，少一个要维护的地方
        return [int(x) for x in (self.cfg["behaviour"].get("private_whitelist") or [])]

    # ---------- 硬限制 ----------

    def _daily_left(self, key: str) -> int:
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._sent_today = {}
        return int(self.conf.get("max_per_day", 4) or 0) - self._sent_today.get(key, 0)

    @staticmethod
    def _in_quiet_hours(spec: str, now: Any = None) -> bool:
        """免打扰时段。复用 schedule 的窗口解析（支持跨零点）。"""
        if not spec:
            return False
        from .schedule import BadWindow, in_window, parse_window
        try:
            window = parse_window(spec)
        except BadWindow:
            return False
        if window is None:
            return False
        return in_window(window, now)

    def should_send_now(self, chat_id: int, last_ts: int, last_role: str) -> tuple[bool, str]:
        """返回 (是否该发, 原因)。原因用于日志，便于排查"为什么没发"。"""
        c = self.conf
        if not c.get("enabled", False):
            return False, "未启用"
        if chat_id not in self._targets():
            return False, "不在主动对象名单里"
        if not self.agent.in_active_hours:
            return False, "不在活跃时段"
        if self._in_quiet_hours(str(c.get("quiet_hours", "") or "")):
            return False, "免打扰时段"
        if self._daily_left(str(chat_id)) <= 0:
            return False, "今天次数已用完"

        silence_min = float(c.get("min_silence_minutes", 45) or 0)
        gap_min = (time.time() - last_ts) / 60 if last_ts else 1e9
        if gap_min < silence_min:
            return False, f"才静了 {gap_min:.0f} 分钟（要 {silence_min:.0f} 分钟）"

        # 对方最后一条还没被回应 -> 那是"该回复"，不是"主动"
        if last_role != "assistant":
            return False, "最后一条是对方发的，属于待回复而不是主动"

        chance = float(c.get("chance", 0.5) or 0)
        if chance < 1.0 and random.random() > chance:
            return False, "概率闸没过"
        return True, "条件满足"

    # ---------- 生成与发送 ----------

    async def _compose(self, chat_id: int, conv_id: int, insist: bool = False) -> str:
        """让模型决定说什么，或者决定不说。

        insist=True 时告诉它"刚才那条太弱，重写"，用于打回催问式开场。
        """
        messages, _ = await self.agent._build_messages(
            "private", chat_id,
            [{"segments": [], "text": "", "sender_id": chat_id, "sender_name": "",
              "message_id": 0, "ts": int(time.time())}],
        )
        # 用主动提示词覆盖"有人给你发了消息"的语境
        messages.insert(0, {"role": "system", "content": PROACTIVE_SYSTEM})
        if insist:
            nudge = ("（刚才你想说的那句太短或是在催问，重写一条。"
                     "说一件你自己的、具体的事，至少 8 个字。想不出来就输出 SKIP）")
        else:
            nudge = (f"（这次的开法：{random.choice(ANGLES)}。"
                     "只输出这一条，不要解释你在做什么，或者输出 SKIP）")
        messages.append({"role": "user", "content": nudge})
        try:
            raw = await self.agent.llm.chat(messages, max_tokens=80)
        except Exception as e:
            log.warning("主动消息生成失败: %s", e)
            return ""
        return (raw or "").strip()

    async def tick(self) -> bool:
        """跑一轮检查。返回是否真的发了。"""
        for chat_id in self._targets():
            conv_id = self.store.get_or_create_conversation("private", chat_id)
            recent = self.store.recent_messages(conv_id, 1)
            last = recent[-1] if recent else None
            last_ts = int(last.get("created_at") or 0) if last else 0
            last_role = str(last.get("role") or "") if last else ""

            ok, why = self.should_send_now(chat_id, last_ts, last_role)
            if not ok:
                continue

            text = await self._compose(chat_id, conv_id)
            if not text or SKIP_MARK in text.upper():
                self.skipped_count += 1
                log.info("主动消息：模型选择不发（%s）", text[:40] if text else "空")
                continue
            # 只取第一行，主动消息绝不连发
            text = text.split("\n")[0].strip()
            if not text:
                continue

            # 硬门槛：太短或催问式开场 -> 打回重写一次；还不行就这次不发。
            # 没有这道闸，实测它会连着发「人呢」「那三个数呢」——那是催债，不是开口。
            if is_weak_opener(text):
                log.info("主动消息太弱（%r），重写一次", text[:20])
                retry = await self._compose(chat_id, conv_id, insist=True)
                retry_first = (retry or "").split("\n")[0].strip()
                if not retry_first or SKIP_MARK in (retry or "").upper() or is_weak_opener(retry_first):
                    self.skipped_count += 1
                    log.info("重写后仍然太弱（%r），这次不发", retry_first[:20])
                    continue
                text = retry_first

            await self.agent._send_reply("private", chat_id, conv_id, text)
            # 注意：**不要再手动存一次**——_send_reply 内部已经把这条存进会话了。
            # 手动再存会让这条消息在历史里出现两遍（实测踩过）。
            self._sent_today[str(chat_id)] = self._sent_today.get(str(chat_id), 0) + 1
            self.sent_count += 1
            log.info("主动消息已发 -> %s: %s（今日第 %d 条）",
                     chat_id, text, self._sent_today[str(chat_id)])
            return True
        return False

    async def run(self) -> None:
        interval = float(self.conf.get("check_interval_seconds", 300) or 300)
        log.info("主动消息任务已启动：间隔 %.0f 秒，对象 %s，每天上限 %s",
                 interval, self._targets(), self.conf.get("max_per_day"))
        while True:
            try:
                await asyncio.sleep(interval)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("主动消息任务异常（不中断运行）: %s", e)

    def start(self) -> None:
        if not self.conf.get("enabled", False):
            log.info("主动消息：未启用")
            return
        if not self._targets():
            log.info("主动消息：没有对象（private_whitelist 为空）")
            return
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
