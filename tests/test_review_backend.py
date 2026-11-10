from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from wuwaterm.application import TermService
from wuwaterm_telegram import review_backend
from wuwaterm_telegram.review_backend import (
    LiveReviewBackend,
    ReviewBackendError,
    enforce_pair_limits,
    infer_direction,
    semantic_request,
)
from wuwaterm_telegram.review_model import PAIR_NEEDS_MANUAL_SPLIT, PAIR_READY, PairInfo


def live(sample_db):
    return LiveReviewBackend(TermService(sample_db), object())


def test_the_installed_core_lacks_the_semantic_track_and_says_so(sample_db):
    # CI installs the pinned Core. While that is 0.7.0 these symbols do not
    # exist; once the pin moves to 0.8.0 this test must be revisited (it then
    # also needs a configured model to be True).
    backend = live(sample_db)
    try:
        import wuwaterm.segmentation  # noqa: F401
        has_segmentation = True
    except ImportError:
        has_segmentation = False
    if has_segmentation:
        pytest.skip("Core with segmentation-v1 installed; see the compat checklist")
    assert backend.semantic_available() is False


def test_without_segmentation_one_request_or_a_manual_split(sample_db):
    backend = live(sample_db)
    if backend._core_attr("wuwaterm.segmentation", "pair_segments") is not None:
        pytest.skip("Core with segmentation-v1 installed")
    (fits,) = backend.plan_pairs("甲" * 2000, "a" * 2000)
    assert fits.status == PAIR_READY and fits.limit == 2000
    (over,) = backend.plan_pairs("甲" * 2001, "a")
    assert over.status == PAIR_NEEDS_MANUAL_SPLIT


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


def test_the_request_body_is_the_published_shape_with_no_precedents():
    body = semantic_request("甲", "a", direction="en", explain_language="zh", context_before=("乙", "b"))
    assert body == {
        "source": "甲", "target": "a", "direction": "en", "review_version": "semantic-review-v1",
        "explain_language": "zh", "precedents": [],
        "context": {"before": {"source": "乙", "target": "b"}},
    }
    assert "context" not in semantic_request("甲", "a", direction="en", explain_language="zh", context_before=None)


def test_a_ready_pair_over_its_limit_is_downgraded_never_sent():
    big = PairInfo(0, PAIR_READY, False, 0, 2001, 0, 2001, 0, 5, 0, 5, 1800)
    empty = PairInfo(1, PAIR_READY, False, 0, 5, 0, 5, 0, 5, 0, 0, 1800)
    ok = PairInfo(2, PAIR_READY, False, 0, 5, 0, 5, 0, 5, 0, 5, 1800)
    assert [p.status for p in enforce_pair_limits([big, empty, ok])] == [
        PAIR_NEEDS_MANUAL_SPLIT, PAIR_NEEDS_MANUAL_SPLIT, PAIR_READY,
    ]


@pytest.fixture()
def fake_core(monkeypatch, sample_db):
    """Inject the Core 0.8.0 symbols the adapter assumes, to test the mapping."""
    calls = []
    application = sys.modules["wuwaterm.application"]

    async def semantic_review_async(service, translator, request):
        calls.append(request)
        if request["source"] == "boom":
            raise RuntimeError("model gateway down")
        return {"review_version": "semantic-review-v1"}

    segmentation = types.ModuleType("wuwaterm.segmentation")
    segmentation.SEGMENT_LIMIT = 1800
    region = lambda s, e: SimpleNamespace(start=s, end=e, core_start=s, core_end=e)  # noqa: E731
    segmentation.segment_text = lambda text, limit=1800: []
    segmentation.pair_segments = lambda src, tgt, limit: [
        SimpleNamespace(index=0, status="ready", hard_cut=True, source=region(0, len(src)), target=region(0, len(tgt)))
    ]
    monkeypatch.setattr(application, "semantic_review_async", semantic_review_async, raising=False)
    monkeypatch.setitem(sys.modules, "wuwaterm.segmentation", segmentation)
    monkeypatch.setattr(review_backend, "llm_configured", lambda: True)
    return calls


def test_adapter_maps_segmentation_and_calls_the_assumed_entry(fake_core, sample_db):
    backend = live(sample_db)
    assert backend.semantic_available() is True
    (item,) = backend.plan_pairs("甲乙", "ab")
    assert item.hard_cut is True and item.limit == 1800 and item.source_len == 2
    body = asyncio.run(backend.semantic_review("甲乙", "ab", direction="en", explain_language="zh", context_before=None))
    assert body["review_version"] == "semantic-review-v1"
    assert fake_core[0]["review_version"] == "semantic-review-v1"
    with pytest.raises(ReviewBackendError) as caught:
        asyncio.run(backend.semantic_review("boom", "x", direction="en", explain_language="zh", context_before=None))
    assert caught.value.code == "internal"


def test_without_a_model_the_semantic_track_is_off_even_if_core_has_it(fake_core, monkeypatch, sample_db):
    monkeypatch.setattr(review_backend, "llm_configured", lambda: False)
    assert live(sample_db).semantic_available() is False


def test_compat_manifest_is_satisfied_by_a_core_that_has_the_symbols(fake_core):
    from wuwaterm_telegram.core_compat import missing_review_symbols

    assert missing_review_symbols() == ()


def test_compat_manifest_on_the_pinned_core(monkeypatch):
    import wuwaterm_telegram.core_compat as compat

    missing = compat.missing_review_symbols()
    if "wuwaterm.application.semantic_review_async" in missing:
        assert "wuwaterm.segmentation.pair_segments" in missing  # 0.7.0: all absent
    else:
        assert missing == ()  # Core 0.8.0: all present
