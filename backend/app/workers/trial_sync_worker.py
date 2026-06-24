"""Periodic ClinicalTrials.gov sync worker.

Wraps :mod:`app.services.trial_sync` and the LLM-driven
:mod:`app.services.criteria_parser` in Celery tasks so they can run on
a schedule, on-demand from the API, or inline during dev.

Tasks
-----
* ``trial_sync.run_periodic_sync``
    The scheduled task fired by celery-beat every
    ``TRIAL_SYNC_INTERVAL_HOURS``.  Pulls fresh trials, then
    auto-queues criteria parsing + notifications for trials whose
    status flipped.

* ``trial_sync.run_manual_sync``
    Same body but parameterised — invoked by the
    ``POST /admin/trial-sync`` endpoint (Phase 7).

* ``trial_sync.parse_criteria_for_trial``
    Idempotent helper: takes one ``trial_id`` and runs the criteria
    parser against it.  Split out so newly-synced trials can be parsed
    in parallel by N workers in production.

* ``trial_sync.detect_and_notify_status_changes``
    After a sync, scans the freshly-synced trials for status
    transitions (``RECRUITING`` ↔ ``COMPLETED``/``WITHDRAWN``) and fans
    out :func:`notify_trial_status_change` to coordinators.

Re-match queueing
-----------------
After a sync brings in new/updated trials, the worker enqueues
``rematch.rematch_for_new_trial`` per affected trial (Phase 6's
re-match worker).  That keeps the trial-sync task fast and lets re-
matching parallelise across workers in production.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import _get_session_factory
from app.models.trial import ClinicalTrial
from app.services.criteria_parser import parse_trial_criteria
from app.services.notification_service import notify_trial_status_change
from app.services.trial_sync import SyncStats, sync_trials
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_scope() -> Session:
    """Open a Session from the lazy factory.

    Workers should own their own session — they're invoked outside the
    request lifecycle, so the per-request ``get_db`` dependency isn't
    available.  Callers are responsible for closing.
    """
    factory = _get_session_factory()
    return factory()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@celery_app.task(name="trial_sync.run_periodic_sync", ignore_result=True)
def run_periodic_sync_task() -> dict:
    """Periodic CT.gov sync — fired by celery beat.

    Returns a JSON-friendly summary so the eager mode test path can
    introspect the result inline.
    """
    return run_manual_sync_task()


@celery_app.task(name="trial_sync.run_manual_sync", ignore_result=False)
def run_manual_sync_task(
    conditions: Optional[list[str]] = None,
    max_trials_per_condition: Optional[int] = None,
    parse_criteria: bool = True,
    notify_status_changes: bool = True,
    rematch_active_patients: bool = True,
    fetch_all: bool = False,
) -> dict:
    """Manually-triggered sync, with knobs for the admin API.

    The work happens in three phases — each phase commits its own
    transaction so a failure in (say) the criteria-parsing phase
    doesn't roll back the trial fetch.

    Set ``fetch_all=True`` to drop the condition filter and pull every
    recruiting trial.  In that mode ``conditions`` is ignored and
    ``max_trials_per_condition`` acts as a hard catalog-wide cap (useful
    for keeping the demo responsive — the real CT.gov catalog has
    ~50,000 recruiting trials).
    """
    db = _session_scope()
    summary: dict = {}
    try:
        # ── 1. Pull trials from CT.gov ────────────────────────────────
        stats = sync_trials(
            db,
            conditions=conditions,
            max_trials_per_condition=max_trials_per_condition,
            fetch_all=fetch_all,
        )
        summary["sync"] = _stats_to_dict(stats)
        logger.info(
            "CT.gov sync: fetched=%d created=%d updated=%d status_changed=%d "
            "errors=%d",
            stats.trials_fetched, stats.trials_created, stats.trials_updated,
            stats.trials_status_changed, len(stats.errors),
        )

        # ── 2. Parse criteria for any trial that lacks them ───────────
        if parse_criteria:
            parsed = _parse_pending_criteria(db)
            summary["criteria_parsed"] = parsed
            logger.info("Criteria parsed for %d trial(s)", parsed["trials_parsed"])

        # ── 3. Status-change notifications + re-match queueing ────────
        if notify_status_changes:
            notif = _detect_and_notify_status_changes(db)
            summary["status_notifications"] = notif

        if rematch_active_patients and (stats.trials_created or stats.trials_updated):
            # Lazy import to break a circular dependency
            # (rematch_worker imports tasks that import this module).
            from app.workers import rematch_worker  # noqa: WPS433

            for trial_id in _recently_synced_trial_ids(db, limit=stats.trials_fetched):
                rematch_worker.rematch_for_new_trial_task.delay(str(trial_id))
            summary["rematches_queued"] = stats.trials_fetched
    finally:
        db.close()
    return summary


@celery_app.task(name="trial_sync.parse_criteria_for_trial", ignore_result=True)
def parse_criteria_for_trial_task(
    trial_id: str,
    *,
    max_bullets: Optional[int] = None,
) -> dict:
    """Parse one trial's eligibility text into structured criteria rows.

    Safe to call repeatedly — :func:`parse_trial_criteria` replaces
    existing criteria by default.
    """
    db = _session_scope()
    try:
        trial = db.get(ClinicalTrial, trial_id)
        if trial is None:
            return {"trial_id": trial_id, "error": "trial not found"}
        if not trial.raw_eligibility_text:
            return {"trial_id": trial_id, "error": "no raw_eligibility_text"}
        stats = parse_trial_criteria(db, trial, max_bullets=max_bullets)
        return {
            "trial_id": trial_id,
            "nct_id": trial.nct_id,
            "criteria_parsed": stats.criteria_parsed,
            "criteria_failed": stats.criteria_failed,
            "inclusion": stats.inclusion_count,
            "exclusion": stats.exclusion_count,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _stats_to_dict(stats: SyncStats) -> dict:
    """Render :class:`SyncStats` for the JSON task result."""
    return {
        "trials_fetched": stats.trials_fetched,
        "trials_created": stats.trials_created,
        "trials_updated": stats.trials_updated,
        "trials_status_changed": stats.trials_status_changed,
        "sites_created": stats.sites_created,
        "duration_seconds": stats.duration_seconds,
        "errors": stats.errors,
    }


def _parse_pending_criteria(db: Session) -> dict:
    """Run the LLM criteria parser against trials with no criteria rows yet.

    Criteria parsing is the LLM-expensive part of a sync, so it's bounded
    two ways (see :class:`~app.config.Settings`):

    * ``TRIAL_PARSE_CATEGORIES`` — only parse trials in these categories
      (empty = all).  Keeps the budget on the specialties that matter.
    * ``TRIAL_PARSE_MAX_PER_SYNC`` — a hard per-run cap so one sync can't
      exhaust the daily token quota.  Run sync again to parse the next
      batch.
    """
    from app.services import runtime_config

    allowed = runtime_config.parse_categories()
    max_per = runtime_config.parse_max_per_sync()

    stmt = select(ClinicalTrial.id).where(
        ClinicalTrial.raw_eligibility_text.isnot(None),
        ~ClinicalTrial.criteria.any(),
    )
    if allowed:
        stmt = stmt.where(ClinicalTrial.category.in_(allowed))
    if max_per and max_per > 0:
        stmt = stmt.limit(max_per)

    pending_ids = db.execute(stmt).scalars().all()

    trials_parsed = 0
    criteria_total = 0
    failures = 0
    for trial_id in pending_ids:
        trial = db.get(ClinicalTrial, trial_id)
        if trial is None or not trial.raw_eligibility_text:
            continue
        try:
            stats = parse_trial_criteria(db, trial)
            trials_parsed += 1
            criteria_total += stats.criteria_parsed
            failures += stats.criteria_failed
        except Exception as exc:  # noqa: BLE001 - one trial mustn't break others
            logger.exception("Parsing criteria failed for %s", trial.nct_id)
            failures += 1

    logger.info(
        "Criteria parsing: %d trial(s) parsed (categories=%s, cap=%s)",
        trials_parsed, allowed or "ALL", max_per or "none",
    )
    return {
        "trials_parsed": trials_parsed,
        "criteria_added": criteria_total,
        "failures": failures,
        "parse_categories": allowed or "all",
        "parse_cap": max_per,
    }


def _detect_and_notify_status_changes(db: Session) -> dict:
    """Walk recently-synced trials and notify on status transitions.

    "Recent" is defined as "synced in the last 5 minutes" — enough
    headroom for a periodic run to cover its own work without scanning
    the whole table.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    candidates = (
        db.execute(
            select(ClinicalTrial).where(
                ClinicalTrial.last_synced_at.is_not(None),
                ClinicalTrial.last_synced_at >= cutoff,
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for trial in candidates:
        # We don't track historic status transitions across runs yet —
        # for now we only notify when a trial flips to / from
        # RECRUITING based on the *current* row state.  A future
        # enhancement would diff against the previous snapshot stored
        # in audit_logs.  This keeps Phase 6 self-contained.
        status_upper = (trial.overall_status or "").upper()
        if status_upper not in {"RECRUITING", "COMPLETED", "WITHDRAWN", "TERMINATED"}:
            continue
        try:
            notify_trial_status_change(
                db,
                trial_id=trial.id,
                nct_id=trial.nct_id,
                previous_status="(unknown)",
                new_status=status_upper,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Status-change notification failed for %s: %s",
                trial.nct_id,
                exc,
            )
    return {"notifications_sent": sent}


def _recently_synced_trial_ids(db: Session, *, limit: int) -> list:
    """Return the most-recently-synced trial IDs (UUIDs)."""
    return list(
        db.execute(
            select(ClinicalTrial.id)
            .where(ClinicalTrial.last_synced_at.is_not(None))
            .order_by(ClinicalTrial.last_synced_at.desc())
            .limit(max(1, limit))
        )
        .scalars()
    )
