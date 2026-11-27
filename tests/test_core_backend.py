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
