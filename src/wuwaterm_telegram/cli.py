"""Standalone Telegram bot command."""

from __future__ import annotations

import argparse
from pathlib import Path

from .core_compat import CoreCompatibilityError, verify_core_compatibility


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wuwaterm-telegram")
    parser.add_argument("--db", default="data/terms.db", help="path to the terms database")
    args = parser.parse_args(argv)
    try:
        # Check before importing the adapter: incompatible Core packages may
        # lack imports the runtime module needs.
        verify_core_compatibility()
    except CoreCompatibilityError as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    from .bot import run_bot

    run_bot(Path(args.db))
    return 0
