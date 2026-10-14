"""配置加载：JSON 文件 + 环境变量覆盖，带默认值。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "onebot": {
        "ws_url": "ws://127.0.0.1:3001",
        "access_token": "",
        "reconnect_min_seconds": 2,
        "reconnect_max_seconds": 60,
    },
    "llm": {
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "temperature": 0.9,
        "max_tokens": 800,
        "timeout_seconds": 90,
        "history_limit": 24,
        "extra_body": {},
    },
    "persona": {
        "name": "小助",
        "system_prompt": "你是一个在 QQ 上与人聊天的真人。",
        "typing_style": {
            "enabled": True,
            "split_on_newline": True,
            "max_chars_per_message": 180,
        },
    },
    "contacts": {},
    "vision": {
        "enabled": False,
        "model": "",
        "image_transport": "auto",
        "max_images_per_message": 2,
        "max_image_bytes": 4194304,
        "image_timeout_seconds": 20,
        "detail": "auto",
    },
    "memory": {
        "enabled": True,
        "trigger_messages": 40,
        "min_chunk_messages": 8,
        "keep_recent": 16,
        "max_summary_chars": 1200,
        "summary_model": "",
    },
    "behaviour": {
        "owner_qq": 0,
        "private_reply": True,
        "private_whitelist": [],
        "group_reply_mode": "mention",
        "group_whitelist": [],
        "group_blacklist": [],
        "private_blacklist": [],
        # 拟人节奏：秒回 + 话多是最容易被认出来的组合
        "typing_delay": {
            "enabled": True,
            "base_seconds": 1.2,
            "per_char": 0.10,
            "max_seconds": 6.0,
        },
        # 主动发消息：防刷屏的硬限制全在代码里，不交给模型判断
        "proactive": {
            "enabled": False,
            "targets": [],
            "min_silence_minutes": 45,
            "max_per_day": 4,
            "quiet_hours": "01:00-09:00",
            "check_interval_seconds": 300,
            "chance": 0.5,
        },
        "debounce_seconds": 1.2,
        "min_reply_interval_seconds": 0.8,
        "active_hours": "",
        "at_sender": True,
        "avoid_repeat": True,
        "self_check": True,
        "repeat_similarity": 0.8,
        "error_reply": "",
        "commands": {
            "enabled": True,
            "reset": "/reset",
            "pause": "/pause",
            "resume": "/resume",
            "persona": "/persona",
            "memory": "/memory",
            "forget": "/forget",
        },
    },
    "storage": {
        "db_path": "",          # 空 = 用户数据目录（不进项目目录，见 agent/paths.py）
        "log_path": "",
        "log_level": "INFO",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _in_project(path: Path, project: Path) -> bool:
    """配置文件是否落在项目目录内（这是要避免的情况）。"""
    try:
        path.resolve().relative_to(project.resolve())
        return True
    except ValueError:
        return False


def migrate_legacy_data(project_root: Path) -> list[str]:
    """把旧部署遗留在项目目录里的配置与数据库搬到用户数据目录。

    只搬不删：旧文件仍在原处，避免误删。返回做过的动作描述。
    """
    import shutil

    from .paths import default_db_path, default_log_path, user_data_dir

    actions: list[str] = []
    data_dir = user_data_dir()

    old_db = project_root / "data" / "agent.db"
    if old_db.exists() and not default_db_path().exists():
        shutil.move(str(old_db), str(default_db_path()))
        for suffix in ("-wal", "-shm"):
            side = old_db.with_name(old_db.name + suffix)
            if side.exists():
                shutil.move(str(side), str(default_db_path().with_name(default_db_path().name + suffix)))
        actions.append(f"数据库 {old_db} → {default_db_path()}")

    old_log = project_root / "data" / "agent.log"
    if old_log.exists() and not default_log_path().exists():
        shutil.move(str(old_log), str(default_log_path()))
        actions.append(f"日志 {old_log} → {default_log_path()}")

    old_cfg = project_root / "config.json"
    new_cfg = data_dir / "config.json"
    if old_cfg.exists() and not new_cfg.exists():
        raw = json.loads(old_cfg.read_text(encoding="utf-8"))
        # 清掉指向旧项目目录的相对路径，让它走新的用户数据目录默认值
        storage = raw.setdefault("storage", {})
        for key in ("db_path", "log_path"):
            if not str(storage.get(key, "")).strip() or storage.get(key, "").startswith("data"):
                storage.pop(key, None)
        new_cfg.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        actions.append(f"配置 {old_cfg} → {new_cfg}（旧文件保留，可自行删除）")

    return actions


def load_persona_text(name: str, project_root: Path | None = None) -> str:
    """按名字找人设正文。

    查找顺序（前者优先，便于用户覆盖仓库自带的）：
      1. 用户数据目录 `persona-<名字>.txt`（自己改的放这儿，不会被 git 跟踪）
      2. 仓库自带 `personas/<名字>.txt`（clone 下来就自带一份）
    """
    from .paths import user_data_dir

    cands = [user_data_dir() / f"persona-{name}.txt"]
    if project_root:
        cands.append(Path(project_root) / "personas" / f"{name}.txt")
    for p in cands:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except OSError:
            continue
    return ""


class Config:
    def __init__(self, data: dict[str, Any], root: Path, source: Path | None = None):
        self._data = data
        self.root = root
        self.source = source
        # 注意：不能叫 self.warnings —— 会和下面的 warnings() 方法重名，把方法覆盖掉
        self.notices: list[str] = []

    @classmethod
    def load(cls, path: str | Path | None = None, project_root: Path | None = None) -> "Config":
        from .paths import default_db_path, default_log_path, find_config_path

        project = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
        p = find_config_path(path, project)
        raw: dict[str, Any] = {}
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ValueError(f"配置文件不是合法 JSON：{p}\n  {e}") from None
        data = _deep_merge(DEFAULTS, raw)
        cfg = cls(data, p.parent if str(p.parent) else Path("."), source=p if p.exists() else None)

        # 运行数据默认放用户数据目录，**绝不默认落在项目目录里**
        if not data["storage"].get("db_path"):
            data["storage"]["db_path"] = str(default_db_path())
        if not data["storage"].get("log_path"):
            data["storage"]["log_path"] = str(default_log_path())

        # 人设：配置里没内嵌正文时，按名字从文件加载。
        # 这样仓库可以自带人设（personas/），用户也能在用户数据目录里覆盖它。
        pname = str(data["persona"].get("name") or "小喵")
        if not str(data["persona"].get("system_prompt") or "").strip():
            text = load_persona_text(pname, project)
            if text:
                data["persona"]["system_prompt"] = text
            elif p.exists():
                cfg.notices.append(
                    f"人设「{pname}」找不到正文：请在 personas/{pname}.txt 放入人设，"
                    f"或在配置的 persona.system_prompt 里直接写。"
                )

        # 密钥：环境变量优先于配置文件里的明文
        key_env = data["llm"].get("api_key_env") or "DEEPSEEK_API_KEY"
        env_key = os.environ.get(key_env, "")
        if env_key:
            data["llm"]["api_key"] = env_key
        elif data["llm"].get("api_key") and cfg.source and _in_project(cfg.source, project):
            cfg.notices.append(
                f"API key 以明文存在项目目录内的 {cfg.source.name} 里。"
                f"建议改用环境变量 {key_env}，并把配置移到用户数据目录。"
            )

        env_url = os.environ.get("QQ_AGENT_WS_URL")
        if env_url:
            data["onebot"]["ws_url"] = env_url
        env_token = os.environ.get("QQ_AGENT_TOKEN")
        if env_token:
            data["onebot"]["access_token"] = env_token
        env_base = os.environ.get("QQ_AGENT_LLM_BASE_URL")
        if env_base:
            data["llm"]["base_url"] = env_base

        if cfg.source and _in_project(cfg.source, project):
            cfg.notices.append(
                f"配置文件在项目目录内（{cfg.source}）。建议移到用户数据目录，"
                f"避免和代码一起被 git 提交或打包分享。"
            )
        return cfg

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    def validate(self) -> list[str]:
        """致命问题：返回非空则拒绝启动。"""
        problems: list[str] = []
        if not self._data["llm"].get("api_key"):
            problems.append("llm.api_key 为空：请设置环境变量 DEEPSEEK_API_KEY，或直接填在 config.json")
        if not self._data["llm"].get("model"):
            problems.append("llm.model 为空")
        if not str(self._data["onebot"].get("ws_url", "")).startswith(("ws://", "wss://")):
            problems.append("onebot.ws_url 必须是 ws:// 或 wss:// 开头")
        mode = self._data["behaviour"].get("group_reply_mode")
        if mode not in ("mention", "all", "off"):
            problems.append("behaviour.group_reply_mode 只能是 mention / all / off")
        hours = self._data["behaviour"].get("active_hours")
        if hours:
            from .schedule import BadWindow, parse_window

            try:
                parse_window(str(hours))
            except BadWindow as e:
                problems.append(f"behaviour.active_hours 格式错误：{e}")
        vision_cfg = self._data.get("vision") or {}
        if vision_cfg.get("enabled"):
            if str(vision_cfg.get("image_transport", "auto")).lower() not in ("auto", "url", "base64"):
                problems.append("vision.image_transport 只能是 auto / url / base64")
        mem = self._data.get("memory") or {}
        if mem.get("enabled", True) and int(mem.get("trigger_messages", 40) or 40) <= int(mem.get("keep_recent", 16) or 16):
            problems.append("memory.trigger_messages 必须大于 memory.keep_recent，否则摘要永远触发不了")
        contacts = self._data.get("contacts")
        if contacts is not None:
            if not isinstance(contacts, dict):
                problems.append("contacts 必须是对象，形如 {\"100000004\": {\"alias\": \"女朋友\"}}")
            else:
                for qq, info in contacts.items():
                    if not str(qq).strip().isdigit():
                        problems.append(f"contacts 的键必须是 QQ 号（纯数字），当前为 {qq!r}")
                    if info is not None and not isinstance(info, dict):
                        problems.append(f"contacts[{qq}] 必须是对象，可含 alias / relation / notes")
        return problems

    def warnings(self) -> list[str]:
        """非致命提示：只打印，不阻止启动。"""
        warns: list[str] = list(self.notices)
        if not self._data["behaviour"].get("owner_qq"):
            warns.append("behaviour.owner_qq 为 0：管理指令已关闭，任何人都无法发 /pause /reset /persona")
        if self._data["behaviour"].get("group_reply_mode") == "all":
            warns.append("group_reply_mode=all：群里每条消息都会回，这是被封号的头号诱因")
        vision_cfg = self._data.get("vision") or {}
        if vision_cfg.get("enabled") and not vision_cfg.get("model"):
            model = str(self._data["llm"].get("model", ""))
            if any(k in model.lower() for k in ("deepseek-chat", "deepseek-reasoner")):
                warns.append(f"vision 已开启，但 llm.model={model} 不支持图片输入，带图请求会失败并降级为纯文本")
        if not self._data["behaviour"].get("active_hours"):
            warns.append("behaviour.active_hours 未设置：机器人 7x24 小时在线，这是最硬的机器特征之一")
        return warns
