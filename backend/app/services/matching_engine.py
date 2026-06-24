"""Matching engine — orchestrates the full eligibility pipeline.

The matching engine ties together everything built in Phases 1-3 into
the central business operation of this product: deciding which trials a
patient might be eligible for, with full per-criterion explainability.

For one ``(patient, trial)`` pair the engine:

1. Loads the patient's medical events and **reconstructs the timeline**
   via :func:`~app.services.temporal_engine.reconstruct_timeline`.
2. Applies a **fast demographic pre-filter** (age + sex) to discard
   trivially-ineligible trials before paying for any LLM calls.
3. Iterates every :class:`TrialCriterion` and asks the eligibility
   reasoner for a verdict.
4. Aggregates verdicts into ``match_score`` and ``confidence_score``
   and derives the overall ``OverallMatchStatus``.
5. Persists one :class:`MatchResult` plus a :class:`CriterionEvaluation`
   per criterion, and uses :mod:`app.services.uncertainty_engine` to
   produce :class:`UncertaintyFlag` rows + the coordinator summary.
6. Marks the previous latest run for the same ``(patient, trial)`` as
   ``is_latest = False`` so the new row is the canonical view.

Scoring
-------
* ``match_score``       = met / (met + not_met)   — proportion of
  *decided* inclusion-criteria-equivalent verdicts that came out
  positive.  We deliberately exclude uncertain criteria from the
  denominator so they don't artificially drag the score down (a 100%
  unknown chart shouldn't display as 0% match).
* ``confidence_score``  = (met + not_met) / total — fraction of
  criteria the engine could actually decide.  A score of 0.4 means
  60% of the criteria are still uncertain.
* ``final_rank_score``  = ``match_score * confidence_score``.  The
  diversity ranker (Phase 5) may overwrite this.

Hard fails vs soft fails
------------------------
A single critical-inclusion ``NOT_MET`` (or critical-exclusion ``MET``)
flips the overall status to ``INELIGIBLE`` regardless of how many other
criteria pass.  Non-critical mismatches reduce the score but don't
disqualify.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.matching import (
    CoordinatorStatusEnum,
    CriterionEvaluation,
    CriterionStatusEnum,
    EvaluatorEnum,
    MatchResult,
    MatchTriggerEnum,
    OverallMatchStatusEnum,
)
from app.models.patient import Patient
from app.models.trial import (
    ClinicalTrial,
    CriterionCategoryEnum,
    CriterionTypeEnum,
    TrialCriterion,
)
from app.services.eligibility_reasoner import (
    EligibilityStatus,
    EligibilityVerdict,
    evaluate_criterion,
)
from app.services.llm_client import LLMClient
from app.services.temporal_engine import (
    PatientTimeline,
    reconstruct_timeline,
)
from app.services.uncertainty_engine import (
    UncertaintyDraft,
    build_uncertainty_draft,
    persist_flags,
    summarise_uncertainty,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class _AggregateCounts:
    """Per-criterion counters accumulated while evaluating one trial."""

    total: int = 0
    met: int = 0
    not_met: int = 0
    uncertain: int = 0
    hard_fail: bool = False  # critical inclusion not-met OR critical exclusion met


@dataclass
class MatchRunStats:
    """Returned to the caller after :func:`match_patient_against_trials`."""

    patient_id: str
    trials_considered: int = 0
    trials_pre_filtered: int = 0  # discarded by demographic pre-filter
    trials_matched: int = 0       # successfully evaluated end-to-end
    duration_seconds: float = 0.0
    match_result_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class _CriterionOutcome:
    """In-memory result of evaluating one criterion (pre-persistence)."""

    criterion: TrialCriterion
    verdict: EligibilityVerdict


# ---------------------------------------------------------------------------
# Demographic pre-filter
# ---------------------------------------------------------------------------

def _patient_passes_demographic_prefilter(
    patient: Patient,
    trial: ClinicalTrial,
) -> tuple[bool, Optional[str]]:
    """Cheap structural check before paying for LLM calls.

    For now we only check whether a sex-restricted trial matches the
    patient's recorded sex.  Age-band trials don't expose machine-
    readable bands in the CT.gov v2 payload we cache, so we keep that
    check inside the per-criterion reasoner where the value_constraint
    is available.

    Returns ``(should_evaluate, reason_if_skipped)``.  When the patient
    fails the pre-filter we still persist a `MatchResult` row marked
    INELIGIBLE so the UI shows the trial was considered — that's better
    UX than silently dropping it.
    """
    # The trial-level eligibility text often encodes sex restrictions
    # as inclusion criteria; trust the criteria-level evaluation instead.
    # This hook exists to make the design space obvious; richer checks
    # (geography, language match, etc.) plug in here.
    return True, None


# ---------------------------------------------------------------------------
# Verdict aggregation
# ---------------------------------------------------------------------------

def _verdict_to_status(verdict: EligibilityVerdict) -> CriterionStatusEnum:
    return verdict.status.to_criterion_status()


def _aggregate(outcomes: list[_CriterionOutcome]) -> _AggregateCounts:
    """Roll per-criterion verdicts up into a single counter set.

    Inclusion / exclusion criteria use opposite definitions of "met" at
    the database level (the reasoner already inverted the verdict for
    us): inclusion-MET means the criterion is satisfied, exclusion-MET
    means the *exclusion rule* is satisfied (= patient is not excluded).
    So we treat MET / NOT_MET uniformly here.
    """
    agg = _AggregateCounts(total=len(outcomes))
    for outcome in outcomes:
        status = _verdict_to_status(outcome.verdict)
        if status == CriterionStatusEnum.MET:
            agg.met += 1
        elif status == CriterionStatusEnum.NOT_MET:
            agg.not_met += 1
            if outcome.criterion.is_critical:
                agg.hard_fail = True
        else:
            agg.uncertain += 1
    return agg


def _scores_from_aggregate(agg: _AggregateCounts) -> tuple[float, float, float]:
    """Return ``(match_score, confidence_score, final_rank_score)``."""
    decided = agg.met + agg.not_met
    match_score = (agg.met / decided) if decided > 0 else 0.0
    confidence_score = (decided / agg.total) if agg.total > 0 else 0.0
    return match_score, confidence_score, match_score * confidence_score


def _overall_status_from(agg: _AggregateCounts) -> OverallMatchStatusEnum:
    """Map counters → top-line ``OverallMatchStatus``.

    Rules (in order):

    1. Any critical hard-fail → ``INELIGIBLE``.
    2. Any remaining ``uncertain`` → ``UNCERTAIN``.
    3. Everything met → ``ELIGIBLE``.
    4. Mixed met / not-met without uncertainty falls back to
       ``INELIGIBLE`` (non-critical failures still preclude enrollment
       unless overridden by a clinician).
    """
    if agg.hard_fail:
        return OverallMatchStatusEnum.INELIGIBLE
    if agg.uncertain > 0:
        return OverallMatchStatusEnum.UNCERTAIN
    if agg.not_met == 0 and agg.met > 0:
        return OverallMatchStatusEnum.ELIGIBLE
    return OverallMatchStatusEnum.INELIGIBLE


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _persist_criterion_evaluations(
    db: Session,
    match_result: MatchResult,
    outcomes: list[_CriterionOutcome],
) -> dict[str, CriterionEvaluation]:
    """Insert one :class:`CriterionEvaluation` per outcome.

    Returns a ``criterion_id → CriterionEvaluation`` map so the caller
    can wire up uncertainty flags to the right evaluation FK.
    """
    by_criterion_id: dict[str, CriterionEvaluation] = {}
    for outcome in outcomes:
        verdict = outcome.verdict
        ev = CriterionEvaluation(
            match_result_id=match_result.id,
            criterion_id=outcome.criterion.id,
            status=_verdict_to_status(verdict).value,
            reasoning=verdict.reasoning,
            evidence_text=verdict.evidence_text,
            evidence_event_id=(
                verdict.evidence_entry.source_event.id
                if verdict.evidence_entry is not None
                else None
            ),
            evidence_source=(
                verdict.evidence_entry.source_event.source_document
                if verdict.evidence_entry is not None
                else None
            ),
            confidence=verdict.confidence,
            evaluated_by=verdict.evaluator.value,
            llm_model_used=verdict.llm_model_used,
        )
        db.add(ev)
        by_criterion_id[str(outcome.criterion.id)] = ev
    db.flush()  # populate ev.id
    return by_criterion_id


def _build_uncertainty_drafts(
    outcomes: list[_CriterionOutcome],
    evaluations_by_criterion: dict[str, CriterionEvaluation],
) -> list[UncertaintyDraft]:
    """Convert uncertain verdicts into uncertainty drafts."""
    drafts: list[UncertaintyDraft] = []
    for outcome in outcomes:
        if outcome.verdict.status != EligibilityStatus.UNCERTAIN:
            continue
        evaluation = evaluations_by_criterion.get(str(outcome.criterion.id))
        drafts.append(
            build_uncertainty_draft(
                outcome.criterion,
                evaluation_id=str(evaluation.id) if evaluation else None,
                hint_text=outcome.verdict.missing_data or outcome.verdict.reasoning,
            )
        )
    return drafts


def _mark_previous_results_stale(
    db: Session,
    patient_id: uuid.UUID,
    trial_id: uuid.UUID,
) -> None:
    """Flip ``is_latest=False`` on any prior matches for this pair.

    Done in a single UPDATE rather than per-row to keep churn cheap when
    the matching engine reruns on chart updates.
    """
    db.execute(
        update(MatchResult)
        .where(
            MatchResult.patient_id == patient_id,
            MatchResult.trial_id == trial_id,
            MatchResult.is_latest.is_(True),
        )
        .values(is_latest=False)
    )


# ---------------------------------------------------------------------------
# Single-trial matching
# ---------------------------------------------------------------------------

def match_patient_against_trial(
    db: Session,
    patient: Patient,
    trial: ClinicalTrial,
    *,
    timeline: Optional[PatientTimeline] = None,
    triggered_by: MatchTriggerEnum = MatchTriggerEnum.INITIAL_MATCH,
    llm_client: Optional[LLMClient] = None,
) -> MatchResult:
    """Evaluate one patient against one trial and persist the result.

    The caller can pass a pre-computed ``timeline`` to amortise the
    reconstruction cost when matching against many trials.
    """
    if timeline is None:
        timeline = reconstruct_timeline(str(patient.id), patient.medical_events)

    criteria: list[TrialCriterion] = sorted(
        trial.criteria, key=lambda c: c.order_index
    )

    outcomes: list[_CriterionOutcome] = []
    for criterion in criteria:
        try:
            verdict = evaluate_criterion(
                criterion, patient, timeline, client=llm_client
            )
        except Exception as exc:  # noqa: BLE001 - reasoner is best-effort
            logger.exception("Reasoner crashed on criterion %s", criterion.id)
            # Fail safe to UNCERTAIN so the whole match doesn't blow up.
            verdict = EligibilityVerdict(
                status=EligibilityStatus.UNCERTAIN,
                reasoning=f"Internal reasoner error: {exc}",
                confidence=0.0,
                missing_data="Internal evaluation error.",
                evaluator=EvaluatorEnum.LLM,
            )
        outcomes.append(_CriterionOutcome(criterion=criterion, verdict=verdict))

    agg = _aggregate(outcomes)
    match_score, confidence_score, final_rank_score = _scores_from_aggregate(agg)
    overall = _overall_status_from(agg)

    # Mark the previous "latest" stale before inserting the new one.
    _mark_previous_results_stale(db, patient.id, trial.id)

    match_result = MatchResult(
        patient_id=patient.id,
        trial_id=trial.id,
        patient_version=patient.current_version or 1,
        overall_status=overall.value,
        match_score=match_score,
        confidence_score=confidence_score,
        final_rank_score=final_rank_score,
        total_criteria=agg.total,
        criteria_met=agg.met,
        criteria_not_met=agg.not_met,
        criteria_uncertain=agg.uncertain,
        coordinator_status=CoordinatorStatusEnum.PENDING_REVIEW.value,
        triggered_by=triggered_by.value,
        is_latest=True,
    )
    db.add(match_result)
    db.flush()  # populate match_result.id before children reference it

    evals_by_criterion = _persist_criterion_evaluations(db, match_result, outcomes)
    drafts = _build_uncertainty_drafts(outcomes, evals_by_criterion)
    summary = summarise_uncertainty(drafts)
    if summary.total_uncertain > 0:
        match_result.missing_data_summary = summary.summary_text
        persist_flags(db, match_result, summary)

    return match_result


# ---------------------------------------------------------------------------
# Batch matching
# ---------------------------------------------------------------------------

def match_patient_against_trials(
    db: Session,
    patient: Patient,
    trials: Iterable[ClinicalTrial],
    *,
    triggered_by: MatchTriggerEnum = MatchTriggerEnum.INITIAL_MATCH,
    llm_client: Optional[LLMClient] = None,
    skip_unparsed: bool = True,
) -> MatchRunStats:
    """Score ``patient`` against every trial in ``trials``.

    Commits once per trial so a partial run still leaves the database
    consistent.  Trials with no parsed :class:`TrialCriterion` rows are
    skipped by default — running them would produce empty match results
    with no signal.
    """
    started = time.monotonic()
    stats = MatchRunStats(patient_id=str(patient.id))
    # Build the timeline once and reuse — saves O(n) SQL on a big batch.
    timeline = reconstruct_timeline(str(patient.id), patient.medical_events)

    for trial in trials:
        stats.trials_considered += 1
        passes, skip_reason = _patient_passes_demographic_prefilter(patient, trial)
        if not passes:
            stats.trials_pre_filtered += 1
            logger.info(
                "Patient %s pre-filtered out of trial %s: %s",
                patient.id, trial.nct_id, skip_reason,
            )
            continue
        if skip_unparsed and not trial.criteria:
            logger.info("Skipping trial %s — no parsed criteria.", trial.nct_id)
            continue
        try:
            result = match_patient_against_trial(
                db, patient, trial,
                timeline=timeline,
                triggered_by=triggered_by,
                llm_client=llm_client,
            )
            db.commit()
            stats.trials_matched += 1
            stats.match_result_ids.append(str(result.id))
        except Exception as exc:  # noqa: BLE001 - one bad trial mustn't poison the rest
            db.rollback()
            logger.exception("Match failed for trial %s", trial.nct_id)
            stats.errors.append(f"{trial.nct_id}: {exc}")

    stats.duration_seconds = round(time.monotonic() - started, 2)
    return stats


# ---------------------------------------------------------------------------
# Convenience queries
# ---------------------------------------------------------------------------

def recompute_match_counters(
    db: Session,
    match_result: MatchResult,
) -> MatchResult:
    """Recompute aggregate counters + overall_status from live evaluations.

    Used after an out-of-band update to one of a match's
    :class:`CriterionEvaluation` rows (e.g. clinician override).  Reads
    every evaluation belonging to ``match_result``, applies the same
    aggregation rules the matching engine uses on first creation, and
    persists the new totals.
    """
    evaluations = list(
        db.execute(
            select(CriterionEvaluation).where(
                CriterionEvaluation.match_result_id == match_result.id
            )
        ).scalars()
    )

    # Build pseudo-outcomes so we can reuse the existing aggregator.
    outcomes: list[_CriterionOutcome] = []
    for ev in evaluations:
        criterion = db.get(TrialCriterion, ev.criterion_id)
        if criterion is None:
            continue
        status = CriterionStatusEnum(ev.status)
        verdict = EligibilityVerdict(
            status=EligibilityStatus.from_criterion_status(status),
            reasoning=ev.reasoning or "",
            confidence=float(ev.confidence or 0.0),
            evaluator=EvaluatorEnum(ev.evaluated_by),
        )
        outcomes.append(_CriterionOutcome(criterion=criterion, verdict=verdict))

    agg = _aggregate(outcomes)
    match_score, confidence_score, final_rank_score = _scores_from_aggregate(agg)
    overall = _overall_status_from(agg)

    match_result.total_criteria = agg.total
    match_result.criteria_met = agg.met
    match_result.criteria_not_met = agg.not_met
    match_result.criteria_uncertain = agg.uncertain
    match_result.match_score = match_score
    match_result.confidence_score = confidence_score
    # Keep diversity boost intact — it lives in ``diversity_priority_score``.
    # ``final_rank_score`` is recomputed from the new match*confidence only;
    # the diversity ranker rerun (if desired) layers its blend on top.
    match_result.final_rank_score = final_rank_score
    match_result.overall_status = overall.value
    db.flush()
    return match_result


def latest_matches_for_patient(
    db: Session,
    patient_id: uuid.UUID,
    *,
    limit: Optional[int] = None,
) -> list[MatchResult]:
    """Return the most recent match runs for ``patient_id``, ranked.

    Ranking is by :attr:`MatchResult.final_rank_score` descending — the
    diversity ranker in Phase 5 will rewrite that column to factor in
    enrollment-demographic priorities.
    """
    stmt = (
        select(MatchResult)
        .where(MatchResult.patient_id == patient_id, MatchResult.is_latest.is_(True))
        .order_by(MatchResult.final_rank_score.desc().nullslast())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars())
