"""The only place that touches Core for `/review`.

Everything `/review` needs from Core goes through `ReviewBackend`. The live
implementation reaches the newer Core symbols (`semantic_review_async`,
`wuwaterm.segmentation`) lazily, so this module imports on a Core that does not
have them; `semantic_available()` then says so and `/review` falls back to the
deterministic dictionary track. Tests drive the same flow with a fake backend.

Assumptions about Core 0.8.0 that must be re-checked once its engine (C2)
is merged are listed in `docs/review-core-contract.md`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from importlib import import_module
from typing import Any, Mapping, Protocol, Sequence

from wuwaterm.application import (
    LLMTranslationError,
    TermService,
    count_cjk,
    error_code_for_llm_reason,
    llm_configured,
    review_pair,
)

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


def semantic_request(
    source: str,
    target: str,
    *,
    direction: str,
    explain_language: str,
    context_before: tuple[str, str] | None,
) -> dict[str, Any]:
    """The `semantic-review-v1` request body for one paired segment.

    Telegram has no manuscript, so there are no human precedents to send.
    """
    body: dict[str, Any] = {
        "source": source,
        "target": target,
        "direction": direction,
        "review_version": "semantic-review-v1",
        "explain_language": explain_language,
        "precedents": [],
    }
    if context_before is not None:
        body["context"] = {
            "before": {"source": context_before[0], "target": context_before[1]}
        }
    return body


def infer_direction(target: str) -> str:
    """Target language of a translation: `zh` when it is mostly CJK, else `en`."""
    letters = sum(1 for ch in target if ch.isalpha())
    if letters == 0:
        return "en"
    return "zh" if count_cjk(target) * 2 >= letters else "en"


def _fallback_pairs(source: str, target: str) -> list[PairInfo]:
    """Pairing without segmentation-v1: one request, or nothing.

    Used only on a Core that does not ship `wuwaterm.segmentation`. Without the
    shared rule there is no trustworthy way to cut, so a text over the request
    limit is reported as needing a manual split.
    """
    s_len, t_len = len(source), len(target)
    fits = s_len <= REQUEST_SIDE_LIMIT and t_len <= REQUEST_SIDE_LIMIT
    status = PAIR_READY if fits else PAIR_NEEDS_MANUAL_SPLIT
    return [
        PairInfo(
            index=0,
            status=status,
            hard_cut=False,
            source_start=0,
            source_end=s_len,
            source_core_start=0,
            source_core_end=s_len,
            target_start=0,
            target_end=t_len,
            target_core_start=0,
            target_core_end=t_len,
            limit=REQUEST_SIDE_LIMIT,
        )
    ]


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

    @staticmethod
    def _core_attr(module: str, name: str) -> Any | None:
        try:
            return getattr(import_module(module), name, None)
        except ImportError:
            return None

    def semantic_available(self) -> bool:
        return not missing_review_symbols() and llm_configured()

    # --- pairing ----------------------------------------------------------

    def plan_pairs(self, source: str, target: str) -> list[PairInfo]:
        pair_segments = self._core_attr("wuwaterm.segmentation", "pair_segments")
        if pair_segments is None:
            return _fallback_pairs(source, target)
        limit = int(self._core_attr("wuwaterm.segmentation", "SEGMENT_LIMIT") or 1800)
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
                limit=limit,
            )
            for item in pair_segments(source, target, limit)
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
        run = self._core_attr("wuwaterm.application", "semantic_review_async")
        if run is None:
            raise ReviewBackendError(CODE_UNAVAILABLE)
        request = semantic_request(
            source,
            target,
            direction=direction,
            explain_language=explain_language,
            context_before=context_before,
        )
        try:
            # ASSUMPTION (re-check after Core C2 merges): the application entry
            # takes the term service, the translator and the published request
            # body, and returns the published response body as a mapping.
            body = await run(self._service, self._translator, request)
        except LLMTranslationError as exc:
            reason = getattr(exc, "reason", None)
            raise ReviewBackendError(error_code_for_llm_reason(reason)) from exc
        except Exception as exc:  # noqa: BLE001 - reduced to a code below
            code = getattr(exc, "code", None)
            LOGGER.warning("semantic review failed error_type=%s", type(exc).__name__)
            raise ReviewBackendError(code if isinstance(code, str) else CODE_INTERNAL) from exc
        if not isinstance(body, Mapping):
            raise ReviewBackendError(CODE_INTERNAL)
        return body
