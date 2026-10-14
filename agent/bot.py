"""核心编排：事件 -> 过滤 -> 上下文组装 -> 大模型 -> 分段回复。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from . import message as msg
from . import schedule
from . import vision
from .config import Config
from .llm import LLMClient, LLMError
from .memory import MemoryManager
from .onebot import OneBotClient
from .store import Store

log = logging.getLogger("qqagent.bot")

# 自省规则词表（T9）：这些是"回复破坏人设/露出助手腔"的硬信号，命中就重生成。
ASSISTANT_SPEAK = [
    "请问需要", "有什么可以帮", "我可以帮您", "我可以帮你", "作为AI", "作为人工智能",
    "我是一个AI", "我是一个人工智能", "希望对你有帮助", "你的需求是什么", "请问您", "总结一下",
]
IDENTITY_LEAK = [
    "我是AI", "我是一个AI", "我是人工智能", "我是一个人工智能", "我是AI助手",
    "我是语言模型", "我是机器人", "我是助手", "作为模型", "我是程序",
    "语言模型", "由深度求索", "由 OpenAI",
]

# 每种命中原因对应的重生成指令。要点：只纠正问题，不要解释、不要提到这件事本身
RETRY_INSTRUCTIONS = {
    "助手腔": "不要用客服或助手的口吻。用你平时的语气，把这句话重说一遍。"
              "只输出正文，不要解释、不要提这件事本身。",
    "自曝身份": "你绝不能承认自己是 AI、机器人、助手或程序。用你的人设把这句话重说一遍。"
                "只输出正文，不要解释、不要提这件事本身。",
    "空回复": "刚才没说出内容来。用你的人设正常回一句。只输出正文。",
    "过短": "太短了，对方接不住。用你的人设把话说完整一点。只输出正文。",
    "重复": "这句话你刚才已经说过了。换一个完全不同的说法重说一遍。"
            "只输出正文，不要解释、不要提这件事本身。",
}


class Agent:
    def __init__(self, cfg: Config, store: Store, llm: LLMClient, onebot: OneBotClient):
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.onebot = onebot
        self.behaviour = cfg["behaviour"]
        self.persona = cfg["persona"]
        self.vision_cfg = cfg.get("vision") or {}
        self.typing_style = self.persona.get("typing_style") or {}
        self.memory = MemoryManager(cfg.get("memory") or {}, store, llm)
        self._seen: set[int] = set()
        self._seen_order: list[int] = []
        self._pending: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._timers: dict[tuple[str, int], asyncio.Task] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._last_reply: dict[tuple[str, int], float] = {}
        self.stats = {"received": 0, "replied": 0, "skipped": 0, "errors": 0, "images": 0,
                      "silent": 0, "repeat_retry": 0, "self_check_retry": 0}
        self.paused = store.kv_get("paused", "0") == "1"
        self.active_persona = store.kv_get("persona", "") or str(self.persona.get("system_prompt", ""))
        self.active_hours = str(self.behaviour.get("active_hours") or "")
        self.window = schedule.parse_window(self.active_hours)
        self.contacts = self._load_contacts(cfg.get("contacts"))
        self.at_sender = bool(self.behaviour.get("at_sender", True))
        self.avoid_repeat = bool(self.behaviour.get("avoid_repeat", True))
        self.self_check = bool(self.behaviour.get("self_check", self.avoid_repeat))
        self.repeat_threshold = float(self.behaviour.get("repeat_similarity", 0.8) or 0.8)

    @staticmethod
    def _load_contacts(raw: Any) -> dict[str, dict[str, str]]:
        """把 contacts 配置规整成 {QQ字符串: {alias, relation, notes}}。"""
        out: dict[str, dict[str, str]] = {}
        if not isinstance(raw, dict):
            return out
        for qq, info in raw.items():
            key = str(qq).strip()
            if not key.isdigit() or not isinstance(info, dict):
                continue
            entry = {
                "alias": str(info.get("alias") or "").strip(),
                "relation": str(info.get("relation") or "").strip(),
                "notes": str(info.get("notes") or "").strip(),
            }
            if any(entry.values()):
                out[key] = entry
        return out

    def _contact_for(self, chat_type: str, chat_id: int) -> dict[str, str] | None:
        if chat_type != "private":
            return None
        return self.contacts.get(str(chat_id))

    @property
    def in_active_hours(self) -> bool:
        return schedule.in_window(self.window)

    @property
    def active_hours_text(self) -> str:
        return schedule.describe(self.window)

    @property
    def vision_enabled(self) -> bool:
        return bool(self.vision_cfg.get("enabled", False))

    # ---------- 入口 ----------

    async def handle_event(self, event: dict[str, Any]) -> None:
        post_type = event.get("post_type")
        if post_type == "meta_event":
            if event.get("meta_event_type") == "lifecycle":
                log.info("OneBot 生命周期事件: %s", (event.get("sub_type") or "?"))
            return
        if post_type != "message":
            return

        message_type = event.get("message_type")
        if message_type not in ("private", "group"):
            return

        message_id = int(event.get("message_id") or 0)
        if message_id and message_id in self._seen:
            return
        if message_id:
            self._remember(message_id)

        self_id = int(event.get("self_id") or self.onebot.self_id or 0)
        sender = event.get("sender") or {}
        sender_id = int(event.get("user_id") or sender.get("user_id") or 0)
        if sender_id == self_id:
            return

        segments = msg.normalize_message(event.get("message"))
        text = msg.render_segments(segments).strip()
        if not text:
            return

        self.stats["received"] += 1
        raw_text = msg.extract_text(segments)
        if not raw_text.strip():
            raw_text = text

        # 主人指令
        if await self._maybe_command(message_type, event, sender, raw_text):
            return

        if not self._should_reply(message_type, event, segments, self_id, sender_id):
            self.stats["skipped"] += 1
            return

        key = (message_type, int(event.get("group_id") if message_type == "group" else sender_id))
        item = {
            "segments": segments,
            "text": text,
            "sender_id": sender_id,
            "sender_name": str(sender.get("card") or sender.get("nickname") or sender_id),
            "message_id": message_id,
            "ts": int(event.get("time") or time.time()),
        }
        self._pending.setdefault(key, []).append(item)
        timer = self._timers.get(key)
        if timer and not timer.done():
            timer.cancel()
        debounce = float(self.behaviour.get("debounce_seconds", 1.2) or 0)
        self._timers[key] = asyncio.create_task(self._flush(key, debounce))

    async def _flush(self, key: tuple[str, int], delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        batch = self._pending.pop(key, [])
        self._timers.pop(key, None)
        if not batch:
            return
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            # 上一条还在生成，直接把这条并进下一轮
            self._pending.setdefault(key, []).extend(batch)
            self._timers[key] = asyncio.create_task(self._flush(key, 0.5))
            return
        async with lock:
            await self._reply(key, batch)

    # ---------- 过滤 ----------

    def _should_reply(
        self,
        chat_type: str,
        event: dict[str, Any],
        segments: list[dict[str, Any]],
        self_id: int,
        sender_id: int,
    ) -> bool:
        b = self.behaviour
        if chat_type == "private":
            if not b.get("private_reply", True):
                return False
            if sender_id in set(b.get("private_blacklist") or []):
                return False
            # 私聊白名单：非空时**只**回复名单里的人，其他一概不理。
            # 这是最省心的收窄方式——比黑名单安全，不会漏掉新加的骚扰者。
            pw = set(b.get("private_whitelist") or [])
            if pw and sender_id not in pw:
                return False
            return True
        group_id = int(event.get("group_id") or 0)
        if group_id in set(b.get("group_blacklist") or []):
            return False
        whitelist = set(b.get("group_whitelist") or [])
        if whitelist and group_id not in whitelist:
            return False
        mode = b.get("group_reply_mode", "mention")
        if mode == "off":
            return False
        if mode == "all":
            return True
        return msg.is_at_bot(segments, self_id)

    # ---------- 指令 ----------

    async def _maybe_command(
        self, chat_type: str, event: dict[str, Any], sender: dict[str, Any], raw_text: str
    ) -> bool:
        cmds = self.behaviour.get("commands") or {}
        if not cmds.get("enabled", True):
            return False
        # raw_text 可能带 " @10001" 之类的渲染前缀（群聊 @ 机器人时），
        # 这里以消息段里的纯文本为准，并去掉开头的 @ 提及。
        text = msg.extract_text(msg.normalize_message(event.get("message"))).strip()
        if not text:
            text = re.sub(r"^[@\s]+\d*\s*", "", raw_text or "").strip()
        if not text.startswith("/"):
            return False
        sender_id = int(event.get("user_id") or 0)
        owner = int(self.behaviour.get("owner_qq") or 0)
        if not owner:
            # 没配主人 = 管理指令整体关闭。
            # 否则任何人都能发 /pause /reset /persona 劫持机器人。
            return False
        if sender_id != owner:
            return False
        chat_id = int(event.get("group_id") if chat_type == "group" else sender_id)
        word = text.split()[0]

        if word == cmds.get("reset", "/reset"):
            conv_id = self.store.get_or_create_conversation(chat_type, chat_id)
            removed = self.store.clear_conversation(conv_id)
            self._pending.pop((chat_type, chat_id), None)
            await self.onebot.send_text(chat_type, chat_id, f"已清空本会话记忆（{removed} 条）。")
            return True
        if word == cmds.get("pause", "/pause"):
            self.paused = True
            self.store.kv_set("paused", "1")
            await self.onebot.send_text(chat_type, chat_id, "已暂停，只记录不回复。发 /resume 恢复。")
            return True
        if word == cmds.get("resume", "/resume"):
            self.paused = False
            self.store.kv_set("paused", "0")
            await self.onebot.send_text(chat_type, chat_id, "已恢复。")
            return True
        if word == cmds.get("persona", "/persona"):
            new_persona = text[len(word):].strip()
            if not new_persona:
                await self.onebot.send_text(chat_type, chat_id, f"当前人设：\n{self.active_persona[:500]}")
                return True
            self.active_persona = new_persona
            self.store.kv_set("persona", new_persona)
            await self.onebot.send_text(chat_type, chat_id, "人设已更新。")
            return True
        if word == cmds.get("memory", "/memory"):
            conv_id = self.store.get_or_create_conversation(chat_type, chat_id)
            summary = self.memory.summary_text(conv_id)
            count = self.store.message_count(conv_id)
            if summary:
                await self.onebot.send_text(
                    chat_type, chat_id, f"长期记忆（{count} 条消息，压缩至此）：\n{summary}"
                )
            else:
                await self.onebot.send_text(
                    chat_type, chat_id, f"暂无长期记忆（{count} 条消息，超过阈值会自动压缩）。"
                )
            return True
        if word == cmds.get("forget", "/forget"):
            conv_id = self.store.get_or_create_conversation(chat_type, chat_id)
            self.store.set_summary(conv_id, "", 0)
            await self.onebot.send_text(chat_type, chat_id, "已清空长期记忆（近期对话保留）。")
            return True
        return False

    # ---------- 回复 ----------

    async def _build_messages(
        self,
        chat_type: str,
        chat_id: int,
        batch: list[dict[str, Any]],
        exclude_ids: set[int] | None = None,
        allow_images: bool = True,
    ) -> tuple[list[dict[str, Any]], bool]:
        history_limit = int(self.cfg["llm"].get("history_limit", 24) or 24)
        conv_id = self.store.get_or_create_conversation(chat_type, chat_id)
        if self.memory.enabled:
            _, summary_upto = self.store.get_summary(conv_id)
            # 摘要之前的内容已经在长期记忆里，不再重复塞进窗口
            history = self.store.recent_messages_after(conv_id, summary_upto, history_limit)
        else:
            history = self.store.recent_messages(conv_id, history_limit)
        if not history:
            history = self.store.recent_messages(conv_id, history_limit)
        if exclude_ids:
            history = [m for m in history if int(m["id"]) not in exclude_ids]
        summary = self.memory.summary_text(conv_id) if self.memory.enabled else ""

        system = self.active_persona or str(self.persona.get("system_prompt", ""))
        if self.vision_enabled and allow_images:
            image_hint = "- [图片] 表示对方发了图片，你能看到图片内容，直接回应画面里的东西，不要复述“我收到了图片”。"
        else:
            image_hint = "- [图片] 表示对方发了图片，但你看不到内容——不要假装看到了，可以追问或结合上下文回应。"
        context_lines = [
            system,
            "",
            "【运行信息】",
            f"- 你的昵称：{self.persona.get('name', '')}（QQ {self.onebot.self_id}）",
            f"- 当前会话：{'群聊 ' + str(chat_id) if chat_type == 'group' else '私聊 ' + str(chat_id)}",
        ]
        # 时间必须显式给：历史消息里的 [时间] 是"过去那条消息的时间"，
        # 模型无法据此推出"现在几点"，会照着旧记录瞎猜。
        last_ts = max((int(m.get("ts") or 0) for m in history), default=0) if history else 0
        context_lines.extend(msg.time_context(last_ts=last_ts or None))
        context_lines.extend([
            "- 历史记录格式为 `[时间] 昵称: 内容`，其中 @数字 表示 @某人。",
            image_hint,
            "- [表情:名称] 是对方发的 QQ 系统表情（如 [表情:笑哭]、[表情:发怒]），[表情包] 是表情包，"
            "按这个情绪理解对方，不要把它当成文字念出来。",
            "- 你只需输出要发送的正文，不要复述时间或昵称，不要输出 CQ 码、不要 @ 别人（除极少数必要时用 @数字 形式）。",
        ])
        if chat_type == "group":
            context_lines.append("- 这是群聊：回复要短、要能接住话头；不要长篇大论，不要重复别人说过的话。")
        if summary:
            context_lines.append("- 下方【长期记忆】是本会话更早之前的内容摘要，当作你确实记得的事，不要复述摘要本身。")

        contact = self._contact_for(chat_type, chat_id)
        if contact:
            known = []
            if contact["alias"]:
                known.append(f"- 你对他的备注：{contact['alias']}")
            if contact["relation"] and contact["relation"] != contact["alias"]:
                known.append(f"- 身份关系：{contact['relation']}")
            if contact["notes"]:
                known.append(f"- 需要记住的补充信息：{contact['notes']}")
            if known:
                context_lines.append("")
                context_lines.append("【对方身份】下面这个人是你本来就认识的，不是陌生人，按这个关系和备注来对待：")
                context_lines.extend(known)
        elif chat_type == "group":
            speakers = {
                str(item["sender_id"]): self.contacts[str(item["sender_id"])]
                for item in batch
                if str(item["sender_id"]) in self.contacts
            }
            for qq, info in speakers.items():
                label = info["alias"] or info["relation"] or qq
                extra = (f"（{info['relation']}）"
                         if info["relation"] and info["alias"] and info["alias"] != info["relation"] else "")
                context_lines.append(f"- 群里 {qq} 是你认识的：{label}{extra}")

        dialogue = "\n".join(
            f"[{msg.fmt_clock(m['created_at'])}] {m['sender_name'] or m['role']}: {m['content']}" for m in history
        )
        current_lines = "\n".join(f"[{msg.fmt_clock(b['ts'])}] {b['sender_name']}: {b['text']}" for b in batch)
        if batch:
            current_lines += f"\n[{msg.fmt_clock(batch[-1]['ts'])}] 系统: （以上是你现在要回应的话，请只回复最后这一段对话）"

        user_block = ""
        if summary:
            user_block += f"【长期记忆】\n{summary}\n\n"
        if dialogue:
            user_block += f"【近期对话】\n{dialogue}\n\n"
        user_block += f"【新消息】\n{current_lines}"

        content: Any = user_block
        used_images = False
        if allow_images and self.vision_enabled:
            image_parts = await self._image_parts(batch)
            if image_parts:
                content = [{"type": "text", "text": user_block}] + image_parts
                used_images = True
                self.stats["images"] += len(image_parts)

        messages: list[dict[str, Any]] = [{"role": "system", "content": "\n".join(context_lines)}]
        messages.append({"role": "user", "content": content})
        return messages, used_images

    async def _image_parts(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        vcfg = self.vision_cfg
        max_images = int(vcfg.get("max_images_per_message", 2) or 2)
        max_bytes = int(vcfg.get("max_image_bytes", 4 * 1024 * 1024) or 4 * 1024 * 1024)
        timeout = float(vcfg.get("image_timeout_seconds", 20) or 20)
        detail = str(vcfg.get("detail", "auto"))
        transport = vision.resolve_transport(vcfg, str(self.cfg["llm"].get("base_url", "")))

        parts: list[dict[str, Any]] = []
        for item in batch:
            for seg in item["segments"]:
                if seg.get("type") != "image":
                    continue
                if len(parts) >= max_images:
                    return parts
                part = await self._resolve_image(seg.get("data") or {}, transport, max_bytes, timeout, detail)
                if part:
                    parts.append(part)
        return parts

    async def _resolve_image(
        self, data: dict[str, Any], transport: str, max_bytes: int, timeout: float, detail: str
    ) -> dict[str, Any] | None:
        direct = str(data.get("url") or "")
        file_id = str(data.get("file") or "")
        candidates: list[str] = []
        if direct:
            candidates.append(direct)
        if file_id:
            try:
                info = await self.onebot.get_image(file_id)
            except Exception as e:
                log.debug("get_image 失败 file=%s: %s", file_id, e)
                info = None
            if isinstance(info, dict):
                for key in ("url", "file"):
                    if info.get(key):
                        candidates.append(str(info[key]))
            elif isinstance(info, str) and info:
                candidates.append(info)
            candidates.append(file_id)

        for cand in candidates:
            if cand.startswith("data:image/"):
                part = vision.build_image_part(cand, detail)
                if part:
                    return part
            elif cand.startswith(vision.REMOTE_SCHEMES):
                if transport == "url":
                    part = vision.build_image_part(cand, detail)
                    if part:
                        log.debug("图片以链接形式交给模型: %s", cand[:80])
                        return part
                else:
                    data_url = await asyncio.to_thread(vision.download_to_data_url, cand, max_bytes, timeout)
                    if data_url:
                        part = vision.build_image_part(data_url, detail)
                        if part:
                            return part
            else:
                path = cand[7:] if cand.startswith("file://") else cand
                data_url = await asyncio.to_thread(vision.file_to_data_url, path, max_bytes)
                if data_url:
                    part = vision.build_image_part(data_url, detail)
                    if part:
                        log.debug("本地图片已转 base64: %s", path)
                        return part
        log.warning("图片无法解析为可发送形式，本轮按纯文本继续（file=%s url=%s）", file_id[:60], direct[:60])
        return None

    def _store_batch(self, chat_type: str, chat_id: int, batch: list[dict[str, Any]]) -> tuple[int, set[int]]:
        conv_id = self.store.get_or_create_conversation(chat_type, chat_id)
        ids: set[int] = set()
        for item in batch:
            ids.add(self.store.add_message(
                conv_id,
                "user",
                item["text"],
                sender_id=item["sender_id"],
                sender_name=item["sender_name"],
                raw=msg.segments_to_cq(item["segments"]),
            ))
        return conv_id, ids

    async def _reply(self, key: tuple[str, int], batch: list[dict[str, Any]]) -> None:
        chat_type, chat_id = key
        if self.paused:
            self._store_batch(chat_type, chat_id, batch)
            log.info("已暂停，仅记录 %d 条", len(batch))
            return
        if not self.in_active_hours:
            # 夜间静默：照常存进上下文（第二天它"知道你半夜来过"），只是不回。
            self._store_batch(chat_type, chat_id, batch)
            self.stats["silent"] += 1
            log.info("静默时段(%s)，仅记录 %d 条不回复", self.active_hours_text, len(batch))
            return

        conv_id, stored_ids = self._store_batch(chat_type, chat_id, batch)

        min_interval = float(self.behaviour.get("min_reply_interval_seconds", 0) or 0)
        if min_interval > 0:
            elapsed = time.time() - self._last_reply.get(key, 0.0)
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

        messages, used_images = await self._build_messages(chat_type, chat_id, batch, stored_ids)
        log.info("请求模型 (会话 %s/%s, 历史 %d 条, 新消息 %d 条%s)",
                 chat_type, chat_id, int(self.cfg["llm"].get("history_limit", 24)), len(batch),
                 "，含图" if used_images else "")
        started = time.time()
        vision_model = str(self.vision_cfg.get("model") or "") or None
        try:
            reply = await self.llm.chat(messages, model=vision_model if used_images else None)
        except Exception as e:
            if used_images:
                log.warning("带图请求失败，降级为纯文本重试: %s", e)
                try:
                    plain, _ = await self._build_messages(
                        chat_type, chat_id, batch, stored_ids, allow_images=False
                    )
                    reply = await self.llm.chat(plain)
                except Exception as e2:
                    self.stats["errors"] += 1
                    log.error("纯文本重试也失败: %s", e2)
                    fallback = str(self.behaviour.get("error_reply") or "")
                    if fallback:
                        await self.onebot.send_text(chat_type, chat_id, fallback)
                    return
            else:
                self.stats["errors"] += 1
                if isinstance(e, LLMError):
                    log.error("模型调用失败: %s", e)
                else:
                    log.exception("模型调用异常")
                fallback = str(self.behaviour.get("error_reply") or "")
                if fallback:
                    await self.onebot.send_text(chat_type, chat_id, fallback)
                return

        log.info("模型返回 %.1fs %d 字: %s", time.time() - started, len(reply), reply[:80].replace("\n", " "))
        reply = await self._avoid_repeat(chat_type, chat_id, conv_id, messages, reply)
        await self._send_reply(chat_type, chat_id, conv_id, reply, at_qq=batch[-1]["sender_id"])
        self.memory.schedule(conv_id)

    async def _avoid_repeat(
        self, chat_type: str, chat_id: int, conv_id: int,
        base_messages: list[dict[str, Any]], reply: str,
    ) -> str:
        """发送前自省（T5 反重复 + T9 人设自检）：命中规则就重生成一次。

        为什么用规则而不是"每轮再调一次模型自省"：后者会让每轮的成本和延迟
        直接翻倍。规则层能抓住的四类硬伤——助手腔、自曝身份、空/过短、重复——
        正是最容易毁人设也最好判定的部分。

        只重试一次：这是成本上限，不可无限重试。
        """
        if not self.self_check or not reply:
            return reply
        recent = [m["content"] for m in self.store.recent_messages(conv_id, 20)
                  if m.get("role") == "assistant"][-5:]

        reason, detail = self._check_reply(reply, recent, check_repeat=self.avoid_repeat)
        if not reason:
            return reply

        self.stats["self_check_retry"] += 1
        if reason == "重复":
            self.stats["repeat_retry"] += 1
        log.info("自省命中「%s」，重生成一次：%s", reason, detail[:40])

        retry_content = RETRY_INSTRUCTIONS[reason]
        if reason == "重复":
            # 只说"换个说法"不够——实测模型会再给一个近乎相同的句子。
            # 把**具体要避开的原句**列出来，它才知道要躲什么。
            avoid = [r for r in recent if msg.similarity(reply, r) >= self.repeat_threshold * 0.75][:3]
            if avoid:
                retry_content = (
                    "你刚才那句话和之前说过的太像了。**不要再使用下面这些说法**：\n"
                    + "\n".join(f"- {r}" for r in avoid)
                    + "\n换一个完全不同的角度和措辞重说一遍。只输出正文，不要解释、不要提这件事本身。"
                )
        retry_messages = list(base_messages) + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": retry_content},
        ]
        try:
            fresh = await self.llm.chat(retry_messages)
        except Exception as e:
            log.warning("重生成失败，沿用原回复: %s", e)
            return reply
        if not fresh:
            return reply
        fresh = fresh.strip()
        again, _ = self._check_reply(fresh, recent, check_repeat=self.avoid_repeat)
        if again:
            log.info("重生成后仍命中「%s」，不再重试", again)
        return fresh

    def _check_reply(self, reply: str, recent: list[str], check_repeat: bool = True) -> tuple[str, str]:
        """返回 (命中原因, 细节)；没命中返回 ("", "")。

        判定顺序有讲究：**自曝身份优先于助手腔**。
        因为「我是一个AI」同时命中两张词表，而身份泄露是更严重的违规，
        必须用更针对性的指令去纠正，不能被泛化的"助手腔"吃掉。
        """
        text = (reply or "").strip()
        if not text:
            return "空回复", ""
        if len(text) < 2:
            return "过短", text
        for word in IDENTITY_LEAK:
            if word in text:
                return "自曝身份", word
        for word in ASSISTANT_SPEAK:
            if word in text:
                return "助手腔", word
        if check_repeat:
            hit = next((r for r in recent if msg.similarity(text, r) >= self.repeat_threshold), None)
            if hit:
                return "重复", hit[:30]
        return "", ""

    async def _send_reply(
        self, chat_type: str, chat_id: int, conv_id: int, reply: str, at_qq: int = 0
    ) -> None:
        style = self.typing_style or {}
        if style.get("enabled", True):
            limit = int(style.get("max_chars_per_message", 180) or 180)
            threshold = int(style.get("long_reply_threshold", 0) or 0)
            if threshold > 0 and len(reply) > threshold:
                # 背长文这类情况：换成大段落，避免几十条短消息刷屏
                limit = int(style.get("long_reply_max_chars_per_message", limit) or limit)
                log.info("长回复 %d 字，改用 %d 字分段", len(reply), limit)
            pieces = msg.split_reply(
                reply,
                limit,
                bool(style.get("split_on_newline", True)),
                int(style.get("max_messages_per_reply", 0) or 0),
            )
        else:
            pieces = [reply]

        # 群聊回复谁就 @ 谁：只加在第一条上，否则每条都通知一次，等于刷屏
        mention = int(at_qq) if (at_qq and chat_type == "group" and self.at_sender) else 0

        # 拟人节奏：真人不会 0.5 秒就吐出一整串。按字数算一个"打字时间"。
        # 这是降低 AI 味最直接的一招——秒回+话多，是最容易被认出来的组合。
        td = self.cfg["behaviour"].get("typing_delay") or {}
        if td.get("enabled", True) and pieces:
            delay = min(
                float(td.get("max_seconds", 6.0) or 6.0),
                float(td.get("base_seconds", 1.2) or 0) + len(reply) * float(td.get("per_char", 0.10) or 0),
            )
            if delay > 0.05:
                await asyncio.sleep(delay)

        sent_any = False
        for i, piece in enumerate(pieces):
            segments = msg.text_to_segments(piece)
            if mention and i == 0 and str(mention) not in msg.mentioned_qqs(segments):
                # 空格并进正文，避免发出 "at + 空格 + 正文" 这种三段式的碎片
                if segments and segments[0]["type"] == "text":
                    segments[0] = {"type": "text",
                                   "data": {"text": " " + segments[0]["data"].get("text", "")}}
                else:
                    segments = [{"type": "text", "data": {"text": " "}}] + segments
                segments = [{"type": "at", "data": {"qq": str(mention)}}] + segments
            try:
                await self.onebot.send_segments(chat_type, chat_id, segments)
                sent_any = True
            except Exception as e:
                log.error("发送失败: %s", e)
                self.stats["errors"] += 1
                break
            if i < len(pieces) - 1:
                await asyncio.sleep(min(1.5, 0.35 + len(piece) / 60))
        if sent_any:
            self.stats["replied"] += 1
            self._last_reply[(chat_type, chat_id)] = time.time()
            self.store.add_message(conv_id, "assistant", reply, sender_id=self.onebot.self_id,
                                   sender_name=self.onebot.nickname or "我")

    # ---------- 工具 ----------

    def _remember(self, message_id: int, cap: int = 2000) -> None:
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        if len(self._seen_order) > cap:
            old = self._seen_order.pop(0)
            self._seen.discard(old)

    async def shutdown(self) -> None:
        for task in self._timers.values():
            if not task.done():
                task.cancel()
        for key, batch in list(self._pending.items()):
            try:
                self._store_batch(key[0], key[1], batch)
            except Exception:
                pass
        self._pending.clear()
        await self.memory.close()

    def status_line(self) -> str:
        s = self.stats
        flags = []
        if self.paused:
            flags.append("已暂停")
        if not self.in_active_hours:
            flags.append(f"静默中({self.active_hours_text})")
        suffix = (" | " + " ".join(flags)) if flags else ""
        return (f"收到 {s['received']} / 回复 {s['replied']} / 跳过 {s['skipped']}"
                f" / 静默 {s['silent']} / 错误 {s['errors']}{suffix}")
