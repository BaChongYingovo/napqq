"""QQ 智能体启动入口。

用法:
    python run.py                       # 使用 config.json
    python run.py -c my.json            # 指定配置
    python run.py --check               # 只做配置/连通性自检，不连 OneBot
    python run.py --selftest            # 离线自测（Mock 模型 + Mock OneBot，不发真实消息）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent.bot import Agent  # noqa: E402
from agent.config import Config, migrate_legacy_data  # noqa: E402
from agent.proactive import ProactiveMessenger  # noqa: E402
from agent import emoji  # noqa: E402
from agent.llm import LLMClient, LLMError  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402
from agent.store import Store  # noqa: E402

BANNER = r"""
  ___  ___        _                    _
 / _ \/ _ \      / \   __ _  ___ _ __ | |_
| | | | | | |   / _ \ / _` |/ _ \ '_ \| __|
| |_| | |_| |  / ___ \ (_| |  __/ | | | |_
 \__\_\\__\_\ /_/   \_\__, |\___|_| |_|\__|
                      |___/   QQ Agent v0.1
""".strip("\n")


INSTANCE_LOCK: "socket.socket | None" = None


def acquire_single_instance(port: int = 39997) -> bool:
    """用本地端口占用做单实例锁。

    进程退出（含被强杀）时操作系统自动释放端口，不会留下需要清理的陈旧锁文件。
    两个实例同时连 NapCat 会导致每条消息被回复两次，所以这个保护是必需的。
    """
    global INSTANCE_LOCK
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return False
    INSTANCE_LOCK = sock
    return True


def setup_logging(cfg: Config) -> None:
    level_name = str(cfg["storage"].get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_path = cfg.resolve(str(cfg["storage"].get("log_path", "data/agent.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    logging.getLogger("qqagent.llm").setLevel(max(level, logging.INFO))


def print_config_summary(cfg: Config, problems: list[str], warnings: list[str] | None = None) -> None:
    from agent.paths import user_data_dir

    print(BANNER)
    print()
    print("位置:")
    print(f"  配置文件 : {cfg.source if cfg.source else '（不存在，使用内置默认值）'}")
    print(f"  数据目录 : {user_data_dir()}")
    print(f"     数据库: {cfg['storage']['db_path']}")
    print(f"     日志  : {cfg['storage']['log_path']}")
    print()
    print("配置:")
    print(f"  OneBot   : {cfg['onebot']['ws_url']}")
    print(f"  Token    : {'已设置' if cfg['onebot'].get('access_token') else '未设置'}")
    print(f"  模型     : {cfg['llm']['model']} @ {cfg['llm']['base_url']}")
    print(f"  API Key  : {'已设置(' + str(len(cfg['llm']['api_key'])) + '字符)' if cfg['llm'].get('api_key') else '缺失'}")
    print(f"  人设名   : {cfg['persona']['name']}")
    print(f"  群聊策略 : {cfg['behaviour']['group_reply_mode']}")
    print(f"  作息时段 : {cfg['behaviour'].get('active_hours') or '未限制（7x24 在线）'}")
    print(f"  主人 QQ  : {cfg['behaviour']['owner_qq'] or '未设置（管理指令关闭）'}")
    print(f"  长期记忆 : {'开' if (cfg.get('memory') or {}).get('enabled', True) else '关'}"
          f" | 图片识别: {'开' if (cfg.get('vision') or {}).get('enabled') else '关'}"
          f" | 表情识别: {emoji.stats()['count']} 条")
    print(f"  数据库   : {cfg.resolve(str(cfg['storage']['db_path']))}")
    print()
    if problems:
        print("致命问题（必须修复才能启动）:")
        for p in problems:
            print(f"  [x] {p}")
        print()
    for w in warnings or []:
        print(f"  [!] {w}")
    if warnings:
        print()


async def run_check(cfg: Config) -> int:
    problems = cfg.validate()
    print_config_summary(cfg, problems, cfg.warnings())
    if problems:
        return 2
    llm = LLMClient(cfg["llm"])
    print("测试大模型连通性 ...")
    try:
        reply = await llm.chat([
            {"role": "system", "content": "你是测试探针。只回复两个字：正常"},
            {"role": "user", "content": "ping"},
        ], temperature=0)
        print(f"  [ok] 模型可用，返回: {reply[:50]!r}")
    except LLMError as e:
        print(f"  [x] 模型不可用: {e}")
        return 3

    print("测试 OneBot WebSocket ...")
    client = OneBotClient(cfg["onebot"]["ws_url"], cfg["onebot"].get("access_token", ""))
    got: list[str] = []

    async def handler(event: dict) -> None:
        got.append(str(event.get("post_type")))

    await client.start(handler)
    try:
        await asyncio.wait_for(client.ready.wait(), timeout=12)
        info = await client.call("get_login_info", timeout=8)
        print(f"  [ok] OneBot 已连接，登录账号: {info}")
    except Exception as e:
        print(f"  [x] OneBot 连接失败: {e}")
        print("      -> 确认 NapCat 已启动、已扫码登录、WebSocket 服务端已开启且端口一致")
        await client.stop()
        return 4
    finally:
        await client.stop()
    return 0


async def run_selftest(cfg: Config) -> int:
    """离线自测：内置 Mock 模型 + Mock OneBot，不接触真实 QQ 与外部 API。"""
    print_config_summary(cfg, [])
    store = Store(cfg.resolve("selftest.db"))
    cfg["behaviour"]["active_hours"] = ""  # 离线自测必须与当前时间无关
    # 自测是测试工具，不该为拟人节奏等待。实测不关掉它，自测会在延迟结束前就判失败。
    cfg["behaviour"]["typing_delay"] = {"enabled": False}
    cfg["behaviour"]["min_reply_interval_seconds"] = 0
    cfg["behaviour"]["debounce_seconds"] = 0.05
    # 自测测的是"链路能不能通"，不该被使用者本机的白/黑名单影响——
    # 否则同一份代码在你机器上通过、在别人机器上失败，自测就失去意义了。
    cfg["behaviour"]["private_whitelist"] = []
    cfg["behaviour"]["private_blacklist"] = []
    cfg["behaviour"]["group_whitelist"] = []
    cfg["behaviour"]["group_blacklist"] = []
    cfg["behaviour"]["group_reply_mode"] = "mention"
    llm = LLMClient(cfg["llm"])
    llm.chat = _mock_chat  # type: ignore[assignment]
    onebot = OneBotClient("ws://mock", "")
    sent: list[tuple[str, int, str]] = []

    async def fake_send(chat_type: str, chat_id: int, segments: list[dict]) -> dict:
        text = "".join(s["data"].get("text", "") for s in segments if s["type"] == "text")
        sent.append((chat_type, chat_id, text))
        print(f"  -> 发送[{chat_type} {chat_id}]: {text}")
        return {"message_id": len(sent)}

    onebot.send_segments = fake_send  # type: ignore[assignment]
    onebot.self_id = 10001
    onebot.nickname = "测试号"

    agent = Agent(cfg, store, llm, onebot)
    agent.behaviour["debounce_seconds"] = 0.05
    agent.behaviour["min_reply_interval_seconds"] = 0

    events = [
        {
            "post_type": "message", "message_type": "private", "message_id": 1,
            "self_id": 10001, "user_id": 20002, "time": 1759000000,
            "sender": {"user_id": 20002, "nickname": "老王"},
            "message": [{"type": "text", "data": {"text": "在吗"}}],
        },
        {
            "post_type": "message", "message_type": "private", "message_id": 2,
            "self_id": 10001, "user_id": 20002, "time": 1759000001,
            "sender": {"user_id": 20002, "nickname": "老王"},
            "message": "[CQ:image,file=abc.jpg][CQ:text] 帮我看看这个[jk]",
        },
        {
            "post_type": "message", "message_type": "group", "message_id": 3,
            "self_id": 10001, "user_id": 30003, "group_id": 55555, "time": 1759000002,
            "sender": {"user_id": 30003, "nickname": "小李"},
            "message": [{"type": "at", "data": {"qq": "10001"}}, {"type": "text", "data": {"text": " 机器人说句话"}}],
        },
        {
            "post_type": "message", "message_type": "group", "message_id": 4,
            "self_id": 10001, "user_id": 30004, "group_id": 55555, "time": 1759000003,
            "sender": {"user_id": 30004, "nickname": "路人"},
            "message": [{"type": "text", "data": {"text": "没@它，应该被跳过"}}],
        },
        {
            "post_type": "message", "message_type": "private", "message_id": 1,
            "self_id": 10001, "user_id": 20002, "time": 1759000004,
            "sender": {"user_id": 20002, "nickname": "老王"},
            "message": [{"type": "text", "data": {"text": "在吗"}}],  # 与事件1同 message_id，应被去重
        },
    ]
    print("灌入测试事件 ...")
    for event in events:
        await agent.handle_event(event)
    await asyncio.sleep(0.6)
    await agent.shutdown()

    print()
    print(f"结果: {agent.status_line()}")
    print("期望: 收到 4 条(去重掉1条重复) / 回复 2 条(私聊1 + 群聊@1) / 跳过 1 条(群聊未@)")
    ok = (len(sent) == 2 and agent.stats["received"] == 4
          and agent.stats["skipped"] == 1 and agent.stats["errors"] == 0)
    print("[ok] 自测通过" if ok else "[x] 自测失败")
    store.close()
    return 0 if ok else 1


async def _dry_run_chat(messages, temperature=None, model=None, max_tokens=None) -> str:  # noqa: ANN001
    """干跑模式的假模型：把收到的原文回显出来，用于验证 QQ 链路。"""
    content = messages[-1]["content"]
    text = content if isinstance(content, str) else "".join(
        part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
    )
    match = re.search(r"【新消息】\s*\n(.+?)$", text, re.S)
    snippet = (match.group(1) if match else text).strip()
    tail = snippet.split("\n")[-1] if snippet else ""
    return f"[干跑模式·未接大模型] 链路正常，我收到了：{tail[:120]}"


async def _mock_chat(messages, temperature=None, model=None, max_tokens=None) -> str:  # noqa: ANN001
    content = messages[-1]["content"]
    last = content if isinstance(content, str) else " ".join(
        p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
    )
    if "机器人说句话" in last:
        return "来了来了，刚在看群。"
    if "帮我看看这个" in last:
        return "[图片] 我这边看不到图，你形容一下？"
    return "在的，怎么了？"


async def run_agent(cfg: Config, dry_run: bool = False) -> int:
    problems = cfg.validate()
    if dry_run:
        # 干跑模式只验证 QQ 链路，不调用大模型，因此不要求 API key / owner_qq
        problems = [p for p in problems if "api_key" not in p and "owner_qq" not in p]
    print_config_summary(cfg, problems, cfg.warnings())
    if dry_run:
        print(">>> 干跑模式：不调用大模型，收到消息后回固定文本 <<<")
        print()
    if problems:
        print("配置不完整，已退出。修好后重跑。")
        return 2
    if not acquire_single_instance():
        print("[x] 已经有一个实例在运行（单实例锁 127.0.0.1:39997 被占用）。")
        print("    同时跑两个会让每条消息被回复两次。先停掉旧的再启动。")
        return 3

    store = Store(cfg.resolve(str(cfg["storage"]["db_path"])))
    llm = LLMClient(cfg["llm"])
    if dry_run:
        llm.chat = _dry_run_chat  # type: ignore[assignment]
    onebot = OneBotClient(
        cfg["onebot"]["ws_url"],
        cfg["onebot"].get("access_token", ""),
        float(cfg["onebot"].get("reconnect_min_seconds", 2)),
        float(cfg["onebot"].get("reconnect_max_seconds", 60)),
    )
    agent = Agent(cfg, store, llm, onebot)

    stop_event = asyncio.Event()

    def request_stop(*_args) -> None:
        log = logging.getLogger("qqagent")
        log.info("收到退出信号，正在收尾 ...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, request_stop)

    await onebot.start(agent.handle_event)
    log = logging.getLogger("qqagent")
    log.info("已启动。等待消息中…… 状态: %s", agent.status_line())
    log.info("作息: %s", agent.active_hours_text)
    if not agent.in_active_hours:
        log.warning("当前处于静默时段，只会记录消息不回复。想立刻测试请把 behaviour.active_hours 临时清空。")
    log.info("管理指令（仅主人 QQ 生效）: /pause /resume /reset /persona <新设定>")

    async def status_reporter() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=120)
            except asyncio.TimeoutError:
                s = store.stats()
                log.info("心跳 | %s | 会话 %d 消息 %d | 已连接=%s",
                         agent.status_line(), s["conversations"], s["messages"],
                         "是" if onebot._ws else "否")

    reporter = asyncio.create_task(status_reporter())

    # 主动发消息：只会被动回话是最大的机器感来源
    proactive = ProactiveMessenger(agent)
    proactive.start()

    await stop_event.wait()
    reporter.cancel()
    await proactive.stop()
    await agent.shutdown()
    await onebot.stop()
    store.close()
    log.info("已退出。%s", agent.status_line())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="napqq — QQ 聊天陪伴智能体")
    parser.add_argument("-c", "--config", default=None,
                        help="配置文件路径（默认：用户数据目录下的 config.json）")
    parser.add_argument("--check", action="store_true", help="配置 + 连通性自检")
    parser.add_argument("--selftest", action="store_true", help="离线自测（不发真实消息）")
    parser.add_argument("--dry-run", action="store_true",
                        help="干跑：连真实 QQ 但不调用大模型，回复固定文本，用于验证链路")
    parser.add_argument("--no-migrate", action="store_true",
                        help="跳过「把旧部署遗留的配置/数据库搬到用户数据目录」这一步")
    args = parser.parse_args()

    # 旧部署可能把 config.json / data 留在项目目录里。先搬到用户数据目录，
    # 避免运行数据继续和代码混在一起——这是分享/开源时最危险的隐患。
    if not args.no_migrate:
        try:
            for m in migrate_legacy_data(ROOT):
                print(f"[i] 已迁移: {m}")
        except Exception as e:  # 迁移失败不能挡住启动
            print(f"[!] 自动迁移失败（不影响启动）: {e}")

    try:
        cfg = Config.load(args.config, project_root=ROOT)
    except ValueError as e:
        print(f"[x] {e}")
        return 2
    if args.selftest:
        setup_logging(cfg)
        return asyncio.run(run_selftest(cfg))
    if args.check:
        setup_logging(cfg)
        return asyncio.run(run_check(cfg))
    setup_logging(cfg)
    try:
        return asyncio.run(run_agent(cfg, dry_run=args.dry_run))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
