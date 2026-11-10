"""In-process `/review` jobs and `/tr` translation records. Memory only.

Nothing here touches disk or logs text. Entries expire `ttl_seconds` after
creation (an entry exactly at the TTL is expired) and each chat keeps at most
`max_per_chat` of each kind. Ids are short enough for Telegram callback_data.
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
    """The chat already has the maximum number of jobs and all are running."""


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
    state: str = STATE_RUNNING
    report: ReviewReport | None = None
    accepted: dict[str, int] = field(default_factory=dict)  # issue.key -> choice
    skipped: set[str] = field(default_factory=set)
    result_message_ids: list[int] = field(default_factory=list)
    keyboard_message_id: int | None = None
    # True once the result reached the chat (message ids are known). A finished
    # job that is not delivered is sent again from the stored report.
    delivered: bool = False
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
        for job in reversed(self._live_jobs(chat_id)):
            if job.content_key == content_key and job.state != STATE_FAILED:
                return job
        return None

    def create(
        self,
        chat_id: int,
        user_id: int | None,
        source: str,
        target: str,
        direction: str,
    ) -> ReviewJob:
        self.purge()
        jobs = self._jobs.setdefault(chat_id, [])
        while len(jobs) >= self._max:
            victim = next((j for j in jobs if j.state != STATE_RUNNING), None)
            if victim is None:
                raise StoreFullError("every review slot in this chat is running")
            jobs.remove(victim)
        job = ReviewJob(
            id=self._new_id(),
            chat_id=chat_id,
            user_id=user_id,
            source=source,
            target=target,
            direction=direction,
            content_key=self.content_key(source, target, direction),
            created_at=self._clock(),
        )
        jobs.append(job)
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

    def latest_done(self, chat_id: int) -> ReviewJob | None:
        for job in reversed(self._live_jobs(chat_id)):
            if job.state == STATE_DONE:
                return job
        return None

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
