from __future__ import annotations

import asyncio
import copy

import pytest
from review_fakes import FakeBackend, load_fixture, pair, wire_response

from wuwaterm_telegram.review_flow import ReviewAborted, run_review
from wuwaterm_telegram.review_model import (
    SEMANTIC_DONE,
    SEMANTIC_PARTIAL,
    SEMANTIC_STOPPED,
    SEMANTIC_UNAVAILABLE,
    SemanticResponseError,
    parse_semantic_response,
)


def read(name, **overrides):
    request, response = load_fixture(name)
    args = dict(pair_index=0, source=request["source"], target=request["target"], source_offset=0, target_offset=0)
    args.update(overrides)
    return parse_semantic_response(response, **args), request, response


def test_c1_minimal_fixture_is_read_and_offsets_move_spans_to_whole_text():
    (issues, outcome), request, _ = read("minimal", source_offset=100, target_offset=1000)
    assert outcome.status == "complete" and len(issues) == 3
    first = issues[0]
    assert first.source_span.text == "第三天" and first.source_span.start == 103
    assert first.target_span.start == 1029 and first.suggestion.target_span.start == 1029
    assert first.suggestion.replacement == "third"
    assert issues[2].target_span is None and issues[2].suggestion is None


def test_c1_withheld_suggestion_keeps_the_complete_span_and_offers_no_replacement():
    (issues, _), request, _ = read("suggestion-span-too-long")
    issue = issues[0]
    assert issue.suggestion is None and issue.suggestion_withheld == "span_too_long"
    assert len(issue.target_span.text) == 300  # never shortened


def test_c1_not_reviewed_fixture_has_no_issues_and_a_reason():
    (issues, outcome), _, _ = read("not-reviewed")
    assert issues == [] and outcome.status == "not_reviewed" and outcome.reason == "invalid_model_output"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["issues"][0]["target_span"].update(text="not the slice"),
        lambda b: b["issues"][0].update(source_span=None, target_span=None),
        lambda b: b["issues"][0].update(category="made_up"),
        lambda b: b["issues"][0].update(origin="dictionary"),
        lambda b: b["issues"][0]["target_span"].update(end=10_000),
        lambda b: b.update(issues="oops"),
        lambda b: b.update(review_version="semantic-review-v2"),
        lambda b: b["issues"][0]["suggestion"].update(term_effect="conflict"),
    ],
)
def test_a_response_that_breaks_the_contract_is_refused(mutate):
    request, response = load_fixture("minimal")
    body = copy.deepcopy(response)
    mutate(body)
    with pytest.raises(SemanticResponseError):
        parse_semantic_response(body, pair_index=0, source=request["source"], target=request["target"], source_offset=0, target_offset=0)


def run(backend, source, target, direction="en", **kw):
    return asyncio.run(run_review(backend, source, target, direction, **kw))


def test_each_segment_is_reviewed_in_turn_with_the_previous_segment_as_context():
    backend = FakeBackend()
    report = run(backend, "甲一\n\n乙二\n\n丙三", "one a\n\ntwo b\n\nthree c")
    assert backend.max_in_flight == 1
    assert [c["source"] for c in backend.calls] == ["甲一", "乙二", "丙三"]
    assert backend.calls[0]["context_before"] is None
    assert backend.calls[1]["context_before"] == ("甲一", "one a")
    assert report.semantic_state == SEMANTIC_DONE and report.segments_reviewed == 3
    # coordinates are whole-text: the second segment starts after "one a\n\n"
    assert [i.target_span.start for i in report.issues] == [0, 7, 14]


def test_context_is_the_tail_of_the_previous_segment_at_most_400_scalars():
    backend = FakeBackend()
    run(backend, ("甲" * 900) + "\n\n乙", ("a" * 900) + "\n\nb")
    before_source, before_target = backend.calls[1]["context_before"]
    assert before_source == "甲" * 400 and before_target == "a" * 400


def test_rate_limit_stops_the_run_and_reports_how_far_it_got():
    backend = FakeBackend(errors={2: "rate_limited"})
    report = run(backend, "甲\n\n乙\n\n丙", "a\n\nb\n\nc")
    assert len(backend.calls) == 2
    assert report.semantic_state == SEMANTIC_STOPPED and report.stopped_code == "rate_limited"
    assert (report.segments_reviewed, report.segments_total) == (1, 3)
    assert len(report.issues) == 1  # what finished is kept


def test_one_failed_segment_does_not_stop_the_others():
    backend = FakeBackend(errors={1: "internal"})
    report = run(backend, "甲\n\n乙", "a\n\nb")
    assert len(backend.calls) == 2 and report.semantic_state == SEMANTIC_PARTIAL
    assert [s.status for s in report.segments] == ["failed", "complete"]


def test_a_segment_that_breaks_the_contract_shows_nothing_from_it():
    def answer(source, target):
        body = wire_response(source, target, find=target)
        body["issues"][0]["target_span"]["text"] = "forged"
        return body

    report = run(FakeBackend(answer=answer), "甲", "a")
    assert report.issues == () and report.semantic_state == SEMANTIC_PARTIAL
    assert report.segments[0].reason == "invalid_response"


def test_without_the_semantic_track_only_the_dictionary_track_runs():
    backend = FakeBackend(semantic=False)
    report = run(backend, "甲", "a")
    assert backend.calls == [] and backend.term_calls == 1
    assert report.semantic_state == SEMANTIC_UNAVAILABLE and "未开启" in report.semantic_note


def test_manual_split_pairs_send_nothing_and_are_listed():
    backend = FakeBackend()
    report = run(backend, "甲\n\n乙", "only one paragraph")
    assert backend.calls == [] and backend.term_calls == 0
    assert [p.status for p in report.pairs] == ["needs_manual_split"]


def test_the_gate_stops_before_the_next_request():
    state = {"n": 0}

    async def gate():
        state["n"] += 1
        return state["n"] <= 3  # term stage x2, first semantic call ok, then refuse

    backend = FakeBackend()
    with pytest.raises(ReviewAborted):
        run(backend, "甲\n\n乙", "a\n\nb", gate=gate)
    assert len(backend.calls) <= 1


def test_too_many_segments_is_refused_before_any_call():
    backend = FakeBackend(plan=lambda s, t: [pair(i, "x", "y", i, i) for i in range(13)])
    with pytest.raises(ValueError):
        run(backend, "x" * 13, "y" * 13)
    assert backend.calls == []
