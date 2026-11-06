from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from wuwaterm_telegram.settings import ChatSettings, ChatSettingsError


def _run_validator(*args: str | Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(cwd),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-m", "wuwaterm_telegram.state_validation", *map(str, args)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _file_identity(path: Path) -> tuple[bytes, int, int, int, int, int]:
    contents = path.read_bytes()
    stat = path.stat()
    return contents, stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns


@pytest.mark.parametrize(
    ("name", "kind", "payload"),
    [
        ("channel_replies.json", "chat-settings", {"public": {"-17": True}, "allowed": [-17, "18"]}),
        ("custom-settings.data", "chat-settings", {}),
        ("chat_settings.json", "channel-replies", {"version": 1, "entries": []}),
        (
            "custom-replies.data",
            "channel-replies",
            {"version": 1, "entries": [{"chat_id": -17, "message_id": 23,
                                        "expires_at": 99.5, "reply_message_ids": [24, 25]}]},
        ),
    ],
)
def test_valid_state_uses_explicit_kind_and_preserves_file(tmp_path, name, kind, payload):
    # A DB-adjacent custom path is as valid as a default state filename.
    data = tmp_path / "data"
    data.mkdir()
    db = data / "terms.db"
    db.write_bytes(b"unopened database fixture")
    path = data / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = _file_identity(path)
    db_before = _file_identity(db)
    names_before = {item.name for item in data.iterdir()}

    result = _run_validator(path, kind, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert _file_identity(path) == before
    assert _file_identity(db) == db_before
    assert {item.name for item in data.iterdir()} == names_before


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("chat-settings", b'{"public":'),
        ("chat-settings", b"\xff"),
        ("chat-settings", b'{"public":{},"allowed":[],"extra":NaN}'),
        ("chat-settings", b"[]"),
        ("chat-settings", b'{"public": []}'),
        ("chat-settings", b'{"allowed": {}}'),
        ("chat-settings", b'{"public": {"01": true}}'),
        ("chat-settings", b'{"public": {"-17": 1}}'),
        ("chat-settings", b'{"allowed": [true]}'),
        ("chat-settings", b'{"allowed": [1.5]}'),
        ("channel-replies", b"{}"),
        ("channel-replies", b'{"version": 2, "entries": []}'),
        ("channel-replies", b'{"version": true, "entries": []}'),
        ("channel-replies", b'{"version": 1, "entries": {}}'),
        ("channel-replies", b'{"version": 1, "entries": [{}]}'),
        ("channel-replies", b'{"version": 1, "entries": [{"chat_id": true, "message_id": 1, "expires_at": 3, "reply_message_ids": [4]}]}'),
        ("channel-replies", b'{"version": 1, "entries": [{"chat_id": 1, "message_id": 2, "expires_at": 3, "reply_message_ids": []}]}'),
    ],
)
def test_invalid_state_is_reported_without_modification(tmp_path, kind, content):
    path = tmp_path / "custom-state"
    path.write_bytes(content)
    before = _file_identity(path)
    names_before = {item.name for item in tmp_path.iterdir()}

    result = _run_validator(path, kind, cwd=tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "invalid or unreadable" in result.stderr
    assert _file_identity(path) == before
    assert {item.name for item in tmp_path.iterdir()} == names_before


def test_missing_unreadable_and_directory_inputs_fail_without_creating_paths(tmp_path):
    missing = tmp_path / "absent" / "custom-settings"
    assert _run_validator(missing, "chat-settings", cwd=tmp_path).returncode == 1
    assert not missing.parent.exists()
    assert _run_validator(tmp_path, "channel-replies", cwd=tmp_path).returncode == 1

    unreadable = tmp_path / "unreadable"
    unreadable.write_text("{}", encoding="utf-8")
    unreadable.chmod(0)
    if os.access(unreadable, os.R_OK):
        pytest.skip("test process can read files regardless of permission bits")
    before = unreadable.stat()
    assert _run_validator(unreadable, "chat-settings", cwd=tmp_path).returncode == 1
    after = unreadable.stat()
    assert (after.st_ino, after.st_mode, after.st_mtime_ns) == (
        before.st_ino, before.st_mode, before.st_mtime_ns
    )


@pytest.mark.parametrize("args", [(), ("missing",), ("missing", "bad-kind"), ("missing", "chat-settings", "extra")])
def test_invalid_usage_exits_two(tmp_path, args):
    result = _run_validator(*args, cwd=tmp_path)
    assert result.returncode == 2


def test_strict_validation_keeps_loose_load_and_refuse_overwrite(tmp_path):
    path = tmp_path / "settings"
    original = b'{"public":{"-17":true,"01":true},"allowed":[-17,true]}'
    path.write_bytes(original)

    assert _run_validator(path, "chat-settings", cwd=tmp_path).returncode == 1
    settings = ChatSettings(path)
    assert settings.is_public(-17)
    assert settings.is_allowed(-17)
    assert not settings.is_allowed(1)
    with pytest.raises(ChatSettingsError):
        settings.allow(-18)
    assert path.read_bytes() == original


def test_validator_does_not_load_env_import_runtime_or_write(tmp_path):
    guard = tmp_path / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        '''import os, sys
def audit(event, args):
    if event == "open":
        path, mode, flags = args
        if isinstance(path, str) and os.path.basename(path) == ".env":
            raise AssertionError("dotenv read")
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            raise AssertionError("file write")
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("file write")
    if event.startswith("socket."):
        raise AssertionError("socket use")
    if event == "import" and (args[0].split(".")[0] in {"telegram", "wuwaterm", "dotenv"} or args[0] == "wuwaterm_telegram.bot"):
        raise AssertionError("runtime import")
sys.addaudithook(audit)
''', encoding="utf-8"
    )
    (tmp_path / ".env").write_text("TOKEN=not-a-real-token\n", encoding="utf-8")
    path = tmp_path / "settings"
    path.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    before = _file_identity(path)
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(guard), os.environ.get("PYTHONPATH", ""))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    result = subprocess.run(
        [sys.executable, "-m", "wuwaterm_telegram.state_validation", str(path), "chat-settings"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
    assert _file_identity(path) == before
