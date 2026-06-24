"""Clinician feedback aggregation worker.

Every clinician accept / reject / override on a match feeds back into
two things this worker computes:

1. **Per-criterion acceptance rate.**  How often does each criterion
   pass / fail the human verification?  A criterion that's
   systematically overridden is a candidate for the LLM-prompt fine-
   tuning pipeline (Phase 5+ of the broader roadmap; for now we just
   expose the data via the stats API).

2. **Per-trial acceptance rate.**  Lets coordinators / sponsors see
   which trials the system is getting right and which it's noisy on.

The worker is invoked:

* On a daily schedule (``feedback.run_daily_rollup`` — registered in
  ``celery_app.beat_schedule``).
* On demand from the API (``feedback.run_now``) when a coordinator
  wants fresh numbers.

Both code paths reuse :func:`aggregate_feedback` so the API can also
return stats live without waiting for the schedule.

Marking-as-used semantics
-------------------------
After an aggregation run we set ``is_used_for_training = True`` on every
feedback row we counted.  That gives downstream fine-tuning jobs a
clean way to find rows that haven't been used yet without re-counting
all of history.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.database import _get_session_factory
from app.models.feedback import ClinicianFeedback, FeedbackAction
from app.models.matching import CriterionEvaluation, CriterionStatusEnum
from app.models.trial import ClinicalTrial, TrialCriterion
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FeedbackAggregate:
    """Per-trial rollup of feedback signals."""

    trial_id: str
    nct_id: Optional[str] = None
    accepted: int = 0
    rejected: int = 0
    overridden: int = 0
    deferred: int = 0

    @property
    def total(self) -> int:
        return self.accepted + self.rejected + self.overridden + self.deferred

    @property
    def acceptance_rate(self) -> float:
        return self.accepted / self.total if self.total else 0.0


@dataclass
class CriterionAggregate:
    """Per-criterion rollup of overrides — drives prompt tuning later."""

    criterion_id: str
    criterion_text: str
    category: str
    total_overrides: int = 0
    overridden_to_met: int = 0
    overridden_to_not_met: int = 0
    overridden_to_uncertain: int = 0

    @property
    def override_rate(self) -> Optional[float]:
        """Fraction of overrides that flipped the evaluation status.

        We don't have a "total times this criterion was evaluated" lookup
        here without another query — callers wanting that ratio should
        join against :class:`CriterionEvaluation` separately.
        """
        if self.total_overrides == 0:
            return None
        return 1.0


@dataclass
class FeedbackRollupStats:
    """Final stats returned by :func:`aggregate_feedback`."""

    rows_aggregated: int = 0
    per_trial: dict[str, FeedbackAggregate] = field(default_factory=dict)
    per_criterion: dict[str, CriterionAggregate] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Aggregation core
# ---------------------------------------------------------------------------

def _session_scope() -> Session:
    return _get_session_factory()()


def aggregate_feedback(
    db: Session,
    *,
    only_unused: bool = True,
    mark_as_used: bool = True,
) -> FeedbackRollupStats:
    """Roll up ``ClinicianFeedback`` rows into per-trial / per-criterion stats.

    Args:
        only_unused:    Limit aggregation to rows where
                        ``is_used_for_training`` is False.  Keeps the
                        daily rollup cheap by default.
        mark_as_used:   After aggregation, flip the counted rows'
                        ``is_used_for_training`` flag to True.  Set
                        ``False`` for the live API endpoint, which
                        should never mutate state.
    """
    rollup = FeedbackRollupStats()

    feedback_stmt = (
        select(ClinicianFeedback)
        .join(
            CriterionEvaluation,
            ClinicianFeedback.criterion_evaluation_id == CriterionEvaluation.id,
            isouter=True,
        )
    )
    if only_unused:
        feedback_stmt = feedback_stmt.where(
            ClinicianFeedback.is_used_for_training.is_(False)
        )

    rows = list(db.execute(feedback_stmt).scalars())
    if not rows:
        return rollup

    # Pre-load criterion + trial information so we don't N+1 inside the loop.
    evaluation_ids = {r.criterion_evaluation_id for r in rows if r.criterion_evaluation_id}
    evaluations: dict = {}
    if evaluation_ids:
        eval_rows = db.execute(
            select(CriterionEvaluation).where(CriterionEvaluation.id.in_(evaluation_ids))
        ).scalars()
        evaluations = {e.id: e for e in eval_rows}

    criterion_ids = {e.criterion_id for e in evaluations.values()}
    criteria: dict = {}
    if criterion_ids:
        crit_rows = db.execute(
            select(TrialCriterion).where(TrialCriterion.id.in_(criterion_ids))
        ).scalars()
        criteria = {c.id: c for c in crit_rows}

    trial_ids = {c.trial_id for c in criteria.values()}
    trial_ncts: dict = {}
    if trial_ids:
        nct_rows = db.execute(
            select(ClinicalTrial.id, ClinicalTrial.nct_id).where(
                ClinicalTrial.id.in_(trial_ids)
            )
        ).all()
        trial_ncts = {row[0]: row[1] for row in nct_rows}

    aggregated_ids: list = []

    for feedback in rows:
        aggregated_ids.append(feedback.id)
        rollup.rows_aggregated += 1
        action = (feedback.action or "").lower()

        # Per-trial rollup — only when we can resolve criterion → trial.
        evaluation = evaluations.get(feedback.criterion_evaluation_id)
        criterion = criteria.get(evaluation.criterion_id) if evaluation else None
        trial_id = str(criterion.trial_id) if criterion else None
        if trial_id is not None:
            agg = rollup.per_trial.setdefault(
                trial_id,
                FeedbackAggregate(
                    trial_id=trial_id,
                    nct_id=trial_ncts.get(criterion.trial_id),
                ),
            )
            if action == FeedbackAction.ACCEPTED.value:
                agg.accepted += 1
            elif action == FeedbackAction.REJECTED.value:
                agg.rejected += 1
            elif action == FeedbackAction.OVERRIDDEN.value:
                agg.overridden += 1
            elif action == FeedbackAction.DEFERRED.value:
                agg.deferred += 1

        # Per-criterion rollup for override actions only.
        if action == FeedbackAction.OVERRIDDEN.value and criterion is not None:
            crit_agg = rollup.per_criterion.setdefault(
                str(criterion.id),
                CriterionAggregate(
                    criterion_id=str(criterion.id),
                    criterion_text=criterion.original_text or "",
                    category=criterion.category or "other",
                ),
            )
            crit_agg.total_overrides += 1
            override_to = (feedback.override_status or "").lower()
            if override_to == CriterionStatusEnum.MET.value:
                crit_agg.overridden_to_met += 1
            elif override_to == CriterionStatusEnum.NOT_MET.value:
                crit_agg.overridden_to_not_met += 1
            elif override_to == CriterionStatusEnum.UNCERTAIN.value:
                crit_agg.overridden_to_uncertain += 1

    if mark_as_used and aggregated_ids:
        db.execute(
            update(ClinicianFeedback)
            .where(ClinicianFeedback.id.in_(aggregated_ids))
            .values(is_used_for_training=True)
        )
        db.commit()

    return rollup


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@celery_app.task(name="feedback.run_daily_rollup", ignore_result=True)
def run_daily_rollup_task() -> dict:
    """Daily aggregation of new feedback rows.

    Fires from celery beat at 02:00 UTC (see
    :func:`app.workers.celery_app._beat_schedule`).
    """
    db = _session_scope()
    try:
        stats = aggregate_feedback(db, only_unused=True, mark_as_used=True)
        return _rollup_to_dict(stats)
    finally:
        db.close()


@celery_app.task(name="feedback.run_now", ignore_result=False)
def run_now_task(only_unused: bool = False) -> dict:
    """On-demand aggregation, never mutates state.

    Backs the ``/feedback/stats`` API endpoint (Phase 7).  The default
    ``only_unused=False`` covers the whole feedback table — useful for
    dashboards.
    """
    db = _session_scope()
    try:
        stats = aggregate_feedback(db, only_unused=only_unused, mark_as_used=False)
        return _rollup_to_dict(stats)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Convenience read helpers (used by the API layer)
# ---------------------------------------------------------------------------

def overall_acceptance_rate(db: Session) -> float:
    """Single-number metric: accepted / total across all feedback rows."""
    counts = db.execute(
        select(ClinicianFeedback.action, func.count(ClinicianFeedback.id))
        .group_by(ClinicianFeedback.action)
    ).all()
    totals = {action.lower(): count for action, count in counts}
    total = sum(totals.values())
    if total == 0:
        return 0.0
    return totals.get(FeedbackAction.ACCEPTED.value, 0) / total


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _rollup_to_dict(stats: FeedbackRollupStats) -> dict:
    """Render :class:`FeedbackRollupStats` for the JSON task result."""
    return {
        "rows_aggregated": stats.rows_aggregated,
        "per_trial": {
            trial_id: {
                "nct_id": agg.nct_id,
                "accepted": agg.accepted,
                "rejected": agg.rejected,
                "overridden": agg.overridden,
                "deferred": agg.deferred,
                "total": agg.total,
                "acceptance_rate": round(agg.acceptance_rate, 3),
            }
            for trial_id, agg in stats.per_trial.items()
        },
        "per_criterion": {
            criterion_id: {
                "criterion_text": agg.criterion_text[:160],
                "category": agg.category,
                "total_overrides": agg.total_overrides,
                "overridden_to_met": agg.overridden_to_met,
                "overridden_to_not_met": agg.overridden_to_not_met,
                "overridden_to_uncertain": agg.overridden_to_uncertain,
            }
            for criterion_id, agg in stats.per_criterion.items()
        },
    }
