"""Patient resource endpoints — ``/api/v1/patients``.

Routes
------
``GET    /``                       — list patients (paginated, coordinators+).
``POST   /``                       — create a patient (coordinators+).
``GET    /{patient_id}``           — full patient record + timeline.
``PATCH  /{patient_id}``           — partial update of demographics.
``DELETE /{patient_id}``           — soft-delete by flipping status to inactive.
``POST   /{patient_id}/fhir``      — ingest a FHIR R4 Bundle for an existing patient.
``POST   /fhir``                   — bootstrap-via-FHIR (create + ingest in one shot).
``GET    /{patient_id}/timeline``  — chronological MedicalEvent list.
``GET    /{patient_id}/versions``  — audit snapshots taken via :class:`PatientVersion`.

Access control
--------------
* Coordinators / clinicians / admins: full access.
* Patient role: read-only on their own ``associated_patient_id``.
* Sponsors: no access (the diversity / aggregate endpoints under
  ``/admin`` cover their use cases).
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import (
    ensure_can_access_patient,
    get_current_user,
    require_role,
)
from app.database import get_db
from app.models.audit import AuditAction
from app.models.patient import MedicalEvent, Patient, PatientStatusEnum, PatientVersion
from app.models.user import User, UserRole
from app.schemas.patient import (
    FHIRBundleIngest,
    IngestionResult,
    MedicalEventRead,
    PatientCreate,
    PatientDetailRead,
    PatientRead,
    PatientUpdate,
    PatientVersionRead,
)
from app.services.ehr_parser import ingest_bundle

router = APIRouter(prefix="/patients", tags=["Patients"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_patient_or_404(db: Session, patient_id: uuid.UUID) -> Patient:
    patient = db.get(
        Patient, patient_id, options=[selectinload(Patient.medical_events)]
    )
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Patient {patient_id} not found.",
        )
    return patient


# ---------------------------------------------------------------------------
# Listing + creation
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=list[PatientRead],
    summary="List patients (paginated).",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def list_patients(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[PatientStatusEnum] = Query(None, alias="status"),
) -> list[PatientRead]:
    stmt = select(Patient).order_by(Patient.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        stmt = stmt.where(Patient.status == status_filter.value)
    rows = db.execute(stmt).scalars().all()
    return [PatientRead.model_validate(p) for p in rows]


@router.post(
    "/",
    response_model=PatientRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new patient record.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def create_patient(
    payload: PatientCreate,
    db: Session = Depends(get_db),
) -> PatientRead:
    if payload.external_id:
        existing = db.execute(
            select(Patient).where(Patient.external_id == payload.external_id)
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A patient with external_id={payload.external_id!r} already exists."
                ),
            )

    patient = Patient(
        external_id=payload.external_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        date_of_birth=payload.date_of_birth,
        sex=payload.sex.value,
        race=payload.race,
        ethnicity=payload.ethnicity,
        preferred_language=payload.preferred_language,
        status=payload.status.value,
        fhir_data=payload.fhir_data,
        current_version=1,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return PatientRead.model_validate(patient)


# ---------------------------------------------------------------------------
# Per-patient detail + update
# ---------------------------------------------------------------------------

@router.get(
    "/{patient_id}",
    response_model=PatientDetailRead,
    summary="Return a patient + embedded timeline.",
)
def get_patient(
    patient_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PatientDetailRead:
    ensure_can_access_patient(current_user, patient_id)
    patient = _load_patient_or_404(db, patient_id)
    # ``selectinload`` already loaded events; sort here so the response is stable.
    sorted_events = sorted(patient.medical_events, key=lambda e: e.event_date)
    response = PatientDetailRead.model_validate(patient)
    response.medical_events = [MedicalEventRead.model_validate(e) for e in sorted_events]
    return response


@router.patch(
    "/{patient_id}",
    response_model=PatientRead,
    summary="Partial demographic update.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def update_patient(
    patient_id: uuid.UUID,
    payload: PatientUpdate,
    db: Session = Depends(get_db),
) -> PatientRead:
    patient = _load_patient_or_404(db, patient_id)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        if field == "sex" and value is not None:
            setattr(patient, "sex", value.value)
        elif field == "status" and value is not None:
            setattr(patient, "status", value.value)
        else:
            setattr(patient, field, value)
    db.commit()
    db.refresh(patient)
    return PatientRead.model_validate(patient)


@router.delete(
    "/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete (status → inactive).",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
def soft_delete_patient(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    patient = _load_patient_or_404(db, patient_id)
    patient.status = PatientStatusEnum.INACTIVE.value
    db.commit()


# ---------------------------------------------------------------------------
# FHIR ingestion
# ---------------------------------------------------------------------------

@router.post(
    "/{patient_id}/fhir",
    response_model=IngestionResult,
    summary="Ingest a FHIR R4 Bundle for an existing patient.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def ingest_fhir_for_existing(
    patient_id: uuid.UUID,
    payload: FHIRBundleIngest,
    db: Session = Depends(get_db),
) -> IngestionResult:
    """Add chart events from a FHIR Bundle to a known patient.

    The bundle's embedded Patient resource must resolve to the same row;
    we enforce this by checking the external_id (or refuse with 409).
    """
    patient = _load_patient_or_404(db, patient_id)
    stats = ingest_bundle(db, payload.bundle)
    if stats.patient_id != patient.id:
        # ingest_bundle created / matched a *different* patient — most
        # likely the bundle's Patient resource has the wrong identifier.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Bundle's embedded Patient resource resolved to a different "
                "row; verify the Patient.identifier matches the target."
            ),
        )
    return IngestionResult(
        patient_id=stats.patient_id,
        events_created=stats.events_created,
        events_updated=stats.events_updated,
        events_skipped=stats.events_skipped,
        warnings=stats.warnings,
    )


@router.post(
    "/fhir",
    response_model=IngestionResult,
    status_code=status.HTTP_201_CREATED,
    summary="Create a patient + ingest a FHIR Bundle in one call.",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def ingest_fhir_bootstrap(
    payload: FHIRBundleIngest,
    db: Session = Depends(get_db),
) -> IngestionResult:
    stats = ingest_bundle(db, payload.bundle)
    return IngestionResult(
        patient_id=stats.patient_id,
        events_created=stats.events_created,
        events_updated=stats.events_updated,
        events_skipped=stats.events_skipped,
        warnings=stats.warnings,
    )


# ---------------------------------------------------------------------------
# Timeline + version history
# ---------------------------------------------------------------------------

@router.get(
    "/{patient_id}/timeline",
    response_model=list[MedicalEventRead],
    summary="Chronological list of MedicalEvent rows.",
)
def patient_timeline(
    patient_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> list[MedicalEventRead]:
    ensure_can_access_patient(current_user, patient_id)
    # Existence check first — friendlier 404 than an empty list.
    _load_patient_or_404(db, patient_id)
    stmt = (
        select(MedicalEvent)
        .where(MedicalEvent.patient_id == patient_id)
        .order_by(MedicalEvent.event_date)
        .limit(limit)
        .offset(offset)
    )
    return [MedicalEventRead.model_validate(e) for e in db.execute(stmt).scalars()]


@router.get(
    "/{patient_id}/versions",
    response_model=list[PatientVersionRead],
    summary="Snapshot history (audit reconstruction).",
    dependencies=[
        Depends(require_role(UserRole.COORDINATOR, UserRole.CLINICIAN, UserRole.ADMIN))
    ],
)
def patient_versions(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> list[PatientVersionRead]:
    _load_patient_or_404(db, patient_id)
    stmt = (
        select(PatientVersion)
        .where(PatientVersion.patient_id == patient_id)
        .order_by(PatientVersion.version_number)
    )
    return [PatientVersionRead.model_validate(v) for v in db.execute(stmt).scalars()]
