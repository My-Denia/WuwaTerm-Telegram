from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from wuwaterm.application import SentenceTranslator, TermService
from wuwaterm_telegram.review_backend import (
    LiveReviewBackend,
    ReviewBackendError,
    enforce_pair_limits,
    infer_direction,
)
from wuwaterm_telegram.review_flow import run_review
from wuwaterm_telegram.review_model import (
    PAIR_NEEDS_MANUAL_SPLIT,
    PAIR_READY,
    SEMANTIC_DONE,
    PairInfo,
)


def live(sample_db, translator=None):
    return LiveReviewBackend(TermService(sample_db), translator or object())


def configure(monkeypatch, *, enabled=True, model=True):
    for name in ("WUWATERM_OPENAI_BASE_URL", "WUWATERM_OPENAI_API_KEY", "WUWATERM_OPENAI_MODEL",
                 "WUWATERM_SEMANTIC_REVIEW_ENABLED", "WUWATERM_SEMANTIC_REVIEW_MODEL"):
        monkeypatch.delenv(name, raising=False)
    if model:
        monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
        monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
        monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    if enabled:
        monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", "1")


def test_the_model_track_needs_the_server_flag_and_a_model(monkeypatch, sample_db):
    backend = live(sample_db)
    configure(monkeypatch, enabled=False, model=True)
    assert backend.semantic_available() is False  # experimental and off by default
    configure(monkeypatch, enabled=True, model=False)
    assert backend.semantic_available() is False
    configure(monkeypatch, enabled=True, model=True)
    assert backend.semantic_available() is True


def test_pairing_uses_segmentation_v1_and_never_guesses(sample_db):
    backend = live(sample_db)
    ready = backend.plan_pairs("甲一\n\n乙二", "one\n\ntwo")
    assert [(p.status, p.source_len, p.target_len) for p in ready] == [(PAIR_READY, 6, 8)]  # merged
    # 2001 unpunctuated characters against 1000: no trustworthy cut, nothing is sent.
    (stuck,) = backend.plan_pairs("甲" * 2001, "a" * 1000)
    assert stuck.status == PAIR_NEEDS_MANUAL_SPLIT
    pairs = backend.plan_pairs("甲" * 1000 + "，" + "乙" * 1000, "a" * 900 + ", " + "b" * 900)
    assert len(pairs) == 2 and all(p.hard_cut and p.status == PAIR_READY for p in pairs)
    assert all(p.source_len <= 1800 and p.target_len <= 1800 for p in pairs)


def test_the_dictionary_track_runs_on_the_real_core(sample_db):
    backend = live(sample_db)
    ok = asyncio.run(backend.term_review("今汐来了。", "Jinhsi arrives.", "en"))
    assert ok.verified == 1 and ok.findings == ()
    off = asyncio.run(backend.term_review("今汐来了。", "Someone arrives.", "en"))
    (finding,) = off.findings
    assert finding.verdict == "needs_review" and finding.candidates == (("今汐", "Jinhsi"),)
    assert (finding.source_span.start, finding.source_span.end) == (0, 2)


def test_direction_is_the_language_of_the_translation():
    assert infer_direction("Jinhsi arrives") == "en"
    assert infer_direction("今汐来了") == "zh"
    assert infer_direction("12345") == "en"


def test_a_ready_pair_over_its_limit_is_downgraded_never_sent():
    big = PairInfo(0, PAIR_READY, False, 0, 2001, 0, 2001, 0, 5, 0, 5, 1800)
    empty = PairInfo(1, PAIR_READY, False, 0, 5, 0, 5, 0, 5, 0, 0, 1800)
    ok = PairInfo(2, PAIR_READY, False, 0, 5, 0, 5, 0, 5, 0, 5, 1800)
    assert [p.status for p in enforce_pair_limits([big, empty, ok])] == [
        PAIR_NEEDS_MANUAL_SPLIT, PAIR_NEEDS_MANUAL_SPLIT, PAIR_READY,
    ]


def answer_with(issue):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"issues": [issue]}, ensure_ascii=False)}}]})
    handler.requests = []
    return handler


def real_backend(sample_db, handler):
    translator = SentenceTranslator(sample_db, llm_transport=httpx.MockTransport(handler), en_zh_protocol=True)
    return live(sample_db, translator)


def test_real_core_engine_answer_flows_through_the_adapter(monkeypatch, sample_db):
    configure(monkeypatch)
    handler = answer_with({
        "category": "negation_change", "confidence": "high", "source_quote": "不会", "target_quote": "will",
        "rationale": "The negation is lost.", "suggestion": {"target_quote": "will", "replacement": "will not"},
        "alternative": None,
    })
    backend = real_backend(sample_db, handler)
    report = asyncio.run(run_review(backend, "他不会来。", "He will come.", "en"))
    assert report.semantic_state == SEMANTIC_DONE and len(handler.requests) == 1
    (issue,) = report.issues
    assert issue.category == "negation_change" and issue.suggestion.replacement == "will not"
    assert report.target[issue.suggestion.target_span.start:issue.suggestion.target_span.end] == "will"


def test_context_of_the_previous_segment_reaches_the_model(monkeypatch, sample_db):
    configure(monkeypatch)
    handler = answer_with({"category": "omission", "confidence": "low", "source_quote": "乙二", "target_quote": None,
                           "rationale": "x", "suggestion": None, "alternative": None})
    backend = real_backend(sample_db, handler)
    long_a, long_b = "甲" * 1500, "乙" * 1500
    asyncio.run(run_review(backend, f"{long_a}\n\n{long_b}", f"{'a' * 1500}\n\n{'b' * 1500}", "en"))
    assert len(handler.requests) == 2
    assert "a" * 400 in json.dumps(handler.requests[1], ensure_ascii=False)  # read-only context, tail of segment 1


def test_a_disabled_core_answers_unavailable_without_calling_the_model(monkeypatch, sample_db):
    configure(monkeypatch, enabled=False)
    handler = answer_with({})
    backend = real_backend(sample_db, handler)
    with pytest.raises(ReviewBackendError) as caught:
        asyncio.run(backend.semantic_review("他不会来。", "He will come.", direction="en", explain_language="zh", context_before=None))
    assert caught.value.code == "llm_unavailable" and handler.requests == []


def test_upstream_failure_is_reduced_to_a_code(monkeypatch, sample_db):
    configure(monkeypatch)
    backend = real_backend(sample_db, lambda request: httpx.Response(500, json={}))
    with pytest.raises(ReviewBackendError) as caught:
        asyncio.run(backend.semantic_review("他不会来。", "He will come.", direction="en", explain_language="zh", context_before=None))
    assert caught.value.code in {"llm_unavailable", "rate_limited", "llm_budget_exhausted", "internal"}


def test_compat_manifest_matches_the_pinned_core():
    from wuwaterm_telegram.core_compat import missing_review_symbols

    assert missing_review_symbols() == ()
