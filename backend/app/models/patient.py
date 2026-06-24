"""Patient data models for the AI Clinical Trial Matching system.

This module defines the core patient-related tables:
- Patient: demographics, identifiers, and FHIR source data.
- MedicalEvent: every clinical event (diagnosis, lab, medication, etc.)
  forming a timeline used for eligibility matching.
- PatientVersion: immutable snapshots for audit and reproducibility.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Column,
    Date,
    DateTime,
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

class SexEnum(str, enum.Enum):
    """Biological sex as recorded in EHR / FHIR demographics."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class PatientStatusEnum(str, enum.Enum):
    """High-level lifecycle status of a patient record."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    DECEASED = "deceased"


class EventTypeEnum(str, enum.Enum):
    """Category of a clinical event in the patient timeline."""

    DIAGNOSIS = "diagnosis"
    MEDICATION = "medication"
    LAB_RESULT = "lab_result"
    PROCEDURE = "procedure"
    ALLERGY = "allergy"
    VITAL_SIGN = "vital_sign"
    HOSPITALIZATION = "hospitalization"
    IMAGING = "imaging"
    NOTE = "note"


class EventStatusEnum(str, enum.Enum):
    """Clinical status of an individual medical event."""

    ACTIVE = "active"
    RESOLVED = "resolved"
    INACTIVE = "inactive"
    ENTERED_IN_ERROR = "entered_in_error"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Patient(Base):
    """Core patient record.

    Stores demographic identifiers and a cached copy of the FHIR resource.
    Sensitive fields (first_name, last_name) should be encrypted at the
    application layer in production deployments.

    Attributes:
        external_id: Medical record number or FHIR Patient resource ID.
        fhir_data: Raw FHIR Patient JSON for reference / re-parsing.
        current_version: Monotonically-increasing version counter bumped
            on every material change (links to PatientVersion).
    """

    __tablename__ = "patients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id = Column(String(255), unique=True, nullable=True, index=True)
    first_name = Column(String(255), nullable=False)
    last_name = Column(String(255), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    sex = Column(String(20), nullable=False, default=SexEnum.UNKNOWN.value)
    race = Column(String(100), nullable=True)
    ethnicity = Column(String(100), nullable=True)
    preferred_language = Column(String(10), nullable=False, default="en")
    status = Column(String(20), nullable=False, default=PatientStatusEnum.ACTIVE.value)
    fhir_data = Column(JSONB, nullable=True)
    current_version = Column(Integer, nullable=False, default=1)

    # -- Relationships -------------------------------------------------------
    medical_events = relationship(
        "MedicalEvent",
        back_populates="patient",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    patient_versions = relationship(
        "PatientVersion",
        back_populates="patient",
        cascade="all, delete-orphan",
        order_by="PatientVersion.version_number",
    )
    match_results = relationship(
        "MatchResult",
        back_populates="patient",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<Patient(id={self.id!s}, "
            f"name={self.last_name}, {self.first_name}, "
            f"status={self.status})>"
        )


class MedicalEvent(Base):
    """A single clinical event in a patient's medical timeline.

    Each row represents a discrete event — a diagnosis, lab result,
    medication prescription, procedure, etc. — extracted from EHR data.
    The ``source_text`` field preserves the original sentence from
    clinical notes so downstream explainability can cite evidence.

    Attributes:
        code: Standardised clinical code (ICD-10, SNOMED-CT, LOINC, RxNorm).
        code_system: Identifies which coding system ``code`` belongs to.
        value / unit: For quantitative events (e.g. lab results).
        event_metadata: Catch-all JSONB column for structured extras that
            don't warrant their own column. Named ``event_metadata`` (not
            ``metadata``) because SQLAlchemy reserves that attribute name
            on declarative classes.
    """

    __tablename__ = "medical_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = Column(String(30), nullable=False)
    event_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=True)
    code = Column(String(50), nullable=True, index=True)
    code_system = Column(String(30), nullable=True)
    display_name = Column(String(500), nullable=False)
    value = Column(String(100), nullable=True)
    unit = Column(String(50), nullable=True)
    status = Column(String(30), nullable=False, default=EventStatusEnum.ACTIVE.value)
    source_text = Column(Text, nullable=True)
    source_document = Column(String(500), nullable=True)
    event_metadata = Column("metadata", JSONB, nullable=True)

    # -- Relationships -------------------------------------------------------
    patient = relationship("Patient", back_populates="medical_events")
    criterion_evaluations = relationship(
        "CriterionEvaluation",
        back_populates="evidence_event",
        foreign_keys="CriterionEvaluation.evidence_event_id",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<MedicalEvent(id={self.id!s}, "
            f"type={self.event_type}, "
            f"name={self.display_name!r})>"
        )


class PatientVersion(Base):
    """Immutable snapshot of patient data at a specific version.

    Created every time a material change is detected in the patient
    record.  Enables exact replay of historical match results and
    satisfies clinical-audit requirements.

    Attributes:
        snapshot_data: Full JSON representation of the patient at this
            version (demographics + events).
        change_summary: Human-readable description of what changed.
    """

    __tablename__ = "patient_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number = Column(Integer, nullable=False)
    snapshot_data = Column(JSONB, nullable=False)
    change_summary = Column(Text, nullable=True)
    changed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # -- Relationships -------------------------------------------------------
    patient = relationship("Patient", back_populates="patient_versions")

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<PatientVersion(id={self.id!s}, "
            f"patient_id={self.patient_id!s}, "
            f"v={self.version_number})>"
        )
