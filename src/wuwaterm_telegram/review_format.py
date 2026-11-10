"""Telegram HTML layout of a `/review` result.

Pure functions over `ReviewReport`. Two things are kept visibly apart: the
deterministic dictionary findings and the model's semantic doubts (which may be
wrong). Every piece of dynamic text is escaped and clipped for display only;
excerpts are never treated as ranges.
"""

from __future__ import annotations

from collections.abc import Collection
from html import escape

from wuwaterm_telegram.review_model import (
    PAIR_NEEDS_MANUAL_SPLIT,
    PAIR_UNPAIRED_TARGET,
    PAIR_UNTRANSLATED,
    SEMANTIC_PARTIAL,
    SEMANTIC_STOPPED,
    SEMANTIC_UNAVAILABLE,
    VERDICT_CONFLICT,
    VERDICT_NEEDS_REVIEW,
    ReviewReport,
    SemanticIssue,
    Span,
    TermFinding,
)
from wuwaterm_telegram.telegram_html import strip_telegram_html, validate_telegram_html
from wuwaterm_telegram.telegram_text import (
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    telegram_text_units,
)

_CATEGORY_LABELS = {
    "omission": "漏译",
    "unsupported_addition": "增译",
    "meaning_shift": "意思偏移",
    "negation_change": "否定变化",
    "quantity_change": "数量",
    "reference_inconsistency": "指代不一致",
    "term_sense": "术语词义",
}
_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "较低"}
_EFFECT_HINTS = {
    "alters_official_match": (
        "此建议会改动一处官方译名匹配。如果这里是普通义（例如欢呼声 Encore），可以采纳。"
    ),
    "introduces_official_form": "此建议会引入官方译名形式。",
}

_NO_ISSUES = "未发现疑点。这只说明模型没有报告问题，不代表译文完全正确。"
_UNAVAILABLE_DEFAULT = "语义疑点未开启，本次只做词典术语检查。"

_EXCERPT_UNITS = 200
_SPAN_UNITS = 60
_TEXT_UNITS = 240
_NOTE_UNITS = 200
_MAX_TERM_LINES = 30
_MAX_NOTES_PER_KIND = 10
_MESSAGE_RESERVE = 16  # room for the "(i/n)" prefix of a multi-message result
_ELLIPSIS = "…"


def category_label(category: str) -> str:
    return _CATEGORY_LABELS.get(category, category)


def _confidence_label(confidence: str) -> str:
    return _CONFIDENCE_LABELS.get(confidence, confidence)


def _clip(text: str, units: int) -> str:
    """Escape `text` and cut the raw text so the escaped form fits `units`.

    Newlines become spaces. The cut only affects what is displayed.
    """
    flat = " ".join(text.split())
    escaped = escape(flat, quote=False)
    if telegram_text_units(escaped) <= units:
        return escaped
    budget = units - 1  # the ellipsis is one unit
    out: list[str] = []
    used = 0
    for char in flat:
        piece = escape(char, quote=False)
        size = telegram_text_units(piece)
        if used + size > budget:
            break
        out.append(piece)
        used += size
    return "".join(out) + _ELLIPSIS


def _excerpt(excerpt: str | None, span: Span | None, units: int = _EXCERPT_UNITS) -> str:
    """Display text for one side: the excerpt, else a clipped span text."""
    if excerpt and excerpt.strip():
        return _clip(excerpt, units)
    if span is not None and span.text.strip():
        return _clip(span.text, min(units, _SPAN_UNITS))
    return ""


def _quote(text: str) -> str:
    return f"「{text}」"


def _issue_fragments(issue: SemanticIssue) -> str:
    parts = []
    source = _excerpt(issue.source_excerpt, issue.source_span)
    target = _excerpt(issue.target_excerpt, issue.target_span)
    if source:
        parts.append(f"原文{_quote(source)}")
    if target:
        parts.append(f"译文{_quote(target)}")
    return " ".join(parts)


def _suggestion_lines(issue: SemanticIssue) -> list[str]:
    suggestion = issue.suggestion
    if suggestion is None:
        if issue.suggestion_withheld:
            return ["范围较长，请人工调整"]
        return []
    if issue.target_span is not None and suggestion.target_span == issue.target_span:
        old = _excerpt(issue.target_excerpt, suggestion.target_span)
    else:
        old = _clip(suggestion.target_span.text, _SPAN_UNITS)
    new = _clip(suggestion.replacement, _TEXT_UNITS)
    lines = [f"建议：把{_quote(old)}改为{_quote(new)}"]
    hint = _EFFECT_HINTS.get(suggestion.term_effect)
    if hint:
        lines.append(hint)
    if issue.alternatives:
        alt = issue.alternatives[0]
        lines.append(f"另一种改法：{_quote(_clip(alt.replacement, _TEXT_UNITS))}")
        alt_hint = _EFFECT_HINTS.get(alt.term_effect)
        if alt_hint:
            lines.append(alt_hint)
    return lines


def _issue_title(number: int, issue: SemanticIssue) -> str:
    label = escape(category_label(issue.category), quote=False)
    if issue.confidence == "low":
        confidence = "把握较低，请自行判断"
    else:
        confidence = f"把握{escape(_confidence_label(issue.confidence), quote=False)}"
    title = f"<b>{number}. {label}</b> · {confidence}"
    fragments = _issue_fragments(issue)
    return f"{title} · {fragments}" if fragments else title


def _issue_block(number: int, issue: SemanticIssue) -> str:
    inner = [_clip(issue.rationale, _TEXT_UNITS)] if issue.rationale.strip() else []
    inner.extend(_suggestion_lines(issue))
    block = _issue_title(number, issue)
    if inner:
        block += "\n<blockquote expandable>" + "\n".join(inner) + "</blockquote>"
    return block


def _term_line(finding: TermFinding) -> str:
    source = _quote(_clip(finding.source_span.text, _SPAN_UNITS))
    target = ""
    if finding.target_span is not None and finding.target_span.text.strip():
        target = f" 译文为{_quote(_clip(finding.target_span.text, _SPAN_UNITS))}"
    if finding.verdict == VERDICT_CONFLICT:
        forms = "；".join(
            f"{_clip(zh, _SPAN_UNITS)} / {_clip(en, _SPAN_UNITS)}"
            for zh, en in finding.candidates[:2]
        )
        official = f"，官方：{forms}" if forms else ""
        return f"· 原文{source}与官方译名不一致{official}{target}"
    return f"· 原文{source}需核对{target}"


def _count_confidence(issues: tuple[SemanticIssue, ...]) -> tuple[int, int, int]:
    return (
        sum(1 for i in issues if i.confidence == "high"),
        sum(1 for i in issues if i.confidence == "medium"),
        sum(1 for i in issues if i.confidence == "low"),
    )


def _term_counts(report: ReviewReport) -> int:
    return len(report.conflicts) + len(report.needs_review)


def _failed_segments(report: ReviewReport) -> int:
    return sum(1 for s in report.segments if s.status in ("not_reviewed", "failed"))


def _direction_text(report: ReviewReport) -> str:
    return "英文译文" if report.direction == "en" else "中文译文"


def _segment_total(report: ReviewReport) -> int:
    return len(report.pairs) or report.segments_total


def summary_line(report: ReviewReport) -> str:
    high, medium, low = _count_confidence(report.issues)
    parts = [f"{_direction_text(report)}，共 {_segment_total(report)} 段"]
    if report.semantic_state in (SEMANTIC_STOPPED, SEMANTIC_PARTIAL):
        parts.append(f"已审 {report.segments_reviewed}/{report.segments_total} 段")
    parts.append(f"术语发现 {_term_counts(report)} 处")
    if report.semantic_state == SEMANTIC_UNAVAILABLE:
        parts.append("语义疑点未运行")
    else:
        parts.append(
            f"语义疑点 {len(report.issues)} 条（高 {high} / 中 {medium} / 较低 {low}）"
        )
    return "；".join(parts)


def _overview_block(report: ReviewReport) -> str:
    high, medium, low = _count_confidence(report.issues)
    lines = [
        "<b>审校结果</b>",
        f"译文方向：{_direction_text(report)}；共 {_segment_total(report)} 段",
    ]
    if report.semantic_state in (SEMANTIC_STOPPED, SEMANTIC_PARTIAL):
        lines.append(f"已审 {report.segments_reviewed}/{report.segments_total} 段")
    lines.append(f"术语发现 {_term_counts(report)} 处")
    if report.semantic_state == SEMANTIC_UNAVAILABLE:
        lines.append("语义疑点：未运行")
    else:
        lines.append(
            f"语义疑点 {len(report.issues)} 条（高 {high} / 中 {medium} / 较低 {low}）"
        )
    return "\n".join(lines)


def _term_blocks(report: ReviewReport) -> list[str]:
    title = "<b>术语（词典，确定性）</b>"
    shown = [
        f
        for f in report.term_findings
        if f.verdict in (VERDICT_CONFLICT, VERDICT_NEEDS_REVIEW)
    ]
    lines = [_term_line(f) for f in shown[:_MAX_TERM_LINES]]
    if len(shown) > _MAX_TERM_LINES:
        lines.append(f"· 另有 {len(shown) - _MAX_TERM_LINES} 处未显示")
    tail: list[str] = []
    if report.term_track_error:
        tail.append(f"术语检查未完成（{_clip(report.term_track_error, _NOTE_UNITS)}）")
    elif report.segments_total == 0:
        tail.append("未检查：没有可审校的段落。")
    elif not shown:
        tail.append("词典没有发现需要处理的术语问题。")
    tail.append(
        f"词面匹配 {report.term_matches_verified} 处"
        "（仅表示出现了官方形式，不等于词义确认）"
    )
    blocks = lines + tail
    blocks[0] = f"{title}\n{blocks[0]}"
    return blocks


def _semantic_blocks(report: ReviewReport) -> list[str]:
    title = "<b>语义疑点（实验性，可能有误）</b>"
    lines: list[str] = []
    if report.semantic_state == SEMANTIC_STOPPED:
        note = _clip(report.semantic_note or "未说明", _NOTE_UNITS)
        lines.append(
            f"已完成 {report.segments_reviewed}/{report.segments_total} 段，"
            f"其余未审（原因：{note}）。"
            "可稍后重新提交或使用 /review last 查看本结果"
        )
    elif report.semantic_state == SEMANTIC_PARTIAL:
        lines.append(f"有 {_failed_segments(report)} 段未能审校，其余已审。")
    blocks = list(lines)
    if report.issues:
        blocks.extend(_issue_block(n, i) for n, i in enumerate(report.issues, 1))
    elif not (
        report.segments_reviewed == 0
        and report.semantic_state in (SEMANTIC_STOPPED, SEMANTIC_PARTIAL)
    ):
        # Nothing was reviewed: no statement about what the model did not find.
        blocks.append(_NO_ISSUES)
    blocks[0] = f"{title}\n{blocks[0]}"
    return blocks


def _note_lines(report: ReviewReport) -> list[str]:
    lines: list[str] = []
    if report.semantic_state == SEMANTIC_UNAVAILABLE:
        reason = _clip(report.semantic_note or _UNAVAILABLE_DEFAULT, _NOTE_UNITS)
        lines.append(reason)

    def capped(items: list[str], more: int) -> None:
        lines.extend(items[:_MAX_NOTES_PER_KIND])
        if more > 0:
            lines.append(f"另有 {more} 段同类情况未逐条列出")

    manual = [
        f"第 {p.index + 1} 段原文 {p.source_len} 字、译文 {p.target_len} 字，"
        "无法可靠配对，未审校。请在原文和译文的对应位置各插入空行后重新提交。"
        for p in report.pairs
        if p.status == PAIR_NEEDS_MANUAL_SPLIT
    ]
    capped(manual, len(manual) - _MAX_NOTES_PER_KIND)
    empty = [
        f"第 {p.index + 1} 段译文为空，未审校"
        for p in report.pairs
        if p.status == PAIR_UNTRANSLATED
    ]
    capped(empty, len(empty) - _MAX_NOTES_PER_KIND)
    extra = [
        f"译文多出一段（可能为增译），未审校（第 {p.index + 1} 段）"
        for p in report.pairs
        if p.status == PAIR_UNPAIRED_TARGET
    ]
    capped(extra, len(extra) - _MAX_NOTES_PER_KIND)
    cut = [f"第 {p.index + 1} 段是强制切分，边界附近可能误判" for p in report.hard_cut_pairs]
    capped(cut, len(cut) - _MAX_NOTES_PER_KIND)
    dropped = sum(s.dropped_unanchored for s in report.segments)
    if dropped > 0:
        lines.append(f"另有 {dropped} 条疑点因无法定位被丢弃")
    if report.omitted_issues > 0:
        lines.append(f"另有 {report.omitted_issues} 条因篇幅未显示")
    return lines


def _note_blocks(report: ReviewReport) -> list[str]:
    lines = _note_lines(report)
    if not lines:
        return []
    lines[0] = f"<b>说明</b>\n{lines[0]}"
    return lines


def _pack(blocks: list[str], limit: int) -> list[str]:
    messages: list[str] = []
    current = ""
    for block in blocks:
        if telegram_text_units(block) > limit:
            block = _fallback_block(block, limit)
        joined = f"{current}\n\n{block}" if current else block
        if current and telegram_text_units(joined) > limit:
            messages.append(current)
            current = block
        else:
            current = joined
    if current:
        messages.append(current)
    return messages


def _fallback_block(block: str, limit: int) -> str:
    """Unreachable for bounded input; never emit an over-long or broken block."""
    plain = strip_telegram_html(block)
    return _clip(plain, limit)


def format_report(report: ReviewReport) -> list[str]:
    blocks = [_overview_block(report)]
    blocks.extend(_term_blocks(report))
    if report.semantic_state != SEMANTIC_UNAVAILABLE:
        blocks.extend(_semantic_blocks(report))
    blocks.extend(_note_blocks(report))
    messages = _pack(blocks, TELEGRAM_TEXT_MESSAGE_LIMIT - _MESSAGE_RESERVE)
    if len(messages) > 1:
        total = len(messages)
        messages = [f"({i}/{total})\n{m}" for i, m in enumerate(messages, 1)]
    return [m if validate_telegram_html(m) else _plain(m) for m in messages]


def _plain(message: str) -> str:
    """Last resort if a message fails validation: escaped plain text."""
    return _clip(strip_telegram_html(message), TELEGRAM_TEXT_MESSAGE_LIMIT)


def format_issue_card(
    report: ReviewReport,
    number: int,
    *,
    accepted: Collection[str] = (),
    skipped: Collection[str] = (),
) -> str:
    if not 1 <= number <= len(report.issues):
        raise IndexError(f"issue number {number} is out of range")
    issue = report.issues[number - 1]
    if issue.key in accepted:
        state = "已采纳 ✅"
    elif issue.key in skipped:
        state = "已跳过"
    else:
        state = "待处理"
    lines = [
        f"<b>疑点 {number}/{len(report.issues)}</b>（模型，可能有误）",
        _issue_title(number, issue),
    ]
    if issue.rationale.strip():
        lines.append("理由：" + _clip(issue.rationale, _TEXT_UNITS))
    lines.extend(_suggestion_lines(issue))
    lines.append(f"状态：{state}")
    card = "\n".join(lines)
    if not validate_telegram_html(card) or telegram_text_units(card) > TELEGRAM_TEXT_MESSAGE_LIMIT:
        return _plain(card)
    return card
