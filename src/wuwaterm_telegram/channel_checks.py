"""Zero-cost deterministic output checks for channel translations.

Reuses Core 0.16.0's ``check_segment_structure`` (structure-check-v1: numbers,
paragraphs, lines, placeholders, empty target) and adds channel-specific
critical guards (leaked internal WuwaTerm placeholders and extreme truncation
diagnostics) without spending any model tokens.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Literal

from wuwaterm.structural_checks import check_segment_structure
from wuwaterm.text_spans import WHITESPACE

from .telegram_html import strip_telegram_html


OutputCheckSeverity = Literal["ok", "warning", "critical"]
OutputCheckAction = Literal["deliver", "deliver_with_warning", "block"]

_LEAKED_INTERNAL_PLACEHOLDER_RE = re.compile(
    r"__WUWA_(?:TERM|HTML)_[0-9a-f]{16}_[0-9]{4,}__"
)

# Minimum non-whitespace source scalars before extreme-truncation heuristics apply.
_TRUNCATION_MIN_SOURCE_SCALARS = 80
_TRUNCATION_RATIO_TO_EN = 0.25
_TRUNCATION_RATIO_TO_ZH = 0.12


@dataclass(frozen=True)
class OutputCheckResult:
    """Sanitized deterministic check verdict (never carries user text)."""

    passed: bool
    severity: OutputCheckSeverity
    action: OutputCheckAction
    rule_counts: dict[str, int]
    findings_count: int
    source_paragraphs: int
    target_paragraphs: int

    def rule_summary(self) -> str:
        if not self.rule_counts:
            return "none"
        return ",".join(
            f"{rule}:{count}" for rule, count in sorted(self.rule_counts.items())
        )


def _visible_scalar_count(text: str) -> int:
    return sum(1 for ch in text if ch not in WHITESPACE)


def check_channel_translation(
    source_plain: str,
    translated_text: str,
    *,
    parse_mode: str | None = None,
    to_chinese: bool = False,
) -> OutputCheckResult:
    """Run zero-cost deterministic structural checks before channel delivery.

    Critical defects (empty output or leaked internal ``__WUWA_*__``
    placeholders) block delivery so broken internal artifacts never reach a
    public channel. Structural mismatches (numbers, paragraph/line counts,
    template placeholders, severe truncation) record sanitized warnings while
    preserving delivery.
    """
    target_plain = (
        strip_telegram_html(translated_text)
        if parse_mode == "HTML"
        else translated_text
    )
    report = check_segment_structure(source_plain, target_plain)
    counts: Counter[str] = Counter(finding.rule_id for finding in report.findings)

    # Also scan raw output (including inside HTML attributes) for leaked
    # internal WuwaTerm placeholders.
    leaked = _LEAKED_INTERNAL_PLACEHOLDER_RE.findall(translated_text)
    if leaked:
        counts["leaked_internal_placeholder"] += len(leaked)

    # Extreme truncation heuristic on substantial posts.
    source_scalars = _visible_scalar_count(source_plain)
    target_scalars = _visible_scalar_count(target_plain)
    if (
        "empty_target" not in counts
        and source_scalars >= _TRUNCATION_MIN_SOURCE_SCALARS
    ):
        threshold = (
            _TRUNCATION_RATIO_TO_ZH if to_chinese else _TRUNCATION_RATIO_TO_EN
        )
        if target_scalars < source_scalars * threshold:
            counts["severe_truncation"] += 1

    rule_counts = dict(sorted(counts.items()))
    total_findings = sum(rule_counts.values())

    if "empty_target" in counts or "leaked_internal_placeholder" in counts:
        return OutputCheckResult(
            passed=False,
            severity="critical",
            action="block",
            rule_counts=rule_counts,
            findings_count=total_findings,
            source_paragraphs=report.layout.source_paragraphs,
            target_paragraphs=report.layout.target_paragraphs,
        )
    if total_findings > 0:
        return OutputCheckResult(
            passed=True,
            severity="warning",
            action="deliver_with_warning",
            rule_counts=rule_counts,
            findings_count=total_findings,
            source_paragraphs=report.layout.source_paragraphs,
            target_paragraphs=report.layout.target_paragraphs,
        )
    return OutputCheckResult(
        passed=True,
        severity="ok",
        action="deliver",
        rule_counts={},
        findings_count=0,
        source_paragraphs=report.layout.source_paragraphs,
        target_paragraphs=report.layout.target_paragraphs,
    )
