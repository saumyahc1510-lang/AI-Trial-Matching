"""Matching endpoints — ``/api/v1/matching``.

Routes
------
``POST  /trigger``                       — kick off a match run for a
    patient (against the full catalog or a supplied trial subset).
    Dispatched via the rematch worker — synchronous in dev, async in
    production.
``GET   /patients/{patient_id}``         — latest match results for one
    patient, ranked.
``GET   /results/{match_result_id}``     — one match row, eager-loaded.
``GET   /results/{match_result_id}/explain`` —
    full :class:`ExplainabilityReport` (JSON or Markdown via ?format=).
``POST  /results/{match_result_id}/review`` —
    coordinator accept / reject / defer flow.

The patient-driven intake (``/intake/*``) lives in ``api/intake.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    ensure_can_access_patient,
    get_current_user,
    require_role,
)
from app.database import get_db
from app.models.audit import AuditAction
from app.models.matching import (
    CoordinatorStatusEnum,
    MatchResult,
    MatchTriggerEnum,
    OverallMatchStatusEnum,
)
from app.models.patient import Patient
from app.models.trial import ClinicalTrial
from app.models.user import User, UserRole
from app.schemas.matching import (
    CoordinatorReviewUpdate,
    CriterionEvaluationRead,
    MatchResultDetailRead,
    MatchResultRead,
    MatchTriggerRequest,
    MatchTriggerResponse,
    PatientMatchesResponse,
    UncertaintyFlagRead,
)
from app.services.explainability import build_report
from app.workers.rematch_worker import rematch_for_patient_task

router = APIRouter(prefix="/matching", tags=["Matching"])


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

@router.post(
    "/trigger",
    response_model=MatchTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Kick off a match run for a patient.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def trigger_match(
    payload: MatchTriggerRequest,
    request_state_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MatchTriggerResponse:
    """Enqueue a match run.

    In eager mode (``USE_CELERY=false``) this returns *after* the run
    has completed; the 202 is still appropriate because conceptually
    the caller asked us to schedule the work.  In async mode the task
    runs in a worker and the caller polls the latest matches endpoint.
    """
    patient = db.get(Patient, payload.patient_id)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Patient {payload.patient_id} not found.",
        )

    # ``trial_ids`` is currently advisory — the rematch worker runs
    # against the whole recruiting catalog.  We surface a 501 if the
    # caller restricts to a subset so they don't think it worked.
    if payload.trial_ids:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Trial-subset matching not yet supported via /trigger; omit "
                "trial_ids to match against the full recruiting catalog."
            ),
        )

    started_at = datetime.now(timezone.utc)
    result = rematch_for_patient_task.delay(
        str(payload.patient_id),
        triggered_by=payload.triggered_by.value,
    )
    payload_out = result.get(timeout=600) if result.ready() or True else {}
    trials_queued = int(payload_out.get("trials_matched", 0))
    return MatchTriggerResponse(
        patient_id=payload.patient_id,
        trials_queued=trials_queued,
        triggered_by=payload.triggered_by,
        started_at=started_at,
    )


# ---------------------------------------------------------------------------
# Per-patient ranking
# ---------------------------------------------------------------------------

@router.get(
    "/patients/{patient_id}",
    response_model=PatientMatchesResponse,
    summary="Latest match results for one patient (ranked).",
)
def patient_matches(
    patient_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    overall_status: Optional[OverallMatchStatusEnum] = Query(
        None, description="Filter by aggregate verdict."
    ),
    limit: int = Query(50, ge=1, le=200),
) -> PatientMatchesResponse:
    ensure_can_access_patient(current_user, patient_id)
    stmt = (
        select(MatchResult)
        .where(MatchResult.patient_id == patient_id, MatchResult.is_latest.is_(True))
        .order_by(MatchResult.final_rank_score.desc().nullslast())
        .limit(limit)
    )
    if overall_status is not None:
        stmt = stmt.where(MatchResult.overall_status == overall_status.value)
    rows = db.execute(stmt).scalars().all()
    return PatientMatchesResponse(
        patient_id=patient_id,
        total=len(rows),
        matches=[MatchResultRead.model_validate(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# Per-match detail + explainability
# ---------------------------------------------------------------------------

def _load_match_or_404(db: Session, match_result_id: uuid.UUID) -> MatchResult:
    match = db.execute(
        select(MatchResult)
        .where(MatchResult.id == match_result_id)
        .options(
            selectinload(MatchResult.criterion_evaluations),
            selectinload(MatchResult.uncertainty_flags),
            selectinload(MatchResult.trial),
        )
    ).scalar_one_or_none()
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MatchResult {match_result_id} not found.",
        )
    return match


@router.get(
    "/results/{match_result_id}",
    response_model=MatchResultDetailRead,
    summary="One match result with embedded evaluations + uncertainty flags.",
)
def get_match(
    match_result_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MatchResultDetailRead:
    match = _load_match_or_404(db, match_result_id)
    ensure_can_access_patient(current_user, match.patient_id)
    detail = MatchResultDetailRead.model_validate(match)
    detail.criterion_evaluations = [
        CriterionEvaluationRead.model_validate(ev)
        for ev in match.criterion_evaluations
    ]
    detail.uncertainty_flags = [
        UncertaintyFlagRead.model_validate(f) for f in match.uncertainty_flags
    ]
    return detail


@router.get(
    "/results/{match_result_id}/explain",
    summary="Per-criterion explainability — JSON by default, Markdown via ?format=md.",
)
def explain_match(
    match_result_id: uuid.UUID,
    format: str = Query("json", pattern="^(json|md|markdown)$"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    match = _load_match_or_404(db, match_result_id)
    ensure_can_access_patient(current_user, match.patient_id)
    report = build_report(db, match.id)
    if format in {"md", "markdown"}:
        return PlainTextResponse(content=report.to_markdown(), media_type="text/markdown")
    return report.to_dict()


# ---------------------------------------------------------------------------
# Coordinator review
# ---------------------------------------------------------------------------

@router.post(
    "/results/{match_result_id}/review",
    response_model=MatchResultRead,
    summary="Coordinator accept / reject / defer.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def coordinator_review(
    match_result_id: uuid.UUID,
    payload: CoordinatorReviewUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MatchResultRead:
    match = _load_match_or_404(db, match_result_id)
    match.coordinator_status = payload.coordinator_status.value
    if payload.coordinator_notes is not None:
        match.coordinator_notes = payload.coordinator_notes
    match.reviewed_by = current_user.id
    match.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(match)
    return MatchResultRead.model_validate(match)


# Patient-driven intake (``/intake/*``) lives in ``api/intake.py`` —
# different audience, different access model, separate router.
