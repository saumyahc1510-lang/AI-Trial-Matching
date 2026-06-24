"""Diversity-aware ranking pass over match results.

A patient who's clinically eligible for two trials might prefer (and
benefit from) the one where their demographic group is *under-
represented*.  This module surfaces that signal by:

1. Comparing the patient's race / ethnicity / sex / age band against
   the trial's current enrollment mix (stored on
   :attr:`ClinicalTrial.enrollment_demographics`).
2. Comparing both against U.S. **population baselines** so an "under-
   represented" call means more than "this trial is small".
3. Blending the resulting ``diversity_priority_score`` into
   :attr:`MatchResult.final_rank_score` so the API's ranked list reflects
   both clinical fit *and* representation priority.

Design choices
--------------
* **Read-modify-write, never re-evaluate criteria.**  The ranker runs as
  a second pass *after* :mod:`matching_engine`; it never re-touches
  per-criterion verdicts.  That makes it cheap, idempotent, and easy to
  toggle off.
* **No LLM calls.**  Everything is structured arithmetic against
  numeric baselines — adding a model in here would be both slow and
  scientifically suspect.
* **Soft signal, not a gate.**  ``diversity_priority`` only nudges the
  rank; it never converts an ineligible match to eligible.  Default
  weights: ``α = 0.85`` for ``match_score * confidence`` and
  ``β = 0.15`` for the diversity boost.  Tune via
  ``DIVERSITY_RANK_ALPHA`` / ``DIVERSITY_RANK_BETA`` env vars.

Score semantics
---------------
``diversity_priority_score ∈ [0, 1]``:

* ``0.5`` — neutral; no enrollment data available, or patient is
  represented at roughly the baseline rate.
* ``> 0.5`` — patient belongs to a group *underrepresented* in this
  trial relative to the population baseline.
* ``< 0.5`` — patient belongs to a group already *overrepresented*.

The companion :attr:`diversity_note` text is what the UI shows to
coordinators ("This trial has enrolled 3% Hispanic participants vs.
18% population baseline").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.matching import MatchResult
from app.models.patient import Patient
from app.models.trial import ClinicalTrial

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Population baselines
# ---------------------------------------------------------------------------

# Rough U.S. census-derived proportions.  Numbers are intentionally
# editable — every health system has its own service-area baselines.
# These can be moved into a config row later without touching this code.
_POPULATION_BASELINE_RACE: dict[str, float] = {
    "white":                                  0.59,
    "black or african american":              0.135,
    "asian":                                  0.063,
    "american indian or alaska native":       0.013,
    "native hawaiian or other pacific islander": 0.003,
    "other":                                  0.082,
    "two or more races":                      0.124,
}

_POPULATION_BASELINE_ETHNICITY: dict[str, float] = {
    "hispanic or latino":     0.187,
    "not hispanic or latino": 0.813,
}

_POPULATION_BASELINE_SEX: dict[str, float] = {
    "female": 0.508,
    "male":   0.492,
}

# Default blend weights — overridable via env (see :func:`get_blend_weights`).
_DEFAULT_ALPHA = 0.85
_DEFAULT_BETA = 0.15


def get_blend_weights() -> tuple[float, float]:
    """Return ``(alpha, beta)`` rank-blend weights from env or defaults.

    Reads settings lazily so tests can monkey-patch
    :func:`app.config.get_settings` without touching this module.
    """
    from app.config import get_settings

    settings = get_settings()
    alpha = float(getattr(settings, "DIVERSITY_RANK_ALPHA", _DEFAULT_ALPHA))
    beta = float(getattr(settings, "DIVERSITY_RANK_BETA", _DEFAULT_BETA))
    # Guard against silly inputs — keep both in [0, 1] and not both zero.
    alpha = max(0.0, min(1.0, alpha))
    beta = max(0.0, min(1.0, beta))
    if alpha + beta == 0.0:
        alpha, beta = _DEFAULT_ALPHA, _DEFAULT_BETA
    return alpha, beta


# ---------------------------------------------------------------------------
# Demographic extraction
# ---------------------------------------------------------------------------

def _normalise(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.strip().lower() or None


def _enrollment_fraction(
    demographics: Optional[dict], category: str, group: Optional[str]
) -> Optional[float]:
    """Extract the enrolled-fraction for ``group`` under ``category``.

    Supports two common shapes for :attr:`ClinicalTrial.enrollment_demographics`:

    * Nested counts::

          {"race": {"white": 45, "black or african american": 5}}

    * Pre-computed fractions::

          {"race": {"white": 0.90, "black or african american": 0.10}}

    Counts are converted to fractions on the fly.  Returns ``None`` when
    no usable value is found.
    """
    if not demographics or group is None:
        return None
    block = demographics.get(category)
    if not isinstance(block, dict):
        return None

    # Look up the group case-insensitively.
    normalised = {k.strip().lower(): v for k, v in block.items() if isinstance(k, str)}
    raw = normalised.get(group)
    if raw is None:
        return None

    try:
        raw_num = float(raw)
    except (TypeError, ValueError):
        return None

    # Pre-computed fraction.
    if 0.0 <= raw_num <= 1.0 and all(
        isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0
        for v in normalised.values()
    ):
        return raw_num

    # Counts — convert to a fraction.
    total = sum(float(v) for v in normalised.values() if isinstance(v, (int, float)))
    if total <= 0:
        return None
    return raw_num / total


# ---------------------------------------------------------------------------
# Per-axis underrepresentation calculus
# ---------------------------------------------------------------------------

@dataclass
class _AxisContribution:
    """One axis of the underrepresentation calculation.

    Attributes:
        axis:                "race" | "ethnicity" | "sex".
        group:               Patient's value on this axis (already
                             lower-cased).
        enrollment_fraction: Fraction of this group already enrolled in
                             the trial; ``None`` when not reported.
        baseline_fraction:   Population baseline fraction for this group.
        deficit:             ``baseline - enrollment``.  Positive ⇒
                             underrepresented; capped at the baseline
                             so deficits are bounded.
    """

    axis: str
    group: str
    enrollment_fraction: Optional[float]
    baseline_fraction: float
    deficit: float

    @property
    def note(self) -> Optional[str]:
        """Short, human-readable explanation (or ``None`` if neutral)."""
        if self.enrollment_fraction is None:
            return (
                f"No reported {self.axis} breakdown — using population "
                f"baseline ({self.baseline_fraction:.0%})."
            )
        if self.deficit > 0.05:
            return (
                f"{self.group.title()} participants are "
                f"{self.enrollment_fraction:.0%} of current enrollment vs. "
                f"{self.baseline_fraction:.0%} population baseline."
            )
        if self.deficit < -0.05:
            return (
                f"{self.group.title()} participants already "
                f"{self.enrollment_fraction:.0%} of enrollment vs. "
                f"{self.baseline_fraction:.0%} baseline (overrepresented)."
            )
        return None


def _axis_contribution(
    axis: str,
    patient_group: Optional[str],
    enrollment: Optional[dict],
    baseline_table: dict[str, float],
) -> Optional[_AxisContribution]:
    """Compute the patient's deficit for one demographic axis.

    Returns ``None`` when the patient has no value recorded on the
    axis (we don't penalise the trial for that — coordinators see it
    as "missing data", which is a separate workflow).
    """
    group = _normalise(patient_group)
    if group is None:
        return None
    baseline = baseline_table.get(group)
    if baseline is None:
        # Unknown group label — neutral.
        return None
    enrollment_fraction = _enrollment_fraction(enrollment, axis, group)
    if enrollment_fraction is None:
        deficit = 0.0  # No data → neutral, lean on baseline.
    else:
        # Cap deficit at the baseline so a single missing axis can't dominate.
        deficit = max(-baseline, min(baseline, baseline - enrollment_fraction))
    return _AxisContribution(
        axis=axis,
        group=group,
        enrollment_fraction=enrollment_fraction,
        baseline_fraction=baseline,
        deficit=deficit,
    )


# ---------------------------------------------------------------------------
# Public dataclass + scoring
# ---------------------------------------------------------------------------

@dataclass
class DiversityScore:
    """The per-match output of :func:`score_match_diversity`.

    Attributes:
        score: ``diversity_priority_score`` ∈ [0, 1].
        note:  Concatenated explanations for axes that meaningfully
               diverged from baseline.  ``None`` when nothing notable.
        contributions: Per-axis breakdown (handy for tests / debugging).
    """

    score: float
    note: Optional[str]
    contributions: list[_AxisContribution] = field(default_factory=list)


def score_match_diversity(
    patient: Patient,
    trial: ClinicalTrial,
) -> DiversityScore:
    """Compute the diversity priority for one ``(patient, trial)`` pair."""
    axes: list[_AxisContribution] = []

    race = _axis_contribution(
        "race", patient.race, trial.enrollment_demographics, _POPULATION_BASELINE_RACE
    )
    ethnicity = _axis_contribution(
        "ethnicity", patient.ethnicity, trial.enrollment_demographics, _POPULATION_BASELINE_ETHNICITY
    )
    sex = _axis_contribution(
        "sex", patient.sex, trial.enrollment_demographics, _POPULATION_BASELINE_SEX
    )

    for contrib in (race, ethnicity, sex):
        if contrib is not None:
            axes.append(contrib)

    if not axes:
        return DiversityScore(score=0.5, note=None, contributions=[])

    # Average deficit, then map [-max_baseline, +max_baseline] → [0, 1]
    # with 0.5 anchored at deficit=0.  We use the *largest baseline* of
    # the contributing axes as the scaler so the math is bounded.
    max_baseline = max(c.baseline_fraction for c in axes) or 0.5
    avg_deficit = sum(c.deficit for c in axes) / len(axes)
    raw = 0.5 + (avg_deficit / (2 * max_baseline))
    score = max(0.0, min(1.0, raw))

    notes = [c.note for c in axes if c.note]
    note = "  ".join(notes) if notes else None

    return DiversityScore(score=score, note=note, contributions=axes)


def blend_rank(
    match_score: float,
    confidence_score: float,
    diversity_score: float,
    *,
    alpha: Optional[float] = None,
    beta: Optional[float] = None,
) -> float:
    """Return the combined ``final_rank_score`` for one match.

    Formula::

        final = α · (match_score · confidence_score)
              + β · diversity_score

    ``α`` defaults to 0.85 and ``β`` to 0.15 unless overridden by env
    (see :func:`get_blend_weights`).
    """
    if alpha is None or beta is None:
        a, b = get_blend_weights()
        alpha = a if alpha is None else alpha
        beta = b if beta is None else beta
    return max(
        0.0,
        min(
            1.0,
            alpha * (match_score * confidence_score) + beta * diversity_score,
        ),
    )


# ---------------------------------------------------------------------------
# Persistence pass
# ---------------------------------------------------------------------------

@dataclass
class DiversityPassStats:
    """Summary of one diversity ranking pass."""

    matches_updated: int = 0
    matches_skipped_missing_trial: int = 0
    matches_skipped_missing_patient: int = 0


def rerank_matches_for_patient(
    db: Session,
    patient_id: str,
    *,
    only_latest: bool = True,
) -> DiversityPassStats:
    """Recompute diversity scores + final rank for every match of one patient.

    The default ``only_latest=True`` matches the common case: the engine
    just finished a batch and we want the freshly-written rows updated.
    Set ``only_latest=False`` to backfill historical rows (rarely needed).
    """
    stats = DiversityPassStats()

    stmt = select(MatchResult).where(MatchResult.patient_id == patient_id)
    if only_latest:
        stmt = stmt.where(MatchResult.is_latest.is_(True))
    matches: Iterable[MatchResult] = db.execute(stmt).scalars().all()

    # Cache patient + trials so we don't reload them per row.
    patient = db.get(Patient, patient_id)
    if patient is None:
        stats.matches_skipped_missing_patient = sum(1 for _ in matches)
        return stats

    # Pre-fetch all referenced trials in a single query.
    trial_ids = {m.trial_id for m in matches}
    if not trial_ids:
        return stats
    trial_rows = (
        db.execute(select(ClinicalTrial).where(ClinicalTrial.id.in_(trial_ids)))
        .scalars()
        .all()
    )
    trials_by_id = {t.id: t for t in trial_rows}

    alpha, beta = get_blend_weights()
    for match in matches:
        trial = trials_by_id.get(match.trial_id)
        if trial is None:
            stats.matches_skipped_missing_trial += 1
            continue
        diversity = score_match_diversity(patient, trial)
        final = blend_rank(
            match.match_score or 0.0,
            match.confidence_score or 0.0,
            diversity.score,
            alpha=alpha,
            beta=beta,
        )
        match.diversity_priority_score = diversity.score
        match.final_rank_score = final
        stats.matches_updated += 1

    db.commit()
    return stats


def reranked_score_for_match(
    patient: Patient,
    trial: ClinicalTrial,
    match: MatchResult,
) -> tuple[float, DiversityScore]:
    """Pure helper: compute the new ``final_rank_score`` without persisting.

    Useful from tests and from the matching engine when it wants to
    optionally apply the diversity pass inline.
    """
    diversity = score_match_diversity(patient, trial)
    final = blend_rank(
        match.match_score or 0.0,
        match.confidence_score or 0.0,
        diversity.score,
    )
    return final, diversity
