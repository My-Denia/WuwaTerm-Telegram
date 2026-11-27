"""Targeted tests for channel P0 upgrades: rich HTML, context, checks, queue, flood delete."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import stat
import time
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from wuwaterm.application import (
    LLM_INPUT_CHAR_LIMIT,
    LLMTranslationError,
    SentenceTranslator,
    TermService,
    TranslationContext,
)
from wuwaterm_telegram.bot import (
    ADMIN_CACHE_KEY,
    CHANNEL_QUEUE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    SERVICE_KEY,
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    TRANSLATOR_KEY,
    AdminStatusCache,
    BotConfig,
    ChannelReplyIndex,
    PerChatRateLimiter,
    status_command,
)
from wuwaterm_telegram.channel import (
    channel_post_handler,
    execute_queued_channel_task,
)
from wuwaterm_telegram.channel_checks import check_channel_translation
from wuwaterm_telegram.channel_queue import (
    ChannelTaskQueue,
    ChannelTaskState,
    TaskExecutionOutcome,
)
from wuwaterm_telegram.settings import ChatSettings
from wuwaterm_telegram.telegram_html import (
    split_telegram_html,
    strip_telegram_html,
    validate_telegram_html,
)
from wuwaterm_telegram.telegram_text import telegram_text_units


# ---------------------------------------------------------------------------
# 1. PTB 22.8 / Bot API 10.0 rich HTML validation & balanced HTML splitting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        '<tg-time unix="1700000000" format="r">tomorrow</tg-time>',
        '<tg-time unix="1700000000" format="wDT">Monday</tg-time>',
        '<tg-time unix="1700000000" format="dt">12:00</tg-time>',
        '<tg-time unix="1700000000" format="">12:00</tg-time>',
        '<tg-time unix="1700000000">12:00</tg-time>',
        '<pre><code class="python">print(1)</code></pre>',
        '<pre><code class="language-python">print(1)</code></pre>',
        '<pre><code class="c++">int x = 1;</code></pre>',
        '<blockquote expandable><b>今汐</b> <tg-emoji emoji-id="5368324170671202286">✨</tg-emoji></blockquote>',
    ],
)
def test_ptb_228_rich_html_tags_validate(snippet):
    assert validate_telegram_html(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "<tg-time>missing unix</tg-time>",
        '<tg-time unix="not-a-number">x</tg-time>',
        '<tg-time unix="1700000000" format="invalid!">x</tg-time>',
        '<tg-time unix="1700000000" format="dd">x</tg-time>',
        '<tg-time unix="1700000000" format="dD">x</tg-time>',
        '<tg-time unix="1700000000" format="tt">x</tg-time>',
        '<tg-time unix="1700000000" format="tT">x</tg-time>',
        '<tg-time unix="1700000000" format="ww">x</tg-time>',
        '<tg-time unix="1700000000" format="rw">x</tg-time>',
        '<tg-time unix="1700000000" onclick="evil">x</tg-time>',
        '<code class="python">bare class outside pre</code>',
        '<pre><code class="">empty lang</code></pre>',
        '<pre><code class="bad lang!">x</code></pre>',
    ],
)
def test_ptb_228_invalid_rich_html_rejected(snippet):
    assert not validate_telegram_html(snippet)


def test_ptb_228_pre_code_class_and_date_time_round_trip():
    from telegram import Chat, Message, MessageEntity

    from wuwaterm_telegram.telegram_html import (
        normalize_telegram_html,
        protect_telegram_html,
    )

    chat = Chat(id=-2001, type="supergroup")
    code_msg = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=chat,
        text="print(1)",
        entities=(
            MessageEntity(
                type=MessageEntity.PRE, offset=0, length=8, language="python"
            ),
        ),
    )
    assert code_msg.text_html == '<pre><code class="python">print(1)</code></pre>'
    normalized = normalize_telegram_html(code_msg.text_html)
    assert normalized == '<pre><code class="language-python">print(1)</code></pre>'
    protected = protect_telegram_html(code_msg.text_html)
    assert protected is not None
    restored = protected.render()
    assert restored == '<pre><code class="language-python">print(1)</code></pre>'
    assert validate_telegram_html(restored)

    dt_msg = Message(
        message_id=2,
        date=datetime.now(timezone.utc),
        chat=chat,
        text="Monday",
        entities=(
            MessageEntity(
                type=MessageEntity.DATE_TIME,
                offset=0,
                length=6,
                unix_time=1700000000,
                date_time_format="wDT",
            ),
        ),
    )
    assert dt_msg.text_html is not None
    assert validate_telegram_html(dt_msg.text_html)


def test_split_telegram_html_preserves_paragraphs_and_balances_tags():
    p1 = '<b>第一段标题</b>\n<a href="https://example.com">详情链接</a> ' + ("甲" * 60)
    p2 = (
        '<blockquote expandable>第二段引用 '
        '<tg-emoji emoji-id="5368324170671202286">✨</tg-emoji> '
        '<tg-time unix="1700000000" format="r">明天</tg-time> '
        + ("乙" * 60)
        + "</blockquote>"
    )
    html = f"{p1}\n\n{p2}"
    chunks = split_telegram_html(html, limit=95, unit_counter=telegram_text_units)
    assert chunks is not None
    assert len(chunks) >= 2
    for chunk in chunks:
        assert validate_telegram_html(chunk)
        assert telegram_text_units(strip_telegram_html(chunk)) <= 95
    assert "".join(strip_telegram_html(c) for c in chunks) == strip_telegram_html(html)
    assert any('emoji-id="5368324170671202286"' in c for c in chunks)
    assert any('unix="1700000000"' in c for c in chunks)
    assert any('<a href="https://example.com">' in c for c in chunks)


def test_split_telegram_html_balances_single_long_bold_block():
    html = "<b>" + ("Hello world! " * 20) + "</b>"
    chunks = split_telegram_html(html, limit=60, unit_counter=telegram_text_units)
    assert chunks is not None
    assert len(chunks) >= 3
    for chunk in chunks:
        assert chunk.startswith("<b>") and chunk.endswith("</b>")
        assert validate_telegram_html(chunk)
        assert telegram_text_units(strip_telegram_html(chunk)) <= 60
    assert "".join(strip_telegram_html(c) for c in chunks) == strip_telegram_html(html)


# ---------------------------------------------------------------------------
# 2. Zero-cost deterministic output checks
# ---------------------------------------------------------------------------


def test_output_checks_detect_ok_warning_and_critical_blocks():
    ok = check_channel_translation(
        "攻击力提升 20%\n\n冷却时间 5 秒",
        "ATK increases by 20%.\n\nCooldown is 5s.",
        to_chinese=False,
    )
    assert ok.passed is True
    assert ok.severity == "ok"
    assert ok.action == "deliver"

    num_warn = check_channel_translation(
        "攻击力提升 20%，暴击率提升 15%",
        "ATK increases by 20%.",
        to_chinese=False,
    )
    assert num_warn.passed is True
    assert num_warn.severity == "warning"
    assert num_warn.action == "deliver_with_warning"
    assert "number_missing" in num_warn.rule_counts

    empty_block = check_channel_translation("今汐登场", "   ", to_chinese=False)
    assert empty_block.passed is False
    assert empty_block.severity == "critical"
    assert empty_block.action == "block"
    assert "empty_target" in empty_block.rule_counts

    leaked_block = check_channel_translation(
        "今汐说声骸很强",
        "__WUWA_TERM_0123456789abcdef_0001__ says Echo is strong.",
        to_chinese=False,
    )
    assert leaked_block.passed is False
    assert leaked_block.severity == "critical"
    assert leaked_block.action == "block"
    assert "leaked_internal_placeholder" in leaked_block.rule_counts


# ---------------------------------------------------------------------------
# 3. Channel long-post context, rich HTML delivery, output check & queue
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id: int, text: str, text_html: str | None = None):
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.text_html = text_html if text_html is not None else text
        self.caption_html = None
        self.date = datetime.now(timezone.utc)
        self.edit_date = None
        self.replies: list[tuple[str, str | None, int | None]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(
            (text, kwargs.get("parse_mode"), kwargs.get("reply_to_message_id"))
        )
        return SimpleNamespace(message_id=self.message_id + 1000 + len(self.replies))


class _FakeBot:
    def __init__(self, *, delete_retry_once: bool = False):
        self.sent: list[tuple[int, str, str | None, int | None]] = []
        self.edits: list[tuple[str, str | None, int, int]] = []
        self.deleted: list[tuple[int, int]] = []
        self._delete_retry_once = delete_retry_once
        self._delete_calls = 0

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(
            (chat_id, text, kwargs.get("parse_mode"), kwargs.get("reply_to_message_id"))
        )
        return SimpleNamespace(message_id=9000 + len(self.sent))

    async def edit_message_text(self, text: str, chat_id: int, message_id: int, **kwargs):
        self.edits.append((text, kwargs.get("parse_mode"), chat_id, message_id))
        return SimpleNamespace(message_id=message_id)

    async def delete_message(self, chat_id: int, message_id: int):
        self._delete_calls += 1
        if self._delete_retry_once and self._delete_calls == 1:
            raise RetryAfter(0.0)
        self.deleted.append((chat_id, message_id))
        return True


def _make_context(sample_db, *, config=None, bot=None, queue=None):
    config = config or BotConfig(owner_user_id=11, channel_llm_calls_per_minute=20)
    settings = ChatSettings(sample_db.parent / "chat_settings.json")
    settings.allow(-2001)
    bot_data = {
        SERVICE_KEY: TermService(sample_db),
        TRANSLATOR_KEY: SentenceTranslator(sample_db),
        CONFIG_KEY: config,
        RATE_LIMITER_KEY: PerChatRateLimiter(limit=10),
        REJECT_LIMITER_KEY: PerChatRateLimiter(limit=10),
        ADMIN_CACHE_KEY: AdminStatusCache(),
        CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(ttl_seconds=3600.0),
        CHAT_SETTINGS_KEY: settings,
    }
    if queue is not None:
        bot_data[CHANNEL_QUEUE_KEY] = queue
    return SimpleNamespace(
        args=[],
        bot=bot or _FakeBot(),
        application=SimpleNamespace(bot_data=bot_data),
    )


def test_long_plain_channel_post_passes_context_before_and_keeps_paragraphs(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    captured_contexts: list[TranslationContext | None] = []
    idx = 0

    async def fake_translate_async(
        self,
        text,
        *,
        to_chinese=False,
        before_llm_call=None,
        propagate_errors=False,
        context=None,
        execution_budget=None,
    ):
        nonlocal idx
        if before_llm_call is not None:
            before_llm_call()
        captured_contexts.append(context)
        idx += 1
        return f"Translated paragraph {idx}."

    monkeypatch.setattr(SentenceTranslator, "translate_async", fake_translate_async)
    para1 = "今汐在第一段释放技能。" * 140
    para2 = "声骸在第二段提供增益。" * 100
    full_text = f"{para1}\n\n{para2}"
    assert len(full_text) > LLM_INPUT_CHAR_LIMIT

    msg = _FakeMessage(5001, full_text)
    update = SimpleNamespace(
        update_id=5001,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = _make_context(sample_db)
    asyncio.run(channel_post_handler(update, context))

    assert len(captured_contexts) == 2
    assert isinstance(captured_contexts[0], TranslationContext)
    assert captured_contexts[0].before is None
    assert isinstance(captured_contexts[1], TranslationContext)
    assert captured_contexts[1].before is not None
    assert captured_contexts[1].before.target == "Translated paragraph 1."
    assert msg.replies == [
        ("Translated paragraph 1.\n\nTranslated paragraph 2.", None, 5001)
    ]


def test_long_html_output_preserves_balanced_html_across_multiple_chunks(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    part1 = "First translated section. " * 100
    part2 = "Second translated section. " * 100

    async def fake_translate_protected_async(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        # Preserve structures verbatim, replace visible text with >4096 UTF-16 output.
        new_segments = list(protected.visible_segments)
        assert len(new_segments) >= 5
        new_segments[1] = part1
        new_segments[3] = part2
        return type(protected)(tuple(new_segments), protected.structures)

    monkeypatch.setattr(
        SentenceTranslator, "translate_protected_async", fake_translate_protected_async
    )
    msg = _FakeMessage(
        5002,
        text="第一段\n\n第二段",
        text_html="<b>第一段</b>\n\n<i>第二段</i>",
    )
    update = SimpleNamespace(
        update_id=5002,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = _make_context(sample_db)
    asyncio.run(channel_post_handler(update, context))

    assert len(msg.replies) >= 2
    assert all(mode == "HTML" for _text, mode, _reply in msg.replies)
    for text, _mode, _reply in msg.replies:
        assert validate_telegram_html(text)
        assert telegram_text_units(strip_telegram_html(text)) <= TELEGRAM_TEXT_MESSAGE_LIMIT
    assert any("<b>" in text for text, _, _ in msg.replies)
    assert any("<i>" in text for text, _, _ in msg.replies)


def test_channel_blocks_leaked_placeholder_and_enqueues_for_recovery(
    monkeypatch, sample_db, caplog
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_translate_protected_async(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        # Simulate a leaked internal token inside visible output.
        return type(protected)(
            ("__WUWA_TERM_0123456789abcdef_0001__ leaked.",), ()
        )

    monkeypatch.setattr(
        SentenceTranslator, "translate_protected_async", fake_translate_protected_async
    )
    queue = ChannelTaskQueue(base_backoff_seconds=1.0)
    msg = _FakeMessage(5003, "机密频道原帖内容今汐")
    update = SimpleNamespace(
        update_id=5003,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = _make_context(sample_db, queue=queue)

    with caplog.at_level(logging.INFO, logger="wuwaterm_telegram"):
        asyncio.run(channel_post_handler(update, context))

    assert msg.replies == []
    assert queue.stats().pending == 1
    runtime = context.application.bot_data[CHANNEL_RUNTIME_KEY]
    assert runtime.snapshot().outcomes["output_check:critical"] == 1
    assert "机密频道原帖内容今汐" not in caplog.text


def test_delete_reply_chunks_uses_flood_retry(monkeypatch, sample_db):
    from wuwaterm_telegram.channel import _delete_reply_chunks

    bot = _FakeBot(delete_retry_once=True)
    context = _make_context(sample_db, bot=bot)
    msg = _FakeMessage(5010, "今汐")

    failed = asyncio.run(
        _delete_reply_chunks(
            context,
            message=msg,
            chat_id=-2001,
            reply_message_ids=(6001,),
        )
    )
    assert failed == ()
    assert bot._delete_calls == 2
    assert bot.deleted == [(-2001, 6001)]


# ---------------------------------------------------------------------------
# 4. Bounded recoverable ChannelTaskQueue + privacy persistence + /status
# ---------------------------------------------------------------------------


def test_channel_queue_state_machine_backoff_supersede_and_disk_privacy(
    tmp_path, caplog
):
    now = [1000.0]
    storage = tmp_path / "channel_queue.json"
    queue = ChannelTaskQueue(
        max_size=2,
        ttl_seconds=300.0,
        max_attempts=3,
        base_backoff_seconds=5.0,
        max_backoff_seconds=30.0,
        storage_path=storage,
        clock=lambda: now[0],
    )
    secret_v1 = "PRIVATE_CHANNEL_BODY_V1"
    secret_v2 = "PRIVATE_CHANNEL_BODY_V2"

    with caplog.at_level(logging.INFO, logger="wuwaterm_telegram.channel_queue"):
        t1 = queue.enqueue(
            chat_id=-2001,
            message_id=7001,
            chat_type="supergroup",
            plain=secret_v1,
            html=f"<b>{secret_v1}</b>",
            is_edit=False,
            update_id=1,
            message_date_ts=now[0],
            length_limit=4096,
            reason="llm_budget",
        )
        assert t1 is not None
        assert t1.state == ChannelTaskState.PENDING

        # Disk persistence enforces 0600 permissions.
        if os.name == "posix":
            assert stat.S_IMODE(storage.stat().st_mode) == 0o600

        # Newer edit supersedes pending task for the same post key.
        t1_edit = queue.enqueue(
            chat_id=-2001,
            message_id=7001,
            chat_type="supergroup",
            plain=secret_v2,
            html=f"<b>{secret_v2}</b>",
            is_edit=True,
            update_id=2,
            message_date_ts=now[0],
            length_limit=4096,
            reason="queue_full",
        )
        assert t1_edit is t1
        assert t1.plain == secret_v2
        assert queue.stats().superseded == 1
        assert queue.stats().pending == 1

        # Second post fills queue to max_size=2; third drops with overflow.
        t2 = queue.enqueue(
            chat_id=-2001,
            message_id=7002,
            chat_type="supergroup",
            plain="second",
            html=None,
            is_edit=False,
            update_id=3,
            message_date_ts=now[0],
            length_limit=4096,
            reason="timeout",
        )
        assert t2 is not None
        t3 = queue.enqueue(
            chat_id=-2001,
            message_id=7003,
            chat_type="supergroup",
            plain="overflow",
            html=None,
            is_edit=False,
            update_id=4,
            message_date_ts=now[0],
            length_limit=4096,
            reason="timeout",
        )
        assert t3 is None
        assert queue.stats().dropped_overflow == 1

        # Reloading from disk recovers persisted tasks.
        reloaded = ChannelTaskQueue(
            max_size=2,
            ttl_seconds=300.0,
            max_attempts=3,
            base_backoff_seconds=5.0,
            max_backoff_seconds=30.0,
            storage_path=storage,
            clock=lambda: now[0],
        )
        assert reloaded.stats().pending == 2

        # Before backoff elapses, nothing is due.
        assert reloaded.lease_due() is None

        # Advance past first backoff (5s) -> retryable failure triggers exponential backoff.
        now[0] += 6.0
        async def fail_then_succeed(task):
            if task.attempts == 1:
                return TaskExecutionOutcome("retry", "timeout")
            return TaskExecutionOutcome("completed", "delivered")

        assert asyncio.run(reloaded.process_once(fail_then_succeed)) is True
        task_after_first = reloaded.get(-2001, 7001)
        assert task_after_first is not None
        assert task_after_first.state == ChannelTaskState.PENDING
        assert task_after_first.attempts == 1
        # Advance past backoff: 7002 (earlier next_attempt_at=1005) runs first,
        # then 7001 (next_attempt_at=1011) runs its 2nd attempt -> completes and wipes text.
        now[0] += 10.0
        assert asyncio.run(reloaded.process_once(fail_then_succeed)) is True
        assert asyncio.run(reloaded.process_once(fail_then_succeed)) is True
        assert reloaded.get(-2001, 7001) is None
        assert reloaded.stats().completed == 1
        persisted_text = storage.read_text(encoding="utf-8")
        assert secret_v1 not in persisted_text
        assert secret_v2 not in persisted_text

        # Advance past TTL -> remaining task expires and wipes text from disk.
        now[0] += 400.0
        assert reloaded.prune_expired() == 1
        assert reloaded.stats().expired == 1
        assert "second" not in storage.read_text(encoding="utf-8")

    # Never log post body text.
    assert secret_v1 not in caplog.text
    assert secret_v2 not in caplog.text


def test_channel_queue_recovers_post_after_transient_llm_failure_and_reports_status(
    monkeypatch, sample_db
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    attempts = [0]

    async def flaky_protected(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        attempts[0] += 1
        if attempts[0] == 1:
            raise LLMTranslationError("unavailable", reason="timeout")
        return type(protected)(("Recovered translation.",), ())

    monkeypatch.setattr(
        SentenceTranslator, "translate_protected_async", flaky_protected
    )
    now = [2000.0]
    queue = ChannelTaskQueue(base_backoff_seconds=2.0, clock=lambda: now[0])
    bot = _FakeBot()
    context = _make_context(sample_db, bot=bot, queue=queue)

    msg = _FakeMessage(5100, "今汐技能说明文本")
    update = SimpleNamespace(
        update_id=5100,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    asyncio.run(channel_post_handler(update, context))
    assert msg.replies == []
    assert queue.stats().pending == 1

    # Advance past backoff and drain queued task via execute_queued_channel_task.
    now[0] += 3.0
    context.application.bot = bot
    processed = asyncio.run(
        queue.process_once(
            lambda task: execute_queued_channel_task(context.application, task)
        )
    )
    assert processed is True
    assert queue.stats().pending == 0
    assert queue.stats().completed == 1
    assert any("Recovered translation." in text for _, text, _, _ in bot.sent)

    # /status reports sanitized queue counts without leaking message text.
    status_msg = _FakeMessage(5101, "/status")
    status_update = SimpleNamespace(
        effective_message=status_msg,
        effective_chat=SimpleNamespace(id=11, type="private"),
        effective_user=SimpleNamespace(id=11),
    )
    asyncio.run(status_command(status_update, context))
    reply = status_msg.replies[0][0]
    assert "Core backend: local" in reply
    assert "Channel queue persistence: off" in reply
    assert "Channel queue pending: 0" in reply
    assert "Channel queue completed: 1" in reply
    assert "今汐技能说明文本" not in reply


# ---------------------------------------------------------------------------
# 5. Review regressions: outcomes, queue ordering/TTL, HTML fallback & gate
# ---------------------------------------------------------------------------


def test_queued_task_timed_out_marks_failed_never_completed_even_with_concurrent_delivery(
    monkeypatch, sample_db
):
    from telegram.error import TimedOut

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def fake_translate_async(
        self,
        text,
        *,
        to_chinese=False,
        before_llm_call=None,
        propagate_errors=False,
        context=None,
        execution_budget=None,
    ):
        if before_llm_call is not None:
            before_llm_call()
        return "Translated."

    monkeypatch.setattr(SentenceTranslator, "translate_async", fake_translate_async)

    now = [time.time()]
    queue = ChannelTaskQueue(
        base_backoff_seconds=1.0,
        ttl_seconds=300.0,
        post_max_age_seconds=86400.0,
        clock=lambda: now[0],
    )

    class _TimeoutBot(_FakeBot):
        async def send_message(self, chat_id: int, text: str, **kwargs):
            # Simulate a concurrent delivery of another post bumping global runtime counters
            # while THIS post hits TimedOut.
            runtime = context.application.bot_data.get(CHANNEL_RUNTIME_KEY)
            if runtime is not None:
                runtime.record("delivered", "delivered")
            raise TimedOut("gateway timeout")

    bot = _TimeoutBot()
    context = _make_context(sample_db, bot=bot, queue=queue)
    context.application.bot = bot

    task = queue.enqueue(
        chat_id=-2001,
        message_id=5201,
        chat_type="supergroup",
        plain="今汐超时交付测试",
        html=None,
        is_edit=False,
        update_id=10,
        message_date_ts=now[0],
        length_limit=4096,
        reason="timeout",
    )
    assert task is not None
    now[0] += 2.0
    processed = asyncio.run(
        queue.process_once(
            lambda t: execute_queued_channel_task(context.application, t)
        )
    )
    assert processed is True
    assert queue.stats().completed == 0
    assert queue.stats().failed == 1
    assert queue.stats().pending == 0
    reply_index = context.application.bot_data[CHANNEL_REPLY_INDEX_KEY]
    assert reply_index.get(-2001, 5201) is None


def test_channel_queue_rejects_stale_update_id_and_retains_inflight_supersede(
    tmp_path,
):
    now = [1000.0]
    storage = tmp_path / "queue_supersede.json"
    queue = ChannelTaskQueue(
        max_size=4,
        ttl_seconds=600.0,
        post_max_age_seconds=86400.0,
        base_backoff_seconds=2.0,
        storage_path=storage,
        clock=lambda: now[0],
    )

    t20 = queue.enqueue(
        chat_id=-2001,
        message_id=7100,
        chat_type="supergroup",
        plain="v20",
        html=None,
        is_edit=False,
        update_id=20,
        message_date_ts=now[0],
        length_limit=4096,
        reason="timeout",
    )
    assert t20 is not None
    # Older or equal update_id must not overwrite pending v20.
    stale = queue.enqueue(
        chat_id=-2001,
        message_id=7100,
        chat_type="supergroup",
        plain="v19",
        html=None,
        is_edit=True,
        update_id=19,
        message_date_ts=now[0],
        length_limit=4096,
        reason="timeout",
    )
    assert stale is t20
    assert t20.plain == "v20"
    assert t20.update_id == 20

    # Advance past backoff so v20 runs IN_FLIGHT; enqueue stale v19 and newer v21 mid-flight.
    now[0] += 5.0
    seen_order: list[tuple[int | None, str]] = []

    async def runner(task):
        seen_order.append((task.update_id, task.plain))
        if task.update_id == 20:
            # Older update ignored while IN_FLIGHT.
            queue.enqueue(
                chat_id=-2001,
                message_id=7100,
                chat_type="supergroup",
                plain="v19-late",
                html=None,
                is_edit=True,
                update_id=19,
                message_date_ts=now[0],
                length_limit=4096,
                reason="timeout",
            )
            # Newer edit retained in pending supersede buffer and persisted with 0600.
            queue.enqueue(
                chat_id=-2001,
                message_id=7100,
                chat_type="supergroup",
                plain="v21",
                html=None,
                is_edit=True,
                update_id=21,
                message_date_ts=now[0],
                length_limit=4096,
                reason="timeout",
            )
            if os.name == "posix":
                assert stat.S_IMODE(storage.stat().st_mode) == 0o600
        return TaskExecutionOutcome("completed", "delivered")

    assert asyncio.run(queue.process_once(runner)) is True
    # After v20 finishes, v21 must immediately be promoted to PENDING (not dropped).
    promoted = queue.get(-2001, 7100)
    assert promoted is not None
    assert promoted.state == ChannelTaskState.PENDING
    assert promoted.update_id == 21
    assert promoted.plain == "v21"

    now[0] += 5.0
    assert asyncio.run(queue.process_once(runner)) is True
    assert seen_order == [(20, "v20"), (21, "v21")]
    assert queue.get(-2001, 7100) is None


@pytest.mark.parametrize("transient_reason", ["upstream", "connect", "request"])
def test_channel_enqueues_core_016_transient_transport_reasons(
    monkeypatch, sample_db, transient_reason
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    async def failing_protected(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        raise LLMTranslationError("transient", reason=transient_reason)

    monkeypatch.setattr(
        SentenceTranslator, "translate_protected_async", failing_protected
    )
    queue = ChannelTaskQueue(base_backoff_seconds=1.0)
    msg = _FakeMessage(5300, "今汐技能测试")
    update = SimpleNamespace(
        update_id=5300,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = _make_context(sample_db, queue=queue)
    asyncio.run(channel_post_handler(update, context))
    assert queue.stats().pending == 1


def test_channel_queue_separates_post_max_age_from_queue_ttl_and_enforces_inflight_deadline(
    monkeypatch, sample_db, tmp_path
):
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    now = [time.time()]
    storage = tmp_path / "ttl_queue.json"
    queue = ChannelTaskQueue(
        max_size=4,
        ttl_seconds=3600.0,
        post_max_age_seconds=86400.0,
        base_backoff_seconds=5.0,
        storage_path=storage,
        clock=lambda: now[0],
    )
    # Post published 2 hours ago (within 24h post_max_age_seconds, older than 1h queue TTL).
    post_ts = now[0] - 7200.0
    task = queue.enqueue(
        chat_id=-2001,
        message_id=5400,
        chat_type="supergroup",
        plain="两小时前发布的今汐帖子",
        html=None,
        is_edit=False,
        update_id=100,
        message_date_ts=post_ts,
        length_limit=4096,
        reason="timeout",
    )
    assert task is not None
    assert task.expires_at == pytest.approx(now[0] + 3600.0)
    assert queue.prune_expired() == 0

    # Advance past backoff, lease IN_FLIGHT, then advance past queue TTL while IN_FLIGHT.
    now[0] += 6.0
    leased = queue.lease_due()
    assert leased is task
    assert leased.state == ChannelTaskState.IN_FLIGHT
    now[0] += 4000.0
    pruned = queue.prune_expired()
    assert pruned == 1
    assert leased.plain == ""
    assert leased.html is None
    assert "两小时前发布的今汐帖子" not in storage.read_text(encoding="utf-8")

    # End-to-end delivery gate aborts if queue deadline crosses during in-flight LLM call.
    now[0] = time.time()
    queue2 = ChannelTaskQueue(
        max_size=4,
        ttl_seconds=60.0,
        post_max_age_seconds=86400.0,
        base_backoff_seconds=1.0,
        clock=lambda: now[0],
    )

    async def slow_translate_async(
        self,
        text,
        *,
        to_chinese=False,
        before_llm_call=None,
        propagate_errors=False,
        context=None,
        execution_budget=None,
    ):
        if before_llm_call is not None:
            before_llm_call()
        # Simulate queue TTL expiring mid-flight before send_message runs.
        now[0] += 120.0
        return "Late translation."

    monkeypatch.setattr(SentenceTranslator, "translate_async", slow_translate_async)
    bot = _FakeBot()
    context = _make_context(sample_db, bot=bot, queue=queue2)
    context.application.bot = bot
    task2 = queue2.enqueue(
        chat_id=-2001,
        message_id=5401,
        chat_type="supergroup",
        plain="今汐在途超时测试",
        html=None,
        is_edit=False,
        update_id=101,
        message_date_ts=now[0],
        length_limit=4096,
        reason="timeout",
    )
    assert task2 is not None
    now[0] += 2.0
    asyncio.run(
        queue2.process_once(
            lambda t: execute_queued_channel_task(context.application, t)
        )
    )
    assert bot.sent == []
    assert queue2.stats().completed == 0
    assert queue2.stats().expired >= 1


def test_long_html_post_recovers_segment_integrity_via_plain_fallback_and_keeps_blank_lines(
    monkeypatch, sample_db
):
    from wuwaterm_telegram.telegram_html import translator_supports_protected_context

    # Core 0.16.0 SentenceTranslator.translate_protected_async does not declare context yet.
    assert translator_supports_protected_context(SentenceTranslator(sample_db)) is False

    class _ContextCapableTranslator:
        async def translate_protected_async(
            self, protected, *, to_chinese=False, context=None, **kwargs
        ):
            return protected

    assert translator_supports_protected_context(_ContextCapableTranslator()) is True

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    protected_calls = [0]
    fallback_contexts: list[TranslationContext | None] = []

    async def fake_translate_protected_async(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        protected_calls[0] += 1
        if protected_calls[0] == 1:
            new_segments = list(protected.visible_segments)
            new_segments[1] = "First rich segment."
            return type(protected)(tuple(new_segments), protected.structures)
        # Second segment breaks placeholder integrity -> triggers budgeted plain fallback.
        raise LLMTranslationError("placeholder broken", reason="html_integrity")

    async def fake_translate_async(
        self,
        text,
        *,
        to_chinese=False,
        before_llm_call=None,
        propagate_errors=False,
        context=None,
        execution_budget=None,
    ):
        if before_llm_call is not None:
            before_llm_call()
        fallback_contexts.append(context)
        return "Second segment plain fallback <safe>."

    monkeypatch.setattr(
        SentenceTranslator, "translate_protected_async", fake_translate_protected_async
    )
    monkeypatch.setattr(SentenceTranslator, "translate_async", fake_translate_async)

    seg1_plain = "今汐在第一段展示技能。" * 130
    seg2_plain = "声骸在第二段提供效果。" * 110
    full_plain = f"{seg1_plain}\n\n{seg2_plain}"
    full_html = f"<b>{seg1_plain}</b>\n\n<i>{seg2_plain}</i>"
    assert len(full_plain) > LLM_INPUT_CHAR_LIMIT

    msg = _FakeMessage(5500, full_plain, text_html=full_html)
    update = SimpleNamespace(
        update_id=5500,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    context = _make_context(sample_db)
    asyncio.run(channel_post_handler(update, context))

    assert len(msg.replies) == 1
    delivered_text, parse_mode, _ = msg.replies[0]
    assert parse_mode == "HTML"
    assert validate_telegram_html(delivered_text)
    assert delivered_text == "<b>First rich segment.</b>\n\nSecond segment plain fallback &lt;safe&gt;."
    assert len(fallback_contexts) == 1
    assert fallback_contexts[0] is not None
    assert fallback_contexts[0].before is not None
    assert fallback_contexts[0].before.target == "First rich segment."


def test_subsequent_html_chunk_badrequest_rechecks_delivery_gate_before_plain_fallback(
    monkeypatch, sample_db
):
    from telegram.error import BadRequest

    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")

    part1 = "Chunk one text. " * 140
    part2 = "Chunk two text. " * 140

    async def fake_protected(
        self, protected, *, to_chinese=False, before_llm_call=None, execution_budget=None
    ):
        if before_llm_call is not None:
            before_llm_call()
        new_segments = list(protected.visible_segments)
        new_segments[1] = part1
        new_segments[3] = part2
        return type(protected)(tuple(new_segments), protected.structures)

    monkeypatch.setattr(SentenceTranslator, "translate_protected_async", fake_protected)

    context = _make_context(sample_db)
    settings = context.application.bot_data[CHAT_SETTINGS_KEY]

    class _RevokeOnSecondChunkMsg(_FakeMessage):
        async def reply_text(self, text: str, **kwargs):
            parse_mode = kwargs.get("parse_mode")
            if len(self.replies) == 0:
                return await super().reply_text(text, **kwargs)
            if parse_mode == "HTML":
                # Authorization revoked while chunk 2 HTML request was in flight.
                settings.disallow(-2001)
                raise BadRequest("Can't parse entities")
            # Plain fallback must never execute once authorization is revoked!
            return await super().reply_text(text, **kwargs)

    msg = _RevokeOnSecondChunkMsg(
        5600,
        text="第一段\n\n第二段",
        text_html="<b>第一段</b>\n\n<i>第二段</i>",
    )
    update = SimpleNamespace(
        update_id=5600,
        effective_message=msg,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    asyncio.run(channel_post_handler(update, context))

    assert len(msg.replies) == 1
    assert msg.replies[0][1] == "HTML"
