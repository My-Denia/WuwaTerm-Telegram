"""Read-only validation of the Telegram product's persisted JSON state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .channel_reply_schema import parse_channel_reply_payload
from .settings import parse_chat_settings_payload


def _invalid_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="state file to validate")
    parser.add_argument("kind", choices=("chat-settings", "channel-replies"))
    args = parser.parse_args(argv)

    try:
        with args.path.open(encoding="utf-8") as file:
            payload = json.load(file, parse_constant=_invalid_constant)
        if args.kind == "chat-settings":
            parse_chat_settings_payload(payload)
        else:
            parse_channel_reply_payload(payload)
    except (OSError, UnicodeError, ValueError, RecursionError):
        print(f"invalid or unreadable {args.kind} state file", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
