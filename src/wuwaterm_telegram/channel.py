"""Passive linked-channel auto-translation (HTML-preserving)."""

from __future__ import annotations

import asyncio
from contextlib import AbstractContextManager, nullcontext
import inspect
import logging
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape as html_escape
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, Literal

from telegram import Update
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut
from telegram.ext import ContextTypes

from wuwaterm import llm_transport
from wuwaterm.application import (
    ContextBefore,
    LLM_INPUT_CHAR_LIMIT,
    LLMTranslationError,
    OperationExecutionBudget,
    SentenceTranslator,
    TermService,
    TRANSLATION_UNAVAILABLE_NOTICE,
    TranslationContext,
    count_cjk,
    count_latin,
    llm_configured,
)
from wuwaterm.logging_utils import redact_id, safe_error_type, safe_text_len
from wuwaterm.segmentation import (
    SEGMENT_LIMIT,
    WHITESPACE as _SEGMENT_WHITESPACE,
    segment_text,
)

from .channel_checks import check_channel_translation
from .channel_queue import ChannelTask, ChannelTaskQueue, TaskExecutionOutcome
from .channel_reply_index import ChannelReplyIndex, OriginalPostClaim
from .channel_runtime import ChannelRuntime
from .core_backend import (
    HTTP_TERM_QUERY_MAX_CHARS,
    CoreBackendError,
    HttpCoreBackend,
)
from .core_compat import strip_telegram_wrapper_noise
from .runtime_keys import (
    CHANNEL_CAPACITY_NOTIFIER_KEY,
    CHANNEL_QUEUE_KEY,
    CHANNEL_REPLY_INDEX_KEY,
    CHANNEL_RUNTIME_KEY,
    CHAT_SETTINGS_KEY,
    CONFIG_KEY,
    CORE_BACKEND_KEY,
    SERVICE_KEY,
    TRANSLATOR_KEY,
)
from .settings import ChatSettings
from .telegram_html import (
    HTML_CONTENT_FAILURE_REASONS,
    normalize_telegram_html,
    split_telegram_html,
    split_telegram_html_segments,
    strip_telegram_html,
    telegram_html_has_tags,
    translate_html_async,
    translator_supports_protected_context,
    validate_telegram_html,
)
from .telegram_text import (
    TELEGRAM_TEXT_MESSAGE_LIMIT,
    split_telegram_text,
    telegram_text_units,
)
if TYPE_CHECKING:
    from .bot import BotConfig


LOGGER = logging.getLogger(__name__)
LONG_OUTPUT_MODE_SUFFIX = "plain-split"
LONG_HTML_OUTPUT_MODE_SUFFIX = "html-split"
CONTEXT_BEFORE_MAX_SCALARS = 400
FLOOD_RETRY_MAX_SLEEP_SECONDS = 60.0
# Capacity rejections the owner should hear about. Content gates (language,
# length, staleness, authorization) stay silent by design - they are policy,
# not degradation.
CAPACITY_SKIP_REASONS = frozenset({"queue_full", "llm_budget"})
# Transient failure reasons eligible for bounded background queue recovery,
# aligned with Core 0.16.0's wuwaterm.llm_transport reason vocabulary.
RECOVERABLE_CHANNEL_REASONS = frozenset(
    {
        "queue_full",
        "llm_budget",
        "timeout",
        "rate_limit",
        "upstream",
        "connect",
        "request",
        "network",
        "upstream_error",
        "service_unavailable",
        "translation_unavailable",
        "connection_error",
        "output_check_blocked",
    }
)


ChannelOutcomeKind = Literal[
    "delivered",
    "already_handled",
    "expired",
    "retryable_failure",
    "uncertain_delivery",
    "permanent_skip",
]


@dataclass(frozen=True)
class ChannelPostOutcome:
    """Explicit per-post outcome for queue recovery and observability."""

    kind: ChannelOutcomeKind
    reason: str = ""
# One DM per window no matter how large the burst; the message carries the
# suppressed count instead.
CAPACITY_NOTIFY_COOLDOWN_SECONDS = 600.0
# After a failed DM attempt the next skip retries sooner than the full
# cooldown, so a transient Telegram blip cannot silence the alert for the
# whole window; still bounded so a persistent failure (e.g. owner never
# started the bot) does not cost one API call per dropped post.
CAPACITY_NOTIFY_FAILURE_RETRY_SECONDS = 60.0
# Headroom an edit must leave unused so a burst of edits cannot starve the
# next new post: a yielded edit only delays refreshing an already-delivered
# translation, a rejected new post means no translation appears at all.
EDIT_BUDGET_HEADROOM_CALLS = 2


class CapacitySkipNotifier:
    """Rate-limited owner DM bookkeeping for capacity-driven channel skips.

    A post dropped because the queue or LLM budget is full is invisible from
    the channel itself: nothing appears and nobody complains to the bot. The
    DM is capped at one per cooldown window; ``note_skip`` returns the
    per-reason counts since the previous notification so a burst produces one
    message that says how much was dropped instead of one message per dropped
    post. The cooldown and pending counts are committed only when the DM
    actually sends (``mark_result``): otherwise a transient send failure
    would both suppress alerts for the rest of the window and lose the failed
    counts.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = CAPACITY_NOTIFY_COOLDOWN_SECONDS,
        failure_retry_seconds: float = CAPACITY_NOTIFY_FAILURE_RETRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._failure_retry_seconds = failure_retry_seconds
        self._clock = clock
        self._last_attempt_at: float | None = None
        self._last_attempt_ok = True
        self._pending_counts: Counter[str] = Counter()

    def note_skip(self, reason: str) -> tuple[bool, dict[str, int]]:
        """Record a skip; returns (should attempt a DM now, counts snapshot).

        The snapshot is a copy: skips accumulating per reason while the DM is
        in flight must not leak into it.
        """
        self._pending_counts[reason] += 1
        now = self._clock()
        if self._last_attempt_at is not None:
            cooldown = (
                self._cooldown_seconds
                if self._last_attempt_ok
                else self._failure_retry_seconds
            )
            if now - self._last_attempt_at < cooldown:
                return False, dict(self._pending_counts)
        self._last_attempt_at = now
        return True, dict(self._pending_counts)

    def mark_result(
        self, sent: bool, *, counted: dict[str, int] | None = None
    ) -> None:
        """Commit the outcome of a DM attempt.

        On success only the skips captured in that DM are cleared: skips that
        arrived while ``send_message`` was in flight stay pending and fold
        into the next notice instead of being silently zeroed.
        """
        self._last_attempt_ok = sent
        if sent and counted:
            for reason, amount in counted.items():
                remaining = self._pending_counts.get(reason, 0) - amount
                if remaining > 0:
                    self._pending_counts[reason] = remaining
                else:
                    self._pending_counts.pop(reason, None)


def _capacity_notifier(context: ContextTypes.DEFAULT_TYPE) -> CapacitySkipNotifier:
    notifier = context.application.bot_data.get(CHANNEL_CAPACITY_NOTIFIER_KEY)
    if isinstance(notifier, CapacitySkipNotifier):
        return notifier
    notifier = CapacitySkipNotifier()
    context.application.bot_data[CHANNEL_CAPACITY_NOTIFIER_KEY] = notifier
    return notifier


async def _notify_owner_capacity_skip(
    context: ContextTypes.DEFAULT_TYPE, config: "BotConfig", reason: str
) -> None:
    """DM the owner when a channel post is dropped for capacity reasons.

    The notice carries counts and internal reason vocabulary only - never
    post text - and its own delivery failure (owner never started the bot,
    network error) must not break the handler, so it is swallowed into the
    log with the same redaction rules as everything else.
    """
    if reason not in CAPACITY_SKIP_REASONS:
        return
    owner_user_id = getattr(config, "owner_user_id", None)
    if not owner_user_id:
        return
    notifier = _capacity_notifier(context)
    should_notify, counts = notifier.note_skip(reason)
    if not should_notify:
        return
    total = sum(counts.values())
    # The aggregate may span several reasons (cooldown or failure-retry
    # windows); attribute each count to its own reason instead of labelling
    # everything with the reason that happened to trigger this notice.
    breakdown = "、".join(f"{name} ×{n}" for name, n in sorted(counts.items()))
    text = (
        f"频道自动翻译因容量限制跳过了 {total} 条帖子（{breakdown}）。"
        "频道内不会显示任何提示，请关注交付情况；"
        "可用 /status 查看计数，需要更高吞吐时调大 "
        "WUWATERM_CHANNEL_LLM_CALLS_PER_MINUTE 或 WUWATERM_CHANNEL_MAX_PENDING。"
    )
    try:
        await context.bot.send_message(chat_id=owner_user_id, text=text)
    except Exception as exc:
        # Counts and short retry re-arm stay uncommitted so the next skip
        # retries with the failed skips folded into the eventual message.
        notifier.mark_result(False)
        LOGGER.info(
            "channel capacity notice to owner failed: %s", safe_error_type(exc)
        )
        return
    notifier.mark_result(True, counted=counts)


async def send_with_flood_retry(send_callable, *, retry_gate=None):
    """Run one Telegram send/edit; on RetryAfter, wait once and retry.

    A 429 flood-wait guarantees the request was NOT executed, so a single
    retry cannot duplicate (unlike TimedOut/NetworkError, which may have
    succeeded server-side and are deliberately not retried). Granularity is
    ONE API call: retrying a whole multi-chunk emit would resend chunks that
    were already delivered. A second RetryAfter propagates to the caller's
    normal error handling.

    ``retry_gate`` (sync or async, returning truthy to proceed) re-checks
    volatile policy after the sleep: authorization can be revoked and posts
    can age out during a flood-wait, and the retry would otherwise be the
    first actual API call. A closed gate re-raises the original RetryAfter,
    which flows into the caller's existing delivery-failure handling —
    logged, silent, and never delivered.
    """
    try:
        return await send_callable()
    except RetryAfter as exc:
        raw_delay = getattr(exc, "retry_after", None)
        if hasattr(raw_delay, "total_seconds"):  # PTB >=23 returns timedelta
            raw_delay = raw_delay.total_seconds()
        requested = max(float(raw_delay) if raw_delay is not None else 1.0, 0.0)
        # Bounded-latency trade-off: a wait beyond the cap retries early and
        # will likely re-flood (the second RetryAfter then propagates). Log
        # both values so a log review can tell "waited fully" from "gave up".
        delay = min(requested, FLOOD_RETRY_MAX_SLEEP_SECONDS)
        LOGGER.warning(
            "telegram flood wait; retrying send in %.1fs (requested %.1fs)",
            delay,
            requested,
        )
        await asyncio.sleep(delay)
        if retry_gate is not None:
            allowed = retry_gate()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                LOGGER.warning("flood-wait retry aborted: delivery gate closed")
                raise
        return await send_callable()


def _channel_runtime(
    context: ContextTypes.DEFAULT_TYPE, config: "BotConfig"
) -> ChannelRuntime:
    runtime = context.application.bot_data.get(CHANNEL_RUNTIME_KEY)
    if isinstance(runtime, ChannelRuntime):
        return runtime
    runtime = ChannelRuntime(
        max_active=config.llm_max_concurrency,
        max_pending=config.channel_max_pending,
        llm_calls_per_minute=config.channel_llm_calls_per_minute,
    )
    context.application.bot_data[CHANNEL_RUNTIME_KEY] = runtime
    return runtime


def _channel_queue(
    context: ContextTypes.DEFAULT_TYPE, config: "BotConfig"
) -> ChannelTaskQueue:
    queue = context.application.bot_data.get(CHANNEL_QUEUE_KEY)
    if isinstance(queue, ChannelTaskQueue):
        return queue
    queue = ChannelTaskQueue(
        max_size=getattr(config, "channel_queue_max_size", 128),
        ttl_seconds=float(
            getattr(config, "channel_queue_ttl_seconds", 3600)
        ),
        post_max_age_seconds=float(config.channel_max_age_seconds),
        max_attempts=getattr(config, "channel_queue_max_attempts", 5),
    )
    context.application.bot_data[CHANNEL_QUEUE_KEY] = queue
    return queue


def _http_core_backend(context: ContextTypes.DEFAULT_TYPE) -> HttpCoreBackend | None:
    backend = context.application.bot_data.get(CORE_BACKEND_KEY)
    return backend if isinstance(backend, HttpCoreBackend) else None


def _queue_deadline_expired(context: ContextTypes.DEFAULT_TYPE) -> bool:
    deadline = getattr(context, "_queue_expires_at", None)
    if deadline is None:
        return False
    clock = getattr(context, "_queue_clock", time.time)
    return clock() >= deadline


def _enqueue_for_recovery(
    context: ContextTypes.DEFAULT_TYPE,
    config: "BotConfig",
    *,
    message,
    chat_id: int | None,
    chat_type: str,
    plain: str,
    html_text: str | None,
    is_edit: bool,
    update: Update,
    length_limit: int,
    reason: str,
) -> None:
    if chat_id is None or getattr(context, "_from_channel_queue", False):
        return
    if reason not in RECOVERABLE_CHANNEL_REASONS:
        return
    queue = _channel_queue(context, config)
    message_date = getattr(message, "date", None)
    message_date_ts = message_date.timestamp() if message_date is not None else None
    update_id = getattr(update, "update_id", None)
    queue.enqueue(
        chat_id=chat_id,
        message_id=message.message_id,
        chat_type=chat_type,
        plain=plain,
        html=html_text,
        is_edit=is_edit,
        update_id=update_id if isinstance(update_id, int) else None,
        message_date_ts=message_date_ts,
        length_limit=length_limit,
        reason=reason,
    )


def _tail_context(text: str, limit: int = CONTEXT_BEFORE_MAX_SCALARS) -> str:
    return text[-limit:] if len(text) > limit else text


def _strip_segment_separators(text: str) -> str:
    start, end = 0, len(text)
    while start < end and text[start] in _SEGMENT_WHITESPACE:
        start += 1
    while end > start and text[end - 1] in _SEGMENT_WHITESPACE:
        end -= 1
    return text[start:end]


def _plan_long_channel_segments(
    plain: str, html_text: str | None
) -> tuple[list[tuple[str, str, str]] | None, list[object] | None, int]:
    """Plan segmented calls for posts longer than ``LLM_INPUT_CHAR_LIMIT``.

    Returns ``(html_segments, plain_segments, base_calls)``. Exactly one of
    ``html_segments`` and ``plain_segments`` is non-None. Each HTML segment is
    ``(leading_ws, core_html, trailing_ws)`` so inter-segment blank lines are
    preserved verbatim across translation.
    """
    if telegram_html_has_tags(html_text):
        assert html_text is not None
        html_segments = split_telegram_html_segments(
            html_text, limit=SEGMENT_LIMIT
        )
        if html_segments:
            non_empty = sum(1 for _l, core, _t in html_segments if core)
            return html_segments, None, max(1, non_empty)
    plain_segments = segment_text(plain, limit=SEGMENT_LIMIT)
    non_empty = sum(1 for seg in plain_segments if seg.core_start < seg.core_end)
    return None, plain_segments, max(1, non_empty)


def _channel_event(
    runtime: ChannelRuntime,
    *,
    stage: str,
    reason: str,
    message=None,
    chat_id: int | None = None,
    is_edit: bool = False,
    direction: str = "unknown",
    text_len: int = 0,
    mode: str = "none",
) -> None:
    runtime.record(stage, reason)
    snapshot = runtime.snapshot()
    log = (
        LOGGER.warning
        if stage in {"llm", "delivery"} and reason not in {"started", "success"}
        else LOGGER.info
    )
    log(
        "channel_translation stage=%s reason=%s chat=%s incoming_message=%s "
        "edit=%s direction=%s text_len=%s mode=%s active=%s pending=%s high_water=%s",
        stage,
        reason,
        redact_id(chat_id),
        redact_id(getattr(message, "message_id", None)),
        is_edit,
        direction,
        text_len,
        mode,
        snapshot.active,
        snapshot.pending,
        snapshot.high_water,
    )


def _message_is_fresh(message, max_age_seconds: int) -> bool:
    message_date = getattr(message, "date", None)
    if message_date is None:
        return True
    return (
        datetime.now(timezone.utc) - message_date
    ).total_seconds() <= max_age_seconds


async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate automatic forwards from the linked channel, in-thread.

    Every early exit is silent: this path never posts notices under
    channel posts (a notice comment under every post would be spam).

    An edited post updates the bot's existing reply for that post in place
    (tracked per (chat, post) in memory). If a fresh edit has no tracked reply,
    handle it as the first translatable version of that post; Telegram can
    deliver media posts before their caption/text is available.
    """
    await _handle_channel_post(update, context)


async def _handle_channel_post(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> ChannelPostOutcome:
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    http_backend = _http_core_backend(context)
    message = update.effective_message
    if message is None:
        _channel_event(runtime, stage="skipped", reason="no_message")
        return ChannelPostOutcome("permanent_skip", "no_message")
    chat = update.effective_chat
    chat_id = chat.id if chat else None
    chat_type = chat.type if chat else "unknown"
    is_edit = getattr(message, "edit_date", None) is not None
    initial_text = message.text or message.caption or ""
    _channel_event(
        runtime,
        stage="received",
        reason="update",
        message=message,
        chat_id=chat_id,
        is_edit=is_edit,
        text_len=safe_text_len(initial_text),
    )
    if not config.channel_autotranslate:
        _channel_event(
            runtime,
            stage="skipped",
            reason="disabled",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return ChannelPostOutcome("permanent_skip", "disabled")
    message_date = getattr(message, "date", None)
    if _queue_deadline_expired(context) or not _message_is_fresh(
        message, config.channel_max_age_seconds
    ):
        _channel_event(
            runtime,
            stage="skipped",
            reason="stale",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return ChannelPostOutcome("expired", "stale")

    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    if chat_id is None or not settings.is_allowed(chat_id):
        _channel_event(
            runtime,
            stage="skipped",
            reason="not_authorized",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(initial_text),
        )
        return ChannelPostOutcome("expired", "not_authorized")

    reply_index: ChannelReplyIndex = context.application.bot_data[
        CHANNEL_REPLY_INDEX_KEY
    ]
    resume_observed = False
    existing_reply_id: int | None = None
    if is_edit:
        existing_reply_id = (
            reply_index.get(chat_id, message.message_id)
            if chat_id is not None
            else None
        )
        if chat_id is not None:
            waited_for_original = await reply_index.wait_in_flight(
                chat_id, message.message_id
            )
            if not waited_for_original and existing_reply_id is None:
                await asyncio.sleep(0)
                waited_for_original = await reply_index.wait_in_flight(
                    chat_id, message.message_id
                )
            if waited_for_original:
                existing_reply_id = reply_index.get(chat_id, message.message_id)
        if existing_reply_id is None:
            if message_date is None:
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="edit_untracked_no_date",
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text_len=safe_text_len(initial_text),
                )
                return ChannelPostOutcome("permanent_skip", "edit_untracked_no_date")
            if chat_id is None or not reply_index.was_observed_without_reply(
                chat_id, message.message_id
            ):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="edit_untracked",
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text_len=safe_text_len(initial_text),
                )
                return ChannelPostOutcome("permanent_skip", "edit_untracked")
            LOGGER.info(
                "channel autotranslate treating fresh edit as new post "
                "incoming_message=%s",
                redact_id(message.message_id),
            )
            is_edit = False
            resume_observed = True
    edit_work_token: int | None = None
    original_claim: OriginalPostClaim | None = None
    resuming_partial = False
    if not is_edit and chat_id is not None:
        from_queue = bool(getattr(context, "_from_channel_queue", False))
        if from_queue:
            resume_observed = True
        claim = reply_index.claim_original(
            chat_id,
            message.message_id,
            resume_observed=resume_observed,
            resume_partial=from_queue,
        )
        if claim.role == "done":
            _channel_event(
                runtime,
                stage="skipped",
                reason="duplicate",
                message=message,
                chat_id=chat_id,
                text_len=safe_text_len(initial_text),
            )
            return ChannelPostOutcome("already_handled", "duplicate")
        if claim.role == "partial":
            _channel_event(
                runtime,
                stage="delivery",
                reason="partial_delivery",
                message=message,
                chat_id=chat_id,
                text_len=safe_text_len(initial_text),
            )
            return ChannelPostOutcome("uncertain_delivery", "partial_delivery")
        if claim.role == "waiter":
            await reply_index.wait_for_original(claim)
            _channel_event(
                runtime,
                stage="skipped",
                reason="duplicate_in_flight",
                message=message,
                chat_id=chat_id,
                text_len=safe_text_len(initial_text),
            )
            return ChannelPostOutcome("already_handled", "duplicate_in_flight")
        if claim.role == "partial_producer":
            resuming_partial = True
            existing_reply_id = reply_index.get(chat_id, message.message_id)
        original_claim = claim
    try:
        if message.text:
            plain = message.text
            html_text = message.text_html
            length_limit = config.channel_text_limit
        elif message.caption:
            plain = message.caption
            html_text = message.caption_html
            length_limit = config.channel_caption_limit
        else:
            _channel_event(
                runtime,
                stage="skipped",
                reason="no_text",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
            )
            return ChannelPostOutcome("permanent_skip", "no_text")
        if not html_text:
            _channel_event(
                runtime,
                stage="received",
                reason="no_html_plain_fallback",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(plain),
            )
            html_text = None
        else:
            html_text = normalize_telegram_html(html_text)
        pipeline_plain = strip_telegram_wrapper_noise(plain, keep_paragraphs=True)
        cjk = count_cjk(pipeline_plain)
        if pipeline_plain and cjk >= config.channel_min_cjk:
            to_chinese = False
        elif (
            pipeline_plain
            and cjk == 0
            and count_latin(pipeline_plain) >= config.channel_min_latin
        ):
            to_chinese = True
        else:
            _channel_event(
                runtime,
                stage="skipped",
                reason="language_threshold",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(plain),
            )
            return ChannelPostOutcome("permanent_skip", "language_threshold")
        direction = "to_zh" if to_chinese else "to_en"
        if len(plain) > length_limit:
            _channel_event(
                runtime,
                stage="skipped",
                reason="too_long",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return ChannelPostOutcome("permanent_skip", "too_long")

        official: str | None = None
        if http_backend is not None:
            if (
                len(pipeline_plain) <= HTTP_TERM_QUERY_MAX_CHARS
                and "\n" not in pipeline_plain
                and "\r" not in pipeline_plain
            ):
                try:
                    official = await http_backend.lookup_exact(
                        pipeline_plain, to_chinese=to_chinese
                    )
                except CoreBackendError as exc:
                    if exc.code in {"invalid_request", "input_too_long"}:
                        official = None
                    else:
                        reason = (
                            "upstream" if exc.code == "llm_unavailable" else exc.code
                        )
                        _channel_event(
                            runtime,
                            stage="llm",
                            reason=reason,
                            message=message,
                            chat_id=chat_id,
                            is_edit=is_edit,
                            direction=direction,
                            text_len=safe_text_len(plain),
                            mode="http-lookup",
                        )
                        _enqueue_for_recovery(
                            context,
                            config,
                            message=message,
                            chat_id=chat_id,
                            chat_type=chat_type,
                            plain=plain,
                            html_text=html_text,
                            is_edit=is_edit,
                            update=update,
                            length_limit=length_limit,
                            reason=reason,
                        )
                        return ChannelPostOutcome("retryable_failure", reason)
        else:
            service: TermService = context.application.bot_data[SERVICE_KEY]
            lookup_result = service.lookup_exact(pipeline_plain)
            if lookup_result.exact and lookup_result.best:
                official = (
                    lookup_result.best.entry.zh
                    if to_chinese
                    else lookup_result.best.entry.en
                )

        if not official:
            if http_backend is not None:
                try:
                    remote_llm_ok = await http_backend.llm_available()
                except CoreBackendError as exc:
                    reason = "upstream" if exc.code == "llm_unavailable" else exc.code
                    _channel_event(
                        runtime,
                        stage="llm",
                        reason=reason,
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                        mode="http-probe",
                    )
                    _enqueue_for_recovery(
                        context,
                        config,
                        message=message,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        plain=plain,
                        html_text=html_text,
                        is_edit=is_edit,
                        update=update,
                        length_limit=length_limit,
                        reason=reason,
                    )
                    return ChannelPostOutcome("retryable_failure", reason)
                if not remote_llm_ok:
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="llm_not_configured",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return ChannelPostOutcome("permanent_skip", "llm_not_configured")
            elif not llm_configured():
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="llm_not_configured",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return ChannelPostOutcome("permanent_skip", "llm_not_configured")

        if official:
            if not _passes_channel_delivery_gate(context, chat_id):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="authorization_changed",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return ChannelPostOutcome("expired", "authorization_changed")
            if is_edit and chat_id is not None:
                update_id = getattr(update, "update_id", None)
                edit_work_token = reply_index.begin_edit(
                    chat_id,
                    message.message_id,
                    update_id if isinstance(update_id, int) else None,
                )
            _channel_event(
                runtime,
                stage="dictionary",
                reason="exact_hit",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
                mode="dictionary",
            )
            delivery = await _emit(
                context,
                reply_index=reply_index,
                message=message,
                chat_id=chat_id,
                chat_type=chat_type,
                is_edit=is_edit,
                existing_reply_id=existing_reply_id,
                edit_work_token=edit_work_token,
                text=official,
                parse_mode=None,
                mode="dictionary",
            )
            if (
                delivery.kind == "delivered"
                and chat_id is not None
                and not getattr(context, "_from_channel_queue", False)
            ):
                existing_queue = context.application.bot_data.get(CHANNEL_QUEUE_KEY)
                if isinstance(existing_queue, ChannelTaskQueue):
                    update_id = getattr(update, "update_id", None)
                    existing_queue.cancel_pending(
                        chat_id,
                        message.message_id,
                        update_id=update_id if isinstance(update_id, int) else None,
                    )
            return delivery

        planned_html_segments: list[tuple[str, str, str]] | None = None
        planned_plain_segments: list[object] | None = None
        if len(pipeline_plain) <= LLM_INPUT_CHAR_LIMIT:
            required_calls = (
                min(2, runtime.llm_calls_per_minute) if html_text else 1
            )
            allow_plain_fallback = bool(html_text) and required_calls >= 2
            max_corrective_calls = 1 if allow_plain_fallback else 0
            max_corrective_per_segment = 1 if allow_plain_fallback else 0
        else:
            (
                planned_html_segments,
                planned_plain_segments,
                base_calls,
            ) = _plan_long_channel_segments(pipeline_plain, html_text)
            if planned_html_segments is not None:
                # Reserve at most 1 shared HTML->plain fallback call when per-minute
                # budget permits, while keeping per-segment timeout/paragraph
                # corrective retries disabled (max_corrective_calls_per_segment=0).
                fallback_slot = (
                    1 if runtime.llm_calls_per_minute > base_calls else 0
                )
                required_calls = base_calls + fallback_slot
                allow_plain_fallback = fallback_slot >= 1
                max_corrective_calls = fallback_slot
                max_corrective_per_segment = 0
            else:
                required_calls = base_calls
                allow_plain_fallback = False
                max_corrective_calls = 0
                max_corrective_per_segment = 0
        if is_edit and (
            runtime.budget_remaining()
            < min(
                required_calls + EDIT_BUDGET_HEADROOM_CALLS,
                runtime.llm_calls_per_minute,
            )
        ):
            _channel_event(
                runtime,
                stage="skipped",
                reason="edit_yield",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return ChannelPostOutcome("permanent_skip", "edit_yield")
        admission, rejection_reason = runtime.reserve(required_calls)
        if admission is None:
            resolved_rejection = rejection_reason or "admission_rejected"
            _channel_event(
                runtime,
                stage="skipped",
                reason=resolved_rejection,
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            _enqueue_for_recovery(
                context,
                config,
                message=message,
                chat_id=chat_id,
                chat_type=chat_type,
                plain=plain,
                html_text=html_text,
                is_edit=is_edit,
                update=update,
                length_limit=length_limit,
                reason=resolved_rejection,
            )
            await _notify_owner_capacity_skip(
                context, config, resolved_rejection
            )
            return ChannelPostOutcome("retryable_failure", resolved_rejection)
        translator: SentenceTranslator | None = (
            None
            if http_backend is not None
            else context.application.bot_data[TRANSLATOR_KEY]
        )
        async with admission:
            if _queue_deadline_expired(context) or not _message_is_fresh(
                message, config.channel_max_age_seconds
            ):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="stale_after_wait",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return ChannelPostOutcome("expired", "stale_after_wait")
            if not _passes_channel_delivery_gate(context, chat_id):
                _channel_event(
                    runtime,
                    stage="skipped",
                    reason="authorization_changed_after_wait",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                )
                return ChannelPostOutcome("expired", "authorization_changed_after_wait")
            started_calls = 0

            def mark_channel_llm_call_started() -> None:
                """Recheck volatile policy after the shared LLM-slot wait."""

                nonlocal started_calls, edit_work_token

                if _queue_deadline_expired(context) or not _message_is_fresh(
                    message, config.channel_max_age_seconds
                ):
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE,
                        reason="stale_before_llm",
                    )
                if not _passes_channel_delivery_gate(context, chat_id):
                    raise LLMTranslationError(
                        TRANSLATION_UNAVAILABLE_NOTICE,
                        reason="authorization_changed_before_llm",
                    )
                admission.mark_call_started()
                if started_calls == 0 and is_edit and chat_id is not None:
                    update_id = getattr(update, "update_id", None)
                    edit_work_token = reply_index.begin_edit(
                        chat_id,
                        message.message_id,
                        update_id if isinstance(update_id, int) else None,
                    )
                started_calls += 1
                _channel_event(
                    runtime,
                    stage="llm",
                    reason="started",
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                    mode=f"call:{started_calls}/{required_calls}",
                )

            op_budget = OperationExecutionBudget(
                max_total_calls=required_calls,
                max_corrective_calls=max_corrective_calls,
                max_corrective_calls_per_segment=max_corrective_per_segment,
            )
            try:
                with op_budget.activate():
                    (
                        translated,
                        translated_parse_mode,
                        translated_mode,
                    ) = await _translate_channel_input(
                        translator,
                        pipeline_plain,
                        html_text,
                        to_chinese=to_chinese,
                        before_llm_call=mark_channel_llm_call_started,
                        allow_plain_fallback=allow_plain_fallback,
                        execution_budget=op_budget,
                        planned_html_segments=planned_html_segments,
                        planned_plain_segments=planned_plain_segments,
                        http_backend=http_backend,
                    )
            except LLMTranslationError as exc:
                reason = getattr(exc, "reason", "translation_unavailable")
                _channel_event(
                    runtime,
                    stage=(
                        "skipped"
                        if reason
                        in {
                            "stale_before_llm",
                            "authorization_changed_before_llm",
                        }
                        else "llm"
                    ),
                    reason=reason,
                    message=message,
                    chat_id=chat_id,
                    is_edit=is_edit,
                    direction=direction,
                    text_len=safe_text_len(plain),
                    mode=getattr(exc, "failure_stage", "none"),
                )
                if reason in {
                    "stale_before_llm",
                    "authorization_changed_before_llm",
                }:
                    return ChannelPostOutcome("expired", reason)
                _enqueue_for_recovery(
                    context,
                    config,
                    message=message,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    plain=plain,
                    html_text=html_text,
                    is_edit=is_edit,
                    update=update,
                    length_limit=length_limit,
                    reason=reason,
                )
                if reason in RECOVERABLE_CHANNEL_REASONS:
                    return ChannelPostOutcome("retryable_failure", reason)
                return ChannelPostOutcome("permanent_skip", reason)
            _channel_event(
                runtime,
                stage="llm",
                reason="success",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
                mode=translated_mode,
            )

        if translated_parse_mode == "HTML":
            translated = normalize_telegram_html(translated)
        if translated_parse_mode == "HTML" and not validate_telegram_html(translated):
            _channel_event(
                runtime,
                stage="llm",
                reason="invalid_html",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            translated = strip_telegram_html(translated)
            translated_parse_mode = None
            translated_mode = "plain-after-invalid-html"

        output_check = check_channel_translation(
            pipeline_plain,
            translated,
            parse_mode=translated_parse_mode,
            to_chinese=to_chinese,
        )
        runtime.record("output_check", output_check.severity)
        for rule_id in output_check.rule_counts:
            runtime.record("output_check_rule", rule_id)
        if output_check.action == "block":
            _channel_event(
                runtime,
                stage="output_check",
                reason="blocked",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(translated),
                mode=output_check.rule_summary(),
            )
            _enqueue_for_recovery(
                context,
                config,
                message=message,
                chat_id=chat_id,
                chat_type=chat_type,
                plain=plain,
                html_text=html_text,
                is_edit=is_edit,
                update=update,
                length_limit=length_limit,
                reason="output_check_blocked",
            )
            return ChannelPostOutcome("retryable_failure", "output_check_blocked")
        if output_check.action == "deliver_with_warning":
            LOGGER.info(
                "channel_output_check severity=warning rules=%s chat=%s "
                "incoming_message=%s src_paras=%s tgt_paras=%s",
                output_check.rule_summary(),
                redact_id(chat_id),
                redact_id(getattr(message, "message_id", None)),
                output_check.source_paragraphs,
                output_check.target_paragraphs,
            )

        update_id = getattr(update, "update_id", None)
        resolved_update_id = update_id if isinstance(update_id, int) else None

        def _finalize_delivery(outcome: ChannelPostOutcome) -> ChannelPostOutcome:
            if chat_id is None or getattr(context, "_from_channel_queue", False):
                return outcome
            if outcome.kind == "delivered":
                existing_queue = context.application.bot_data.get(CHANNEL_QUEUE_KEY)
                if isinstance(existing_queue, ChannelTaskQueue):
                    existing_queue.cancel_pending(
                        chat_id,
                        message.message_id,
                        update_id=resolved_update_id,
                    )
            elif outcome.kind == "retryable_failure":
                _enqueue_for_recovery(
                    context,
                    config,
                    message=message,
                    chat_id=chat_id,
                    chat_type=chat_type,
                    plain=plain,
                    html_text=html_text,
                    is_edit=is_edit,
                    update=update,
                    length_limit=length_limit,
                    reason="rate_limit",
                )
            return outcome

        emit_as_edit = is_edit or resuming_partial
        if translated_parse_mode == "HTML":
            plain_translated = strip_telegram_html(translated)
            if (
                telegram_text_units(plain_translated) > TELEGRAM_TEXT_MESSAGE_LIMIT
                and not telegram_html_has_tags(translated)
            ):
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return ChannelPostOutcome("expired", "authorization_changed_before_delivery")
                return _finalize_delivery(
                    await _emit(
                        context,
                        reply_index=reply_index,
                        message=message,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        is_edit=emit_as_edit,
                        existing_reply_id=existing_reply_id,
                        edit_work_token=edit_work_token,
                        text=plain_translated,
                        parse_mode=None,
                        mode=translated_mode,
                    )
                )
            try:
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return ChannelPostOutcome("expired", "authorization_changed_before_delivery")
                return _finalize_delivery(
                    await _emit(
                        context,
                        reply_index=reply_index,
                        message=message,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        is_edit=emit_as_edit,
                        existing_reply_id=existing_reply_id,
                        edit_work_token=edit_work_token,
                        text=translated,
                        parse_mode="HTML",
                        mode=translated_mode,
                    )
                )
            except BadRequest:
                if not _passes_channel_delivery_gate(context, chat_id):
                    _channel_event(
                        runtime,
                        stage="skipped",
                        reason="authorization_changed_before_delivery",
                        message=message,
                        chat_id=chat_id,
                        is_edit=is_edit,
                        direction=direction,
                        text_len=safe_text_len(plain),
                    )
                    return ChannelPostOutcome("expired", "authorization_changed_before_delivery")
                return _finalize_delivery(
                    await _emit(
                        context,
                        reply_index=reply_index,
                        message=message,
                        chat_id=chat_id,
                        chat_type=chat_type,
                        is_edit=emit_as_edit,
                        existing_reply_id=existing_reply_id,
                        edit_work_token=edit_work_token,
                        text=plain_translated,
                        parse_mode=None,
                        mode="plain-after-badrequest",
                    )
                )
        if not _passes_channel_delivery_gate(context, chat_id):
            _channel_event(
                runtime,
                stage="skipped",
                reason="authorization_changed_before_delivery",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                direction=direction,
                text_len=safe_text_len(plain),
            )
            return ChannelPostOutcome("expired", "authorization_changed_before_delivery")
        return _finalize_delivery(
            await _emit(
                context,
                reply_index=reply_index,
                message=message,
                chat_id=chat_id,
                chat_type=chat_type,
                is_edit=emit_as_edit,
                existing_reply_id=existing_reply_id,
                edit_work_token=edit_work_token,
                text=strip_telegram_html(translated)
                if translated_parse_mode == "HTML"
                else translated,
                parse_mode=None,
                mode="plain" if translated_parse_mode == "HTML" else translated_mode,
            )
        )
    finally:
        if original_claim is not None and chat_id is not None:
            try:
                if reply_index.get(chat_id, message.message_id) is None:
                    reply_index.remember_observed_without_reply(
                        chat_id, message.message_id
                    )
            finally:
                reply_index.finish_original(original_claim)


def _passes_channel_delivery_gate(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int | None
) -> bool:
    if chat_id is None:
        return False
    settings: ChatSettings = context.application.bot_data[CHAT_SETTINGS_KEY]
    return settings.is_allowed(chat_id)


def _passes_final_delivery_gate(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_id: int | None,
    is_edit: bool,
    text: str,
    mode: str,
) -> bool:
    """Recheck volatile policy immediately before a Telegram API call."""

    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    if _queue_deadline_expired(context) or not _message_is_fresh(
        message, config.channel_max_age_seconds
    ):
        _channel_event(
            runtime,
            stage="skipped",
            reason="stale_before_delivery",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        return False
    if not _passes_channel_delivery_gate(context, chat_id):
        _channel_event(
            runtime,
            stage="skipped",
            reason="authorization_changed_before_delivery",
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        return False
    return True


async def _translate_channel_input(
    translator: SentenceTranslator | None,
    plain: str,
    html_text: str | None,
    *,
    to_chinese: bool,
    before_llm_call,
    allow_plain_fallback: bool = True,
    execution_budget: OperationExecutionBudget | None = None,
    planned_html_segments: list[tuple[str, str, str]] | None = None,
    planned_plain_segments: list[object] | None = None,
    http_backend: HttpCoreBackend | None = None,
) -> tuple[str, str | None, str]:
    """Translate one post; LLMTranslationError carries a ``failure_stage``
    attribute (html-attempt / plain-fallback / plain-no-html /
    html-input-split / plain-input-split) so the caller's log event can say
    which call failed."""
    is_long = len(plain) > LLM_INPUT_CHAR_LIMIT
    if is_long and planned_html_segments is None and planned_plain_segments is None:
        (
            planned_html_segments,
            planned_plain_segments,
            _planned_calls,
        ) = _plan_long_channel_segments(plain, html_text)
    existing = llm_transport.current_execution_budget()
    budget_cm: AbstractContextManager[OperationExecutionBudget]
    if execution_budget is not None:
        budget_cm = (
            nullcontext(execution_budget)
            if existing is execution_budget
            else execution_budget.activate()
        )
    elif isinstance(existing, OperationExecutionBudget):
        budget_cm = nullcontext(existing)
    else:
        if not is_long:
            if html_text and not allow_plain_fallback:
                max_total, max_corr = 1, 0
            else:
                max_total, max_corr = 2, 1
            max_corr_seg = max_corr
        else:
            if planned_html_segments is not None:
                base = max(1, sum(1 for _l, core, _t in planned_html_segments if core))
                max_corr = 1 if allow_plain_fallback else 0
                max_total = base + max_corr
            else:
                assert planned_plain_segments is not None
                max_total = max(
                    1,
                    sum(
                        1
                        for seg in planned_plain_segments
                        if seg.core_start < seg.core_end
                    ),
                )
                max_corr = 0
            max_corr_seg = 0
        budget_cm = OperationExecutionBudget(
            max_total_calls=max_total,
            max_corrective_calls=max_corr,
            max_corrective_calls_per_segment=max_corr_seg,
        ).activate()

    async def _call_plain(
        text: str,
        *,
        stage: str,
        context: TranslationContext | None = None,
        budget: OperationExecutionBudget | None = None,
    ) -> str:
        if http_backend is not None:
            outcome = await http_backend.translate_text(
                text,
                to_chinese=to_chinese,
                context=context,
                before_llm_call=before_llm_call,
                execution_budget=budget,
            )
            if outcome.failed:
                exc = LLMTranslationError(
                    outcome.text or TRANSLATION_UNAVAILABLE_NOTICE,
                    reason=outcome.error_code or "upstream",
                )
                exc.failure_stage = stage
                raise exc
            return outcome.text
        assert translator is not None
        return await _translate_plain_stage(
            translator,
            text,
            stage=stage,
            to_chinese=to_chinese,
            before_llm_call=before_llm_call,
            context=context,
            execution_budget=budget,
        )

    async def _call_html(
        seg_html: str,
        *,
        context: TranslationContext | None = None,
        budget: OperationExecutionBudget | None = None,
    ) -> tuple[str, bool]:
        if http_backend is not None:
            return await http_backend.translate_protected_html(
                seg_html,
                to_chinese=to_chinese,
                context=context,
                before_llm_call=before_llm_call,
                execution_budget=budget,
            )
        assert translator is not None
        translated_seg = await translate_html_async(
            translator,
            seg_html,
            to_chinese=to_chinese,
            before_llm_call=before_llm_call,
            execution_budget=budget,
            context=(
                context
                if context is not None and translator_supports_protected_context(translator)
                else None
            ),
        )
        return translated_seg, True

    with budget_cm as active_budget:
        if not is_long:
            if not html_text:
                translated = await _call_plain(
                    plain,
                    stage="plain-no-html",
                    budget=active_budget,
                )
                return translated, None, "plain-no-html"
            try:
                translated_html, markup_used = await _call_html(
                    html_text,
                    budget=active_budget,
                )
                if markup_used:
                    return translated_html, "HTML", "HTML"
                return translated_html, None, "plain-http-degraded"
            except LLMTranslationError as exc:
                if (
                    getattr(exc, "reason", "") not in HTML_CONTENT_FAILURE_REASONS
                    or not allow_plain_fallback
                ):
                    exc.failure_stage = "html-attempt"
                    raise
                if active_budget.is_cancelled or active_budget.deadline_expired():
                    exc.failure_stage = "html-attempt"
                    raise
                if (
                    active_budget.calls_used > 0
                    and not active_budget.try_consume_html_fallback()
                ):
                    exc.failure_stage = "html-attempt"
                    raise
                translated = await _call_plain(
                    plain,
                    stage="plain-fallback",
                    budget=active_budget,
                )
                return translated, None, "plain-after-html-failure"

        # Long post (> LLM_INPUT_CHAR_LIMIT): prefer paragraph-aware balanced
        # HTML segmentation with verbatim inter-segment whitespace separators
        # and budgeted per-segment plain fallback on HTML integrity failure.
        if planned_html_segments is not None:
            translated_html_chunks: list[str] = []
            any_markup_used = False
            before: ContextBefore | None = None
            for lead_ws, core_html, trail_ws in planned_html_segments:
                if not core_html:
                    translated_html_chunks.append(lead_ws + trail_ws)
                    continue
                seg_plain = strip_telegram_html(core_html)
                if not seg_plain.strip():
                    translated_html_chunks.append(lead_ws + core_html + trail_ws)
                    continue
                seg_context = TranslationContext(before=before)
                try:
                    translated_seg, seg_markup = await _call_html(
                        core_html,
                        context=seg_context,
                        budget=active_budget,
                    )
                    if seg_markup:
                        any_markup_used = True
                    else:
                        translated_seg = html_escape(
                            _strip_segment_separators(translated_seg), quote=False
                        )
                except LLMTranslationError as exc:
                    if (
                        getattr(exc, "reason", "") in HTML_CONTENT_FAILURE_REASONS
                        and allow_plain_fallback
                        and not active_budget.is_cancelled
                        and not active_budget.deadline_expired()
                        and active_budget.try_consume_html_fallback()
                    ):
                        seg_plain_clean = strip_telegram_wrapper_noise(
                            seg_plain, keep_paragraphs=True
                        )
                        plain_seg = await _call_plain(
                            seg_plain_clean or seg_plain,
                            stage="plain-fallback",
                            context=seg_context,
                            budget=active_budget,
                        )
                        plain_seg = _strip_segment_separators(plain_seg)
                        translated_seg = html_escape(plain_seg, quote=False)
                    else:
                        exc.failure_stage = "html-input-split"
                        raise
                translated_html_chunks.append(lead_ws + translated_seg + trail_ws)
                before = ContextBefore(
                    source=_tail_context(seg_plain),
                    target=_tail_context(strip_telegram_html(translated_seg)),
                )
            combined_html = normalize_telegram_html("".join(translated_html_chunks))
            if any_markup_used and validate_telegram_html(combined_html):
                return combined_html, "HTML", "html-input-split"
            return (
                strip_telegram_html(combined_html),
                None,
                "plain-after-invalid-html" if any_markup_used else "plain-http-degraded",
            )

        # Plain long post: Core segmentation-v1 + TranslationContext(before=...)
        # preserving original paragraph breaks and blank lines verbatim.
        assert planned_plain_segments is not None
        pieces: list[str] = []
        before = None
        for seg in planned_plain_segments:
            core = plain[seg.core_start : seg.core_end]
            if not core:
                pieces.append(plain[seg.start : seg.end])
                continue
            translated = await _call_plain(
                core,
                stage="plain-input-split",
                context=TranslationContext(before=before),
                budget=active_budget,
            )
            translated = _strip_segment_separators(translated)
            pieces.append(
                plain[seg.start : seg.core_start]
                + translated
                + plain[seg.core_end : seg.end]
            )
            before = ContextBefore(
                source=_tail_context(core),
                target=_tail_context(translated),
            )
        return "".join(pieces), None, "plain-input-split"


async def _translate_plain_stage(
    translator: SentenceTranslator,
    text: str,
    *,
    stage: str,
    to_chinese: bool,
    before_llm_call,
    context: TranslationContext | None = None,
    execution_budget: OperationExecutionBudget | None = None,
) -> str:
    try:
        kwargs = {
            "to_chinese": to_chinese,
            "before_llm_call": before_llm_call,
            "propagate_errors": True,
        }
        if context is not None:
            kwargs["context"] = context
        if execution_budget is not None:
            kwargs["execution_budget"] = execution_budget
        return await translator.translate_async(text, **kwargs)
    except LLMTranslationError as exc:
        exc.failure_stage = stage
        raise


async def _emit(
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
) -> ChannelPostOutcome:
    message = kwargs["message"]
    chat_id = kwargs.get("chat_id")
    is_edit = bool(kwargs.get("is_edit"))
    text = kwargs["text"]
    mode = kwargs["mode"]
    config: BotConfig = context.application.bot_data[CONFIG_KEY]
    runtime = _channel_runtime(context, config)
    if not _passes_final_delivery_gate(
        context,
        message=message,
        chat_id=chat_id,
        is_edit=is_edit,
        text=text,
        mode=mode,
    ):
        return ChannelPostOutcome("expired", "gated")
    try:
        outcome = await _emit_unchecked(context, **kwargs)
    except BadRequest as exc:
        err_name = safe_error_type(exc)
        _channel_event(
            runtime,
            stage="delivery",
            reason=err_name,
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        if kwargs.get("parse_mode") == "HTML":
            raise
        return ChannelPostOutcome("permanent_skip", err_name)
    except RetryAfter as exc:
        err_name = safe_error_type(exc)
        _channel_event(
            runtime,
            stage="delivery",
            reason=err_name,
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        # 429 guarantees Telegram did NOT execute the send; safe to retry.
        return ChannelPostOutcome("retryable_failure", err_name)
    except (TimedOut, TelegramError) as exc:
        err_name = safe_error_type(exc)
        _channel_event(
            runtime,
            stage="delivery",
            reason=err_name,
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        # TimedOut / NetworkError / unknown TelegramError may have succeeded
        # server-side; never report as completed and never blind-resend.
        return ChannelPostOutcome("uncertain_delivery", err_name)
    else:
        if outcome == "gated":
            return ChannelPostOutcome("expired", "gated")
        if outcome == "stale_edit":
            _channel_event(
                runtime,
                stage="skipped",
                reason="stale_edit_before_delivery",
                message=message,
                chat_id=chat_id,
                is_edit=is_edit,
                text_len=safe_text_len(text),
                mode=mode,
            )
            return ChannelPostOutcome("already_handled", "stale_edit_before_delivery")
        _channel_event(
            runtime,
            stage="delivery",
            reason=outcome,
            message=message,
            chat_id=chat_id,
            is_edit=is_edit,
            text_len=safe_text_len(text),
            mode=mode,
        )
        if outcome == "edit_target_unavailable":
            return ChannelPostOutcome("permanent_skip", outcome)
        return ChannelPostOutcome("delivered", outcome)


async def _emit_unchecked(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_index: ChannelReplyIndex,
    message,
    chat_id: int | None,
    chat_type: str,
    is_edit: bool,
    existing_reply_id: int | None,
    edit_work_token: int | None,
    text: str,
    parse_mode: str | None,
    mode: str,
) -> str:
    """Deliver one translation, then remember its reply chunks for later edits.

    New post: send one or more in-thread replies and remember their IDs.
    Edit: update those remembered replies in place, add continuation chunks,
    or delete stale extras. On the edit path, a "message is not modified"
    error is the dedup ideal (identical re-translation) and is a silent no-op.
    An HTML edit Telegram rejects re-raises so the caller's plain fallback
    runs. A failed plain edit (reply deleted / uneditable) is swallowed; an
    edit must never crash the listener or leave a duplicate.
    """
    chunks, delivery_parse_mode, delivery_mode = _channel_delivery_chunks(
        text, parse_mode, mode
    )
    if is_edit:
        if chat_id is not None and edit_work_token is not None:
            async with reply_index.edit_delivery_lock(chat_id, message.message_id):
                if not reply_index.is_latest_edit(
                    chat_id, message.message_id, edit_work_token
                ):
                    LOGGER.info(
                        "channel edit skipped: stale translation "
                        "incoming_message=%s reply_message=%s",
                        redact_id(message.message_id),
                        redact_id(existing_reply_id),
                    )
                    return "stale_edit"
                if not _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text=text,
                    mode=delivery_mode,
                ):
                    return "gated"
                existing_reply_ids = reply_index.get_many(chat_id, message.message_id)
                if not existing_reply_ids and existing_reply_id is not None:
                    existing_reply_ids = (existing_reply_id,)
                return await _edit_reply_chunks(
                    context,
                    reply_index=reply_index,
                    message=message,
                    chat_type=chat_type,
                    existing_reply_ids=existing_reply_ids,
                    chunks=chunks,
                    chat_id=chat_id,
                    parse_mode=delivery_parse_mode,
                    mode=delivery_mode,
                )
        existing_reply_ids = (
            reply_index.get_many(chat_id, message.message_id)
            if chat_id is not None
            else ()
        )
        if not existing_reply_ids and existing_reply_id is not None:
            existing_reply_ids = (existing_reply_id,)
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=True,
            text=text,
            mode=delivery_mode,
        ):
            return "gated"
        return await _edit_reply_chunks(
            context,
            reply_index=reply_index,
            message=message,
            chat_type=chat_type,
            existing_reply_ids=existing_reply_ids,
            chunks=chunks,
            chat_id=chat_id,
            parse_mode=delivery_parse_mode,
            mode=delivery_mode,
        )

    reply_message_ids = []
    for index, chunk in enumerate(chunks):
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=False,
            text=chunk,
            mode=delivery_mode,
        ):
            return "gated"
        chunk_parse_mode = delivery_parse_mode
        chunk_text = chunk
        chunk_mode = delivery_mode
        try:
            sent_message = await send_with_flood_retry(
                lambda: message.reply_text(
                    chunk_text,
                    parse_mode=chunk_parse_mode,
                    reply_to_message_id=message.message_id,
                ),
                retry_gate=lambda: _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=False,
                    text=chunk_text,
                    mode=chunk_mode,
                ),
            )
        except BadRequest:
            if chunk_parse_mode == "HTML" and index > 0:
                # Earlier chunks already delivered; recheck volatile delivery
                # authorization/freshness before degrading this chunk to plain.
                chunk_text = strip_telegram_html(chunk)
                chunk_parse_mode = None
                chunk_mode = "plain-after-badrequest"
                if not _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=False,
                    text=chunk_text,
                    mode=chunk_mode,
                ):
                    return "gated"
                sent_message = await send_with_flood_retry(
                    lambda: message.reply_text(
                        chunk_text,
                        parse_mode=None,
                        reply_to_message_id=message.message_id,
                    ),
                    retry_gate=lambda: _passes_final_delivery_gate(
                        context,
                        message=message,
                        chat_id=chat_id,
                        is_edit=False,
                        text=chunk_text,
                        mode=chunk_mode,
                    ),
                )
            else:
                raise
        reply_message_id = getattr(sent_message, "message_id", None)
        if reply_message_id is not None:
            reply_message_ids.append(reply_message_id)
            if chat_id is not None:
                reply_index.remember_many(
                    chat_id,
                    message.message_id,
                    tuple(reply_message_ids),
                    complete=(index == len(chunks) - 1),
                    expected_chunks=len(chunks),
                )
        _log_emit(
            chat_type,
            message,
            reply_message_id,
            chunk_mode,
            edited=False,
            text=chunk_text,
        )
    if chat_id is not None and reply_message_ids:
        reply_index.mark_complete(chat_id, message.message_id)
    return "success"


def _channel_delivery_chunks(
    text: str, parse_mode: str | None, mode: str
) -> tuple[list[str], str | None, str]:
    if parse_mode == "HTML":
        text = normalize_telegram_html(text)
        visible_text = strip_telegram_html(text)
        if telegram_text_units(visible_text) > TELEGRAM_TEXT_MESSAGE_LIMIT:
            if telegram_html_has_tags(text):
                html_chunks = split_telegram_html(
                    text,
                    limit=TELEGRAM_TEXT_MESSAGE_LIMIT,
                    unit_counter=telegram_text_units,
                    max_raw_units=TELEGRAM_TEXT_MESSAGE_LIMIT,
                )
                if html_chunks:
                    return (
                        html_chunks,
                        "HTML",
                        f"{mode}-{LONG_HTML_OUTPUT_MODE_SUFFIX}",
                    )
            return (
                split_telegram_text(visible_text),
                None,
                f"{mode}-{LONG_OUTPUT_MODE_SUFFIX}",
            )
        return [text], parse_mode, mode
    chunks = split_telegram_text(text)
    if len(chunks) > 1:
        return chunks, parse_mode, f"{mode}-{LONG_OUTPUT_MODE_SUFFIX}"
    return chunks, parse_mode, mode


async def _edit_reply_chunks(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    reply_index: ChannelReplyIndex,
    message,
    chat_type: str,
    existing_reply_ids: tuple[int, ...],
    chunks: list[str],
    chat_id: int | None,
    parse_mode: str | None,
    mode: str,
) -> str:
    if chat_id is None or not existing_reply_ids:
        return "edit_target_unavailable"
    remembered_reply_ids: list[int] = []
    # Ids kept tracked only because their delete failed. Collected apart from
    # freshly updated/sent chunk ids and appended at the very end: splicing
    # them in at the position where the delete failed would map the next
    # edit's chunk onto the stale, still-uneditable reply and treat the real
    # replacement as an extra chunk to delete.
    stale_extra_ids: list[int] = []
    remaining_reply_ids = list(existing_reply_ids)
    for chunk_index, chunk in enumerate(chunks):
        while remaining_reply_ids:
            if not _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=chunk,
                mode=mode,
            ):
                return "gated"
            reply_message_id = remaining_reply_ids.pop(0)
            edit_result = await _edit_existing_reply(
                context,
                message=message,
                chat_type=chat_type,
                existing_reply_id=reply_message_id,
                text=chunk,
                chat_id=chat_id,
                parse_mode=parse_mode,
                mode=mode,
            )
            if edit_result == "applied":
                remembered_reply_ids.append(reply_message_id)
                break
            if edit_result == "uneditable" and remembered_reply_ids:
                # A later-chunk reply that rejects edits would otherwise be
                # dropped from the rebuilt index while staying visible - the
                # orphan this path exists to prevent. Delete it whatever the
                # chunk position; keep it tracked only if the delete fails.
                failed_delete_ids = await _delete_reply_chunks(
                    context,
                    message=message,
                    chat_id=chat_id,
                    reply_message_ids=(reply_message_id,),
                )
                stale_extra_ids.extend(failed_delete_ids)
                continue
            if not remembered_reply_ids:
                # "gone" needs no delete of the failed id (it no longer
                # exists); "uneditable" must include it, or the post keeps a
                # stale translation that is no longer tracked - every later
                # edit would skip as untracked and it would never update.
                prune_ids = (
                    tuple(remaining_reply_ids)
                    if edit_result == "gone"
                    else (reply_message_id, *remaining_reply_ids)
                )
                failed_delete_ids = await _delete_reply_chunks(
                    context,
                    message=message,
                    chat_id=chat_id,
                    reply_message_ids=prune_ids,
                )
                if failed_delete_ids:
                    reply_index.remember_many(
                        chat_id, message.message_id, failed_delete_ids
                    )
                else:
                    reply_index.forget(chat_id, message.message_id)
                return "edit_target_unavailable"
        else:
            if not _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=chunk,
                mode=mode,
            ):
                return "gated"
            sent_message = await send_with_flood_retry(
                lambda: message.reply_text(
                    chunk,
                    parse_mode=parse_mode,
                    reply_to_message_id=message.message_id,
                ),
                retry_gate=lambda: _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text=chunk,
                    mode=mode,
                ),
            )
            reply_message_id = getattr(sent_message, "message_id", None)
            if reply_message_id is not None:
                remembered_reply_ids.append(reply_message_id)
                reply_index.remember_many(
                    chat_id,
                    message.message_id,
                    tuple(remembered_reply_ids),
                    complete=(chunk_index == len(chunks) - 1),
                    expected_chunks=len(chunks),
                )
            _log_emit(
                chat_type,
                message,
                reply_message_id,
                mode,
                edited=False,
                text=chunk,
            )
    if remaining_reply_ids:
        failed_delete_ids = await _delete_reply_chunks(
            context,
            message=message,
            chat_id=chat_id,
            reply_message_ids=tuple(remaining_reply_ids),
        )
        stale_extra_ids.extend(failed_delete_ids)
    if remembered_reply_ids or stale_extra_ids:
        reply_index.remember_many(
            chat_id,
            message.message_id,
            tuple(remembered_reply_ids) + tuple(stale_extra_ids),
            complete=True,
            expected_chunks=len(chunks),
        )
    else:
        reply_index.forget(chat_id, message.message_id)
    if stale_extra_ids:
        return "stale_chunks_retained"
    return "success"


async def _delete_reply_chunks(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_id: int,
    reply_message_ids: tuple[int, ...],
) -> tuple[int, ...]:
    failed_reply_message_ids = []
    for index, reply_message_id in enumerate(reply_message_ids):
        if not _passes_final_delivery_gate(
            context,
            message=message,
            chat_id=chat_id,
            is_edit=True,
            text="",
            mode="edit-delete",
        ):
            failed_reply_message_ids.extend(reply_message_ids[index:])
            break
        try:
            await send_with_flood_retry(
                lambda: context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=reply_message_id,
                ),
                retry_gate=lambda: _passes_final_delivery_gate(
                    context,
                    message=message,
                    chat_id=chat_id,
                    is_edit=True,
                    text="",
                    mode="edit-delete",
                ),
            )
        except BadRequest as exc:
            if _delete_error_means_already_gone(exc):
                LOGGER.info(
                    "channel edit extra chunk already gone "
                    "incoming_message=%s reply_message=%s",
                    redact_id(message.message_id),
                    redact_id(reply_message_id),
                )
                continue
            failed_reply_message_ids.append(reply_message_id)
            LOGGER.info(
                "channel edit extra chunk delete skipped "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(reply_message_id),
            )
        except TelegramError:
            failed_reply_message_ids.append(reply_message_id)
            LOGGER.info(
                "channel edit extra chunk delete skipped "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(reply_message_id),
            )
    return tuple(failed_reply_message_ids)


def _delete_error_means_already_gone(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "not found" in text or "message to delete not found" in text


async def _edit_existing_reply(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    message,
    chat_type: str,
    existing_reply_id: int | None,
    text: str,
    chat_id: int | None,
    parse_mode: str | None,
    mode: str,
) -> Literal["applied", "gone", "uneditable"]:
    """Edit one tracked reply in place.

    The caller needs the failure shape, not just success/failure: "gone" the
    message no longer exists (nothing to clean up), "uneditable" it still
    exists but rejects the edit (it must be deleted, or it stays visible
    with stale text while no longer tracked - an orphan).
    """
    try:
        # The flood sleep here runs inside edit_delivery_lock; a newer edit
        # queues behind it and re-checks is_latest_edit after acquiring, so
        # only the volatile freshness/authorization gates need rechecking.
        await send_with_flood_retry(
            lambda: context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=existing_reply_id,
                parse_mode=parse_mode,
            ),
            retry_gate=lambda: _passes_final_delivery_gate(
                context,
                message=message,
                chat_id=chat_id,
                is_edit=True,
                text=text,
                mode=mode,
            ),
        )
    except BadRequest as exc:
        lowered = str(exc).lower()
        if "not modified" in lowered:
            LOGGER.info(
                "channel edit no-op (unchanged translation) "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(existing_reply_id),
            )
            return "applied"
        if parse_mode is not None:
            # HTML edit rejected -> let the caller retry as plain.
            raise
        if "not found" in lowered:
            LOGGER.info(
                "channel edit skipped: reply already gone "
                "incoming_message=%s reply_message=%s",
                redact_id(message.message_id),
                redact_id(existing_reply_id),
            )
            return "gone"
        LOGGER.info(
            "channel edit skipped: reply not updatable "
            "incoming_message=%s reply_message=%s",
            redact_id(message.message_id),
            redact_id(existing_reply_id),
        )
        return "uneditable"
    _log_emit(chat_type, message, existing_reply_id, mode, edited=True, text=text)
    return "applied"


def _log_emit(
    chat_type: str,
    message,
    reply_message_id,
    mode: str,
    *,
    edited: bool,
    text: str,
) -> None:
    LOGGER.info(
        "bot_reply chat_type=%s incoming_message=%s reply_message=%s "
        "reply_to_message=%s mode=%s edited=%s text_len=%s",
        chat_type,
        redact_id(message.message_id),
        redact_id(reply_message_id),
        redact_id(message.message_id),
        mode,
        edited,
        safe_text_len(text),
    )


class _QueuedChannelMessage:
    """Minimal message proxy for replaying a queued channel task via Bot API."""

    def __init__(self, bot, task: ChannelTask) -> None:
        self._bot = bot
        self._chat_id = task.chat_id
        self.message_id = task.message_id
        self.text = task.plain
        self.caption = None
        self.text_html = task.html
        self.caption_html = None
        self.date = (
            datetime.fromtimestamp(task.message_date_ts, tz=timezone.utc)
            if task.message_date_ts is not None
            else None
        )
        self.edit_date = (
            datetime.now(timezone.utc) if task.is_edit else None
        )

    async def reply_text(self, text: str, **kwargs):
        return await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            **kwargs,
        )


async def execute_queued_channel_task(
    application, task: ChannelTask
) -> TaskExecutionOutcome:
    """Replay one queued channel translation task using explicit per-post outcome."""
    config: BotConfig = application.bot_data[CONFIG_KEY]
    settings: ChatSettings = application.bot_data[CHAT_SETTINGS_KEY]
    queue = application.bot_data.get(CHANNEL_QUEUE_KEY)
    clock = getattr(queue, "_clock", time.time)
    if clock() >= task.expires_at:
        return TaskExecutionOutcome("expired", "queue_ttl_expired")
    if not settings.is_allowed(task.chat_id):
        return TaskExecutionOutcome("expired", "not_authorized")
    message = _QueuedChannelMessage(application.bot, task)
    if not _message_is_fresh(message, config.channel_max_age_seconds):
        return TaskExecutionOutcome("expired", "stale")
    update = SimpleNamespace(
        update_id=task.update_id,
        effective_message=message,
        effective_chat=SimpleNamespace(id=task.chat_id, type=task.chat_type),
        effective_user=None,
    )
    context = SimpleNamespace(
        args=[],
        bot=application.bot,
        application=application,
        _from_channel_queue=True,
        _queue_expires_at=task.expires_at,
        _queue_clock=clock,
    )
    outcome = await _handle_channel_post(update, context)
    if outcome.kind in {"delivered", "already_handled"}:
        return TaskExecutionOutcome("completed", outcome.reason)
    if outcome.kind == "expired":
        return TaskExecutionOutcome("expired", outcome.reason)
    if outcome.kind == "retryable_failure":
        return TaskExecutionOutcome("retry", outcome.reason)
    if outcome.kind == "uncertain_delivery":
        # Never swallow TimedOut/unknown delivery as completed and never blind-resend.
        return TaskExecutionOutcome("failed", f"delivery_uncertain:{outcome.reason}")
    return TaskExecutionOutcome("failed", outcome.reason or "permanent_skip")
