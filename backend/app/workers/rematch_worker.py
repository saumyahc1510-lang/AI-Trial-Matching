"""Longitudinal re-matching worker.

The trial-match world is *not* static: new chart data lands, new trials
open, statuses flip.  This worker keeps every patient's ranked match
list current.

Trigger points
--------------
The API and other workers dispatch one of these tasks:

* ``rematch.rematch_for_patient`` — full re-match for *one patient*
  against every recruiting trial in the catalog.  Fires when the
  patient's chart is updated (new EHR ingestion).
* ``rematch.rematch_for_new_trial`` — match *one trial* against every
  active patient.  Fires from the trial-sync worker after a fresh sync.
* ``rematch.rematch_for_trial_status_change`` — refresh existing matches
  against a trial whose status flipped.

Each task:

1. Loads what it needs in one batched query.
2. Delegates the actual reasoning to
   :mod:`app.services.matching_engine` (no duplication of scoring
   logic).
3. Runs the diversity ranker pass so ``final_rank_score`` is fresh.
4. Fires notifications for genuinely new matches and resolutions —
   we explicitly avoid spamming coordinators when a match stays in the
   same overall_status across runs.

Idempotency
-----------
The matching engine already replaces the previous "latest" run for the
same ``(patient, trial)`` pair and writes a new immutable row.  Re-
running these tasks is therefore safe and produces a clean audit trail
of how each match evolved.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.database import _get_session_factory
from app.models.matching import MatchResult, MatchTriggerEnum, OverallMatchStatusEnum
from app.models.patient import Patient, PatientStatusEnum
from app.models.trial import ClinicalTrial
from app.services.diversity_ranker import rerank_matches_for_patient
from app.services.matching_engine import (
    MatchRunStats,
    match_patient_against_trial,
    match_patient_against_trials,
    recompute_match_counters,
)
from app.services.notification_service import (
    notify_new_match,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session helper (mirrors trial_sync_worker)
# ---------------------------------------------------------------------------

def _session_scope() -> Session:
    """Workers manage their own DB sessions."""
    return _get_session_factory()()


# ---------------------------------------------------------------------------
# Active-state queries
# ---------------------------------------------------------------------------

def _active_patients(db: Session) -> list[Patient]:
    """Patients whose status is ACTIVE — the universe for new-trial matches."""
    stmt = (
        select(Patient)
        .where(Patient.status == PatientStatusEnum.ACTIVE.value)
        .options(selectinload(Patient.medical_events))
    )
    return list(db.execute(stmt).scalars())


def _recruiting_trials(db: Session) -> list[ClinicalTrial]:
    """Trials currently accepting patients (RECRUITING or NOT_YET_RECRUITING).

    Pre-loads ``criteria`` since the matching engine reads them in its
    main loop — saves N+1 queries on a big batch.
    """
    stmt = (
        select(ClinicalTrial)
        .where(
            ClinicalTrial.overall_status.in_(
                ["RECRUITING", "NOT_YET_RECRUITING"]
            )
        )
        .options(selectinload(ClinicalTrial.criteria))
    )
    return list(db.execute(stmt).scalars())


def _previous_overall_status(
    db: Session, patient_id, trial_id
) -> Optional[str]:
    """Return the overall_status of the most-recent match for this pair.

    Called *before* the matching engine writes the new run, so the
    "current latest" row is, semantically, the *previous* status from
    the new run's perspective.  We deliberately do not filter on
    ``is_latest`` — the engine has not yet flipped it, and filtering
    ``is_latest=False`` here would always return ``None`` on the second
    rematch and erroneously refire notifications.
    """
    prev = db.execute(
        select(MatchResult)
        .where(
            MatchResult.patient_id == patient_id,
            MatchResult.trial_id == trial_id,
        )
        .order_by(MatchResult.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return prev.overall_status if prev else None


# ---------------------------------------------------------------------------
# Notification rules
# ---------------------------------------------------------------------------

def _should_notify_on_new_match(
    previous_status: Optional[str],
    new_status: str,
) -> bool:
    """Decide whether a fresh match is *new enough* to alert on.

    First-time matches always notify.  Subsequent runs only notify when
    the overall_status *changed* — coordinators don't want a ping every
    six hours saying the same patient is still uncertain on the same
    trial.
    """
    if previous_status is None:
        return True
    return previous_status.lower() != new_status.lower()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@celery_app.task(
    name="rematch.rematch_for_patient",
    ignore_result=False,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def rematch_for_patient_task(
    patient_id: str,
    *,
    triggered_by: str = MatchTriggerEnum.CHART_UPDATE.value,
    notify: bool = True,
) -> dict:
    """Re-match one patient against every recruiting trial.

    Use this when the patient's chart has changed and you want the full
    universe re-evaluated.
    """
    db = _session_scope()
    try:
        patient = db.get(
            Patient, patient_id,
            options=[selectinload(Patient.medical_events)],
        )
        if patient is None:
            return {"error": "patient not found", "patient_id": patient_id}

        trials = _recruiting_trials(db)
        previous_status_by_trial = {
            t.id: _previous_overall_status(db, patient.id, t.id) for t in trials
        }

        stats = match_patient_against_trials(
            db,
            patient,
            trials,
            triggered_by=MatchTriggerEnum(triggered_by),
        )

        # Refresh diversity scores in one pass (covers everything we
        # just wrote since the matching engine flips is_latest correctly).
        rerank_matches_for_patient(db, str(patient.id))

        if notify:
            _fan_out_new_match_notifications(
                db, stats, previous_status_by_trial
            )

        return _run_stats_to_dict(stats)
    finally:
        db.close()


@celery_app.task(
    name="rematch.rematch_for_new_trial",
    ignore_result=False,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def rematch_for_new_trial_task(
    trial_id: str,
    *,
    notify: bool = True,
    triggered_by: str = MatchTriggerEnum.NEW_TRIAL.value,
) -> dict:
    """Match one trial against every active patient.

    Fired by the trial-sync worker after a fresh sync brings in
    new / updated trials.  Patients with no medical events at all are
    skipped — there's nothing to match against.
    """
    db = _session_scope()
    try:
        trial = db.get(
            ClinicalTrial, trial_id,
            options=[selectinload(ClinicalTrial.criteria)],
        )
        if trial is None:
            return {"error": "trial not found", "trial_id": trial_id}
        if not trial.criteria:
            return {
                "trial_id": trial_id,
                "nct_id": trial.nct_id,
                "skipped": "no parsed criteria",
            }

        patients = _active_patients(db)
        patients_matched = 0
        new_match_ids: list[str] = []

        for patient in patients:
            if not patient.medical_events:
                continue
            previous_status = _previous_overall_status(db, patient.id, trial.id)
            match = match_patient_against_trial(
                db, patient, trial,
                triggered_by=MatchTriggerEnum(triggered_by),
            )
            db.commit()
            patients_matched += 1
            new_match_ids.append(str(match.id))

            # Diversity rerank is per-patient — call after we save.
            rerank_matches_for_patient(db, str(patient.id))

            if notify and _should_notify_on_new_match(previous_status, match.overall_status):
                notify_new_match(
                    db, match,
                    trial_title=trial.title,
                    nct_id=trial.nct_id,
                )

        return {
            "trial_id": trial_id,
            "nct_id": trial.nct_id,
            "patients_matched": patients_matched,
            "match_result_ids": new_match_ids,
        }
    finally:
        db.close()


@celery_app.task(
    name="rematch.rematch_for_trial_status_change",
    ignore_result=False,
)
def rematch_for_trial_status_change_task(trial_id: str) -> dict:
    """Refresh existing matches against a trial whose status flipped.

    We re-run only against patients who already have a latest match for
    this trial — there's no value in scoring patients who never matched
    in the first place against a trial that just closed.
    """
    db = _session_scope()
    try:
        trial = db.get(
            ClinicalTrial, trial_id,
            options=[selectinload(ClinicalTrial.criteria)],
        )
        if trial is None:
            return {"error": "trial not found", "trial_id": trial_id}

        affected_patient_ids = list(
            db.execute(
                select(MatchResult.patient_id)
                .where(
                    MatchResult.trial_id == trial.id,
                    MatchResult.is_latest.is_(True),
                )
                .distinct()
            ).scalars()
        )

        rematched = 0
        for pid in affected_patient_ids:
            patient = db.get(
                Patient, pid,
                options=[selectinload(Patient.medical_events)],
            )
            if patient is None or not patient.medical_events:
                continue
            match_patient_against_trial(
                db, patient, trial,
                triggered_by=MatchTriggerEnum.TRIAL_STATUS_CHANGE,
            )
            db.commit()
            rerank_matches_for_patient(db, str(patient.id))
            rematched += 1

        return {
            "trial_id": trial_id,
            "nct_id": trial.nct_id,
            "patients_rematched": rematched,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_stats_to_dict(stats: MatchRunStats) -> dict:
    return {
        "patient_id": stats.patient_id,
        "trials_considered": stats.trials_considered,
        "trials_pre_filtered": stats.trials_pre_filtered,
        "trials_matched": stats.trials_matched,
        "duration_seconds": stats.duration_seconds,
        "match_result_ids": stats.match_result_ids,
        "errors": stats.errors,
    }


def _fan_out_new_match_notifications(
    db: Session,
    stats: MatchRunStats,
    previous_status_by_trial: dict,
) -> None:
    """Fire one notification per match whose status changed."""
    if not stats.match_result_ids:
        return
    matches = (
        db.execute(
            select(MatchResult)
            .where(MatchResult.id.in_(stats.match_result_ids))
            .options(selectinload(MatchResult.trial))
        )
        .scalars()
    )
    for match in matches:
        previous = previous_status_by_trial.get(match.trial_id)
        if not _should_notify_on_new_match(previous, match.overall_status):
            continue
        try:
            notify_new_match(
                db, match,
                trial_title=match.trial.title,
                nct_id=match.trial.nct_id,
            )
        except Exception as exc:  # noqa: BLE001 - notifications never block
            logger.warning("notify_new_match failed: %s", exc)
