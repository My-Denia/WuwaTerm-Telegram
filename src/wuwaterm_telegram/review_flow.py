"""Run one `/review`: pair, check terms, then review each segment in turn.

The two tracks stay apart. The deterministic dictionary track (review-v2) runs
for every ready pair. The model track runs one segment at a time, never two at
once, and stops on a rate limit or an exhausted budget with the count of
segments finished. A segment answer that breaks the contract is reported as
not reviewed; nothing in it is shown.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from .review_backend import (
    STOP_CODES,
    ReviewBackend,
    ReviewBackendError,
)
from .review_model import (
    PAIR_READY,
    SEMANTIC_DONE,
    SEMANTIC_PARTIAL,
    SEMANTIC_STOPPED,
    SEMANTIC_UNAVAILABLE,
    PairInfo,
    ReviewReport,
    SegmentOutcome,
    SemanticIssue,
    SemanticResponseError,
    Span,
    TermFinding,
    parse_semantic_response,
)

LOGGER = logging.getLogger(__name__)

# One /review sends at most this many segments to the model. The limit is per
# command, not per segment: a command spends one rate-limit slot, and this cap
# bounds how many model calls that slot can cause.
MAX_REVIEW_PAIRS = 12
CONTEXT_SCALARS = 400
EXPLAIN_LANGUAGE = "zh"
# Two segments failing one after the other ends the run.
MAX_FAILED_IN_A_ROW = 2

SEMANTIC_OFF_NOTE = "语义疑点未开启（实验性功能，服务端默认关闭），本次只做词典术语检查。"
NO_READY_NOTE = "没有可审校的段落（见下方说明）。"
STOP_NOTES = {
    "rate_limited": "模型请求被限流",
    "llm_budget_exhausted": "模型调用额度已用完",
    "aborted": "授权状态已变化",
    "invalid_response": "模型回答不符合约定",
}

Gate = Callable[[], Awaitable[bool]]


class ReviewAborted(Exception):
    """The caller's authorization gate said stop; nothing more should be sent."""


def _shift(span: Span | None, offset: int) -> Span | None:
    if span is None:
        return None
    return Span(span.start + offset, span.end + offset, span.text)


def _tail(text: str) -> str:
    return text[-CONTEXT_SCALARS:]


async def run_review(
    backend: ReviewBackend,
    source: str,
    target: str,
    direction: str,
    *,
    gate: Gate | None = None,
    explain_language: str = EXPLAIN_LANGUAGE,
) -> ReviewReport:
    """Review one translation. Raises `ReviewAborted` when the gate refuses."""
    pairs = tuple(backend.plan_pairs(source, target))
    ready = [pair for pair in pairs if pair.status == PAIR_READY]
    report = ReviewReport(
        source=source,
        target=target,
        direction=direction,
        explain_language=explain_language,
        pairs=pairs,
        segments_total=len(ready),
    )
    if len(ready) > MAX_REVIEW_PAIRS:
        # The caller rejects over-long input before calling; guard the invariant.
        raise ValueError("too many segments for one review")

    # Deterministic track: cheap, no model, runs for every ready pair.
    findings: list[TermFinding] = []
    verified = 0
    for pair in ready:
        if gate is not None and not await gate():
            raise ReviewAborted()
        src, tgt = _pair_texts(source, target, pair)
        try:
            term = await backend.term_review(src, tgt, direction)
        except ReviewBackendError as exc:
            report.term_track_error = exc.code
            break
        verified += term.verified
        for item in term.findings:
            findings.append(
                TermFinding(
                    verdict=item.verdict,
                    source_span=_shift(item.source_span, pair.source_core_start),
                    target_span=_shift(item.target_span, pair.target_core_start),
                    candidates=item.candidates,
                )
            )
    report.term_findings = tuple(findings)
    report.term_matches_verified = verified

    if not ready:
        report.semantic_state = SEMANTIC_UNAVAILABLE
        report.semantic_note = NO_READY_NOTE
        return report
    if not backend.semantic_available():
        report.semantic_state = SEMANTIC_UNAVAILABLE
        report.semantic_note = SEMANTIC_OFF_NOTE
        return report

    issues: list[SemanticIssue] = []
    outcomes: list[SegmentOutcome] = []
    previous: PairInfo | None = None
    previous_texts: tuple[str, str] | None = None
    stopped: str | None = None
    failed_in_a_row = 0
    for pair in ready:
        if gate is not None and not await gate():
            raise ReviewAborted()
        src, tgt = _pair_texts(source, target, pair)
        context = (
            previous_texts
            if previous is not None and previous.index == pair.index - 1
            else None
        )
        try:
            body = await backend.semantic_review(
                src,
                tgt,
                direction=direction,
                explain_language=explain_language,
                context_before=context,
            )
        except ReviewBackendError as exc:
            if exc.code in STOP_CODES:
                stopped = exc.code
                break
            outcomes.append(SegmentOutcome(pair.index, "failed", exc.code))
            failed_in_a_row += 1
            if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                # The model service is down or refusing: stop, do not wait out
                # a timeout for every remaining segment.
                stopped = exc.code
                break
        else:
            try:
                got, outcome = parse_semantic_response(
                    body,
                    pair_index=pair.index,
                    source=src,
                    target=tgt,
                    source_offset=pair.source_core_start,
                    target_offset=pair.target_core_start,
                )
            except SemanticResponseError:
                LOGGER.warning("semantic review response rejected pair=%s", pair.index)
                outcomes.append(SegmentOutcome(pair.index, "failed", "invalid_response"))
                failed_in_a_row += 1
                if failed_in_a_row >= MAX_FAILED_IN_A_ROW:
                    stopped = "invalid_response"
                    break
            else:
                failed_in_a_row = 0
                issues.extend(got)
                outcomes.append(outcome)
        previous = pair
        previous_texts = (_tail(src), _tail(tgt))

    report.issues = tuple(issues)
    report.segments = tuple(outcomes)
    report.segments_reviewed = sum(1 for o in outcomes if o.status == "complete")
    if stopped is not None:
        report.semantic_state = SEMANTIC_STOPPED
        report.stopped_code = stopped
        report.semantic_note = STOP_NOTES.get(stopped, "模型服务连续失败")
    elif report.segments_reviewed < len(ready):
        report.semantic_state = SEMANTIC_PARTIAL
    else:
        report.semantic_state = SEMANTIC_DONE
    return report


def _pair_texts(source: str, target: str, pair: PairInfo) -> tuple[str, str]:
    return (
        source[pair.source_core_start : pair.source_core_end],
        target[pair.target_core_start : pair.target_core_end],
    )
