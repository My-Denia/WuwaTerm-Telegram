"""Local evidence of a recent successful SDK poll, never a network probe."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import time


HEALTH_DIR = Path("/run/wuwaterm-telegram")
RECORD_NAME = "poll.json"
DEFAULT_MAX_AGE = 90
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
_FIELDS = {"schema", "boot_id", "pid", "start_ticks", "success_monotonic_ns",
           "sentinel_fd", "sentinel_dev", "sentinel_inode"}


def enabled() -> bool:
    value = os.getenv("WUWATERM_POLL_HEALTH", "").strip()
    if value not in ("", "0", "1"):
        raise ValueError("WUWATERM_POLL_HEALTH must be 0 or 1")
    return value == "1"


def _directory_fd() -> int:
    fd = os.open(HEALTH_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    mode = os.fstat(fd)
    if mode.st_uid != os.geteuid() or mode.st_mode & 0o700 != 0o700:
        os.close(fd)
        raise ValueError("poll health directory is not owned and writable")
    return fd


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate poll health field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _read(dir_fd: int) -> dict:
    fd = os.open(RECORD_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o600 != 0o600 or info.st_nlink != 1):
            raise ValueError("invalid poll health record")
        raw = file.read(4097)
    if len(raw) > 4096:
        raise ValueError("poll health record too large")
    try:
        item = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid poll health record") from exc
    if not isinstance(item, dict) or set(item) != _FIELDS or type(item["schema"]) is not int or item["schema"] != 1:
        raise ValueError("invalid poll health schema")
    boot = item["boot_id"]
    if not isinstance(boot, str) or not _BOOT_ID.fullmatch(boot):
        raise ValueError("invalid boot identity")
    for name in ("pid", "start_ticks", "success_monotonic_ns", "sentinel_inode"):
        if type(item[name]) is not int or item[name] <= 0:
            raise ValueError("invalid poll health value")
    for name in ("sentinel_fd", "sentinel_dev"):
        if type(item[name]) is not int or item[name] < 0:
            raise ValueError("invalid poll health value")
    return item


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    if not _BOOT_ID.fullmatch(value):
        raise ValueError("invalid boot identity")
    return value


def _start_ticks(pid: int) -> int:
    data = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    prefix, closing, tail = data.rpartition(")")
    if not closing or "(" not in prefix:
        raise ValueError("invalid process identity")
    fields = tail.split()
    if len(fields) < 20 or fields[0] in ("Z", "X", "x"):
        raise ValueError("process is not alive")
    start = int(fields[19])
    if start <= 0:
        raise ValueError("invalid process start")
    return start


def _alive(item: dict) -> bool:
    try:
        if item["boot_id"] != _boot_id() or item["start_ticks"] != _start_ticks(item["pid"]):
            return False
        sentinel = os.stat(f'/proc/{item["pid"]}/fd/{item["sentinel_fd"]}')
        return (stat.S_ISFIFO(sentinel.st_mode) and sentinel.st_dev == item["sentinel_dev"]
                and sentinel.st_ino == item["sentinel_inode"])
    except (OSError, ValueError, OverflowError):
        return False


def record_success(sentinel_fd: int) -> None:
    """Atomically update the reserved file after a complete SDK poll."""
    import fcntl  # This optional health interface is Linux-only.

    sentinel = os.fstat(sentinel_fd)
    if not stat.S_ISFIFO(sentinel.st_mode):
        raise ValueError("invalid polling sentinel")
    pid = os.getpid()
    current = {
        "schema": 1,
        "boot_id": _boot_id(),
        "pid": pid,
        "start_ticks": _start_ticks(pid),
        "success_monotonic_ns": time.monotonic_ns(),
        "sentinel_fd": sentinel_fd,
        "sentinel_dev": sentinel.st_dev,
        "sentinel_inode": sentinel.st_ino,
    }
    dir_fd = _directory_fd()
    name = ".poll-" + secrets.token_hex(12)
    temp_created = False
    try:
        fcntl.flock(dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            previous = _read(dir_fd)
        except FileNotFoundError:
            previous = None
        if previous and _alive(previous) and (
            previous["pid"] != pid or previous["start_ticks"] != current["start_ticks"]
            or previous["sentinel_fd"] != sentinel_fd
            or previous["sentinel_dev"] != sentinel.st_dev
            or previous["sentinel_inode"] != sentinel.st_ino
        ):
            raise ValueError("another poller owns the health record")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        temp_created = True
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(current, file, sort_keys=True, separators=(",", ":"))
        os.replace(name, RECORD_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_created = False
    finally:
        if temp_created:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
        os.close(dir_fd)


def health(max_age: int = DEFAULT_MAX_AGE) -> dict:
    if type(max_age) is not int or not 1 <= max_age <= 3600:
        raise ValueError("invalid poll age")
    dir_fd = _directory_fd()
    try:
        item = _read(dir_fd)
    finally:
        os.close(dir_fd)
    age = time.monotonic_ns() - item["success_monotonic_ns"]
    if age < 0 or age > max_age * 1_000_000_000 or not _alive(item):
        raise ValueError("poll health is not current")
    return {"status": "ready", "pid": item["pid"], "age_seconds": round(age / 1_000_000_000, 3)}
