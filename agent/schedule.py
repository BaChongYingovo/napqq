"""作息时段：决定机器人什么时候可以回复。

设计要点：
- 支持跨零点窗口（如 "22:00-06:00"），不只是同一天内的区间。
- 解析失败**不会**静默变成"全天在线"——调用方应当直接拒绝启动，
  否则用户写错一个字符就以为自己受保护，实际是 7x24 小时在线。
- 只有纯函数 + 显式传入的当前时间，方便确定性测试。
"""
from __future__ import annotations

import re
from datetime import datetime

# 接受半角/全角/波浪线等多种分隔写法
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*[-~～—–至到]\s*(\d{1,2})\s*:\s*(\d{2})\s*$"
)

ALWAYS_ACTIVE: None = None


class BadWindow(ValueError):
    """作息格式无法解析。"""


def _to_minutes(hour: int, minute: int) -> int:
    if minute > 59:
        raise BadWindow(f"分钟数非法: {minute}")
    if hour > 24 or (hour == 24 and minute != 0):
        raise BadWindow(f"小时数非法: {hour}")
    return hour * 60 + minute


def parse_window(spec: str | None) -> tuple[int, int] | None:
    """把 '09:00-24:00' 解析成 (start_minutes, end_minutes)。

    空字符串/None 表示不限制（全天在线），返回 None。
    格式非法抛 BadWindow，而不是回退成"全天"。
    """
    if spec is None:
        return None
    text = str(spec).strip()
    if not text:
        return None
    match = _WINDOW_RE.match(text)
    if not match:
        raise BadWindow(f"作息格式无法解析: {spec!r}（正确写法如 \"09:00-24:00\" 或 \"22:00-06:00\"）")
    sh, sm, eh, em = (int(g) for g in match.groups())
    start = _to_minutes(sh, sm)
    end = _to_minutes(eh, em)
    if start == end:
        # 起止相同视为不限制，避免用户误写导致永久静默
        return None
    return start, end


def in_window(window: tuple[int, int] | None, now: datetime | None = None) -> bool:
    """当前是否在可回复时段内。window 为 None 表示全天可回复。"""
    if window is None:
        return True
    start, end = window
    moment = now or datetime.now()
    now_min = moment.hour * 60 + moment.minute
    if start < end:
        return start <= now_min < end
    # 跨零点：例如 22:00-06:00
    return now_min >= start or now_min < end


def describe(window: tuple[int, int] | None) -> str:
    if window is None:
        return "全天（未限制）"
    start, end = window

    def fmt(v: int) -> str:
        return f"{v // 60:02d}:{v % 60:02d}"

    return f"{fmt(start)}-{fmt(end)}" + ("（跨零点）" if start > end else "")


def is_silent(spec: str | None, now: datetime | None = None) -> bool:
    return not in_window(parse_window(spec), now)
