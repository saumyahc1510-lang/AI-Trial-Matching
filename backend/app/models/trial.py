"""Clinical trial catalog models for the AI Clinical Trial Matching system.

This module defines the tables that represent trials sourced from
ClinicalTrials.gov (or manually added by sponsors):

- ClinicalTrial: top-level trial record with metadata, status, and raw
  eligibility text.
- TrialCriterion: individual inclusion / exclusion criterion parsed by
  the LLM pipeline, with optional temporal and value constraints stored
  as JSONB.
- TrialSite: geocoded facility where the trial is conducted.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class CriterionTypeEnum(str, enum.Enum):
    """Whether a criterion is for inclusion or exclusion."""

    INCLUSION = "inclusion"
    EXCLUSION = "exclusion"


class CriterionCategoryEnum(str, enum.Enum):
    """Semantic category of a parsed eligibility criterion."""

    DEMOGRAPHIC = "demographic"
    DIAGNOSIS = "diagnosis"
    LAB_VALUE = "lab_value"
    MEDICATION = "medication"
    PROCEDURE = "procedure"
    TEMPORAL = "temporal"
    LIFESTYLE = "lifestyle"
    GENETIC = "genetic"
    ORGAN_FUNCTION = "organ_function"
    OTHER = "other"


class SiteStatusEnum(str, enum.Enum):
    """Recruitment status of an individual trial site."""

    RECRUITING = "recruiting"
    NOT_YET_RECRUITING = "not_yet_recruiting"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    WITHDRAWN = "withdrawn"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ClinicalTrial(Base):
    """A clinical trial pulled from ClinicalTrials.gov or added manually.

    The ``raw_eligibility_text`` stores the original free-text inclusion /
    exclusion block.  The LLM pipeline parses this into individual
    :class:`TrialCriterion` rows for structured matching.

    Attributes:
        nct_id: National Clinical Trial identifier (e.g. ``NCT04886804``).
        conditions: JSONB list of condition/disease strings.
        interventions: JSONB list of intervention objects
            (``{"type": "Drug", "name": "Pembrolizumab"}``).
        enrollment_demographics: Optional JSONB with race/ethnicity
            breakdown used by the diversity ranker.
        is_manually_added: ``True`` for trials entered by sponsors rather
            than synced from CT.gov.
    """

    __tablename__ = "clinical_trials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nct_id = Column(String(20), unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    brief_summary = Column(Text, nullable=True)
    phase = Column(String(30), nullable=True)
    overall_status = Column(String(30), nullable=False)
    study_type = Column(String(30), nullable=True)
    conditions = Column(JSONB, nullable=True)
    interventions = Column(JSONB, nullable=True)
    sponsor = Column(String(500), nullable=True)
    # Top-level clinical specialty derived from ``conditions`` by
    # :mod:`app.services.trial_category`.  Indexed because the catalog
    # dropdown filters on it.
    category = Column(String(50), nullable=True, index=True)
    enrollment_count = Column(Integer, nullable=True)
    enrollment_demographics = Column(JSONB, nullable=True)
    start_date = Column(Date, nullable=True)
    completion_date = Column(Date, nullable=True)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    raw_eligibility_text = Column(Text, nullable=True)
    source_url = Column(String(500), nullable=True)
    is_manually_added = Column(Boolean, nullable=False, default=False)
    # JSONB map of ``{lang_code: plain_language_summary}`` — populated
    # by :mod:`app.services.plain_language` so that translations are
    # cached across patients who share a language.  Sync-and-overwrite
    # in :mod:`trial_sync` is careful never to touch this column.
    summary_cache = Column(JSONB, nullable=True)

    # -- Relationships -------------------------------------------------------
    criteria = relationship(
        "TrialCriterion",
        back_populates="trial",
        cascade="all, delete-orphan",
        order_by="TrialCriterion.order_index",
    )
    sites = relationship(
        "TrialSite",
        back_populates="trial",
        cascade="all, delete-orphan",
    )
    match_results = relationship(
        "MatchResult",
        back_populates="trial",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<ClinicalTrial(id={self.id!s}, "
            f"nct_id={self.nct_id!r}, "
            f"status={self.overall_status!r})>"
        )


class TrialCriterion(Base):
    """A single parsed eligibility criterion for a clinical trial.

    Each criterion is extracted from the trial's raw eligibility text by
    the LLM parsing pipeline.  Structured constraint columns allow the
    matching engine to evaluate criteria programmatically.

    Attributes:
        original_text: Verbatim text from the trial protocol.
        parsed_description: LLM-generated structured interpretation.
        temporal_constraint: Optional JSONB encoding temporal rules,
            e.g. ``{"type": "within", "duration_months": 6,
            "reference": "enrollment"}``.
        value_constraint: Optional JSONB encoding numeric thresholds,
            e.g. ``{"metric": "HbA1c", "operator": "<", "value": 8.0,
            "unit": "%"}``.
        is_critical: ``True`` (default) means failing this criterion is a
            hard disqualifier; ``False`` marks it as advisory.
    """

    __tablename__ = "trial_criteria"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clinical_trials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    criterion_type = Column(String(20), nullable=False)
    category = Column(String(30), nullable=False, default=CriterionCategoryEnum.OTHER.value)
    original_text = Column(Text, nullable=False)
    parsed_description = Column(Text, nullable=True)
    temporal_constraint = Column(JSONB, nullable=True)
    value_constraint = Column(JSONB, nullable=True)
    is_critical = Column(Boolean, nullable=False, default=True)
    order_index = Column(Integer, nullable=False, default=0)

    # -- Relationships -------------------------------------------------------
    trial = relationship("ClinicalTrial", back_populates="criteria")
    criterion_evaluations = relationship(
        "CriterionEvaluation",
        back_populates="criterion",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<TrialCriterion(id={self.id!s}, "
            f"type={self.criterion_type!r}, "
            f"category={self.category!r})>"
        )


class TrialSite(Base):
    """A physical facility participating in a clinical trial.

    Geocoded latitude / longitude enable proximity-based matching and
    map display for patients.  Each site tracks its own recruitment
    status independently of the trial-level status.

    Attributes:
        latitude / longitude: Geocoded coordinates (nullable until
            geocoding is run).
        site_status: Per-site recruitment status which may differ from
            the trial's ``overall_status``.
    """

    __tablename__ = "trial_sites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clinical_trials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    facility_name = Column(String(500), nullable=False)
    city = Column(String(200), nullable=True)
    state = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    zip_code = Column(String(20), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    site_status = Column(
        String(30), nullable=False, default=SiteStatusEnum.RECRUITING.value
    )
    contact_name = Column(String(300), nullable=True)
    contact_email = Column(String(300), nullable=True)
    contact_phone = Column(String(50), nullable=True)

    # -- Relationships -------------------------------------------------------
    trial = relationship("ClinicalTrial", back_populates="sites")

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<TrialSite(id={self.id!s}, "
            f"facility={self.facility_name!r}, "
            f"status={self.site_status!r})>"
        )
