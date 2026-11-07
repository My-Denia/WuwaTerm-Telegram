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


def parser(description: str | None = __doc__) -> argparse.ArgumentParser:
    """Official mode: --lock + --core. Explicit local mode: --local-core-wheel + --local-core-sha256.

    The older ``--core`` + ``--local-core-sha256`` spelling stays accepted for
    the same explicit local mode. A local Core never changes the committed lock.
    """
    p = argparse.ArgumentParser(description=description)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--lock", type=Path)
    group.add_argument("--local-core-sha256")
    wheel = p.add_mutually_exclusive_group(required=True)
    wheel.add_argument("--core", type=Path)
    wheel.add_argument("--local-core-wheel", type=Path)
    p.add_argument("--telegram", type=Path, required=True)
    p.add_argument("--telegram-sha256", required=True)
    return p


def finish(p: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    """Resolve the Core wheel path and reject mixed official/local inputs."""
    if args.local_core_wheel is not None:
        if args.local_core_sha256 is None:
            p.error("--local-core-wheel requires --local-core-sha256")
        args.core = args.local_core_wheel
    return args


def parse_args(argv: list[str] | None = None, p: argparse.ArgumentParser | None = None) -> argparse.Namespace:
    p = p or parser()
    return finish(p, p.parse_args(argv))


if __name__ == "__main__":
    try:
        verify(parse_args())
    except LockError as exc:
        raise SystemExit(str(exc)) from exc
    print("Both wheel checks passed")
