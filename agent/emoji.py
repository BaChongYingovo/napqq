"""把 QQ 系统表情的 id 还原成人能看懂的名字。

QQ 表情在 OneBot 报文里只是 `[CQ:face,id=4]` 这样的数字。不做映射，机器人只知道
"对方发了个表情"，不知道是哪个，等于没看懂情绪。映射表见 qq_faces.json，
由 tools/gen_face_table.py 从 NapCat 自带表提取并交叉校验。

未收录的 id 会退化成 `[表情#id]`，绝不猜——猜错等于曲解别人的情绪。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_TABLE_PATH = Path(__file__).with_name("qq_faces.json")


@lru_cache(maxsize=1)
def faces() -> dict[str, str]:
    try:
        raw = json.loads(_TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def face_name(face_id: Any) -> str:
    """查 id 对应的表情名；查不到返回空串。"""
    key = str(face_id if face_id is not None else "").strip()
    return faces().get(key, "")


def face_label(face_id: Any) -> str:
    """给模型看的标签：已知用名字，未知保留 id（不猜）。"""
    name = face_name(face_id)
    return f"[表情:{name}]" if name else f"[表情#{face_id}]"


_NAME_TO_ID: dict[str, int] | None = None


def name_to_id() -> dict[str, int]:
    """反向表：表情名 → QQ 表情 id。用于让模型**发出**表情，而不只是识别。

    同一个名字可能对应多个 id（新旧版本、相似表情），只保留 id 最小的那个，
    因为旧版表情更符合"有年代感"的风格，且兼容性更好。
    """
    global _NAME_TO_ID
    if _NAME_TO_ID is None:
        table: dict[str, int] = {}
        for key, name in faces().items():
            try:
                fid = int(key)
            except (TypeError, ValueError):
                continue
            cur = table.get(name)
            if cur is None or fid < cur:
                table[name] = fid
        _NAME_TO_ID = table
    return _NAME_TO_ID


def face_id_by_name(name: str) -> int | None:
    return name_to_id().get((name or "").strip())


def mface_label(data: dict[str, Any]) -> str:
    """商城表情包。能拿到 summary 就用它，否则给个笼统标签。"""
    summary = str(data.get("summary") or "").strip()
    if summary and summary not in ("[表情]", "[动画表情]"):
        return f"[表情包:{summary.strip('[]')}]"
    return "[表情包]"


def stats() -> dict[str, Any]:
    table = faces()
    return {"count": len(table), "path": str(_TABLE_PATH), "loaded": bool(table)}
