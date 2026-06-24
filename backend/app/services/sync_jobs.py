"""In-process background-job registry for long-running admin syncs.

The ``POST /admin/trial-sync`` endpoint used to call
``run_manual_sync_task.delay(...).get(timeout=1800)`` synchronously, which
blocked the HTTP request handler for the entire sync — up to 30 minutes
for a catalog-wide "sync everything" run.  In eager mode (``USE_CELERY=false``)
the task body executes inline, so the request thread is held the whole
time; in Celery mode the ``.get()`` parks a web worker waiting on the
broker.  Either way the browser hangs and proxies time the request out.

This registry decouples the request from the work: the endpoint submits a
job, gets a ``job_id`` back immediately, and the actual sync runs on a
daemon thread.  Clients poll ``GET /admin/trial-sync/{job_id}`` for status.

Scope / caveats
---------------
State lives in-process.  That's correct for the eager single-process dev
deployment this project ships with, and remains correct in a single-web-
worker Celery deployment (the background thread blocks on the broker, not
the request handler).  A multi-process web tier would need a shared store
(Redis / Celery ``AsyncResult``) so a poll landing on a different process
can still see the job — wired the same way, just swapping this registry
for ``AsyncResult`` lookups.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# How long a finished job stays pollable before it's pruned.
_JOB_TTL = timedelta(hours=1)


@dataclass
class SyncJob:
    """A single background sync invocation and its lifecycle state."""

    id: str
    status: str = "queued"  # queued | running | succeeded | failed
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class JobRegistry:
    """Thread-safe registry of in-flight and recently-finished sync jobs."""

    def __init__(self) -> None:
        self._jobs: dict[str, SyncJob] = {}
        self._lock = threading.Lock()

    def create(self) -> SyncJob:
        job = SyncJob(id=str(uuid.uuid4()))
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[SyncJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def active_count(self) -> int:
        """Number of jobs currently queued or running."""
        with self._lock:
            return sum(
                1 for j in self._jobs.values()
                if j.status in ("queued", "running")
            )

    def run_in_background(self, job: SyncJob, fn: Callable[[], dict]) -> None:
        """Execute ``fn`` on a daemon thread, recording status transitions."""

        def _runner() -> None:
            self._update(job.id, status="running", started_at=_now())
            try:
                result = fn()
                self._update(
                    job.id,
                    status="succeeded",
                    result=result if isinstance(result, dict) else {"result": result},
                    finished_at=_now(),
                )
            except Exception as exc:  # noqa: BLE001 - report, don't crash the thread
                logger.exception("Background sync job %s failed", job.id)
                self._update(
                    job.id, status="failed", error=str(exc), finished_at=_now()
                )

        thread = threading.Thread(
            target=_runner, name=f"sync-job-{job.id[:8]}", daemon=True
        )
        thread.start()

    # -- internals ----------------------------------------------------------

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for key, value in changes.items():
                setattr(job, key, value)

    def _prune_locked(self) -> None:
        cutoff = _now() - _JOB_TTL
        stale = [
            jid for jid, job in self._jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]
        for jid in stale:
            del self._jobs[jid]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Module-level singleton shared by the admin router.
registry = JobRegistry()
