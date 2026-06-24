"""Pydantic schemas for the matching engine results.

These schemas drive the ``/matching`` endpoints — triggering a match run,
returning aggregate match results, and exposing per-criterion evaluations
with their evidence for the explainability UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.matching import (
    CoordinatorStatusEnum,
    CriterionStatusEnum,
    EvaluatorEnum,
    MatchTriggerEnum,
    MissingDataTypeEnum,
    OverallMatchStatusEnum,
    UncertaintyPriorityEnum,
)


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

class MatchTriggerRequest(BaseModel):
    """Trigger a match run for a patient (optionally scoped to specific trials)."""

    patient_id: uuid.UUID
    trial_ids: Optional[list[uuid.UUID]] = Field(
        default=None,
        description=(
            "If omitted, the engine matches against every recruiting trial in "
            "the catalog. Otherwise only the listed trial IDs are evaluated."
        ),
    )
    triggered_by: MatchTriggerEnum = MatchTriggerEnum.MANUAL


class MatchTriggerResponse(BaseModel):
    """Acknowledgement payload for a match-run request."""

    patient_id: uuid.UUID
    trials_queued: int
    triggered_by: MatchTriggerEnum
    started_at: datetime


# ---------------------------------------------------------------------------
# UncertaintyFlag
# ---------------------------------------------------------------------------

class UncertaintyFlagRead(BaseModel):
    """Actionable data-gap flag exposed to coordinators."""

    id: uuid.UUID
    match_result_id: uuid.UUID
    criterion_evaluation_id: Optional[uuid.UUID] = None
    missing_data_type: MissingDataTypeEnum
    description: str
    resolution_action: Optional[str] = None
    priority: UncertaintyPriorityEnum = UncertaintyPriorityEnum.MEDIUM
    resolved: bool
    resolved_at: Optional[datetime] = None
    resolved_by_event_id: Optional[uuid.UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# CriterionEvaluation
# ---------------------------------------------------------------------------

class CriterionEvaluationRead(BaseModel):
    """Per-criterion verdict — the unit of explainability."""

    id: uuid.UUID
    match_result_id: uuid.UUID
    criterion_id: uuid.UUID
    status: CriterionStatusEnum
    reasoning: str
    evidence_text: Optional[str] = None
    evidence_source: Optional[str] = None
    evidence_event_id: Optional[uuid.UUID] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    evaluated_by: EvaluatorEnum
    llm_model_used: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Coordinator review
# ---------------------------------------------------------------------------

class CoordinatorReviewUpdate(BaseModel):
    """Coordinator-supplied review state for a match result."""

    coordinator_status: CoordinatorStatusEnum
    coordinator_notes: Optional[str] = None


# ---------------------------------------------------------------------------
# MatchResult
# ---------------------------------------------------------------------------

class MatchResultRead(BaseModel):
    """Aggregate match outcome between one patient and one trial."""

    id: uuid.UUID
    patient_id: uuid.UUID
    trial_id: uuid.UUID
    patient_version: int
    overall_status: OverallMatchStatusEnum
    match_score: float = Field(..., ge=0.0, le=1.0)
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    diversity_priority_score: Optional[float] = None
    final_rank_score: Optional[float] = None
    total_criteria: int
    criteria_met: int
    criteria_not_met: int
    criteria_uncertain: int
    missing_data_summary: Optional[str] = None
    plain_language_summary: Optional[str] = None
    summary_language: str = "en"
    coordinator_status: CoordinatorStatusEnum
    coordinator_notes: Optional[str] = None
    reviewed_by: Optional[uuid.UUID] = None
    reviewed_at: Optional[datetime] = None
    triggered_by: MatchTriggerEnum
    is_latest: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MatchResultDetailRead(MatchResultRead):
    """Match result enriched with per-criterion evaluations and flags."""

    criterion_evaluations: list[CriterionEvaluationRead] = Field(default_factory=list)
    uncertainty_flags: list[UncertaintyFlagRead] = Field(default_factory=list)


class PatientMatchesResponse(BaseModel):
    """Listing of latest matches for a patient, ranked."""

    patient_id: uuid.UUID
    total: int
    matches: list[MatchResultRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Explainability export
# ---------------------------------------------------------------------------

class ExplainabilityRow(BaseModel):
    """One row in the human-readable explainability table."""

    criterion_text: str
    status: CriterionStatusEnum
    evidence_text: Optional[str] = None
    data_source: Optional[str] = None
    reasoning: str


class ExplainabilityExport(BaseModel):
    """Full per-criterion breakdown ready for clinician review."""

    match_result_id: uuid.UUID
    patient_id: uuid.UUID
    trial_id: uuid.UUID
    overall_status: OverallMatchStatusEnum
    rows: list[ExplainabilityRow] = Field(default_factory=list)
    rendered_markdown: Optional[str] = None
    rendered_json: Optional[dict[str, Any]] = None
