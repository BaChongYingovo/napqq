"""极简 WebSocket 客户端（RFC6455），只用标准库。

只实现本场景需要的能力：文本帧、分片重组、ping/pong、close、mask。
不实现：扩展协商（permessage-deflate）、二进制业务消息（会被安全丢弃）、TLS 之外的重定向。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
from typing import AsyncIterator
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME = 16 * 1024 * 1024


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._send_lock = asyncio.Lock()

    # ---------- 建连 ----------

    @classmethod
    async def connect(cls, url: str, timeout: float = 15.0, headers: dict | None = None) -> "WebSocket":
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"不支持的 scheme: {parsed.scheme}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        ssl_ctx = None
        if parsed.scheme == "wss":
            ssl_ctx = ssl.create_default_context()

        conn = asyncio.open_connection(host, port, ssl=ssl_ctx)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if headers:
            lines.extend(f"{k}: {v}" for k, v in headers.items())
        request = "\r\n".join(lines) + "\r\n\r\n"
        writer.write(request.encode("utf-8"))
        await writer.drain()

        status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not status_line:
            writer.close()
            raise WebSocketError("握手阶段连接被关闭")
        parts = status_line.decode("latin-1").strip().split(" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        accept_expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()

        resp_headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin-1").partition(":")
            resp_headers[k.strip().lower()] = v.strip()

        if code != 101:
            body = await reader.read(512)
            writer.close()
            raise WebSocketError(f"握手失败 HTTP {code}: {body[:200]!r}")
        if resp_headers.get("sec-websocket-accept") != accept_expected:
            writer.close()
            raise WebSocketError("Sec-WebSocket-Accept 校验失败")
        return cls(reader, writer)

    # ---------- 收发 ----------

    async def _write_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        if self._closed:
            raise WebSocketError("连接已关闭")
        header = bytearray()
        header.append((0x80 if fin else 0x00) | opcode)
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            header.append(mask_bit | length)
        elif length < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        async with self._send_lock:
            self._writer.write(bytes(header) + masked)
            await self._writer.drain()

    async def send_text(self, text: str) -> None:
        await self._write_frame(OP_TEXT, text.encode("utf-8"))

    async def _read_exact(self, n: int) -> bytes:
        data = await self._reader.readexactly(n)
        return data

    async def _read_frame(self) -> tuple[int, bool, bytes]:
        b1, b2 = await self._read_exact(2)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._read_exact(8))[0]
        if length > MAX_FRAME:
            raise WebSocketError(f"帧过大: {length}")
        mask = await self._read_exact(4) if masked else b""
        payload = await self._read_exact(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, fin, payload

    async def messages(self) -> AsyncIterator[str]:
        """产出文本消息；自动回 pong，自动处理分片。"""
        buffer = bytearray()
        while not self._closed:
            opcode, fin, payload = await self._read_frame()
            if opcode == OP_TEXT:
                if fin:
                    yield payload.decode("utf-8", errors="replace")
                else:
                    buffer = bytearray(payload)
            elif opcode == OP_CONT:
                buffer += payload
                if fin:
                    yield buffer.decode("utf-8", errors="replace")
                    buffer = bytearray()
            elif opcode == OP_PING:
                await self._write_frame(OP_PONG, payload)
            elif opcode == OP_PONG:
                continue
            elif opcode == OP_CLOSE:
                self._closed = True
                raise WebSocketError("服务端发起关闭")
            elif opcode == OP_BINARY:
                continue

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(self._write_frame(OP_CLOSE, struct.pack("!H", 1000)), timeout=2)
        except Exception:
            pass
        try:
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), timeout=3)
        except Exception:
            pass
