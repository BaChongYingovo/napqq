"""生成可公开的干净副本（开源打包）。

要解决的问题（手改容易漏，所以做成可重复执行的脚本）：

1. **隐私**：真实 QQ 号 / 群号 / token / 本地用户名 / 真实聊天库 → 全部替换或排除
2. **NapCat 衍生资产**：`agent/qq_faces.json` 是从 napcat.mjs 提取的，属衍生品 →
   不进仓库，改由使用者本地生成（`gen_face_table.py` 保留）
3. **环境耦合**：本机绝对路径 → 通用占位
4. **人设**：只带仓库自带的 `personas/`（里面的猫娘人设），
   用户数据目录里的私人人设（本人风格档案等）**一律不进仓库**

    python tools/prepare_oss.py                      # 生成到 ../../../napqq
    python tools/prepare_oss.py <目标目录>

注意：脚本里**不硬编码任何真实 QQ 号或密钥**——那样脚本自己就成了泄漏源。
QQ 号通过"扫描 + 自动编号"通用替换。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT.parent.parent.parent / "napqq"

EXCLUDE_DIRS = {"__pycache__", "data", "out", ".pytest_cache", ".git", ".venv", "venv"}
EXCLUDE_FILES = {
    "config.json",              # 含真实 API key
    "agent/qq_faces.json",      # 从 napcat.mjs 提取的衍生数据
    "run-detached.cmd",         # 含本机绝对路径与用户名
}
# 只允许仓库自带的人设进包（用户数据目录里的私人人设本来就不会被复制，这里是双保险）
PERSONA_ALLOW_PREFIX = "personas/"

TEXT_SUFFIX = (".py", ".md", ".json", ".cmd", ".ps1", ".txt")

# 通用替换：不写死任何真实值
GENERIC_REPLACEMENTS: list[tuple[str, str]] = [
    (r"sk-[0-9a-zA-Z]{16,}", "<API-KEY>"),
    (r"token=[0-9a-f]{8,}", "token=<WEBUI-TOKEN>"),
    # 本机绝对路径（暴露用户名与目录结构）
    (r"[A-Za-z]:\\\\Users\\\\[^\\\\\"]+\\\\Documents\\\\deepseek-harness\\\\default-workspace", "<WORKSPACE>"),
    (r"[A-Za-z]:\\Users\\[^\\\"]+\\Documents\\deepseek-harness\\default-workspace", "<WORKSPACE>"),
    (r"[A-Za-z]:\\\\NapCat\\\\NapCat\.Shell", "<NAPCAT_DIR>"),
    (r"[A-Za-z]:\\NapCat\\NapCat\.Shell", "<NAPCAT_DIR>"),
    (r"[A-Za-z]:/NapCat/NapCat\.Shell", "<NAPCAT_DIR>"),
    (r"[A-Za-z]:\\\\NapCat", "<NAPCAT_DIR>"),
    (r"[A-Za-z]:\\NapCat", "<NAPCAT_DIR>"),
    # dsh 自带 python 路径
    (r"[A-Za-z]:\\\\Users\\\\[^\\\\\"]+\\\\\.dsh\\\\[^\"\s]+python\.exe", "python"),
    (r"[A-Za-z]:\\Users\\[^\\\"]+\\\.dsh\\[^\"\s]+python\.exe", "python"),
    # 用户名（兜底，放最后）
    (r"[A-Za-z]:\\\\Users\\\\[^\\\\\"]+", "<HOME>"),
    (r"[A-Za-z]:\\Users\\[^\\\"]+", "<HOME>"),
]

# QQ 号替换**不能靠"9~11 位数字"自动识别**——实测那会把测试里的时间戳
# （1759000000）、INT_MAX（2147483647）等一起换掉，测试直接挂。
# 所以改成读一份**本地清单**：清单放在用户数据目录，不进脚本、不进仓库，
# 这样脚本本身不会成为泄漏源，替换也精确。
SCRUB_FILE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "napqq" / "oss_scrub.json"


def _load_local_scrub() -> dict[str, str]:
    """读取真实 QQ 号 -> 占位号 的映射（本地文件，不进仓库）。"""
    if not SCRUB_FILE.exists():
        return {}
    try:
        data = json.loads(SCRUB_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for real, placeholder in (data.get("qq") or {}).items():
        out[str(real)] = str(placeholder)
    return out


LICENSE = """MIT License

Copyright (c) 2026 八重櫻

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

注意：本许可证只覆盖本仓库中由本项目作者编写的代码。
本项目的运行依赖 NapCat（许可见 NOTICE.md），该依赖禁止商业用途；
本仓库不包含也不分发 NapCat 的任何代码或数据。
"""

NOTICE = """# NOTICE — 第三方依赖与使用限制

本项目作者：**八重櫻**（Copyright © 2026）

## 1. 运行依赖：NapCat（**禁止商业用途**）

本项目**不包含也不分发** NapCat 的代码或数据，但运行时需要它作为 QQ 协议端。

NapCat 使用自定义的 *Limited Redistribution License for NapCat*（Copyright © 2024 Mlikiowa），
其中第 3 条明确写着：

> **Non-Commercial Use: This code is not to be used for any commercial purposes.**

因此：

- **你不可以把「本项目 + NapCat」用于任何商业用途。**
- 本项目的 MIT 许可证只覆盖本仓库作者编写的代码，**不能覆盖 NapCat**，
  也不能授权你绕过 NapCat 的限制。
- 获取 NapCat：https://github.com/NapNeko/NapCatQQ （请自行阅读其许可）

## 2. QQ 客户端（腾讯专有软件）

NapCat 的工作方式是**注入腾讯官方 QQ 客户端**。请注意：

- QQ 客户端是腾讯的专有软件，其许可协议禁止逆向、修改、注入、绕过技术保护措施。
- 腾讯不提供面向第三方的官方消息 API；使用协议端属于灰色地带。
- 可能的后果包括**账号被封禁**，以及涉及平台协议/著作权层面的风险。
- **这一层风险不会因为 NapCat 作者授权你使用而消失。**

**本项目仅供个人学习与自用。请自行评估风险，不要用于对外经营。**

## 3. QQ 表情映射表（本地生成，不含在仓库内）

`agent/qq_faces.json` **不在本仓库中**。它由使用者自己运行：

```
python tools/gen_face_table.py
```

从**自己本机安装的 NapCat** 中读取生成，因此仓库里不含任何 NapCat 衍生数据。

未生成该文件时程序仍可正常运行，只是 QQ 系统表情会显示为 `[表情#id]` 而不是 `[表情:微笑]`。

## 4. 大模型 API

默认使用 DeepSeek（https://api.deepseek.com）。商用请自行确认该服务的条款。
也可以换成任何兼容 OpenAI `/chat/completions` 的服务。

## 5. 与 NapCat / 腾讯的关系

**本项目是第三方独立项目，与 NapCat 项目及其作者、与腾讯公司均无隶属关系，
未获其授权、赞助或认可。**

- 本项目不包含 NapCat 的代码或数据，也不分发 NapCat；使用者需自行获取并遵守其许可。
- 本项目与 NapCat 之间只是**协议依赖**（OneBot v11），不是插件、不是分支、不是衍生作品。
- **任何实现 OneBot v11 的协议端都可以替换 NapCat**（改 config.json 的 ws_url 即可）。
- 「napqq」这个名称只是项目自身的命名，不代表与 NapCat 有任何官方关联。
- 本项目不主张任何与 NapCat 或腾讯相关的商标权。

"""

GITIGNORE = """# 隐私与运行时数据 —— 绝对不要提交
config.json
data/
out/
*.db
*.db-shm
*.db-wal
*.log

# 从本机 NapCat 提取的衍生数据（见 NOTICE.md 第 3 节）
agent/qq_faces.json

# Python
__pycache__/
*.py[cod]
.venv/
venv/
"""

README_TAIL = """
---

## 全新 clone 后的两步准备

```bash
# 1. 生成 QQ 表情映射表（从你自己本机安装的 NapCat 读取，仓库里不含该数据）
python tools/gen_face_table.py

# 2. 准备配置（config.json 不在仓库里，因为它会存你的 API key）
cp config.example.json config.json   # Windows: copy config.example.json config.json
```

然后 `python run.py --check` 看三项连通性，`python tests/test_integration.py` 跑测试。

### 人设

仓库自带一套猫娘人设（`personas/小喵.txt`）。`config.example.json` 里
`persona.system_prompt` 留空、`name` 写「小喵」，启动时会自动从
`personas/小喵.txt` 加载正文——所以改人设直接改那个 txt 就行。

想看清单或切换：

```bash
python tools/use_persona.py --list
python tools/use_persona.py 小喵 --target <QQ号>
```

想自己写一套：在 `personas/` 放一个 `<名字>.txt`，在 `personas/personas.json` 里登记，
然后 `python tools/use_persona.py <名字>`。也可以把自己的版本放到用户数据目录里覆盖仓库自带的。

**使用前请先读 [NOTICE.md](NOTICE.md)**：运行依赖的 NapCat **禁止商业用途**，
且协议端方式存在账号与合规风险，本项目仅供个人学习自用。
"""


def _rmtree_force(path: Path) -> None:
    """强制删除目录树。

    Windows 上 git 的对象文件是**只读**的，直接 rmtree 会 PermissionError，
    所以先把只读属性清掉。
    """
    import stat

    if not path.exists():
        return

    def on_error(func, p, exc_info):  # noqa: ANN001
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise

    shutil.rmtree(path, onerror=on_error)


def main() -> int:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT

    # 保留已有的 .git（免得之前建过的仓库历史丢了）
    # 注意：备份必须放在 out **外面**——放里面会被下面的 rmtree 一起删掉（踩过这个坑）。
    git_backup = out.parent / (out.name + ".gitbak")
    if (out / ".git").exists():
        _rmtree_force(git_backup)
        (out / ".git").rename(git_backup)
    _rmtree_force(out)
    out.mkdir(parents=True)

    src_files: list[Path] = []
    skipped: list[str] = []
    for src in ROOT.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if rel_posix in EXCLUDE_FILES or src.name in EXCLUDE_FILES:
            skipped.append(rel_posix)
            continue
        # 人设：只允许 personas/ 下的（仓库自带的猫娘）
        if rel.parts and rel.parts[0] == "personas" and not rel_posix.startswith(PERSONA_ALLOW_PREFIX):
            skipped.append(rel_posix)
            continue
        if src.suffix not in TEXT_SUFFIX:
            skipped.append(rel_posix)
            continue
        src_files.append(src)

    qq_map = _load_local_scrub()
    if not qq_map:
        print(f"[!] 没找到本地脱敏清单 {SCRUB_FILE}")
        print("    真实 QQ 号将**不会被替换**——打包前请先建这份清单。")
    replacements: list[tuple[str, str]] = list(GENERIC_REPLACEMENTS)
    for qq, placeholder in qq_map.items():
        replacements.append((rf"(?<!\d){re.escape(qq)}(?!\d)", placeholder))

    copied = 0
    scrubbed: list[str] = []
    for src in src_files:
        rel = src.relative_to(ROOT)
        text = src.read_text(encoding="utf-8")
        original = text
        for pattern, repl in replacements:
            text = re.sub(pattern, repl, text)
        # 文档里的旧目录名统一成仓库名（只改文档，不动代码里的字符串）
        if src.suffix in (".md", ".txt", ".cmd"):
            text = text.replace("qq-agent", "napqq")
        if text != original:
            scrubbed.append(rel.as_posix())
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        copied += 1

    (out / "LICENSE").write_text(LICENSE, encoding="utf-8")
    (out / "NOTICE.md").write_text(NOTICE, encoding="utf-8")
    (out / ".gitignore").write_text(GITIGNORE, encoding="utf-8")

    readme = out / "README.md"
    if readme.exists():
        readme.write_text(readme.read_text(encoding="utf-8").rstrip() + "\n" + README_TAIL,
                          encoding="utf-8")

    if git_backup.exists():
        git_backup.rename(out / ".git")

    print("=" * 72)
    print(f"已生成可发布副本: {out}")
    print("=" * 72)
    print(f"  复制文件                : {copied}")
    print(f"  排除（隐私/衍生/非文本）: {len(skipped)}")
    for s in sorted(skipped)[:10]:
        print(f"      - {s}")
    if len(skipped) > 10:
        print(f"      ... 还有 {len(skipped) - 10} 个")
    print(f"  脱敏改写                : {len(scrubbed)} 个文件")
    print(f"  QQ 号自动编号           : {len(qq_map)} 个真实号 -> 占位号")
    for real, ph in sorted(qq_map.items(), key=lambda kv: kv[1]):
        print(f"      {real} -> {ph}")
    print()
    print("  新增: LICENSE(MIT) / NOTICE.md / .gitignore")
    persona_dir = out / "personas"
    if persona_dir.exists():
        print(f"  人设: personas/ 下 {len(list(persona_dir.glob('*.txt')))} 套 -> "
              f"{[p.name for p in persona_dir.glob('*.txt')]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
