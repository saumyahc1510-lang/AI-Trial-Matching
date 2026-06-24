"""Three-state logic + actionable missing-data flagging.

When the eligibility reasoner returns ``uncertain`` for a criterion, the
matching engine needs to know *why* — and, more importantly, what a
coordinator could do to resolve it.  This module converts an uncertain
:class:`~app.services.eligibility_reasoner.EligibilityVerdict` into:

* an :class:`UncertaintyFlag` row (persisted to the DB) with a priority
  and a concrete ``resolution_action``;
* a coordinator-friendly aggregate summary stitched into the
  :class:`MatchResult` so the UI can show a single "missing data" panel
  per trial without having to drill into individual criteria.

The classification is intentionally rule-based, not LLM-driven.  We
already paid for an LLM call in the reasoner; rerunning the model just
to label "what kind of missing data is this?" would be wasteful when a
keyword match against the criterion's category + text gets it right
~95 % of the time.

Priorities
----------
* ``HIGH``  — the criterion is marked critical *and* lies on a common
  missing-data class (e.g. recent lab, medication reconciliation, vital
  signs).  These block enrollment and a coordinator should resolve them
  first.
* ``MEDIUM`` — critical criterion but the missing-data class is harder
  to act on (e.g. genetic testing).
* ``LOW``   — non-critical / advisory criterion.

Resolution actions
------------------
The module ships a small lookup of recommended next steps per
:class:`MissingDataTypeEnum`.  Callers can override per flag — useful
for site-specific workflows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.matching import (
    CriterionEvaluation,
    CriterionStatusEnum,
    MatchResult,
    MissingDataTypeEnum,
    UncertaintyFlag,
    UncertaintyPriorityEnum,
)
from app.models.trial import CriterionCategoryEnum, TrialCriterion

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Category → default missing-data type when nothing better can be inferred.
_CATEGORY_TO_TYPE: dict[str, MissingDataTypeEnum] = {
    CriterionCategoryEnum.LAB_VALUE.value:      MissingDataTypeEnum.LAB_RESULT,
    CriterionCategoryEnum.MEDICATION.value:     MissingDataTypeEnum.MEDICATION_HISTORY,
    CriterionCategoryEnum.DIAGNOSIS.value:      MissingDataTypeEnum.DIAGNOSIS_CONFIRMATION,
    CriterionCategoryEnum.PROCEDURE.value:      MissingDataTypeEnum.DIAGNOSIS_CONFIRMATION,
    CriterionCategoryEnum.TEMPORAL.value:       MissingDataTypeEnum.TEMPORAL_DATA,
    CriterionCategoryEnum.GENETIC.value:        MissingDataTypeEnum.GENETIC_TEST,
    CriterionCategoryEnum.ORGAN_FUNCTION.value: MissingDataTypeEnum.LAB_RESULT,
    CriterionCategoryEnum.DEMOGRAPHIC.value:    MissingDataTypeEnum.OTHER,
    CriterionCategoryEnum.LIFESTYLE.value:      MissingDataTypeEnum.OTHER,
    CriterionCategoryEnum.OTHER.value:          MissingDataTypeEnum.OTHER,
}


# Keyword sets we scan in the criterion's text to *refine* the category
# default — e.g. an ORGAN_FUNCTION criterion mentioning "MRI" should
# flag IMAGING, not LAB_RESULT.
_KEYWORD_OVERRIDES: list[tuple[tuple[str, ...], MissingDataTypeEnum]] = [
    (("mri", "ct scan", "x-ray", "ultrasound", "imaging", "radiograph"), MissingDataTypeEnum.IMAGING),
    (("genetic", "mutation", "brca", "gene"),                             MissingDataTypeEnum.GENETIC_TEST),
    (("blood pressure", "heart rate", "spo2", "vital"),                   MissingDataTypeEnum.VITAL_SIGN),
    (("hba1c", "creatinine", "hemoglobin", "platelet", "lab"),            MissingDataTypeEnum.LAB_RESULT),
]


# Default resolution prompts shown to coordinators.  Keep them
# *concrete actions* — "Order the test", "Verify with patient" — never
# vague hand-waves like "Get more data".
_DEFAULT_RESOLUTION: dict[MissingDataTypeEnum, str] = {
    MissingDataTypeEnum.LAB_RESULT:
        "Order the missing lab test or pull the latest result from the EHR.",
    MissingDataTypeEnum.MEDICATION_HISTORY:
        "Confirm the current medication list with the patient or pharmacy.",
    MissingDataTypeEnum.DIAGNOSIS_CONFIRMATION:
        "Verify the diagnosis with the treating clinician or attach the pathology report.",
    MissingDataTypeEnum.TEMPORAL_DATA:
        "Look up the exact date of the event in the source EHR system.",
    MissingDataTypeEnum.IMAGING:
        "Request the imaging study or its report.",
    MissingDataTypeEnum.GENETIC_TEST:
        "Order the genetic / molecular test, or attach an existing report.",
    MissingDataTypeEnum.VITAL_SIGN:
        "Capture the relevant vital sign at the next visit.",
    MissingDataTypeEnum.OTHER:
        "Document the missing information in the patient record.",
}


def _classify_missing_type(
    criterion: TrialCriterion,
    *,
    hint_text: Optional[str] = None,
) -> MissingDataTypeEnum:
    """Classify what kind of data is missing for an uncertain criterion.

    First scans the criterion's text + the LLM's missing-data hint for a
    keyword override, then falls back to the category default.
    """
    haystack_parts = [
        (criterion.original_text or "").lower(),
        (criterion.parsed_description or "").lower(),
        (hint_text or "").lower(),
    ]
    haystack = " ".join(part for part in haystack_parts if part)
    for keywords, dtype in _KEYWORD_OVERRIDES:
        if any(kw in haystack for kw in keywords):
            return dtype
    return _CATEGORY_TO_TYPE.get(criterion.category or "", MissingDataTypeEnum.OTHER)


def _classify_priority(
    criterion: TrialCriterion,
    missing_type: MissingDataTypeEnum,
) -> UncertaintyPriorityEnum:
    """Rank the urgency of resolving this flag.

    Critical inclusion criteria with a quick-win missing-data class
    (labs / medication / vitals) become HIGH.  Critical-but-hard-to-act
    (genetic) become MEDIUM.  Non-critical criteria are always LOW.
    """
    if not criterion.is_critical:
        return UncertaintyPriorityEnum.LOW

    high_impact = {
        MissingDataTypeEnum.LAB_RESULT,
        MissingDataTypeEnum.MEDICATION_HISTORY,
        MissingDataTypeEnum.VITAL_SIGN,
        MissingDataTypeEnum.TEMPORAL_DATA,
        MissingDataTypeEnum.DIAGNOSIS_CONFIRMATION,
    }
    if missing_type in high_impact:
        return UncertaintyPriorityEnum.HIGH
    return UncertaintyPriorityEnum.MEDIUM


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UncertaintyDraft:
    """In-memory draft of an :class:`UncertaintyFlag` row.

    Held separately from the ORM model so the matching engine can build
    a list of drafts inside its loop and then commit them in one batch
    — keeps the unit-of-work tidy and the function under test trivially.
    """

    missing_data_type: MissingDataTypeEnum
    description: str
    resolution_action: str
    priority: UncertaintyPriorityEnum
    criterion_id: str
    criterion_evaluation_id: Optional[str] = None


@dataclass
class UncertaintySummary:
    """Coordinator-friendly aggregate of all flags on a match result."""

    total_uncertain: int = 0
    high_priority: int = 0
    medium_priority: int = 0
    low_priority: int = 0
    summary_text: str = ""
    drafts: list[UncertaintyDraft] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Draft builders
# ---------------------------------------------------------------------------

def build_uncertainty_draft(
    criterion: TrialCriterion,
    *,
    evaluation_id: Optional[str] = None,
    description_override: Optional[str] = None,
    hint_text: Optional[str] = None,
    resolution_override: Optional[str] = None,
) -> UncertaintyDraft:
    """Create an :class:`UncertaintyDraft` from one uncertain criterion."""
    missing_type = _classify_missing_type(criterion, hint_text=hint_text)
    priority = _classify_priority(criterion, missing_type)

    description = (
        description_override
        or hint_text
        or (
            f"Missing data to evaluate criterion: "
            f"{criterion.parsed_description or criterion.original_text!r}"
        )
    )
    resolution = resolution_override or _DEFAULT_RESOLUTION[missing_type]

    return UncertaintyDraft(
        missing_data_type=missing_type,
        description=description,
        resolution_action=resolution,
        priority=priority,
        criterion_id=str(criterion.id),
        criterion_evaluation_id=evaluation_id,
    )


def summarise_uncertainty(
    drafts: Iterable[UncertaintyDraft],
    *,
    max_items_in_text: int = 5,
) -> UncertaintySummary:
    """Aggregate flags into the per-match ``missing_data_summary`` text.

    Produces a compact, ordered (HIGH → LOW) bullet list capped at
    ``max_items_in_text``.  The matching engine writes this directly
    into :attr:`MatchResult.missing_data_summary`.
    """
    drafts_list = list(drafts)
    summary = UncertaintySummary()
    if not drafts_list:
        return summary

    summary.total_uncertain = len(drafts_list)
    for d in drafts_list:
        if d.priority == UncertaintyPriorityEnum.HIGH:
            summary.high_priority += 1
        elif d.priority == UncertaintyPriorityEnum.MEDIUM:
            summary.medium_priority += 1
        else:
            summary.low_priority += 1
    summary.drafts = drafts_list

    # Render: HIGH first, then MEDIUM, then LOW.  Stable within each tier.
    rank = {
        UncertaintyPriorityEnum.HIGH:   0,
        UncertaintyPriorityEnum.MEDIUM: 1,
        UncertaintyPriorityEnum.LOW:    2,
    }
    ordered = sorted(drafts_list, key=lambda d: rank[d.priority])
    visible = ordered[:max_items_in_text]
    bullets = [
        f"- [{d.priority.value.upper()}] {d.description.strip()}  "
        f"→ {d.resolution_action.strip()}"
        for d in visible
    ]
    extra = len(ordered) - len(visible)
    if extra > 0:
        bullets.append(f"- … plus {extra} additional uncertain criterion(s).")
    summary.summary_text = "\n".join(bullets)
    return summary


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist_flags(
    db: Session,
    match_result: MatchResult,
    summary: UncertaintySummary,
) -> list[UncertaintyFlag]:
    """Write the summary's drafts to the ``uncertainty_flags`` table.

    Existing flags for ``match_result`` are *not* cleared here — the
    matching engine owns that decision and typically replaces flags as
    part of its own transaction.
    """
    rows: list[UncertaintyFlag] = []
    for d in summary.drafts:
        row = UncertaintyFlag(
            match_result_id=match_result.id,
            criterion_evaluation_id=d.criterion_evaluation_id,
            missing_data_type=d.missing_data_type.value,
            description=d.description,
            resolution_action=d.resolution_action,
            priority=d.priority.value,
            resolved=False,
        )
        db.add(row)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Convenience: derive a summary from a set of persisted evaluations
# ---------------------------------------------------------------------------

def summarise_from_evaluations(
    evaluations: Iterable[CriterionEvaluation],
    criteria_by_id: dict[str, TrialCriterion],
) -> UncertaintySummary:
    """Build a summary directly from existing :class:`CriterionEvaluation` rows.

    Useful when a re-match worker wants to refresh just the summary text
    after one flag has been resolved by new EHR data.
    """
    drafts: list[UncertaintyDraft] = []
    for ev in evaluations:
        if ev.status != CriterionStatusEnum.UNCERTAIN.value:
            continue
        criterion = criteria_by_id.get(str(ev.criterion_id))
        if criterion is None:
            continue
        drafts.append(
            build_uncertainty_draft(
                criterion,
                evaluation_id=str(ev.id),
                hint_text=ev.reasoning,
            )
        )
    return summarise_uncertainty(drafts)
