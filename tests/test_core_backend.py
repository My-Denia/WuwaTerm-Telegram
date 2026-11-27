"""Zero-network unit tests for CoreBackend / HttpCoreBackend capability negotiation."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wuwaterm.application import (
    ContextBefore,
    SentenceTranslator,
    TermService,
    TranslationContext,
)
from wuwaterm_telegram.core_backend import (
    CAP_DICTIONARY_FALLBACK,
    CAP_DICTIONARY_IDENTITY,
    CAP_HYBRID_RAG,
    CAP_KNOWLEDGE_RETRIEVAL,
    CAP_PROTECTED_TRANSLATION,
    CAP_SEMANTIC_REVIEW,
    CAP_STRUCTURE_CHECK,
    CAP_TRANSLATION_CONTEXT,
    CAPABILITY_TOKEN_RE,
    CoreBackendError,
    HttpCoreBackend,
    HttpReviewBackend,
    LocalCoreBackend,
)
from wuwaterm_telegram.review_backend import ReviewBackendError


def _meta_payload() -> dict:
    """Exact published 11-key /v1/meta payload (Web 0.7.0 exactKeys contract)."""
    return {
        "service_version": "0.16.0",
        "api_version": "v1",
        "schema_version": "2",
        "source_profile": "official",
        "source_commit": "8565b48b628addb6a01569cee00503f1547077c8",
        "source_game_version": "3.7",
        "source_resource_version": None,
        "source_changelist": None,
        "term_count": 42,
        "llm_configured": True,
        "request_id": "req-meta-1",
    }


def test_capability_tokens_follow_kebab_case_contract():
    for token in (
        CAP_PROTECTED_TRANSLATION,
        CAP_TRANSLATION_CONTEXT,
        CAP_DICTIONARY_FALLBACK,
        CAP_KNOWLEDGE_RETRIEVAL,
        CAP_HYBRID_RAG,
        CAP_STRUCTURE_CHECK,
        CAP_SEMANTIC_REVIEW,
        CAP_DICTIONARY_IDENTITY,
    ):
        assert CAPABILITY_TOKEN_RE.fullmatch(token)
        assert "_" not in token


def test_http_backend_negotiates_capabilities_strictly_via_capabilities_endpoint():
    seen_paths: list[str] = []
    seen_auth: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        seen_auth.append(request.headers.get("Authorization"))
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "capabilities": [
                        CAP_TRANSLATION_CONTEXT,
                        CAP_STRUCTURE_CHECK,
                        CAP_DICTIONARY_IDENTITY,
                        CAP_PROTECTED_TRANSLATION,
                        "bad_underscore_token",  # must be ignored
                    ],
                    "request_id": "req-cap-1",
                },
            )
        if request.url.path == "/v1/dictionary":
            return httpx.Response(
                200,
                json={
                    "revision": "a" * 64,
                    "schema_version": "2",
                    "source_commit": "8565b48b628addb6a01569cee00503f1547077c8",
                    "source_game_version": "3.7",
                    "term_count": 42,
                    "request_id": "req-dict-1",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            api_token="secret-device-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            meta = await backend.probe_capabilities()
            assert meta.backend_kind == "http"
            assert meta.service_version == "0.16.0"
            assert meta.dictionary_revision == "a" * 64
            assert CAP_TRANSLATION_CONTEXT in meta.capabilities
            assert CAP_PROTECTED_TRANSLATION in meta.capabilities
            assert "bad_underscore_token" not in meta.capabilities
            # Cached on second call within TTL.
            await backend.probe_capabilities()
        finally:
            await backend.aclose()

    asyncio.run(exercise())
    assert seen_paths == ["/v1/meta", "/v1/capabilities", "/v1/dictionary"]
    assert all(val == "Bearer secret-device-token" for val in seen_auth)


def test_http_backend_degrades_gracefully_when_capabilities_endpoint_is_404():
    sent_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(404, json={"error": {"code": "invalid_request"}})
        if request.url.path == "/v1/translations":
            body = json.loads(request.content)
            sent_bodies.append(body)
            return httpx.Response(
                200,
                json={
                    "kind": "llm",
                    "text": "Jinhsi arrived.",
                    "direction": "en",
                    "dictionary_miss": False,
                    "request_id": "req-tr-1",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            meta = await backend.probe_capabilities()
            assert meta.capabilities == frozenset()

            # Context is omitted when translation-context-v1 is not advertised.
            outcome = await backend.translate_text(
                "今汐来了。",
                to_chinese=False,
                context=TranslationContext(
                    before=ContextBefore(source="前文", target="Prev")
                ),
            )
            assert outcome.text == "Jinhsi arrived."
            assert "context" not in sent_bodies[0]

            # HTML degrades cleanly to plain when protected-translation-v1 is absent.
            html_out, markup_used = await backend.translate_protected_html(
                "<b>今汐</b>来了。", to_chinese=False
            )
            assert html_out == "Jinhsi arrived."
            assert markup_used is False
            assert "protected_text" not in sent_bodies[1]
            assert sent_bodies[1]["text"] == "今汐来了。"

            # Structure checks fall back locally when structure-check-v1 is absent.
            checks = await backend.check_structure([("ATK +20%", "攻击力 +10%")])
            assert len(checks) == 1
            assert "number_missing" in checks[0].rule_ids
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_backend_uses_opt_in_protected_translation_and_context_when_negotiated():
    sent_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "capabilities": [
                        CAP_TRANSLATION_CONTEXT,
                        CAP_PROTECTED_TRANSLATION,
                        CAP_DICTIONARY_FALLBACK,
                        CAP_STRUCTURE_CHECK,
                        CAP_SEMANTIC_REVIEW,
                    ],
                    "request_id": "req-cap-2",
                },
            )
        if request.url.path == "/v1/translations":
            body = json.loads(request.content)
            sent_bodies.append(body)
            if "protected_text" in body:
                return httpx.Response(
                    200,
                    json={
                        "kind": "llm",
                        "text": "Jinhsi arrived.",
                        "direction": "en",
                        "dictionary_miss": False,
                        "request_id": "req-tr-html",
                        "visible_segments": ["", "Jinhsi", " arrived."],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "kind": "llm",
                    "text": "Translated plain.",
                    "direction": "en",
                    "dictionary_miss": False,
                    "request_id": "req-tr-plain",
                },
            )
        if request.url.path == "/v1/structure-checks":
            return httpx.Response(
                200,
                json={
                    "request_id": "req-sc-1",
                    "check_version": "structure-check-v1",
                    "checks": ["empty_target", "numbers", "placeholders", "paragraphs"],
                    "segments": [
                        {
                            "index": 0,
                            "segment_id": None,
                            "findings": [{"rule_id": "number_missing"}],
                            "omitted_findings": 0,
                            "layout": {
                                "source_paragraphs": 1,
                                "target_paragraphs": 1,
                                "source_lines": 1,
                                "target_lines": 1,
                            },
                        }
                    ],
                },
            )
        if request.url.path == "/v1/terms":
            return httpx.Response(
                200,
                json={
                    "query": "今汐",
                    "matches": [
                        {
                            "zh": "今汐",
                            "en": "Jinhsi",
                            "category": "character",
                            "score": 100.0,
                            "reason": "exact",
                        }
                    ],
                    "request_id": "req-terms-1",
                },
            )
        if request.url.path == "/v1/reviews":
            return httpx.Response(
                200,
                json={
                    "request_id": "req-rev-1",
                    "source_revision": "a" * 64,
                    "target_revision": "b" * 64,
                    "rule_version": "review-v2",
                    "dictionary": {"schema_version": "2", "source_commit": "x", "term_count": 1},
                    "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": []},
                    "findings": [
                        {
                            "id": "f1",
                            "verdict": "verified_constraint",
                            "rule_id": "exact",
                            "source_span": {"start": 0, "end": 2, "text": "今汐"},
                            "target_span": {"start": 0, "end": 6, "text": "Jinhsi"},
                            "candidates": [],
                            "candidates_truncated": False,
                        }
                    ],
                    "truncated": False,
                },
            )
        if request.url.path == "/v1/semantic-reviews":
            return httpx.Response(
                200,
                json={"status": "complete", "issues": []},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            ctx = TranslationContext(
                before=ContextBefore(source="第一段", target="Para 1")
            )
            html_out, markup_used = await backend.translate_protected_html(
                "<b>今汐</b>来了。", to_chinese=False, context=ctx
            )
            assert markup_used is True
            assert html_out == "<b>Jinhsi</b> arrived."
            assert "protected_text" in sent_bodies[0]
            assert sent_bodies[0]["context"]["before"] == {
                "source": "第一段",
                "target": "Para 1",
            }

            plain_out = await backend.translate_text(
                "今汐来了。",
                to_chinese=False,
                context=ctx,
                allow_dictionary_fallback=True,
            )
            assert plain_out.text == "Translated plain."
            assert sent_bodies[1]["dictionary_fallback"] is True

            assert await backend.lookup_exact("今汐", to_chinese=False) == "Jinhsi"

            checks = await backend.check_structure([("ATK +20%", "ATK")])
            assert checks[0].rule_ids == ("number_missing",)

            review_adapter = HttpReviewBackend(backend)
            assert review_adapter.semantic_available() is True
            term_rev = await review_adapter.term_review("今汐", "Jinhsi", "en")
            assert term_rev.verified == 1
            sem = await review_adapter.semantic_review(
                "今汐", "Jinhsi", direction="en", explain_language="zh", context_before=None
            )
            assert sem["status"] == "complete"
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_backend_errors_never_leak_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        raise AssertionError(f"unexpected: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            api_token="super-secret-token-xyz",
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(CoreBackendError) as exc:
                await backend.probe_capabilities()
            assert exc.value.code == "unauthorized"
            assert "super-secret-token-xyz" not in str(exc.value)
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_review_backend_refuses_semantic_when_capability_absent():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json={"api_version": "v1", "capabilities": [], "request_id": "r1"})
        raise AssertionError(f"unexpected: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            await backend.probe_capabilities()
            review_adapter = HttpReviewBackend(backend)
            assert review_adapter.semantic_available() is False
            with pytest.raises(ReviewBackendError) as exc:
                await review_adapter.semantic_review(
                    "a", "b", direction="en", explain_language="zh", context_before=None
                )
            assert exc.value.code == "llm_unavailable"
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_local_core_backend_smoke(sample_db):
    async def exercise() -> None:
        service = TermService(sample_db)
        translator = SentenceTranslator(sample_db)
        backend = LocalCoreBackend(service, translator)
        try:
            meta = await backend.probe_capabilities()
            assert meta.backend_kind == "local"
            assert CAP_TRANSLATION_CONTEXT in meta.capabilities
            assert CAP_STRUCTURE_CHECK in meta.capabilities
            assert await backend.lookup_exact("今汐", to_chinese=False) == "Jinhsi"
            checks = await backend.check_structure([("攻击力提升 20%", "ATK +20%")])
            assert checks[0].rule_ids == ()
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_backend_non_json_capabilities_200_degrades_safely():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                text="<html>proxy gateway ok</html>",
                headers={"content-type": "text/html"},
            )
        raise AssertionError(f"unexpected: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            meta = await backend.probe_capabilities()
            assert meta.service_version == "0.16.0"
            assert meta.capabilities == frozenset()
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_backend_wires_channel_commands_and_review_without_local_llm(
    monkeypatch, sample_db
):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from wuwaterm_telegram.bot import (
        ADMIN_CACHE_KEY,
        CHANNEL_REPLY_INDEX_KEY,
        CHAT_SETTINGS_KEY,
        CONFIG_KEY,
        CORE_BACKEND_KEY,
        RATE_LIMITER_KEY,
        REJECT_LIMITER_KEY,
        SERVICE_KEY,
        TRANSLATOR_KEY,
        AdminStatusCache,
        BotConfig,
        ChannelReplyIndex,
        PerChatRateLimiter,
        create_application,
        sentence_command,
        term_command,
    )
    from wuwaterm_telegram.channel import channel_post_handler
    from wuwaterm_telegram.review_bot import REVIEW_BACKEND_KEY
    from wuwaterm_telegram.settings import ChatSettings

    for env_var in (
        "WUWATERM_OPENAI_BASE_URL",
        "WUWATERM_OPENAI_API_KEY",
        "WUWATERM_OPENAI_MODEL",
    ):
        monkeypatch.delenv(env_var, raising=False)

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "capabilities": [
                        CAP_PROTECTED_TRANSLATION,
                        CAP_TRANSLATION_CONTEXT,
                    ],
                    "request_id": "req-cap-wire",
                },
            )
        if request.url.path == "/v1/terms":
            return httpx.Response(
                200,
                json={"query": "x", "matches": [], "request_id": "req-terms-wire"},
            )
        if request.url.path == "/v1/translations":
            body = json.loads(request.content)
            if "protected_text" in body:
                return httpx.Response(
                    200,
                    json={
                        "kind": "llm",
                        "text": "Remote HTTP HTML translation.",
                        "direction": "en",
                        "dictionary_miss": False,
                        "request_id": "req-tr-html",
                        "visible_segments": ["", "Remote HTTP HTML", " translation."],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "kind": "llm",
                    "text": "Remote HTTP plain translation.",
                    "direction": "en",
                    "dictionary_miss": False,
                    "request_id": "req-tr-plain",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    class _FakeMsg:
        def __init__(self, msg_id: int, text: str, text_html: str | None = None):
            self.message_id = msg_id
            self.text = text
            self.caption = None
            self.text_html = text_html if text_html is not None else text
            self.caption_html = None
            self.entities = ()
            self.date = datetime.now(timezone.utc)
            self.edit_date = None
            self.reply_to_message = None
            self.replies: list[tuple[str, str | None]] = []

        async def reply_text(self, text: str, **kwargs):
            self.replies.append((text, kwargs.get("parse_mode")))
            return SimpleNamespace(message_id=self.message_id + 100)

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            config = BotConfig(owner_user_id=11, channel_llm_calls_per_minute=20)
            settings = ChatSettings(sample_db.parent / "http_chat_settings.json")
            settings.allow(-2001)
            bot_data = {
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: SentenceTranslator(sample_db),
                CORE_BACKEND_KEY: backend,
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=10),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=10),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(ttl_seconds=3600.0),
                CHAT_SETTINGS_KEY: settings,
            }
            ctx = SimpleNamespace(
                args=[],
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data=bot_data),
            )

            # 1. Channel post translates via HTTP Core even with local LLM unset.
            ch_msg = _FakeMsg(8001, "今汐技能说明", "<b>今汐</b>技能说明")
            ch_update = SimpleNamespace(
                update_id=8001,
                effective_message=ch_msg,
                effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
                effective_user=None,
            )
            await channel_post_handler(ch_update, ctx)
            assert ch_msg.replies == [("<b>Remote HTTP HTML</b> translation.", "HTML")]

            # 2. /tr translates via HTTP Core even with local LLM unset.
            tr_msg = _FakeMsg(8002, "/tr 今汐释放共鸣解放")
            tr_ctx = SimpleNamespace(
                args=["今汐释放共鸣解放"],
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data=bot_data),
            )
            tr_update = SimpleNamespace(
                effective_message=tr_msg,
                effective_chat=SimpleNamespace(id=11, type="private"),
                effective_user=SimpleNamespace(id=11),
            )
            await term_command(tr_update, tr_ctx)
            assert tr_msg.replies == [("Remote HTTP plain translation.", None)]

            # 3. /sentence translates via HTTP Core even with local LLM unset.
            sent_msg = _FakeMsg(8003, "/sentence 声骸系统已更新")
            sent_ctx = SimpleNamespace(
                args=["声骸系统已更新"],
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data=bot_data),
            )
            sent_update = SimpleNamespace(
                effective_message=sent_msg,
                effective_chat=SimpleNamespace(id=11, type="private"),
                effective_user=SimpleNamespace(id=11),
            )
            await sentence_command(sent_update, sent_ctx)
            assert sent_msg.replies == [("Remote HTTP plain translation.", None)]
        finally:
            await backend.aclose()

    asyncio.run(exercise())
    assert "/v1/translations" in seen_paths

    # 4. create_application wires HttpCoreBackend + HttpReviewBackend when core_api_url is set.
    app = create_application(
        "123456:TESTTOKEN",
        sample_db,
        config=BotConfig(
            owner_user_id=11,
            core_api_url="http://127.0.0.1:8000",
        ),
    )
    assert isinstance(app.bot_data[CORE_BACKEND_KEY], HttpCoreBackend)
    assert isinstance(app.bot_data[REVIEW_BACKEND_KEY], HttpReviewBackend)
    asyncio.run(app.bot_data[CORE_BACKEND_KEY].aclose())


def test_http_channel_post_over_200_chars_skips_terms_and_translates(
    monkeypatch, sample_db
):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from wuwaterm_telegram.bot import (
        ADMIN_CACHE_KEY,
        CHANNEL_REPLY_INDEX_KEY,
        CHAT_SETTINGS_KEY,
        CONFIG_KEY,
        CORE_BACKEND_KEY,
        RATE_LIMITER_KEY,
        REJECT_LIMITER_KEY,
        SERVICE_KEY,
        TRANSLATOR_KEY,
        AdminStatusCache,
        BotConfig,
        ChannelReplyIndex,
        PerChatRateLimiter,
    )
    from wuwaterm_telegram.channel import channel_post_handler
    from wuwaterm_telegram.settings import ChatSettings

    for env_var in (
        "WUWATERM_OPENAI_BASE_URL",
        "WUWATERM_OPENAI_API_KEY",
        "WUWATERM_OPENAI_MODEL",
    ):
        monkeypatch.delenv(env_var, raising=False)

    terms_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={"api_version": "v1", "capabilities": [], "request_id": "cap-200"},
            )
        if request.url.path == "/v1/terms":
            q = request.url.params.get("q", "")
            terms_calls.append(q)
            if len(q) > 200:
                return httpx.Response(
                    400,
                    json={"error": {"code": "invalid_request", "message": "too long"}},
                )
            return httpx.Response(
                400, json={"error": {"code": "invalid_request", "message": "bad"}}
            )
        if request.url.path == "/v1/translations":
            return httpx.Response(
                200,
                json={
                    "kind": "llm",
                    "text": "Translated 224-char channel post.",
                    "direction": "en",
                    "dictionary_miss": False,
                    "request_id": "req-tr-224",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    class _FakeMsg:
        def __init__(self, msg_id: int, text: str):
            self.message_id = msg_id
            self.text = text
            self.caption = None
            self.text_html = text
            self.caption_html = None
            self.date = datetime.now(timezone.utc)
            self.edit_date = None
            self.replies: list[tuple[str, str | None]] = []

        async def reply_text(self, text: str, **kwargs):
            self.replies.append((text, kwargs.get("parse_mode")))
            return SimpleNamespace(message_id=self.message_id + 100)

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            config = BotConfig(owner_user_id=11, channel_llm_calls_per_minute=20)
            settings = ChatSettings(sample_db.parent / "http_224_settings.json")
            settings.allow(-2001)
            bot_data = {
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: SentenceTranslator(sample_db),
                CORE_BACKEND_KEY: backend,
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=10),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=10),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(ttl_seconds=3600.0),
                CHAT_SETTINGS_KEY: settings,
            }
            ctx = SimpleNamespace(
                args=[],
                bot=SimpleNamespace(),
                application=SimpleNamespace(bot_data=bot_data),
            )
            post_224 = "今汐技能说明" * 37 + "测试"
            assert len(post_224) == 224
            msg = _FakeMsg(8100, post_224)
            update = SimpleNamespace(
                update_id=8100,
                effective_message=msg,
                effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
                effective_user=None,
            )
            await channel_post_handler(update, ctx)
            assert terms_calls == []
            assert msg.replies == [("Translated 224-char channel post.", None)]

            # Even for <=200 chars, invalid_request from /v1/terms degrades to cache miss.
            assert await backend.lookup_exact("今汐测试", to_chinese=False) is None
            assert terms_calls == ["今汐测试"]
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_long_tr_uses_full_text_direction_for_all_segments(monkeypatch, sample_db):
    from wuwaterm_telegram.bot import TranslationRequest, _http_translation_reply

    sent_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "capabilities": [CAP_TRANSLATION_CONTEXT],
                    "request_id": "cap-dir",
                },
            )
        if request.url.path == "/v1/translations":
            body = json.loads(request.content)
            sent_bodies.append(body)
            direction = body.get("to") or "zh"
            return httpx.Response(
                200,
                json={
                    "kind": "llm",
                    "text": f"seg-{len(sent_bodies)}-{direction}",
                    "direction": direction,
                    "dictionary_miss": False,
                    "request_id": f"req-{len(sent_bodies)}",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    # First paragraph is purely English, followed by predominantly Chinese content.
    # Full-text rule: CJK is dominant (latin <= cjk * 4) -> overall direction must be to_en.
    en_lead = "Wuthering Waves patch notes overview. " * 25
    zh_body = "今汐在本次更新中调整了共鸣回路与共鸣解放的倍率数值。" * 80
    long_text = f"{en_lead}\n\n{zh_body}"
    assert len(long_text) > 2000

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        )
        try:
            reply = await _http_translation_reply(
                backend,
                TranslationRequest(text=long_text, forced_to_chinese=None),
                input_limit=10000,
            )
            assert "seg-1-en" in reply.text
            assert "seg-2-en" in reply.text
            assert len(sent_bodies) >= 2
            assert all(b.get("to") == "en" for b in sent_bodies)
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_review_backend_recovers_semantic_capability_after_startup_failure():
    from wuwaterm_telegram.review_flow import run_review
    from wuwaterm_telegram.review_model import SEMANTIC_DONE, SEMANTIC_UNAVAILABLE

    now = [100.0]
    core_up = [False]
    cap_calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/meta":
            if not core_up[0]:
                return httpx.Response(503, json={"error": {"code": "llm_unavailable"}})
            return httpx.Response(200, json=_meta_payload())
        if request.url.path == "/v1/capabilities":
            cap_calls[0] += 1
            if not core_up[0]:
                return httpx.Response(503, json={"error": {"code": "llm_unavailable"}})
            return httpx.Response(
                200,
                json={
                    "api_version": "v1",
                    "capabilities": [CAP_SEMANTIC_REVIEW],
                    "request_id": f"cap-{cap_calls[0]}",
                },
            )
        if request.url.path == "/v1/reviews":
            return httpx.Response(
                200,
                json={
                    "request_id": "rev-1",
                    "source_revision": "a" * 64,
                    "target_revision": "b" * 64,
                    "rule_version": "review-v2",
                    "dictionary": {
                        "schema_version": "2",
                        "source_commit": "x",
                        "term_count": 1,
                    },
                    "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": []},
                    "findings": [],
                    "truncated": False,
                },
            )
        if request.url.path == "/v1/semantic-reviews":
            return httpx.Response(
                200,
                json={
                    "review_version": "semantic-review-v1",
                    "status": "complete",
                    "issues": [],
                    "dropped_unanchored": 0,
                    "omitted_issues": 0,
                    "truncated_reason": None,
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            capability_retry_cooldown_seconds=15.0,
            transport=httpx.MockTransport(handler),
            clock=lambda: now[0],
        )
        review_backend = HttpReviewBackend(backend)
        try:
            # Startup capability probe fails while Core is down.
            with pytest.raises(CoreBackendError):
                await backend.probe_capabilities()
            assert review_backend.semantic_available() is False

            # Within cooldown (now=105), /review stays dictionary-only without hammering.
            now[0] = 105.0
            first_report = await run_review(review_backend, "今汐登场", "Jinhsi appears", "en")
            assert first_report.semantic_state == SEMANTIC_UNAVAILABLE
            assert cap_calls[0] == 0

            # Core recovers and cooldown elapses (now=120) -> /review automatically recovers.
            core_up[0] = True
            now[0] = 120.0
            second_report = await run_review(review_backend, "今汐登场", "Jinhsi appears", "en")
            assert second_report.semantic_state == SEMANTIC_DONE
            assert review_backend.semantic_available() is True
            assert cap_calls[0] == 1
        finally:
            await backend.aclose()

    asyncio.run(exercise())


def test_http_review_backend_dedups_concurrent_probes_and_enforces_cooldown_after_failed_refresh():
    from wuwaterm_telegram.review_flow import run_review
    from wuwaterm_telegram.review_model import SEMANTIC_DONE, SEMANTIC_UNAVAILABLE

    now = [100.0]
    mode = ["empty"]  # "empty" -> "down" -> "ready"
    meta_calls = [0]

    class _DelayedAsyncTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/meta":
                meta_calls[0] += 1
                if mode[0] == "down":
                    await asyncio.sleep(0.03)
                    return httpx.Response(
                        503, json={"error": {"code": "llm_unavailable"}}
                    )
                return httpx.Response(200, json=_meta_payload())
            if request.url.path == "/v1/capabilities":
                caps = [CAP_SEMANTIC_REVIEW] if mode[0] == "ready" else []
                return httpx.Response(
                    200,
                    json={"api_version": "v1", "capabilities": caps, "request_id": "c"},
                )
            if request.url.path == "/v1/reviews":
                return httpx.Response(
                    200,
                    json={
                        "request_id": "rev-1",
                        "source_revision": "a" * 64,
                        "target_revision": "b" * 64,
                        "rule_version": "review-v2",
                        "dictionary": {
                            "schema_version": "2",
                            "source_commit": "x",
                            "term_count": 1,
                        },
                        "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": []},
                        "findings": [],
                        "truncated": False,
                    },
                )
            if request.url.path == "/v1/semantic-reviews":
                return httpx.Response(
                    200,
                    json={
                        "review_version": "semantic-review-v1",
                        "status": "complete",
                        "issues": [],
                        "dropped_unanchored": 0,
                        "omitted_issues": 0,
                        "truncated_reason": None,
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        backend = HttpCoreBackend(
            "http://127.0.0.1:8000",
            capability_retry_cooldown_seconds=15.0,
            transport=_DelayedAsyncTransport(),
            clock=lambda: now[0],
        )
        review_backend = HttpReviewBackend(backend)
        try:
            # 1. Initial probe caches empty capabilities at t=100.
            await backend.probe_capabilities()
            assert meta_calls[0] == 1
            assert review_backend.semantic_available() is False

            # 2. Empty cache expires at t=120 while Core /v1/meta is down (503).
            # Two concurrent /review calls must share one in-flight probe (meta_calls == 2, not 5).
            mode[0] = "down"
            now[0] = 120.0
            r1, r2 = await asyncio.gather(
                run_review(review_backend, "今汐登场", "Jinhsi appears", "en"),
                run_review(review_backend, "声骸更新", "Echo updated", "en"),
            )
            assert r1.semantic_state == SEMANTIC_UNAVAILABLE
            assert r2.semantic_state == SEMANTIC_UNAVAILABLE
            assert meta_calls[0] == 2

            # 3. Within 15s cooldown of failed refresh (t=125), no further /v1/meta probe fires.
            now[0] = 125.0
            r3 = await run_review(review_backend, "今汐登场", "Jinhsi appears", "en")
            assert r3.semantic_state == SEMANTIC_UNAVAILABLE
            assert meta_calls[0] == 2

            # 4. After cooldown elapses (t=136) and Core recovers, semantic review activates.
            mode[0] = "ready"
            now[0] = 136.0
            r4 = await run_review(review_backend, "今汐登场", "Jinhsi appears", "en")
            assert r4.semantic_state == SEMANTIC_DONE
            assert meta_calls[0] == 3
        finally:
            await backend.aclose()

    asyncio.run(exercise())
