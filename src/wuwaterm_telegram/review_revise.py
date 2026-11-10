"""Apply accepted `/review` suggestions to the translation. Pure logic.

Every edit carries the text it expects to replace. A range that was shortened
or went stale never silently produces a mixed result: the edit is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from wuwaterm_telegram.review_model import ReviewReport, SemanticIssue


class RevisionError(ValueError):
    """The accepted edits cannot be applied to the translation as it is."""


@dataclass(frozen=True)
class Edit:
    key: str  # SemanticIssue.key
    start: int  # scalar offsets in the whole translation, half-open
    end: int
    expected: str  # must equal target[start:end]
    replacement: str


def choice_replacement(issue: SemanticIssue, choice: int) -> tuple[str, str] | None:
    """(replacement, term_effect) for choice 0 (main) or n (alternatives[n-1])."""
    suggestion = issue.suggestion
    if suggestion is None or isinstance(choice, bool) or not isinstance(choice, int):
        return None
    if choice == 0:
        return suggestion.replacement, suggestion.term_effect
    if 1 <= choice <= len(issue.alternatives):
        alt = issue.alternatives[choice - 1]
        return alt.replacement, alt.term_effect
    return None


def _find(report: ReviewReport, key: str) -> SemanticIssue | None:
    for issue in report.issues:
        if issue.key == key:
            return issue
    return None


def check_accept(
    report: ReviewReport,
    accepted: Mapping[str, int],
    key: str,
    choice: int = 0,
) -> str | None:
    """None when `key` may be accepted with `choice`, otherwise a reason code."""
    issue = _find(report, key)
    if issue is None:
        return "unknown_issue"
    if issue.suggestion is None:
        return "no_suggestion"
    if choice_replacement(issue, choice) is None:
        return "bad_choice"
    span = issue.suggestion.target_span
    if report.target[span.start : span.end] != span.text:
        return "stale"
    for other_key in accepted:
        if other_key == key:
            continue
        other = _find(report, other_key)
        if other is None or other.suggestion is None:
            continue
        o = other.suggestion.target_span
        if span.start < o.end and o.start < span.end:
            return f"overlaps:{other_key}"
    return None


def build_edits(report: ReviewReport, accepted: Mapping[str, int]) -> list[Edit]:
    edits: list[Edit] = []
    for key, choice in accepted.items():
        issue = _find(report, key)
        if issue is None or issue.suggestion is None:
            raise RevisionError(f"{key}: no suggestion to apply")
        chosen = choice_replacement(issue, choice)
        if chosen is None:
            raise RevisionError(f"{key}: no such choice")
        span = issue.suggestion.target_span
        edits.append(Edit(key, span.start, span.end, span.text, chosen[0]))
    edits.sort(key=lambda e: (e.start, e.end))
    return edits


def apply_edits(target: str, edits: Sequence[Edit]) -> str:
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    previous: Edit | None = None
    for edit in ordered:
        if not 0 <= edit.start < edit.end <= len(target):
            raise RevisionError(f"{edit.key}: range is outside the translation")
        if target[edit.start : edit.end] != edit.expected:
            raise RevisionError(f"{edit.key}: the translation no longer matches the range")
        if previous is not None and edit.start < previous.end:
            raise RevisionError(f"{edit.key}: overlaps {previous.key}")
        previous = edit
    result = target
    for edit in reversed(ordered):
        result = result[: edit.start] + edit.replacement + result[edit.end :]
    return result


def revise(report: ReviewReport, accepted: Mapping[str, int]) -> str:
    return apply_edits(report.target, build_edits(report, accepted))
