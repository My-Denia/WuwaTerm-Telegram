"""`/review` command, its inline buttons and the `/tr` review button.

State lives in `ReviewJobStore` (memory only: 30 minutes, five per chat, never
written to disk). Rate-limit rule: one `/review` (or one press of a `/tr`
review button) that actually starts a run spends one slot of the chat's
translation budget, like `/tr`. A run is then at most `MAX_REVIEW_PAIRS` model
calls, one at a time. Duplicates, usage hints and "busy" answers cost nothing.

Handlers import `.bot` lazily; `bot.py` imports this module at load.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Iterator

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from wuwaterm.logging_utils import redact_id

from .channel import send_with_flood_retry
from .review_backend import LiveReviewBackend, ReviewBackend, infer_direction
from .review_flow import MAX_REVIEW_PAIRS, ReviewAborted, run_review
from .review_format import format_issue_card, format_report, summary_line
from .review_input import (
    KIND_BAD_SEPARATOR,
    KIND_LAST,
    KIND_PAIR,
    KIND_REPLY_TARGET,
    KIND_REPLY_TRANSLATION,
    command_body,
    parse_review_input,
)
from .review_jobs import (
    STATE_DONE,
    WAITER_FULL,
    ReviewJob,
    ReviewJobStore,
    StoreFullError,
    TranslationRecord,
)
from .review_model import (
    PAIR_NEEDS_MANUAL_SPLIT,
    PAIR_READY,
    SEMANTIC_PARTIAL,
    SEMANTIC_STOPPED,
    ReviewReport,
)
from .review_revise import RevisionError, check_accept, revise
from .runtime_keys import REVIEW_BACKEND_KEY, REVIEW_STORE_KEY

LOGGER = logging.getLogger(__name__)

CALLBACK_PREFIX = "rv"
# Untrusted (public-member) callers keep the plain translation input limit.
UNTRUSTED_SIDE_LIMIT = 2000
# Trusted callers: up to MAX_REVIEW_PAIRS segments of 1800 scalars.
TRUSTED_SIDE_LIMIT = MAX_REVIEW_PAIRS * 1800

REVIEW_USAGE_NOTICE = (
    "用法：/review（别名 /check）\n"
    "1) 回复原文消息，命令后面写译文\n"
    "2) 一条消息里写原文，单独一行 ---，再写译文\n"
    "3) 回复 bot 的 /tr 译文（仅限最近 30 分钟内的结果）\n"
    "/review last：查看上一条审校结果\n"
    "Usage: /review (alias /check) — reply to the source with the translation after the "
    "command; or put source, a line with only ---, then the translation; or reply to a "
    "recent /tr result. /review last shows the previous result."
)
BAD_SEPARATOR_NOTICE = (
    "分隔线 --- 需要单独一行，只能有一条，两侧都要有内容。\n"
    "Use exactly one line containing only ---, with text on both sides."
)
NO_TRANSLATION_RECORD_NOTICE = (
    "找不到这条译文对应的原文（记录只保留 30 分钟，bot 重启后也会清空）。\n"
    "请回复原文消息并在命令后写译文，或用 --- 分隔原文和译文。\n"
    "No record for that translation (kept 30 minutes, cleared on restart). Reply to the "
    "source with the translation, or use a --- line."
)
NO_LAST_NOTICE = (
    "没有可查看的审校结果（结果只保留 30 分钟，bot 重启后也会清空），请重新提交 /review。\n"
    "No recent review result (kept 30 minutes, cleared on restart); submit a new /review."
)
SAME_LANGUAGE_NOTICE = (
    "原文和译文看起来是同一种语言，已取消审校。\n"
    "Source and translation look like the same language; review cancelled."
)
BUSY_NOTICE = (
    "本聊天已有一个审校在进行，或结果正在发送，请等它完成。\n"
    "A review is already running in this chat, or a result is still being sent; wait for it."
)
DELIVERY_FAILED_NOTICE = (
    "审校结果发送失败，点下面的按钮重发，或再次提交同样的内容。\n"
    "The result could not be sent. Press the button to send it again, or submit the same content."
)
FAILED_NOTICE = (
    "审校失败，请稍后再试。\n"
    "The review failed; please try again later."
)
EXPIRED_TOAST = (
    "审校结果已过期（保留 30 分钟），请重新运行 /review。\n"
    "Review expired (kept 30m); run /review again."
)
TR_EXPIRED_TOAST = (
    "这条译文的审校记录已过期（保留 30 分钟），请重新运行 /tr。\n"
    "This translation's review record expired (kept 30m); run /tr again."
)
DENIED_TOAST = "无权操作 / Not allowed"
NOT_OWNER_TOAST = "仅发起人或管理员可操作 / Only the requester or an admin can use this"
RUNNING_NOTICE = "同一内容正在审校中，请等待结果。\nThis exact review is already running."
WAITER_NOTICE = (
    "同一内容正在审校，完成后会把结果和你的按钮自动发到本聊天，不用重新提交。\n"
    "That content is being reviewed; your own result and buttons will arrive here when it "
    "finishes."
)
WAITER_FULL_NOTICE = (
    "同一内容正在审校，等待的人已满，请稍后重新提交。\n"
    "That content is being reviewed and the list of waiting requests is full; submit it again "
    "shortly."
)
WAITERS_FAILED_NOTICE = (
    "你等的这份审校没有完成，请稍后重新提交。\n"
    "The shared review you were waiting for did not complete; submit it again later."
)
WAITER_SLOTS_FULL_NOTICE = (
    "同一内容审校完了，但本聊天的审校结果已满，请重新提交同样的内容来取回结果。\n"
    "The shared review finished but this chat's result slots are full; submit the same content "
    "again to get the result."
)
RESEND_BUTTON_TEXT = "🔄 重新发送结果 / Resend"
# One retry of a send that definitely did not reach Telegram.
TRANSIENT_RETRY_DELAY = 1.5

RESULT_HINT = (
    "用下方按钮逐条处理；/review last 回到上一条结果。\n"
    "Use the buttons below to step through the issues; /review last returns to the previous result."
)
DONE_NOTICE = "这组内容刚审校过，见上一条结果。\nThis exact review was just done; see the previous result."
NOTHING_ACCEPTED_TOAST = "还没有采纳任何建议 / No suggestion accepted yet"
STALE_TOAST = "译文与审校时不一致，请重新审校 / The translation changed since the review; review again"
ACCEPT_REASONS = {
    "no_suggestion": "这条没有可直接采纳的建议 / No directly usable suggestion",
    "bad_choice": "没有这个备选 / No such alternative",
    "stale": "译文与审校时不一致，请重新审校 / The translation changed since the review; review again",
    "unknown_issue": "这条疑点已失效 / This issue is no longer valid",
}


def _b() -> Any:
    """The bot module, imported late to avoid an import cycle."""
    return import_module("wuwaterm_telegram.bot")


def too_long_notice(limit: int) -> str:
    return (
        f"原文和译文各不能超过 {limit} 字。\n"
        f"Source and translation are each limited to {limit} characters."
    )


def too_many_segments_notice(count: int) -> str:
    return (
        f"这段文本会被切成 {count} 段，超过单次审校的 {MAX_REVIEW_PAIRS} 段上限，请分批提交。\n"
        f"That is {count} segments; one review takes at most {MAX_REVIEW_PAIRS}. Please split it."
    )


# --- keyboards -------------------------------------------------------------------


def _data(job_id: str, action: str, number: int | None = None) -> str:
    parts = [CALLBACK_PREFIX, job_id, action]
    if number is not None:
        parts.append(str(number))
    return ":".join(parts)


def resend_keyboard(job: ReviewJob) -> InlineKeyboardMarkup:
    """One button that sends this job's stored result again."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(RESEND_BUTTON_TEXT, callback_data=_data(job.id, "d"))]]
    )


def has_revisable_suggestion(report: ReviewReport) -> bool:
    return any(issue.suggestion is not None for issue in report.issues)


def summary_keyboard(job: ReviewJob) -> InlineKeyboardMarkup | None:
    report = job.report
    if report is None or not report.issues:
        return None
    rows = [
        [InlineKeyboardButton(f"逐条处理（{len(report.issues)}）", callback_data=_data(job.id, "c", 1))]
    ]
    if has_revisable_suggestion(report):
        rows.append([InlineKeyboardButton("生成修订译文", callback_data=_data(job.id, "f"))])
    return InlineKeyboardMarkup(rows)


def card_keyboard(job: ReviewJob, number: int) -> InlineKeyboardMarkup:
    report = job.report
    assert report is not None
    total = len(report.issues)
    issue = report.issues[number - 1]
    prev_n = number - 1 if number > 1 else total
    next_n = number + 1 if number < total else 1
    rows = [
        [
            InlineKeyboardButton("◀", callback_data=_data(job.id, "c", prev_n)),
            InlineKeyboardButton(f"{number}/{total}", callback_data=_data(job.id, "n")),
            InlineKeyboardButton("▶", callback_data=_data(job.id, "c", next_n)),
        ]
    ]
    action_row: list[InlineKeyboardButton] = []
    if issue.key in job.accepted:
        action_row.append(InlineKeyboardButton("撤销采纳", callback_data=_data(job.id, "u", number)))
    elif issue.suggestion is not None:
        action_row.append(InlineKeyboardButton("采纳建议", callback_data=_data(job.id, "a", number)))
        if issue.alternatives:
            action_row.append(InlineKeyboardButton("用备选", callback_data=_data(job.id, "b", number)))
    if issue.key not in job.accepted:
        action_row.append(InlineKeyboardButton("跳过", callback_data=_data(job.id, "x", number)))
    if action_row:
        rows.append(action_row)
    tail = [InlineKeyboardButton("返回汇总", callback_data=_data(job.id, "s"))]
    if has_revisable_suggestion(report):
        tail.append(InlineKeyboardButton("生成修订译文", callback_data=_data(job.id, "f")))
    rows.append(tail)
    return InlineKeyboardMarkup(rows)


def tr_review_keyboard(record: TranslationRecord) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("审校这段译文", callback_data=_data(record.id, "g"))]]
    )


# --- wiring ----------------------------------------------------------------------


def register_review(app: Application, service: Any, translator: Any, *, backend: ReviewBackend | None = None) -> None:
    app.bot_data[REVIEW_BACKEND_KEY] = backend or LiveReviewBackend(service, translator)
    app.bot_data[REVIEW_STORE_KEY] = ReviewJobStore()
    app.add_handler(CommandHandler(["review", "check"], review_command, block=False))
    app.add_handler(
        CallbackQueryHandler(review_callback, pattern=rf"^{CALLBACK_PREFIX}:", block=False)
    )


def _backend(context: ContextTypes.DEFAULT_TYPE) -> ReviewBackend:
    return context.application.bot_data[REVIEW_BACKEND_KEY]


def _store(context: ContextTypes.DEFAULT_TYPE) -> ReviewJobStore:
    return context.application.bot_data[REVIEW_STORE_KEY]


def _user_id(update: Update) -> int | None:
    user = update.effective_user
    return int(user.id) if user is not None else None


# --- /tr integration -------------------------------------------------------------


@dataclass(frozen=True)
class TranslationOffer:
    record: TranslationRecord
    markup: InlineKeyboardMarkup | None  # None: the record is kept, no button


def offer_translation_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    source: str,
    target: str | None,
    direction: str | None,
) -> TranslationOffer | None:
    """Keep a time-limited source/translation record for a model translation.

    The record always lets the user reply to the result with `/review`. The
    button is offered only while the model track is on.
    """
    if not target or direction not in ("en", "zh") or not source.strip():
        return None
    store: ReviewJobStore | None = context.application.bot_data.get(REVIEW_STORE_KEY)
    backend: ReviewBackend | None = context.application.bot_data.get(REVIEW_BACKEND_KEY)
    chat_id = _b().chat_id_for(update)
    if store is None or backend is None or chat_id is None:
        return None
    record = store.remember_translation(chat_id, _user_id(update), source, target, direction, ())
    markup = tr_review_keyboard(record) if backend.semantic_available() else None
    return TranslationOffer(record, markup)


def attach_sent_messages(offer: TranslationOffer, sent: list) -> None:
    ids = tuple(int(m.message_id) for m in sent if getattr(m, "message_id", None) is not None)
    offer.record.message_ids = ids


# --- /review ---------------------------------------------------------------------


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    b = _b()
    message = update.effective_message
    if message is None:
        return
    actor = await b._translation_actor_or_reject(update, context)
    if actor is None:
        return
    replied = getattr(message, "reply_to_message", None)
    replied_user = getattr(replied, "from_user", None)
    replied_is_bot = bool(
        replied is not None
        and replied_user is not None
        and (getattr(replied_user, "is_bot", False) or replied_user.id == context.bot.id)
    )
    replied_text = getattr(replied, "text", None) or getattr(replied, "caption", None)
    parsed = parse_review_input(
        command_body(getattr(message, "text", None)),
        replied_text=replied_text,
        replied_is_bot=replied_is_bot,
    )
    if parsed.kind == KIND_BAD_SEPARATOR:
        await b.reply_to_user(update, BAD_SEPARATOR_NOTICE)
        return
    if parsed.kind == KIND_LAST:
        await _show_last(update, context)
        return
    store = _store(context)
    chat_id = b.chat_id_for(update)
    if chat_id is None:
        return
    if parsed.kind in (KIND_PAIR, KIND_REPLY_TARGET):
        source, target = parsed.source, parsed.target
    elif parsed.kind == KIND_REPLY_TRANSLATION:
        record = (
            store.translation_for_message(chat_id, int(replied.message_id))
            if replied is not None
            else None
        )
        if record is None:
            await b.reply_to_user(update, NO_TRANSLATION_RECORD_NOTICE)
            return
        source, target = record.source, record.target
    else:
        await b.reply_to_user(update, REVIEW_USAGE_NOTICE)
        return
    await _start_review(update, context, actor, source=source, target=target)


async def _show_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    b = _b()
    chat_id = b.chat_id_for(update)
    done = _store(context).done_jobs(chat_id) if chat_id is not None else []
    user_id = _user_id(update)
    # The requester's own result first; another member's only once it is in the
    # chat, because an unsent result belongs to whoever asked for it.
    job = next((j for j in done if j.user_id == user_id), None) or next(
        (j for j in done if j.delivered), None
    )
    if job is None or job.report is None:
        await b.reply_to_user(update, NO_LAST_NOTICE)
        return
    if _in_progress(job):
        await b.reply_to_user(update, RUNNING_NOTICE)
        return
    if not job.delivered:
        await _deliver(update, context, job)
        return
    await _point_to_result(update, context, job, f"上一条审校结果：{summary_line(job.report)}")


async def _point_to_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob, text: str
) -> None:
    """Answer by replying to the old result message, so the chat can jump to it."""
    b = _b()
    message = update.effective_message
    target_id = job.result_message_ids[0] if job.result_message_ids else None
    if message is not None and target_id is not None:
        try:
            await send_with_flood_retry(
                lambda: message.reply_text(
                    text, reply_to_message_id=target_id, allow_sending_without_reply=False
                ),
                retry_gate=lambda: b._passes_delivery_gate(update, context),
            )
            return
        except BadRequest:
            pass  # the old message is gone; fall through to a plain answer
    await b.reply_to_user(update, text)


async def _start_review(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    actor: Any,
    *,
    source: str,
    target: str,
) -> None:
    """Validate, deduplicate, spend one rate-limit slot, run, and deliver."""
    b = _b()
    chat_id = b.chat_id_for(update)
    if chat_id is None:
        return
    store, backend = _store(context), _backend(context)
    limit = TRUSTED_SIDE_LIMIT if actor.trusted else UNTRUSTED_SIDE_LIMIT
    if len(source) > limit or len(target) > limit:
        await b.reply_to_user(update, too_long_notice(limit))
        return
    direction = infer_direction(target)
    if infer_direction(source) == direction:
        await b.reply_to_user(update, SAME_LANGUAGE_NOTICE)
        return
    pairs = backend.plan_pairs(source, target)
    ready = [p for p in pairs if p.status == PAIR_READY]
    if len(ready) > MAX_REVIEW_PAIRS:
        await b.reply_to_user(update, too_many_segments_notice(len(ready)))
        return
    if not ready and not any(p.status == PAIR_NEEDS_MANUAL_SPLIT for p in pairs):
        # Nothing to review and nothing to explain.
        await b.reply_to_user(update, REVIEW_USAGE_NOTICE)
        return

    key = store.content_key(source, target, direction)
    mine = store.find_owner_duplicate(chat_id, key, _user_id(update))
    if mine is not None and _is_incomplete(mine):
        mine = None  # a stopped or partial review may be run again
    if mine is not None:
        await _answer_own_duplicate(update, context, mine)
        return
    duplicate = store.find_duplicate(chat_id, key)
    if duplicate is not None and _is_incomplete(duplicate):
        duplicate = None  # a stopped or partial review may be run again
    if duplicate is not None:
        if _in_progress(duplicate):
            await _wait_for_shared_result(update, context, duplicate)
            return
        # The content was already reviewed for someone else in this chat: the
        # report is reused, but this requester gets her own buttons and state.
        # Nothing awaits between the lookup above and the new job, so a repeat
        # from this requester finds that job instead of creating a second one.
        await _share_result(update, context, duplicate)
        return
    if store.running(chat_id) is not None:
        await b.reply_to_user(update, BUSY_NOTICE)
        return
    if actor.rate_limited and not b._consume_rate_limit(update, context):
        await b.reply_to_user(update, b.THROTTLE_NOTICE)
        return
    try:
        job = store.create(chat_id, _user_id(update), source, target, direction)
    except StoreFullError:
        await b.reply_to_user(update, BUSY_NOTICE)
        return
    await _run_and_deliver(update, context, job)


async def _run_and_deliver(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    b = _b()
    store, backend = _store(context), _backend(context)

    async def gate() -> bool:
        return await b._passes_delivery_gate(update, context)

    chat = update.effective_chat
    if chat is not None:
        try:
            await context.bot.send_chat_action(chat.id, "typing")
        except TelegramError:
            pass  # cosmetic only
    try:
        report = await run_review(
            backend, job.source, job.target, job.direction, gate=gate
        )
    except ReviewAborted:
        store.fail(job)
        LOGGER.info("review stopped: authorization changed chat=%s", redact_id(job.chat_id))
        await _release_waiters(context, job)
        return
    except Exception as exc:  # noqa: BLE001 - one command must not take the bot down
        store.fail(job)
        LOGGER.warning(
            "review failed chat=%s error_type=%s", redact_id(job.chat_id), type(exc).__name__
        )
        try:
            await b.reply_to_user(update, FAILED_NOTICE)
        finally:
            await _release_waiters(context, job)
        return
    if not await gate():
        store.fail(job)
        LOGGER.info("review result skipped: authorization changed chat=%s", redact_id(job.chat_id))
        await _release_waiters(context, job)
        return
    store.finish(job, report)
    await _deliver(update, context, job)
    await _release_waiters(context, job)


async def _wait_for_shared_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    """Queue this requester behind another member's identical running review.

    The report is computed once; when it finishes every waiter gets her own job,
    message and buttons, so nobody resubmits and no extra model call happens.
    """
    b = _b()
    status = _store(context).join_waiters(job, _user_id(update), update)
    await b.reply_to_user(
        update, WAITER_FULL_NOTICE if status == WAITER_FULL else WAITER_NOTICE
    )


async def _release_waiters(
    context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    """Answer everyone who waited on this job while it ran, then forget them.

    A waiter gets her own reusing job over the finished report (its own buttons,
    its own message ids, no model call and no rate-limit slot). If the review
    did not finish, she is told so instead. The waiters are taken off the job
    first and their updates are dropped on every path, so no `Update` from a
    finished submission is kept in memory.
    """
    b = _b()
    store = _store(context)
    waiters = store.drain_waiters(job)
    try:
        finished = job.state == STATE_DONE and job.report is not None
        for waiter in waiters:
            if not await b._passes_delivery_gate(waiter.update, context):
                continue  # the chat's policy changed while she waited
            if not finished:
                await _best_effort(waiter.update, WAITERS_FAILED_NOTICE)
                continue
            # The same reuse a late duplicate gets: her own job over the one
            # computed report, answered under her own submission.
            await _share_result(
                waiter.update, context, job, slots_full_notice=WAITER_SLOTS_FULL_NOTICE
            )
    finally:
        job.waiters.clear()


async def _best_effort(
    update: Update, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Say something once; a failing notice is not itself retried."""
    b = _b()
    try:
        await b.reply_to_user(update, text, reply_markup=reply_markup)
    except TelegramError:
        LOGGER.info("review notice not delivered")


async def _answer_own_duplicate(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    """Answer a repeat submission with the requester's own earlier result."""
    b = _b()
    if _in_progress(job):
        await b.reply_to_user(update, RUNNING_NOTICE)
        return
    if not job.delivered:
        # Computed but never reached the chat: send it again, no model call.
        await _deliver(update, context, job)
        return
    text = (
        f"这组内容刚审校过：{summary_line(job.report)}" if job.report else DONE_NOTICE
    )
    await _point_to_result(update, context, job, text)


async def _share_result(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    source: ReviewJob,
    *,
    slots_full_notice: str = BUSY_NOTICE,
) -> None:
    """Give a second requester her own job over the already computed report.

    The content stays deduplicated chat-wide, so this costs no model call and
    no rate-limit slot; what is new is the job id, the message and the accepted
    or skipped state. If `source` never reached the chat there is nothing to
    point at, and the first requester still has her own result to receive, so
    the new job gets the full result instead. If the pointer cannot be sent, the
    job stays undelivered and her next identical submission delivers the stored
    report instead. `slots_full_notice` is the answer when the chat has no free
    slot left; a waiter of an identical review can say something more precise.
    """
    b = _b()
    chat_id = b.chat_id_for(update)
    report = source.report
    if chat_id is None or report is None:
        return
    store = _store(context)
    try:
        job = store.create_reusing(chat_id, _user_id(update), source)
    except StoreFullError:
        await b.reply_to_user(update, slots_full_notice)
        return
    if not source.delivered:
        await _deliver(update, context, job)
        return
    with _delivering(job):
        await _point_to_shared_result(update, context, source, job)


async def _point_to_shared_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, source: ReviewJob, job: ReviewJob
) -> None:
    b = _b()
    report = job.report
    assert report is not None
    text = f"这组内容刚审校过：{summary_line(report)}\n\n{RESULT_HINT}"
    markup = summary_keyboard(job)
    sent_ids: list[int] = []
    message = update.effective_message
    target_id = source.result_message_ids[0] if source.result_message_ids else None

    async def gate() -> bool:
        return await b._passes_delivery_gate(update, context)

    async def send_pointer():
        return await send_with_flood_retry(
            lambda: message.reply_text(
                text,
                reply_to_message_id=target_id,
                allow_sending_without_reply=False,
                reply_markup=markup,
            ),
            retry_gate=gate,
        )

    if message is not None and target_id is not None:
        try:
            sent = await _send_with_one_retry(send_pointer, gate)
        except TelegramError as exc:
            if not _missing_reply_target(exc):
                LOGGER.warning(
                    "review result not delivered chat=%s error_type=%s",
                    redact_id(job.chat_id),
                    type(exc).__name__,
                )
                return
            sent = None  # the old pointer is gone; a plain answer still works
        if sent is not None:
            sent_ids.extend(_message_ids([sent]))
    if not sent_ids:
        if not await gate():
            return
        try:
            sent_messages = await _send_with_one_retry(
                lambda: b.reply_to_user(
                    update,
                    text,
                    retry_gate=lambda: b._passes_delivery_gate(update, context),
                    reply_markup=markup,
                ),
                gate,
            )
        except TelegramError as exc:
            LOGGER.warning(
                "review result not delivered chat=%s error_type=%s",
                redact_id(job.chat_id),
                type(exc).__name__,
            )
            return
        if sent_messages is not None:
            sent_ids.extend(_message_ids(sent_messages))
    job.result_message_ids = sent_ids
    job.delivered = bool(sent_ids)


def _message_ids(messages: list) -> list[int]:
    return [
        int(m.message_id) for m in messages if getattr(m, "message_id", None) is not None
    ]


def _missing_reply_target(exc: TelegramError) -> bool:
    """Whether the only problem is that the message being replied to is gone."""
    return isinstance(exc, BadRequest) and "message to be replied not found" in str(exc).lower()


def _retryable_send_error(exc: TelegramError) -> bool:
    """Whether one more send could work and definitely cannot duplicate a message.

    `TimedOut` is a `NetworkError` in python-telegram-bot 22.8, but a timed-out
    request may already have reached Telegram, so it is never retried here; the
    same goes for `BadRequest` (the request is wrong, not the transport) and
    `Forbidden` (the bot is out of that chat).
    """
    return isinstance(exc, NetworkError) and not isinstance(
        exc, (BadRequest, Forbidden, TimedOut)
    )


async def _send_with_one_retry(send, gate, *, allow_retry: bool = True):
    """Run one send, retrying a definite transport failure once.

    The retry re-checks `gate` after the wait, because authorization can change
    while it waits. Returns `None` when the gate refused, which means nothing
    was sent. Every other failure re-raises: the caller keeps its own handling.
    `allow_retry=False` is used once a chunk of the same result has already
    reached the chat, where a retry could post that chunk twice.
    """
    try:
        return await send()
    except TelegramError as exc:
        if not allow_retry or not _retryable_send_error(exc):
            raise
    await asyncio.sleep(TRANSIENT_RETRY_DELAY)
    if not await gate():
        return None
    return await send()


@contextmanager
def _delivering(job: ReviewJob) -> Iterator[None]:
    job.delivering = True
    try:
        yield
    finally:
        job.delivering = False


def _in_progress(job: ReviewJob) -> bool:
    """Running, or finished with its result still being sent."""
    return job.state == "running" or job.delivering


async def _deliver(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> bool:
    with _delivering(job):
        return await _send_result(update, context, job)


async def _send_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> bool:
    """Send a finished job's stored result. It counts as reusable only once sent.

    A send that definitely did not reach Telegram is retried once, but only
    while no chunk of this result has been sent: a later chunk may already be in
    the chat, and resending the first one would duplicate it. On any other
    failure the job stays finished but undelivered, and the short notice carries
    one button that sends this stored result again — so neither a resubmission
    nor that button costs a model call.
    """
    b = _b()
    report = job.report
    assert report is not None

    async def gate() -> bool:
        return await b._passes_delivery_gate(update, context)

    messages = format_report(report, footer=RESULT_HINT)
    markup = summary_keyboard(job)
    sent_ids: list[int] = []

    async def send_chunk(text: str, last: bool, first: bool):
        return await b.reply_to_user(
            update,
            text,
            parse_mode="HTML",
            retry_gate=gate,
            reply_markup=markup if last else None,
            quote=first,
        )

    try:
        for index, text in enumerate(messages):
            sent = await _send_with_one_retry(
                lambda: send_chunk(text, index == len(messages) - 1, index == 0),
                gate,
                allow_retry=not sent_ids,
            )
            if sent is None:  # the retry was refused: nothing was sent
                LOGGER.info(
                    "review result retry stopped: delivery gate closed chat=%s",
                    redact_id(job.chat_id),
                )
                return False
            sent_ids.extend(_message_ids(sent))
    except TelegramError as exc:
        LOGGER.warning(
            "review result not delivered chat=%s error_type=%s",
            redact_id(job.chat_id),
            type(exc).__name__,
        )
        job.delivered = False
        await _best_effort(update, DELIVERY_FAILED_NOTICE, reply_markup=resend_keyboard(job))
        return False
    job.result_message_ids = sent_ids
    job.delivered = bool(sent_ids)
    return job.delivered


# --- callbacks -------------------------------------------------------------------


async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    b = _b()
    query = update.callback_query
    if query is None or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        await query.answer()
        return
    _, item_id, action, *rest = parts
    number = int(rest[0]) if rest and rest[0].isdigit() else None
    # Buttons change another user's review, so admin status is read live: a
    # demoted admin must not keep the 5-minute cached verdict.
    actor = await b._translation_actor(update, context, use_cache=False)
    if actor is None:
        await query.answer(DENIED_TOAST, show_alert=True)
        return
    chat_id = b.chat_id_for(update)
    if chat_id is None:
        await query.answer()
        return
    store = _store(context)
    user_id = _user_id(update)

    if action == "g":  # review button under a /tr result
        record = store.get_translation(chat_id, item_id)
        if record is None:
            await query.answer(TR_EXPIRED_TOAST, show_alert=True)
            return
        if not (actor.trusted or user_id == record.user_id):
            await query.answer(NOT_OWNER_TOAST, show_alert=True)
            return
        await query.answer()
        await _start_review(
            update, context, actor, source=record.source, target=record.target
        )
        return

    job = store.get(chat_id, item_id)
    if job is None or job.report is None:
        await query.answer(EXPIRED_TOAST, show_alert=True)
        return
    if not (actor.trusted or user_id == job.user_id):
        await query.answer(NOT_OWNER_TOAST, show_alert=True)
        return
    report = job.report
    total = len(report.issues)

    if action == "n":
        await query.answer()
        return
    if action == "d":  # send a stored result the chat never received
        await _resend_result(update, context, job)
        return
    if action == "s":
        await query.answer()
        await _edit(update, format_report(report)[-1], summary_keyboard(job))
        return
    if action == "f":
        await _send_revision(update, context, job)
        return
    if number is None or not 1 <= number <= total:
        await query.answer(ACCEPT_REASONS["unknown_issue"], show_alert=True)
        return
    issue = report.issues[number - 1]
    if action == "a" or action == "b":
        choice = 0 if action == "a" else 1
        reason = check_accept(report, job.accepted, issue.key, choice)
        if reason is not None:
            text = (
                f"与第 {_issue_number(report, reason.split(':', 1)[1])} 条已采纳的修改范围重叠"
                if reason.startswith("overlaps:")
                else ACCEPT_REASONS.get(reason, STALE_TOAST)
            )
            await query.answer(text, show_alert=True)
            return
        job.accepted[issue.key] = choice
        job.skipped.discard(issue.key)
    elif action == "u":
        job.accepted.pop(issue.key, None)
    elif action == "x":
        if issue.key in job.skipped:
            job.skipped.discard(issue.key)
        else:
            job.skipped.add(issue.key)
    elif action != "c":
        await query.answer()
        return
    await query.answer()
    await _edit(
        update,
        format_issue_card(report, number, accepted=set(job.accepted), skipped=job.skipped),
        card_keyboard(job, number),
    )


async def _resend_result(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    """Send a finished job's stored result again, without another review."""
    b = _b()
    query = update.callback_query
    if job.delivering or job.delivered:
        if query is not None:
            await query.answer()
        return
    if not await b._passes_delivery_gate(update, context):
        if query is not None:
            await query.answer(DENIED_TOAST, show_alert=True)
        return
    if query is not None:
        await query.answer()
    await _deliver(update, context, job)


def _is_incomplete(job: ReviewJob) -> bool:
    return job.state == "done" and job.report is not None and job.report.semantic_state in (
        SEMANTIC_STOPPED,
        SEMANTIC_PARTIAL,
    )


def _issue_number(report: ReviewReport, key: str) -> int:
    for position, issue in enumerate(report.issues, start=1):
        if issue.key == key:
            return position
    return 0


async def _edit(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).casefold():
            raise


async def _send_revision(
    update: Update, context: ContextTypes.DEFAULT_TYPE, job: ReviewJob
) -> None:
    b = _b()
    query = update.callback_query
    report = job.report
    assert report is not None and query is not None
    if not job.accepted:
        await query.answer(NOTHING_ACCEPTED_TOAST, show_alert=True)
        return
    try:
        revised = revise(report, job.accepted)
    except RevisionError:
        await query.answer(STALE_TOAST, show_alert=True)
        return
    await query.answer()
    if not await b._passes_delivery_gate(update, context):
        return
    count = len(job.accepted)
    # Plain text on purpose: the user copies it.
    await b.reply_to_user(
        update,
        f"修订后的译文（已采纳 {count} 条）：\n\n{revised}",
        retry_gate=lambda: b._passes_delivery_gate(update, context),
    )


