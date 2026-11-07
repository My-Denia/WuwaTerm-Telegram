"""Assemble a minimal, verified Docker context without the workspace or secrets."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from core_lock import ROOT, LockError
from verify_wheels import parse_args, parser, verify


def main() -> None:
    p: argparse.ArgumentParser = parser(__doc__)
    p.add_argument("--output-dir", type=Path, required=True)
    args = parse_args(p=p)
    try:
        verify(args)
        if args.core.name == args.telegram.name:
            raise LockError("wheel filenames must differ")
        if args.output_dir.exists():
            raise LockError("Docker context output already exists")
        args.output_dir.mkdir(parents=True)
        for name in ("scripts", "src/wuwaterm_telegram", "wheels"):
            (args.output_dir / name).mkdir(parents=True, exist_ok=True)
        # An explicit local Core carries no reviewed lock; the committed one is
        # neither copied nor changed, and the image records no official lock.
        lock = [(args.lock, "core-lock.json")] if args.lock else []
        for source, target in (
            *lock,
            (ROOT / "pyproject.toml", "pyproject.toml"),
            (ROOT / "src/wuwaterm_telegram/core_compat.py", "src/wuwaterm_telegram/core_compat.py"),
            (ROOT / "scripts/core_lock.py", "scripts/core_lock.py"),
            (ROOT / "scripts/verify_wheels.py", "scripts/verify_wheels.py"),
            (ROOT / "scripts/install_verified.py", "scripts/install_verified.py"),
            (ROOT / "scripts/install_probe.py", "scripts/install_probe.py"),
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
    if args.local_core_sha256:
        print(f"Local Core: build with --build-arg CORE_WHEEL_SHA256={args.local_core_sha256}")


if __name__ == "__main__":
    main()
