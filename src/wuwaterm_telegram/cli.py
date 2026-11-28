"""Standalone Telegram bot command."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .core_compat import CoreCompatibilityError, verify_core_compatibility


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wuwaterm-telegram")
    parser.add_argument(
        "--db",
        default=os.getenv("WUWATERM_DB_PATH", "").strip() or "data/terms.db",
        help="path to the terms database (unused in HTTP Core mode)",
    )
    args = parser.parse_args(argv)
    try:
        # Check before importing the adapter: incompatible Core packages may
        # lack imports the runtime module needs.
        verify_core_compatibility()
    except CoreCompatibilityError as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    from .bot import BotConfigError, run_bot

    try:
        run_bot(Path(args.db))
    except (BotConfigError, CoreCompatibilityError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    return 0
