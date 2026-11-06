"""Offline product identity and read-only planning for Telegram state files."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import stat
import sys

from .state_paths import state_paths


_KINDS = {
    "chat-settings": ("chat_settings.json", "WUWATERM_SETTINGS_PATH"),
    "channel-replies": ("channel_replies.json", "WUWATERM_CHANNEL_REPLY_INDEX_PATH"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


class OperationsError(ValueError):
    """The identity or requested state plan cannot be established safely."""


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _filename(value: object) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or "\\" in value:
        raise OperationsError("invalid wheel filename")
    return value


def _artifacts(root: Path, core_version: str) -> dict[str, str] | None:
    lock_path = root / "core-lock.json"
    wheels = root / "wheels"
    if not lock_path.exists() and not wheels.exists():
        return None
    lock_bytes = lock_path.read_bytes()
    lock = json.loads(lock_bytes)
    if (not isinstance(lock, dict) or lock.get("schema") != 2
            or lock.get("repository") != "My-Denia/WuwaTerm"):
        raise OperationsError("invalid Core lock")
    core_sha = lock.get("sha256")
    core_name = _filename(lock.get("filename"))
    if not isinstance(core_sha, str) or not _SHA256.fullmatch(core_sha):
        raise OperationsError("invalid Core wheel digest")
    listed_core = _filename((wheels / "core.filename").read_text(encoding="ascii").strip())
    telegram_name = _filename((wheels / "telegram.filename").read_text(encoding="ascii").strip())
    telegram_sha = (wheels / "telegram.sha256").read_text(encoding="ascii").strip()
    if not _SHA256.fullmatch(telegram_sha):
        raise OperationsError("invalid Telegram wheel digest")
    if (
        listed_core != core_name
        or core_name != f"wuwaterm-{core_version}-py3-none-any.whl"
        or _digest(wheels / core_name) != core_sha
        or _digest(wheels / telegram_name) != telegram_sha
        or lock.get("version") != core_version
    ):
        raise OperationsError("artifact, installed Core version, or supplied lock mismatch")
    producer = lock.get("kind")
    source = lock.get("checkout_sha") if producer == "github-actions-artifact" else (
        lock.get("source_commit") if producer == "github-release" else None
    )
    if not isinstance(source, str) or not _REVISION.fullmatch(source):
        raise OperationsError("invalid Core source identity in lock")
    return {
        "core_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "core_wheel_sha256": core_sha,
        "telegram_wheel_sha256": telegram_sha,
        "core_producer_kind": producer,
        "core_source_revision_claim": source,
    }


def identity(root: Path = Path("/app")) -> dict[str, object]:
    """Describe installed versions and available, locally checked build inputs.

    A revision passed to the image build is a claim. It is not evidence that
    the wheel was built from that commit or that a running bot is ready.
    """
    telegram_version = metadata.version("wuwaterm-telegram")
    core_version = metadata.version("wuwaterm")
    source_file = root / ".build-revision"
    revision = source_file.read_text(encoding="ascii").strip() if source_file.exists() else ""
    if revision and not _REVISION.fullmatch(revision):
        raise OperationsError("invalid build revision")
    return {
        "telegram_version": telegram_version,
        "core_version": core_version,
        "source_revision_claim": revision or None,
        "artifacts_matching_supplied_records": _artifacts(root, core_version),
    }


def _mounts(values: list[str]) -> list[tuple[Path, Path, bool]]:
    mounts = []
    seen = set()
    for value in values:
        item = json.loads(value)
        if (
            not isinstance(item, dict)
            or set(item) != {"container", "host", "read_only"}
            or not isinstance(item["container"], str)
            or not isinstance(item["host"], str)
            or not isinstance(item["read_only"], bool)
        ):
            raise OperationsError("invalid mount description")
        container = Path(item["container"])
        host = Path(item["host"])
        if (
            not container.is_absolute() or not host.is_absolute()
            or ".." in container.parts or ".." in host.parts
            or container in seen or not host.is_dir()
        ):
            raise OperationsError("invalid or duplicate mount")
        seen.add(container)
        mounts.append((container, host.resolve(strict=True), item["read_only"]))
    return sorted(mounts, key=lambda item: len(item[0].parts), reverse=True)


def _map_path(path: Path, mounts: list[tuple[Path, Path, bool]], *, write: bool) -> tuple[Path, Path]:
    # Preserve the leaf entry: channel reply saves replace that entry, even
    # when reading it follows a symlink to a different directory.
    absolute = path if path.is_absolute() else Path.cwd() / path
    for container, host, read_only in mounts:
        try:
            relative = absolute.relative_to(container)
        except ValueError:
            continue
        raw = host / relative
        parent = raw.parent.resolve(strict=False)
        entry = parent / raw.name
        resolved = entry.resolve(strict=False)
        if not parent.is_relative_to(host) or not resolved.is_relative_to(host):
            raise OperationsError("mapped path escapes mount")
        if write and read_only:
            raise OperationsError("target is mounted read-only")
        return entry, resolved
    raise OperationsError("state path has no supplied mount")


def _can_access(path: Path, mode: int) -> bool:
    options = {"effective_ids": True} if os.access in os.supports_effective_ids else {}
    return os.access(path, mode, **options)


def _preflight_target(kind: str, entry: Path, resolved: Path) -> None:
    """Check metadata/access for the operations this state kind will perform."""
    try:
        info = resolved.stat()
    except FileNotFoundError:
        info = None
    if info is not None and (not stat.S_ISREG(info.st_mode) or not _can_access(resolved, os.R_OK)):
        raise OperationsError("existing state target is not readable")

    # ChatSettings resolves its leaf in __init__; the reply index keeps its
    # original leaf for os.replace while following that leaf for reading.
    destination = resolved if kind == "chat-settings" else entry
    parent = destination.parent
    try:
        parent_info = parent.stat()
    except FileNotFoundError:
        ancestor = parent
        while True:
            ancestor = ancestor.parent
            try:
                ancestor_info = ancestor.stat()
                break
            except FileNotFoundError:
                continue
        if not stat.S_ISDIR(ancestor_info.st_mode) or not _can_access(ancestor, os.W_OK | os.X_OK):
            raise OperationsError("state parent cannot be created")
    else:
        if not stat.S_ISDIR(parent_info.st_mode) or not _can_access(parent, os.R_OK | os.W_OK | os.X_OK):
            raise OperationsError("state parent is not accessible")
        if os.name == "posix" and parent_info.st_mode & stat.S_ISVTX:
            try:
                entry_info = destination.lstat()
            except FileNotFoundError:
                entry_info = None
            uid = os.geteuid()
            if entry_info is not None and uid not in (0, parent_info.st_uid, entry_info.st_uid):
                raise OperationsError("sticky directory prevents replacing state entry")

    if kind == "chat-settings":
        lock = destination.with_name(destination.name + ".lock")
        try:
            lock_info = lock.lstat()
        except FileNotFoundError:
            lock_info = None
        if lock_info is not None and (not stat.S_ISREG(lock_info.st_mode)
                                      or not _can_access(lock, os.R_OK | os.W_OK)):
            raise OperationsError("state lock is not readable and writable")


def state_plan(db_path: Path, mount_args: list[str]) -> dict[str, object]:
    mounts = _mounts(mount_args)
    plans = {}
    for kind, (filename, variable) in _KINDS.items():
        paths = state_paths(
            db_path,
            filename=filename,
            explicit_path=os.getenv(variable, "").strip() or None,
            state_dir=os.getenv("WUWATERM_STATE_DIR", "").strip() or None,
        )
        target = paths.effective
        legacy = paths.legacy
        if mounts:
            entry, resolved = _map_path(target, mounts, write=True)
            _preflight_target(kind, entry, resolved)
            host_target = resolved if kind == "chat-settings" else entry
            host_legacy = (_map_path(legacy, mounts, write=False)[1]
                           if paths.migration_target is not None else legacy)
        else:
            host_target, host_legacy = target, legacy
        action = "none"
        if paths.migration_target is not None and host_legacy.exists():
            if host_target.exists():
                action = "validate-existing"
            elif host_legacy.resolve(strict=False) != host_target.resolve(strict=False):
                if mounts and entry.is_symlink() and not resolved.exists():
                    raise OperationsError("legacy state cannot be copied to a dangling target")
                if mounts and (not host_legacy.is_file() or not _can_access(host_legacy, os.R_OK)):
                    raise OperationsError("legacy state file is not readable")
                action = "copy-if-absent"
        plans[kind] = {
            "effective_path": str(target),
            "legacy_path": str(legacy),
            "migration": action,
            "mapped_target": str(host_target) if mounts else None,
            "mapped_legacy": str(host_legacy) if mounts and paths.migration_target is not None else None,
        }
    return {"database_path": str(db_path), "kinds": plans}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    identity_parser = commands.add_parser("identity", help="report offline installed/build identity")
    identity_parser.add_argument("--artifact-root", type=Path, default=Path("/app"))
    plan_parser = commands.add_parser("state-plan", help="plan state paths without writes")
    plan_parser.add_argument("--db", type=Path, required=True)
    plan_parser.add_argument(
        "--mount", action="append", default=[], metavar="JSON",
        help='mapping as JSON: {"container":"/app/state","host":"/srv/state","read_only":false}',
    )
    args = parser.parse_args(argv)
    try:
        result = identity(args.artifact_root) if args.command == "identity" else state_plan(args.db, args.mount)
    except (OSError, UnicodeError, ValueError, metadata.PackageNotFoundError) as exc:
        print(f"could not determine {args.command}: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
