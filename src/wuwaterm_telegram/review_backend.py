"""The only place that touches Core for `/review`.

Everything `/review` needs from Core goes through `ReviewBackend`. The live
implementation calls Core 0.8.0 (`semantic_review_async`, segmentation-v1,
`review_pair`); tests drive the same flow with a fake backend.

Semantic review is experimental and off by default in Core
(`WUWATERM_SEMANTIC_REVIEW_ENABLED`). While it is off, or no model is
configured, `semantic_available()` is False and `/review` reports only the
deterministic dictionary track.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from wuwaterm.application import (
    ContextBefore,
    LLMTranslationError,
    SemanticReviewJob,
    TermService,
    count_cjk,
    error_code_for_llm_reason,
    llm_configured,
    review_pair,
    semantic_review_async,
    semantic_review_enabled,
)
from wuwaterm.segmentation import SEGMENT_LIMIT, pair_segments

from .core_compat import missing_review_symbols
from .review_model import (
    PAIR_NEEDS_MANUAL_SPLIT,
    PAIR_READY,
    PairInfo,
    Span,
    TermFinding,
    VERDICT_NOT_EVALUATED,
    VERDICT_VERIFIED,
)

LOGGER = logging.getLogger(__name__)

# The request limit of one model call, in Unicode scalars per side.
REQUEST_SIDE_LIMIT = 2000
# Error codes from Core that end the whole review rather than one segment.
STOP_CODES = frozenset({"rate_limited", "llm_budget_exhausted"})
CODE_UNAVAILABLE = "llm_unavailable"
CODE_INTERNAL = "internal"
_MAX_CANDIDATES_SHOWN = 2


class ReviewBackendError(Exception):
    """A Core failure, reduced to a stable code. Never carries user text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TermReview:
    """Deterministic findings for one pair, in that pair's own coordinates."""

    findings: tuple[TermFinding, ...] = ()
    verified: int = 0


class ReviewBackend(Protocol):
    def semantic_available(self) -> bool: ...

    def plan_pairs(self, source: str, target: str) -> list[PairInfo]: ...

    async def term_review(self, source: str, target: str, direction: str) -> TermReview: ...

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
    ) -> Mapping[str, Any]: ...


def infer_direction(target: str) -> str:
    """Target language of a translation: `zh` when it is mostly CJK, else `en`."""
    letters = sum(1 for ch in target if ch.isalpha())
    if letters == 0:
        return "en"
    return "zh" if count_cjk(target) * 2 >= letters else "en"


def enforce_pair_limits(pairs: Sequence[PairInfo]) -> list[PairInfo]:
    """Last check before sending: a ready pair over its limit is never sent."""
    checked: list[PairInfo] = []
    for pair in pairs:
        if pair.status == PAIR_READY and (
            pair.source_len > pair.limit
            or pair.target_len > pair.limit
            or pair.source_len > REQUEST_SIDE_LIMIT
            or pair.target_len > REQUEST_SIDE_LIMIT
            or pair.source_len <= 0
            or pair.target_len <= 0
        ):
            pair = replace(pair, status=PAIR_NEEDS_MANUAL_SPLIT)
        checked.append(pair)
    return checked


class LiveReviewBackend:
    """Reaches Core in this process."""

    def __init__(self, service: TermService, translator: Any) -> None:
        self._service = service
        self._translator = translator

    # --- capability probe -------------------------------------------------

    def semantic_available(self) -> bool:
        return (
            not missing_review_symbols()
            and semantic_review_enabled()
            and llm_configured()
        )

    # --- pairing ----------------------------------------------------------

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

    # --- deterministic track ----------------------------------------------

    async def term_review(self, source: str, target: str, direction: str) -> TermReview:
        try:
            report = await asyncio.to_thread(
                review_pair,
                self._service,
                source,
                target,
                direction,
                review_version="review-v2",
            )
        except Exception as exc:  # noqa: BLE001 - reduced to a code below
            code = getattr(exc, "code", CODE_INTERNAL)
            LOGGER.warning("review term track failed code=%s", code)
            raise ReviewBackendError(str(code)) from exc
        findings: list[TermFinding] = []
        verified = 0
        for item in report.findings:
            if item.verdict == VERDICT_VERIFIED:
                verified += 1
                continue
            if item.verdict == VERDICT_NOT_EVALUATED:
                continue
            target_span = item.target_span
            findings.append(
                TermFinding(
                    verdict=item.verdict,
                    source_span=Span(
                        item.source_span.start, item.source_span.end, item.source_span.text
                    ),
                    target_span=(
                        Span(target_span.start, target_span.end, target_span.text)
                        if target_span is not None
                        else None
                    ),
                    candidates=tuple(
                        (candidate.zh, candidate.en)
                        for candidate in item.candidates[:_MAX_CANDIDATES_SHOWN]
                    ),
                )
            )
        return TermReview(tuple(findings), verified)

    # --- semantic track ---------------------------------------------------

    async def semantic_review(
        self,
        source: str,
        target: str,
        *,
        direction: str,
        explain_language: str,
        context_before: tuple[str, str] | None,
    ) -> Mapping[str, Any]:
        job = SemanticReviewJob(
            source=source,
            target=target,
            direction=direction,
            explain_language=explain_language,
            context_before=(
                ContextBefore(source=context_before[0], target=context_before[1])
                if context_before is not None
                else None
            ),
            # Telegram has no manuscript, hence no human precedents.
            precedents=(),
        )
        try:
            outcome = await semantic_review_async(
                self._service,
                self._translator,
                job,
                request_id=uuid.uuid4().hex,
                before_llm_call=None,
                offload=asyncio.to_thread,
            )
        except LLMTranslationError as exc:
            raise ReviewBackendError(
                error_code_for_llm_reason(getattr(exc, "reason", None))
            ) from exc
        except Exception as exc:  # noqa: BLE001 - reduced to a code below
            LOGGER.warning("semantic review failed error_type=%s", type(exc).__name__)
            raise ReviewBackendError(CODE_INTERNAL) from exc
        if outcome.error_code is not None:
            raise ReviewBackendError(outcome.error_code)
        if not isinstance(outcome.body, Mapping):
            raise ReviewBackendError(CODE_INTERNAL)
        return outcome.body
