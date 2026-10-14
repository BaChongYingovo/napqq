"""主动向指定群/私聊发一条由模型生成的回复。

用途：让机器人在群里说一句特定的话，而不必真的有人 @ 它。

    python tools/say.py --group 999999999 "用喵语背一下出师表"
    python tools/say.py --private 100000001 "在吗"
    python tools/say.py --group 999999999 "..." --dry-run    # 只打印不发送

注意：**不挂事件处理器**。否则本脚本会和正在运行的 agent 实例抢消息，
导致每条消息被回复两次。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.bot import Agent  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.llm import LLMClient  # noqa: E402
from agent.onebot import OneBotClient  # noqa: E402
from agent.store import Store  # noqa: E402

DEFAULT_SENDER = 100000001


def parse_args() -> dict:
    argv = sys.argv[1:]
    out = {"group": 0, "private": 0, "dry_run": False, "sender": DEFAULT_SENDER,
           "text": "", "at": 0, "draft": False, "verbatim": False, "file": ""}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--group":
            out["group"] = int(argv[i + 1]); i += 2
        elif a == "--private":
            out["private"] = int(argv[i + 1]); i += 2
        elif a == "--sender":
            out["sender"] = int(argv[i + 1]); i += 2
        elif a == "--at":
            out["at"] = int(argv[i + 1]); i += 2
        elif a == "--file":
            out["file"] = argv[i + 1]; i += 2
        elif a == "--draft":
            out["draft"] = True; i += 1
        elif a == "--verbatim":
            out["verbatim"] = True; i += 1
        elif a == "--dry-run":
            out["dry_run"] = True; i += 1
        else:
            out["text"] = a; i += 1
    if out["file"]:
        out["text"] = Path(out["file"]).read_text(encoding="utf-8").strip()
    return out


REWRITE_PROMPT = """把下面这句话用你的语气重新说一遍。

要求：
- 更可爱一点，但别腻，保持你平时的傲娇劲儿。
- 只是个回应，别写长，20 字以内。
- 只输出要发送的内容本身。不要解释、不要加引号、不要写"好的"、不要复述这句要求。
- 不要带 @ 任何人，@ 已经由发送方处理好了。

原话：{draft}"""


async def send_pieces(onebot: OneBotClient, chat_type: str, chat_id: int,
                      pieces: list[str], at: int = 0) -> int:
    """逐条发送。at > 0 时给第一条加一个真正的 at 段（纯文本的 "@123" 不会通知到人）。"""
    sent = 0
    for i, piece in enumerate(pieces):
        segments: list[dict] = []
        if at and i == 0:
            segments = [{"type": "at", "data": {"qq": str(at)}},
                        {"type": "text", "data": {"text": " " + piece}}]
        else:
            segments = [{"type": "text", "data": {"text": piece}}]
        try:
            await onebot.send_segments(chat_type, chat_id, segments)
            sent += 1
        except Exception as e:
            print(f"[x] 第{i + 1}条发送失败: {e}")
            break
        if i < len(pieces) - 1:
            await asyncio.sleep(min(1.5, 0.35 + len(piece) / 60))
    return sent


async def main() -> int:
    args = parse_args()
    if not args["text"] or not (args["group"] or args["private"]):
        print(__doc__)
        return 2
    chat_type = "group" if args["group"] else "private"
    chat_id = args["group"] or args["private"]

    cfg = Config.load(project_root=ROOT)
    problems = cfg.validate()
    if problems:
        print("配置有问题:", problems)
        return 2

    store = Store(cfg.resolve(str(cfg["storage"]["db_path"])))
    llm = LLMClient(cfg["llm"])
    onebot = OneBotClient(
        cfg["onebot"]["ws_url"],
        cfg["onebot"].get("access_token", ""),
        1, 2,
    )
    agent = Agent(cfg, store, llm, onebot)

    async def noop(event):  # 故意空实现：本脚本只发不收
        return None

    await onebot.start(noop)
    try:
        await asyncio.wait_for(onebot.ready.wait(), timeout=15)
    except asyncio.TimeoutError:
        print("[x] OneBot 没连上（NapCat 是否在跑？）")
        await onebot.stop(); store.close()
        return 1
    print(f"[ok] 已连接，机器人 QQ {onebot.self_id}")

    if chat_type == "group":
        try:
            info = await onebot.call("get_group_info", {"group_id": chat_id, "no_cache": False})
            if isinstance(info, dict):
                print(f"[i] 目标群: {info.get('group_name')}（{chat_id}，{info.get('member_count')} 人）")
        except Exception as e:
            print(f"[!] 拿群信息失败（继续）: {e}")

    # --verbatim：原样发送给定的话，不过模型
    if args["verbatim"]:
        print(f"[i] --verbatim 模式：原样发送（{len(args['text'])} 字）")
        print(f"    内容: {args['text']}")
        if args["dry_run"]:
            print("[dry-run] 不实际发送")
            await onebot.stop(); store.close()
            return 0
        sent = await send_pieces(onebot, chat_type, chat_id, [args["text"]], at=args["at"])
        print(f"[{'ok' if sent else 'x'}] 已发送 {sent} 条")
        await onebot.stop(); store.close()
        return 0 if sent else 1

    # --draft：把给定的话按人设重写（"再可爱一点"这类要求），不走"回复消息"的组装逻辑
    if args["draft"]:
        print("[i] --draft 模式：按人设重写这句话")
        print(f"    原话: {args['text']}")
        try:
            reply = await llm.chat([
                {"role": "system", "content": agent.active_persona},
                {"role": "user", "content": REWRITE_PROMPT.format(draft=args["text"])},
            ], temperature=0.95)
        except Exception as e:
            print(f"[x] 模型调用失败: {type(e).__name__}: {e}")
            await onebot.stop(); store.close()
            return 1
        reply = reply.strip().strip('"').strip("“”")
        print(f"[ok] 重写结果: {reply}")
        if args["dry_run"]:
            print("[dry-run] 不实际发送")
            await onebot.stop(); store.close()
            return 0
        sent = await send_pieces(onebot, chat_type, chat_id, [reply], at=args["at"])
        print(f"[ok] 已发送 {sent} 条")
        await onebot.stop(); store.close()
        return 0 if sent else 1

    # 构造成"有人在群里 @ 了机器人并提出要求"，走与生产完全相同的组装逻辑
    segments = []
    if chat_type == "group":
        segments.append({"type": "at", "data": {"qq": str(onebot.self_id)}})
    segments.append({"type": "text", "data": {"text": " " + args["text"]}})
    batch = [{
        "segments": segments,
        "text": (f"@{onebot.self_id} " if chat_type == "group" else "") + args["text"],
        "sender_id": args["sender"],
        "sender_name": "我",
        "message_id": int(time.time() * 1000) % 2147483647,
        "ts": int(time.time()),
    }]

    conv_id, ids = agent._store_batch(chat_type, chat_id, batch)
    messages, used_images = await agent._build_messages(chat_type, chat_id, batch, ids)
    print(f"[i] 上下文已组装（{len(messages[0]['content'])} 字 system，含图={used_images}），调用模型 ...")

    started = time.time()
    try:
        reply = await llm.chat(messages)
    except Exception as e:
        print(f"[x] 模型调用失败: {type(e).__name__}: {e}")
        await onebot.stop(); store.close()
        return 1
    print(f"[ok] 模型返回 {time.time() - started:.1f}s，{len(reply)} 字")
    print("-" * 66)
    print(reply)
    print("-" * 66)

    style = cfg["persona"].get("typing_style") or {}
    limit = int(style.get("max_chars_per_message", 180) or 180)
    threshold = int(style.get("long_reply_threshold", 0) or 0)
    if threshold > 0 and len(reply) > threshold:
        limit = int(style.get("long_reply_max_chars_per_message", limit) or limit)

    if args["dry_run"]:
        from agent import message as msg
        pieces = msg.split_reply(reply, limit, bool(style.get("split_on_newline", True)),
                                 int(style.get("max_messages_per_reply", 0) or 0))
        print(f"[dry-run] 会发 {len(pieces)} 条（单条上限 {limit} 字），不实际发送：")
        for i, piece in enumerate(pieces, 1):
            print(f"  第{i}条 ({len(piece)} 字): {piece[:60]}...")
        await onebot.stop(); store.close()
        return 0

    print(f"[i] 开始发送（单条上限 {limit} 字{('，@' + str(args['at'])) if args['at'] else ''}）...")
    if args["at"]:
        from agent import message as msg
        pieces = msg.split_reply(reply, limit, bool(style.get("split_on_newline", True)),
                                 int(style.get("max_messages_per_reply", 0) or 0))
        sent = await send_pieces(onebot, chat_type, chat_id, pieces, at=args["at"])
        print(f"[{'ok' if sent else 'x'}] 发送完成，{sent}/{len(pieces)} 条")
    else:
        await agent._send_reply(chat_type, chat_id, conv_id, reply)
        print("[ok] 发送完成")

    await onebot.stop()
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
