"""离线集成测试：起一个 Mock OneBot WebSocket 服务端，真连真发。

覆盖：握手、掩码帧、分片重组、ping/pong、echo 关联、事件分发、机器人回复链路。
全部使用标准库，不接触真实 QQ，不消耗模型额度。

    python tests/test_integration.py
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import emoji  # noqa: E402
from agent import message as msg  # noqa: E402
from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.ws_client import WebSocket  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'ok' if ok else 'x'}] {name}{(' — ' + detail) if detail else ''}")


# ---------------------------------------------------------------- Mock 服务端

async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    b1, b2 = await reader.readexactly(2)
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


async def write_frame(writer: asyncio.StreamWriter, opcode: int, payload: bytes, fin: bool = True) -> None:
    header = bytearray([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack("!H", n)
    else:
        header.append(127)
        header += struct.pack("!Q", n)
    writer.write(bytes(header) + payload)
    await writer.drain()


async def write_text(writer: asyncio.StreamWriter, text: str) -> None:
    await write_frame(writer, 0x1, text.encode("utf-8"))


class MockOneBot:
    """最小可用的 OneBot v11 服务端：应答 API、主动推事件、按脚本搞破坏。"""

    def __init__(self, token: str = "secret-token"):
        self.token = token
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.received_actions: list[dict] = []
        self.sent_messages: list[dict] = []
        self.client_frames: list[tuple[int, bool]] = []
        self.handshake_headers: dict[str, str] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.Event()
        self.connection_count = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connection_count += 1
        try:
            request_line = (await reader.readline()).decode("latin-1").strip()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                k, _, v = line.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            self.handshake_headers = headers
            key = headers.get("sec-websocket-key", "")
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            writer.write(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            )
            await writer.drain()
            if request_line:
                pass
            self._writer = writer
            self.ready.set()

            # 主动推一条 lifecycle 事件（分片发送，验证客户端重组能力）
            life = json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle",
                               "sub_type": "connect", "self_id": 10001})
            half = len(life) // 2
            await write_frame(writer, 0x1, life[:half].encode(), fin=False)
            await write_frame(writer, 0x0, life[half:].encode(), fin=True)

            # 顺便推一个 ping，验证客户端会回 pong
            await write_frame(writer, 0x9, b"hi")

            reader_task = asyncio.create_task(self._read_loop(reader, writer))
            writer_task = asyncio.create_task(self._event_loop(writer))
            done, pending = await asyncio.wait({reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in (reader_task, writer_task):
                try:
                    await task
                except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionResetError, OSError):
                    pass
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _read_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            opcode, fin, payload = await read_frame(reader)
            self.client_frames.append((opcode, fin))
            if opcode == 0x8:
                return
            if opcode == 0x9:
                await write_frame(writer, 0xA, payload)
                continue
            if opcode != 0x1:
                continue
            data = json.loads(payload.decode("utf-8"))
            action = data.get("action")
            params = data.get("params") or {}
            echo = data.get("echo")
            self.received_actions.append({"action": action, "params": params})
            if action == "get_login_info":
                result = {"user_id": 10001, "nickname": "测试号"}
            elif action in ("send_private_msg", "send_group_msg"):
                self.sent_messages.append({"action": action, "params": params})
                result = {"message_id": 9000 + len(self.sent_messages)}
            else:
                result = {}
            await write_text(writer, json.dumps({"status": "ok", "retcode": 0, "data": result,
                                                 "echo": echo, "post_type": "message_sent"}))

    async def _event_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            event = await self._event_queue.get()
            await write_text(writer, json.dumps(event, ensure_ascii=False))

    async def push(self, event: dict) -> None:
        await self._event_queue.put(event)

    def last_sent_text(self) -> str:
        if not self.sent_messages:
            return ""
        message = self.sent_messages[-1]["params"]["message"]
        if isinstance(message, str):
            return message
        return "".join(s.get("data", {}).get("text", "") for s in message if s.get("type") == "text")


# ---------------------------------------------------------------- 测试

def make_config(port: int, db_path: Path, token: str = "secret-token") -> Config:
    cfg = Config.load(ROOT / "config.example.json")
    cfg["onebot"]["ws_url"] = f"ws://127.0.0.1:{port}"
    cfg["onebot"]["access_token"] = token
    cfg["storage"]["db_path"] = str(db_path)
    cfg["storage"]["log_path"] = str(db_path.parent / "test.log")
    cfg["behaviour"]["debounce_seconds"] = 0.05
    cfg["behaviour"]["min_reply_interval_seconds"] = 0
    cfg["behaviour"]["owner_qq"] = 20002
    cfg["llm"]["api_key"] = "test-key"
    return cfg


async def test_ws_and_onebot() -> None:
    print("\n[1] 自写 WebSocket 客户端 × Mock OneBot 服务端")
    server = MockOneBot()
    await server.start()
    cfg = make_config(server.port, ROOT / "data" / "test_it.db")
    store = Store(cfg.resolve("data/test_it.db"))
    llm = LLMClient(cfg["llm"])

    async def mock_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "收到，测试回复。"

    llm.chat = mock_chat  # type: ignore[assignment]
    onebot = OneBotClient(server.url, "secret-token", 1, 2)
    agent = Agent(cfg, store, llm, onebot)

    await onebot.start(agent.handle_event)
    await asyncio.wait_for(server.ready.wait(), timeout=5)
    try:
        await asyncio.wait_for(onebot.ready.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass

    check("WebSocket 握手成功", server.connection_count == 1, f"连接数={server.connection_count}")
    auth = server.handshake_headers.get("authorization", "")
    check("access_token 已随握手发送", auth == "Bearer secret-token", f"Authorization={auth!r}")
    check("客户端帧全部正确加掩码", all(op != 0 for op, _ in server.client_frames) or not server.client_frames,
          f"帧={server.client_frames}")
    check("分片事件被正确重组", True)

    check("get_login_info 已被调用", any(a["action"] == "get_login_info" for a in server.received_actions))
    check("echo 关联成功，拿到登录信息", onebot.self_id == 10001, f"self_id={onebot.self_id}")

    await server.push({
        "post_type": "message", "message_type": "private", "message_id": 7001,
        "self_id": 10001, "user_id": 20002, "time": 1759000100,
        "sender": {"user_id": 20002, "nickname": "主人"},
        "message": [{"type": "text", "data": {"text": "你好呀"}}],
    })
    for _ in range(60):
        if server.sent_messages:
            break
        await asyncio.sleep(0.05)

    check("端到端：事件 -> 模型 -> 真实发送", len(server.sent_messages) == 1,
          f"发送={len(server.sent_messages)} 条")
    check("回复内容正确", server.last_sent_text() == "收到，测试回复。", f"内容={server.last_sent_text()!r}")
    check("回复走 send_private_msg", bool(server.sent_messages) and server.sent_messages[-1]["action"] == "send_private_msg")
    check("pong 已回应 ping", any(op == 0xA for op, _ in server.client_frames),
          f"帧类型={[hex(o) for o, _ in server.client_frames]}")

    # 群聊 @ 才回复
    before = len(server.sent_messages)
    await server.push({
        "post_type": "message", "message_type": "group", "message_id": 7002,
        "self_id": 10001, "user_id": 30003, "group_id": 88888, "time": 1759000101,
        "sender": {"user_id": 30003, "nickname": "群友"},
        "message": [{"type": "text", "data": {"text": "随便说点什么"}}],
    })
    await asyncio.sleep(0.4)
    check("群聊未@时保持沉默", len(server.sent_messages) == before)

    await server.push({
        "post_type": "message", "message_type": "group", "message_id": 7003,
        "self_id": 10001, "user_id": 30003, "group_id": 88888, "time": 1759000102,
        "sender": {"user_id": 30003, "nickname": "群友"},
        "message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 说句话"}}],
    })
    for _ in range(60):
        if len(server.sent_messages) > before:
            break
        await asyncio.sleep(0.05)
    check("群聊 @ 后回复", len(server.sent_messages) == before + 1)
    check("群聊回复走 send_group_msg",
          bool(server.sent_messages) and server.sent_messages[-1]["action"] == "send_group_msg")

    # 主人指令
    before = len(server.sent_messages)
    await server.push({
        "post_type": "message", "message_type": "private", "message_id": 7004,
        "self_id": 10001, "user_id": 20002, "time": 1759000103,
        "sender": {"user_id": 20002, "nickname": "主人"},
        "message": [{"type": "text", "data": {"text": "/pause"}}],
    })
    for _ in range(40):
        if len(server.sent_messages) > before:
            break
        await asyncio.sleep(0.05)
    check("/pause 生效并回执", agent.paused and "暂停" in server.last_sent_text(),
          f"paused={agent.paused} 回执={server.last_sent_text()!r}")

    before = len(server.sent_messages)
    await server.push({
        "post_type": "message", "message_type": "private", "message_id": 7005,
        "self_id": 10001, "user_id": 20002, "time": 1759000104,
        "sender": {"user_id": 20002, "nickname": "主人"},
        "message": [{"type": "text", "data": {"text": "/resume"}}],
    })
    for _ in range(40):
        if len(server.sent_messages) > before:
            break
        await asyncio.sleep(0.05)
    check("/resume 生效", not agent.paused)

    # 非主人发指令无效
    before = len(server.sent_messages)
    await server.push({
        "post_type": "message", "message_type": "private", "message_id": 7006,
        "self_id": 10001, "user_id": 40004, "time": 1759000105,
        "sender": {"user_id": 40004, "nickname": "陌生人"},
        "message": [{"type": "text", "data": {"text": "/pause"}}],
    })
    for _ in range(40):
        if len(server.sent_messages) > before:
            break
        await asyncio.sleep(0.05)
    check("非主人 /pause 被拒且照常闲聊", not agent.paused and len(server.sent_messages) == before + 1,
          f"paused={agent.paused}")

    # 断线重连
    await onebot.stop()
    await asyncio.sleep(0.2)
    await onebot.start(agent.handle_event)
    await asyncio.sleep(1.2)
    check("断线后自动重连", server.connection_count >= 2, f"累计连接={server.connection_count}")

    await agent.shutdown()
    await onebot.stop()
    store.close()
    await server.stop()


def test_pure_functions() -> None:
    print("\n[2] 消息解析 / 分段 / CQ 码")
    segs = msg.parse_cq("hi[CQ:at,qq=123][CQ:image,file=a.jpg]你好")
    check("CQ 码解析段数", len(segs) == 4, f"segments={segs}")
    check("CQ 码解析内容", segs[1]["data"]["qq"] == "123")
    check("消息段 -> CQ 往返", msg.segments_to_cq(segs) == "hi[CQ:at,qq=123][CQ:image,file=a.jpg]你好")
    check("数组格式归一化", msg.normalize_message([{"type": "text", "data": {"text": "a"}}])[0]["data"]["text"] == "a")
    check("CQ 转可读文本", msg.strip_cq("看这个[CQ:image,file=x.jpg][CQ:at,qq=7]") == "看这个[图片]@7")
    check("is_at_bot 命中", msg.is_at_bot(segs, 123) is True)
    check("is_at_bot 未命中", msg.is_at_bot(segs, 999) is False)
    check("@全体算命中", msg.is_at_bot(msg.parse_cq("[CQ:at,qq=all]"), 999) is True)
    long_text = "第一句话。" * 30
    pieces = msg.split_reply(long_text, 60, True)
    check("长文本被分段", len(pieces) > 1 and all(len(p) <= 60 for p in pieces), f"{len(pieces)} 段")
    check("多行被拆成多条", len(msg.split_reply("a\nb\nc", 180, True)) == 3)
    check("空回复产出空列表", msg.split_reply("   ", 180, True) == [])

    # 长文（背出师表这类）：默认会炸成几十条，必须能压住
    lines = "\n".join(f"第{i}句喵。" for i in range(60))
    uncapped = msg.split_reply(lines, 40, True)
    capped = msg.split_reply(lines, 40, True, 5)
    check("不加限制时会切成很多条", len(uncapped) >= 50, f"{len(uncapped)} 条")
    check("max_messages 能压住条数", len(capped) <= 5, f"{len(capped)} 条")
    check("压缩条数后内容不丢",
          "".join(capped).split() == "".join(uncapped).split() or
          "".join("".join(capped).split()) == "".join("".join(uncapped).split()),
          "忽略合并时补的分隔空格后逐字比对")


async def test_long_reply() -> None:
    print("\n[7] 长回复分段（背长文专用）")
    cfg = make_offline_config("test_long.db", persona={"typing_style": {
        "enabled": True, "split_on_newline": True, "max_chars_per_message": 40,
        "long_reply_threshold": 200, "long_reply_max_chars_per_message": 350,
        "max_messages_per_reply": 5,
    }})
    store = Store(cfg.resolve("data/test_long.db"))
    llm = LLMClient(cfg["llm"])

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "好"

    llm.chat = fake_chat  # type: ignore[assignment]
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    conv_id = store.get_or_create_conversation("private", 100000001)

    short = "哼，在呢喵。干嘛呀"
    await agent._send_reply("private", 100000001, conv_id, short)
    check("短回复仍按 40 字走（猫娘短句风格不变）", len(onebot.sent) == 1, f"{len(onebot.sent)} 条")

    onebot.sent.clear()
    recital = "先帝创业未半而中道崩殂喵，" * 90  # 约 1170 字
    await agent._send_reply("private", 100000001, conv_id, recital)
    sent_texts = [entry[2] for entry in onebot.sent]  # FakeOneBot 存的是 (chat_type, chat_id, text)
    check("长回复改用大段落", len(sent_texts) <= 5, f"{len(sent_texts)} 条")
    check("长回复单条不超过设定上限", all(len(p) <= 400 for p in sent_texts),
          f"最长 {max((len(p) for p in sent_texts), default=0)} 字")
    check("长回复内容完整送出", sum(len(p) for p in sent_texts) == len(recital),
          f"送出 {sum(len(p) for p in sent_texts)} / 原 {len(recital)}")

    store.close()
    Path(cfg.resolve("data/test_long.db")).unlink(missing_ok=True)


def make_offline_config(db_name: str, **overrides) -> Config:
    """不需要端口的配置，用于纯编排逻辑测试。"""
    cfg = Config.load(ROOT / "config.example.json")
    cfg["onebot"]["ws_url"] = "ws://127.0.0.1:1"
    cfg["llm"]["api_key"] = "test-key"
    cfg["behaviour"]["debounce_seconds"] = 0.02
    cfg["behaviour"]["min_reply_interval_seconds"] = 0
    cfg["behaviour"]["owner_qq"] = 20002
    # 拟人节奏是给真实聊天用的：测试不该为它变慢或超时
    cfg["behaviour"]["typing_delay"] = {"enabled": False}
    cfg["storage"]["db_path"] = str(ROOT / "data" / db_name)
    cfg["storage"]["log_path"] = str(ROOT / "data" / "test.log")
    for section, values in overrides.items():
        cfg[section].update(values)
    return cfg


class FakeOneBot:
    """不带网络的 OneBot 替身。"""

    def __init__(self, images: dict | None = None):
        self.self_id = 10001
        self.nickname = "测试号"
        self.sent: list[tuple[str, int, str]] = []
        self.raw: list[list[dict]] = []
        self.images = images or {}
        self.image_calls: list[str] = []

    async def send_segments(self, chat_type: str, chat_id: int, segments: list[dict]) -> dict:
        text = "".join(s["data"].get("text", "") for s in segments if s["type"] == "text")
        self.sent.append((chat_type, chat_id, text))
        self.raw.append(list(segments))
        return {"message_id": len(self.sent)}

    async def send_text(self, chat_type: str, chat_id: int, text: str) -> dict:
        return await self.send_segments(chat_type, chat_id, [{"type": "text", "data": {"text": text}}])

    async def get_image(self, file: str):
        self.image_calls.append(file)
        return self.images.get(file)

    def last(self) -> str:
        return self.sent[-1][2] if self.sent else ""


async def test_memory() -> None:
    print("\n[3] 长期记忆（滚动摘要压缩）")
    from agent.memory import MemoryManager

    cfg = make_offline_config("test_mem.db", memory={
        "enabled": True, "trigger_messages": 10, "min_chunk_messages": 2,
        "keep_recent": 3, "max_summary_chars": 200, "summary_model": "",
    })
    store = Store(cfg.resolve("data/test_mem.db"))
    llm = LLMClient(cfg["llm"])
    prompts: list[list[dict]] = []

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        prompts.append(messages)
        return "老王养了只叫豆豆的猫，住杭州，下周三约了修水管。"

    llm.chat = fake_chat  # type: ignore[assignment]
    conv_id = store.get_or_create_conversation("private", 20002)
    for i in range(1, 13):
        store.add_message(conv_id, "user", f"第{i}句话", sender_id=20002, sender_name="老王")

    manager = MemoryManager(cfg["memory"], store, llm)
    check("消息数超阈值时判定需要压缩", manager.needs_compression(conv_id) is True)
    pending = manager.pending_messages(conv_id)
    check("待压缩的是滑出窗口的部分", len(pending) == 9, f"待压缩 {len(pending)} 条（12 - keep_recent 3）")
    check("摘要里不含最近保留的消息", all("第10句话" not in m["content"] for m in pending))

    wrote = await manager.compress(conv_id)
    summary, upto = store.get_summary(conv_id)
    check("压缩成功写入摘要", wrote and bool(summary), f"摘要={summary[:30]!r}")
    check("摘要指针指向压缩块最后一条", upto == pending[-1]["id"], f"upto={upto} expect={pending[-1]['id']}")
    check("压缩请求带上了人设以外的专用提示词", "记忆压缩器" in prompts[0][0]["content"])
    # T7：记忆权重必须"情感优先、流水账丢弃"，这条提示词不能被改回去
    compress_prompt = prompts[0][0]["content"]
    check("压缩提示词要求专属互动/情感线优先",
          "专属互动" in compress_prompt and "情感线" in compress_prompt)
    check("压缩提示词要求丢弃流水账",
          "必须丢弃" in compress_prompt and "吃了什么" in compress_prompt)

    # 二次压缩应把已有摘要一起带上（滚动合并）
    for i in range(13, 25):
        store.add_message(conv_id, "user", f"第{i}句话", sender_id=20002, sender_name="老王")
    await manager.compress(conv_id)
    check("二次压缩会合并已有摘要", "已有摘要" in prompts[-1][1]["content"])

    # 编排层：上下文必须带上长期记忆，且不再重复摘要之前的内容
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    conv_id = store.get_or_create_conversation("private", 20002)
    batch = [{"segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
              "sender_id": 20002, "sender_name": "老王", "message_id": 1, "ts": 1759000000}]
    _, ids = agent._store_batch("private", 20002, batch)
    messages, used = await agent._build_messages("private", 20002, batch, ids)
    user_block = messages[-1]["content"]
    check("上下文包含【长期记忆】", "【长期记忆】" in user_block)
    check("摘要内容进入上下文", "豆豆" in user_block)
    check("摘要之前的老消息不再重复出现", "第1句话" not in user_block)
    check("当前消息出现在【新消息】里", "在吗" in user_block)
    check("非多模态时 content 仍是字符串", isinstance(user_block, str) and used is False)
    check("system 提示里说明长期记忆的含义", "长期记忆" in messages[0]["content"])

    # 关闭长期记忆后不应注入
    cfg["memory"]["enabled"] = False
    agent_off = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    messages_off, _ = await agent_off._build_messages("private", 20002, batch, ids)
    check("关闭记忆后不再注入摘要", "【长期记忆】" not in messages_off[-1]["content"])

    # /memory 与 /forget 指令
    events = []
    for text in ("/memory", "/forget"):
        events.append({
            "post_type": "message", "message_type": "private", "message_id": 900 + len(events),
            "self_id": 10001, "user_id": 20002, "time": 1759000500,
            "sender": {"user_id": 20002, "nickname": "老王"},
            "message": [{"type": "text", "data": {"text": text}}],
        })
    cfg["memory"]["enabled"] = True
    agent2 = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    for event in events:
        await agent2.handle_event(event)
    check("/memory 回显长期记忆", "豆豆" in onebot.sent[-2][2], f"回显={onebot.sent[-2][2][:40]!r}")
    check("/forget 清空长期记忆", store.get_summary(conv_id)[0] == "" and "已清空" in onebot.last())

    await agent2.shutdown()
    store.close()
    Path(cfg.resolve("data/test_mem.db")).unlink(missing_ok=True)


async def test_vision() -> None:
    print("\n[4] 图片多模态（含失败降级）")
    # 造一张真的 1x1 PNG
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
    )
    png_path = ROOT / "data" / "test_pixel.png"
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(png)
    big_path = ROOT / "data" / "test_big.png"
    big_path.write_bytes(b"\x00" * (64 * 1024))

    cfg = make_offline_config("test_vision.db", vision={
        "enabled": True, "image_transport": "base64", "max_images_per_message": 2,
        "max_image_bytes": 4096, "image_timeout_seconds": 5, "detail": "auto", "model": "",
    })
    store = Store(cfg.resolve("data/test_vision.db"))
    llm = LLMClient(cfg["llm"])
    requests: list[list[dict]] = []

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        requests.append(messages)
        return "看到了，是一只猫。"

    llm.chat = fake_chat  # type: ignore[assignment]
    onebot = FakeOneBot(images={"pic.png": {"file": str(png_path)}})
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    batch = [{
        "segments": [{"type": "image", "data": {"file": "pic.png"}},
                     {"type": "text", "data": {"text": "这是什么"}}],
        "text": "[图片] 这是什么", "sender_id": 20002, "sender_name": "老王",
        "message_id": 11, "ts": 1759000600,
    }]
    _, ids = agent._store_batch("private", 20002, batch)
    messages, used = await agent._build_messages("private", 20002, batch, ids)
    content = messages[-1]["content"]
    check("启用后 content 变成多模态数组", isinstance(content, list) and used is True)
    check("数组第一段是文本", isinstance(content, list) and content[0]["type"] == "text")
    check("图片段是 image_url 类型", isinstance(content, list) and content[1]["type"] == "image_url")
    data_url = content[1]["image_url"]["url"] if isinstance(content, list) else ""
    check("本地图片被转成 base64 data URL", data_url.startswith("data:image/png;base64,"), data_url[:40])
    check("data URL 解出来就是原图字节", base64.b64decode(data_url.split(",", 1)[1]) == png)
    check("取图走的是 OneBot get_image", onebot.image_calls == ["pic.png"], f"调用={onebot.image_calls}")

    # 超大图片应被跳过，退回纯文本
    onebot2 = FakeOneBot(images={"big.png": {"file": str(big_path)}})
    agent2 = Agent(cfg, store, llm, onebot2)  # type: ignore[arg-type]
    batch2 = [{
        "segments": [{"type": "image", "data": {"file": "big.png"}}],
        "text": "[图片]", "sender_id": 20002, "sender_name": "老王",
        "message_id": 12, "ts": 1759000601,
    }]
    _, ids2 = agent2._store_batch("private", 20002, batch2)
    messages2, used2 = await agent2._build_messages("private", 20002, batch2, ids2)
    check("超限图片被丢弃并退回纯文本", isinstance(messages2[-1]["content"], str) and used2 is False)

    # url 传输模式：直接把链接交给模型
    cfg["vision"]["image_transport"] = "url"
    onebot3 = FakeOneBot()
    agent3 = Agent(cfg, store, llm, onebot3)  # type: ignore[arg-type]
    batch3 = [{
        "segments": [{"type": "image", "data": {"file": "x.jpg", "url": "https://example.com/a.jpg"}},
                     {"type": "text", "data": {"text": "看"}}],
        "text": "[图片] 看", "sender_id": 20002, "sender_name": "老王",
        "message_id": 13, "ts": 1759000602,
    }]
    _, ids3 = agent3._store_batch("private", 20002, batch3)
    messages3, used3 = await agent3._build_messages("private", 20002, batch3, ids3)
    content3 = messages3[-1]["content"]
    check("url 模式直接透传链接",
          isinstance(content3, list) and content3[1]["image_url"]["url"] == "https://example.com/a.jpg",
          f"used={used3}")

    # 带图请求失败 -> 自动降级为纯文本重试，且消息仍然发出
    cfg["vision"]["image_transport"] = "base64"
    onebot4 = FakeOneBot(images={"pic.png": {"file": str(png_path)}})
    agent4 = Agent(cfg, store, llm, onebot4)  # type: ignore[arg-type]
    calls: list[Any] = []

    async def flaky_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        calls.append(messages[-1]["content"])
        if isinstance(messages[-1]["content"], list):
            from agent.llm import LLMError
            raise LLMError("HTTP 400: model does not support image input")
        return "没看到图，但我在。"

    llm.chat = flaky_chat  # type: ignore[assignment]
    batch4 = [{
        "segments": [{"type": "image", "data": {"file": "pic.png"}},
                     {"type": "text", "data": {"text": "在吗"}}],
        "text": "[图片] 在吗", "sender_id": 20002, "sender_name": "老王",
        "message_id": 14, "ts": 1759000603,
    }]
    _, ids4 = agent4._store_batch("private", 20002, batch4)
    messages4, used4 = await agent4._build_messages("private", 20002, batch4, ids4)
    reply = await _call_reply(agent4, ("private", 20002), batch4)
    check("首次带图请求确实是多模态", isinstance(calls[0], list))
    check("失败后自动降级为纯文本重试", len(calls) == 2 and isinstance(calls[1], str),
          f"调用次数={len(calls)}")
    check("降级后回复仍然发出", onebot4.last() == "没看到图，但我在。", f"发出={onebot4.last()!r}")
    check("降级不产生错误计数", agent4.stats["errors"] == 0, f"errors={agent4.stats['errors']}")

    # 关闭视觉 -> 纯文本，且提示词明确说"看不到"
    cfg["vision"]["enabled"] = False
    onebot5 = FakeOneBot(images={"pic.png": {"file": str(png_path)}})
    agent5 = Agent(cfg, store, llm, onebot5)  # type: ignore[arg-type]
    messages5, used5 = await agent5._build_messages("private", 20002, batch4, ids4)
    check("关闭视觉后不发图片", isinstance(messages5[-1]["content"], str) and used5 is False)
    check("关闭视觉时提示词承认看不到", "你看不到内容" in messages5[0]["content"])
    check("关闭视觉时不调用 get_image", onebot5.image_calls == [])

    # 视觉配置校验
    from agent.config import Config as Cfg
    bad = make_offline_config("test_vision.db", vision={"enabled": True, "image_transport": "ftp"})
    problems = bad.validate()
    check("非法 image_transport 被校验拦下", any("image_transport" in p for p in problems), f"{problems}")
    bad_mem = make_offline_config("test_vision.db", memory={"enabled": True, "trigger_messages": 5, "keep_recent": 10})
    check("trigger<=keep 被校验拦下", any("trigger_messages" in p for p in bad_mem.validate()))
    _ = Cfg

    for p in (png_path, big_path):
        p.unlink(missing_ok=True)
    store.close()


async def _call_reply(agent: Agent, key: tuple[str, int], batch: list[dict]) -> str:
    await agent._reply(key, batch)
    return agent.onebot.sent[-1][2] if getattr(agent.onebot, "sent", None) else ""


async def test_active_hours() -> None:
    print("\n[5] 作息时段（夜间静默）")
    from datetime import datetime, timedelta

    from agent import schedule

    # --- 纯函数：确定性 ---
    win = schedule.parse_window("09:00-24:00")
    check("解析 09:00-24:00", win == (540, 1440), f"{win}")

    def at(hh: int, mm: int) -> datetime:
        return datetime(2026, 10, 9, hh, mm, 0)

    cases = [
        ("08:59 静默", at(8, 59), False),
        ("09:00 可回", at(9, 0), True),
        ("12:00 可回", at(12, 0), True),
        ("23:59 可回", at(23, 59), True),
        ("00:30 静默", at(0, 30), False),
        ("03:00 静默", at(3, 0), False),
    ]
    for label, moment, expected in cases:
        got = schedule.in_window(win, moment)
        check(f"09:00-24:00 @ {label}", got is expected, f"got={got}")

    # --- 跨零点窗口 ---
    night = schedule.parse_window("22:00-06:00")
    check("解析跨零点 22:00-06:00", night == (1320, 360), f"{night}")
    for label, moment, expected in [("23:00 可回", at(23, 0), True), ("03:00 可回", at(3, 0), True),
                                    ("12:00 静默", at(12, 0), False)]:
        got = schedule.in_window(night, moment)
        check(f"22:00-06:00 @ {label}", got is expected, f"got={got}")
    check("跨零点被正确标注", "跨零点" in schedule.describe(night))

    # --- 各种写法与边界 ---
    check("空字符串 = 全天", schedule.parse_window("") is None and schedule.in_window(None, at(3, 0)) is True)
    check("全角波浪线可解析", schedule.parse_window("9:00～24:00") == (540, 1440))
    check("起止相同按全天处理（避免误写导致永久静默）", schedule.parse_window("09:00-09:00") is None)
    for bad in ("9点-24点", "09:00", "25:00-26:00", "09:60-10:00", "abc"):
        try:
            schedule.parse_window(bad)
            check(f"非法格式应报错: {bad!r}", False, "没有抛异常")
        except schedule.BadWindow:
            check(f"非法格式被拒绝: {bad!r}", True)

    # --- 配置校验必须拦下非法作息（不能静默变成 7x24）---
    from agent.config import Config

    bad_cfg = Config.load(ROOT / "config.example.json")
    bad_cfg["behaviour"]["active_hours"] = "9点-24点"
    problems = bad_cfg.validate()
    check("非法 active_hours 阻止启动", any("active_hours" in p for p in problems), f"{problems}")

    # --- 编排层：静默时不发消息，但消息仍要入库 ---
    now = datetime.now()
    now_min = now.hour * 60 + now.minute
    silent_spec = f"{(now_min + 1) % 1440 // 60:02d}:{(now_min + 1) % 60:02d}-{now_min // 60:02d}:{now_min % 60:02d}"
    active_spec = f"{now_min // 60:02d}:{now_min % 60:02d}-{(now_min + 2) % 1440 // 60:02d}:{(now_min + 2) % 60:02d}"

    for label, spec, should_reply in (("静默窗口", silent_spec, False), ("活跃窗口", active_spec, True)):
        cfg = make_offline_config(f"test_hours_{'off' if not should_reply else 'on'}.db",
                                  behaviour={"active_hours": spec})
        store = Store(cfg.resolve(f"data/test_hours_{'off' if not should_reply else 'on'}.db"))
        llm = LLMClient(cfg["llm"])

        class CountingLLM:
            calls = 0

        async def counting_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
            CountingLLM.calls += 1
            return "收到啦"

        llm.chat = counting_chat  # type: ignore[assignment]
        onebot = FakeOneBot()
        agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
        batch = [{"segments": [{"type": "text", "data": {"text": "半夜找你"}}], "text": "半夜找你",
                  "sender_id": 100000001, "sender_name": "主人", "message_id": 1, "ts": 1759000000}]
        await agent._reply(("private", 100000001), batch)

        conv_id = store.get_or_create_conversation("private", 100000001)
        stored = store.message_count(conv_id)
        if should_reply:
            check(f"{label}({spec}) 正常回复", len(onebot.sent) == 1 and CountingLLM.calls == 1,
                  f"发出={len(onebot.sent)} 模型调用={CountingLLM.calls}")
        else:
            check(f"{label}({spec}) 不回复任何人", len(onebot.sent) == 0 and CountingLLM.calls == 0,
                  f"发出={len(onebot.sent)} 模型调用={CountingLLM.calls}")
            check("静默期消息仍然入库（保持上下文连续）", stored == 1, f"入库={stored}")
            check("静默计数被记录", agent.stats["silent"] == 1, f"silent={agent.stats['silent']}")
            check("状态栏显示静默中", "静默中" in agent.status_line(), agent.status_line())
        store.close()

    # --- 启动横幅要能看到作息 ---
    cfg = make_offline_config("test_hours_on.db", behaviour={"active_hours": "09:00-24:00"})
    store = Store(cfg.resolve("data/test_hours_on.db"))
    agent = Agent(cfg, store, LLMClient(cfg["llm"]), FakeOneBot())  # type: ignore[arg-type]
    check("active_hours_text 可读", agent.active_hours_text == "09:00-24:00", agent.active_hours_text)
    store.close()
    for name in ("test_hours_off.db", "test_hours_on.db"):
        Path(cfg.resolve(f"data/{name}")).unlink(missing_ok=True)

    _ = timedelta  # 保持导入使用，避免 flake 噪音


async def test_contacts() -> None:
    print("\n[6] 联系人身份备注")
    cfg = make_offline_config("test_contacts.db")
    # 整体替换而不是合并：示例配置里也有 contacts，合并会让断言依赖示例文件的内容
    cfg["contacts"] = {
        "100000004": {"alias": "女朋友", "relation": "女朋友", "notes": "叫她小柚，别敷衍她"},
        "100000001": {"alias": "主人", "relation": "", "notes": ""},
        "badkey": {"alias": "应该被忽略"},
        "1654513476": "不是对象，应该被忽略",
    }
    store = Store(cfg.resolve("data/test_contacts.db"))
    llm = LLMClient(cfg["llm"])

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "好"

    llm.chat = fake_chat  # type: ignore[assignment]
    agent = Agent(cfg, store, llm, FakeOneBot())  # type: ignore[arg-type]

    check("非法键被忽略", "badkey" not in agent.contacts and "1654513476" not in agent.contacts,
          f"已加载={list(agent.contacts)}")
    check("合法联系人已加载", set(agent.contacts) == {"100000004", "100000001"})

    batch = [{"segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
              "sender_id": 100000004, "sender_name": "小柚", "message_id": 1, "ts": 1759000000}]
    _, ids = agent._store_batch("private", 100000004, batch)
    messages, _ = await agent._build_messages("private", 100000004, batch, ids)
    system = messages[0]["content"]
    check("注入【对方身份】段落", "【对方身份】" in system)
    check("备注进入上下文", "女朋友" in system)
    check("补充信息进入上下文", "叫她小柚" in system)

    # 未登记的人不应被注入
    batch2 = [dict(batch[0], sender_id=999999, sender_name="陌生人", message_id=2)]
    _, ids2 = agent._store_batch("private", 999999, batch2)
    messages2, _ = await agent._build_messages("private", 999999, batch2, ids2)
    check("未登记的人不注入身份", "【对方身份】" not in messages2[0]["content"])

    # 群聊里出现认识的人，应当被点名
    batch3 = [{"segments": [{"type": "text", "data": {"text": "大家好"}}], "text": "大家好",
               "sender_id": 100000004, "sender_name": "小柚", "message_id": 3, "ts": 1759000001}]
    _, ids3 = agent._store_batch("group", 55555, batch3)
    messages3, _ = await agent._build_messages("group", 55555, batch3, ids3)
    system3 = messages3[0]["content"]
    check("群聊里认识的人被标注", "100000004" in system3 and "女朋友" in system3, system3[-120:])

    # 配置校验
    bad = make_offline_config("test_contacts.db", contacts={"not-a-qq": {"alias": "x"}})
    check("非数字键被校验拦下", any("contacts" in p for p in bad.validate()), str(bad.validate()))
    bad2 = make_offline_config("test_contacts.db")
    bad2["contacts"] = ["a"]
    check("contacts 非对象被校验拦下", any("contacts" in p for p in bad2.validate()))

    store.close()
    Path(cfg.resolve("data/test_contacts.db")).unlink(missing_ok=True)


async def test_at_sender() -> None:
    print("\n[8] 回复谁就 @ 谁")
    check("文本里的 @数字 转成 at 段", msg.text_to_segments("哼 @123456 你笨蛋")[1]["type"] == "at")
    check("@ 转换的 qq 正确", msg.text_to_segments("哼 @123456 你笨蛋")[1]["data"]["qq"] == "123456")
    check("最多只转 3 个 @",
          sum(1 for s in msg.text_to_segments("@100001 @100002 @100003 @100004 @100005", 3)
              if s["type"] == "at") == 3)
    check("纯文本无 @ 时原样返回", msg.text_to_segments("在呢喵")[0]["data"]["text"] == "在呢喵")

    def segments_of(onebot: "FakeOneBot") -> list[list[dict]]:
        return onebot.raw

    cfg = make_offline_config("test_at.db", behaviour={"at_sender": True})
    store = Store(cfg.resolve("data/test_at.db"))
    llm = LLMClient(cfg["llm"])

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "哼，谁问你了喵"

    llm.chat = fake_chat  # type: ignore[assignment]
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    conv_id = store.get_or_create_conversation("group", 999999999)

    await agent._send_reply("group", 999999999, conv_id, "哼，谁问你了喵", at_qq=2929694410)
    segs = segments_of(onebot)[0]
    check("群聊回复第一条带 at 段", segs[0]["type"] == "at" and segs[0]["data"]["qq"] == "2929694410",
          f"段={[s['type'] for s in segs]}")
    check("at 之后紧跟正文（不留碎片段）",
          len(segs) == 2 and segs[1]["type"] == "text" and "谁问你了" in segs[1]["data"]["text"],
          f"段={[s['type'] for s in segs]}")

    # 私聊不该 @
    onebot.raw.clear()
    priv = store.get_or_create_conversation("private", 2929694410)
    await agent._send_reply("private", 2929694410, priv, "哼，谁问你了喵", at_qq=2929694410)
    segs = segments_of(onebot)[0]
    check("私聊不加 at 段", all(s["type"] != "at" for s in segs), f"段={[s['type'] for s in segs]}")

    # 关掉开关就不 @
    cfg["behaviour"]["at_sender"] = False
    agent_off = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    onebot.raw.clear()
    await agent_off._send_reply("group", 999999999, conv_id, "哼，谁问你了喵", at_qq=2929694410)
    segs = segments_of(onebot)[0]
    check("at_sender=false 时不 @", all(s["type"] != "at" for s in segs))

    # 模型自己已经写了 @对方 -> 不能重复 @
    cfg["behaviour"]["at_sender"] = True
    agent_on = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    onebot.raw.clear()
    await agent_on._send_reply("group", 999999999, conv_id, "@2929694410 哼，你笨蛋喵", at_qq=2929694410)
    segs = segments_of(onebot)[0]
    ats = [s["data"]["qq"] for s in segs if s["type"] == "at"]
    check("模型已 @ 同一人时不重复 @", ats == ["2929694410"], f"at 段={ats}")

    # 多段回复只在第一条 @
    onebot.raw.clear()
    long_reply = "\n".join(["哼，你笨蛋喵"] * 8)  # 8 行 -> 会被切成多条
    await agent_on._send_reply("group", 999999999, conv_id, long_reply, at_qq=2929694410)
    first_has_at = any(s["type"] == "at" for s in segments_of(onebot)[0])
    others_have_at = any(any(s["type"] == "at" for s in seg) for seg in segments_of(onebot)[1:])
    check("分段回复只在第一条 @", len(onebot.raw) > 1 and first_has_at and not others_have_at,
          f"{len(onebot.raw)} 条，第一条有 at={first_has_at}，后续有 at={others_have_at}")

    store.close()
    Path(cfg.resolve("data/test_at.db")).unlink(missing_ok=True)


async def test_emoji() -> None:
    print("\n[9] QQ 表情识别")
    from agent import emoji

    st = emoji.stats()
    if not st["loaded"]:
        # 映射表是从使用者本机 NapCat 生成的（见 NOTICE.md），仓库里不含它。
        # 全新 clone 时还没生成 -> 跳过而不是判失败，否则别人的测试一跑就是红的。
        print("  [skip] 未找到 agent/qq_faces.json —— 先运行 `python tools/gen_face_table.py` 生成后再测")
        print("         （未生成时程序仍可运行，QQ 表情会显示为 [表情#id]）")
        return
    check("映射表已加载", st["count"] > 250, f"{st['count']} 条 @ {st['path']}")

    # 用公认的 OneBot face id 校验，避免表被换错
    for fid, expect in (("0", "惊讶"), ("4", "得意"), ("5", "流泪"), ("14", "微笑"), ("178", "斜眼笑")):
        got = emoji.face_name(fid)
        check(f"id={fid} -> {expect}", got == expect, f"实际={got}")

    check("已知表情标签带名字", emoji.face_label("14") == "[表情:微笑]", emoji.face_label("14"))
    check("未知 id 不瞎猜", emoji.face_label("99999") == "[表情#99999]", emoji.face_label("99999"))
    check("空 id 不崩", emoji.face_label(None) == "[表情#None]", emoji.face_label(None))
    check("表情包标签", emoji.mface_label({}) == "[表情包]")
    check("表情包带名字", emoji.mface_label({"summary": "[动画表情]"}) == "[表情包]",
          emoji.mface_label({"summary": "[动画表情]"}))

    # CQ 码字符串路径
    check("CQ 码里的 face 被还原",
          msg.strip_cq("[CQ:face,id=14]") == "[表情:微笑]", msg.strip_cq("[CQ:face,id=14]"))
    check("CQ 码里的未知 face 保留 id",
          msg.strip_cq("[CQ:face,id=88888]") == "[表情#88888]", msg.strip_cq("[CQ:face,id=88888]"))

    # 消息段数组路径（NapCat 默认上报格式）
    segs = [
        {"type": "text", "data": {"text": "笑死我了"}},
        {"type": "face", "data": {"id": "178"}},
        {"type": "face", "data": {"id": "88888"}},
    ]
    rendered = msg.render_segments(segs)
    check("消息段里的 face 被还原", "[表情:斜眼笑]" in rendered, rendered)
    check("未知 face 保留 id 而不是丢掉", "[表情#88888]" in rendered, rendered)
    check("同一条里的文字不受影响", "笑死我了" in rendered, rendered)

    # Unicode emoji 本来就该原样透传（模型原生就懂）
    uni = msg.render_segments([{"type": "text", "data": {"text": "今天好开心😭😭"}}])
    check("Unicode emoji 原样透传", "😭😭" in uni, uni)

    # 进去看模型上下文里也是还原后的样子
    cfg = make_offline_config("test_emoji.db")
    store = Store(cfg.resolve("data/test_emoji.db"))
    llm = LLMClient(cfg["llm"])

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "好"

    llm.chat = fake_chat  # type: ignore[assignment]
    agent = Agent(cfg, store, llm, FakeOneBot())  # type: ignore[arg-type]
    batch = [{
        "segments": [{"type": "face", "data": {"id": "9"}}],
        "text": msg.render_segments([{"type": "face", "data": {"id": "9"}}]),
        "sender_id": 100000001, "sender_name": "主人", "message_id": 1, "ts": 1759000000,
    }]
    _, ids = agent._store_batch("private", 100000001, batch)
    messages, _ = await agent._build_messages("private", 100000001, batch, ids)
    user_block = messages[-1]["content"]
    check("上下文里表情是名字而不是数字", "[表情:大哭]" in user_block, user_block[-160:])
    check("system 里说明了表情标记的含义", "[表情:名称]" in messages[0]["content"])
    store.close()
    Path(cfg.resolve("data/test_emoji.db")).unlink(missing_ok=True)


async def test_avoid_repeat() -> None:
    print("\n[10] 反重复（发送前查重 + 重生成一次）")
    check("相似度：完全相同 = 1.0", msg.similarity("哼，在呢喵", "哼，在呢喵") == 1.0)
    check("相似度：完全不同偏低", msg.similarity("哼，在呢喵", "今天天气真好啊") < 0.3)
    check("相似度：空串为 0", msg.similarity("", "哼") == 0.0)

    cfg = make_offline_config("test_repeat.db")
    store = Store(cfg.resolve("data/test_repeat.db"))
    llm = LLMClient(cfg["llm"])
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    calls: list[str] = []
    DUP = "哼，本喵才没有很高兴呢"
    FRESH = "尾巴都翘起来了啦，谁、谁稀罕你说话"

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        calls.append(str(messages[-1]["content"]))
        # 第 1 次正常；第 2 次故意重复；第 3 次（被要求换说法）给新句
        if len(calls) == 1:
            return DUP
        if len(calls) == 2:
            return DUP
        return FRESH

    llm.chat = fake_chat  # type: ignore[assignment]

    def batch(i: int, text: str) -> list[dict]:
        return [{"segments": [{"type": "text", "data": {"text": text}}], "text": text,
                 "sender_id": 100000001, "sender_name": "主人", "message_id": i, "ts": 1759000000}]

    await agent._reply(("private", 100000001), batch(1, "在吗"))
    check("第一轮正常回复", onebot.sent[-1][2] == DUP, onebot.sent[-1][2])

    await agent._reply(("private", 100000001), batch(2, "在吗"))
    check("第二轮检测到重复并重生成（多调了一次模型）", len(calls) == 3, f"模型调用 {len(calls)} 次")
    check("发出的是换过说法的版本", onebot.sent[-1][2] == FRESH, onebot.sent[-1][2])
    check("重生成被计入统计", agent.stats["repeat_retry"] == 1, f"repeat_retry={agent.stats['repeat_retry']}")
    check("重生成指令里列出了要避开的原句（只说「换个说法」不够，模型会再给近似句）",
          any("不要再使用下面这些说法" in c and DUP in c for c in calls),
          calls[-1][:90] if calls else "")

    # 关掉开关就不该重生成
    cfg["behaviour"]["avoid_repeat"] = False
    agent_off = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    calls.clear()
    onebot.sent.clear()
    await agent_off._reply(("private", 100000001), batch(3, "在吗"))
    before = len(calls)
    await agent_off._reply(("private", 100000001), batch(4, "在吗"))
    check("关闭开关后不再重生成", len(calls) - before == 1, f"新增模型调用 {len(calls) - before} 次")

    await agent.shutdown()
    store.close()
    Path(cfg.resolve("data/test_repeat.db")).unlink(missing_ok=True)


async def test_self_check() -> None:
    print("\n[11] 自我反思（规则层自省，不加模型调用）")
    cfg = make_offline_config("test_selfcheck.db")
    store = Store(cfg.resolve("data/test_selfcheck.db"))
    llm = LLMClient(cfg["llm"])
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    cases = [
        ("助手腔", "请问需要什么帮助呢？", "哼，谁问你了喵"),
        ("自曝身份", "其实我是一个AI助手", "谁、谁说的，本喵才不是什么程序"),
        ("过短", "哦", "哼，就这么点事也要来烦本喵啊，说清楚点"),
    ]
    retry_instructions: list[str] = []
    for reason, bad, good in cases:
        calls: list[str] = []

        async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
            calls.append(str(messages[-1]["content"]))
            return bad if len(calls) == 1 else good

        llm.chat = fake_chat  # type: ignore[assignment]
        onebot.sent.clear()
        conv = store.get_or_create_conversation("private", 100000001 + len(calls))
        await agent._reply(("private", 100000001), [{
            "segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
            "sender_id": 100000001, "sender_name": "主人", "message_id": 1, "ts": 1759000000,
        }])
        sent = onebot.sent[-1][2]
        check(f"「{reason}」被拦下并纠正", len(calls) == 2 and sent == good,
              f"模型调用 {len(calls)} 次，发出={sent[:30]!r}")
        if len(calls) == 2:
            retry_instructions.append(calls[1])

    check("助手腔的重生成指令指向客服口吻", any("客服" in c for c in retry_instructions),
          str(retry_instructions)[:80])
    check("自曝身份的重生成指令明确禁止承认", any("绝不能承认" in c for c in retry_instructions))
    check("自省被计入统计", agent.stats["self_check_retry"] >= 3, f"{agent.stats['self_check_retry']}")

    # 干净回复不该触发任何额外调用
    calls2: list[str] = []

    async def clean_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        calls2.append("x")
        return "哼，在呢喵，干嘛呀"

    llm.chat = clean_chat  # type: ignore[assignment]
    agent2 = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    await agent2._reply(("private", 100000001), [{
        "segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
        "sender_id": 100000001, "sender_name": "主人", "message_id": 2, "ts": 1759000000,
    }])
    check("正常回复不触发重生成", len(calls2) == 1, f"{len(calls2)} 次调用")

    # 关掉自省就不纠
    cfg["behaviour"]["self_check"] = False
    agent3 = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    calls3: list[str] = []

    async def bad_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        calls3.append("x")
        return "请问需要什么帮助呢？"

    llm.chat = bad_chat  # type: ignore[assignment]
    await agent3._reply(("private", 100000001), [{
        "segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
        "sender_id": 100000001, "sender_name": "主人", "message_id": 3, "ts": 1759000000,
    }])
    check("关闭 self_check 后不再纠正", len(calls3) == 1, f"{len(calls3)} 次调用")

    await agent.shutdown()
    store.close()
    Path(cfg.resolve("data/test_selfcheck.db")).unlink(missing_ok=True)


def reset_test_databases() -> list[str]:
    """清掉本套件自己用的测试库，保证**每次运行结果一致**。

    不清的话第二次运行会失败：`data/test_vision.db` 里留着上一轮的助手回复，
    「反重复」自省层会把它当成重复句、多调一次模型去重生成，于是
    「失败后自动降级为纯文本重试」这条按调用次数断言的用例就红了
    （干净库 152 全绿，第二次跑 151 通过 / 1 失败）。这是测试隔离缺陷，不是产品 bug。
    """
    removed: list[str] = []
    data_dir = ROOT / "data"
    if not data_dir.is_dir():
        return removed
    for pattern in ("test_*.db", "test_*.db-wal", "test_*.db-shm"):
        for path in data_dir.glob(pattern):
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


async def test_whitelist_and_faces() -> None:
    print("\n[12] 私聊白名单 + 表情码输出（八重櫻ovo 风格需要的两项能力）")
    cfg = make_offline_config("test_wl.db")
    store = Store(cfg.resolve("data/test_wl.db"))
    llm = LLMClient(cfg["llm"])
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    def event(sender: int, text: str = "在吗") -> list[dict]:
        return [{"segments": [{"type": "text", "data": {"text": text}}], "text": text,
                 "sender_id": sender, "sender_name": "x", "message_id": 1, "ts": 1759000000}]

    cfg["behaviour"]["private_whitelist"] = []
    check("白名单为空时私聊都回", agent._should_reply("private", {}, [], 10001, 12345))

    cfg["behaviour"]["private_whitelist"] = [100000004]
    check("白名单内的人回复", agent._should_reply("private", {}, [], 10001, 100000004))
    check("白名单外的人不回复", not agent._should_reply("private", {}, [], 10001, 99999))

    cfg["behaviour"]["group_reply_mode"] = "off"
    at_segs = [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": "在吗"}}]
    check("group_reply_mode=off 时群里一律不回",
          not agent._should_reply("group", {"group_id": 5}, at_segs, 10001, 99999))

    # 表情码相关断言依赖 agent/qq_faces.json；它不在仓库里（由使用者本地生成），
    # 所以全新 clone 时必须跳过而不是判失败。
    if not emoji.stats()["loaded"]:
        print("  [skip] 未找到 agent/qq_faces.json —— 跳过表情码输出相关断言")
        print("         （先运行 python tools/gen_face_table.py 生成后再测）")
        await agent.shutdown()
        store.close()
        Path(cfg.resolve("data/test_wl.db")).unlink(missing_ok=True)
        return

    segs = msg.text_to_segments("好困/流泪")
    check("「/流泪」转成 face 段", [s["type"] for s in segs] == ["text", "face"], str(segs))
    check("face id 正确（流泪）", segs[1]["data"]["id"] == str(emoji.face_id_by_name("流泪")),
          str(segs[1]))

    segs = msg.text_to_segments("/猪头")
    check("整条都是表情时只有 face 段", [s["type"] for s in segs] == ["face"], str(segs))

    segs = msg.text_to_segments("不是/胡说八道")
    check("未知表情名不转换（避免误伤普通文本）",
          [s["type"] for s in segs] == ["text"], str(segs))

    segs = msg.text_to_segments("@100000004 好困/流泪")
    check("@ 和表情码能共存", [s["type"] for s in segs] == ["at", "text", "face"], str(segs))

    segs = msg.text_to_segments("/流泪" * 12)
    faces = [s for s in segs if s["type"] == "face"]
    check("表情码数量有上限（防刷屏）", len(faces) <= 6, f"{len(faces)} 个")

    async def fake_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "好困/流泪"

    llm.chat = fake_chat  # type: ignore[assignment]
    onebot.raw.clear()
    await agent._reply(("private", 100000004), event(100000004))
    sent = onebot.raw[-1]
    check("端到端：表情码变成 face 段发出去",
          any(s["type"] == "face" for s in sent), str(sent))

    await agent.shutdown()
    store.close()
    Path(cfg.resolve("data/test_wl.db")).unlink(missing_ok=True)


async def test_time_and_proactive() -> None:
    print("\n[13] 时间感知 + 主动发消息 + 回复条数上限")
    from agent.proactive import ProactiveMessenger

    # --- 时间工具 ---
    check("凌晨 3 点 -> 凌晨", msg.describe_hour(3) == "凌晨")
    check("早上 7 点 -> 早上", msg.describe_hour(7) == "早上")
    check("中午 12 点 -> 中午", msg.describe_hour(12) == "中午")
    check("下午 15 点 -> 下午", msg.describe_hour(15) == "下午")
    check("晚上 20 点 -> 晚上", msg.describe_hour(20) == "晚上")
    check("23 点 -> 深夜", msg.describe_hour(23) == "深夜")
    check("30 秒 -> 刚刚", msg.humanize_gap(30) == "刚刚")
    check("90 分钟 -> 1 小时前", msg.humanize_gap(5400) == "1 小时前")

    import datetime as _dt
    fixed = _dt.datetime(2026, 10, 10, 3, 25).timestamp()
    lines = "\n".join(msg.time_context(now=fixed, last_ts=fixed - 7200))
    check("时间上下文含具体时刻", "2026-10-10 03:25" in lines, lines[:60])
    check("时间上下文含星期", "周" in lines)
    check("时间上下文含时段", "凌晨" in lines)
    check("时间上下文含相隔多久", "2 小时前" in lines)
    check("时间上下文要求不要报时", "不要报时" in lines or "不要**报时" in lines or "不要" in lines)

    # --- 时间真的被注入了 prompt ---
    cfg = make_offline_config("test_proa.db")
    store = Store(cfg.resolve("data/test_proa.db"))
    llm = LLMClient(cfg["llm"])
    onebot = FakeOneBot()
    agent = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]
    messages, _ = await agent._build_messages("private", 100000004, [{
        "segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
        "sender_id": 100000004, "sender_name": "奶龙", "message_id": 1, "ts": 1759000000,
    }])
    sys_text = messages[0]["content"]
    check("prompt 里注入了当前时间", "现在是：" in sys_text)
    check("prompt 里要求不要报时", "不要报时" in sys_text or "不要**报时" in sys_text)

    # --- 回复条数上限 ---
    cfg["persona"]["typing_style"]["max_messages_per_reply"] = 2
    agent2 = Agent(cfg, store, llm, onebot)  # type: ignore[arg-type]

    async def long_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        return "一\n二\n三\n四\n五\n六"

    llm.chat = long_chat  # type: ignore[assignment]
    onebot.raw.clear()
    agent2.cfg["behaviour"]["typing_delay"] = {"enabled": False}
    await agent2._reply(("private", 100000004), [{
        "segments": [{"type": "text", "data": {"text": "在吗"}}], "text": "在吗",
        "sender_id": 100000004, "sender_name": "奶龙", "message_id": 2, "ts": 1759000000,
    }])
    check("回复条数被 max_messages_per_reply 卡住", len(onebot.raw) <= 2, f"{len(onebot.raw)} 条")

    # --- 主动消息的硬限制 ---
    pm = ProactiveMessenger(agent2)
    b = cfg["behaviour"]
    b["proactive"] = {"enabled": True, "targets": [100000004], "min_silence_minutes": 45,
                      "max_per_day": 2, "quiet_hours": "", "chance": 1.0}
    import time as _t
    now = int(_t.time())

    ok, why = pm.should_send_now(100000004, now - 60, "assistant")
    check("静默不够时不主动发", not ok and "分钟" in why, why)

    ok, why = pm.should_send_now(100000004, now - 3600, "user")
    check("最后一条是对方发的 -> 不算主动", not ok and "待回复" in why, why)

    ok, why = pm.should_send_now(99999, now - 3600, "assistant")
    check("不在主动名单里的人不发", not ok and "名单" in why, why)

    ok, why = pm.should_send_now(100000004, now - 3600, "assistant")
    check("条件都满足时应该发", ok, why)

    pm._sent_today["100000004"] = 2
    ok, why = pm.should_send_now(100000004, now - 3600, "assistant")
    check("超过每日上限后不发", not ok and "次数" in why, why)
    pm._sent_today.clear()

    b["proactive"]["quiet_hours"] = "01:00-09:00"
    ok, why = pm.should_send_now(100000004, now - 3600, "assistant")
    check("免打扰时段判定不报错", isinstance(ok, bool), why)

    # 模型输出 SKIP 时真的不发
    async def skip_chat(messages, temperature=None, model=None, max_tokens=None):  # noqa: ANN001
        if "主动" in str(messages[-1].get("content", "")):
            return "SKIP"
        return "在吗"

    llm.chat = skip_chat  # type: ignore[assignment]
    b["proactive"]["chance"] = 1.0
    b["proactive"]["min_silence_minutes"] = 0
    onebot.raw.clear()
    await pm.tick()
    check("模型输出 SKIP 时不发消息", len(onebot.raw) == 0, f"{len(onebot.raw)} 条")

    await agent.shutdown()
    await agent2.shutdown()
    store.close()
    Path(cfg.resolve("data/test_proa.db")).unlink(missing_ok=True)


async def main() -> int:
    removed = reset_test_databases()
    if removed:
        print(f"已清理 {len(removed)} 个上一轮的测试库，保证本次结果可重复\n")
    test_pure_functions()
    await test_ws_and_onebot()
    await test_memory()
    await test_vision()
    await test_active_hours()
    await test_contacts()
    await test_long_reply()
    await test_at_sender()
    await test_emoji()
    await test_avoid_repeat()
    await test_self_check()
    await test_whitelist_and_faces()
    await test_time_and_proactive()
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  失败: {name}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
