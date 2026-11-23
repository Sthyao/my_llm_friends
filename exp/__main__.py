"""Inspect and exercise the persona experiment from the terminal."""

import argparse
import asyncio
import json
from pathlib import Path

from .harness import Harness, NAMES, PROFILES, Store
from .model import DeepSeek


async def run(args, store):
    model = DeepSeek()
    harness = Harness(store, model)
    if args.command == "publish":
        source = args.file.read_text(encoding="utf-8") if args.file else PROFILES[args.agent]
        await harness.publish(args.agent, source)
        return store.profile(args.agent)
    if args.command == "init":
        return {agent: await harness.publish(agent, source) for agent, source in PROFILES.items()}
    return await harness.turn(args.session, args.text, disabled=set(args.disable), history_mode=args.history)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(__file__).parent / ".data" / "chat.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="发布两个内置人格")
    publish = commands.add_parser("publish", help="从自然语言文件编译指定角色")
    publish.add_argument("agent", choices=NAMES)
    publish.add_argument("file", nargs="?", type=Path)
    message = commands.add_parser("say", help="在一个会话里发送一条消息，显示各角色的决定")
    message.add_argument("session")
    message.add_argument("text")
    message.add_argument("--disable", choices=NAMES, action="append", default=[])
    message.add_argument("--history", choices=("auto", "lookup", "off"), default="auto",
                         help="自动判断、主动查历史，或关闭当轮历史检索")
    sharing = commands.add_parser("archive-sharing", help="设置一个会话能否被其他会话查到")
    sharing.add_argument("session")
    sharing.add_argument("mode", choices=("on", "off"))
    inspect = commands.add_parser("inspect", help="读取指定角色当前的人格技能和私有记忆")
    inspect.add_argument("agent", choices=NAMES)
    rewind = commands.add_parser("rewind", help="撤回一条用户消息及同会话后续消息")
    rewind.add_argument("event_id", type=int)
    args = parser.parse_args()
    store = Store(args.db)
    try:
        if args.command == "inspect":
            result = {"profile": store.profile(args.agent), "memories": store.memories(args.agent)}
        elif args.command == "rewind":
            result = {"rewound_events_in_session": store.rewind(args.event_id)}
        elif args.command == "archive-sharing":
            enabled = args.mode == "on"
            store.set_archive_sharing(args.session, enabled)
            result = {"session": args.session, "archive_shared": enabled}
        else:
            result = asyncio.run(run(args, store))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
