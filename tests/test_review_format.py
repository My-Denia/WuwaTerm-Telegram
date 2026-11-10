from __future__ import annotations

import pytest

from wuwaterm_telegram.review_format import (
    category_label,
    format_issue_card,
    format_report,
    summary_line,
)
from wuwaterm_telegram.review_model import (
    CATEGORIES,
    Alternative,
    PairInfo,
    ReviewReport,
    SegmentOutcome,
    SemanticIssue,
    Span,
    Suggestion,
    TermFinding,
)
from wuwaterm_telegram.telegram_html import strip_telegram_html, validate_telegram_html
from wuwaterm_telegram.telegram_text import telegram_text_units


def issue(n=1, **kw):
    span = Span(0, 4, "abcd")
    base = dict(
        key=f"0.i{n}",
        pair_index=0,
        category="meaning_shift",
        confidence="medium",
        source_span=span,
        target_span=span,
        source_excerpt="source bit",
        target_excerpt="target bit",
        rationale="because reasons",
        suggestion=None,
        suggestion_withheld=None,
    )
    base.update(kw)
    return SemanticIssue(**base)


def suggestion(effect="none", replacement="new text"):
    return Suggestion(Span(0, 4, "abcd"), replacement, effect)


def pair(index=0, status="ready", hard_cut=False, src=(0, 10), tgt=(0, 20)):
    return PairInfo(index, status, hard_cut, 0, 10, *src, 0, 20, *tgt, 100)


def report(**kw):
    base = dict(
        source="s",
        target="t",
        direction="en",
        explain_language="zh",
        semantic_state="done",
        segments_reviewed=1,
        segments_total=1,
    )
    base.update(kw)
    return ReviewReport(**base)


def check_all(messages):
    assert messages
    for m in messages:
        assert telegram_text_units(m) <= 4096
        assert validate_telegram_html(m)


def test_many_issues_split_between_items_and_stay_valid():
    issues = tuple(issue(n, rationale="长" * 240, source_excerpt="é" * 200) for n in range(1, 41))
    messages = format_report(report(issues=issues))
    check_all(messages)
    assert len(messages) > 1
    text = "\n".join(strip_telegram_html(m) for m in messages)
    for n in range(1, 41):
        assert f"{n}. 意思偏移" in text


def test_special_characters_are_escaped_everywhere():
    nasty = '<script>&"x'
    r = report(
        issues=(
            issue(
                source_excerpt=nasty,
                target_excerpt=nasty,
                rationale=nasty,
                suggestion=suggestion(replacement=nasty),
                alternatives=(Alternative(nasty, "none"),),
            ),
        ),
        term_findings=(
            TermFinding("needs_review", Span(0, 3, nasty), Span(0, 3, nasty)),
        ),
        semantic_note=None,
    )
    messages = format_report(r)
    check_all(messages)
    joined = "".join(messages)
    assert "<script>" not in joined
    assert "&lt;script&gt;&amp;" in joined
    check_all([format_issue_card(r, 1)])


def test_utf16_counting_with_emoji_and_overlong_text():
    r = report(issues=(issue(rationale="😀" * 5000, source_excerpt="中" * 5000, target_excerpt="&" * 5000),))
    check_all(format_report(r))
    card = format_issue_card(r, 1)
    check_all([card])
    assert "…" in card


def test_sections_are_separate_and_worded_by_nature():
    r = report(
        term_findings=(
            TermFinding("confirmed_conflict", Span(0, 2, "今汐"), Span(0, 5, "Jinhsi"),
                        (("今汐", "Jinhsi"), ("a", "b"), ("c", "d"))),
            TermFinding("needs_review", Span(0, 2, "忌炎"), None),
        ),
        term_matches_verified=7,
        issues=(issue(),),
    )
    text = strip_telegram_html("\n\n".join(format_report(r)))
    terms, semantic = text.index("术语（词典，确定性）"), text.index("语义疑点（模型，可能有误）")
    assert terms < semantic
    term_part = text[terms:semantic]
    assert "与官方译名不一致" in term_part and "Jinhsi" in term_part and "需核对" in term_part
    assert "c / d" not in term_part  # at most two forms
    assert "词面匹配 7 处（仅表示出现了官方形式，不等于词义确认）" in term_part
    assert "because reasons" not in term_part


def test_term_track_error_is_stated():
    text = strip_telegram_html(format_report(report(term_track_error="boom"))[0])
    assert "术语检查未完成" in text


def test_no_issues_wording_and_banned_words():
    text = strip_telegram_html("\n".join(format_report(report())))
    assert "未发现疑点。这只说明模型没有报告问题，不代表译文完全正确。" in text
    for banned in ("通过", "无误", "全部正确"):
        assert banned not in text


def test_issue_title_confidence_and_blockquote():
    r = report(issues=(issue(confidence="low", category="quantity_change"),))
    message = format_report(r)[0]
    assert "<b>1. 数量</b>" in message
    assert "把握较低，请自行判断" in message
    assert "原文「source bit」" in message and "译文「target bit」" in message
    start = message.index("<blockquote expandable>")
    assert "because reasons" in message[start : message.index("</blockquote>")]


def test_suggestion_and_term_effect_hints():
    effect = "此建议会改动一处官方译名匹配。如果这里是普通义（例如欢呼声 Encore），可以采纳。"
    r = report(
        issues=(
            issue(1, suggestion=suggestion("alters_official_match"),
                  alternatives=(Alternative("alt a", "none"), Alternative("alt b", "none"))),
            issue(2, suggestion=suggestion("introduces_official_form")),
            issue(3, suggestion=suggestion("not_checked")),
        )
    )
    text = strip_telegram_html("\n".join(format_report(r)))
    assert "建议：把「target bit」改为「new text」" in text
    assert effect in text
    assert "此建议会引入官方译名形式。" in text
    assert text.count("此建议会") == 2
    assert "另一种改法：「alt a」" in text and "alt b" not in text


def test_withheld_and_bare_issue_do_not_show_replacement():
    r = report(
        issues=(
            issue(1, suggestion_withheld="span_too_long"),
            issue(2),
        )
    )
    text = strip_telegram_html("\n".join(format_report(r)))
    assert "范围较长，请人工调整" in text
    assert "建议：" not in text and "new text" not in text


def test_excerpt_falls_back_to_truncated_span_text():
    long_text = "x" * 500
    r = report(
        issues=(
            issue(source_excerpt=None, target_excerpt="",
                  source_span=Span(0, 500, long_text), target_span=Span(0, 500, long_text)),
        )
    )
    message = format_report(r)[0]
    assert long_text not in message and "…" in message


def test_unavailable_stopped_and_partial_states():
    unavailable = strip_telegram_html("\n".join(format_report(report(semantic_state="unavailable"))))
    assert "本次未运行语义审校" in unavailable
    assert "语义疑点（模型" not in unavailable
    with_note = strip_telegram_html(
        "\n".join(format_report(report(semantic_state="unavailable", semantic_note="no <model>")))
    )
    assert "no <model>" in with_note

    stopped = strip_telegram_html(
        "\n".join(
            format_report(
                report(semantic_state="stopped", semantic_note="额度用尽",
                       segments_reviewed=2, segments_total=5)
            )
        )
    )
    assert "已审 2/5 段" in stopped
    assert "已完成 2/5 段，其余未审（原因：额度用尽）。可稍后重新提交或使用 /review last 查看本结果" in stopped

    partial = strip_telegram_html(
        "\n".join(
            format_report(
                report(
                    semantic_state="partial",
                    segments=(
                        SegmentOutcome(0, "complete"),
                        SegmentOutcome(1, "failed"),
                        SegmentOutcome(2, "not_reviewed", "x"),
                    ),
                    segments_reviewed=1,
                    segments_total=3,
                )
            )
        )
    )
    assert "有 2 段未能审校" in partial


def test_pair_notes_in_one_notes_block():
    r = report(
        pairs=(
            pair(0, "needs_manual_split", src=(0, 1234), tgt=(0, 567)),
            pair(1, "untranslated"),
            pair(2, "unpaired_target"),
            pair(3, "ready", hard_cut=True),
        ),
        segments=(SegmentOutcome(3, "complete", dropped_unanchored=2, omitted_issues=3),),
    )
    messages = format_report(r)
    assert "".join(messages).count("<b>说明</b>") == 1
    text = strip_telegram_html("\n".join(messages))
    notes = text[text.index("说明\n") :]
    assert "第 1 段原文 1234 字、译文 567 字，无法可靠配对，未审校。请在原文和译文的对应位置各插入空行后重新提交。" in notes
    assert "第 2 段译文为空，未审校" in notes
    assert "译文多出一段（可能为增译），未审校" in notes
    assert "第 4 段是强制切分，边界附近可能误判" in notes
    assert "另有 2 条疑点因无法定位被丢弃" in notes
    assert "另有 3 条因篇幅未显示" in notes


def test_issue_card_status_and_bounds():
    r = report(issues=(issue(1, rationale="why <b>"), issue(2)))
    assert "状态：待处理" in format_issue_card(r, 1)
    assert "状态：已采纳 ✅" in format_issue_card(r, 1, accepted={"0.i1"})
    assert "状态：已跳过" in format_issue_card(r, 2, skipped=["0.i2"])
    card = format_issue_card(r, 1)
    assert "理由：why &lt;b&gt;" in card and "<blockquote" not in card
    check_all([card])
    for bad in (0, 3, -1):
        with pytest.raises(IndexError):
            format_issue_card(r, bad)


def test_labels_and_summary_line():
    assert all(category_label(c) != c for c in CATEGORIES)
    assert category_label("quantity_change") == "数量"
    assert category_label("mystery") == "mystery"
    r = report(
        issues=(issue(1, confidence="high"), issue(2, confidence="low")),
        term_findings=(TermFinding("needs_review", Span(0, 1, "a"), None),),
    )
    line = summary_line(r)
    assert "<" not in line and "\n" not in line
    assert "语义疑点 2 条（高 1 / 中 0 / 较低 1）" in line and "术语发现 1 处" in line
