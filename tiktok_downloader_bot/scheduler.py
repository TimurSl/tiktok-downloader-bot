"""Runs download jobs on a thread pool with fair per-user limits."""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Hashable

LOG = logging.getLogger("tiktok_downloader_bot")


class JobScheduler:
    """At most ``max_workers`` jobs run at once and at most ``per_key_limit`` per user.

    Jobs that cannot start yet wait in a queue without occupying a worker, so one busy
    user never blocks everybody else. ``max_pending_per_key`` caps what one user can queue.
    """

    def __init__(self, max_workers: int, per_key_limit: int, max_pending_per_key: int = 8) -> None:
        self.max_workers = max_workers
        self.per_key_limit = per_key_limit
        self.max_pending_per_key = max_pending_per_key
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job")
        self.lock = threading.Lock()
        self.pending: deque[tuple[Hashable, Callable[[], None]]] = deque()
        self.running_total = 0
        self.running_by_key: dict[Hashable, int] = {}
        self.queued_by_key: dict[Hashable, int] = {}
        self.closed = False

    def submit(self, key: Hashable, job: Callable[[], None]) -> bool:
        """Queue a job. Returns False when ``key`` already has too many jobs."""
        with self.lock:
            if self.closed:
                return False
            in_flight = self.running_by_key.get(key, 0) + self.queued_by_key.get(key, 0)
            if in_flight >= self.max_pending_per_key:
                return False
            self.queued_by_key[key] = self.queued_by_key.get(key, 0) + 1
            self.pending.append((key, job))
        self.dispatch()
        return True

    def dispatch(self) -> None:
        to_start: list[tuple[Hashable, Callable[[], None]]] = []
        with self.lock:
            waiting: deque[tuple[Hashable, Callable[[], None]]] = deque()
            while self.pending:
                key, job = self.pending.popleft()
                if (
                    not self.closed
                    and self.running_total < self.max_workers
                    and self.running_by_key.get(key, 0) < self.per_key_limit
                ):
                    self.queued_by_key[key] -= 1
                    if not self.queued_by_key[key]:
                        del self.queued_by_key[key]
                    self.running_by_key[key] = self.running_by_key.get(key, 0) + 1
                    self.running_total += 1
                    to_start.append((key, job))
                else:
                    waiting.append((key, job))
            self.pending = waiting
        for key, job in to_start:
            self.executor.submit(self.run, key, job)

    def run(self, key: Hashable, job: Callable[[], None]) -> None:
        try:
            job()
        except Exception:
            LOG.exception("Unhandled error in background job")
        finally:
            with self.lock:
                self.running_total -= 1
                self.running_by_key[key] -= 1
                if not self.running_by_key[key]:
                    del self.running_by_key[key]
            self.dispatch()

    def counts(self) -> tuple[int, int]:
        """(running, queued)"""
        with self.lock:
            return self.running_total, len(self.pending)

    def shutdown(self) -> None:
        """Stop accepting jobs, drop queued ones, and wait for running ones to finish."""
        with self.lock:
            self.closed = True
            self.pending.clear()
            self.queued_by_key.clear()
        self.executor.shutdown(wait=True, cancel_futures=True)
