"""OneBot v11 客户端：WebSocket 正向连接 + API 调用（带 echo 关联）+ 断线重连。"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Awaitable

from . import message as msg
from .ws_client import WebSocket, WebSocketError

log = logging.getLogger("qqagent.onebot")

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class OneBotClient:
    def __init__(
        self,
        ws_url: str,
        access_token: str = "",
        reconnect_min: float = 2.0,
        reconnect_max: float = 60.0,
    ):
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max

        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._seq = 0
        self._handler: EventHandler | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self.self_id = 0
        self.nickname = ""
        self.ready = asyncio.Event()
        self._api_lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def start(self, handler: EventHandler) -> None:
        self._handler = handler
        self._running = True
        self._task = asyncio.create_task(self._run_forever(), name="onebot")

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_forever(self) -> None:
        delay = self.reconnect_min
        while self._running:
            try:
                await self._connect_once()
                delay = self.reconnect_min
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("连接断开: %s —— %.1fs 后重连", e, delay)
            if not self._running:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.8, self.reconnect_max)

    async def _connect_once(self) -> None:
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        log.info("连接 %s", self.ws_url)
        ws = await WebSocket.connect(self.ws_url, timeout=15, headers=headers)
        self._ws = ws
        self.ready.clear()
        self._event_queue = asyncio.Queue()

        # 关键架构：读循环只负责读+把事件丢进队列；事件处理在独立任务里跑。
        # 若在读循环里直接 await 事件处理器，处理器内部发消息时等待的 API 响应
        # 就永远读不出来 —— 必然死锁。
        read_task = asyncio.create_task(self._read_loop(ws), name="onebot-read")
        dispatch_task = asyncio.create_task(self._dispatch_loop(), name="onebot-dispatch")
        try:
            try:
                info = await self.call("get_login_info", timeout=8)
                if isinstance(info, dict):
                    self.self_id = int(info.get("user_id") or 0)
                    self.nickname = str(info.get("nickname") or "")
                    log.info("已登录 QQ %s (%s)", self.self_id, self.nickname)
            except Exception as e:
                log.warning("get_login_info 失败（不影响收消息）: %s", e)
            self.ready.set()
            await asyncio.wait({read_task, dispatch_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (read_task, dispatch_task):
                if not task.done():
                    task.cancel()
            for task in (read_task, dispatch_task):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(WebSocketError("连接已断开"))
            self._pending.clear()
            self._ws = None
            await ws.close()
            self.ready.clear()

    async def _read_loop(self, ws: WebSocket) -> None:
        async for text in ws.messages():
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                log.debug("无法解析的消息: %s", text[:200])
                continue
            echo = event.get("echo")
            if echo is not None:
                self._resolve_pending(echo, event)
                continue
            await self._event_queue.put(event)

    async def _dispatch_loop(self) -> None:
        while True:
            event = await self._event_queue.get()
            if self._handler:
                try:
                    await self._handler(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("事件处理异常")
            self._event_queue.task_done()

    async def _dispatch(self, event: dict[str, Any]) -> None:
        """兼容旧路径：echo 直接结算，其余交给事件队列。"""
        echo = event.get("echo")
        if echo is not None:
            self._resolve_pending(echo, event)
            return
        await self._event_queue.put(event)

    def _resolve_pending(self, echo: Any, event: dict[str, Any]) -> None:
        fut = self._pending.get(str(echo))
        if not fut or fut.done():
            return
        if event.get("status") in ("failed", "error") and event.get("retcode") not in (0, None):
            fut.set_exception(RuntimeError(f"API 失败: {event.get('message') or event.get('wording')}"))
        else:
            fut.set_result(event.get("data"))

    # ---------- API ----------

    async def call(self, action: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        ws = self._ws
        if ws is None:
            raise WebSocketError("尚未连接")
        async with self._api_lock:
            self._seq += 1
            echo = f"echo-{self._seq}"
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[echo] = fut
        try:
            await ws.send_text(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
            data = await asyncio.wait_for(fut, timeout=timeout)
            return data
        finally:
            self._pending.pop(echo, None)

    async def send_segments(self, chat_type: str, chat_id: int, segments: list[dict[str, Any]]) -> Any:
        if chat_type == "group":
            return await self.call("send_group_msg", {"group_id": chat_id, "message": segments})
        return await self.call("send_private_msg", {"user_id": chat_id, "message": segments})

    async def send_text(self, chat_type: str, chat_id: int, text: str) -> Any:
        return await self.send_segments(chat_type, chat_id, [{"type": "text", "data": {"text": text}}])

    async def send_cq(self, chat_type: str, chat_id: int, cq: str) -> Any:
        return await self.send_segments(chat_type, chat_id, msg.parse_cq(cq))

    async def delete_msg(self, message_id: int) -> Any:
        return await self.call("delete_msg", {"message_id": int(message_id)})

    async def get_group_member_info(self, group_id: int, user_id: int) -> Any:
        return await self.call("get_group_member_info", {"group_id": group_id, "user_id": user_id, "no_cache": False})

    async def get_image(self, file: str) -> Any:
        """把消息里的图片 file 标识解析成真实 URL / 本地路径。"""
        return await self.call("get_image", {"file": file}, timeout=15)
