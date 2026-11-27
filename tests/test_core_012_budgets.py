"""Integration tests against the real pinned Core: execution budgets (since 0.12.0) & compat."""

from __future__ import annotations

import asyncio
import json
import re
from inspect import signature
from types import SimpleNamespace

import httpx
import pytest

from wuwaterm import llm_transport
from wuwaterm.application import (
    LLM_INPUT_CHAR_LIMIT,
    OperationExecutionBudget,
    SentenceTranslator,
    TRANSLATION_UNAVAILABLE_NOTICE,
    TermService,
    semantic_review_async,
    translate_request_async,
)
from wuwaterm_telegram.bot import (
    ADMIN_CACHE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    RATE_LIMITER_KEY,
    REJECT_LIMITER_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
    AdminStatusCache,
    BotConfig,
    ChannelReplyIndex,
    PerChatRateLimiter,
    TranslationRequest,
    telegram_translation_reply,
)
from wuwaterm_telegram.channel import channel_post_handler
from wuwaterm_telegram.core_compat import (
    CORE_VERSION,
    missing_review_symbols,
    verify_core_compatibility,
)
from wuwaterm_telegram.review_backend import (
    LiveReviewBackend,
    ReviewBackendError,
    _code_for_failure,
)
from wuwaterm_telegram.review_flow import ReviewAborted, run_review
from wuwaterm_telegram.review_model import PAIR_READY, PairInfo
from wuwaterm_telegram.settings import ChatSettings


def _configure_llm(monkeypatch: pytest.MonkeyPatch, *, review_enabled: bool = False) -> None:
    monkeypatch.setenv("WUWATERM_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("WUWATERM_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("WUWATERM_OPENAI_MODEL", "test-model")
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_EN_ZH", "auto")
    monkeypatch.setenv("WUWATERM_TRANSLATION_THINKING_ZH_EN", "disabled")
    if review_enabled:
        monkeypatch.setenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", "1")
    else:
        monkeypatch.delenv("WUWATERM_SEMANTIC_REVIEW_ENABLED", raising=False)


class _DummyMessage:
    def __init__(
        self,
        *,
        message_id: int = 4001,
        text: str | None = None,
        text_html: str | None = None,
    ) -> None:
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.text_html = text_html
        self.caption_html = None
        self.sender_chat = SimpleNamespace(id=-3001, type="channel")
        self.is_automatic_forward = True
        self.date = None
        self.edit_date = None
        self.replies: list[tuple[str, str | None, int | None]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(
            (text, kwargs.get("parse_mode"), kwargs.get("reply_to_message_id"))
        )
        return SimpleNamespace(message_id=self.message_id + 1000 + len(self.replies))


class _DummyBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int | None, str | None]] = []

    async def get_chat_member(self, chat_id: int, user_id: int):
        return SimpleNamespace(status="administrator")

    async def send_message(self, chat_id=None, text=None, **kwargs):
        self.sent_messages.append((chat_id, text))
        return SimpleNamespace(message_id=1)


def _channel_update(*, text: str, text_html: str | None = None, message_id: int = 4001):
    message = _DummyMessage(
        message_id=message_id,
        text=text,
        text_html=text_html if text_html is not None else text,
    )
    update = SimpleNamespace(
        update_id=message_id,
        effective_message=message,
        effective_chat=SimpleNamespace(id=-2001, type="supergroup"),
        effective_user=None,
    )
    return update, message


def _channel_context(
    sample_db,
    handler,
    *,
    calls_per_minute: int = 6,
):
    config = BotConfig(
        rate_limit_per_minute=10,
        owner_user_id=11,
        channel_llm_calls_per_minute=calls_per_minute,
    )
    chat_settings = ChatSettings(sample_db.parent / "chat_settings.json")
    chat_settings.allow(-2001)
    translator = SentenceTranslator(
        sample_db,
        llm_transport=httpx.MockTransport(handler),
        en_zh_protocol=True,
    )
    return SimpleNamespace(
        args=[],
        bot=_DummyBot(),
        application=SimpleNamespace(
            bot_data={
                SERVICE_KEY: TermService(sample_db),
                TRANSLATOR_KEY: translator,
                CONFIG_KEY: config,
                RATE_LIMITER_KEY: PerChatRateLimiter(limit=config.rate_limit_per_minute),
                REJECT_LIMITER_KEY: PerChatRateLimiter(limit=config.rate_limit_per_minute),
                ADMIN_CACHE_KEY: AdminStatusCache(),
                CHANNEL_REPLY_INDEX_KEY: ChannelReplyIndex(
                    ttl_seconds=config.channel_max_age_seconds
                ),
                CHAT_SETTINGS_KEY: chat_settings,
            }
        ),
    )


def _mock_chat_completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_core_compat_verifies_pinned_core_symbols() -> None:
    assert CORE_VERSION == "0.16.0"
    verify_core_compatibility()
    assert missing_review_symbols() == ()
    assert callable(llm_transport.current_execution_budget)
    assert "execution_budget" in signature(SentenceTranslator.translate_protected_async).parameters
    assert "execution_budget" in signature(SentenceTranslator.translate_async).parameters
    assert "execution_budget" in signature(translate_request_async).parameters
    assert "execution_budget" in signature(semantic_review_async).parameters
    budget = OperationExecutionBudget.for_segments(1)
    assert budget.max_total_calls == 2
    assert budget.max_corrective_calls == 1


def test_tr_rich_html_auto_fallback_capped_at_two_calls(monkeypatch, sample_db) -> None:
    _configure_llm(monkeypatch)
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            # Call 1 (HTML) breaks placeholders -> triggers plain fallback via try_consume_html_fallback().
            return _mock_chat_completion("Broken markup output without placeholders.")
        # Call 2 (plain fallback under auto/low thinking) times out; the corrective budget is
        # already spent on the HTML->plain fallback, so _retry_without_thinking must NOT make call 3.
        raise httpx.ReadTimeout("timeout", request=request)

    translator = SentenceTranslator(
        sample_db,
        llm_transport=httpx.MockTransport(handler),
        en_zh_protocol=True,
    )
    service = TermService(sample_db)
    reply = asyncio.run(
        telegram_translation_reply(
            service,
            translator,
            TranslationRequest(
                text="Gorges of Spirits has strong Echo today.",
                html="<b>Gorges of Spirits</b> has strong Echo today.",
                forced_to_chinese=True,
            ),
        )
    )
    assert len(seen) <= 2
    assert len(seen) == 2
    assert seen[1].get("thinking") != {"type": "disabled"}
    assert reply.text == TRANSLATION_UNAVAILABLE_NOTICE
    assert reply.parse_mode is None

    # When HTML breaks once, plain fallback succeeds cleanly within the 2-call budget.
    seen_recover: list[dict] = []

    def recover_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_recover.append(payload)
        if len(seen_recover) == 1:
            return _mock_chat_completion("Missing placeholders first time.")
        user_content = payload["messages"][-1]["content"]
        term_tokens = re.findall(r"__WUWA_TERM_[0-9a-f]{16}_[0-9]{4}__", user_content)
        return _mock_chat_completion(f"{term_tokens[0]}今天有很强的{term_tokens[1]}。")

    recover_translator = SentenceTranslator(
        sample_db,
        llm_transport=httpx.MockTransport(recover_handler),
        en_zh_protocol=True,
    )
    ok_reply = asyncio.run(
        telegram_translation_reply(
            service,
            recover_translator,
            TranslationRequest(
                text="Gorges of Spirits has strong Echo today.",
                html="<b>Gorges of Spirits</b> has strong Echo today.",
                forced_to_chinese=True,
            ),
        )
    )
    assert len(seen_recover) == 2
    assert ok_reply.parse_mode is None
    assert "云陵谷今天有很强的声骸。" in ok_reply.text


def test_channel_plain_single_segment_auto_never_blows_admission(monkeypatch, sample_db) -> None:
    _configure_llm(monkeypatch)
    seen: list[dict] = []

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        # Under auto/low thinking (<4 locks, <25 paragraphs), Core normally retries timeout
        # with thinking=disabled; OperationExecutionBudget(max_total_calls=1, max_corrective=0)
        # must suppress that retry so ChannelRuntime.reserve(1) is never blown.
        raise httpx.ReadTimeout("timeout", request=request)

    update, message = _channel_update(text="Gorges of Spirits has strong Echo today.")
    context = _channel_context(sample_db, timeout_handler, calls_per_minute=1)
    asyncio.run(channel_post_handler(update, context))

    assert len(seen) == 1
    assert seen[0].get("thinking") != {"type": "disabled"}
    assert message.replies == []

    # With a valid single response, calls_per_minute=1 translates cleanly.
    ok_seen: list[dict] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ok_seen.append(payload)
        user_content = payload["messages"][-1]["content"]
        term_tokens = re.findall(r"__WUWA_TERM_[0-9a-f]{16}_[0-9]{4}__", user_content)
        return _mock_chat_completion(f"{term_tokens[0]}今天有很强的{term_tokens[1]}。")

    ok_update, ok_message = _channel_update(
        text="Gorges of Spirits has strong Echo today.",
        message_id=4002,
    )
    ok_context = _channel_context(sample_db, ok_handler, calls_per_minute=1)
    asyncio.run(channel_post_handler(ok_update, ok_context))

    assert len(ok_seen) == 1
    assert len(ok_message.replies) == 1
    assert "云陵谷" in ok_message.replies[0][0]
    assert "声骸" in ok_message.replies[0][0]


def test_channel_html_auto_fallback_capped_at_two_calls(monkeypatch, sample_db) -> None:
    _configure_llm(monkeypatch)

    # Budget of 1: required_calls=1, allow_plain_fallback=False -> never makes a 2nd call,
    # whether Call 1 breaks HTML integrity or times out under auto/low thinking.
    seen_one_broken: list[dict] = []

    def broken_handler_one(request: httpx.Request) -> httpx.Response:
        seen_one_broken.append(json.loads(request.content))
        return _mock_chat_completion("broken without html or term placeholders")

    update1a, msg1a = _channel_update(
        text="Gorges of Spirits has strong Echo today.",
        text_html="<b>Gorges of Spirits</b> has strong Echo today.",
        message_id=4100,
    )
    ctx1a = _channel_context(sample_db, broken_handler_one, calls_per_minute=1)
    asyncio.run(channel_post_handler(update1a, ctx1a))
    assert len(seen_one_broken) == 1
    assert msg1a.replies == []

    seen_one_timeout: list[dict] = []

    def timeout_handler_one(request: httpx.Request) -> httpx.Response:
        seen_one_timeout.append(json.loads(request.content))
        raise httpx.ReadTimeout("timeout", request=request)

    update1b, msg1b = _channel_update(
        text="Gorges of Spirits has strong Echo today.",
        text_html="<b>Gorges of Spirits</b> has strong Echo today.",
        message_id=4101,
    )
    ctx1b = _channel_context(sample_db, timeout_handler_one, calls_per_minute=1)
    asyncio.run(channel_post_handler(update1b, ctx1b))
    assert len(seen_one_timeout) == 1
    assert seen_one_timeout[0].get("thinking") != {"type": "disabled"}
    assert msg1b.replies == []

    # Budget >= 2: required_calls=2, allow_plain_fallback=True -> HTML failure falls back to
    # plain, whose timeout must NOT trigger a 3rd corrective call.
    seen_two: list[dict] = []

    def html_then_timeout_handler(request: httpx.Request) -> httpx.Response:
        seen_two.append(json.loads(request.content))
        if len(seen_two) == 1:
            return _mock_chat_completion("broken without html or term placeholders")
        raise httpx.ReadTimeout("timeout", request=request)

    update2, msg2 = _channel_update(
        text="Gorges of Spirits has strong Echo today.",
        text_html="<b>Gorges of Spirits</b> has strong Echo today.",
        message_id=4102,
    )
    ctx2 = _channel_context(sample_db, html_then_timeout_handler, calls_per_minute=4)
    asyncio.run(channel_post_handler(update2, ctx2))
    assert len(seen_two) <= 2
    assert len(seen_two) == 2
    assert seen_two[1].get("thinking") != {"type": "disabled"}
    assert msg2.replies == []


def test_channel_long_split_post_with_html_disables_corrective_calls(
    monkeypatch, sample_db
) -> None:
    _configure_llm(monkeypatch)
    # Build > LLM_INPUT_CHAR_LIMIT text in 2 chunks where chunk 1 hard-locks "Gorges of Spirits".
    filler = "a " * ((LLM_INPUT_CHAR_LIMIT - 40) // 2)
    first_line = f"Gorges of Spirits is peaceful today. {filler}".strip()
    second_line = "Echo resonance looks steady across every zone."
    plain = f"{first_line}\n{second_line}"
    assert len(plain) > LLM_INPUT_CHAR_LIMIT

    seen_fail: list[dict] = []

    def timeout_first_chunk(request: httpx.Request) -> httpx.Response:
        seen_fail.append(json.loads(request.content))
        # Split post with HTML still forces max_corrective_calls=0, so chunk 1 timeout under
        # auto/low thinking must NOT trigger _retry_without_thinking.
        raise httpx.ReadTimeout("timeout", request=request)

    update_fail, msg_fail = _channel_update(
        text=plain,
        text_html=f"<b>{first_line}</b>\n<i>{second_line}</i>",
        message_id=4201,
    )
    ctx_fail = _channel_context(sample_db, timeout_first_chunk, calls_per_minute=10)
    asyncio.run(channel_post_handler(update_fail, ctx_fail))

    assert len(seen_fail) == 1
    assert seen_fail[0].get("thinking") != {"type": "disabled"}
    assert msg_fail.replies == []

    # When chunks keep placeholders valid, split executes 1 call per chunk and
    # preserves balanced HTML formatting instead of stripping everything to plain.
    seen_ok: list[dict] = []

    def ok_split_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_ok.append(payload)
        user_content = payload["messages"][-1]["content"]
        term_tokens = re.findall(r"__WUWA_TERM_[0-9a-f]{16}_[0-9]{4}__", user_content)
        html_tokens = re.findall(r"__WUWA_HTML_[0-9a-f]{16}_[0-9]{4}__", user_content)
        if len(html_tokens) == 2:
            return _mock_chat_completion(
                f"{html_tokens[0]}{term_tokens[0]}状态稳定。{html_tokens[1]}"
            )
        if len(html_tokens) == 4:
            return _mock_chat_completion(
                f"{html_tokens[0]}续段{html_tokens[1]}\n"
                f"{html_tokens[2]}{term_tokens[0]}状态稳定。{html_tokens[3]}"
            )
        return _mock_chat_completion(f"{term_tokens[0]}状态稳定。")

    update_ok, msg_ok = _channel_update(
        text=plain,
        text_html=f"<b>{first_line}</b>\n<i>{second_line}</i>",
        message_id=4202,
    )
    ctx_ok = _channel_context(sample_db, ok_split_handler, calls_per_minute=10)
    asyncio.run(channel_post_handler(update_ok, ctx_ok))

    assert len(seen_ok) == 2
    assert len(msg_ok.replies) == 1
    reply_text, reply_mode, _reply_to = msg_ok.replies[0]
    assert reply_mode == "HTML"
    assert "<b>云陵谷状态稳定。</b>" in reply_text
    assert "<i>声骸状态稳定。</i>" in reply_text


def test_review_run_budget_and_cancellation_boundaries(monkeypatch, sample_db) -> None:
    _configure_llm(monkeypatch, review_enabled=True)
    assert _code_for_failure("llm_unavailable", "operation_budget") == "llm_budget_exhausted"
    assert _code_for_failure("llm_unavailable", "cancelled") == "aborted"

    seen: list[dict] = []

    def ok_review_handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _mock_chat_completion(json.dumps({"issues": []}))

    translator = SentenceTranslator(
        sample_db,
        llm_transport=httpx.MockTransport(ok_review_handler),
        en_zh_protocol=True,
    )
    backend = LiveReviewBackend(TermService(sample_db), translator)

    # Pre-exhausted OperationExecutionBudget fails before any HTTP call.
    exhausted = OperationExecutionBudget(
        max_total_calls=1,
        max_corrective_calls=0,
        max_corrective_calls_per_segment=0,
    )
    with exhausted.call_attempt():
        pass
    assert exhausted.calls_used == 1
    assert exhausted.can_start_call is False
    with pytest.raises(ReviewBackendError) as caught_budget:
        asyncio.run(
            backend.semantic_review(
                "今汐来了。",
                "Jinhsi arrived.",
                direction="en",
                explain_language="zh",
                context_before=None,
                execution_budget=exhausted,
            )
        )
    assert caught_budget.value.code == "llm_budget_exhausted"
    assert seen == []

    # Pre-cancelled OperationExecutionBudget reduces to "aborted" without calling LLM.
    cancelled_budget = OperationExecutionBudget(
        max_total_calls=2,
        max_corrective_calls=0,
        max_corrective_calls_per_segment=0,
    )
    cancelled_budget.cancel("aborted")
    with pytest.raises(ReviewBackendError) as caught_abort:
        asyncio.run(
            backend.semantic_review(
                "今汐来了。",
                "Jinhsi arrived.",
                direction="en",
                explain_language="zh",
                context_before=None,
                execution_budget=cancelled_budget,
            )
        )
    assert caught_abort.value.code == "aborted"
    assert seen == []

    # Gate returning False on segment 2 cancels the run budget and raises ReviewAborted.
    captured_budgets: list[OperationExecutionBudget | None] = []

    class _AbortGateWrapper:
        def __init__(self, inner: LiveReviewBackend) -> None:
            self._inner = inner

        def semantic_available(self) -> bool:
            return self._inner.semantic_available()

        def plan_pairs(self, source: str, target: str) -> list[PairInfo]:
            return self._inner.plan_pairs(source, target)

        async def term_review(self, source: str, target: str, direction: str):
            return await self._inner.term_review(source, target, direction)

        async def semantic_review(
            self,
            source: str,
            target: str,
            *,
            direction: str,
            explain_language: str,
            context_before,
            execution_budget=None,
        ):
            captured_budgets.append(execution_budget)
            return await self._inner.semantic_review(
                source,
                target,
                direction=direction,
                explain_language=explain_language,
                context_before=context_before,
                execution_budget=execution_budget,
            )

    gate_calls = 0

    async def gate() -> bool:
        nonlocal gate_calls
        gate_calls += 1
        return gate_calls <= 3  # 2 term_review gates + segment 1 semantic gate -> abort on seg 2

    src = ("甲" * 1000) + "\n\n" + ("乙" * 1000)
    tgt = ("a" * 1000) + "\n\n" + ("b" * 1000)
    with pytest.raises(ReviewAborted):
        asyncio.run(run_review(_AbortGateWrapper(backend), src, tgt, "en", gate=gate))
    assert len(seen) == 1
    assert len(captured_budgets) == 1
    assert isinstance(captured_budgets[0], OperationExecutionBudget)
    assert captured_budgets[0].max_total_calls == 2
    assert captured_budgets[0].max_corrective_calls == 0
    assert captured_budgets[0].is_cancelled is True

    # asyncio.CancelledError also marks the run budget cancelled and propagates.
    cancel_budgets: list[OperationExecutionBudget | None] = []

    class _CancelledBackend:
        def semantic_available(self) -> bool:
            return True

        def plan_pairs(self, source: str, target: str) -> list[PairInfo]:
            return [PairInfo(0, PAIR_READY, False, 0, len(source), 0, len(source), 0, len(target), 0, len(target), 1800)]

        async def term_review(self, source: str, target: str, direction: str):
            return await backend.term_review(source, target, direction)

        async def semantic_review(
            self,
            source: str,
            target: str,
            *,
            direction: str,
            explain_language: str,
            context_before,
            execution_budget=None,
        ):
            cancel_budgets.append(execution_budget)
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_review(_CancelledBackend(), "甲", "a", "en"))
    assert len(cancel_budgets) == 1
    assert isinstance(cancel_budgets[0], OperationExecutionBudget)
    assert cancel_budgets[0].is_cancelled is True
