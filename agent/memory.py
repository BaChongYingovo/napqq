"""长期记忆：把滑出窗口的对话滚动压缩成摘要，存在 summaries 表里。

设计取舍：
- 压缩在回复之后异步进行，绝不阻塞回复路径；同一会话同时只跑一个压缩任务。
- 摘要只保留事实与关系（谁是谁、聊过什么、约定过什么），不保留语气细节——
  语气由人设负责，摘要负责"不失忆"。
- 压缩失败不影响正常对话，下一轮再试。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .llm import LLMClient, LLMError
from .store import Store

log = logging.getLogger("qqagent.memory")

COMPRESS_SYSTEM = """你是「聊天陪伴型」智能体的记忆压缩器。把「已有摘要」和「新增对话」合并成一段新摘要。

**这不是会议纪要，是你在记住一个和你亲近的人。** 所以按下面的优先级取舍：

【必须优先保留】
1. 你们之间的**专属互动**：专属称呼、只有你们懂的梗、约定（时间/地点/要做的事）、共同经历。
2. **对方的情感线**：他的情绪触发点（怕什么、讨厌什么、什么让他开心）、在意的事、
   你们之间亲密程度的变化（从客气到熟、从试探到靠近）。
3. 对方的重要事实：身份、关系、习惯、偏好、正在经历的难处。
4. 未说完的话、未解决的问题、他说"下次再说"的事。

【必须丢弃】
- 寒暄、天气、吃了什么、喝了什么、看了什么剧、躺哪、几点睡这类流水账
  ——**一句话都不要写进摘要**，不要用"他提到过…"的形式转述。
- 表情、重复内容、一次性的玩笑。
- 你自己（智能体）的台词，除非它包含上面的约定/梗。
- 判断标准：这条信息如果三个月后忘掉，会不会影响你们的关系？不会 → 丢掉。

【写法】
- 第三人称陈述，紧凑短句，不要评价，不要推测。
- 每条尽量带上"为什么重要"（例如"她怕黑，晚上必须开着灯——提过一次，是软肋"）。
- 不确定的信息标注"（不确定）"，**不要编造**。
- 只输出摘要正文，不要前后缀、标题或解释。
- 总长度不超过 {max_chars} 字。信息太多时：先删流水账，**不要删专属互动和情感线**。"""


class MemoryManager:
    def __init__(self, cfg: dict[str, Any], store: Store, llm: LLMClient):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def _summary_block(self, conv_id: int) -> str:
        content, _ = self.store.get_summary(conv_id)
        return content.strip()

    def summary_text(self, conv_id: int) -> str:
        return self._summary_block(conv_id)

    def needs_compression(self, conv_id: int) -> bool:
        if not self.enabled:
            return False
        trigger = int(self.cfg.get("trigger_messages", 40) or 40)
        return self.store.message_count(conv_id) > trigger

    def pending_messages(self, conv_id: int, keep_recent: int | None = None) -> list[dict[str, Any]]:
        """已滑出窗口、还没进摘要的消息。"""
        keep = int(self.cfg.get("keep_recent", 16) if keep_recent is None else keep_recent)
        _, upto = self.store.get_summary(conv_id)
        rows = self.store.messages_after(conv_id, upto)
        if len(rows) <= keep:
            return []
        return rows[:-keep] if keep > 0 else rows

    async def compress(self, conv_id: int, force: bool = False) -> bool:
        """执行一次压缩。返回是否真的写了新摘要。"""
        if not self.enabled or conv_id in self._busy:
            return False
        chunk = self.pending_messages(conv_id)
        if len(chunk) < int(self.cfg.get("min_chunk_messages", 8) or 8) and not force:
            return False
        if not chunk:
            return False

        self._busy.add(conv_id)
        try:
            old_summary, _ = self.store.get_summary(conv_id)
            max_chars = int(self.cfg.get("max_summary_chars", 1200) or 1200)
            transcript = "\n".join(
                f"{m['sender_name'] or m['role']}: {m['content']}" for m in chunk
            )
            user_block = ""
            if old_summary:
                user_block += f"【已有摘要】\n{old_summary}\n\n"
            user_block += f"【新增对话】\n{transcript}"

            model = str(self.cfg.get("summary_model") or "") or None
            new_summary = await self.llm.chat(
                [
                    {"role": "system", "content": COMPRESS_SYSTEM.format(max_chars=max_chars)},
                    {"role": "user", "content": user_block},
                ],
                temperature=0.3,
                model=model,
                max_tokens=max(256, max_chars),
            )
            new_summary = new_summary.strip()[: max_chars * 2]
            if not new_summary:
                return False
            self.store.set_summary(conv_id, new_summary, int(chunk[-1]["id"]))
            log.info("已压缩会话 %s 的 %d 条消息 -> %d 字摘要", conv_id, len(chunk), len(new_summary))
            return True
        except LLMError as e:
            log.warning("记忆压缩失败（不影响对话）: %s", e)
            return False
        except Exception:
            log.exception("记忆压缩异常（不影响对话）")
            return False
        finally:
            self._busy.discard(conv_id)

    def schedule(self, conv_id: int) -> None:
        """回复完成后调用：需要压缩时丢到后台，不阻塞。"""
        if not self.needs_compression(conv_id) or conv_id in self._busy:
            return
        task = asyncio.create_task(self.compress(conv_id), name=f"memory-{conv_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self, timeout: float = 15.0) -> None:
        """优雅退出：等后台压缩收尾，超时就取消。"""
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc:
                log.warning("后台压缩任务异常: %s", exc)

    async def wait_idle(self, timeout: float = 10.0) -> None:
        """给测试和优雅退出用。"""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._busy and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
