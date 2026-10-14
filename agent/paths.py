"""运行数据与配置的存放位置。

**设计原则（这是本项目最初的设计缺陷，现在修掉）**：
代码目录里**不允许**出现任何运行数据或密钥。

原因很直接——之前 `config.json`（含 API key）和 `data/agent.db`（含真实聊天记录）
默认都放在项目目录里。结果就是：这个文件夹**天然含隐私**，一旦要开源、压缩发给别人、
或者误推到 GitHub，密钥和聊天记录就一起泄漏了，而且 Git 历史删不干净。

所以现在：所有可变数据一律放到**用户数据目录**，项目目录只留代码。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "napqq"


def user_data_dir() -> Path:
    """跨平台的用户数据目录（不随项目目录走）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME")
        root = Path(base) if base else Path.home() / ".local" / "share"
    path = root / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_db_path() -> Path:
    return user_data_dir() / "napqq.db"


def default_log_path() -> Path:
    return user_data_dir() / "napqq.log"


def find_config_path(explicit: str | Path | None = None, project_root: Path | None = None) -> Path:
    """定位配置文件。

    优先顺序：
      1. 显式传入的 -c 参数
      2. 用户数据目录下的 config.json   ← 推荐位置
      3. 项目目录下的 config.json        ← 兼容旧部署，会给出告警
      4. 都不存在时返回用户数据目录下的路径（供首次创建）
    """
    if explicit:
        return Path(explicit).expanduser()
    in_data = user_data_dir() / "config.json"
    if in_data.exists():
        return in_data
    if project_root:
        legacy = project_root / "config.json"
        if legacy.exists():
            return legacy
    return in_data
