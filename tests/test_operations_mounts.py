"""Mounted planning follows container paths, including nested bind mounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

from wuwaterm_telegram import bot
from wuwaterm_telegram.operations import OperationsError, state_plan


KINDS = {
    "settings": ("WUWATERM_SETTINGS_PATH", "chat-settings", "chat_settings.json"),
    "channel": ("WUWATERM_CHANNEL_REPLY_INDEX_PATH", "channel-replies", "channel_replies.json"),
}


def mount(container, host, read_only=False):
    return json.dumps({"container": str(container), "host": str(host), "read_only": read_only})


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("WUWATERM_STATE_DIR", *(item[0] for item in KINDS.values())):
        monkeypatch.delenv(name, raising=False)


def fixture(base):
    data, outer, inner = (base / name for name in ("data", "outer", "inner"))
    for path in (data, outer, inner):
        path.mkdir(parents=True)
    decoy = outer / "nested"
    decoy.mkdir()
    for name in ("chat_settings.json", "channel_replies.json"):
        (decoy / name).write_text("hidden decoy", encoding="utf-8")
    decoy.chmod(0o555)
    return data, outer, inner


def snapshot(base):
    result = {}
    for path in [base, *sorted(base.rglob("*"))]:
        info = path.lstat()
        row = [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
               info.st_size, info.st_mtime_ns]
        if stat.S_ISLNK(info.st_mode):
            row.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            row.append(path.read_bytes())
        elif stat.S_ISDIR(info.st_mode):
            row.append(sorted(item.name for item in path.iterdir()))
        result[str(path.relative_to(base))] = row
    return result


def configure(monkeypatch, settings, channel):
    monkeypatch.setenv(KINDS["settings"][0], settings)
    monkeypatch.setenv(KINDS["channel"][0], channel)


@pytest.mark.parametrize("kind", KINDS)
def test_direct_nested_read_only_rejected_despite_writable_outer(monkeypatch, tmp_path, kind):
    data, outer, inner = fixture(tmp_path)
    configure(monkeypatch, "/app/state/safe-settings.json", "/app/state/safe-channel.json")
    variable, _, filename = KINDS[kind]
    monkeypatch.setenv(variable, "/app/state/nested/" + filename)
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner, True)]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="target is mounted read-only"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", KINDS)
def test_direct_nested_writable_overrides_read_only_outer(monkeypatch, tmp_path, kind):
    data, outer, inner = fixture(tmp_path)
    configure(monkeypatch, "/app/state/nested/chat_settings.json",
              "/app/state/nested/channel_replies.json")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer, True),
              mount("/app/state/nested", inner)]
    before = snapshot(tmp_path)
    result = state_plan(Path("/app/data/terms.db"), mounts)
    _, key, filename = KINDS[kind]
    assert result["kinds"][key]["mapped_target"] == str(inner / filename)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("nested_ro", [True, False])
@pytest.mark.parametrize("link", ["../nested/target.json", "../nested/deep/../../safe.json"])
def test_cross_mount_link_and_return_rejected_before_hidden_backing(monkeypatch, tmp_path, kind, nested_ro, link):
    data, outer, inner = fixture(tmp_path)
    entry = outer / "entry"
    entry.mkdir()
    (entry / "link.json").symlink_to(link)
    configure(monkeypatch, "/app/state/safe-settings.json", "/app/state/safe-channel.json")
    monkeypatch.setenv(KINDS[kind][0], "/app/state/entry/link.json")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner, nested_ro)]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="unsupported mount traversal"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", KINDS)
def test_missing_tail_does_not_hide_later_mount_crossing(monkeypatch, tmp_path, kind):
    data, outer, inner = fixture(tmp_path)
    configure(monkeypatch, "/app/state/safe-settings.json", "/app/state/safe-channel.json")
    monkeypatch.setenv(KINDS[kind][0], "/app/state/missing/../nested/target.json")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner)]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="unsupported mount traversal"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("link, target", [
    ("real/sub", "/app/state/link/../stored.json"),
    ("/app/state/real", "/app/state/link/stored.json"),
])
def test_same_mount_links_and_dotdot_use_container_namespace(monkeypatch, tmp_path, kind, link, target):
    data, outer, inner = fixture(tmp_path)
    (outer / "real" / "sub").mkdir(parents=True)
    (outer / "link").symlink_to(link, target_is_directory=True)
    configure(monkeypatch, "/app/state/safe-settings.json", "/app/state/safe-channel.json")
    monkeypatch.setenv(KINDS[kind][0], target)
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner)]
    before = snapshot(tmp_path)
    result = state_plan(Path("/app/data/terms.db"), mounts)
    assert result["kinds"][KINDS[kind][1]]["mapped_target"] == str(outer / "real" / "stored.json")
    assert snapshot(tmp_path) == before


def test_overlapping_host_roots_do_not_infer_container_mount(monkeypatch, tmp_path):
    data, shared = tmp_path / "data", tmp_path / "shared"
    data.mkdir()
    shared.mkdir()
    mounts = [mount("/app/data", data, True), mount("/app/rw", shared),
              mount("/app/ro", shared, True)]
    configure(monkeypatch, "/app/rw/chat_settings.json", "/app/rw/channel_replies.json")
    before = snapshot(tmp_path)
    result = state_plan(Path("/app/data/terms.db"), mounts)
    assert result["kinds"]["chat-settings"]["mapped_target"] == str(shared / "chat_settings.json")
    monkeypatch.setenv(KINDS["channel"][0], "/app/ro/channel_replies.json")
    with pytest.raises(OperationsError, match="target is mounted read-only"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("alias", ["legacy", "target"])
def test_relative_cwd_db_and_explicit_alias_match_runtime(monkeypatch, tmp_path, alias):
    def prepare(base):
        data, state = base / "data", base / "state"
        (data / "real").mkdir(parents=True)
        state.mkdir()
        (data / "link").symlink_to("real", target_is_directory=True)
        (data / "real" / "chat_settings.json").write_text('{"public":{},"allowed":[]}', encoding="utf-8")
        (data / "real" / "channel_replies.json").write_text('{"version":1,"entries":[]}', encoding="utf-8")
        for _, _, filename in KINDS.values():
            (state / ("alias-" + filename)).symlink_to(filename)
        return data, state

    plan_root, run_root = tmp_path / "plan", tmp_path / "runtime"
    data, state = prepare(plan_root)
    prepare(run_root)
    monkeypatch.setenv("WUWATERM_STATE_DIR", "state")
    for variable, _, filename in KINDS.values():
        path = "data/link/" + filename if alias == "legacy" else "state/alias-" + filename
        monkeypatch.setenv(variable, path)
    monkeypatch.chdir(plan_root)
    before = snapshot(plan_root)
    result = state_plan(Path("data/link/terms.db"), [mount(data, data, True), mount(state, state)])
    for _, key, filename in KINDS.values():
        item = result["kinds"][key]
        assert item["effective_path"] == "state/" + filename
        assert item["legacy_path"] == str(data / "real" / filename)
        assert item["migration"] == "copy-if-absent"
    assert snapshot(plan_root) == before
    monkeypatch.chdir(run_root)
    db = Path("data/link/terms.db")
    assert bot._chat_settings_path_from_env(db) == Path("state/chat_settings.json")
    assert bot._channel_reply_index_path(bot.BotConfig.from_env(), db) == Path("state/channel_replies.json")
    for _, _, filename in KINDS.values():
        assert (run_root / "state" / filename).read_bytes() == (run_root / "data" / "real" / filename).read_bytes()


def test_existing_absolute_container_links_validate_existing_targets(monkeypatch, tmp_path):
    data, outer, inner = fixture(tmp_path)
    (outer / "real").mkdir()
    for _, _, filename in KINDS.values():
        (data / filename).write_text("legacy", encoding="utf-8")
        (outer / "real" / filename).write_text("existing", encoding="utf-8")
        (outer / filename).symlink_to("/app/state/real/" + filename)
    monkeypatch.setenv("WUWATERM_STATE_DIR", "/app/state")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner)]
    before = snapshot(tmp_path)
    result = state_plan(Path("/app/data/terms.db"), mounts)
    for _, key, _ in KINDS.values():
        assert result["kinds"][key]["migration"] == "validate-existing"
    assert snapshot(tmp_path) == before


def test_nested_mount_behind_unestablished_parent_alias_rejected(monkeypatch, tmp_path):
    data, outer, inner = fixture(tmp_path)
    (outer / "real").mkdir()
    (outer / "alias").symlink_to("real", target_is_directory=True)
    configure(monkeypatch, "/app/state/alias/nested/chat_settings.json",
              "/app/state/alias/nested/channel_replies.json")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/alias/nested", inner)]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="unsupported mount traversal"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


def test_cli_reports_static_unsupported_reason_without_paths(monkeypatch, tmp_path):
    from test_operations import run_ops

    data, outer, inner = fixture(tmp_path)
    (outer / "link").symlink_to("nested/target.json")
    configure(monkeypatch, "/app/state/link", "/app/state/safe-channel.json")
    result = run_ops("state-plan", "--db", "/app/data/terms.db",
                     "--mount", mount("/app/data", data, True),
                     "--mount", mount("/app/state", outer),
                     "--mount", mount("/app/state/nested", inner))
    assert result.returncode == 1
    assert "unsupported mount traversal" in result.stderr
    assert str(tmp_path) not in result.stderr
    assert "/app/state" not in result.stderr


def test_overlapping_host_backings_do_not_establish_cross_mount_links(monkeypatch, tmp_path):
    data, shared = tmp_path / "data", tmp_path / "shared"
    data.mkdir()
    (shared / "child").mkdir(parents=True)
    (shared / "link.json").symlink_to("/app/inner/target.json")
    configure(monkeypatch, "/app/outer/link.json", "/app/outer/safe-channel.json")
    mounts = [mount("/app/data", data, True), mount("/app/outer", shared),
              mount("/app/inner", shared / "child")]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="unsupported mount traversal"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before


def test_symlink_loop_has_static_unsupported_reason(monkeypatch, tmp_path):
    data, outer, inner = fixture(tmp_path)
    (outer / "cycle-a").symlink_to("cycle-b")
    (outer / "cycle-b").symlink_to("cycle-a")
    configure(monkeypatch, "/app/state/cycle-a", "/app/state/safe-channel.json")
    mounts = [mount("/app/data", data, True), mount("/app/state", outer),
              mount("/app/state/nested", inner)]
    before = snapshot(tmp_path)
    with pytest.raises(OperationsError, match="unsupported mount traversal"):
        state_plan(Path("/app/data/terms.db"), mounts)
    assert snapshot(tmp_path) == before
