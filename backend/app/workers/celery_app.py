"""Celery application factory.

The system runs in **two modes** with **one set of task definitions**:

* **Dev / synchronous mode** (``USE_CELERY=false`` in ``.env``) —
  Celery's ``task_always_eager`` flag makes every ``.delay()`` call
  execute the task body *inline*, in the current process, with no broker
  required.  No Redis, no docker, no extra ports — just import and run.

* **Production / asynchronous mode** (``USE_CELERY=true``) — switches
  to a real Redis broker + result backend and enables ``celery beat``
  for the periodic schedule.

A single Celery instance is built and configured for whichever mode is
active.  Worker modules can therefore decorate their functions with
``@celery_app.task`` and never have to branch on the mode.

Beat schedule
-------------
Defined here even in eager mode (it's still inspectable as
``celery_app.conf.beat_schedule``) — production deployments just need
to start ``celery beat`` against the same app to activate it.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

logger = logging.getLogger(__name__)


def _build_app() -> Celery:
    """Construct and configure the Celery app for the current settings.

    Reading settings inside the factory (rather than at import time) lets
    tests monkey-patch :func:`app.config.get_settings` and rebuild the
    app via :func:`reset_celery_app`.
    """
    settings = get_settings()

    # When running eagerly, broker / backend URLs are still required by
    # Celery's config validator but are never opened — point them at
    # ``memory://`` so a misconfigured ``USE_CELERY=true`` flip without
    # a real Redis URL fails loudly rather than silently using memory.
    broker_url = settings.REDIS_URL if settings.USE_CELERY else "memory://"
    backend_url = settings.REDIS_URL if settings.USE_CELERY else "cache+memory://"

    app = Celery(
        "trial_matcher",
        broker=broker_url,
        backend=backend_url,
        include=[
            # Listed up-front so ``celery worker`` auto-imports them.
            "app.workers.trial_sync_worker",
            "app.workers.rematch_worker",
            "app.workers.feedback_worker",
        ],
    )

    app.conf.update(
        # Eager mode.  ``True`` means tasks execute in-process when
        # ``.delay()`` or ``.apply_async()`` is called; ``False`` means
        # they go to the broker.
        task_always_eager=not settings.USE_CELERY,
        # Surface exceptions instead of swallowing them in eager mode —
        # otherwise a bug in a task is invisible during dev.
        task_eager_propagates=not settings.USE_CELERY,
        # Serialisation.  JSON keeps the broker debuggable from the CLI.
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Don't waste storage on results we never read back — periodic
        # tasks succeed/fail logged separately.  Override per-task with
        # ``ignore_result=False`` when needed.
        task_ignore_result=True,
        # Keep retries bounded.  Each worker sets its own retry policy;
        # this is just the global ceiling.
        task_default_retry_delay=60,
        task_max_retries=3,
        # Beat schedule — only meaningful when ``USE_CELERY=true`` and
        # ``celery beat`` is running, but configured unconditionally so
        # ``celery_app.conf.beat_schedule`` is always inspectable.
        beat_schedule=_beat_schedule(settings.TRIAL_SYNC_INTERVAL_HOURS),
    )

    return app


def _beat_schedule(trial_sync_interval_hours: int) -> dict[str, Any]:
    """Build the periodic-task schedule.

    Entries:

    * ``ctgov_sync``                  — every ``TRIAL_SYNC_INTERVAL_HOURS``
      hours, pull fresh trials from ClinicalTrials.gov.
    * ``daily_feedback_rollup``       — once a day at 02:00 UTC,
      collapse new feedback rows into the per-criterion / per-trial
      stats the API exposes.
    """
    return {
        "ctgov_sync": {
            "task": "trial_sync.run_periodic_sync",
            "schedule": timedelta(hours=max(1, trial_sync_interval_hours)),
            "options": {"expires": 60 * 60 * 2},  # don't backlog if missed
        },
        "daily_feedback_rollup": {
            "task": "feedback.run_daily_rollup",
            "schedule": crontab(hour=2, minute=0),
        },
    }


# ---------------------------------------------------------------------------
# Module-level singleton + rebuilder for tests
# ---------------------------------------------------------------------------

celery_app: Celery = _build_app()


def reset_celery_app() -> Celery:
    """Rebuild :data:`celery_app` with the *current* settings.

    Used by tests after monkey-patching settings (e.g. flipping
    ``USE_CELERY``) so subsequent ``@celery_app.task`` decorations bind
    to the rebuilt instance.
    """
    global celery_app  # noqa: PLW0603
    celery_app = _build_app()
    return celery_app
