"""Check both wheel bytes and metadata before invoking the package resolver."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
from io import BytesIO
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from core_lock import HEX64, ROOT, LockError, load_lock, product_core_version
import tomllib


def check_wheel(path: Path, digest: str, name: str, version: str | None = None) -> None:
    if not HEX64.fullmatch(digest):
        raise LockError("invalid expected wheel SHA-256")
    try:
        data = path.read_bytes()
        check_wheel_bytes(data, digest, name, version)
    except OSError as exc:
        raise LockError(f"{name} wheel cannot be read") from exc


def check_wheel_bytes(data: bytes, digest: str, name: str, version: str | None = None) -> None:
    try:
        if hashlib.sha256(data).hexdigest() != digest:
            raise LockError(f"{name} wheel SHA-256 mismatch")
        with ZipFile(BytesIO(data)) as z:
            meta = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            if len(meta) != 1:
                raise LockError(f"{name} wheel metadata is invalid")
            fields = BytesParser().parsebytes(z.read(meta[0]))
            def canonical(s: str) -> str:
                return re.sub(r"[-_.]+", "-", s).lower()
            if canonical(fields.get("Name", "")) != canonical(name):
                raise LockError(f"{name} wheel has the wrong package name")
            if version is not None and fields.get("Version") != version:
                raise LockError(f"{name} wheel has the wrong version")
    except (OSError, BadZipFile) as exc:
        raise LockError(f"{name} wheel cannot be read") from exc


def verify(args: argparse.Namespace) -> None:
    version = product_core_version()
    if args.lock:
        lock = load_lock(args.lock)
        if args.core.name != lock["filename"]:
            raise LockError("Core wheel filename does not match lock")
        core_digest = lock["sha256"]
    else:
        # For an explicitly supplied local development fixture only. CI and
        # published builds use the reviewed lock, not this argument.
        core_digest = args.local_core_sha256
    check_wheel(args.core, core_digest, "wuwaterm", version)
    product = tomllib.loads((ROOT / "pyproject.toml").read_text())
    check_wheel(args.telegram, args.telegram_sha256, "wuwaterm-telegram", product["project"]["version"])


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--lock", type=Path)
    group.add_argument("--local-core-sha256")
    p.add_argument("--core", type=Path, required=True)
    p.add_argument("--telegram", type=Path, required=True)
    p.add_argument("--telegram-sha256", required=True)
    return p


if __name__ == "__main__":
    try:
        verify(parser().parse_args())
    except LockError as exc:
        raise SystemExit(str(exc)) from exc
    print("Both wheel checks passed")
