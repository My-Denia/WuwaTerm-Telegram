"""Offline regressions for shared-report delivery and requester ownership."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from review_fakes import FakeBackend, wire_response
from telegram.error import BadRequest, RetryAfter
from test_review_bot import (
    CHAT,
    OWNER,
    Msg,
    command,
    make_context,
    network_error,
    newest_done,
    press,
    texts,
    update_for,
)

from wuwaterm_telegram import review_bot
from wuwaterm_telegram.bot import RATE_LIMITER_KEY
from wuwaterm_telegram.review_jobs import ReviewJobStore
from wuwaterm_telegram.runtime_keys import REVIEW_STORE_KEY

SOURCE = "甲乙"
TARGET = "this is wrong"
BODY = f"/review {SOURCE}\n---\n{TARGET}"


@pytest.mark.parametrize(
    ("failure", "notice"),
    [
        ("retry_revoked", None),
        ("bad_request", review_bot.DELIVERY_FAILED_NOTICE),
        ("missing_reply_revoked", None),
    ],
)
def test_shared_result_does_not_fallback_after_unsafe_failure(tmp_path, failure, notice):
    backend = FakeBackend(
        answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right")
    )
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup")

    class FailingMessage(Msg):
        def maybe_fail(self, attempt):
            if attempt > 1:
                return
            if failure in {"retry_revoked", "missing_reply_revoked"}:
                settings.set_public(CHAT, False)
                settings.disallow(CHAT)
            if failure == "retry_revoked":
                raise RetryAfter(0)
            if failure == "missing_reply_revoked":
                raise BadRequest("Message to be replied not found")
            raise BadRequest("Invalid reply markup")

    # Synthetic member and message IDs; no real Telegram transport is used.
    message = FailingMessage(21, BODY)
    asyncio.run(
        review_bot.review_command(
            update_for(message, user_id=88, chat_type="supergroup"), context
        )
    )
    job = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert job is not None and not job.delivered
    assert job.result_message_ids == []
    assert len(backend.calls) == 1
    if notice is None:
        # The chat was revoked while the send failed: stay silent.
        assert message.attempts == 1 and message.sent == []
        return
    # A rejected request posted nothing, so the requester is told and gets the
    # button that sends her own stored report.
    assert message.attempts == 2 and texts(message) == [notice]
    button = message.sent[0][1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"rv:{job.id}:d"


def test_an_uncertain_network_failure_of_the_shared_pointer_is_not_retried(tmp_path):
    backend = FakeBackend(
        answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right")
    )
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup")

    class FlakyMessage(Msg):
        def maybe_fail(self, attempt):
            if attempt == 1:
                raise network_error(httpx.ReadError("read failed"))

    message = FlakyMessage(21, BODY)
    asyncio.run(
        review_bot.review_command(
            update_for(message, user_id=88, chat_type="supergroup"), context
        )
    )
    job = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert job is not None and not job.delivered
    # One pointer send that may have arrived, then the uncertain notice: no
    # second pointer, which could post the same report twice.
    assert message.attempts == 2 and texts(message) == [
        review_bot.DELIVERY_FAILED_UNCERTAIN_NOTICE
    ]
    assert len(backend.calls) == 1
    button = message.sent[0][1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"rv:{job.id}:d"
    press(context, button.callback_data, message, user_id=88, chat_type="supergroup")
    assert job.delivered is True and "语义疑点（实验性" in texts(message)[-1]
    assert len(backend.calls) == 1


def test_a_shared_pointer_whose_connection_never_opened_is_retried_once(tmp_path, monkeypatch):
    monkeypatch.setattr(review_bot, "TRANSIENT_RETRY_DELAY", 0)
    backend = FakeBackend(
        answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right")
    )
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    source = command(context, BODY, user_id=77, chat_type="supergroup")
    assert source.sent

    class FlakyMessage(Msg):
        def maybe_fail(self, attempt):
            if attempt == 1:
                raise network_error(httpx.ConnectError("connection refused"))

    message = FlakyMessage(21, BODY)
    asyncio.run(
        review_bot.review_command(
            update_for(message, user_id=88, chat_type="supergroup"), context
        )
    )
    job = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert message.attempts == 2 and len(message.sent) == 1  # one retry, one pointer
    assert job is not None and job.delivered and len(backend.calls) == 1
    assert "刚审校过" in texts(message)[0]


def test_shared_result_can_fallback_when_only_the_old_reply_is_missing(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup")

    class MissingReplyMessage(Msg):
        def maybe_fail(self, attempt):
            if attempt == 1:
                raise BadRequest("Message to be replied not found")

    message = MissingReplyMessage(21, BODY)
    asyncio.run(
        review_bot.review_command(
            update_for(message, user_id=88, chat_type="supergroup"), context
        )
    )
    job = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert message.attempts == 2 and len(message.sent) == 1
    assert job is not None and job.delivered
    assert len(backend.calls) == 1


def test_concurrent_repeats_from_the_same_requester_get_one_result_and_a_running_notice(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup", fail_times=2)
    first = newest_done(store)
    assert first is not None and not first.delivered

    async def submit_both():
        ready = asyncio.Event()
        arrivals = 0

        class PausedMessage(Msg):
            paused = False

            async def reply_text(self, text, **kwargs):
                nonlocal arrivals
                if not self.paused:
                    self.paused = True
                    arrivals += 1
                    if arrivals == 2:
                        ready.set()
                    await ready.wait()
                return await super().reply_text(text, **kwargs)

        messages = [PausedMessage(n, BODY) for n in (21, 22)]
        await asyncio.gather(
            *(
                review_bot.review_command(
                    update_for(message, user_id=88, chat_type="supergroup"),
                    context,
                )
                for message in messages
            )
        )
        assert arrivals == 2
        return messages

    _, second_repeat = asyncio.run(submit_both())
    assert texts(second_repeat) == [review_bot.RUNNING_NOTICE]
    assert store.stats()["jobs"] == 2
    second = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert second is not None and second is not first and second.delivered
    assert first.user_id == 77 and second.user_id == 88
    assert len(backend.calls) == 1


# --- an undelivered result stays with the member who asked for it -------------------


def test_a_second_member_gets_the_full_report_on_their_own_job_while_the_first_still_waits(tmp_path):
    backend = FakeBackend(
        answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right")
    )
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup")
    command(context, BODY, user_id=77, fail_times=2, **group)
    first = newest_done(store)
    assert first.user_id == 77 and not first.delivered
    reply = command(context, BODY, user_id=88, message_id=21, **group)
    mine = store.find_owner_duplicate(CHAT, store.content_key(SOURCE, TARGET, "en"), 88)
    assert mine is not None and mine is not first and mine.delivered
    assert "语义疑点（实验性" in "\n".join(texts(reply))  # the full result reached 88
    ids = {
        button.callback_data.split(":")[1]
        for _, kwargs in reply.sent
        if kwargs.get("reply_markup")
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert ids == {mine.id}  # every button under 88's messages is 88's own
    # 77's result was not delivered into 88's message.
    assert not first.delivered and first.result_message_ids == []
    kb = Msg(mine.result_message_ids[-1], "summary")
    assert press(context, f"rv:{mine.id}:a:1", kb, user_id=88, **group).edits
    again = command(context, BODY, user_id=77, message_id=22, **group)
    assert "语义疑点（实验性" in "\n".join(texts(again)) and first.delivered
    assert len(backend.calls) == 1


@pytest.mark.parametrize("shared", [False, True])
def test_a_repeat_sent_while_the_result_is_in_flight_waits_instead_of_sending_it_again(
    tmp_path, shared
):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    group = dict(user_id=88, chat_type="supergroup")
    if shared:  # 77's result is out, so 88's own pointer is the send held in flight
        command(context, BODY, user_id=77, chat_type="supergroup")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class HeldMessage(Msg):
            async def reply_text(self, text, **kwargs):
                started.set()
                await release.wait()
                return await super().reply_text(text, **kwargs)

        held = HeldMessage(21, BODY)
        task = asyncio.create_task(review_bot.review_command(update_for(held, **group), context))
        await asyncio.wait_for(started.wait(), 5)
        job = store.find_owner_duplicate(CHAT, store.content_key(SOURCE, TARGET, "en"), 88)
        assert job is not None and job.state == "done" and not job.delivered and held.sent == []
        repeat = Msg(22, BODY)
        await review_bot.review_command(update_for(repeat, **group), context)
        release.set()
        await task
        return held, repeat, job

    held, repeat, job = asyncio.run(scenario())
    assert texts(repeat) == [review_bot.RUNNING_NOTICE]
    assert held.sent and job.delivered and len(backend.calls) == 1


# --- /review last follows the requester ---------------------------------------------


def test_review_last_returns_the_requesters_own_result_not_the_chats_newest(tmp_path):
    backend = FakeBackend(errors={2: "llm_budget_exhausted"})
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup")
    stopped = command(context, "/review 甲\n\n乙\n---\na\n\nb", user_id=77, **group)
    body = "\n".join(texts(stopped))
    assert "已审 1/2" in body and "/review last" in body  # the stop notice promises it
    mine = newest_done(store)
    backend.errors = {}
    command(context, "/review 丙\n---\nc", user_id=88, message_id=21, **group)
    newest = newest_done(store)
    assert newest is not mine and newest.user_id == 88
    pointer = command(context, "/review last", user_id=77, message_id=22, **group)
    (_, kwargs), = pointer.sent
    assert kwargs["reply_to_message_id"] == mine.result_message_ids[0]
    # A member with no result of their own is pointed at the newest one in the chat.
    nobody = command(context, "/review last", user_id=99, message_id=23, **group)
    (_, kwargs), = nobody.sent
    assert kwargs["reply_to_message_id"] == newest.result_message_ids[0]


def test_review_last_does_not_hand_over_another_members_unsent_result_and_costs_no_slot(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend, limit=2)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup")
    command(context, BODY, user_id=77, fail_times=2, **group)  # one slot; nothing reached the chat
    first = newest_done(store)
    assert not first.delivered
    other = command(context, "/review last", user_id=88, message_id=21, **group)
    assert texts(other) == [review_bot.NO_LAST_NOTICE]
    assert not first.delivered and first.result_message_ids == []
    mine = command(context, "/review last", user_id=77, message_id=22, **group)
    assert "语义疑点（实验性" in "\n".join(texts(mine)) and first.delivered
    assert len(backend.calls) == 1
    limiter = context.application.bot_data[RATE_LIMITER_KEY]
    assert [limiter.allow(CHAT), limiter.allow(CHAT)] == [True, False]  # the run took one slot


# --- expiry, failure and ownership messages -----------------------------------------


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_expired_results_tell_the_user_how_to_continue_and_resubmitting_works(tmp_path):
    backend = FakeBackend()
    context, _, _ = make_context(tmp_path, backend)
    clock = Clock()
    store = ReviewJobStore(clock=clock)  # a clock the test can move
    context.application.bot_data[REVIEW_STORE_KEY] = store
    ran = command(context, "/review 甲\n---\na")
    job = newest_done(store)
    offer = review_bot.offer_translation_review(
        update_for(Msg(1)), context, source="乙", target="b", direction="en"
    )
    clock.now += 1800.0  # exactly the TTL
    # The job's own toast talks about /review, the /tr record's about /tr; each
    # names the command that actually brings the thing back.
    job_toast = press(context, f"rv:{job.id}:c:1", ran)
    assert job_toast.answers == [(review_bot.EXPIRED_TOAST, True)]
    assert "/review" in review_bot.EXPIRED_TOAST and "重新运行" in review_bot.EXPIRED_TOAST
    record_toast = press(context, f"rv:{offer.record.id}:g", ran)
    assert record_toast.answers == [(review_bot.TR_EXPIRED_TOAST, True)]
    assert "/tr" in review_bot.TR_EXPIRED_TOAST and "/review" not in review_bot.TR_EXPIRED_TOAST
    (notice,) = command(context, "/review last", message_id=12).sent
    assert "/review" in notice[0]  # not just "nothing here": it says what to send
    command(context, "/review 甲\n---\na", message_id=13)
    assert len(backend.calls) == 2  # the same content is reviewed afresh


def test_a_crashed_review_says_to_try_again_and_the_same_content_can_be_resubmitted(tmp_path):
    class CrashingBackend(FakeBackend):
        crash = True

        async def term_review(self, source, target, direction):
            if self.crash:
                raise RuntimeError("boom")  # unlike a ReviewBackendError, nothing absorbs it
            return await super().term_review(source, target, direction)

    backend = CrashingBackend()
    context, store, _ = make_context(tmp_path, backend)
    failed = command(context, "/review 甲\n---\na")
    assert texts(failed) == [review_bot.FAILED_NOTICE]
    assert newest_done(store) is None  # a failed job is never offered as a result
    backend.crash = False
    again = command(context, "/review 甲\n---\na", message_id=11)
    assert len(backend.calls) == 1 and "语义疑点（实验性" in "\n".join(texts(again))


def test_only_the_requester_or_an_admin_may_press_the_tr_review_button(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend, limit=1)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup")
    tr_message = Msg(40, "bot translation")
    offer = review_bot.offer_translation_review(
        update_for(tr_message, user_id=77, **group),
        context, source="今汐来了", target="Jinhsi arrives", direction="en",
    )
    data = f"rv:{offer.record.id}:g"
    other = press(context, data, tr_message, user_id=78, **group)
    assert other.answers == [(review_bot.NOT_OWNER_TOAST, True)]
    assert backend.calls == [] and newest_done(store) is None
    mine = press(context, data, tr_message, user_id=77, **group)  # 78 spent no slot
    assert mine.answers == [(None, False)] and len(backend.calls) == 1
    admin = press(context, data, tr_message, user_id=OWNER, **group)
    assert admin.answers == [(None, False)] and len(backend.calls) == 1  # reused, not rerun
    assert store.find_owner_duplicate(CHAT, store.content_key("今汐来了", "Jinhsi arrives", "en"), OWNER)
