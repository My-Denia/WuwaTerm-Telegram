from __future__ import annotations

import asyncio
from types import SimpleNamespace

from review_fakes import FakeBackend, wire_response

from wuwaterm_telegram import review_bot
from wuwaterm_telegram.bot import (
    ADMIN_CACHE_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    THROTTLE_NOTICE,
    AdminStatusCache,
    BotConfig,
    PerChatRateLimiter,
    TranslationReply,
    _telegram_reply,
)
from wuwaterm_telegram.review_jobs import ReviewJobStore
from wuwaterm_telegram.runtime_keys import REVIEW_BACKEND_KEY, REVIEW_STORE_KEY
from wuwaterm_telegram.settings import ChatSettings

OWNER = 11
CHAT = 500


class Msg:
    def __init__(self, message_id, text=None, *, reply_to=None, from_user=None):
        self.message_id, self.text, self.reply_to_message = message_id, text, reply_to
        self.from_user, self.sender_chat = from_user, None
        self.sent = []  # (text, kwargs)
        self._next = message_id * 100

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        self._next += 1
        return SimpleNamespace(message_id=self._next)


class Query:
    def __init__(self, data, message):
        self.data, self.message = data, message
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def make_context(tmp_path, backend, *, limit=10):
    settings = ChatSettings(tmp_path / "chat_settings.json")
    settings.allow(CHAT)
    store = ReviewJobStore()
    config = BotConfig(rate_limit_per_minute=limit, owner_user_id=OWNER)
    context = SimpleNamespace(
        args=[],
        bot=SimpleNamespace(id=999, send_chat_action=_noop, get_chat_member=_member),
        application=SimpleNamespace(bot_data={
            CONFIG_KEY: config, CHAT_SETTINGS_KEY: settings, ADMIN_CACHE_KEY: AdminStatusCache(),
            RATE_LIMITER_KEY: PerChatRateLimiter(limit=limit), REJECT_LIMITER_KEY: PerChatRateLimiter(limit=limit),
            REVIEW_BACKEND_KEY: backend, REVIEW_STORE_KEY: store,
        }),
    )
    return context, store, settings


async def _noop(*a, **k):
    return True


async def _member(chat_id, user_id):
    return SimpleNamespace(status="member")


def update_for(message, *, user_id=OWNER, chat_type="private", query=None):
    chat = SimpleNamespace(id=CHAT, type=chat_type)
    return SimpleNamespace(
        effective_message=message, effective_chat=chat,
        effective_user=SimpleNamespace(id=user_id), callback_query=query,
    )


def command(context, text, *, reply_to=None, user_id=OWNER, chat_type="private", message_id=10):
    message = Msg(message_id, text, reply_to=reply_to)
    update = update_for(message, user_id=user_id, chat_type=chat_type)
    asyncio.run(review_bot.review_command(update, context))
    return message


def press(context, data, message, *, user_id=OWNER, chat_type="private"):
    query = Query(data, message)
    update = update_for(message, user_id=user_id, chat_type=chat_type, query=query)
    asyncio.run(review_bot.review_callback(update, context))
    return query


def texts(message):
    return [t for t, _ in message.sent]


def test_inline_form_replies_with_two_sections_and_a_keyboard_on_the_last_message(tmp_path):
    backend = FakeBackend()
    context, store, _ = make_context(tmp_path, backend)
    message = command(context, "/review 今汐来了\n---\nJinhsi arrives")
    assert len(backend.calls) == 1 and backend.calls[0]["direction"] == "en"
    body = "\n".join(texts(message))
    assert "语义疑点（实验性" in body
    assert all(kw.get("parse_mode") == "HTML" for _, kw in message.sent)
    markup = message.sent[-1][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data.startswith("rv:")
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)
    assert store.latest_done(CHAT) is not None


def test_reply_form_uses_the_replied_message_as_source(tmp_path):
    backend = FakeBackend()
    context, _, _ = make_context(tmp_path, backend)
    source = Msg(5, "今汐来了", from_user=SimpleNamespace(id=OWNER, is_bot=False))
    command(context, "/review Jinhsi arrives", reply_to=source)
    assert (backend.calls[0]["source"], backend.calls[0]["target"]) == ("今汐来了", "Jinhsi arrives")


def test_no_input_shows_usage_and_costs_nothing(tmp_path):
    backend = FakeBackend()
    context, _, _ = make_context(tmp_path, backend, limit=1)
    message = command(context, "/review")
    assert texts(message) == [review_bot.REVIEW_USAGE_NOTICE] and backend.calls == []
    assert context.application.bot_data[RATE_LIMITER_KEY].allow(CHAT)  # slot untouched


def test_manual_split_explains_and_sends_no_request(tmp_path):
    backend = FakeBackend()
    context, _, _ = make_context(tmp_path, backend)
    message = command(context, "/review 甲\n\n乙\n---\nonly one block")
    body = "\n".join(texts(message))
    assert backend.calls == [] and "无法可靠配对" in body and "插入空行" in body
    assert "未检查" in body  # nothing was checked, so no "all clear" is implied


def test_same_content_twice_points_to_the_first_result_without_a_second_call(tmp_path):
    backend = FakeBackend()
    context, store, _ = make_context(tmp_path, backend)
    first = command(context, "/review 甲\n---\na")
    second = command(context, "/review 甲\n---\na", message_id=11)
    assert len(backend.calls) == 1
    first_result_id = store.latest_done(CHAT).result_message_ids[0]
    (_, kwargs), = second.sent
    assert kwargs["reply_to_message_id"] == first_result_id
    assert first.sent


def test_review_last_points_to_the_previous_result_or_says_there_is_none(tmp_path):
    context, store, _ = make_context(tmp_path, FakeBackend())
    assert texts(command(context, "/review last")) == [review_bot.NO_LAST_NOTICE]
    command(context, "/review 甲\n---\na")
    pointer = command(context, "/review last", message_id=12)
    (text, kwargs), = pointer.sent
    assert kwargs["reply_to_message_id"] == store.latest_done(CHAT).result_message_ids[0]
    assert "上一条审校结果" in text


def test_a_command_spends_one_rate_limit_slot_however_many_segments(tmp_path):
    backend = FakeBackend()
    context, _, settings = make_context(tmp_path, backend, limit=1)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup", user_id=77)
    command(context, "/review 甲\n\n乙\n---\na\n\nb", **group)
    assert len(backend.calls) == 2  # two segments, one slot
    duplicate = command(context, "/review 甲\n\n乙\n---\na\n\nb", message_id=11, **group)
    assert len(backend.calls) == 2 and THROTTLE_NOTICE not in texts(duplicate)  # a duplicate is free
    other = command(context, "/review 丙\n---\nc", message_id=12, **group)
    assert texts(other) == [THROTTLE_NOTICE] and len(backend.calls) == 2


def test_untrusted_member_is_limited_to_one_request_sized_input(tmp_path):
    backend = FakeBackend()
    context, _, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    message = command(
        context, "/review " + "甲" * 2001 + "\n---\n" + "a" * 10, chat_type="supergroup", user_id=77
    )
    assert backend.calls == []
    assert texts(message) == [review_bot.too_long_notice(2000)]


def test_a_stranger_in_private_is_rejected_and_nothing_runs(tmp_path):
    backend = FakeBackend()
    context, _, _ = make_context(tmp_path, backend)
    message = command(context, "/review 甲\n---\na", user_id=12345)
    assert backend.calls == [] and message.sent
    assert not any("语义疑点" in t for t in texts(message))


def test_authorization_lost_while_running_sends_nothing(tmp_path):
    holder = {}

    def answer(source, target):
        holder["settings"].disallow(CHAT)  # /revoke lands while the model works
        return wire_response(source, target, find=target)

    backend = FakeBackend(answer=answer)
    context, store, holder["settings"] = make_context(tmp_path, backend)
    message = command(context, "/review 甲\n---\na", chat_type="supergroup")
    assert len(backend.calls) == 1
    assert message.sent == [] and store.latest_done(CHAT) is None


def card_flow(tmp_path):
    backend = FakeBackend(answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right"))
    context, store, _ = make_context(tmp_path, backend)
    command(context, "/review 甲乙\n---\nthis is wrong now")
    job = store.latest_done(CHAT)
    keyboard_message = Msg(job.keyboard_message_id, "summary")
    return context, store, job, keyboard_message


def test_buttons_step_accept_and_build_the_revised_translation(tmp_path):
    context, store, job, kb = card_flow(tmp_path)
    opened = press(context, f"rv:{job.id}:c:1", kb)
    assert "1/1" in opened.edits[0][1]["reply_markup"].inline_keyboard[0][1].text
    accepted = press(context, f"rv:{job.id}:a:1", kb)
    assert job.accepted and "✅" in accepted.edits[0][0]
    final = press(context, f"rv:{job.id}:f", kb)
    assert final.answers
    assert "this is right now" in kb.sent[-1][0]
    undone = press(context, f"rv:{job.id}:u:1", kb)
    assert not job.accepted and undone.edits


def test_revision_without_acceptance_says_so_and_sends_nothing(tmp_path):
    context, _, job, kb = card_flow(tmp_path)
    query = press(context, f"rv:{job.id}:f", kb)
    assert query.answers == [(review_bot.NOTHING_ACCEPTED_TOAST, True)] and kb.sent == []


def test_only_the_requester_or_an_admin_may_press(tmp_path):
    backend = FakeBackend(answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right"))
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    group = dict(chat_type="supergroup")
    command(context, "/review 甲\n---\nthis is wrong", user_id=77, **group)
    job = store.latest_done(CHAT)
    kb = Msg(job.keyboard_message_id, "summary")
    other = press(context, f"rv:{job.id}:a:1", kb, user_id=78, **group)
    assert other.answers == [(review_bot.NOT_OWNER_TOAST, True)] and not job.accepted
    mine = press(context, f"rv:{job.id}:a:1", kb, user_id=77, **group)
    assert job.accepted and mine.edits
    admin = press(context, f"rv:{job.id}:u:1", kb, user_id=OWNER, **group)
    assert not job.accepted and admin.edits


def test_expired_job_buttons_answer_with_an_alert(tmp_path):
    context, store, job, kb = card_flow(tmp_path)
    query = press(context, "rv:nosuchid:c:1", kb)
    assert query.answers == [(review_bot.EXPIRED_TOAST, True)]


def test_buttons_stop_working_when_the_chat_is_revoked(tmp_path):
    backend = FakeBackend(answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right"))
    context, store, settings = make_context(tmp_path, backend)
    command(context, "/review 甲\n---\nthis is wrong", chat_type="supergroup")
    job = store.latest_done(CHAT)
    kb = Msg(job.keyboard_message_id, "summary")
    settings.disallow(CHAT)
    query = press(context, f"rv:{job.id}:c:1", kb, chat_type="supergroup")
    assert query.answers == [(review_bot.DENIED_TOAST, True)] and query.edits == []


# --- /tr integration ---------------------------------------------------------------


def test_a_model_translation_offers_a_review_button_only_when_the_model_track_exists(tmp_path):
    reply = TranslationReply("Hello", review_target="Hello", review_direction="en")
    on_context, store, _ = make_context(tmp_path, FakeBackend(semantic=True))
    update = update_for(Msg(1, "/tr 你好"))
    offer = review_bot.offer_translation_review(update, on_context, source="你好", target=reply.review_target, direction="en")
    assert offer is not None and offer.markup.inline_keyboard[0][0].callback_data == f"rv:{offer.record.id}:g"
    off_context, _, _ = make_context(tmp_path, FakeBackend(semantic=False))
    kept = review_bot.offer_translation_review(update, off_context, source="你好", target="Hello", direction="en")
    assert kept is not None and kept.markup is None  # the record is kept, the button is not shown
    assert review_bot.offer_translation_review(update, on_context, source="你好", target=None, direction=None) is None


def test_tr_button_runs_a_review_of_that_translation_once(tmp_path):
    backend = FakeBackend()
    context, store, _ = make_context(tmp_path, backend)
    tr_message = Msg(40, "bot translation")
    update = update_for(tr_message)
    offer = review_bot.offer_translation_review(update, context, source="今汐来了", target="Jinhsi arrives", direction="en")
    review_bot.attach_sent_messages(offer, [SimpleNamespace(message_id=40)])
    press(context, f"rv:{offer.record.id}:g", tr_message)
    press(context, f"rv:{offer.record.id}:g", tr_message)  # double tap
    assert len(backend.calls) == 1
    assert (backend.calls[0]["source"], backend.calls[0]["target"]) == ("今汐来了", "Jinhsi arrives")


def test_replying_to_a_tr_result_reviews_it_and_an_unknown_reply_explains(tmp_path):
    backend = FakeBackend()
    context, store, _ = make_context(tmp_path, backend)
    update = update_for(Msg(1))
    offer = review_bot.offer_translation_review(update, context, source="今汐来了", target="Jinhsi arrives", direction="en")
    review_bot.attach_sent_messages(offer, [SimpleNamespace(message_id=77)])
    bot_message = Msg(77, "Jinhsi arrives", from_user=SimpleNamespace(id=999, is_bot=True))
    command(context, "/review", reply_to=bot_message)
    assert backend.calls and backend.calls[0]["target"] == "Jinhsi arrives"
    stranger = Msg(88, "old bot text", from_user=SimpleNamespace(id=999, is_bot=True))
    answer = command(context, "/review", reply_to=stranger, message_id=12)
    assert texts(answer) == [review_bot.NO_TRANSLATION_RECORD_NOTICE]


def test_translation_reply_carries_the_plain_text_for_llm_results_only():
    from wuwaterm.application import KIND_EXACT, KIND_LLM, TranslationOutcome

    llm = _telegram_reply(TranslationOutcome(kind=KIND_LLM, text="Hello", dictionary_miss=True))
    assert llm.review_target == "Hello" and llm.review_direction == "en"
    assert "词典外" in llm.text  # the flag stays in the sent text, not in the review text
    exact = _telegram_reply(TranslationOutcome(kind=KIND_EXACT, text="Echo"))
    assert exact.review_target is None


def test_tr_command_adds_the_review_button_to_a_model_translation_only(monkeypatch, sample_db):
    from test_bot import enable_mock_llm, fake_context, fake_update, placeholder_for
    from wuwaterm_telegram.bot import term_command

    calls = []

    def response(_locked_text, locks, references):
        return f"{placeholder_for(locks, 'Jinhsi', references, to_chinese=False)} says hello."

    enable_mock_llm(monkeypatch, calls, response)
    store = ReviewJobStore()
    context = fake_context(sample_db, ["今汐说你好"])
    context.application.bot_data[REVIEW_STORE_KEY] = store
    context.application.bot_data[REVIEW_BACKEND_KEY] = FakeBackend(semantic=True)
    update, message = fake_update()
    asyncio.run(term_command(update, context))

    (text, _), = [(t, k) for t, k in zip([r[0] for r in message.replies], message.reply_kwargs)]
    markup = message.reply_kwargs[-1]["reply_markup"]
    button = markup.inline_keyboard[0][0]
    record_id = button.callback_data.split(":")[1]
    record = store.get_translation(1, record_id)
    assert record.source == "今汐说你好" and record.target == "Jinhsi says hello."
    assert record.direction == "en" and record.message_ids  # the sent message is findable
    assert "reply_markup" in message.reply_kwargs[-1]
    assert store.translation_for_message(1, record.message_ids[0]) is record

    # A dictionary hit has nothing to review and gets no button.
    exact_update, exact_message = fake_update()
    exact_context = fake_context(sample_db, ["今汐"])
    exact_context.application.bot_data[REVIEW_STORE_KEY] = store
    exact_context.application.bot_data[REVIEW_BACKEND_KEY] = FakeBackend(semantic=True)
    asyncio.run(term_command(exact_update, exact_context))
    assert all("reply_markup" not in kw for kw in exact_message.reply_kwargs)
    assert text.startswith("Jinhsi says hello")


def test_a_review_that_stopped_early_can_be_run_again(tmp_path):
    backend = FakeBackend(errors={2: "rate_limited"})
    context, store, _ = make_context(tmp_path, backend)
    first = command(context, "/review 甲\n\n乙\n---\na\n\nb")
    assert len(backend.calls) == 2 and "已审 1/2" in "\n".join(texts(first))
    backend.errors = {}
    command(context, "/review 甲\n\n乙\n---\na\n\nb", message_id=11)
    assert len(backend.calls) == 4  # not treated as a duplicate of the unfinished run


def test_long_tr_keeps_the_senders_blank_lines_and_adds_none(monkeypatch, sample_db):
    from test_bot import fake_context, fake_update
    from wuwaterm_telegram.bot import term_command

    calls = []

    async def fake_call(locked_text, locks, html_mode=False, to_chinese=False, timeout_seconds=45.0,
                        transport=None, references=()):
        calls.append(locked_text)
        return f"part-{len(calls)}"

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setattr("wuwaterm.sentence._call_llm_async", fake_call)
    text = "甲" * 1000 + "\n\n" + "乙" * 1000 + "\n\n" + "丙" * 1000
    context = fake_context(sample_db, [text], member_status="administrator")
    update, message = fake_update(chat_id=-2001, chat_type="supergroup", message_id=801, user_id=42)
    asyncio.run(term_command(update, context))
    assert len(calls) == 3  # one request per segmentation-v1 segment
    assert message.replies[0][0] == "part-1\n\npart-2\n\npart-3"


def test_review_with_the_experimental_track_off_gives_terms_only_and_does_not_claim_more(
    monkeypatch, tmp_path, sample_db
):
    from wuwaterm.application import TermService
    from wuwaterm_telegram.review_backend import LiveReviewBackend

    for name in ("WUWATERM_SEMANTIC_REVIEW_ENABLED", "WUWATERM_OPENAI_BASE_URL",
                 "WUWATERM_OPENAI_API_KEY", "WUWATERM_OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    context, _, _ = make_context(tmp_path, LiveReviewBackend(TermService(sample_db), object()))
    message = command(context, "/review 今汐来了。\n---\nSomeone arrives.")
    body = "\n".join(texts(message))
    assert "术语（词典，确定性）" in body and "需核对" in body
    assert "语义疑点未开启" in body and "语义疑点（实验性" not in body
    assert "reply_markup" not in message.sent[-1][1]  # no model issues, no buttons


def test_a_demoted_admin_loses_the_buttons_at_once(tmp_path):
    from wuwaterm_telegram.runtime_keys import REVIEW_STORE_KEY as _  # noqa: F401

    backend = FakeBackend(answer=lambda s, t: wire_response(s, t, find="wrong", replacement="right"))
    context, store, settings = make_context(tmp_path, backend)
    settings.set_public(CHAT, True)
    command(context, "/review 甲乙\n---\nthis is wrong", user_id=77, chat_type="supergroup")
    job = store.latest_done(CHAT)
    context.application.bot_data[ADMIN_CACHE_KEY].put(CHAT, 78, True)  # stale "admin" verdict
    checks = []

    async def now_a_member(chat_id, user_id):
        checks.append(user_id)
        return SimpleNamespace(status="member")

    context.bot.get_chat_member = now_a_member
    kb = Msg(job.keyboard_message_id, "summary")
    query = press(context, f"rv:{job.id}:a:1", kb, user_id=78, chat_type="supergroup")
    assert checks == [78]  # asked Telegram, did not trust the cache
    assert query.answers == [(review_bot.NOT_OWNER_TOAST, True)] and not job.accepted and query.edits == []


def test_a_recent_tr_result_can_be_reviewed_while_the_model_track_is_off(monkeypatch, tmp_path, sample_db):
    from wuwaterm.application import TermService
    from wuwaterm_telegram.review_backend import LiveReviewBackend

    for name in ("WUWATERM_SEMANTIC_REVIEW_ENABLED", "WUWATERM_OPENAI_BASE_URL",
                 "WUWATERM_OPENAI_API_KEY", "WUWATERM_OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    context, store, _ = make_context(tmp_path, LiveReviewBackend(TermService(sample_db), object()))
    offer = review_bot.offer_translation_review(
        update_for(Msg(1)), context, source="今汐来了。", target="Someone arrives.", direction="en"
    )
    assert offer is not None and offer.markup is None  # no button while off
    review_bot.attach_sent_messages(offer, [SimpleNamespace(message_id=77)])
    bot_message = Msg(77, "Someone arrives.", from_user=SimpleNamespace(id=999, is_bot=True))
    answer = command(context, "/review", reply_to=bot_message)
    body = "\n".join(texts(answer))
    assert "需核对" in body and review_bot.NO_TRANSLATION_RECORD_NOTICE not in body
