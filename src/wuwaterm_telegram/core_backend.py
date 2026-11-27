"""Service-boundary abstraction for consuming WuwaTerm Core.

Supports two execution modes behind one clean contract:
- ``HttpCoreBackend`` (service consumption mode): talks to an independently
  deployed WuwaTerm Core HTTP API (``/v1/meta``, ``/v1/capabilities``,
  ``/v1/dictionary``, ``/v1/terms``, ``/v1/translations``, ``/v1/reviews``,
  ``/v1/semantic-reviews``, ``/v1/structure-checks``) with runtime capability
  negotiation and safe degradation when optional capabilities are absent.
- ``LocalCoreBackend`` (embedded compatibility mode): calls the in-process
  ``wuwaterm`` library directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

import httpx

from wuwaterm.application import (
    ContextBefore,
    KIND_ERROR,
    KIND_LLM,
    LLM_INPUT_CHAR_LIMIT,
    LLMTranslationError,
    OperationExecutionBudget,
    SentenceTranslator,
    TRANSLATION_UNAVAILABLE_NOTICE,
    TermService,
    TranslationContext,
    TranslationJob,
    TranslationOutcome,
    llm_configured,
    translate_request_async,
)
from wuwaterm.segmentation import (
    SEGMENT_LIMIT,
    WHITESPACE as _SEGMENT_WHITESPACE,
    pair_segments,
    segment_text,
)
from wuwaterm.structural_checks import check_segment_structure

from .core_compat import CORE_VERSION, probe_local_core_capabilities
from .review_backend import (
    CODE_INTERNAL,
    CODE_UNAVAILABLE,
    LiveReviewBackend,
    ReviewBackendError,
    TermReview,
    enforce_pair_limits,
)
from .review_model import PairInfo, Span, TermFinding, VERDICT_VERIFIED
from .telegram_html import (
    normalize_telegram_html,
    protect_telegram_html,
    strip_telegram_html,
    translate_html_async,
    validate_telegram_html,
)


LOGGER = logging.getLogger(__name__)

# Capability tokens follow kebab-case + version suffix (^[a-z0-9][a-z0-9.-]{0,63}$).
CAPABILITY_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}\Z")

CAP_TRANSLATION_CONTEXT = "translation-context-v1"
CAP_STRUCTURE_CHECK = "structure-check-v1"
CAP_SEMANTIC_REVIEW = "semantic-review-v1"
CAP_DICTIONARY_IDENTITY = "dictionary-identity-v1"
CAP_AMBIGUOUS_MENTION = "ambiguous-mention-v1"
# Opt-in Core HTTP capabilities negotiated via GET /v1/capabilities.
CAP_PROTECTED_TRANSLATION = "protected-translation-v1"
CAP_DICTIONARY_FALLBACK = "dictionary-fallback-v1"
CAP_KNOWLEDGE_RETRIEVAL = "knowledge-retrieval-v1"
CAP_HYBRID_RAG = "hybrid-rag-v1"

DEFAULT_CAPABILITY_CACHE_TTL_SECONDS = 60.0
DEFAULT_CORE_HTTP_TIMEOUT_SECONDS = 45.0


class CoreBackendError(RuntimeError):
    """Sanitized Core service error carrying a fixed error code (never user text)."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CoreMeta:
    """Service metadata and negotiated capabilities."""

    backend_kind: Literal["http", "local"]
    service_version: str
    api_version: str
    schema_version: str | None = None
    source_profile: str | None = None
    source_commit: str | None = None
    source_game_version: str | None = None
    term_count: int = 0
    llm_configured: bool = False
    dictionary_revision: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CoreTermMatch:
    zh: str
    en: str
    category: str = ""
    score: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class CoreStructureSegmentResult:
    index: int
    rule_ids: tuple[str, ...]
    source_paragraphs: int
    target_paragraphs: int
    source_lines: int
    target_lines: int


class CoreBackend(Protocol):
    """Protocol-neutral boundary between Telegram and WuwaTerm Core."""

    @property
    def backend_kind(self) -> Literal["http", "local"]: ...

    async def probe_capabilities(self, *, force: bool = False) -> CoreMeta: ...

    async def lookup_terms(self, query: str) -> list[CoreTermMatch]: ...

    async def lookup_exact(
        self, query: str, *, to_chinese: bool = False
    ) -> str | None: ...

    async def translate_text(
        self,
        text: str,
        *,
        to_chinese: bool | None = None,
        context: TranslationContext | None = None,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> TranslationOutcome: ...

    async def translate_protected_html(
        self,
        html_text: str,
        *,
        to_chinese: bool = False,
        context: TranslationContext | None = None,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> tuple[str, bool]: ...

    async def check_structure(
        self, segments: Sequence[tuple[str, str]]
    ) -> list[CoreStructureSegmentResult]: ...

    async def term_review(
        self, source: str, target: str, direction: str
    ) -> TermReview: ...

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> Mapping[str, Any]: ...

    async def aclose(self) -> None: ...


class LocalCoreBackend:
    """Embedded Core backend for single-process deployments and tests."""

    def __init__(
        self,
        service: TermService,
        translator: SentenceTranslator,
    ) -> None:
        self._service = service
        self._translator = translator
        self._review = LiveReviewBackend(service, translator)

    @property
    def backend_kind(self) -> Literal["http", "local"]:
        return "local"

    async def probe_capabilities(self, *, force: bool = False) -> CoreMeta:
        del force
        meta = self._service.metadata()
        return CoreMeta(
            backend_kind="local",
            service_version=CORE_VERSION,
            api_version="local",
            schema_version=meta.get("schema_version"),
            source_profile=meta.get("source_profile"),
            source_commit=meta.get("wutheringdata_commit"),
            source_game_version=meta.get("source_game_version"),
            term_count=self._service.term_count(),
            llm_configured=llm_configured(),
            capabilities=probe_local_core_capabilities(),
        )

    async def lookup_terms(self, query: str) -> list[CoreTermMatch]:
        result = await asyncio.to_thread(self._service.lookup, query, limit=5)
        return [
            CoreTermMatch(
                zh=item.entry.zh,
                en=item.entry.en,
                category=getattr(item.entry, "category", ""),
                score=float(item.score),
                reason=str(item.reason),
            )
            for item in result.matches
        ]

    async def lookup_exact(
        self, query: str, *, to_chinese: bool = False
    ) -> str | None:
        result = await asyncio.to_thread(self._service.lookup_exact, query)
        if result.exact and result.best:
            official = result.best.entry.zh if to_chinese else result.best.entry.en
            return official or None
        return None

    async def translate_text(
        self,
        text: str,
        *,
        to_chinese: bool | None = None,
        context: TranslationContext | None = None,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> TranslationOutcome:
        return await translate_request_async(
            self._service,
            self._translator,
            TranslationJob(
                text=text,
                forced_to_chinese=to_chinese,
                context=context,
            ),
            before_llm_call=before_llm_call,
            offload=asyncio.to_thread,
            execution_budget=execution_budget,
        )

    async def translate_protected_html(
        self,
        html_text: str,
        *,
        to_chinese: bool = False,
        context: TranslationContext | None = None,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> tuple[str, bool]:
        translated = await translate_html_async(
            self._translator,
            html_text,
            to_chinese=to_chinese,
            before_llm_call=before_llm_call,
            execution_budget=execution_budget,
            context=context,
        )
        return translated, True

    async def check_structure(
        self, segments: Sequence[tuple[str, str]]
    ) -> list[CoreStructureSegmentResult]:
        results: list[CoreStructureSegmentResult] = []
        for idx, (source, target) in enumerate(segments):
            report = check_segment_structure(source, target)
            results.append(
                CoreStructureSegmentResult(
                    index=idx,
                    rule_ids=tuple(f.rule_id for f in report.findings),
                    source_paragraphs=report.layout.source_paragraphs,
                    target_paragraphs=report.layout.target_paragraphs,
                    source_lines=report.layout.source_lines,
                    target_lines=report.layout.target_lines,
                )
            )
        return results

    async def term_review(
        self, source: str, target: str, direction: str
    ) -> TermReview:
        return await self._review.term_review(source, target, direction)

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> Mapping[str, Any]:
        return await self._review.semantic_review(
            source,
            target,
            direction=direction,
            explain_language=explain_language,
            context_before=context_before,
            execution_budget=execution_budget,
        )

    async def aclose(self) -> None:
        await self._translator.aclose()


def _serialize_translation_context(
    context: TranslationContext | None,
) -> dict[str, Any] | None:
    if context is None:
        return None
    payload: dict[str, Any] = {}
    if context.before is not None:
        payload["before"] = {
            "source": context.before.source,
            "target": context.before.target,
        }
    if context.precedents:
        payload["precedents"] = [
            {
                "surface": item.surface,
                "choice": item.choice,
                "rendering": item.rendering,
                "snippet": item.snippet,
                "anchor": {
                    "segment_index": item.segment_index,
                    "source_start": item.source_start,
                    "source_end": item.source_end,
                },
                "scope": "occurrence",
            }
            for item in context.precedents
        ]
    return payload


def _extract_error_code(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and isinstance(err.get("code"), str):
                return err["code"]
            if isinstance(data.get("code"), str):
                return data["code"]
    except ValueError:
        pass
    if response.status_code == 401:
        return "unauthorized"
    if response.status_code == 403:
        return "forbidden"
    if response.status_code == 429:
        return "rate_limited"
    if response.status_code == 422:
        return "input_too_long"
    if response.status_code >= 500:
        return "llm_unavailable"
    return "invalid_request"


class HttpCoreBackend:
    """Service-consumption backend talking to WuwaTerm Core HTTP API (v1).

    Negotiates optional capabilities strictly via ``GET /v1/capabilities``
    (treating 404 on ``/v1/capabilities`` as an empty optional set, per the
    Core contract; ``GET /v1/meta`` stays on its published exact-key schema).
    Opt-in extension fields on ``POST /v1/translations`` such as ``context``
    (``translation-context-v1``) and ``protected_text``
    (``protected-translation-v1``) are sent only when negotiated, and degrade
    cleanly otherwise.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        timeout_seconds: float = DEFAULT_CORE_HTTP_TIMEOUT_SECONDS,
        capability_ttl_seconds: float = DEFAULT_CAPABILITY_CACHE_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Core HTTP API base_url must start with http:// or https://")
        self._base_url = normalized
        self._api_token = (api_token or "").strip() or None
        self._timeout_seconds = timeout_seconds
        self._capability_ttl_seconds = capability_ttl_seconds
        self._clock = clock
        headers = {"Accept": "application/json"}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
        )
        self._cached_meta: CoreMeta | None = None
        self._cached_at: float | None = None

    @property
    def backend_kind(self) -> Literal["http", "local"]:
        return "http"

    @property
    def cached_meta(self) -> CoreMeta | None:
        return self._cached_meta

    async def probe_capabilities(self, *, force: bool = False) -> CoreMeta:
        now = self._clock()
        if (
            not force
            and self._cached_meta is not None
            and self._cached_at is not None
            and (now - self._cached_at) < self._capability_ttl_seconds
        ):
            return self._cached_meta

        try:
            meta_resp = await self._client.get("/v1/meta")
        except httpx.HTTPError as exc:
            raise CoreBackendError("llm_unavailable") from exc
        if meta_resp.status_code != 200:
            raise CoreBackendError(_extract_error_code(meta_resp))
        try:
            meta_body = meta_resp.json()
        except ValueError as exc:
            raise CoreBackendError("llm_unavailable") from exc
        if not isinstance(meta_body, Mapping):
            raise CoreBackendError("llm_unavailable")

        caps: set[str] = set()
        # Primary capability channel: GET /v1/capabilities.
        # GET /v1/meta keeps its published exact-key contract for Web/Windows clients.
        try:
            caps_resp = await self._client.get("/v1/capabilities")
        except httpx.HTTPError:
            caps_resp = None
        if caps_resp is not None and caps_resp.status_code == 200:
            try:
                caps_body = caps_resp.json()
            except ValueError:
                LOGGER.warning("core capabilities probe returned non-JSON 200; degrading safely")
                caps_body = None
            if isinstance(caps_body, Mapping):
                raw_caps = caps_body.get("capabilities")
                if isinstance(raw_caps, list):
                    caps.update(
                        item
                        for item in raw_caps
                        if isinstance(item, str) and CAPABILITY_TOKEN_RE.fullmatch(item)
                    )
        elif caps_resp is not None and caps_resp.status_code not in (404, 405):
            LOGGER.warning(
                "core capabilities probe returned status=%s", caps_resp.status_code
            )

        dictionary_revision: str | None = None
        if CAP_DICTIONARY_IDENTITY in caps:
            try:
                dict_resp = await self._client.get("/v1/dictionary")
                if dict_resp.status_code == 200:
                    dict_body = dict_resp.json()
                    if isinstance(dict_body, Mapping):
                        rev = dict_body.get("revision")
                        if isinstance(rev, str):
                            dictionary_revision = rev
            except (httpx.HTTPError, ValueError):
                pass

        meta = CoreMeta(
            backend_kind="http",
            service_version=str(meta_body.get("service_version", "unknown")),
            api_version=str(meta_body.get("api_version", "v1")),
            schema_version=meta_body.get("schema_version"),
            source_profile=meta_body.get("source_profile"),
            source_commit=meta_body.get("source_commit"),
            source_game_version=meta_body.get("source_game_version"),
            term_count=int(meta_body.get("term_count") or 0),
            llm_configured=bool(meta_body.get("llm_configured", False)),
            dictionary_revision=dictionary_revision,
            capabilities=frozenset(caps),
        )
        self._cached_meta = meta
        self._cached_at = now
        return meta

    async def has_capability(self, capability: str) -> bool:
        meta = await self.probe_capabilities()
        return capability in meta.capabilities

    async def lookup_terms(self, query: str) -> list[CoreTermMatch]:
        try:
            resp = await self._client.get("/v1/terms", params={"q": query})
        except httpx.HTTPError as exc:
            raise CoreBackendError("llm_unavailable") from exc
        if resp.status_code != 200:
            raise CoreBackendError(_extract_error_code(resp))
        body = resp.json()
        matches = body.get("matches") or []
        return [
            CoreTermMatch(
                zh=str(item.get("zh", "")),
                en=str(item.get("en", "")),
                category=str(item.get("category", "")),
                score=float(item.get("score", 0.0)),
                reason=str(item.get("reason", "")),
            )
            for item in matches
            if isinstance(item, Mapping)
        ]

    async def lookup_exact(
        self, query: str, *, to_chinese: bool = False
    ) -> str | None:
        matches = await self.lookup_terms(query)
        for item in matches:
            if item.reason == "exact":
                official = item.zh if to_chinese else item.en
                if official:
                    return official
        return None

    async def llm_available(self) -> bool:
        meta = await self.probe_capabilities()
        return meta.llm_configured

    async def _translate_single_text(
        self,
        text: str,
        *,
        meta: CoreMeta,
        to_chinese: bool | None = None,
        context: TranslationContext | None = None,
        allow_dictionary_fallback: bool = False,
        before_llm_call=None,
    ) -> TranslationOutcome:
        if before_llm_call is not None:
            result = before_llm_call()
            if asyncio.iscoroutine(result):
                await result

        body: dict[str, Any] = {"text": text}
        if to_chinese is not None:
            body["to"] = "zh" if to_chinese else "en"
        if context is not None and CAP_TRANSLATION_CONTEXT in meta.capabilities:
            serialized = _serialize_translation_context(context)
            if serialized is not None:
                body["context"] = serialized
        if allow_dictionary_fallback and CAP_DICTIONARY_FALLBACK in meta.capabilities:
            body["dictionary_fallback"] = True

        try:
            resp = await self._client.post("/v1/translations", json=body)
        except httpx.TimeoutException:
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code="timeout",
            )
        except httpx.ConnectError:
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code="connect",
            )
        except httpx.HTTPError:
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code="request",
            )
        if resp.status_code != 200:
            code = _extract_error_code(resp)
            if resp.status_code >= 500:
                code = "upstream"
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code=code,
            )
        try:
            data = resp.json()
        except ValueError:
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code="invalid_response",
            )
        if not isinstance(data, Mapping):
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code="invalid_response",
            )
        direction = data.get("direction", "zh" if to_chinese else "en")
        return TranslationOutcome(
            kind=data.get("kind", KIND_LLM),
            text=str(data.get("text", "")),
            to_chinese=(direction == "zh"),
            dictionary_miss=bool(data.get("dictionary_miss", False)),
            markup_used=False,
        )

    async def translate_text(
        self,
        text: str,
        *,
        to_chinese: bool | None = None,
        context: TranslationContext | None = None,
        allow_dictionary_fallback: bool = False,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> TranslationOutcome:
        del execution_budget
        try:
            meta = await self.probe_capabilities()
        except CoreBackendError as exc:
            return TranslationOutcome(
                kind=KIND_ERROR,
                text=TRANSLATION_UNAVAILABLE_NOTICE,
                to_chinese=bool(to_chinese),
                error_code=exc.code,
            )
        if len(text) <= LLM_INPUT_CHAR_LIMIT:
            return await self._translate_single_text(
                text,
                meta=meta,
                to_chinese=to_chinese,
                context=context,
                allow_dictionary_fallback=allow_dictionary_fallback,
                before_llm_call=before_llm_call,
            )
        segments = segment_text(text, limit=SEGMENT_LIMIT)
        pieces: list[str] = []
        before = context.before if context is not None else None
        resolved_to_chinese = to_chinese
        for seg in segments:
            core = text[seg.core_start : seg.core_end]
            if not core:
                pieces.append(text[seg.start : seg.end])
                continue
            seg_ctx = TranslationContext(before=before) if before is not None or context is not None else None
            outcome = await self._translate_single_text(
                core,
                meta=meta,
                to_chinese=resolved_to_chinese,
                context=seg_ctx,
                allow_dictionary_fallback=allow_dictionary_fallback,
                before_llm_call=before_llm_call,
            )
            if outcome.failed:
                return outcome
            resolved_to_chinese = outcome.to_chinese
            translated = outcome.text
            start, end = 0, len(translated)
            while start < end and translated[start] in _SEGMENT_WHITESPACE:
                start += 1
            while end > start and translated[end - 1] in _SEGMENT_WHITESPACE:
                end -= 1
            translated = translated[start:end]
            pieces.append(
                text[seg.start : seg.core_start]
                + translated
                + text[seg.core_end : seg.end]
            )
            before = ContextBefore(source=core[-400:], target=translated[-400:])
        return TranslationOutcome(
            kind=KIND_LLM,
            text="".join(pieces),
            to_chinese=bool(resolved_to_chinese),
        )

    async def translate_protected_html(
        self,
        html_text: str,
        *,
        to_chinese: bool = False,
        context: TranslationContext | None = None,
        before_llm_call=None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> tuple[str, bool]:
        """Translate Telegram HTML using negotiated Core HTTP capabilities.

        When the Core server advertises ``protected-translation-v1`` via
        ``GET /v1/capabilities``, sends the opt-in ``protected_text`` payload
        (plus ``context`` when ``translation-context-v1`` is also advertised)
        and restores/validates the returned segments. When absent, degrades
        safely to plain text via ``POST /v1/translations`` and reports
        ``markup_used=False``.
        """
        meta = await self.probe_capabilities()
        normalized_html = normalize_telegram_html(html_text)
        if CAP_PROTECTED_TRANSLATION in meta.capabilities:
            protected = protect_telegram_html(normalized_html)
            if before_llm_call is not None:
                result = before_llm_call()
                if asyncio.iscoroutine(result):
                    await result
            body: dict[str, Any] = {
                "text": strip_telegram_html(normalized_html),
                "to": "zh" if to_chinese else "en",
                "protected_text": {
                    "visible_segments": list(protected.visible_segments),
                    "structures": [
                        {"original": s.original, "visible_context": s.visible_context}
                        for s in protected.structures
                    ],
                },
            }
            if context is not None and CAP_TRANSLATION_CONTEXT in meta.capabilities:
                serialized = _serialize_translation_context(context)
                if serialized is not None:
                    body["context"] = serialized
            try:
                resp = await self._client.post("/v1/translations", json=body)
            except httpx.TimeoutException as exc:
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE, reason="timeout"
                ) from exc
            except httpx.ConnectError as exc:
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE, reason="connect"
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE, reason="request"
                ) from exc
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE, reason="invalid_response"
                    ) from exc
                returned_segments = data.get("visible_segments") if isinstance(data, Mapping) else None
                if (
                    isinstance(returned_segments, list)
                    and len(returned_segments) == len(protected.visible_segments)
                    and all(isinstance(seg, str) for seg in returned_segments)
                ):
                    restored_parts: list[str] = []
                    for index, seg in enumerate(returned_segments):
                        restored_parts.append(seg)
                        if index < len(protected.structures):
                            restored_parts.append(protected.structures[index].original)
                    restored = "".join(restored_parts)
                    if validate_telegram_html(restored):
                        return restored, True
                plain_text = data.get("text") if isinstance(data, Mapping) else None
                if isinstance(plain_text, str) and plain_text.strip():
                    return strip_telegram_html(plain_text), False
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE, reason="html_integrity"
                )
            code = "upstream" if resp.status_code >= 500 else _extract_error_code(resp)
            if code != "invalid_request":
                raise LLMTranslationError(
                    TRANSLATION_UNAVAILABLE_NOTICE, reason=code
                )

        plain_source = strip_telegram_html(normalized_html)
        outcome = await self.translate_text(
            plain_source,
            to_chinese=to_chinese,
            context=context,
            before_llm_call=before_llm_call,
            execution_budget=execution_budget,
        )
        if outcome.failed:
            raise LLMTranslationError(
                outcome.text or TRANSLATION_UNAVAILABLE_NOTICE,
                reason=outcome.error_code or "upstream",
            )
        return outcome.text, False

    async def check_structure(
        self, segments: Sequence[tuple[str, str]]
    ) -> list[CoreStructureSegmentResult]:
        if not segments:
            return []
        meta = await self.probe_capabilities()
        if CAP_STRUCTURE_CHECK in meta.capabilities:
            payload = {
                "check_version": "structure-check-v1",
                "segments": [
                    {"source": source, "target": target}
                    for source, target in segments
                ],
            }
            try:
                resp = await self._client.post("/v1/structure-checks", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    out: list[CoreStructureSegmentResult] = []
                    for item in data.get("segments", []):
                        layout = item.get("layout", {})
                        out.append(
                            CoreStructureSegmentResult(
                                index=int(item.get("index", len(out))),
                                rule_ids=tuple(
                                    str(f["rule_id"])
                                    for f in item.get("findings", [])
                                    if isinstance(f, Mapping) and "rule_id" in f
                                ),
                                source_paragraphs=int(layout.get("source_paragraphs", 0)),
                                target_paragraphs=int(layout.get("target_paragraphs", 0)),
                                source_lines=int(layout.get("source_lines", 0)),
                                target_lines=int(layout.get("target_lines", 0)),
                            )
                        )
                    return out
            except (httpx.HTTPError, ValueError, KeyError):
                LOGGER.warning("core HTTP structure-checks failed; falling back locally")

        # Fallback: zero-cost local deterministic structure checks.
        results: list[CoreStructureSegmentResult] = []
        for idx, (source, target) in enumerate(segments):
            report = check_segment_structure(source, target)
            results.append(
                CoreStructureSegmentResult(
                    index=idx,
                    rule_ids=tuple(f.rule_id for f in report.findings),
                    source_paragraphs=report.layout.source_paragraphs,
                    target_paragraphs=report.layout.target_paragraphs,
                    source_lines=report.layout.source_lines,
                    target_lines=report.layout.target_lines,
                )
            )
        return results

    async def term_review(
        self, source: str, target: str, direction: str
    ) -> TermReview:
        payload = {
            "review_version": "review-v2",
            "source": source,
            "target": target,
            "direction": direction,
        }
        try:
            resp = await self._client.post("/v1/reviews", json=payload)
        except httpx.HTTPError as exc:
            raise ReviewBackendError(CODE_UNAVAILABLE) from exc
        if resp.status_code != 200:
            raise ReviewBackendError(_extract_error_code(resp))
        data = resp.json()
        findings: list[TermFinding] = []
        verified = 0
        for item in data.get("findings", []):
            verdict = item.get("verdict")
            if verdict == VERDICT_VERIFIED:
                verified += 1
                continue
            src = item.get("source_span")
            if not isinstance(src, Mapping):
                continue
            tgt = item.get("target_span")
            findings.append(
                TermFinding(
                    verdict=str(verdict),
                    source_span=Span(int(src["start"]), int(src["end"]), str(src["text"])),
                    target_span=(
                        Span(int(tgt["start"]), int(tgt["end"]), str(tgt["text"]))
                        if isinstance(tgt, Mapping)
                        else None
                    ),
                    candidates=tuple(
                        (str(c["zh"]), str(c["en"]))
                        for c in (item.get("candidates") or [])[:2]
                        if isinstance(c, Mapping)
                    ),
                    reason=item.get("reason"),
                )
            )
        return TermReview(
            findings=tuple(findings),
            verified=verified,
            truncated=bool(data.get("truncated", False)),
        )

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> Mapping[str, Any]:
        del execution_budget
        meta = await self.probe_capabilities()
        if CAP_SEMANTIC_REVIEW not in meta.capabilities:
            raise ReviewBackendError(CODE_UNAVAILABLE)
        payload: dict[str, Any] = {
            "source": source,
            "target": target,
            "direction": direction,
            "review_version": "semantic-review-v1",
            "explain_language": explain_language,
            "precedents": [],
        }
        if context_before is not None:
            payload["context"] = {
                "before": {
                    "source": context_before[0],
                    "target": context_before[1],
                }
            }
        try:
            resp = await self._client.post("/v1/semantic-reviews", json=payload)
        except httpx.HTTPError as exc:
            raise ReviewBackendError(CODE_UNAVAILABLE) from exc
        if resp.status_code != 200:
            raise ReviewBackendError(_extract_error_code(resp))
        data = resp.json()
        if not isinstance(data, Mapping):
            raise ReviewBackendError(CODE_INTERNAL)
        return data

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpReviewBackend:
    """Adapter exposing ``HttpCoreBackend`` through the ``ReviewBackend`` protocol."""

    def __init__(self, backend: HttpCoreBackend) -> None:
        self._backend = backend

    def semantic_available(self) -> bool:
        meta = self._backend.cached_meta
        return bool(
            meta is not None
            and meta.llm_configured
            and CAP_SEMANTIC_REVIEW in meta.capabilities
        )

    def plan_pairs(self, source: str, target: str) -> list[PairInfo]:
        pairs = [
            PairInfo(
                index=item.index,
                status=item.status,
                hard_cut=bool(item.hard_cut),
                source_start=item.source.start,
                source_end=item.source.end,
                source_core_start=item.source.core_start,
                source_core_end=item.source.core_end,
                target_start=item.target.start,
                target_end=item.target.end,
                target_core_start=item.target.core_start,
                target_core_end=item.target.core_end,
                limit=SEGMENT_LIMIT,
            )
            for item in pair_segments(source, target, SEGMENT_LIMIT)
        ]
        return enforce_pair_limits(pairs)

    async def term_review(
        self, source: str, target: str, direction: str
    ) -> TermReview:
        return await self._backend.term_review(source, target, direction)

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
        execution_budget: OperationExecutionBudget | None = None,
    ) -> Mapping[str, Any]:
        return await self._backend.semantic_review(
            source,
            target,
            direction=direction,
            explain_language=explain_language,
            context_before=context_before,
            execution_budget=execution_budget,
        )
