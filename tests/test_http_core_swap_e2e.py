"""Zero-cost end-to-end acceptance tests for pure HTTP Core mode & hot-swap upgrades.

Uses only a local loopback mock HTTP Core server (``127.0.0.1:0``) and mock
Telegram message objects: zero external network, zero real Telegram API calls,
and zero paid LLM calls.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from wuwaterm_telegram import bot, cli, operations
from wuwaterm_telegram.bot import (
    BotConfig,
    BotConfigError,
    about_command,
    create_application,
    run_bot,
    sentence_command,
    status_command,
    term_command,
)
from wuwaterm_telegram.channel import channel_post_handler, execute_queued_channel_task
from wuwaterm_telegram.core_backend import (
    CAP_DICTIONARY_IDENTITY,
    CAP_KNOWLEDGE_RETRIEVAL,
    CAP_PROTECTED_TRANSLATION,
    CAP_SEMANTIC_REVIEW,
    CAP_STRUCTURE_CHECK,
    CAP_TRANSLATION_CONTEXT,
    HttpCoreBackend,
    HttpReviewBackend,
    LocalCoreBackend,
)
from wuwaterm_telegram.review_bot import REVIEW_BACKEND_KEY, review_command
from wuwaterm_telegram.runtime_keys import (
    CHANNEL_QUEUE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHAT_SETTINGS_KEY,
    CORE_BACKEND_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
)


class _SwappableCoreState:
    """Mutable state for an in-place upgradeable mock Core HTTP API service."""

    def __init__(self, *, required_token: str | None = None) -> None:
        self.required_token = required_token
        self.lock = threading.Lock()
        self.version = "v1"
        self.fail_translations_once = False
        self.force_503 = False
        self.requests: list[dict[str, Any]] = []
        self._set_profile_locked("v1")

    def _set_profile_locked(self, version: str) -> None:
        self.version = version
        if version == "v1":
            self.service_version = "0.16.0"
            self.dictionary_revision = "rev_1"
            self.source_commit = "1111111111111111111111111111111111111111"
            self.term_count = 100
            self.capabilities = [
                CAP_TRANSLATION_CONTEXT,
                CAP_STRUCTURE_CHECK,
                CAP_DICTIONARY_IDENTITY,
                CAP_PROTECTED_TRANSLATION,
                CAP_SEMANTIC_REVIEW,
            ]
        elif version == "v2":
            self.service_version = "0.17.0"
            self.dictionary_revision = "rev_2"
            self.source_commit = "2222222222222222222222222222222222222222"
            self.term_count = 128
            self.capabilities = [
                CAP_TRANSLATION_CONTEXT,
                CAP_STRUCTURE_CHECK,
                CAP_DICTIONARY_IDENTITY,
                CAP_PROTECTED_TRANSLATION,
                CAP_SEMANTIC_REVIEW,
                CAP_KNOWLEDGE_RETRIEVAL,
            ]
        else:
            raise ValueError(f"unknown version: {version}")

    def activate_v1(self) -> None:
        with self.lock:
            self._set_profile_locked("v1")

    def activate_v2(self) -> None:
        with self.lock:
            self._set_profile_locked("v2")

    def translate_segment(self, seg: str) -> str:
        if not seg:
            return seg
        if self.version == "v2":
            replacements = (
                ("今汐", "Jinhsi [RAG: Magistrate of Jinzhou]"),
                ("共鸣回路", "Resonance Circuit (Incandescence)"),
                ("云陵谷", "Gorges of Spirits [RAG]"),
                ("触发了", " triggered "),
                ("在", " in "),
                ("使用", " used "),
                ("。", "."),
            )
        else:
            replacements = (
                ("今汐", "Jinhsi"),
                ("共鸣回路", "Resonance Circuit"),
                ("云陵谷", "Gorges of Spirits"),
                ("触发了", " triggered "),
                ("在", " in "),
                ("使用", " used "),
                ("。", "."),
            )
        out = seg
        for src, tgt in replacements:
            out = out.replace(src, tgt)
        return out


@contextmanager
def swappable_core_http_server(*, required_token: str | None = None):
    state = _SwappableCoreState(required_token=required_token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            with suppress(BrokenPipeError):
                self.wfile.write(raw)

        def _check_auth(self) -> bool:
            if state.required_token is None:
                return True
            expected = f"Bearer {state.required_token}"
            if self.headers.get("Authorization") != expected:
                self._send_json(401, {"error": {"code": "unauthorized"}})
                return False
            return True

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            with state.lock:
                state.requests.append(
                    {
                        "method": "GET",
                        "path": path,
                        "query": query,
                        "auth": self.headers.get("Authorization"),
                        "version": state.version,
                    }
                )
                force_503 = state.force_503
                service_version = state.service_version
                source_commit = state.source_commit
                term_count = state.term_count
                capabilities = list(state.capabilities)
                dictionary_revision = state.dictionary_revision

            if not self._check_auth():
                return
            if force_503:
                self._send_json(503, {"error": {"code": "llm_unavailable"}})
                return

            if path == "/v1/meta":
                self._send_json(
                    200,
                    {
                        "service_version": service_version,
                        "api_version": "v1",
                        "schema_version": "2",
                        "source_profile": "official",
                        "source_commit": source_commit,
                        "source_game_version": "3.7",
                        "source_resource_version": None,
                        "source_changelist": None,
                        "term_count": term_count,
                        "llm_configured": True,
                        "request_id": f"req-meta-{service_version}",
                    },
                )
                return
            if path == "/v1/capabilities":
                self._send_json(
                    200,
                    {
                        "api_version": "v1",
                        "capabilities": capabilities,
                        "request_id": f"req-caps-{service_version}",
                    },
                )
                return
            if path == "/v1/dictionary":
                self._send_json(
                    200,
                    {
                        "revision": dictionary_revision,
                        "schema_version": "2",
                        "source_commit": source_commit,
                        "source_game_version": "3.7",
                        "term_count": term_count,
                        "request_id": f"req-dict-{dictionary_revision}",
                    },
                )
                return
            if path == "/v1/terms":
                from urllib.parse import parse_qs

                q = (parse_qs(query).get("q") or [""])[0]
                matches = []
                if q == "声骸":
                    matches.append(
                        {
                            "zh": "声骸",
                            "en": "Echo",
                            "category": "term",
                            "score": 100.0,
                            "reason": "exact",
                        }
                    )
                elif q == "今汐":
                    matches.append(
                        {
                            "zh": "今汐",
                            "en": "Jinhsi",
                            "category": "character",
                            "score": 100.0,
                            "reason": "exact",
                        }
                    )
                self._send_json(
                    200,
                    {"query": q, "matches": matches, "request_id": "req-terms"},
                )
                return
            self._send_json(404, {"error": {"code": "invalid_request"}})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw_body.decode("utf-8"))
            path = self.path.partition("?")[0]
            with state.lock:
                version = state.version
                state.requests.append(
                    {
                        "method": "POST",
                        "path": path,
                        "body": body,
                        "auth": self.headers.get("Authorization"),
                        "version": version,
                    }
                )
                force_503 = state.force_503
                fail_once = state.fail_translations_once and path == "/v1/translations"
                if fail_once:
                    state.fail_translations_once = False

            if not self._check_auth():
                return
            if force_503 or fail_once:
                self._send_json(503, {"error": {"code": "llm_unavailable"}})
                return

            if path == "/v1/translations":
                text = str(body.get("text", ""))
                direction = str(body.get("to") or "en")
                if text == "声骸" and "protected_text" not in body:
                    self._send_json(
                        200,
                        {
                            "kind": "dictionary",
                            "text": "Echo",
                            "direction": "en",
                            "dictionary_miss": False,
                            "request_id": "req-tr-dict",
                        },
                    )
                    return
                if "protected_text" in body:
                    segs = list(body["protected_text"]["visible_segments"])
                    translated_segs = [state.translate_segment(seg) for seg in segs]
                    self._send_json(
                        200,
                        {
                            "kind": "llm",
                            "text": "".join(translated_segs),
                            "direction": direction,
                            "dictionary_miss": False,
                            "request_id": f"req-tr-html-{version}",
                            "visible_segments": translated_segs,
                        },
                    )
                    return
                has_ctx = "context" in body and "before" in (body.get("context") or {})
                ctx_tag = " [ctx]" if has_ctx else ""
                translated = (
                    f"{state.translate_segment(text)}{ctx_tag}"
                    if len(text) <= 80
                    else f"[{version}]{ctx_tag} Translated segment ({len(text)} chars) with Jinhsi."
                )
                self._send_json(
                    200,
                    {
                        "kind": "llm",
                        "text": translated,
                        "direction": direction,
                        "dictionary_miss": False,
                        "request_id": f"req-tr-plain-{version}",
                    },
                )
                return

            if path == "/v1/reviews":
                source = str(body.get("source", ""))
                target = str(body.get("target", ""))
                findings = []
                if "今汐" in source and "Jinhsi" in target:
                    src_idx = source.index("今汐")
                    tgt_idx = target.index("Jinhsi")
                    findings.append(
                        {
                            "id": "f-1",
                            "verdict": "verified_constraint",
                            "rule_id": "exact",
                            "source_span": {
                                "start": src_idx,
                                "end": src_idx + 2,
                                "text": "今汐",
                            },
                            "target_span": {
                                "start": tgt_idx,
                                "end": tgt_idx + 6,
                                "text": "Jinhsi",
                            },
                            "candidates": [],
                            "candidates_truncated": False,
                        }
                    )
                self._send_json(
                    200,
                    {
                        "request_id": "req-rev-1",
                        "source_revision": "a" * 64,
                        "target_revision": "b" * 64,
                        "rule_version": "review-v2",
                        "dictionary": {
                            "schema_version": "2",
                            "source_commit": state.source_commit,
                            "term_count": state.term_count,
                        },
                        "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": []},
                        "findings": findings,
                        "truncated": False,
                    },
                )
                return

            if path == "/v1/semantic-reviews":
                self._send_json(
                    200,
                    {
                        "review_version": "semantic-review-v1",
                        "status": "complete",
                        "issues": [],
                        "dropped_unanchored": 0,
                        "omitted_issues": 0,
                        "truncated_reason": None,
                    },
                )
                return

            if path == "/v1/structure-checks":
                segments = body.get("segments") or []
                self._send_json(
                    200,
                    {
                        "request_id": "req-sc-1",
                        "check_version": "structure-check-v1",
                        "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                        "segments": [
                            {
                                "index": idx,
                                "segment_id": None,
                                "findings": [],
                                "omitted_findings": 0,
                                "layout": {
                                    "source_paragraphs": 1,
                                    "target_paragraphs": 1,
                                    "source_lines": 1,
                                    "target_lines": 1,
                                },
                            }
                            for idx, _seg in enumerate(segments)
                        ],
                    },
                )
                return

            self._send_json(404, {"error": {"code": "invalid_request"}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeTelegramMessage:
    def __init__(
        self,
        msg_id: int,
        text: str,
        *,
        text_html: str | None = None,
        entities: tuple = (),
        reply_to_message=None,
    ) -> None:
        self.message_id = msg_id
        self.text = text
        self.caption = None
        self.text_html = text_html if text_html is not None else text
        self.caption_html = None
        self.entities = entities
        self.date = datetime.now(timezone.utc)
        self.edit_date = None
        self.reply_to_message = reply_to_message
        self.replies: list[tuple[str, str | None]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs.get("parse_mode")))
        return SimpleNamespace(message_id=self.message_id + 1000 + len(self.replies))


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.id = 999001
        self.commands_set: list = []
        self.sent_messages: list[dict[str, Any]] = []
        self.chat_actions: list[tuple[int, str]] = []

    async def set_my_commands(self, commands) -> None:
        self.commands_set.append(list(commands))

    async def send_chat_action(self, chat_id: int, action: str, **_kwargs) -> None:
        self.chat_actions.append((chat_id, action))

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": kwargs.get("parse_mode"),
                "reply_to_message_id": kwargs.get("reply_to_message_id"),
            }
        )
        return SimpleNamespace(message_id=9000 + len(self.sent_messages))


def _clear_core_and_llm_env(monkeypatch) -> None:
    for key in (
        "WUWATERM_CORE_MODE",
        "WUWATERM_CORE_API_URL",
        "WUWATERM_CORE_API_TOKEN",
        "WUWATERM_CORE_API_TIMEOUT_SECONDS",
        "WUWATERM_OPENAI_BASE_URL",
        "WUWATERM_OPENAI_API_KEY",
        "WUWATERM_OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "WUWATERM_POLL_HEALTH",
        "WUWATERM_STATE_DIR",
        "WUWATERM_SETTINGS_PATH",
        "WUWATERM_CHANNEL_REPLY_INDEX_PATH",
        "WUWATERM_CHANNEL_QUEUE_PATH",
        "WUWATERM_CHANNEL_QUEUE_PERSIST",
    ):
        monkeypatch.delenv(key, raising=False)


def test_pure_http_mode_full_pipeline_without_local_db(monkeypatch, tmp_path, caplog):
    """Pure HTTP mode boots, passes health, and serves all features with zero local terms.db."""
    _clear_core_and_llm_env(monkeypatch)
    missing_db = Path("/nonexistent/wuwaterm-missing-db/terms.db")
    assert not missing_db.exists()
    assert not missing_db.parent.exists()

    state_dir = tmp_path / "state"
    secret_token = "http-core-secret-token-never-log"

    # Guard: local DB verification & local Core constructors must NEVER be invoked.
    def _forbid(*_args, **_kwargs):
        raise AssertionError("local DB / embedded Core must not be initialized in HTTP mode")

    monkeypatch.setattr(bot, "verify_dictionary", _forbid)
    monkeypatch.setattr(bot, "build_term_service", _forbid)
    monkeypatch.setattr(bot, "build_translator", _forbid)

    with swappable_core_http_server(required_token=secret_token) as (server_state, base_url):
        monkeypatch.setenv("WUWATERM_CORE_MODE", "http")
        monkeypatch.setenv("WUWATERM_CORE_API_URL", base_url)
        monkeypatch.setenv("WUWATERM_CORE_API_TOKEN", secret_token)
        monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))
        monkeypatch.setenv("WUWATERM_CHANNEL_QUEUE_PERSIST", "1")
        monkeypatch.setenv("OWNER_USER_ID", "11")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TESTTOKEN")
        monkeypatch.setenv("NO_PROXY", "*")

        # 1. Verify run_bot startup path skips verify_dictionary and logs sanitized core_mode=http.
        polled_apps: list = []
        from telegram.ext import Application

        monkeypatch.setattr(Application, "run_polling", lambda self: polled_apps.append(self))
        with caplog.at_level(logging.INFO, logger="wuwaterm_telegram.bot"):
            run_bot(missing_db)

        assert len(polled_apps) == 1
        app = polled_apps[0]
        assert "core_mode=http" in caplog.text
        assert secret_token not in caplog.text
        assert isinstance(app.bot_data[CORE_BACKEND_KEY], HttpCoreBackend)
        assert isinstance(app.bot_data[REVIEW_BACKEND_KEY], HttpReviewBackend)
        assert SERVICE_KEY not in app.bot_data
        assert TRANSLATOR_KEY not in app.bot_data

        # State files resolve safely to WUWATERM_STATE_DIR without local terms.db existing.
        settings = app.bot_data[CHAT_SETTINGS_KEY]
        reply_index = app.bot_data[CHANNEL_REPLY_INDEX_KEY]
        channel_queue = app.bot_data[CHANNEL_QUEUE_KEY]
        assert settings.path == state_dir / "chat_settings.json"
        assert reply_index.storage_path == state_dir / "channel_replies.json"
        assert channel_queue.storage_path == state_dir / "channel_queue.json"
        settings.allow(-2001)
        assert (state_dir / "chat_settings.json").exists()

        # 2. operations.health passes via Core API /v1/meta (+ /v1/capabilities + /v1/dictionary),
        #    with zero local terms.db and zero token leak.
        health_result = operations.health()
        assert health_result["status"] == "ready"
        assert health_result["core_mode"] == "http"
        assert health_result["service_version"] == "0.16.0"
        assert health_result["dictionary_revision"] == "rev_1"
        assert CAP_PROTECTED_TRANSLATION in health_result["capabilities"]
        assert secret_token not in json.dumps(health_result)

        cli_env = {**os.environ}
        cli_proc = subprocess.run(
            [sys.executable, "-B", "-m", "wuwaterm_telegram.operations", "health"],
            env=cli_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert cli_proc.returncode == 0, cli_proc.stderr
        assert json.loads(cli_proc.stdout)["dictionary_revision"] == "rev_1"
        assert secret_token not in (cli_proc.stdout + cli_proc.stderr)

        # 3. Full Telegram command, lookup, review, rich HTML channel & queue pipeline.
        fake_bot = _FakeTelegramBot()
        monkeypatch.setattr(app, "bot", fake_bot)

        async def exercise() -> None:
            try:
                await app.post_init(app)

                # 3a. Exact dictionary lookup via /tr
                dict_msg = _FakeTelegramMessage(101, "/tr 声骸")
                dict_ctx = SimpleNamespace(args=["声骸"], bot=fake_bot, application=app)
                dict_update = SimpleNamespace(
                    effective_message=dict_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await term_command(dict_update, dict_ctx)
                assert dict_msg.replies == [("Echo", None)]

                # 3b. Plain /tr & /sentence
                tr_msg = _FakeTelegramMessage(102, "/tr 今汐释放共鸣回路")
                tr_ctx = SimpleNamespace(args=["今汐释放共鸣回路"], bot=fake_bot, application=app)
                tr_update = SimpleNamespace(
                    effective_message=tr_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await term_command(tr_update, tr_ctx)
                assert len(tr_msg.replies) == 1
                assert "Jinhsi" in tr_msg.replies[0][0]

                sent_msg = _FakeTelegramMessage(103, "/sentence 云陵谷探索")
                sent_ctx = SimpleNamespace(args=["云陵谷探索"], bot=fake_bot, application=app)
                sent_update = SimpleNamespace(
                    effective_message=sent_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await sentence_command(sent_update, sent_ctx)
                assert len(sent_msg.replies) == 1
                assert "Gorges of Spirits" in sent_msg.replies[0][0]

                # 3c. /review (both dictionary + semantic via HttpReviewBackend)
                rev_msg = _FakeTelegramMessage(104, "/review 今汐登场\n---\nJinhsi appears")
                rev_ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                rev_update = SimpleNamespace(
                    effective_message=rev_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await review_command(rev_update, rev_ctx)
                assert len(rev_msg.replies) >= 1
                assert any("审校" in text or "词典" in text or "Jinhsi" in text for text, _ in rev_msg.replies)

                # 3d. /about and /status without local TermService
                about_msg = _FakeTelegramMessage(105, "/about")
                about_ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                about_update = SimpleNamespace(
                    effective_message=about_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await about_command(about_update, about_ctx)
                assert "Dictionary terms: 100" in about_msg.replies[0][0]

                status_msg = _FakeTelegramMessage(106, "/status")
                status_ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                status_update = SimpleNamespace(
                    effective_message=status_msg,
                    effective_chat=SimpleNamespace(id=11, type="private"),
                    effective_user=SimpleNamespace(id=11),
                )
                await status_command(status_update, status_ctx)
                status_reply = status_msg.replies[0][0]
                assert "Core backend: http" in status_reply
                assert "Core dictionary revision: rev_1" in status_reply

                # 3e. Linked-channel rich HTML translation
                ch_msg = _FakeTelegramMessage(
                    201,
                    "今汐在云陵谷触发了共鸣回路。",
                    text_html="<b>今汐</b>在<u>云陵谷</u>触发了<i>共鸣回路</i>。",
                )
                ch_ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                ch_update = SimpleNamespace(
                    update_id=201,
                    effective_message=ch_msg,
                    effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
                    effective_user=None,
                )
                await channel_post_handler(ch_update, ch_ctx)
                assert ch_msg.replies == [
                    ("<b>Jinhsi</b> in <u>Gorges of Spirits</u> triggered <i>Resonance Circuit</i>.", "HTML")
                ]

                # 3f. Long channel post (>2000 chars) forwards cross-segment context.before
                long_para = "今汐在云陵谷释放共鸣回路并强化队伍伤害。" * 65
                long_text = f"{long_para}\n\n{long_para}"
                assert len(long_text) > 2000
                long_msg = _FakeTelegramMessage(202, long_text)
                long_update = SimpleNamespace(
                    update_id=202,
                    effective_message=long_msg,
                    effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
                    effective_user=None,
                )
                await channel_post_handler(long_update, ch_ctx)
                assert len(long_msg.replies) >= 1
                assert any("[ctx]" in reply for reply, _ in long_msg.replies)

                # 3g. Transient Core 503 enqueues into ChannelTaskQueue and recovers via replay
                server_state.fail_translations_once = True
                queued_msg = _FakeTelegramMessage(
                    203,
                    "今汐使用共鸣回路。",
                    text_html="<b>今汐</b>使用<i>共鸣回路</i>。",
                )
                queued_update = SimpleNamespace(
                    update_id=203,
                    effective_message=queued_msg,
                    effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
                    effective_user=None,
                )
                await channel_post_handler(queued_update, ch_ctx)
                assert queued_msg.replies == []
                assert channel_queue.stats().pending == 1

                # Replay the queued task now that Core has recovered.
                queued_tasks = list(channel_queue._tasks.values())
                assert len(queued_tasks) == 1
                replay_outcome = await execute_queued_channel_task(app, queued_tasks[0])
                assert replay_outcome.status == "completed"
                assert any(
                    m["parse_mode"] == "HTML" and "<b>Jinhsi</b>" in m["text"]
                    for m in fake_bot.sent_messages
                )
            finally:
                await app.post_shutdown(app)

        asyncio.run(exercise())

        # Confirm zero local terms.db ever existed and all HTTP endpoints were exercised.
        assert not missing_db.exists()
        paths_hit = {req["path"] for req in server_state.requests}
        assert {
            "/v1/meta",
            "/v1/capabilities",
            "/v1/dictionary",
            "/v1/terms",
            "/v1/translations",
            "/v1/reviews",
            "/v1/semantic-reviews",
        } <= paths_hit


def test_in_place_core_hot_swap_v1_to_v2_without_restarting_telegram(
    monkeypatch, tmp_path
):
    """Hot-swap Core v1 -> Core v2 behind the same URL without restarting Telegram.

    Telegram keeps the exact same Application & HttpCoreBackend instance,
    automatically discovers ``dictionary_revision=rev_2`` and the new
    ``knowledge-retrieval-v1`` capability after TTL expiry, and emits the
    upgraded RAG-enhanced rich HTML translation with zero local ``terms.db``.
    """
    _clear_core_and_llm_env(monkeypatch)
    missing_db = Path("/nonexistent/hot-swap-no-db/terms.db")
    assert not missing_db.exists()

    state_dir = tmp_path / "hot-swap-state"

    def _forbid(*_args, **_kwargs):
        raise AssertionError("local DB / embedded Core must not be initialized in HTTP mode")

    monkeypatch.setattr(bot, "verify_dictionary", _forbid)
    monkeypatch.setattr(bot, "build_term_service", _forbid)
    monkeypatch.setattr(bot, "build_translator", _forbid)

    with swappable_core_http_server(required_token="swap-token") as (server_state, base_url):
        monkeypatch.setenv("WUWATERM_CORE_MODE", "http")
        monkeypatch.setenv("WUWATERM_CORE_API_URL", base_url)
        monkeypatch.setenv("WUWATERM_CORE_API_TOKEN", "swap-token")
        monkeypatch.setenv("WUWATERM_STATE_DIR", str(state_dir))
        monkeypatch.setenv("OWNER_USER_ID", "11")
        monkeypatch.setenv("NO_PROXY", "*")

        app = create_application("123456:TESTTOKEN", missing_db)
        app_id_before = id(app)
        backend = app.bot_data[CORE_BACKEND_KEY]
        backend_id_before = id(backend)
        assert isinstance(backend, HttpCoreBackend)
        assert SERVICE_KEY not in app.bot_data
        assert TRANSLATOR_KEY not in app.bot_data

        # Deterministic virtual clock for capability TTL expiry (no real sleep).
        virtual_now = [1000.0]
        backend._clock = lambda: virtual_now[0]

        settings = app.bot_data[CHAT_SETTINGS_KEY]
        settings.allow(-3001)
        fake_bot = _FakeTelegramBot()
        monkeypatch.setattr(app, "bot", fake_bot)

        async def exercise() -> None:
            try:
                # --- Phase 1: Core v1 (rev_1, base capabilities) ---
                server_state.activate_v1()
                await app.post_init(app)
                meta_v1 = backend.cached_meta
                assert meta_v1 is not None
                assert meta_v1.service_version == "0.16.0"
                assert meta_v1.dictionary_revision == "rev_1"
                assert CAP_KNOWLEDGE_RETRIEVAL not in meta_v1.capabilities

                health_v1 = operations.health()
                assert health_v1["status"] == "ready"
                assert health_v1["dictionary_revision"] == "rev_1"
                assert CAP_KNOWLEDGE_RETRIEVAL not in health_v1["capabilities"]

                msg_v1 = _FakeTelegramMessage(
                    3001,
                    "今汐触发了共鸣回路。",
                    text_html="<b>今汐</b>触发了<i>共鸣回路</i>。",
                )
                ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                await channel_post_handler(
                    SimpleNamespace(
                        update_id=3001,
                        effective_message=msg_v1,
                        effective_chat=SimpleNamespace(id=-3001, type="supergroup"),
                        effective_user=None,
                    ),
                    ctx,
                )
                assert msg_v1.replies == [
                    ("<b>Jinhsi</b> triggered <i>Resonance Circuit</i>.", "HTML")
                ]

                # --- Phase 2: In-place hot-swap to Core v2 (rev_2 + knowledge-retrieval-v1) ---
                server_state.activate_v2()
                # Advance past 60s capability cache TTL; Telegram instance is untouched.
                virtual_now[0] += 61.0

                msg_v2 = _FakeTelegramMessage(
                    3002,
                    "今汐触发了共鸣回路。",
                    text_html="<b>今汐</b>触发了<i>共鸣回路</i>。",
                )
                await channel_post_handler(
                    SimpleNamespace(
                        update_id=3002,
                        effective_message=msg_v2,
                        effective_chat=SimpleNamespace(id=-3001, type="supergroup"),
                        effective_user=None,
                    ),
                    ctx,
                )

                # Same Telegram Application & HttpCoreBackend instance, zero restart!
                assert id(app) == app_id_before
                assert id(app.bot_data[CORE_BACKEND_KEY]) == backend_id_before

                # Telegram automatically discovered Core v2 revision & RAG capability.
                meta_v2 = backend.cached_meta
                assert meta_v2 is not None
                assert meta_v2.service_version == "0.17.0"
                assert meta_v2.dictionary_revision == "rev_2"
                assert CAP_KNOWLEDGE_RETRIEVAL in meta_v2.capabilities

                # Output reflects Core v2 RAG-enhanced segments while preserving rich HTML.
                assert msg_v2.replies == [
                    (
                        "<b>Jinhsi [RAG: Magistrate of Jinzhou]</b> triggered "
                        "<i>Resonance Circuit (Incandescence)</i>.",
                        "HTML",
                    )
                ]

                # /status on the same instance reports Core v2 revision and capability.
                status_msg = _FakeTelegramMessage(3003, "/status")
                await status_command(
                    SimpleNamespace(
                        effective_message=status_msg,
                        effective_chat=SimpleNamespace(id=11, type="private"),
                        effective_user=SimpleNamespace(id=11),
                    ),
                    ctx,
                )
                status_reply = status_msg.replies[0][0]
                assert "Core service version: 0.17.0" in status_reply
                assert "Core dictionary revision: rev_2" in status_reply
                assert CAP_KNOWLEDGE_RETRIEVAL in status_reply

                # operations.health also sees Core v2 immediately.
                health_v2 = operations.health()
                assert health_v2["status"] == "ready"
                assert health_v2["service_version"] == "0.17.0"
                assert health_v2["dictionary_revision"] == "rev_2"
                assert CAP_KNOWLEDGE_RETRIEVAL in health_v2["capabilities"]
            finally:
                await app.post_shutdown(app)

        asyncio.run(exercise())
        assert not missing_db.exists()


def test_core_mode_fail_closed_and_local_compatibility(monkeypatch, sample_db):
    """WUWATERM_CORE_MODE enforces fail-closed semantics while preserving local fallback."""
    _clear_core_and_llm_env(monkeypatch)

    # 1. WUWATERM_CORE_MODE=http without WUWATERM_CORE_API_URL fails closed.
    monkeypatch.setenv("WUWATERM_CORE_MODE", "http")
    with pytest.raises(BotConfigError, match="WUWATERM_CORE_API_URL"):
        BotConfig.from_env()
    with pytest.raises(BotConfigError, match="WUWATERM_CORE_API_URL"):
        BotConfig(core_mode="http", core_api_url=None)
    with pytest.raises(operations.OperationsError, match="WUWATERM_CORE_API_URL"):
        operations.health()
    with pytest.raises(SystemExit) as cli_exc:
        cli.main(["--db", "/nonexistent/terms.db"])
    assert cli_exc.value.code == 2

    # 2. WUWATERM_CORE_MODE=http with invalid URL fails closed.
    monkeypatch.setenv("WUWATERM_CORE_API_URL", "ftp://invalid-scheme")
    with pytest.raises(BotConfigError, match="absolute HTTP\\(S\\) URL"):
        BotConfig.from_env()
    with pytest.raises(operations.OperationsError, match="absolute HTTP\\(S\\) URL"):
        operations.health()

    # 3. Unknown WUWATERM_CORE_MODE fails closed.
    monkeypatch.setenv("WUWATERM_CORE_MODE", "hybrid")
    monkeypatch.delenv("WUWATERM_CORE_API_URL", raising=False)
    with pytest.raises(BotConfigError, match="auto, http, local"):
        BotConfig.from_env()
    with pytest.raises(operations.OperationsError, match="auto, http, local"):
        operations.health()

    # 4. WUWATERM_CORE_MODE=local conflicts with WUWATERM_CORE_API_URL (fail-closed).
    monkeypatch.setenv("WUWATERM_CORE_MODE", "local")
    monkeypatch.setenv("WUWATERM_CORE_API_URL", "http://127.0.0.1:8000")
    with pytest.raises(BotConfigError, match="conflicts with WUWATERM_CORE_API_URL"):
        BotConfig.from_env()
    with pytest.raises(operations.OperationsError, match="conflicts with WUWATERM_CORE_API_URL"):
        operations.health()

    # 5. WUWATERM_CORE_MODE=local without URL preserves exact legacy LocalCoreBackend behavior.
    monkeypatch.delenv("WUWATERM_CORE_API_URL", raising=False)
    local_cfg = BotConfig.from_env()
    assert local_cfg.core_mode == "local"
    assert local_cfg.resolved_core_mode == "local"
    local_app = create_application("123456:TESTTOKEN", sample_db, config=local_cfg)
    assert isinstance(local_app.bot_data[CORE_BACKEND_KEY], LocalCoreBackend)
    assert local_app.bot_data[SERVICE_KEY].term_text("声骸") == "Echo"
    assert TRANSLATOR_KEY in local_app.bot_data
    asyncio.run(local_app.post_shutdown(local_app))

    # 6. WUWATERM_CORE_MODE=auto (default): URL -> http, no URL -> local.
    monkeypatch.delenv("WUWATERM_CORE_MODE", raising=False)
    auto_local = BotConfig.from_env()
    assert auto_local.core_mode == "auto"
    assert auto_local.resolved_core_mode == "local"
    monkeypatch.setenv("WUWATERM_CORE_API_URL", "http://127.0.0.1:8000")
    auto_http = BotConfig.from_env()
    assert auto_http.core_mode == "auto"
    assert auto_http.resolved_core_mode == "http"


def test_http_mode_state_paths_fallback_to_db_parent_without_db_file(monkeypatch, tmp_path):
    """When WUWATERM_STATE_DIR is unset in HTTP mode, state files fall back to db_path.parent."""
    _clear_core_and_llm_env(monkeypatch)
    monkeypatch.setenv("WUWATERM_CORE_MODE", "http")
    monkeypatch.setenv("WUWATERM_CORE_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WUWATERM_CHANNEL_QUEUE_PERSIST", "1")

    db_parent = tmp_path / "no-db-dir"
    missing_db = db_parent / "terms.db"
    assert not missing_db.exists()

    app = create_application("123456:TESTTOKEN", missing_db)
    try:
        assert app.bot_data[CHAT_SETTINGS_KEY].path == db_parent / "chat_settings.json"
        assert app.bot_data[CHANNEL_REPLY_INDEX_KEY].storage_path == db_parent / "channel_replies.json"
        assert app.bot_data[CHANNEL_QUEUE_KEY].storage_path == db_parent / "channel_queue.json"
        app.bot_data[CHAT_SETTINGS_KEY].allow(-4001)
        assert (db_parent / "chat_settings.json").exists()
        assert not missing_db.exists()
    finally:
        asyncio.run(app.post_shutdown(app))


def test_http_mode_about_and_status_degrade_cleanly_when_core_unreachable(
    monkeypatch, tmp_path
):
    """/about and /status must never crash with KeyError when Core HTTP is temporarily 503."""
    _clear_core_and_llm_env(monkeypatch)
    with swappable_core_http_server() as (server_state, base_url):
        server_state.force_503 = True
        monkeypatch.setenv("WUWATERM_CORE_MODE", "http")
        monkeypatch.setenv("WUWATERM_CORE_API_URL", base_url)
        monkeypatch.setenv("WUWATERM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("OWNER_USER_ID", "11")
        monkeypatch.setenv("NO_PROXY", "*")

        app = create_application("123456:TESTTOKEN", Path("/nonexistent/terms.db"))
        fake_bot = _FakeTelegramBot()
        monkeypatch.setattr(app, "bot", fake_bot)

        with pytest.raises(operations.OperationsError, match="core http health probe failed"):
            operations.health()

        async def exercise() -> None:
            try:
                about_msg = _FakeTelegramMessage(401, "/about")
                status_msg = _FakeTelegramMessage(402, "/status")
                ctx = SimpleNamespace(args=[], bot=fake_bot, application=app)
                await about_command(
                    SimpleNamespace(
                        effective_message=about_msg,
                        effective_chat=SimpleNamespace(id=11, type="private"),
                        effective_user=SimpleNamespace(id=11),
                    ),
                    ctx,
                )
                await status_command(
                    SimpleNamespace(
                        effective_message=status_msg,
                        effective_chat=SimpleNamespace(id=11, type="private"),
                        effective_user=SimpleNamespace(id=11),
                    ),
                    ctx,
                )
                assert "unavailable" in about_msg.replies[0][0]
                assert "Core backend: http" in status_msg.replies[0][0]
            finally:
                await app.post_shutdown(app)

        asyncio.run(exercise())


def test_deploy_files_demonstrate_pure_http_mode_and_local_fallback():
    root = Path(__file__).resolve().parents[1]
    compose_text = (root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    env_text = (root / "deploy" / "env.example").read_text(encoding="utf-8")

    # Compose must not actively mount data/terms.db by default.
    active_compose_lines = [
        line.strip()
        for line in compose_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "- ../state:/app/state" in active_compose_lines
    assert "- ../data:/app/data:ro" not in active_compose_lines
    assert "WUWATERM_CORE_MODE=http" in compose_text

    # env.example documents pure HTTP mode defaults and local compatibility fallback.
    assert "WUWATERM_CORE_MODE=http" in env_text
    assert "WUWATERM_CORE_API_URL=" in env_text
    assert "WUWATERM_CORE_API_TOKEN=" in env_text
    assert "local" in env_text
