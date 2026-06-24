"""Match result and explainability models for the AI Clinical Trial Matching system.

This module houses the tables that capture *why* a patient was (or was
not) matched to a trial:

- MatchResult: the aggregate outcome for one patient–trial pair,
  including scores, coordinator review status, and plain-language
  summaries.
- CriterionEvaluation: per-criterion verdict with LLM reasoning and
  evidence provenance — the fundamental unit of explainability.
- UncertaintyFlag: actionable items describing data gaps that prevent
  a definitive eligibility determination.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class OverallMatchStatusEnum(str, enum.Enum):
    """Aggregate eligibility verdict for a patient–trial pair."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNCERTAIN = "uncertain"


class CoordinatorStatusEnum(str, enum.Enum):
    """Human-review status assigned by a trial coordinator."""

    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEFERRED = "deferred"


class MatchTriggerEnum(str, enum.Enum):
    """What event caused this match run to be executed."""

    INITIAL_MATCH = "initial_match"
    CHART_UPDATE = "chart_update"
    NEW_TRIAL = "new_trial"
    MANUAL = "manual"
    TRIAL_STATUS_CHANGE = "trial_status_change"


class CriterionStatusEnum(str, enum.Enum):
    """Evaluation verdict for a single criterion."""

    MET = "met"
    NOT_MET = "not_met"
    UNCERTAIN = "uncertain"


class EvaluatorEnum(str, enum.Enum):
    """Which engine produced the criterion evaluation."""

    LLM = "llm"
    TEMPORAL_ENGINE = "temporal_engine"
    RULE_ENGINE = "rule_engine"


class MissingDataTypeEnum(str, enum.Enum):
    """Category of data that is missing and needed for evaluation."""

    LAB_RESULT = "lab_result"
    MEDICATION_HISTORY = "medication_history"
    DIAGNOSIS_CONFIRMATION = "diagnosis_confirmation"
    TEMPORAL_DATA = "temporal_data"
    IMAGING = "imaging"
    GENETIC_TEST = "genetic_test"
    VITAL_SIGN = "vital_sign"
    OTHER = "other"


class UncertaintyPriorityEnum(str, enum.Enum):
    """Urgency level for resolving an uncertainty flag."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class MatchResult(Base):
    """Aggregate match result between one patient and one clinical trial.

    A new row is created for every match run (triggered by chart updates,
    new trials, status changes, etc.).  The ``is_latest`` flag marks the
    most-recent evaluation for each patient–trial pair so the UI can
    query without window functions.

    Attributes:
        match_score: Weighted proportion of criteria met (0.0–1.0).
        confidence_score: Proportion of criteria evaluated with high
            confidence (0.0–1.0).
        diversity_priority_score: Optional boost from the equity/diversity
            ranker.
        final_rank_score: Combined score used for display ordering.
        plain_language_summary: Auto-generated patient-facing description
            of the trial and their eligibility.
        coordinator_status: Human-in-the-loop review state.
        triggered_by: Which system event caused this match run.
    """

    __tablename__ = "match_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trial_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clinical_trials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    patient_version = Column(Integer, nullable=False)
    overall_status = Column(
        String(20), nullable=False, default=OverallMatchStatusEnum.UNCERTAIN.value
    )
    match_score = Column(Float, nullable=False, default=0.0)
    confidence_score = Column(Float, nullable=False, default=0.0)
    diversity_priority_score = Column(Float, nullable=True)
    final_rank_score = Column(Float, nullable=True)

    # Criterion counters
    total_criteria = Column(Integer, nullable=False, default=0)
    criteria_met = Column(Integer, nullable=False, default=0)
    criteria_not_met = Column(Integer, nullable=False, default=0)
    criteria_uncertain = Column(Integer, nullable=False, default=0)

    # Summaries
    missing_data_summary = Column(Text, nullable=True)
    plain_language_summary = Column(Text, nullable=True)
    summary_language = Column(String(10), nullable=False, default="en")

    # Coordinator review
    coordinator_status = Column(
        String(30),
        nullable=False,
        default=CoordinatorStatusEnum.PENDING_REVIEW.value,
    )
    coordinator_notes = Column(Text, nullable=True)
    reviewed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at = Column(DateTime(timezone=True), nullable=True)

    # Provenance
    triggered_by = Column(
        String(30), nullable=False, default=MatchTriggerEnum.INITIAL_MATCH.value
    )
    is_latest = Column(Boolean, nullable=False, default=True)

    # -- Relationships -------------------------------------------------------
    patient = relationship("Patient", back_populates="match_results")
    trial = relationship("ClinicalTrial", back_populates="match_results")
    criterion_evaluations = relationship(
        "CriterionEvaluation",
        back_populates="match_result",
        cascade="all, delete-orphan",
    )
    uncertainty_flags = relationship(
        "UncertaintyFlag",
        back_populates="match_result",
        cascade="all, delete-orphan",
    )
    feedbacks = relationship(
        "ClinicianFeedback",
        back_populates="match_result",
        cascade="all, delete-orphan",
    )
    reviewer = relationship("User", foreign_keys=[reviewed_by])

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<MatchResult(id={self.id!s}, "
            f"status={self.overall_status!r}, "
            f"score={self.match_score:.2f})>"
        )


class CriterionEvaluation(Base):
    """Per-criterion evaluation result — the core explainability unit.

    Each row records whether a single :class:`TrialCriterion` was met,
    not met, or uncertain for a given :class:`MatchResult`, along with
    the LLM reasoning and a link to the supporting evidence in the
    patient timeline.

    Attributes:
        reasoning: LLM-generated natural-language explanation.
        evidence_text: The exact sentence / data point from the patient
            record that supports the verdict.
        evidence_event_id: Optional FK to the specific
            :class:`MedicalEvent` used as evidence.
        evaluated_by: Which engine produced this evaluation (LLM,
            temporal engine, or rule engine).
        llm_model_used: Model identifier for reproducibility.
    """

    __tablename__ = "criterion_evaluations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("match_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    criterion_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trial_criteria.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(
        String(20), nullable=False, default=CriterionStatusEnum.UNCERTAIN.value
    )
    reasoning = Column(Text, nullable=False)
    evidence_text = Column(Text, nullable=True)
    evidence_source = Column(String(500), nullable=True)
    evidence_event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("medical_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence = Column(Float, nullable=False, default=0.0)
    evaluated_by = Column(
        String(30), nullable=False, default=EvaluatorEnum.LLM.value
    )
    llm_model_used = Column(String(100), nullable=True)

    # -- Relationships -------------------------------------------------------
    match_result = relationship("MatchResult", back_populates="criterion_evaluations")
    criterion = relationship("TrialCriterion", back_populates="criterion_evaluations")
    evidence_event = relationship(
        "MedicalEvent",
        back_populates="criterion_evaluations",
        foreign_keys=[evidence_event_id],
    )
    feedbacks = relationship(
        "ClinicianFeedback",
        back_populates="criterion_evaluation",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<CriterionEvaluation(id={self.id!s}, "
            f"status={self.status!r}, "
            f"confidence={self.confidence:.2f})>"
        )


class UncertaintyFlag(Base):
    """Actionable missing-data item that prevents a definitive evaluation.

    Flags are generated when a :class:`CriterionEvaluation` returns
    ``uncertain`` and the system can identify *what* data is missing and
    *what action* would resolve the uncertainty.

    Attributes:
        description: Human-readable statement, e.g. "Most recent HbA1c
            lab value not found.  Last recorded: 8 months ago."
        resolution_action: Suggested next step, e.g. "Order HbA1c test".
        resolved_by_event_id: FK to the :class:`MedicalEvent` that
            eventually resolved this flag (once new data arrives).
    """

    __tablename__ = "uncertainty_flags"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    match_result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("match_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    criterion_evaluation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("criterion_evaluations.id", ondelete="SET NULL"),
        nullable=True,
    )
    missing_data_type = Column(String(40), nullable=False)
    description = Column(Text, nullable=False)
    resolution_action = Column(Text, nullable=True)
    priority = Column(
        String(10), nullable=False, default=UncertaintyPriorityEnum.MEDIUM.value
    )
    resolved = Column(Boolean, nullable=False, default=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolved_by_event_id = Column(
        UUID(as_uuid=True),
        ForeignKey("medical_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- Relationships -------------------------------------------------------
    match_result = relationship("MatchResult", back_populates="uncertainty_flags")
    criterion_evaluation = relationship(
        "CriterionEvaluation", foreign_keys=[criterion_evaluation_id]
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<UncertaintyFlag(id={self.id!s}, "
            f"type={self.missing_data_type!r}, "
            f"priority={self.priority!r}, "
            f"resolved={self.resolved})>"
        )
