"""Assemble a minimal, verified Docker context without the workspace or secrets."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from core_lock import ROOT, LockError
from verify_wheels import verify


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--core", type=Path, required=True)
    p.add_argument("--telegram", type=Path, required=True)
    p.add_argument("--telegram-sha256", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    args.local_core_sha256 = None
    try:
        verify(args)
        if args.core.name == args.telegram.name:
            raise LockError("wheel filenames must differ")
        if args.output_dir.exists():
            raise LockError("Docker context output already exists")
        args.output_dir.mkdir(parents=True)
        for name in ("scripts", "src/wuwaterm_telegram", "wheels"):
            (args.output_dir / name).mkdir(parents=True, exist_ok=True)
        for source, target in (
            (args.lock, "core-lock.json"),
            (ROOT / "pyproject.toml", "pyproject.toml"),
            (ROOT / "src/wuwaterm_telegram/core_compat.py", "src/wuwaterm_telegram/core_compat.py"),
            (ROOT / "scripts/core_lock.py", "scripts/core_lock.py"),
            (ROOT / "scripts/verify_wheels.py", "scripts/verify_wheels.py"),
            (ROOT / "scripts/install_verified.py", "scripts/install_verified.py"),
            (args.core, f"wheels/{args.core.name}"),
            (args.telegram, f"wheels/{args.telegram.name}"),
        ):
            shutil.copyfile(source, args.output_dir / target)
        (args.output_dir / "wheels/telegram.sha256").write_text(args.telegram_sha256 + "\n")
        (args.output_dir / "wheels/core.filename").write_text(args.core.name + "\n")
        (args.output_dir / "wheels/telegram.filename").write_text(args.telegram.name + "\n")
    except (LockError, OSError) as exc:
        raise SystemExit(f"Docker context preparation failed: {exc}") from exc
    print("Verified Docker build context prepared")


if __name__ == "__main__":
    main()
