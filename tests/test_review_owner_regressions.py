"""Offline regressions for shared-report delivery and requester ownership."""

from __future__ import annotations

import asyncio

import pytest
from review_fakes import FakeBackend, wire_response
from telegram.error import BadRequest, NetworkError, RetryAfter
from test_review_bot import CHAT, Msg, command, make_context, update_for

from wuwaterm_telegram import review_bot

SOURCE = "甲乙"
TARGET = "this is wrong"
BODY = f"/review {SOURCE}\n---\n{TARGET}"


@pytest.mark.parametrize(
    "failure",
    ["network", "retry_revoked", "bad_request", "missing_reply_revoked"],
)
def test_shared_result_does_not_fallback_after_unsafe_failure(tmp_path, failure):
    backend = FakeBackend(
        answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right")
    )
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup")

    class FailingMessage(Msg):
        attempts = 0

        async def reply_text(self, text, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                if failure in {"retry_revoked", "missing_reply_revoked"}:
                    settings.set_public(CHAT, False)
                    settings.disallow(CHAT)
                if failure == "network":
                    raise NetworkError("uncertain delivery")
                if failure == "retry_revoked":
                    raise RetryAfter(0)
                if failure == "missing_reply_revoked":
                    raise BadRequest("Message to be replied not found")
                raise BadRequest("Invalid reply markup")
            return await super().reply_text(text, **kwargs)

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
    assert message.attempts == 1 and message.sent == []
    assert job is not None and not job.delivered
    assert job.result_message_ids == []
    assert len(backend.calls) == 1


def test_shared_result_can_fallback_when_only_the_old_reply_is_missing(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup")

    class MissingReplyMessage(Msg):
        attempts = 0

        async def reply_text(self, text, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise BadRequest("Message to be replied not found")
            return await super().reply_text(text, **kwargs)

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


def test_concurrent_repeats_recheck_owner_after_delivering_the_source(tmp_path):
    backend = FakeBackend()
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, BODY, user_id=77, chat_type="supergroup", fail_times=1)
    first = store.latest_done(CHAT)
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

        await asyncio.gather(
            *(
                review_bot.review_command(
                    update_for(PausedMessage(n, BODY), user_id=88, chat_type="supergroup"),
                    context,
                )
                for n in (21, 22)
            )
        )
        assert arrivals == 2

    asyncio.run(submit_both())
    assert store.stats()["jobs"] == 2
    second = store.find_owner_duplicate(
        CHAT, store.content_key(SOURCE, TARGET, "en"), 88
    )
    assert second is not None and second is not first and second.delivered
    assert first.user_id == 77 and second.user_id == 88
    assert len(backend.calls) == 1
