"""Offline operations must follow the actual bot's state path rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from wuwaterm_telegram import bot


@pytest.fixture(autouse=True)
def no_state_overrides(monkeypatch):
    for key in ("WUWATERM_STATE_DIR", "WUWATERM_SETTINGS_PATH", "WUWATERM_CHANNEL_REPLY_INDEX_PATH"):
        monkeypatch.delenv(key, raising=False)


def run_ops(*args, env=None):
    command = [sys.executable, "-B", "-m", "wuwaterm_telegram.operations", *map(str, args)]
    return subprocess.run(command, env=env or os.environ, capture_output=True, text=True, check=False)


def mount(container, host, read_only=False):
    return json.dumps({"container": str(container), "host": str(host), "read_only": read_only})


def snapshot(paths):
    return {str(path): (path.read_bytes(), path.stat().st_ino, path.stat().st_mode, path.stat().st_mtime_ns)
            for path in paths}


@pytest.mark.parametrize("case", ["default", "directory", "legacy-explicit", "target-explicit", "custom"])
def test_plan_matches_runtime_and_does_not_copy(monkeypatch, tmp_path, case):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "terms.db"
    db.write_bytes(b"fixture")
    state = tmp_path / "state"
    state.mkdir()
    old_settings = data / "chat_settings.json"
    old_replies = data / "channel_replies.json"
    old_settings.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    old_replies.write_text('{"version":1,"entries":[]}', encoding="utf-8")
    if case != "default":
        monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    if case == "legacy-explicit":
        monkeypatch.setenv("WUWATERM_SETTINGS_PATH", str(old_settings))
        monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", str(old_replies))
    if case == "target-explicit":
        monkeypatch.setenv("WUWATERM_SETTINGS_PATH", str(state / old_settings.name))
        monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", str(state / old_replies.name))
    if case == "custom":
        monkeypatch.setenv("WUWATERM_SETTINGS_PATH", str(state / "other-settings.json"))
        monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", str(state / "other-replies.json"))
    before = snapshot([db, old_settings, old_replies])
    result = run_ops("state-plan", "--db", db)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)["kinds"]
    assert snapshot([db, old_settings, old_replies]) == before
    assert not list(state.iterdir())
    # Exercise the real runtime path and migration after proving the plan was read-only.
    actual_settings = bot._chat_settings_path_from_env(db)
    actual_replies = bot._channel_reply_index_path(bot.BotConfig.from_env(), db)
    assert Path(plan["chat-settings"]["effective_path"]) == actual_settings
    assert Path(plan["channel-replies"]["effective_path"]) == actual_replies
    expected = "copy-if-absent" if case in ("directory", "legacy-explicit", "target-explicit") else "none"
    assert plan["chat-settings"]["migration"] == expected
    assert plan["channel-replies"]["migration"] == expected


def test_existing_target_is_preserved(monkeypatch, tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    old = data / "chat_settings.json"
    target = state / "chat_settings.json"
    old.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    target.write_text('{"public":{},"allowed":[1]}', encoding="utf-8")
    before = snapshot([old, target])
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    result = run_ops("state-plan", "--db", data / "terms.db")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["kinds"]["chat-settings"]["migration"] == "validate-existing"
    assert snapshot([old, target]) == before
    assert bot._chat_settings_path_from_env(data / "terms.db") == target
    assert snapshot([old, target]) == before


def test_mounted_plan_fails_closed_and_reports_original_source(monkeypatch, tmp_path):
    data, state = tmp_path / "host-data", tmp_path / "host-state"
    data.mkdir()
    state.mkdir()
    source = data / "chat_settings.json"
    source.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    before = snapshot([source])
    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    data_mount = mount("/app/data", data, True)
    state_mount = mount("/app/state", state)
    result = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", data_mount, "--mount", state_mount)
    assert result.returncode == 0, result.stderr
    item = json.loads(result.stdout)["kinds"]["chat-settings"]
    assert item["migration"] == "copy-if-absent"
    assert item["mapped_legacy"] == str(source)
    assert item["mapped_target"] == str(state / "chat_settings.json")
    assert snapshot([source]) == before and not list(state.iterdir())
    no_source_mapping = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", state_mount)
    assert no_source_mapping.returncode == 1
    read_only_target = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", data_mount, "--mount", mount("/app/state", state, True))
    assert read_only_target.returncode == 1
    state.chmod(0o555)
    unwritable = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", data_mount, "--mount", state_mount)
    assert unwritable.returncode == 1
    assert snapshot([source]) == before and not list(state.iterdir())


def test_explicit_custom_unmapped_is_rejected(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/outside/custom-name.json")
    result = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert not list(state.iterdir())


def test_mapping_escape_through_symlink_is_rejected(monkeypatch, tmp_path):
    state, outside = tmp_path / "state", tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    (state / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/app/state/escape/custom.json")
    monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "/app/state/replies.json")
    result = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert not list(outside.iterdir())


def test_installed_identity_has_no_token_or_network_requirement(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token-never-print")
    result = run_ops("identity", "--artifact-root", tmp_path)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["telegram_version"] == "0.1.0.dev1"
    assert value["core_version"] == "0.7.0.dev1"
    assert value["source_revision_claim"] is None
    assert value["artifacts_matching_supplied_records"] is None
    assert "synthetic-token-never-print" not in result.stdout + result.stderr


def test_revision_must_be_valid_and_is_a_claim(tmp_path):
    revision = "a" * 40
    (tmp_path / ".build-revision").write_text(revision, encoding="ascii")
    result = run_ops("identity", "--artifact-root", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source_revision_claim"] == revision
    (tmp_path / ".build-revision").write_text("not-a-revision", encoding="ascii")
    assert run_ops("identity", "--artifact-root", tmp_path).returncode == 1


def test_identity_refuses_changed_wheel_or_lock_version(tmp_path):
    import hashlib
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    core = wheels / "wuwaterm-0.7.0.dev1-py3-none-any.whl"
    telegram = wheels / "wuwaterm_telegram-0.1.0.dev1-py3-none-any.whl"
    core.write_bytes(b"synthetic core artifact")
    telegram.write_bytes(b"synthetic telegram artifact")
    core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
    tg_hash = hashlib.sha256(telegram.read_bytes()).hexdigest()
    lock = {"schema": 2, "repository": "My-Denia/WuwaTerm", "kind": "github-actions-artifact",
            "filename": core.name, "version": "0.7.0.dev1", "sha256": core_hash, "checkout_sha": "b" * 40}
    (tmp_path / "core-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    (wheels / "core.filename").write_text(core.name, encoding="ascii")
    (wheels / "telegram.filename").write_text(telegram.name, encoding="ascii")
    (wheels / "telegram.sha256").write_text(tg_hash, encoding="ascii")
    result = run_ops("identity", "--artifact-root", tmp_path)
    assert result.returncode == 0, result.stderr
    artifacts = json.loads(result.stdout)["artifacts_matching_supplied_records"]
    assert artifacts["core_wheel_sha256"] == core_hash
    assert artifacts["telegram_wheel_sha256"] == tg_hash
    assert artifacts["core_producer_kind"] == "github-actions-artifact"
    assert artifacts["core_source_revision_claim"] == "b" * 40
    lock["kind"] = "github-release"
    lock["source_commit"] = "c" * 40
    (tmp_path / "core-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    result = run_ops("identity", "--artifact-root", tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["artifacts_matching_supplied_records"]["core_source_revision_claim"] == "c" * 40
    lock["kind"] = "unknown"
    (tmp_path / "core-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    assert run_ops("identity", "--artifact-root", tmp_path).returncode == 1
    lock["kind"] = "github-release"
    (tmp_path / "core-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    telegram.write_bytes(b"modified artifact")
    assert run_ops("identity", "--artifact-root", tmp_path).returncode == 1
    telegram.write_bytes(b"synthetic telegram artifact")
    lock["version"] = "0.6.1"
    (tmp_path / "core-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    assert run_ops("identity", "--artifact-root", tmp_path).returncode == 1


def test_mapped_settings_lock_cannot_point_outside_mount(monkeypatch, tmp_path):
    state, outside = tmp_path / "state", tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    other_lock = outside / "foreign-lock"
    other_lock.write_text("existing", encoding="utf-8")
    (state / "chat_settings.json.lock").symlink_to(other_lock)
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/app/state/chat_settings.json")
    monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "/app/state/replies.json")
    before = snapshot([other_lock])
    result = run_ops("state-plan", "--db", "/app/data/terms.db", "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert snapshot([other_lock]) == before
