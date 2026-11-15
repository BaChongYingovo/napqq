"""配置加载：JSON 文件 + 环境变量覆盖，带默认值。"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "onebot": {
        "ws_url": "ws://127.0.0.1:3001",
        "access_token": "",  # 空值拒绝启动，见 validate()；首次生成走 init_user_config
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
        # 本地读图只允许这些目录。空 = 不读任何本地路径（含消息里带来的 file 路径）。
        "allowed_dirs": [],
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
        "private_reply": False,
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


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    """写入 JSON，并尽量收成仅当前用户可读。不记录内容。"""
    from .paths import restrict_file_permissions

    path.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077) if os.name == "posix" else None
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    finally:
        if old_umask is not None:
            os.umask(old_umask)
    restrict_file_permissions(path)


def _blank_project_storage(raw: dict[str, Any]) -> None:
    """新配置不要把数据库指回项目目录里的 data/。"""
    storage = raw.setdefault("storage", {})
    if not isinstance(storage, dict):
        raw["storage"] = {}
        storage = raw["storage"]
    for key in ("db_path", "log_path"):
        val = str(storage.get(key, "")).strip()
        if not val or val.replace("\\", "/").startswith("data/") or val == "data":
            storage[key] = ""


def init_user_config(project_root: Path, dest: Path | None = None) -> tuple[str, Path]:
    """首次生成用户数据目录里的配置。已存在则原样保留，不改 private_reply、不改 token。

    返回 ("created"|"exists", path)。拒绝写进项目目录。不打印 token 或 api key。
    """
    from .paths import user_data_dir

    project = Path(project_root).resolve()
    target = Path(dest).expanduser().resolve() if dest else (user_data_dir() / "config.json")
    if _in_project(target, project):
        raise ValueError(
            "拒绝在项目目录里生成 config.json。该文件会包含 OneBot access token，"
            "只能写到用户数据目录（已被 .gitignore 覆盖，且不在仓库路径内）。"
        )
    if target.exists():
        return "exists", target

    example = project / "config.example.json"
    if example.is_file():
        try:
            raw = json.loads(example.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ValueError(f"配置模板不是合法 JSON：{example.name}") from None
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = json.loads(json.dumps(DEFAULTS))
    raw.setdefault("onebot", {})
    if not isinstance(raw["onebot"], dict):
        raw["onebot"] = {}
    raw["onebot"]["access_token"] = secrets.token_urlsafe(32)
    raw.setdefault("llm", {})
    if isinstance(raw["llm"], dict):
        raw["llm"]["api_key"] = ""
    raw.setdefault("behaviour", {})
    if isinstance(raw["behaviour"], dict) and "private_reply" not in raw["behaviour"]:
        raw["behaviour"]["private_reply"] = False
    _blank_project_storage(raw)
    _write_private_json(target, raw)
    return "created", target


def ensure_stored_access_token(project_root: Path) -> str:
    """返回用户数据目录配置里的 OneBot token。

    文件不存在就按 init_user_config 创建；已有非空 token 原样使用，不改其它字段。
    调用方不得把返回值写入日志、标准输出或仓库。
    """
    from .paths import user_data_dir

    path = user_data_dir() / "config.json"
    if not path.exists():
        _state, path = init_user_config(project_root, dest=path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError(f"配置文件不是合法 JSON：{path.name}") from None
    if not isinstance(data, dict):
        raise ValueError(f"配置文件不是对象：{path.name}")
    onebot = data.setdefault("onebot", {})
    if not isinstance(onebot, dict):
        data["onebot"] = {}
        onebot = data["onebot"]
    token = str(onebot.get("access_token") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(32)
    onebot["access_token"] = token
    _write_private_json(path, data)
    return token


def write_napcat_onebot_config(target: Path, project_root: Path, template: dict[str, Any]) -> None:
    """把与 napqq 用户配置相同的 token 写入 NapCat 的 onebot11.json。不打印 token。"""
    import copy

    token = ensure_stored_access_token(project_root)
    cfg = copy.deepcopy(template)
    servers = ((cfg.get("network") or {}).get("websocketServers") or [])
    if not servers or not isinstance(servers[0], dict):
        raise ValueError("OneBot 模板缺少 websocketServers")
    servers[0]["token"] = token
    _write_private_json(target, cfg)


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
            except json.JSONDecodeError:
                # 不附带解析器原文：错误片段可能含 access token 或 api key
                raise ValueError(f"配置文件不是合法 JSON：{p}") from None
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
        if not str(self._data["onebot"].get("access_token") or "").strip():
            problems.append(
                "onebot.access_token 为空：拒绝启动。本机 127.0.0.1 上的 OneBot/NapCat "
                "若没有 token，任何本地进程都能借这个 QQ 号发消息。"
                "请在配置里填写非空 token（或设置环境变量 QQ_AGENT_TOKEN），并在 NapCat 里使用同一个值。"
                "新安装可运行 python run.py --init，它会在用户数据目录生成 token，终端不显示具体值。"
            )
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
        if vision_cfg.get("enabled") and not (vision_cfg.get("allowed_dirs") or []):
            warns.append(
                "vision 已开启但 allowed_dirs 为空：消息里的本地路径一律不读，只接受 http/https 图片链接"
            )
        if vision_cfg.get("enabled") and not vision_cfg.get("model"):
            model = str(self._data["llm"].get("model", ""))
            if any(k in model.lower() for k in ("deepseek-chat", "deepseek-reasoner")):
                warns.append(f"vision 已开启，但 llm.model={model} 不支持图片输入，带图请求会失败并降级为纯文本")
        if not self._data["behaviour"].get("active_hours"):
            warns.append("behaviour.active_hours 未设置：机器人 7x24 小时在线，这是最硬的机器特征之一")
        return warns
