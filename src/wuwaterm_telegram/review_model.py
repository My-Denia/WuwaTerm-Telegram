"""Value types for `/review` and the strict reader of a semantic-review-v1 body.

Everything here is pure: no Core import, no Telegram import. The shape of a
segment response follows the published `semantic-review-v1` contract (Core ADR
0018, `contracts/semantic-review-v1/`). A body that does not meet the contract
is not trusted: `parse_semantic_response` raises `SemanticResponseError`, and
the caller reports that segment as not reviewed instead of showing guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

REVIEW_VERSION = "semantic-review-v1"

CATEGORIES = (
    "omission",
    "unsupported_addition",
    "meaning_shift",
    "negation_change",
    "quantity_change",
    "reference_inconsistency",
    "term_sense",
)
CONFIDENCES = ("high", "medium", "low")
TERM_EFFECTS = (
    "none",
    "alters_official_match",
    "introduces_official_form",
    "not_checked",
)
WITHHELD_REASONS = ("span_too_long", "replacement_too_long")
MAX_ISSUES_PER_SEGMENT = 16

# Pair statuses. "ready" is sent to the model; the others are only reported.
PAIR_READY = "ready"
PAIR_NEEDS_MANUAL_SPLIT = "needs_manual_split"
PAIR_UNTRANSLATED = "untranslated"
PAIR_UNPAIRED_TARGET = "unpaired_target"

# Why the semantic track did not (fully) run.
SEMANTIC_DONE = "done"
SEMANTIC_UNAVAILABLE = "unavailable"  # Core without the feature, or no model
SEMANTIC_STOPPED = "stopped"  # rate limit, budget or authorization change
SEMANTIC_PARTIAL = "partial"  # ran, but some segments could not be reviewed

# Term-finding verdicts, as published by review-v2.
VERDICT_VERIFIED = "verified_constraint"
VERDICT_CONFLICT = "confirmed_conflict"
VERDICT_NEEDS_REVIEW = "needs_review"
VERDICT_NOT_EVALUATED = "not_evaluated"


class SemanticResponseError(ValueError):
    """A segment response that does not meet the semantic-review-v1 contract."""


@dataclass(frozen=True)
class Span:
    """A half-open range in Unicode scalars of the whole submitted side."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Suggestion:
    target_span: Span
    replacement: str
    term_effect: str


@dataclass(frozen=True)
class Alternative:
    replacement: str
    term_effect: str


@dataclass(frozen=True)
class SemanticIssue:
    """One model finding. Coordinates are already in whole-text scalars."""

    key: str  # unique within a report: "<pair index>.<issue id>"
    pair_index: int
    category: str
    confidence: str
    source_span: Span | None
    target_span: Span | None
    source_excerpt: str | None
    target_excerpt: str | None
    rationale: str
    suggestion: Suggestion | None
    suggestion_withheld: str | None
    alternatives: tuple[Alternative, ...] = ()


@dataclass(frozen=True)
class SegmentOutcome:
    """What happened to one paired segment on the semantic track."""

    pair_index: int
    status: str  # "complete" | "not_reviewed" | "failed"
    reason: str | None = None
    dropped_unanchored: int = 0
    omitted_issues: int = 0
    truncated_reason: str | None = None


@dataclass(frozen=True)
class TermFinding:
    """A deterministic dictionary finding (surface match, not sense proof)."""

    verdict: str
    source_span: Span
    target_span: Span | None
    candidates: tuple[tuple[str, str], ...] = ()  # (zh, en)


@dataclass(frozen=True)
class PairInfo:
    """One pair from segmentation-v1. Regions carry their core range."""

    index: int
    status: str
    hard_cut: bool
    source_start: int
    source_end: int
    source_core_start: int
    source_core_end: int
    target_start: int
    target_end: int
    target_core_start: int
    target_core_end: int
    limit: int

    @property
    def source_len(self) -> int:
        return self.source_core_end - self.source_core_start

    @property
    def target_len(self) -> int:
        return self.target_core_end - self.target_core_start


@dataclass
class ReviewReport:
    """The result of one `/review`; the formatter and the revision read it."""

    source: str
    target: str
    direction: str  # target language of the translation: "en" or "zh"
    explain_language: str  # "zh" or "en"
    pairs: tuple[PairInfo, ...] = ()
    term_findings: tuple[TermFinding, ...] = ()
    term_matches_verified: int = 0
    term_track_error: str | None = None
    issues: tuple[SemanticIssue, ...] = ()
    segments: tuple[SegmentOutcome, ...] = ()
    semantic_state: str = SEMANTIC_UNAVAILABLE
    semantic_note: str | None = None  # why unavailable / stopped, user wording
    segments_reviewed: int = 0
    segments_total: int = 0  # ready pairs
    stopped_code: str | None = None  # rate_limited | llm_budget_exhausted | ...

    @property
    def manual_split_pairs(self) -> tuple[PairInfo, ...]:
        return tuple(p for p in self.pairs if p.status == PAIR_NEEDS_MANUAL_SPLIT)

    @property
    def untranslated_pairs(self) -> tuple[PairInfo, ...]:
        return tuple(p for p in self.pairs if p.status == PAIR_UNTRANSLATED)

    @property
    def unpaired_target_pairs(self) -> tuple[PairInfo, ...]:
        return tuple(p for p in self.pairs if p.status == PAIR_UNPAIRED_TARGET)

    @property
    def hard_cut_pairs(self) -> tuple[PairInfo, ...]:
        return tuple(p for p in self.pairs if p.hard_cut and p.status == PAIR_READY)

    @property
    def conflicts(self) -> tuple[TermFinding, ...]:
        return tuple(f for f in self.term_findings if f.verdict == VERDICT_CONFLICT)

    @property
    def needs_review(self) -> tuple[TermFinding, ...]:
        return tuple(f for f in self.term_findings if f.verdict == VERDICT_NEEDS_REVIEW)

    @property
    def omitted_issues(self) -> int:
        return sum(s.omitted_issues for s in self.segments)


# --- reading a segment response -------------------------------------------------


def _fail(message: str) -> SemanticResponseError:
    return SemanticResponseError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _fail(message)


def _scalars(text: str) -> int:
    return len(text)  # Python str indexes scalars


def _read_span(
    raw: Any,
    side_text: str,
    offset: int,
    name: str,
) -> Span | None:
    if raw is None:
        return None
    _require(isinstance(raw, Mapping), f"{name} must be an object or null")
    start, end, text = raw.get("start"), raw.get("end"), raw.get("text")
    _require(
        isinstance(start, int) and isinstance(end, int) and isinstance(text, str),
        f"{name} has the wrong field types",
    )
    _require(not isinstance(start, bool) and not isinstance(end, bool), f"{name} bad type")
    _require(0 <= start < end <= _scalars(side_text), f"{name} is out of range")
    # The contract: text is the complete slice of the submitted side. Anything
    # else could be replaced or highlighted at the wrong place.
    _require(side_text[start:end] == text, f"{name} text is not the submitted slice")
    return Span(start + offset, end + offset, text)


def _read_term_effect(value: Any, name: str) -> str:
    _require(value in TERM_EFFECTS, f"{name} has an unknown term_effect")
    return value


def parse_semantic_response(
    body: Any,
    *,
    pair_index: int,
    source: str,
    target: str,
    source_offset: int,
    target_offset: int,
) -> tuple[list[SemanticIssue], SegmentOutcome]:
    """Read one segment response. `source`/`target` are the exact texts sent.

    Spans come back in segment coordinates; the offsets move them to the whole
    text. Raises `SemanticResponseError` for any contract violation.
    """
    _require(isinstance(body, Mapping), "response is not an object")
    _require(body.get("review_version") == REVIEW_VERSION, "unexpected review_version")
    status = body.get("status")
    _require(status in ("complete", "not_reviewed"), "unexpected status")
    dropped = body.get("dropped_unanchored", 0)
    omitted = body.get("omitted_issues", 0)
    _require(
        isinstance(dropped, int) and not isinstance(dropped, bool) and dropped >= 0,
        "bad dropped_unanchored",
    )
    _require(
        isinstance(omitted, int) and not isinstance(omitted, bool) and omitted >= 0,
        "bad omitted_issues",
    )
    truncated_reason = body.get("truncated_reason")
    _require(truncated_reason in (None, "issue_limit", "byte_limit"), "bad truncated_reason")
    raw_issues = body.get("issues")
    _require(isinstance(raw_issues, list), "issues is not a list")
    _require(len(raw_issues) <= MAX_ISSUES_PER_SEGMENT, "too many issues")

    if status == "not_reviewed":
        _require(not raw_issues, "a not_reviewed response carries issues")
        reason = body.get("status_reason")
        _require(isinstance(reason, str) or reason is None, "bad status_reason")
        return [], SegmentOutcome(pair_index, "not_reviewed", reason or "not_reviewed")

    issues: list[SemanticIssue] = []
    seen: set[str] = set()
    for position, raw in enumerate(raw_issues):
        label = f"issues[{position}]"
        _require(isinstance(raw, Mapping), f"{label} is not an object")
        issue_id = raw.get("id")
        _require(isinstance(issue_id, str) and issue_id != "", f"{label}.id")
        _require(issue_id not in seen, f"{label}.id repeats")
        seen.add(issue_id)
        _require(raw.get("origin") == "model", f"{label}.origin")
        category = raw.get("category")
        _require(category in CATEGORIES, f"{label}.category")
        confidence = raw.get("confidence")
        _require(confidence in CONFIDENCES, f"{label}.confidence")
        source_span = _read_span(raw.get("source_span"), source, source_offset, f"{label}.source_span")
        target_span = _read_span(raw.get("target_span"), target, target_offset, f"{label}.target_span")
        _require(
            source_span is not None or target_span is not None,
            f"{label} has no span",
        )
        rationale = raw.get("rationale")
        _require(isinstance(rationale, str), f"{label}.rationale")
        withheld = raw.get("suggestion_withheld")
        _require(withheld in (None, *WITHHELD_REASONS), f"{label}.suggestion_withheld")
        suggestion: Suggestion | None = None
        raw_suggestion = raw.get("suggestion")
        if raw_suggestion is not None:
            _require(isinstance(raw_suggestion, Mapping), f"{label}.suggestion")
            suggestion_span = _read_span(
                raw_suggestion.get("target_span"),
                target,
                target_offset,
                f"{label}.suggestion.target_span",
            )
            _require(suggestion_span is not None, f"{label}.suggestion.target_span")
            replacement = raw_suggestion.get("replacement")
            _require(isinstance(replacement, str), f"{label}.suggestion.replacement")
            suggestion = Suggestion(
                suggestion_span,
                replacement,
                _read_term_effect(raw_suggestion.get("term_effect"), f"{label}.suggestion"),
            )
            # One unit: a suggestion and a withheld reason never coexist.
            _require(withheld is None, f"{label} has both a suggestion and a withheld reason")
        raw_alternatives = raw.get("alternatives", [])
        _require(isinstance(raw_alternatives, list), f"{label}.alternatives")
        alternatives: list[Alternative] = []
        for alt in raw_alternatives:
            _require(isinstance(alt, Mapping), f"{label}.alternatives item")
            replacement = alt.get("replacement")
            _require(isinstance(replacement, str), f"{label}.alternatives.replacement")
            alternatives.append(
                Alternative(replacement, _read_term_effect(alt.get("term_effect"), f"{label}.alternatives"))
            )
        if suggestion is None:
            # Alternatives belong to a suggestion; without one they are unusable.
            alternatives = []
        source_excerpt = raw.get("source_excerpt")
        target_excerpt = raw.get("target_excerpt")
        _require(source_excerpt is None or isinstance(source_excerpt, str), f"{label}.source_excerpt")
        _require(target_excerpt is None or isinstance(target_excerpt, str), f"{label}.target_excerpt")
        issues.append(
            SemanticIssue(
                key=f"{pair_index}.{issue_id}",
                pair_index=pair_index,
                category=category,
                confidence=confidence,
                source_span=source_span,
                target_span=target_span,
                source_excerpt=source_excerpt,
                target_excerpt=target_excerpt,
                rationale=rationale,
                suggestion=suggestion,
                suggestion_withheld=withheld,
                alternatives=tuple(alternatives),
            )
        )
    outcome = SegmentOutcome(
        pair_index,
        "complete",
        dropped_unanchored=dropped,
        omitted_issues=omitted,
        truncated_reason=truncated_reason,
    )
    return issues, outcome
