"""Pydantic schemas for patient resources.

These schemas validate request bodies and shape API responses for the
``/patients`` endpoint group. They mirror the SQLAlchemy ORM models in
:mod:`app.models.patient` but stay decoupled so the API layer can evolve
independently of the database schema.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.patient import (
    EventStatusEnum,
    EventTypeEnum,
    PatientStatusEnum,
    SexEnum,
)


# ---------------------------------------------------------------------------
# MedicalEvent
# ---------------------------------------------------------------------------

class MedicalEventBase(BaseModel):
    """Fields common to creating and reading a medical event."""

    event_type: EventTypeEnum
    event_date: datetime
    end_date: Optional[datetime] = None
    code: Optional[str] = Field(default=None, max_length=50)
    code_system: Optional[str] = Field(default=None, max_length=30)
    display_name: str = Field(..., max_length=500)
    value: Optional[str] = Field(default=None, max_length=100)
    unit: Optional[str] = Field(default=None, max_length=50)
    status: EventStatusEnum = EventStatusEnum.ACTIVE
    source_text: Optional[str] = None
    source_document: Optional[str] = Field(default=None, max_length=500)
    event_metadata: Optional[dict[str, Any]] = None


class MedicalEventCreate(MedicalEventBase):
    """Payload for adding a single medical event to a patient timeline."""


class MedicalEventRead(MedicalEventBase):
    """Medical event as returned by the API."""

    id: uuid.UUID
    patient_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Patient
# ---------------------------------------------------------------------------

class PatientBase(BaseModel):
    """Demographic fields shared across patient create / update / read."""

    external_id: Optional[str] = Field(default=None, max_length=255)
    first_name: str = Field(..., max_length=255)
    last_name: str = Field(..., max_length=255)
    date_of_birth: date
    sex: SexEnum = SexEnum.UNKNOWN
    race: Optional[str] = Field(default=None, max_length=100)
    ethnicity: Optional[str] = Field(default=None, max_length=100)
    preferred_language: str = Field(default="en", max_length=10)
    status: PatientStatusEnum = PatientStatusEnum.ACTIVE


class PatientCreate(PatientBase):
    """Payload for creating a new patient.

    ``fhir_data`` may be supplied to keep a raw copy of the originating
    FHIR Patient resource (parsed downstream by the EHR parser service).
    """

    fhir_data: Optional[dict[str, Any]] = None


class PatientUpdate(BaseModel):
    """Partial-update payload — every field is optional."""

    first_name: Optional[str] = Field(default=None, max_length=255)
    last_name: Optional[str] = Field(default=None, max_length=255)
    date_of_birth: Optional[date] = None
    sex: Optional[SexEnum] = None
    race: Optional[str] = Field(default=None, max_length=100)
    ethnicity: Optional[str] = Field(default=None, max_length=100)
    preferred_language: Optional[str] = Field(default=None, max_length=10)
    status: Optional[PatientStatusEnum] = None
    fhir_data: Optional[dict[str, Any]] = None


class PatientRead(PatientBase):
    """Patient resource as returned by the API."""

    id: uuid.UUID
    current_version: int
    fhir_data: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PatientDetailRead(PatientRead):
    """Patient resource with the full event timeline embedded."""

    medical_events: list[MedicalEventRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# FHIR ingestion
# ---------------------------------------------------------------------------

class FHIRBundleIngest(BaseModel):
    """Wrapper for a FHIR R4 Bundle being submitted for ingestion."""

    bundle: dict[str, Any] = Field(
        ...,
        description="A FHIR R4 Bundle JSON resource containing Patient + clinical events.",
    )


class IngestionResult(BaseModel):
    """Result returned after a FHIR Bundle has been ingested."""

    patient_id: uuid.UUID
    events_created: int
    events_updated: int
    events_skipped: int
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------

class PatientVersionRead(BaseModel):
    """A historical snapshot of a patient record."""

    id: uuid.UUID
    patient_id: uuid.UUID
    version_number: int
    snapshot_data: dict[str, Any]
    change_summary: Optional[str] = None
    changed_by: Optional[uuid.UUID] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
