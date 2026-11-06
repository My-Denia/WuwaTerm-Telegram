"""State migration must preserve the existing fail-closed semantics."""
from __future__ import annotations

from pathlib import Path

import pytest

import wuwaterm_telegram.bot as bot
from wuwaterm_telegram.runtime_keys import CHAT_SETTINGS_KEY
from wuwaterm_telegram.settings import ChatSettingsError


LEGACY = '{"public":{"-2001":true},"allowed":[-2001]}'
COMPETING = '{"public":{},"allowed":[-3001]}'


def test_corrupt_target_without_legacy_loads_closed_and_refuses_overwrite(monkeypatch, sample_db, tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    target = state_dir / 'chat_settings.json'
    target.write_text('{broken')
    monkeypatch.setenv('WUWATERM_STATE_DIR', str(state_dir))
    application = bot.create_application('123:LOCAL', sample_db, config=bot.BotConfig())
    settings = application.bot_data[CHAT_SETTINGS_KEY]
    assert not settings.is_allowed(-2001)
    with pytest.raises(ChatSettingsError):
        settings.allow(-2001)
    assert not settings.is_allowed(-2001)
    assert target.read_text() == '{broken'


def test_migration_racing_target_does_not_overwrite_competitor(monkeypatch, tmp_path):
    legacy = tmp_path / 'legacy' / 'chat_settings.json'
    target = tmp_path / 'state' / 'chat_settings.json'
    legacy.parent.mkdir()
    legacy.write_text(LEGACY)

    def competitor_wins(_source, destination):
        Path(destination).write_text(COMPETING)
        raise FileExistsError()

    monkeypatch.setattr(bot.os, 'link', competitor_wins)
    bot._migrate_legacy_state_file(legacy, target, label='chat settings')
    assert legacy.read_text() == LEGACY
    assert target.read_text() == COMPETING


def test_migration_fsync_failure_reports_failure_and_preserves_legacy(monkeypatch, tmp_path):
    legacy = tmp_path / 'legacy' / 'chat_settings.json'
    target = tmp_path / 'state' / 'chat_settings.json'
    legacy.parent.mkdir()
    legacy.write_text(LEGACY)

    def fail_fsync(_fd):
        raise OSError('fixture fsync failure')

    monkeypatch.setattr(bot.os, 'fsync', fail_fsync)
    with pytest.raises(bot.StateMigrationError):
        bot._migrate_legacy_state_file(legacy, target, label='chat settings')
    assert legacy.read_text() == LEGACY
    assert not target.exists()
