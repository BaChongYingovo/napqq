"""人设切换。

一次只能启用一套人设（一个 agent 进程只有一个人设），所以切换＝改配置里的
`persona` 和 `behaviour.private_whitelist`，然后重启 agent。

    python tools/use_persona.py --list
    python tools/use_persona.py 拿破仑 --target 100000003
    python tools/use_persona.py 小喵                     # 只换人设，不动对象
    python tools/use_persona.py 八重櫻ovo --target 100000004 --dry

人设文件与元数据都在用户数据目录（不进项目目录）：
    <userdata>/persona-<名字>.txt   人设正文
    <userdata>/personas.json        每套人设的排版参数
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.paths import user_data_dir  # noqa: E402

DEFAULT_META = {
    "小喵": {"file": "persona-小喵.txt", "max_chars": 40, "max_messages": 5,
             "desc": "猫娘 + 高浓度傲娇，短句连发"},
    "八重櫻ovo": {"file": "persona-八重櫻ovo.txt", "max_chars": 9, "max_messages": 2,
                  "desc": "本人风格复刻：碎、零标点、表情码收尾"},
    "拿破仑": {"file": "persona-拿破仑.txt", "max_chars": 30, "max_messages": 3,
               "desc": "30 岁拿破仑，口语、命令式、打仗打比方"},
}


def meta_path() -> Path:
    """人设索引：用户数据目录优先，其次仓库自带。"""
    local = user_data_dir() / "personas.json"
    if local.exists():
        return local
    bundled = ROOT / "personas" / "personas.json"
    return bundled if bundled.exists() else local


def load_meta() -> dict:
    p = meta_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except json.JSONDecodeError:
            pass
    p.write_text(json.dumps(DEFAULT_META, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return dict(DEFAULT_META)


def persona_text(name: str, meta: dict) -> str | None:
    """找人设正文：先用户数据目录（自己改的），再仓库自带的 personas/。"""
    entry = meta.get(name)
    if not entry:
        return None
    fn = str(entry.get("file", f"{name}.txt"))
    for cand in (user_data_dir() / fn, user_data_dir() / f"persona-{name}.txt",
                 ROOT / "personas" / fn, ROOT / "personas" / f"{name}.txt"):
        if cand.exists():
            return cand.read_text(encoding="utf-8")
    return None


def cmd_list(meta: dict) -> int:
    cfg_p = user_data_dir() / "config.json"
    cur = ""
    if cfg_p.exists():
        cur = json.loads(cfg_p.read_text(encoding="utf-8")).get("persona", {}).get("name", "")
    print("可用人设：")
    print(f"  {'名字':<12}{'单条':>6}{'每轮':>6}   说明")
    for name, e in meta.items():
        mark = " ←当前" if name == cur else ""
        print(f"  {name:<12}{e.get('max_chars', '?'):>5}字{e.get('max_messages', '?'):>5}条   "
              f"{e.get('desc', '')}{mark}")
    print()
    print(f"当前启用：{cur or '（未知）'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="切换人设")
    ap.add_argument("name", nargs="?", help="人设名字")
    ap.add_argument("--target", type=int, nargs="+",
                    help="同时把私聊白名单换成这些 QQ（可给多个）")
    ap.add_argument("--dry", action="store_true", help="只看结果不写配置")
    ap.add_argument("--list", action="store_true", help="列出可用人设")
    args = ap.parse_args()

    meta = load_meta()
    if args.list or not args.name:
        return cmd_list(meta)

    text = persona_text(args.name, meta)
    if text is None:
        print(f"[x] 找不到人设「{args.name}」，或它的 txt 文件不存在。")
        print("    可用：", "、".join(meta.keys()))
        return 2

    cfg_p = user_data_dir() / "config.json"
    cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    entry = meta[args.name]

    print(f"人设      : {cfg['persona'].get('name', '?')} -> {args.name}（{len(text)} 字）")
    print(f"排版      : 单条 {entry.get('max_chars')} 字 / 每轮最多 {entry.get('max_messages')} 条")
    if args.target:
        old = cfg["behaviour"].get("private_whitelist")
        print(f"私聊白名单: {old} -> {args.target}")
    else:
        print(f"私聊白名单: {cfg['behaviour'].get('private_whitelist')}（不变）")

    if args.dry:
        print("[dry] 未写入")
        return 0

    shutil.copy(cfg_p, user_data_dir() / f"config.json.before-{args.name}")
    cfg["persona"]["name"] = args.name
    cfg["persona"]["system_prompt"] = text
    ts = cfg["persona"].setdefault("typing_style", {})
    ts["enabled"] = True
    ts["split_on_newline"] = True
    ts["max_chars_per_message"] = int(entry.get("max_chars", 25))
    ts["max_messages_per_reply"] = int(entry.get("max_messages", 3))
    if args.target:
        cfg["behaviour"]["private_whitelist"] = list(args.target)
        # 主动消息跟随白名单，避免它跑去找上一个人设的对象
        cfg["behaviour"].setdefault("proactive", {})["targets"] = []
    cfg_p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print("[ok] 已写入配置（旧配置已备份）")
    print("     重启 agent 生效：先停掉正在跑的 python run.py，再重新启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
