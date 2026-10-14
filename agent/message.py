"""OneBot 消息段工具：CQ 码解析/生成、消息段归一化、上下文渲染。"""
from __future__ import annotations

import re
import time
from typing import Any

from . import emoji

CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_\-]+)((?:,[^\]]*)?)\]")


def parse_cq(text: str) -> list[dict[str, Any]]:
    """把 CQ 码字符串切成消息段列表。"""
    segments: list[dict[str, Any]] = []
    pos = 0
    for m in CQ_RE.finditer(text or ""):
        if m.start() > pos:
            segments.append({"type": "text", "data": {"text": text[pos:m.start()]}})
        seg_type = m.group(1)
        params: dict[str, str] = {}
        raw = m.group(2).lstrip(",")
        if raw:
            for pair in raw.split(","):
                k, _, v = pair.partition("=")
                if k:
                    params[k] = v
        segments.append({"type": seg_type, "data": params})
        pos = m.end()
    if pos < len(text or ""):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    return segments or [{"type": "text", "data": {"text": ""}}]


def normalize_message(raw: Any) -> list[dict[str, Any]]:
    """兼容 string(CQ码) / array(消息段数组) 两种格式，统一成消息段列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return parse_cq(raw)
    if isinstance(raw, list):
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            seg_type = str(item.get("type", "text"))
            data = item.get("data") or {}
            if not isinstance(data, dict):
                data = {"value": data}
            out.append({"type": seg_type, "data": {k: v for k, v in data.items()}})
        return out
    return [{"type": "text", "data": {"text": str(raw)}}]


def cq_escape(value: str) -> str:
    return (value or "").replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;").replace(",", "&#44;")


def segments_to_cq(segments: list[dict[str, Any]]) -> str:
    """把消息段列表还原成 CQ 字符串，用于以字符串方式发送。"""
    parts: list[str] = []
    for seg in segments or []:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            parts.append(f"[CQ:at,qq={data.get('qq', '')}]")
        elif seg_type == "image":
            parts.append(f"[CQ:image,file={data.get('file', '')}]")
        elif seg_type == "face":
            parts.append(f"[CQ:face,id={data.get('id', '')}]")
        else:
            params = ",".join(f"{k}={cq_escape(str(v))}" for k, v in data.items())
            parts.append(f"[CQ:{seg_type}{',' + params if params else ''}]")
    return "".join(parts)


def strip_cq(text: str) -> str:
    """给模型看的历史消息里，把 CQ 码转成人能读的形式。"""
    def repl(m: re.Match) -> str:
        seg_type = m.group(1)
        raw = m.group(2).lstrip(",")
        params = dict(pair.split("=", 1) for pair in raw.split(",") if "=" in pair) if raw else {}
        if seg_type == "at":
            qq = params.get("qq", "")
            return "[@全体]" if qq == "all" else f"@{qq}"
        if seg_type == "image":
            return "[图片]"
        if seg_type == "face":
            return emoji.face_label(params.get("id"))
        if seg_type == "mface":
            return "[表情包]"
        if seg_type == "reply":
            return ""
        if seg_type == "record":
            return "[语音]"
        if seg_type == "video":
            return "[视频]"
        if seg_type == "file":
            return "[文件]"
        return f"[{seg_type}]"
    return CQ_RE.sub(repl, text or "")


def render_segments(segments: list[dict[str, Any]]) -> str:
    """消息段 -> 可读纯文本（含 @QQ、[图片]），用于塞进模型上下文。"""
    parts: list[str] = []
    for seg in segments or []:
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text", "")))
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            parts.append("[@全体成员]" if qq == "all" else f"@{qq}")
        elif seg_type == "image":
            summary = data.get("summary") or "[图片]"
            parts.append(str(summary))
        elif seg_type == "face":
            parts.append(emoji.face_label(data.get("id")))
        elif seg_type == "mface":
            parts.append(emoji.mface_label(data))
        elif seg_type == "reply":
            parts.append("[引用]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
        elif seg_type == "file":
            parts.append(f"[文件:{data.get('name', '')}]")
        elif seg_type == "json":
            parts.append("[卡片消息]")
        else:
            parts.append(f"[{seg_type}]")
    text = "".join(parts).strip()
    return re.sub(r"\s+", " ", text)


def is_at_bot(segments: list[dict[str, Any]], self_id: int) -> bool:
    """消息里是否 @ 了机器人自己（含 @全体）。"""
    for seg in segments or []:
        if seg.get("type") == "at":
            qq = str((seg.get("data") or {}).get("qq", ""))
            if qq in ("all", str(self_id)):
                return True
    return False


def reply_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in (segments or []) if s.get("type") == "reply"]


def extract_text(segments: list[dict[str, Any]]) -> str:
    return "".join(str((s.get("data") or {}).get("text", "")) for s in (segments or []) if s.get("type") == "text")


def strip_at_prefix(text: str, keywords: list[str]) -> str:
    out = text.strip()
    for kw in keywords or []:
        if kw and out.startswith(kw):
            out = out[len(kw):].strip()
    return out


def split_reply(text: str, max_chars: int, split_on_newline: bool, max_messages: int = 0) -> list[str]:
    """把模型的整段回复切成多条 QQ 消息，模拟真人分段发送。

    max_messages > 0 时限制最多切成几条：超出就合并重排，避免背长文时
    一条回复炸成几十条消息（既像炸群，也是风控敏感行为）。
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0:
        return [text]
    chunks: list[str] = []
    if split_on_newline:
        for line in text.split("\n"):
            line = line.strip()
            if line:
                chunks.extend(_chunk(line, max_chars))
    else:
        chunks = _chunk(text, max_chars)
    chunks = [c for c in chunks if c.strip()]
    if max_messages > 0 and len(chunks) > max_messages:
        # 超出条数上限时重新均分。两个要求必须同时满足：
        #   1) **内容一个字都不能丢**（背长文场景，截断尾巴不可接受）
        #   2) 用空格重新连接，不要在合并时把空格吃掉——否则
        #      "累了就歇着 别硬撑 抱抱你" 会变成 "累了就歇着别硬撑抱抱你" 这种一坨，
        #      比多发一条更像机器人。
        joined = " ".join(c.strip() for c in chunks if c.strip())
        per = max(max_chars, len(joined) // max_messages + 1)
        merged = _chunk(joined, per)
        guard = 0
        while len(merged) > max_messages and guard < 50:
            guard += 1
            per += max(1, len(joined) // (max_messages * 2))
            merged = _chunk(joined, per)
        chunks = merged
    return chunks


# 表情码：风格档案里的核心表达手段（7.8% 的消息靠它表达情绪）。
# 模型写 "/流泪"，要转成真正的 QQ 表情段，否则对方只会看到一串文字。
# 只有**能在表情表里精确查到**的名字才转换——这样不会误伤普通文本里的 "/"。
FACE_TEXT_RE = re.compile(r"/([\u4e00-\u9fff]{2,6})")


def _face_spans(text: str) -> list[tuple[int, int]]:
    """文本里所有**确实能转成表情**的 `/表情名` 区间。"""
    from . import emoji

    spans: list[tuple[int, int]] = []
    for m in FACE_TEXT_RE.finditer(text or ""):
        if emoji.face_id_by_name(m.group(1)) is not None:
            spans.append((m.start(), m.end()))
    return spans


def _protect_face_cut(text: str, cut: int) -> int:
    """把切点往回挪，避免把一个 /表情名 从中间切断。

    实测过的 bug：`/我方了` 被切成 `/我` + `方了`，前半段匹配不上表情表，
    对方收到的就是一串乱码文字，而不是表情。
    """
    for start, end in _face_spans(text):
        if start < cut < end:
            return start
    return cut


def _chunk(text: str, max_chars: int) -> list[str]:
    """按最大字数切块。优先在标点、空格处切，且绝不切断表情码。"""
    if len(text) <= max_chars:
        return [text]
    out: list[str] = []
    guard = 0
    while len(text) > max_chars and guard < 200:
        guard += 1
        window = text[:max_chars]
        cut = max(window.rfind("。"), window.rfind("！"), window.rfind("？"),
                  window.rfind("."), window.rfind("!"), window.rfind("?"),
                  window.rfind("，"), window.rfind(","), window.rfind(" "))
        if cut < max_chars // 3:
            cut = max_chars
        else:
            cut += 1
        cut = _protect_face_cut(text, cut)
        if cut <= 0:  # 保护后没地方可切，只能硬切，避免死循环
            cut = max_chars
            cut = _protect_face_cut(text, cut) or max_chars
        out.append(text[:cut].strip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return [c for c in out if c]


def now_ts() -> int:
    return int(time.time())


_WEEK = "一二三四五六日"


def describe_hour(hour: int) -> str:
    """把 0-23 时映射成中文时段说法。语气分类要贴合中文习惯。"""
    if 0 <= hour < 5:
        return "凌晨"
    if 5 <= hour < 9:
        return "早上"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def humanize_gap(seconds: float) -> str:
    """把时间差说成人话，喂给模型用。"""
    s = max(0.0, float(seconds))
    if s < 60:
        return "刚刚"
    m = int(s // 60)
    if m < 60:
        return f"{m} 分钟前"
    h = m // 60
    if h < 24:
        return f"{h} 小时前"
    d = h // 24
    return f"{d} 天前"


def time_context(now: float | None = None, last_ts: float | None = None) -> list[str]:
    """给模型的时间信息。

    **为什么必须显式注入**：历史消息虽然带 `[时间]` 前缀，但那是"过去某条消息的时间"，
    模型拿它推不出"现在几点"。实测它会照着昨晚的记录说"都一点了"，而当时是上午。
    """
    t = time.localtime(now if now is not None else time.time())
    ref = float(now) if now is not None else time.time()
    lines = [
        f"- 现在是：{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} "
        f"{t.tm_hour:02d}:{t.tm_min:02d} 周{_WEEK[t.tm_wday]}（{describe_hour(t.tm_hour)}）"
    ]
    if last_ts:
        lines.append(f"- 最近一条消息是 {humanize_gap(ref - float(last_ts))}")
    lines.append(
        "- 时间要**自然融入**，不要报时。比如凌晨可以提一句「这么晚还不睡」，"
        "白天就别硬提时间；问「在忙吗」这类话之前先看现在几点合不合适。"
    )
    return lines


# 模型习惯把 @ 写成纯文本 "@2929694410"，那样对方收不到任何通知。发送前转成真 at 段。
AT_TEXT_RE = re.compile(r"@(\d{5,12})")


def text_to_segments(text: str, max_ats: int = 3, max_faces: int = 6) -> list[dict[str, Any]]:
    """把回复正文转成消息段：@数字 → at 段，/表情名 → face 段。

    max_ats / max_faces 都是防刷屏上限——模型偶尔会连着写一串。
    """
    from . import emoji  # 局部导入：避免 message ↔ emoji 的循环依赖

    text = text or ""

    # 先把两类标记都找出来，再按位置统一切分，避免互相干扰
    marks: list[tuple[int, int, dict[str, Any]]] = []
    for m in list(AT_TEXT_RE.finditer(text))[:max_ats]:
        marks.append((m.start(), m.end(), {"type": "at", "data": {"qq": m.group(1)}}))
    faces = 0
    for m in FACE_TEXT_RE.finditer(text):
        if faces >= max_faces:
            break
        fid = emoji.face_id_by_name(m.group(1))
        if fid is None:
            continue
        marks.append((m.start(), m.end(), {"type": "face", "data": {"id": str(fid)}}))
        faces += 1
    marks.sort(key=lambda x: x[0])

    segments: list[dict[str, Any]] = []
    pos = 0
    for start, end, seg in marks:
        if start < pos:  # 重叠（理论上不会，防御性处理）
            continue
        if start > pos:
            segments.append({"type": "text", "data": {"text": text[pos:start]}})
        segments.append(seg)
        pos = end
    if pos < len(text):
        segments.append({"type": "text", "data": {"text": text[pos:]}})
    segments = [s for s in segments if s["type"] != "text" or s["data"].get("text")]
    return segments or [{"type": "text", "data": {"text": text}}]


def mentioned_qqs(segments: list[dict[str, Any]]) -> set[str]:
    return {str((s.get("data") or {}).get("qq", "")) for s in segments or [] if s.get("type") == "at"}


def similarity(a: str, b: str) -> float:
    """两句回复的粗相似度（字符集合 Jaccard）。

    只用来判断"是不是又说了同一句话"，不追求语义级准确：
    宁可偶尔漏判，也不要误判导致频繁重生成（每次重生成都是一次模型调用）。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / max(len(sa | sb), 1)

def fmt_clock(ts: int, now: float | None = None) -> str:
    """历史消息的时间前缀。

    **跨天必须标出日期**：只给 `HH:MM` 的话，昨晚 03:25 的消息和今早 03:25 长得一样，
    模型会把它当成"现在"——实测它张口就说「都三点多了」，而当时是上午 11 点。
    今天只给时刻（够用且省 token），昨天标「昨天」，更早标「M-D」。
    """
    import datetime as _dt

    ref = _dt.datetime.fromtimestamp(now if now is not None else time.time())
    t = _dt.datetime.fromtimestamp(ts)
    if t.date() == ref.date():
        return t.strftime("%H:%M")
    if t.date() == (ref.date() - _dt.timedelta(days=1)):
        return t.strftime("昨天 %H:%M")
    return t.strftime("%m-%d %H:%M")
