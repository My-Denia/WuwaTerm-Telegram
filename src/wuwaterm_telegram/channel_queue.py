"""Bounded, recoverable channel translation retry queue with strict privacy bounds.

Explicit task state machine:
    pending -> in_flight -> completed
                         -> pending (exponential backoff while attempts < max_attempts & not expired)
                         -> failed  (non-retryable or max_attempts exhausted)
                         -> expired (past ttl_seconds)
    pending -> expired (past ttl_seconds)

Privacy & self-hosting invariants:
- Pure in-memory bounded queue by default (``storage_path=None``).
- Optional disk persistence requires explicit opt-in, writes atomically with
  ``0o600`` permissions, enforces ``ttl_seconds`` and ``max_size``, and wipes
  message text immediately on completion, failure, or expiration.
- Message text is NEVER written to logs or ``/status`` diagnostics.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from enum import Enum
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Awaitable, Callable

from wuwaterm.logging_utils import redact_id, safe_error_type


LOGGER = logging.getLogger(__name__)
QUEUE_SCHEMA_VERSION = 1
DEFAULT_QUEUE_MAX_SIZE = 128
DEFAULT_QUEUE_TTL_SECONDS = 3600.0
DEFAULT_QUEUE_MAX_ATTEMPTS = 5
DEFAULT_QUEUE_BASE_BACKOFF_SECONDS = 5.0
DEFAULT_QUEUE_MAX_BACKOFF_SECONDS = 120.0
DEFAULT_QUEUE_POLL_INTERVAL_SECONDS = 1.0


class ChannelTaskState(str, Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class ChannelTask:
    """One queued channel translation task."""

    task_id: str
    chat_id: int
    message_id: int
    chat_type: str
    plain: str
    html: str | None
    is_edit: bool
    update_id: int | None
    message_date_ts: float | None
    length_limit: int
    state: ChannelTaskState
    attempts: int
    created_at: float
    updated_at: float
    next_attempt_at: float
    expires_at: float
    last_reason: str = ""

    @property
    def post_key(self) -> tuple[int, int]:
        return (self.chat_id, self.message_id)


@dataclass(frozen=True)
class ChannelQueueStats:
    """Privacy-safe aggregate counters for /status."""

    pending: int
    in_flight: int
    completed: int
    failed: int
    expired: int
    superseded: int
    dropped_overflow: int
    persistence_enabled: bool


@dataclass(frozen=True)
class TaskExecutionOutcome:
    """Result returned by a queue consumer callback."""

    status: str  # "completed" | "retry" | "failed" | "expired"
    reason: str = ""


TaskExecutor = Callable[[ChannelTask], Awaitable[TaskExecutionOutcome]]


class ChannelTaskQueue:
    """Bounded, deduplicating, backoff-retryable queue for channel posts."""

    def __init__(
        self,
        *,
        max_size: int = DEFAULT_QUEUE_MAX_SIZE,
        ttl_seconds: float = DEFAULT_QUEUE_TTL_SECONDS,
        max_attempts: int = DEFAULT_QUEUE_MAX_ATTEMPTS,
        base_backoff_seconds: float = DEFAULT_QUEUE_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_QUEUE_MAX_BACKOFF_SECONDS,
        storage_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_backoff_seconds < 0:
            raise ValueError("base_backoff_seconds must be non-negative")
        if max_backoff_seconds < base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")

        self.max_size = max_size
        self.ttl_seconds = float(ttl_seconds)
        self.max_attempts = max_attempts
        self.base_backoff_seconds = float(base_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.storage_path = Path(storage_path) if storage_path is not None else None
        self._clock = clock

        self._tasks: dict[tuple[int, int], ChannelTask] = {}
        self._counts: Counter[str] = Counter()
        self._wakeup = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self._stop_requested = False

        if self.storage_path is not None:
            self._load()

    def persistence_enabled(self) -> bool:
        return self.storage_path is not None

    def backoff_delay(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        return min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2**exponent),
        )

    def enqueue(
        self,
        *,
        chat_id: int,
        message_id: int,
        chat_type: str,
        plain: str,
        html: str | None,
        is_edit: bool,
        update_id: int | None,
        message_date_ts: float | None,
        length_limit: int,
        reason: str,
        initial_delay_seconds: float | None = None,
    ) -> ChannelTask | None:
        """Enqueue a channel post or overwrite an existing pending task for the same post."""
        now = self._clock()
        self.prune_expired(now=now)
        key = (chat_id, message_id)
        delay = (
            self.base_backoff_seconds
            if initial_delay_seconds is None
            else max(0.0, float(initial_delay_seconds))
        )
        expires_at = now + self.ttl_seconds
        if message_date_ts is not None:
            # Never keep a task past the post's natural replay/freshness horizon
            # or queue TTL, whichever is shorter.
            expires_at = min(expires_at, max(now + 1.0, message_date_ts + self.ttl_seconds))

        existing = self._tasks.get(key)
        if existing is not None:
            if existing.state == ChannelTaskState.PENDING:
                # Newer edit/update overwrites the queued payload in place.
                existing.plain = plain
                existing.html = html
                existing.is_edit = existing.is_edit or is_edit
                existing.update_id = update_id
                existing.message_date_ts = message_date_ts
                existing.length_limit = length_limit
                existing.updated_at = now
                existing.next_attempt_at = min(existing.next_attempt_at, now + delay)
                existing.expires_at = expires_at
                existing.last_reason = reason
                self._counts["superseded"] += 1
                self._persist()
                self._signal_wakeup()
                LOGGER.info(
                    "channel_queue action=superseded chat=%s message=%s reason=%s pending=%s",
                    redact_id(chat_id),
                    redact_id(message_id),
                    reason,
                    self._pending_count(),
                )
                return existing
            # If currently in-flight, keep the in-flight task running; do not
            # create a second duplicate entry for the same post key.
            return existing

        if len(self._tasks) >= self.max_size:
            self._counts["dropped_overflow"] += 1
            LOGGER.warning(
                "channel_queue action=dropped_overflow chat=%s message=%s reason=%s size=%s",
                redact_id(chat_id),
                redact_id(message_id),
                reason,
                len(self._tasks),
            )
            return None

        task = ChannelTask(
            task_id=f"{chat_id}:{message_id}:{int(now * 1000)}",
            chat_id=chat_id,
            message_id=message_id,
            chat_type=chat_type,
            plain=plain,
            html=html,
            is_edit=is_edit,
            update_id=update_id,
            message_date_ts=message_date_ts,
            length_limit=length_limit,
            state=ChannelTaskState.PENDING,
            attempts=0,
            created_at=now,
            updated_at=now,
            next_attempt_at=now + delay,
            expires_at=expires_at,
            last_reason=reason,
        )
        self._tasks[key] = task
        self._persist()
        self._signal_wakeup()
        LOGGER.info(
            "channel_queue action=enqueued chat=%s message=%s edit=%s reason=%s pending=%s",
            redact_id(chat_id),
            redact_id(message_id),
            is_edit,
            reason,
            self._pending_count(),
        )
        return task

    def cancel_pending(self, chat_id: int, message_id: int) -> bool:
        """Remove a pending task when the post was delivered directly."""
        key = (chat_id, message_id)
        task = self._tasks.get(key)
        if task is None or task.state != ChannelTaskState.PENDING:
            return False
        self._wipe_and_remove(key, ChannelTaskState.COMPLETED, count_key=None)
        self._persist()
        return True

    def get(self, chat_id: int, message_id: int) -> ChannelTask | None:
        return self._tasks.get((chat_id, message_id))

    def prune_expired(self, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        expired_keys = [
            key
            for key, task in self._tasks.items()
            if task.state == ChannelTaskState.PENDING and task.expires_at <= now
        ]
        for key in expired_keys:
            self._wipe_and_remove(key, ChannelTaskState.EXPIRED, count_key="expired")
        if expired_keys:
            self._persist()
            LOGGER.info("channel_queue action=expired count=%s", len(expired_keys))
        return len(expired_keys)

    def lease_due(self, *, now: float | None = None) -> ChannelTask | None:
        """Transition the earliest due pending task to IN_FLIGHT."""
        now = self._clock() if now is None else now
        self.prune_expired(now=now)
        due = [
            task
            for task in self._tasks.values()
            if task.state == ChannelTaskState.PENDING and task.next_attempt_at <= now
        ]
        if not due:
            return None
        due.sort(key=lambda item: (item.next_attempt_at, item.created_at))
        chosen = due[0]
        chosen.state = ChannelTaskState.IN_FLIGHT
        chosen.attempts += 1
        chosen.updated_at = now
        self._persist()
        return chosen

    def complete(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        task = self._tasks.get(key)
        if task is None:
            return
        self._wipe_and_remove(key, ChannelTaskState.COMPLETED, count_key="completed")
        self._persist()
        LOGGER.info(
            "channel_queue action=completed chat=%s message=%s attempts=%s",
            redact_id(chat_id),
            redact_id(message_id),
            task.attempts,
        )

    def expire(self, chat_id: int, message_id: int, *, reason: str = "expired") -> None:
        key = (chat_id, message_id)
        task = self._tasks.get(key)
        if task is None:
            return
        task.last_reason = reason
        self._wipe_and_remove(key, ChannelTaskState.EXPIRED, count_key="expired")
        self._persist()
        LOGGER.info(
            "channel_queue action=expired chat=%s message=%s reason=%s",
            redact_id(chat_id),
            redact_id(message_id),
            reason,
        )

    def fail_or_requeue(
        self,
        chat_id: int,
        message_id: int,
        *,
        reason: str,
        retryable: bool = True,
        now: float | None = None,
    ) -> ChannelTaskState:
        """Retry with exponential backoff or move to terminal failed/expired state."""
        now = self._clock() if now is None else now
        key = (chat_id, message_id)
        task = self._tasks.get(key)
        if task is None:
            return ChannelTaskState.FAILED
        task.last_reason = reason
        task.updated_at = now
        if now >= task.expires_at:
            self._wipe_and_remove(key, ChannelTaskState.EXPIRED, count_key="expired")
            self._persist()
            return ChannelTaskState.EXPIRED
        if retryable and task.attempts < self.max_attempts:
            delay = self.backoff_delay(task.attempts)
            if now + delay >= task.expires_at:
                self._wipe_and_remove(key, ChannelTaskState.EXPIRED, count_key="expired")
                self._persist()
                return ChannelTaskState.EXPIRED
            task.state = ChannelTaskState.PENDING
            task.next_attempt_at = now + delay
            self._persist()
            self._signal_wakeup()
            LOGGER.info(
                "channel_queue action=requeued chat=%s message=%s attempts=%s "
                "delay=%.1fs reason=%s",
                redact_id(chat_id),
                redact_id(message_id),
                task.attempts,
                delay,
                reason,
            )
            return ChannelTaskState.PENDING
        self._wipe_and_remove(key, ChannelTaskState.FAILED, count_key="failed")
        self._persist()
        LOGGER.warning(
            "channel_queue action=failed chat=%s message=%s attempts=%s reason=%s",
            redact_id(chat_id),
            redact_id(message_id),
            task.attempts,
            reason,
        )
        return ChannelTaskState.FAILED

    async def process_once(
        self, executor: TaskExecutor, *, now: float | None = None
    ) -> bool:
        """Process at most one due task; returns True if a task was executed."""
        task = self.lease_due(now=now)
        if task is None:
            return False
        try:
            outcome = await executor(task)
        except asyncio.CancelledError:
            # Return to pending on cancellation so shutdown does not drop work.
            task.state = ChannelTaskState.PENDING
            task.attempts = max(0, task.attempts - 1)
            self._persist()
            raise
        except Exception as exc:  # noqa: BLE001 - worker must never crash loop
            self.fail_or_requeue(
                task.chat_id,
                task.message_id,
                reason=safe_error_type(exc),
                retryable=True,
                now=now,
            )
            return True

        if outcome.status == "completed":
            self.complete(task.chat_id, task.message_id)
        elif outcome.status == "expired":
            self.expire(task.chat_id, task.message_id, reason=outcome.reason or "expired")
        elif outcome.status == "retry":
            self.fail_or_requeue(
                task.chat_id,
                task.message_id,
                reason=outcome.reason or "retry",
                retryable=True,
                now=now,
            )
        else:
            self.fail_or_requeue(
                task.chat_id,
                task.message_id,
                reason=outcome.reason or "failed",
                retryable=False,
                now=now,
            )
        return True

    def start(
        self,
        executor: TaskExecutor,
        *,
        poll_interval_seconds: float = DEFAULT_QUEUE_POLL_INTERVAL_SECONDS,
    ) -> None:
        """Start the background consumer task on the active event loop."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_requested = False
        loop = asyncio.get_running_loop()
        self._worker_task = loop.create_task(
            self._run_worker(executor, poll_interval_seconds=poll_interval_seconds),
            name="wuwaterm-channel-queue-worker",
        )

    async def stop(self) -> None:
        """Stop the background consumer cleanly and persist remaining state."""
        self._stop_requested = True
        self._signal_wakeup()
        task = self._worker_task
        self._worker_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.prune_expired()
        self._persist()

    async def _run_worker(
        self, executor: TaskExecutor, *, poll_interval_seconds: float
    ) -> None:
        while not self._stop_requested:
            try:
                processed = await self.process_once(executor)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - keep worker alive
                LOGGER.warning(
                    "channel_queue worker iteration error error_type=%s",
                    safe_error_type(exc),
                )
                processed = False
            if processed:
                await asyncio.sleep(0)
                continue
            wait_seconds = self._next_wait_seconds(poll_interval_seconds)
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

    def _next_wait_seconds(self, default_interval: float) -> float:
        now = self._clock()
        pending = [
            task.next_attempt_at
            for task in self._tasks.values()
            if task.state == ChannelTaskState.PENDING
        ]
        if not pending:
            return default_interval
        soonest = min(pending) - now
        return max(0.05, min(default_interval, soonest))

    def _pending_count(self) -> int:
        return sum(
            1 for task in self._tasks.values() if task.state == ChannelTaskState.PENDING
        )

    def _in_flight_count(self) -> int:
        return sum(
            1 for task in self._tasks.values() if task.state == ChannelTaskState.IN_FLIGHT
        )

    def stats(self) -> ChannelQueueStats:
        self.prune_expired()
        return ChannelQueueStats(
            pending=self._pending_count(),
            in_flight=self._in_flight_count(),
            completed=self._counts["completed"],
            failed=self._counts["failed"],
            expired=self._counts["expired"],
            superseded=self._counts["superseded"],
            dropped_overflow=self._counts["dropped_overflow"],
            persistence_enabled=self.persistence_enabled(),
        )

    def _wipe_and_remove(
        self,
        key: tuple[int, int],
        terminal_state: ChannelTaskState,
        *,
        count_key: str | None,
    ) -> None:
        task = self._tasks.pop(key, None)
        if task is not None:
            task.state = terminal_state
            # Immediately wipe text payloads so completed/failed/expired text
            # is not retained in memory or flushed to disk.
            task.plain = ""
            task.html = None
        if count_key is not None:
            self._counts[count_key] += 1

    def _signal_wakeup(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wakeup.set()

    def _load(self) -> None:
        if self.storage_path is None or not self.storage_path.exists():
            return
        try:
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            LOGGER.warning("channel_queue persistence file unreadable, starting empty")
            return
        if not isinstance(raw, dict) or raw.get("version") != QUEUE_SCHEMA_VERSION:
            LOGGER.warning("channel_queue persistence schema invalid, starting empty")
            return
        entries = raw.get("tasks")
        if not isinstance(entries, list):
            return
        now = self._clock()
        changed = False
        for item in entries:
            if not isinstance(item, dict):
                changed = True
                continue
            try:
                chat_id = int(item["chat_id"])
                message_id = int(item["message_id"])
                expires_at = float(item["expires_at"])
                if expires_at <= now:
                    self._counts["expired"] += 1
                    changed = True
                    continue
                if len(self._tasks) >= self.max_size:
                    self._counts["dropped_overflow"] += 1
                    changed = True
                    continue
                # Any task left in_flight before process crash recovers as pending.
                task = ChannelTask(
                    task_id=str(item["task_id"]),
                    chat_id=chat_id,
                    message_id=message_id,
                    chat_type=str(item.get("chat_type", "supergroup")),
                    plain=str(item["plain"]),
                    html=item.get("html") if isinstance(item.get("html"), str) else None,
                    is_edit=bool(item.get("is_edit", False)),
                    update_id=(
                        int(item["update_id"])
                        if isinstance(item.get("update_id"), int)
                        else None
                    ),
                    message_date_ts=(
                        float(item["message_date_ts"])
                        if item.get("message_date_ts") is not None
                        else None
                    ),
                    length_limit=int(item.get("length_limit", 4096)),
                    state=ChannelTaskState.PENDING,
                    attempts=max(0, int(item.get("attempts", 0))),
                    created_at=float(item.get("created_at", now)),
                    updated_at=now,
                    next_attempt_at=max(now, float(item.get("next_attempt_at", now))),
                    expires_at=min(expires_at, now + self.ttl_seconds),
                    last_reason=str(item.get("last_reason", "")),
                )
                self._tasks[(chat_id, message_id)] = task
            except (KeyError, TypeError, ValueError):
                changed = True
                continue
        if changed:
            self._persist()

    def _persist(self) -> None:
        if self.storage_path is None:
            return
        payload = {
            "version": QUEUE_SCHEMA_VERSION,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "chat_id": task.chat_id,
                    "message_id": task.message_id,
                    "chat_type": task.chat_type,
                    "plain": task.plain,
                    "html": task.html,
                    "is_edit": task.is_edit,
                    "update_id": task.update_id,
                    "message_date_ts": task.message_date_ts,
                    "length_limit": task.length_limit,
                    "state": task.state.value,
                    "attempts": task.attempts,
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "next_attempt_at": task.next_attempt_at,
                    "expires_at": task.expires_at,
                    "last_reason": task.last_reason,
                }
                for task in self._tasks.values()
                if task.state in {ChannelTaskState.PENDING, ChannelTaskState.IN_FLIGHT}
            ],
        }
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=f".{self.storage_path.name}.", dir=self.storage_path.parent
            )
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.storage_path)
                if os.name != "nt":
                    os.chmod(self.storage_path, 0o600)
                    dir_fd = os.open(
                        self.storage_path.parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                    )
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            LOGGER.warning(
                "channel_queue persistence write failed error_type=%s",
                safe_error_type(exc),
            )
