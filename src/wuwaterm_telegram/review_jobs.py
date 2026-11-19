"""In-process `/review` jobs and `/tr` translation records. Memory only.

Nothing here touches disk or logs text. Entries expire `ttl_seconds` after
creation (an entry exactly at the TTL is expired) and each chat keeps at most
`max_per_chat` of each kind. Ids are short enough for Telegram callback_data.

A job carries two clocks. `created_at` starts the job's own life: its delivery
window and the inline buttons that belong to that one requester. The report it
shows is dated by `report_created_at`. A job that reuses a computed report keeps
the report's time there, so a chain of reuses never keeps a report past its own
TTL, while the requester who was handed a reused report still gets a full
window on her own buttons.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

from wuwaterm_telegram.review_model import ReviewReport

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"


class StoreFullError(RuntimeError):
    """Every review slot in the chat is running or delivering a result."""


# Results of `ReviewJobStore.join_waiters`.
WAITER_QUEUED = "queued"
WAITER_ALREADY = "already"
WAITER_FULL = "full"


@dataclass
class TranslationRecord:
    id: str
    chat_id: int
    user_id: int | None
    source: str
    target: str
    direction: str  # language of the translation: "en" / "zh"
    message_ids: tuple[int, ...]
    created_at: float


@dataclass(frozen=True)
class Waiter:
    """A requester waiting for a report another job in the chat is computing.

    `update` is that requester's own triggering update: the bot needs it to
    answer into her chat and to re-check her authorization when the report is
    ready. It is opaque here, so the store keeps no dependency on Telegram, and
    it lives only until `drain_waiters` drops it.
    """

    user_id: int | None
    update: object


@dataclass
class ReviewJob:
    id: str
    chat_id: int
    user_id: int | None
    source: str
    target: str
    direction: str
    content_key: str
    created_at: float
    # When the report this job shows was computed. Equal to `created_at` for a
    # job that ran the review; the source's value for one that reuses a report.
    report_created_at: float
    state: str = STATE_RUNNING
    report: ReviewReport | None = None
    accepted: dict[str, int] = field(default_factory=dict)  # issue.key -> choice
    skipped: set[str] = field(default_factory=set)
    result_message_ids: list[int] = field(default_factory=list)
    # True once the result reached the chat (message ids are known). A finished
    # job that is not delivered is sent again from the stored report.
    delivered: bool = False
    # True while a send of this job's result is awaiting Telegram, so a repeat
    # that arrives meanwhile waits instead of sending the result a second time.
    delivering: bool = False
    # Other requesters of the same content, served when this job finishes.
    waiters: list[Waiter] = field(default_factory=list)
    finished_at: float | None = None


class ReviewJobStore:
    def __init__(
        self,
        *,
        ttl_seconds: float = 1800.0,
        max_per_chat: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_per_chat
        self._clock = clock
        self._jobs: dict[int, list[ReviewJob]] = {}  # oldest first
        self._translations: dict[int, list[TranslationRecord]] = {}

    @staticmethod
    def content_key(source: str, target: str, direction: str) -> str:
        payload = json.dumps([source, target, direction], ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _expired(self, created_at: float) -> bool:
        return self._clock() - created_at >= self._ttl

    def _new_id(self) -> str:
        used = {x.id for items in self._jobs.values() for x in items}
        used |= {x.id for items in self._translations.values() for x in items}
        while True:
            candidate = secrets.token_urlsafe(6)
            if candidate not in used:
                return candidate

    def _live_jobs(self, chat_id: int) -> list[ReviewJob]:
        return [j for j in self._jobs.get(chat_id, ()) if not self._expired(j.created_at)]

    def purge(self) -> int:
        removed = 0
        for table in (self._jobs, self._translations):
            for chat_id in list(table):
                kept = [x for x in table[chat_id] if not self._expired(x.created_at)]
                removed += len(table[chat_id]) - len(kept)
                if kept:
                    table[chat_id] = kept
                else:
                    del table[chat_id]
        return removed

    def find_duplicate(self, chat_id: int, content_key: str) -> ReviewJob | None:
        """The chat's job for this content whose report may still be served.

        The bound is the report's age, not the job's: `report_created_at`
        reaches one TTL one TTL after the model answered, so however many times
        a report is reused, the next `/review` of the same content after that
        point runs a fresh one.
        """
        for job in reversed(self._live_jobs(chat_id)):
            if (
                job.content_key == content_key
                and job.state != STATE_FAILED
                and not self._expired(job.report_created_at)
            ):
                return job
        return None

    def find_owner_duplicate(
        self, chat_id: int, content_key: str, user_id: int | None
    ) -> ReviewJob | None:
        """The requester's own job for this content, if they already have one.

        The chat-wide lookup keeps the model work deduplicated; this narrower
        one answers a repeat submission with the buttons and acceptance state
        that belong to the person who sent it, not to whoever asked first.

        It deliberately follows the job's own `created_at`, not the report's,
        because those buttons are the job's: a requester who received a reused
        report late still keeps the full window on her own result.
        """
        for job in reversed(self._live_jobs(chat_id)):
            if (
                job.content_key == content_key
                and job.state != STATE_FAILED
                and job.user_id == user_id
            ):
                return job
        return None

    @property
    def waiter_limit(self) -> int:
        """How many other requesters may wait on one job.

        One slot stays free for the waiters' own jobs: `create` evicts the
        oldest job that is neither running nor delivering, so their results must
        fit without pushing each other out.
        """
        return max(0, self._max - 1)

    def join_waiters(
        self, job: ReviewJob, user_id: int | None, update: object
    ) -> str:
        """Queue a requester who submitted this content while it was running.

        Returns `WAITER_QUEUED`, `WAITER_ALREADY` (this requester is queued) or
        `WAITER_FULL`. `update` is her own submission; it is dropped by
        `drain_waiters` as soon as the job finishes.
        """
        if any(waiter.user_id == user_id for waiter in job.waiters):
            return WAITER_ALREADY
        if len(job.waiters) >= self.waiter_limit:
            return WAITER_FULL
        job.waiters.append(Waiter(user_id=user_id, update=update))
        return WAITER_QUEUED

    def drain_waiters(self, job: ReviewJob) -> list[Waiter]:
        """Take the waiting requesters off the job in one step."""
        waiters, job.waiters = list(job.waiters), []
        return waiters

    def create(
        self,
        chat_id: int,
        user_id: int | None,
        source: str,
        target: str,
        direction: str,
        *,
        preserve: ReviewJob | None = None,
    ) -> ReviewJob:
        self.purge()
        jobs = self._jobs.setdefault(chat_id, [])
        while len(jobs) >= self._max:
            # A job whose result is on its way to Telegram is not a victim: its
            # message ids are still being collected, and dropping it mid-send
            # would lose the result the requester is already watching for.
            victim = next(
                (
                    j
                    for j in jobs
                    if j.state != STATE_RUNNING and not j.delivering and j is not preserve
                ),
                None,
            )
            if victim is None:
                raise StoreFullError(
                    "every review slot in this chat is running or being delivered"
                )
            jobs.remove(victim)
        now = self._clock()
        job = ReviewJob(
            id=self._new_id(),
            chat_id=chat_id,
            user_id=user_id,
            source=source,
            target=target,
            direction=direction,
            content_key=self.content_key(source, target, direction),
            created_at=now,
            report_created_at=now,
        )
        jobs.append(job)
        return job

    def create_reusing(
        self, chat_id: int, user_id: int | None, source: ReviewJob
    ) -> ReviewJob:
        """A later requester's own job over an already computed report.

        The report is never mutated after `finish`, so both jobs read the same
        object: reusing it costs no model call and no report copy. Acceptance,
        skipped issues and message ids stay per job. `source` is never the job
        evicted for the new one, so the first requester keeps her buttons.

        Two clocks, on purpose. `created_at` is now, so this requester gets her
        own full `ttl_seconds` window on her own result and buttons.
        `report_created_at` keeps the source's time, so reuse never extends how
        long the computed report is served to the chat: `find_duplicate` stops
        matching it at one TTL after it was computed, and the next `/review` of
        the same content runs a fresh one.

        This reverses the trade-off PR #38 (`92b3ef2`) chose, where a reusing job
        simply inherited `source.created_at`: then a member handed a reused
        report near the end of its life got buttons that expired seconds later.
        The price is that a reused result's buttons can stay interactive for up
        to about two TTLs after the report was computed. PR #38's actual bound is
        kept: the report itself is deduplicated for at most one TTL.
        """
        if source.report is None:
            raise ValueError("only a computed review report can be reused")
        job = self.create(
            chat_id, user_id, source.source, source.target, source.direction, preserve=source
        )
        job.created_at = self._clock()
        job.report_created_at = source.report_created_at
        job.report = source.report
        job.state = STATE_DONE
        job.finished_at = self._clock()
        return job

    def get(self, chat_id: int, job_id: str) -> ReviewJob | None:
        for job in self._live_jobs(chat_id):
            if job.id == job_id:
                return job
        return None

    def running(self, chat_id: int) -> ReviewJob | None:
        for job in self._live_jobs(chat_id):
            if job.state == STATE_RUNNING:
                return job
        return None

    def has_slot(self, chat_id: int) -> bool:
        """Whether `create` would find or free a slot in this chat right now."""
        live = self._live_jobs(chat_id)
        return len(live) < self._max or any(
            j.state != STATE_RUNNING and not j.delivering for j in live
        )

    def done_jobs(self, chat_id: int) -> list[ReviewJob]:
        """The chat's finished jobs, newest first."""
        return [j for j in reversed(self._live_jobs(chat_id)) if j.state == STATE_DONE]

    def finish(self, job: ReviewJob, report: ReviewReport) -> None:
        job.report = report
        job.state = STATE_DONE
        job.finished_at = self._clock()

    def fail(self, job: ReviewJob) -> None:
        job.state = STATE_FAILED
        job.finished_at = self._clock()

    def remember_translation(
        self,
        chat_id: int,
        user_id: int | None,
        source: str,
        target: str,
        direction: str,
        message_ids: tuple[int, ...] | list[int],
    ) -> TranslationRecord:
        self.purge()
        records = self._translations.setdefault(chat_id, [])
        while len(records) >= self._max:
            records.pop(0)
        record = TranslationRecord(
            id=self._new_id(),
            chat_id=chat_id,
            user_id=user_id,
            source=source,
            target=target,
            direction=direction,
            message_ids=tuple(message_ids),
            created_at=self._clock(),
        )
        records.append(record)
        return record

    def _live_translations(self, chat_id: int) -> list[TranslationRecord]:
        return [r for r in self._translations.get(chat_id, ()) if not self._expired(r.created_at)]

    def get_translation(self, chat_id: int, record_id: str) -> TranslationRecord | None:
        for record in self._live_translations(chat_id):
            if record.id == record_id:
                return record
        return None

    def translation_for_message(self, chat_id: int, message_id: int) -> TranslationRecord | None:
        for record in reversed(self._live_translations(chat_id)):
            if message_id in record.message_ids:
                return record
        return None

    def stats(self) -> dict[str, int]:
        self.purge()
        return {
            "jobs": sum(len(v) for v in self._jobs.values()),
            "translations": sum(len(v) for v in self._translations.values()),
        }
