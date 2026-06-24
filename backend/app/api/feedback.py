"""Clinician feedback endpoints — ``/api/v1/feedback``.

Routes
------
``POST  /``           — submit feedback on a match (or one criterion).
``GET   /stats``      — aggregate stats (per-trial / per-criterion).
``GET   /trials/{trial_id}`` — rolled-up feedback for one trial.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_role
from app.database import get_db
from app.models.feedback import ClinicianFeedback
from app.models.matching import CriterionEvaluation, MatchResult
from app.models.trial import TrialCriterion
from app.models.user import User, UserRole
from app.schemas.feedback import (
    ClinicianFeedbackCreate,
    ClinicianFeedbackRead,
    FeedbackStatsResponse,
)
from app.workers.feedback_worker import (
    aggregate_feedback,
    overall_acceptance_rate,
)

router = APIRouter(prefix="/feedback", tags=["Feedback"])


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

@router.post(
    "/",
    response_model=ClinicianFeedbackRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit clinician feedback on a match result.",
    dependencies=[
        Depends(require_role(UserRole.CLINICIAN, UserRole.COORDINATOR, UserRole.ADMIN))
    ],
)
def submit_feedback(
    payload: ClinicianFeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClinicianFeedbackRead:
    """Persist feedback + (optionally) flip the evaluation status.

    When the action is ``OVERRIDDEN`` the feedback row carries the new
    status; the matching engine's :func:`recompute_match_counters` is
    *not* called here — that's the feedback worker's job during its
    daily rollup so the API stays fast.
    """
    match = db.get(MatchResult, payload.match_result_id)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MatchResult {payload.match_result_id} not found.",
        )
    if payload.criterion_evaluation_id is not None:
        ev = db.get(CriterionEvaluation, payload.criterion_evaluation_id)
        if ev is None or ev.match_result_id != match.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="CriterionEvaluation not found or not part of this match.",
            )

    row = ClinicianFeedback(
        match_result_id=payload.match_result_id,
        criterion_evaluation_id=payload.criterion_evaluation_id,
        user_id=current_user.id,
        action=payload.action.value,
        override_status=(
            payload.override_status.value if payload.override_status else None
        ),
        reason=payload.reason,
        is_used_for_training=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ClinicianFeedbackRead.model_validate(row)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get(
    "/stats",
    response_model=FeedbackStatsResponse,
    summary="System-wide feedback rollup (read-only).",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN, UserRole.SPONSOR))
    ],
)
def feedback_stats(
    db: Session = Depends(get_db),
) -> FeedbackStatsResponse:
    rollup = aggregate_feedback(db, only_unused=False, mark_as_used=False)
    totals = {"accepted": 0, "rejected": 0, "overridden": 0, "deferred": 0}
    for agg in rollup.per_trial.values():
        totals["accepted"] += agg.accepted
        totals["rejected"] += agg.rejected
        totals["overridden"] += agg.overridden
        totals["deferred"] += agg.deferred

    return FeedbackStatsResponse(
        total_feedbacks=sum(totals.values()),
        accepted=totals["accepted"],
        rejected=totals["rejected"],
        overridden=totals["overridden"],
        deferred=totals["deferred"],
        acceptance_rate=overall_acceptance_rate(db),
        per_trial={
            trial_id: {
                "accepted": agg.accepted,
                "rejected": agg.rejected,
                "overridden": agg.overridden,
                "deferred": agg.deferred,
            }
            for trial_id, agg in rollup.per_trial.items()
        },
    )
