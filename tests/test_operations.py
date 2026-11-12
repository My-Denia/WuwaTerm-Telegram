"""Offline operations must follow the actual bot's state path rules."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

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


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX file permissions")
@pytest.mark.parametrize("target_exists", [False, True])
def test_mounted_plan_checks_only_legacy_files_that_need_copying(monkeypatch, target_exists):
    from wuwaterm_telegram.operations import OperationsError, state_plan

    with tempfile.TemporaryDirectory(prefix="wuwaterm-state-access-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        data, state = root / "data", root / "state"
        data.mkdir(mode=0o755)
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        legacy = data / "chat_settings.json"
        legacy.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
        legacy.chmod(0o600)
        target = state / legacy.name
        if target_exists:
            target.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
        before = snapshot([legacy] + ([target] if target_exists else []))
        if os.geteuid() != 0:
            legacy.chmod(0)
        monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
        mappings = [mount("/app/data", data, True), mount("/app/state", state)]
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(read_fd)
            try:
                if os.geteuid() == 0:
                    os.setgroups([])
                    os.setgid(65534)
                    os.setuid(65534)
                assert not os.access(legacy, os.R_OK)
                with pytest.raises(PermissionError):
                    legacy.read_bytes()
                if target_exists:
                    plan = state_plan(Path("/app/data/terms.db"), mappings)
                    assert plan["kinds"]["chat-settings"]["migration"] == "validate-existing"
                else:
                    with pytest.raises(OperationsError, match="legacy state file is not readable"):
                        state_plan(Path("/app/data/terms.db"), mappings)
            except BaseException:
                os.write(write_fd, traceback.format_exc().encode("utf-8"))
                os._exit(1)
            os._exit(0)
        os.close(write_fd)
        _, status = os.waitpid(child, 0)
        error = os.read(read_fd, 65536).decode("utf-8")
        os.close(read_fd)
        legacy.chmod(0o600)
        assert os.waitstatus_to_exitcode(status) == 0, error
        assert snapshot([legacy] + ([target] if target_exists else [])) == before
        assert list(state.iterdir()) == ([target] if target_exists else [])


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
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/outside/custom-name.json")
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True), "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert "state path has no supplied mount" in result.stderr
    assert not list(state.iterdir())


def test_mapping_escape_through_symlink_is_rejected(monkeypatch, tmp_path):
    data, state, outside = tmp_path / "data", tmp_path / "state", tmp_path / "outside"
    data.mkdir()
    state.mkdir()
    outside.mkdir()
    (state / "escape").symlink_to("../outside", target_is_directory=True)
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/app/state/escape/custom.json")
    monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "/app/state/replies.json")
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True), "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert "unsupported mount traversal" in result.stderr
    assert not list(outside.iterdir())


def test_installed_identity_has_no_token_or_network_requirement(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token-never-print")
    result = run_ops("identity", "--artifact-root", tmp_path)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["telegram_version"] == "0.2.6"
    assert value["core_version"] == "0.8.2"
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
    core = wheels / "wuwaterm-0.8.2-py3-none-any.whl"
    telegram = wheels / "wuwaterm_telegram-0.2.6-py3-none-any.whl"
    core.write_bytes(b"synthetic core artifact")
    telegram.write_bytes(b"synthetic telegram artifact")
    core_hash = hashlib.sha256(core.read_bytes()).hexdigest()
    tg_hash = hashlib.sha256(telegram.read_bytes()).hexdigest()
    lock = {"schema": 2, "repository": "My-Denia/WuwaTerm", "kind": "github-actions-artifact",
            "filename": core.name, "version": "0.8.2", "sha256": core_hash, "checkout_sha": "b" * 40}
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
    data, state, outside = tmp_path / "data", tmp_path / "state", tmp_path / "outside"
    data.mkdir()
    state.mkdir()
    outside.mkdir()
    other_lock = outside / "foreign-lock"
    other_lock.write_text("existing", encoding="utf-8")
    (state / "chat_settings.json.lock").symlink_to("../outside/foreign-lock")
    monkeypatch.setenv("WUWATERM_SETTINGS_PATH", "/app/state/chat_settings.json")
    monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "/app/state/replies.json")
    before = snapshot([other_lock])
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True), "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert "state lock is not readable and writable" in result.stderr
    assert snapshot([other_lock]) == before


# These cases use the current non-root UID and real file permissions. Runtime
# actions run in a second fixture so preflight snapshots remain untouched.
_ACCESS_CASES = [
    ("read-only-target", True, "settings"),
    ("read-only-target", True, "channel"),
    ("unreadable-settings-with-legacy", False, "settings"),
    ("unreadable-settings-without-legacy", False, "settings"),
    ("unreadable-custom-settings", False, "settings"),
    ("unreadable-channel", False, "channel"),
    ("parent-no-read", False, "settings"),
    ("parent-no-read", False, "channel"),
    ("parent-no-write", False, "settings"),
    ("parent-no-execute", False, "settings"),
    ("settings-lock-write-only", False, "settings"),
    ("settings-lock-read-only", False, "settings"),
    ("legacy-unreadable-target-preserved", True, "settings"),
    ("channel-legacy-unreadable-target-preserved", True, "channel"),
    ("read-only-source-copy", True, "settings"),
    ("missing-nested-wx-ancestor", True, "settings"),
    ("channel-lock-directory", True, "channel"),
    ("channel-lock-symlink", True, "channel"),
    ("channel-link-parent-unwritable", False, "channel"),
    ("channel-link-referent-parent-unwritable", True, "channel"),
    ("settings-link-entry-parent-unwritable", True, "settings"),
    ("settings-link-referent-parent-unwritable", False, "settings"),
    ("settings-parent-symlink-dotdot", False, "settings"),
    ("channel-parent-symlink-dotdot", False, "channel"),
]


def _prepare_access_case(root, case):
    data, state = root / "data", root / "state"
    data.mkdir(parents=True)
    state.mkdir()
    config = {"state_dir": "/app/state", "settings": None, "channel": None}
    settings = state / "chat_settings.json"
    replies = state / "channel_replies.json"
    original = data / "chat_settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if case != "read-only-source-copy" and case != "missing-nested-wx-ancestor":
        if case == "unreadable-custom-settings":
            settings = state / "custom-settings.json"
            config["settings"] = "/app/state/custom-settings.json"
        settings.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    if case != "missing-nested-wx-ancestor":
        replies.write_text('{"version":1,"entries":[]}', encoding="utf-8")
    if case in ("unreadable-settings-with-legacy", "legacy-unreadable-target-preserved", "read-only-source-copy"):
        original.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    if case == "channel-legacy-unreadable-target-preserved":
        (data / "channel_replies.json").write_text('{"version":1,"entries":[]}', encoding="utf-8")
    if case in ("settings-lock-write-only", "settings-lock-read-only"):
        (state / "chat_settings.json.lock").write_text("", encoding="utf-8")
    if case == "channel-lock-directory":
        (state / "channel_replies.json.lock").mkdir()
    if case == "channel-lock-symlink":
        (state / "channel_replies.json.lock").symlink_to("not-used")
    if case.startswith("channel-link-"):
        entry_dir, referent_dir = state / "entry", state / "referent"
        entry_dir.mkdir()
        referent_dir.mkdir()
        referent = referent_dir / "stored.json"
        referent.write_text('{"version":1,"entries":[]}', encoding="utf-8")
        (entry_dir / "custom-replies.json").symlink_to("../referent/stored.json")
        config["channel"] = "/app/state/entry/custom-replies.json"
    if case.startswith("settings-link-"):
        entry_dir, referent_dir = state / "entry", state / "referent"
        entry_dir.mkdir()
        referent_dir.mkdir()
        referent = referent_dir / "stored-settings.json"
        referent.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
        (entry_dir / "custom-settings.json").symlink_to("../referent/stored-settings.json")
        config["settings"] = "/app/state/entry/custom-settings.json"
    if case.endswith("parent-symlink-dotdot"):
        nested = state / "real" / "nested"
        nested.mkdir(parents=True)
        (state / "link").symlink_to("real/nested", target_is_directory=True)
        config["identity"] = True
        config["state_dir"] = str(state)
        if case.startswith("settings"):
            (state / "real" / settings.name).write_text('{"public":{},"allowed":[]}', encoding="utf-8")
            config["settings"] = str(state / "link" / ".." / settings.name)
        else:
            (state / "real" / replies.name).write_text('{"version":1,"entries":[]}', encoding="utf-8")
            config["channel"] = str(state / "link" / ".." / replies.name)
    if case == "missing-nested-wx-ancestor":
        config["state_dir"] = "/app/state/missing/nested"

    # Hold descriptors before removing access; their byte/stat and directory
    # snapshots allow verifying the preflight without changing permissions.
    directories = [root, data, state] + [x for x in state.iterdir() if x.is_dir() and not x.is_symlink()]
    entries = [x for d in directories for x in d.iterdir()]
    dirs = [(str(d), os.open(d, os.O_RDONLY | os.O_DIRECTORY)) for d in directories]
    paths = [(str(p), os.open(p, os.O_PATH | os.O_NOFOLLOW)) for p in entries]
    files = [(str(p), os.open(p, os.O_RDONLY)) for p in entries if p.is_file()]

    if case == "read-only-target":
        settings.chmod(0o400)
        replies.chmod(0o400)
    if case.startswith("unreadable-settings") or case == "unreadable-custom-settings":
        settings.chmod(0)
    if case == "unreadable-channel":
        replies.chmod(0)
    if case == "parent-no-read":
        state.chmod(0o300)
    if case == "parent-no-write":
        state.chmod(0o500)
    if case == "parent-no-execute":
        state.chmod(0o600)
    if case == "settings-lock-write-only":
        (state / "chat_settings.json.lock").chmod(0o200)
    if case == "settings-lock-read-only":
        (state / "chat_settings.json.lock").chmod(0o400)
    if case == "legacy-unreadable-target-preserved":
        original.chmod(0)
        settings.chmod(0o400)
    if case == "channel-legacy-unreadable-target-preserved":
        (data / "channel_replies.json").chmod(0)
        replies.chmod(0o400)
    if case == "read-only-source-copy":
        original.chmod(0o400)
        data.chmod(0o500)
    if case == "missing-nested-wx-ancestor":
        state.chmod(0o300)
    if case == "channel-link-parent-unwritable":
        (state / "entry").chmod(0o500)
    if case == "channel-link-referent-parent-unwritable":
        (state / "referent").chmod(0o500)
    if case == "settings-link-entry-parent-unwritable":
        (state / "entry").chmod(0o500)
    if case == "settings-link-referent-parent-unwritable":
        (state / "referent").chmod(0o500)
    if case.endswith("parent-symlink-dotdot"):
        (state / "real" / (settings.name if case.startswith("settings") else replies.name)).chmod(0)
    return data, state, config, dirs, paths, files


def _snapshot_handles(dirs, paths, files):
    def metadata(fd):
        s = os.fstat(fd)
        return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid, s.st_size, s.st_mtime_ns)
    result = {"directories": {}, "entries": {}, "contents": {}}
    for name, fd in dirs:
        os.lseek(fd, 0, os.SEEK_SET)
        result["directories"][name] = (metadata(fd), sorted(os.listdir(fd)))
    for name, fd in paths:
        result["entries"][name] = metadata(fd)
    for name, fd in files:
        result["contents"][name] = os.pread(fd, os.fstat(fd).st_size, 0)
    return result


def _close_access_handles(dirs, paths, files):
    for _name, fd in dirs + paths + files:
        os.close(fd)


def _runtime_access_result(data, state, config, kind, monkeypatch):
    from wuwaterm_telegram.channel_reply_index import ChannelReplyIndex
    from wuwaterm_telegram.settings import ChatSettings

    def actual(path):
        return str(state) + path.removeprefix("/app/state") if path and path.startswith("/app/state") else path

    monkeypatch.setenv("WUWATERM_STATE_DIR", actual(config["state_dir"]))
    if config["settings"]:
        monkeypatch.setenv("WUWATERM_SETTINGS_PATH", actual(config["settings"]))
    else:
        monkeypatch.delenv("WUWATERM_SETTINGS_PATH", raising=False)
    try:
        if kind == "settings":
            location = bot._chat_settings_path_from_env(data / "terms.db")
            ChatSettings(location).allow(17)
        else:
            cfg = bot.BotConfig(channel_reply_index_path=actual(config["channel"]))
            location = bot._channel_reply_index_path(cfg, data / "terms.db")
            index = ChannelReplyIndex(storage_path=location)
            if index.last_load_succeeded() is False:
                return False
            index._write_payload({"version": 1, "entries": []})
    except (OSError, RuntimeError, ValueError):
        return False
    return True


@pytest.mark.skipif(os.name != "posix" or not hasattr(os, "O_PATH") or os.geteuid() == 0,
                    reason="requires a non-root Linux UID and real POSIX permissions")
@pytest.mark.parametrize("case, expected, kind", _ACCESS_CASES)
def test_mapped_access_matches_real_state_operations(monkeypatch, tmp_path, case, expected, kind):
    plan_root, runtime_root = tmp_path / "plan", tmp_path / "runtime"
    plan_root.mkdir()
    runtime_root.mkdir()
    data, state, config, *handles = _prepare_access_case(plan_root, case)
    run_data, run_state, run_config, *run_handles = _prepare_access_case(runtime_root, case)
    try:
        monkeypatch.setenv("WUWATERM_STATE_DIR", config["state_dir"])
        if config["settings"]:
            monkeypatch.setenv("WUWATERM_SETTINGS_PATH", config["settings"])
        if config["channel"]:
            monkeypatch.setenv("WUWATERM_CHANNEL_REPLY_INDEX_PATH", config["channel"])
        identity = config.get("identity", False)
        mappings = [mount(data if identity else "/app/data", data, True),
                    mount(state if identity else "/app/state", state)]
        before = _snapshot_handles(*handles)
        result = run_ops("state-plan", "--db", data / "terms.db" if identity else "/app/data/terms.db",
                         "--mount", mappings[0], "--mount", mappings[1])
        after = _snapshot_handles(*handles)
        assert after == before
        assert (result.returncode == 0) is expected, result.stderr
        assert _runtime_access_result(run_data, run_state, run_config, kind, monkeypatch) is expected
    finally:
        _close_access_handles(*handles)
        _close_access_handles(*run_handles)
        for path in (state, run_state, data, run_data):
            path.chmod(0o700)
        for root in (state, run_state):
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    child.chmod(0o700)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="requires non-root permissions")
def test_unmapped_mode_reports_path_without_reading_legacy(monkeypatch, tmp_path):
    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    legacy = data / "chat_settings.json"
    legacy.write_text('{"public":{},"allowed":[]}', encoding="utf-8")
    with legacy.open("rb") as keep:
        legacy.chmod(0)
        before = (os.fstat(keep.fileno()), os.pread(keep.fileno(), 1000, 0), sorted(state.iterdir()))
        monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
        result = run_ops("state-plan", "--db", data / "terms.db")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["kinds"]["chat-settings"]["migration"] == "copy-if-absent"
        assert (os.fstat(keep.fileno()), os.pread(keep.fileno(), 1000, 0), sorted(state.iterdir())) == before


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_mapped_dangling_target_with_legacy_rejected_without_changes(monkeypatch, tmp_path, kind):
    filename = "chat_settings.json" if kind == "chat-settings" else "channel_replies.json"
    payload = ('{"public":{},"allowed":[]}' if kind == "chat-settings"
               else '{"version":1,"entries":[]}')
    fixtures = []
    for name in ("preflight", "runtime"):
        base = tmp_path / name
        data, state = base / "data", base / "state"
        data.mkdir(parents=True)
        state.mkdir()
        legacy = data / filename
        legacy.write_text(payload, encoding="utf-8")
        target = state / filename
        target.symlink_to("missing.json")
        fixtures.append((data, state, legacy, target))
    data, state, legacy, target = fixtures[0]

    def snapshot():
        def metadata(path):
            value = path.lstat()
            return value.st_ino, value.st_mode, value.st_uid, value.st_size, value.st_mtime_ns
        return metadata(legacy), legacy.read_bytes(), metadata(target), metadata(state), sorted(p.name for p in state.iterdir())

    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    before = snapshot()
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True), "--mount", mount("/app/state", state))
    assert result.returncode == 1
    assert snapshot() == before
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    no_mount = run_ops("state-plan", "--db", data / "terms.db")
    assert no_mount.returncode == 0, no_mount.stderr
    assert json.loads(no_mount.stdout)["kinds"][kind]["migration"] == "copy-if-absent"
    assert snapshot() == before

    data, state, legacy, target = fixtures[1]
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    if kind == "chat-settings":
        returned = bot._chat_settings_path_from_env(data / "terms.db")
        assert returned == target and returned.is_symlink() and not returned.exists()
    else:
        with pytest.raises(bot.StateMigrationError):
            bot._channel_reply_index_path(bot.BotConfig(), data / "terms.db")
    assert target.is_symlink() and not target.exists()
    assert legacy.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("kind", ["chat-settings", "channel-replies"])
def test_mapped_dangling_target_without_legacy_keeps_runtime_semantics(monkeypatch, tmp_path, kind):
    from wuwaterm_telegram.channel_reply_index import ChannelReplyIndex
    from wuwaterm_telegram.settings import ChatSettings

    data, state = tmp_path / "data", tmp_path / "state"
    data.mkdir()
    state.mkdir()
    filename = "chat_settings.json" if kind == "chat-settings" else "channel_replies.json"
    target = state / filename
    target.symlink_to("missing.json")
    before = (target.lstat().st_ino, target.lstat().st_mtime_ns, sorted(p.name for p in state.iterdir()))
    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True), "--mount", mount("/app/state", state))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["kinds"][kind]["migration"] == "none"
    assert (target.lstat().st_ino, target.lstat().st_mtime_ns, sorted(p.name for p in state.iterdir())) == before
    monkeypatch.setenv("WUWATERM_STATE_DIR", str(state))
    if kind == "chat-settings":
        ChatSettings(bot._chat_settings_path_from_env(data / "terms.db")).allow(17)
        assert target.is_symlink() and target.exists()
    else:
        location = bot._channel_reply_index_path(bot.BotConfig(), data / "terms.db")
        ChannelReplyIndex(storage_path=location)._write_payload({"version": 1, "entries": []})
        assert target.is_file() and not target.is_symlink()
