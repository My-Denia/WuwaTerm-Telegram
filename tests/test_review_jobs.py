from __future__ import annotations

import pytest

from wuwaterm_telegram.review_jobs import ReviewJobStore, StoreFullError
from wuwaterm_telegram.review_model import ReviewReport


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(ttl=100.0, max_per_chat=5):
    clock = Clock()
    return ReviewJobStore(ttl_seconds=ttl, max_per_chat=max_per_chat, clock=clock), clock


def report() -> ReviewReport:
    return ReviewReport(source="s", target="t", direction="en", explain_language="zh")


def test_ttl_boundary_and_chat_isolation():
    store, clock = make()
    job = store.create(1, 7, "a", "b", "en")
    assert store.get(1, job.id) is job
    assert store.get(2, job.id) is None
    clock.now += 99.9
    assert store.get(1, job.id) is job
    clock.now += 0.1  # exactly the TTL
    assert store.get(1, job.id) is None
    assert store.running(1) is None
    assert store.purge() == 1
    assert store.stats() == {"jobs": 0, "translations": 0}


def test_content_key_is_unambiguous():
    key = ReviewJobStore.content_key
    assert key("ab", "c", "en") != key("a", "bc", "en")
    assert key("a", "b", "en") == key("a", "b", "en")
    assert len(key("a", "b", "en")) == 64


def test_duplicate_detection_allows_retry_after_failure():
    store, _ = make()
    job = store.create(1, None, "a", "b", "en")
    assert store.find_duplicate(1, job.content_key) is job
    assert store.find_duplicate(2, job.content_key) is None
    store.finish(job, report())
    assert store.find_duplicate(1, job.content_key) is job
    failed = store.create(1, None, "c", "d", "zh")
    store.fail(failed)
    assert failed.state == "failed" and failed.finished_at is not None
    assert store.find_duplicate(1, failed.content_key) is None


def test_eviction_keeps_running_and_full_of_running_raises():
    store, _ = make(max_per_chat=3)
    first = store.create(1, None, "1", "1", "en")
    second = store.create(1, None, "2", "2", "en")
    third = store.create(1, None, "3", "3", "en")
    store.finish(second, report())
    store.create(1, None, "4", "4", "en")  # evicts the oldest non-running one
    assert store.get(1, second.id) is None
    assert store.get(1, first.id) is first and store.get(1, third.id) is third
    with pytest.raises(StoreFullError):
        store.create(1, None, "5", "5", "en")
    store.create(2, None, "5", "5", "en")  # another chat is unaffected


def test_running_and_latest_done():
    store, clock = make()
    a = store.create(1, None, "a", "a", "en")
    assert store.running(1) is a and store.latest_done(1) is None
    store.finish(a, report())
    assert a.report is not None and a.finished_at == clock.now
    clock.now += 1
    b = store.create(1, None, "b", "b", "en")
    store.finish(b, report())
    assert store.running(1) is None
    assert store.latest_done(1) is b
    clock.now += 99  # a expires, b still lives
    assert store.latest_done(1) is b
    clock.now += 1
    assert store.latest_done(1) is None


def test_ids_fit_callback_data_and_are_unique():
    store, _ = make(ttl=1e9, max_per_chat=500)
    ids = [store.create(1, None, str(i), "t", "en").id for i in range(300)]
    ids += [store.remember_translation(1, None, "s", "t", "en", (i,)).id for i in range(300)]
    assert len(set(ids)) == 600
    assert all(len(i) <= 8 and i.isascii() for i in ids)


def test_translation_records_ttl_eviction_and_message_lookup():
    store, clock = make(max_per_chat=2)
    one = store.remember_translation(1, 7, "s1", "t1", "en", (10, 11))
    assert store.translation_for_message(1, 11) is one
    assert store.translation_for_message(2, 11) is None
    assert store.get_translation(1, one.id) is one
    assert store.get_translation(2, one.id) is None
    two = store.remember_translation(1, None, "s2", "t2", "zh", [20])
    assert two.message_ids == (20,)
    store.remember_translation(1, None, "s3", "t3", "en", (30,))
    assert store.get_translation(1, one.id) is None  # oldest evicted
    assert store.translation_for_message(1, 20) is two
    assert store.stats()["translations"] == 2
    clock.now += 100
    assert store.translation_for_message(1, 20) is None
    assert store.stats() == {"jobs": 0, "translations": 0}


def test_owner_scoped_duplicate_keeps_each_requester_separate():
    store, _ = make()
    first = store.create(1, 7, "a", "b", "en")
    store.finish(first, report())
    key = first.content_key
    assert store.find_owner_duplicate(1, key, 7) is first
    assert store.find_owner_duplicate(1, key, 8) is None  # another member has no job yet
    assert store.find_owner_duplicate(2, key, 7) is None  # chats stay isolated
    assert store.find_duplicate(1, key) is first  # the chat-wide lookup is unchanged
    store.fail(store.create(1, 8, "c", "d", "en"))
    assert store.find_owner_duplicate(1, store.content_key("c", "d", "en"), 8) is None


def test_create_reusing_shares_the_report_but_not_the_acceptance_state():
    store, _ = make()
    first = store.create(1, 7, "a", "b", "en")
    store.finish(first, report())
    first.accepted["0.1"] = 0
    first.skipped.add("0.2")
    first.result_message_ids.append(40)
    first.delivered = True
    job = store.create_reusing(1, 8, first)
    assert job.id != first.id and job.content_key == first.content_key
    assert job.report is first.report  # one computed report, not a copy framework
    assert job.state == "done" and job.delivered is False  # its own delivery
    assert job.user_id == 8 and job.result_message_ids == []
    assert job.accepted == {} and job.skipped == set()
    assert job.accepted is not first.accepted and job.skipped is not first.skipped
    assert first.accepted == {"0.1": 0} and first.skipped == {"0.2"}


def test_create_reusing_never_evicts_the_job_whose_report_it_reuses():
    store, _ = make(max_per_chat=2)
    oldest = store.create(1, 5, "x", "y", "en")
    store.finish(oldest, report())
    first = store.create(1, 7, "a", "b", "en")
    store.finish(first, report())
    job = store.create_reusing(1, 8, first)
    assert store.get(1, first.id) is first and store.get(1, job.id) is job
    assert store.get(1, oldest.id) is None  # the ordinary eviction rule still applies
    running = store.create(1, 9, "p", "q", "en")
    with pytest.raises(StoreFullError):
        store.create_reusing(1, 10, job)
    assert store.get(1, job.id) is job and store.get(1, running.id) is running
