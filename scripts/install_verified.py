"""Verify two wheel artifacts, then install both with pip's normal resolver."""
from __future__ import annotations

import subprocess
import sys

from core_lock import LockError
from verify_wheels import parser, verify


def main() -> None:
    args = parser().parse_args()
    try:
        verify(args)
    except LockError as exc:
        raise SystemExit(str(exc)) from exc
    subprocess.run([sys.executable, "-m", "pip", "install", str(args.core), str(args.telegram)], check=True)


if __name__ == "__main__":
    main()
