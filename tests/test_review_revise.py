from __future__ import annotations

import pytest

from wuwaterm_telegram.review_model import (
    Alternative,
    ReviewReport,
    SemanticIssue,
    Span,
    Suggestion,
)
from wuwaterm_telegram.review_revise import (
    Edit,
    RevisionError,
    apply_edits,
    build_edits,
    check_accept,
    choice_replacement,
    revise,
)


def issue(key, target, start, end, replacement, *, alternatives=(), withheld=None, drop=False):
    span = Span(start, end, target[start:end])
    suggestion = None if drop else Suggestion(span, replacement, "none")
    return SemanticIssue(
        key=key,
        pair_index=0,
        category="meaning_shift",
        confidence="high",
        source_span=None,
        target_span=span,
        source_excerpt=None,
        target_excerpt=None,
        rationale="r",
        suggestion=suggestion,
        suggestion_withheld=withheld,
        alternatives=tuple(Alternative(a, "none") for a in alternatives),
    )


def report(target, *issues):
    return ReviewReport(source="s", target=target, direction="en", explain_language="zh", issues=tuple(issues))


TARGET = "aaa bbb ccc"


def test_multiple_accepts_replace_back_to_front_with_length_change():
    rep = report(
        TARGET,
        issue("0.a", TARGET, 0, 3, "X"),
        issue("0.b", TARGET, 8, 11, "longer text"),
    )
    assert revise(rep, {"0.b": 0, "0.a": 0}) == "X bbb longer text"
    assert [e.start for e in build_edits(rep, {"0.b": 0, "0.a": 0})] == [0, 8]


def test_adjacent_is_not_overlap_but_intersection_is_refused():
    rep = report(
        TARGET,
        issue("0.a", TARGET, 0, 4, "1"),
        issue("0.b", TARGET, 4, 7, "2"),
        issue("0.c", TARGET, 3, 5, "3"),
    )
    assert check_accept(rep, {"0.a": 0}, "0.b") is None
    assert revise(rep, {"0.a": 0, "0.b": 0}) == "12 ccc"
    assert check_accept(rep, {"0.a": 0}, "0.c") == "overlaps:0.a"
    # re-choosing the same key does not conflict with itself
    assert check_accept(rep, {"0.a": 0}, "0.a") is None
    with pytest.raises(RevisionError):
        revise(rep, {"0.a": 0, "0.c": 0})


def test_alternatives_use_the_main_span():
    rep = report(TARGET, issue("0.a", TARGET, 4, 7, "main", alternatives=("alt",)))
    assert choice_replacement(rep.issues[0], 0) == ("main", "none")
    assert choice_replacement(rep.issues[0], 1) == ("alt", "none")
    assert choice_replacement(rep.issues[0], 2) is None
    assert choice_replacement(rep.issues[0], -1) is None
    assert revise(rep, {"0.a": 1}) == "aaa alt ccc"
    assert check_accept(rep, {}, "0.a", 2) == "bad_choice"


def test_refusal_codes():
    rep = report(
        TARGET,
        issue("0.w", TARGET, 0, 3, "", withheld="span_too_long", drop=True),
        issue("0.ok", TARGET, 4, 7, "y"),
    )
    assert check_accept(rep, {}, "nope") == "unknown_issue"
    assert check_accept(rep, {}, "0.w") == "no_suggestion"
    assert check_accept(rep, {}, "0.ok") is None
    with pytest.raises(RevisionError):
        build_edits(rep, {"0.w": 0})


def test_stale_range_is_refused_not_truncated():
    # A 300-scalar span cut to 200 and paired with a 240-scalar replacement
    # would leave the old tail behind; the full text must match the slice.
    long_target = "x" * 300
    full = Span(0, 300, long_target)
    short = Span(0, 200, "x" * 200)
    rep = report(long_target)
    rep.issues = (
        SemanticIssue("0.a", 0, "omission", "low", None, short, None, None, "r", Suggestion(full, "y" * 240, "none"), None),
    )
    assert revise(rep, {"0.a": 0}) == "y" * 240
    stale = report("z" + long_target[1:])
    stale.issues = rep.issues
    assert check_accept(stale, {}, "0.a") == "stale"
    with pytest.raises(RevisionError):
        revise(stale, {"0.a": 0})
    with pytest.raises(RevisionError):
        apply_edits(long_target, [Edit("0.a", 0, 200, "x" * 300, "y" * 240)])


def test_apply_edits_validates_before_changing_anything():
    assert apply_edits("abc", []) == "abc"
    good, bad = Edit("a", 0, 1, "a", "Z"), Edit("b", 2, 3, "q", "Y")
    with pytest.raises(RevisionError):
        apply_edits("abc", [good, bad])
    with pytest.raises(RevisionError):
        apply_edits("abc", [Edit("a", 2, 9, "c", "Z")])
    with pytest.raises(RevisionError):
        apply_edits("abc", [Edit("a", 0, 2, "ab", "1"), Edit("b", 1, 3, "bc", "2")])


def test_offsets_count_scalars_for_emoji_and_cjk():
    target = "好😀好 you 好😀"
    start = target.index("you")
    end = start + 3
    rep = report(target, issue("0.a", target, 1, 2, "!"), issue("0.b", target, start, end, "你"))
    assert revise(rep, {"0.a": 0, "0.b": 0}) == "好!好 你 好😀"
