"""从 NapCat 自带的映射表里提取「QQ 表情 id -> 名称」，生成 agent/qq_faces.json。

为什么这么做：
- QQ 系统表情在 OneBot 报文里只是 `[CQ:face,id=4]` 这样一个数字。不做映射，
  机器人只知道"对方发了个表情"，不知道是哪个——等于没看懂。
- 自己手写映射表容易错，错一个 id 就是曲解别人的情绪。
- NapCat 的 napcat.mjs 里带着腾讯侧的表情表（`QSid` 就是 CQ 码里的 face id），
  用它做数据源，并用公认的 OneBot 表情映射交叉校验。

    python tools/gen_face_table.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agent" / "qq_faces.json"
SOURCES = [
    Path(r"<NAPCAT_DIR>\napcat.mjs"),
]

ENTRY = re.compile(r'\{"QSid":"(\d+)","QDes":"([^"]+)"')

# 公认的 OneBot / go-cqhttp face.json 标准映射，用来校验提取结果
EXPECTED = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意", "5": "流泪",
    "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭", "10": "尴尬", "11": "发怒",
    "12": "调皮", "13": "呲牙", "14": "微笑", "15": "难过",
}


def main() -> int:
    src = next((p for p in SOURCES if p.exists()), None)
    if src is None:
        print("[x] 找不到 napcat.mjs，无法提取。")
        print("    路径候选:", [str(p) for p in SOURCES])
        return 1

    text = src.read_text(encoding="utf-8", errors="replace")
    faces: dict[str, str] = {}
    for m in ENTRY.finditer(text):
        qid, des = m.group(1), m.group(2).lstrip("/").strip()
        if qid not in faces and des:
            faces[qid] = des

    if not faces:
        print("[x] 没提取到任何条目（NapCat 版本可能改了表结构）")
        return 1

    hit = sum(1 for k, v in EXPECTED.items() if faces.get(k) == v)
    print(f"[i] 来源: {src}")
    print(f"[i] 提取 {len(faces)} 条")
    print(f"[i] 标准映射校验: {hit}/{len(EXPECTED)} 命中")
    if hit < len(EXPECTED):
        bad = {k: (v, faces.get(k)) for k, v in EXPECTED.items() if faces.get(k) != v}
        print(f"[x] 校验不通过，说明 id 语义可能不是 face id，拒绝写入: {bad}")
        return 1

    OUT.write_text(json.dumps(faces, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    size = OUT.stat().st_size
    print(f"[ok] 已写入 {OUT}（{size} 字节）")
    for qid in ("0", "14", "74", "178", "450"):
        print(f"     id={qid} -> {faces.get(qid, '(无)')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
