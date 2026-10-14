"""看一眼运行状态：单实例锁、kv 表（有没有人设覆盖值）、各会话消息数、长期记忆摘要。

    python tools/peek_state.py
"""
from __future__ import annotations

import json
import sqlite3
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from agent.config import Config
    cfg = Config.load(project_root=ROOT)
    print(f"  配置文件 : {cfg.source}")
    db = Path(cfg["storage"]["db_path"])

    print("=== 配置 ===")
    print(f"  人设名        : {cfg['persona']['name']}")
    print(f"  temperature   : {cfg['llm']['temperature']}")
    print(f"  max_tokens    : {cfg['llm']['max_tokens']}")
    print(f"  单条字数上限  : {cfg['persona']['typing_style']['max_chars_per_message']}")
    print(f"  群聊策略      : {cfg['behaviour']['group_reply_mode']}")
    print(f"  主人 QQ       : {cfg['behaviour']['owner_qq'] or '未设置（管理指令关闭）'}")

    print("=== 单实例锁 127.0.0.1:39997 ===")
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 39997))
        print("  已被占用 -> 有实例正在运行")
    except OSError:
        print("  空闲 -> 当前没有实例在运行")
    finally:
        s.close()

    if not db.exists():
        print(f"=== 数据库不存在: {db} ===")
        return 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    print("=== kv 表 ===")
    rows = conn.execute("SELECT key, value FROM kv").fetchall()
    if not rows:
        print("  空")
    for r in rows:
        value = r["value"]
        if r["key"] == "persona":
            print(f"  persona = {value[:70]}...  <-- 存在！它会覆盖 config.json 的人设")
        else:
            print(f"  {r['key']} = {value[:70]}")

    print("=== 会话 ===")
    convs = conn.execute(
        "SELECT c.id, c.chat_type, c.chat_id, "
        "(SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS n, "
        "(SELECT content FROM summaries s WHERE s.conv_id=c.id) AS summary "
        "FROM conversations c ORDER BY c.updated_at DESC"
    ).fetchall()
    if not convs:
        print("  还没有任何会话")
    for c in convs:
        has = "有长期记忆" if c["summary"] else "无长期记忆"
        print(f"  #{c['id']} {c['chat_type']}/{c['chat_id']}  消息 {c['n']} 条  {has}")

    print("=== 最近 6 条消息 ===")
    for m in conn.execute(
        "SELECT conv_id, role, sender_name, content, created_at FROM messages ORDER BY id DESC LIMIT 6"
    ).fetchall():
        import time
        clock = time.strftime("%H:%M", time.localtime(m["created_at"]))
        who = m["sender_name"] or m["role"]
        print(f"  [{clock}] #{m['conv_id']} {who}: {m['content'][:60]}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
