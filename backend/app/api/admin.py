"""Admin endpoints — ``/api/v1/admin``.

Admin-only — full RBAC enforcement at the router level.

Routes
------
``POST  /users``                    — create a staff user with elevated role.
``GET   /users``                    — list users.
``PATCH /users/{user_id}``          — change role / activation.
``POST  /trial-sync``               — trigger an on-demand CT.gov sync.
``GET   /config``                   — non-secret runtime configuration.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    hash_password,
    require_role,
)
from app.config import get_settings
from app.database import get_db
from app.models.patient import Patient, PatientStatusEnum
from app.models.user import User, UserRole
from app.services.sync_jobs import registry as sync_job_registry
from app.workers.trial_sync_worker import run_manual_sync_task

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

class AdminUserCreate(BaseModel):
    """Admin-supplied user creation payload."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=256)
    role: UserRole
    associated_patient_id: Optional[uuid.UUID] = None


class AdminUserUpdate(BaseModel):
    """Admin can change role, deactivate, or rename users."""

    full_name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None


class AdminUserRead(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    associated_patient_id: Optional[uuid.UUID] = None

    model_config = ConfigDict(from_attributes=True)


class AdminUserList(BaseModel):
    """One page of users plus the total matching the active filter."""

    items: list[AdminUserRead]
    total: int
    limit: int
    offset: int


class AdminUserStats(BaseModel):
    """System-wide user counters for the dashboard stat cards.

    Independent of the listing's page / filter so the cards always
    summarise the whole system rather than the current page.
    """

    total: int
    active: int
    admins: int


@router.post(
    "/users",
    response_model=AdminUserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a staff user.",
)
def create_user(
    payload: AdminUserCreate,
    db: Session = Depends(get_db),
) -> AdminUserRead:
    email = payload.email.lower()
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    if payload.role == UserRole.PATIENT and payload.associated_patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="associated_patient_id is required for patient-role users.",
        )

    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role.value,
        is_active=True,
        associated_patient_id=payload.associated_patient_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return AdminUserRead.model_validate(user)


@router.get(
    "/users/stats",
    response_model=AdminUserStats,
    summary="System-wide user counts (total, active, admins).",
)
def user_stats(db: Session = Depends(get_db)) -> AdminUserStats:
    total = db.execute(select(sa.func.count()).select_from(User)).scalar_one()
    active = db.execute(
        select(sa.func.count()).select_from(User).where(User.is_active.is_(True))
    ).scalar_one()
    admins = db.execute(
        select(sa.func.count())
        .select_from(User)
        .where(User.role == UserRole.ADMIN.value)
    ).scalar_one()
    return AdminUserStats(total=total, active=active, admins=admins)


@router.get(
    "/users",
    response_model=AdminUserList,
    summary="List users (paginated, filterable).",
)
def list_users(
    db: Session = Depends(get_db),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    role: Optional[UserRole] = Query(None),
    is_active: Optional[bool] = Query(None),
    q: Optional[str] = Query(
        None, description="Case-insensitive match on full name or email."
    ),
) -> AdminUserList:
    filters = []
    if role is not None:
        filters.append(User.role == role.value)
    if is_active is not None:
        filters.append(User.is_active.is_(is_active))
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(sa.or_(User.full_name.ilike(like), User.email.ilike(like)))

    total = db.execute(
        select(sa.func.count()).select_from(User).where(*filters)
    ).scalar_one()
    rows = (
        db.execute(
            select(User)
            .where(*filters)
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return AdminUserList(
        items=[AdminUserRead.model_validate(u) for u in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch(
    "/users/{user_id}",
    response_model=AdminUserRead,
    summary="Update role, name, or activation state.",
)
def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    db: Session = Depends(get_db),
) -> AdminUserRead:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found.",
        )
    updates = payload.model_dump(exclude_unset=True)
    if "role" in updates and updates["role"] is not None:
        updates["role"] = updates["role"].value

    # Resolve the post-update state so we can validate invariants before
    # mutating the row.
    new_role = updates.get("role", user.role)
    new_active = updates.get("is_active", user.is_active)

    # Guard 1: the patient role is only coherent when the account is linked
    # to a patient record.  ``create_user`` enforces this on creation; the
    # edit path must too, or we end up with a patient-role login that can
    # never resolve its own chart.
    if new_role == UserRole.PATIENT.value and user.associated_patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Cannot assign the patient role to a user without an "
                "associated_patient_id."
            ),
        )

    # Guard 2: never let the last active admin be demoted or deactivated —
    # that would lock everyone out of the console.  Only relevant when the
    # target is currently an active admin and the edit drops that status.
    is_active_admin_now = user.role == UserRole.ADMIN.value and user.is_active
    still_active_admin = new_role == UserRole.ADMIN.value and new_active
    if is_active_admin_now and not still_active_admin:
        other_active_admins = db.execute(
            select(sa.func.count())
            .select_from(User)
            .where(
                User.role == UserRole.ADMIN.value,
                User.is_active.is_(True),
                User.id != user.id,
            )
        ).scalar_one()
        if other_active_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot demote or deactivate the last active admin.",
            )

    for field, value in updates.items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return AdminUserRead.model_validate(user)


# ---------------------------------------------------------------------------
# Patients (admin lifecycle management)
# ---------------------------------------------------------------------------
#
# The patient resource router (``/api/v1/patients``) only soft-deletes
# (status → inactive).  Admins occasionally need to *hard* delete — e.g.
# clearing demo / test records to start fresh.  These routes provide a
# listing, system counts, single hard-delete, and a guarded bulk purge.
# Hard deletes cascade at the DB level (medical events, versions, match
# results, evaluations, flags) and SET NULL on linked users / notifications.

class AdminPatientRead(BaseModel):
    id: uuid.UUID
    external_id: Optional[str] = None
    first_name: str
    last_name: str
    date_of_birth: date
    sex: str
    status: str
    current_version: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminPatientList(BaseModel):
    items: list[AdminPatientRead]
    total: int
    limit: int
    offset: int


class AdminPatientStats(BaseModel):
    total: int
    active: int
    inactive: int
    deceased: int


class PatientPurgeRequest(BaseModel):
    """Bulk hard-delete request.

    ``confirm`` must equal the literal ``"DELETE"`` — a deliberate guard so
    a stray call can't wipe every patient.  ``status`` optionally narrows
    the purge to one lifecycle state (e.g. only ``inactive`` records).
    """

    confirm: str = Field(..., description="Must equal 'DELETE' to proceed.")
    status: Optional[PatientStatusEnum] = Field(
        default=None,
        description="Restrict the purge to one status; omit to purge all.",
    )


class PatientPurgeResult(BaseModel):
    deleted: int


@router.get(
    "/patients/stats",
    response_model=AdminPatientStats,
    summary="Patient counts by lifecycle status.",
)
def patient_stats(db: Session = Depends(get_db)) -> AdminPatientStats:
    def _count(*where) -> int:
        return db.execute(
            select(sa.func.count()).select_from(Patient).where(*where)
        ).scalar_one()

    return AdminPatientStats(
        total=_count(),
        active=_count(Patient.status == PatientStatusEnum.ACTIVE.value),
        inactive=_count(Patient.status == PatientStatusEnum.INACTIVE.value),
        deceased=_count(Patient.status == PatientStatusEnum.DECEASED.value),
    )


@router.get(
    "/patients",
    response_model=AdminPatientList,
    summary="List patients (paginated, filterable).",
)
def list_patients_admin(
    db: Session = Depends(get_db),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[PatientStatusEnum] = Query(None, alias="status"),
    q: Optional[str] = Query(
        None, description="Case-insensitive match on name or external id."
    ),
) -> AdminPatientList:
    filters = []
    if status_filter is not None:
        filters.append(Patient.status == status_filter.value)
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            sa.or_(
                Patient.first_name.ilike(like),
                Patient.last_name.ilike(like),
                Patient.external_id.ilike(like),
            )
        )

    total = db.execute(
        select(sa.func.count()).select_from(Patient).where(*filters)
    ).scalar_one()
    rows = (
        db.execute(
            select(Patient)
            .where(*filters)
            .order_by(Patient.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return AdminPatientList(
        items=[AdminPatientRead.model_validate(p) for p in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.delete(
    "/patients/{patient_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hard-delete one patient and all of its records.",
)
def hard_delete_patient(
    patient_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Patient {patient_id} not found.",
        )
    # ORM delete walks the configured cascades (events, versions, matches).
    db.delete(patient)
    db.commit()


@router.post(
    "/patients/purge",
    response_model=PatientPurgeResult,
    summary="Bulk hard-delete patients (guarded by a confirm token).",
)
def purge_patients(
    payload: PatientPurgeRequest,
    db: Session = Depends(get_db),
) -> PatientPurgeResult:
    if payload.confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Confirmation failed: 'confirm' must equal 'DELETE'.",
        )

    where = []
    if payload.status is not None:
        where.append(Patient.status == payload.status.value)

    # Bulk Core DELETE — relies on the DB-level ON DELETE CASCADE / SET NULL
    # foreign keys so children are cleaned up without loading every row.
    result = db.execute(
        sa.delete(Patient).where(*where) if where else sa.delete(Patient)
    )
    db.commit()
    return PatientPurgeResult(deleted=result.rowcount or 0)


# ---------------------------------------------------------------------------
# Trial sync
# ---------------------------------------------------------------------------

class TrialSyncTriggerRequest(BaseModel):
    """Admin-supplied trial-sync trigger parameters."""

    conditions: Optional[list[str]] = Field(
        default=None,
        description=(
            "Override TRIAL_SYNC_CONDITIONS for this run.  When omitted, the "
            "configured defaults are used.  Ignored when ``fetch_all`` is True."
        ),
    )
    max_trials_per_condition: Optional[int] = Field(
        default=None, ge=1, le=5000,
        description=(
            "Hard cap per condition (or catalog-wide when fetch_all=True).  "
            "Bumped to 5000 to support the catalog-wide sync mode."
        ),
    )
    parse_criteria: bool = True
    rematch_active_patients: bool = True
    fetch_all: bool = Field(
        default=False,
        description=(
            "Drop the condition filter entirely and pull every recruiting "
            "trial from ClinicalTrials.gov.  Mutually exclusive with "
            "``conditions``."
        ),
    )


class SyncJobAccepted(BaseModel):
    """Returned when a sync job is accepted for background execution."""

    job_id: str
    status: str


@router.post(
    "/trial-sync",
    response_model=SyncJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Kick off a manual CT.gov trial sync (runs in the background).",
)
def trigger_trial_sync(
    payload: TrialSyncTriggerRequest = Body(default_factory=TrialSyncTriggerRequest),
) -> SyncJobAccepted:
    """Submit a sync and return immediately with a job handle.

    The work runs on a background thread so the request doesn't block for
    the (potentially 30-minute) catalog-wide sync.  Poll
    ``GET /admin/trial-sync/{job_id}`` for progress and the final result.

    Only one sync runs at a time — a second request while one is in flight
    is rejected with 409 so concurrent catalog pulls can't stampede CT.gov
    or the LLM criteria parser.
    """
    if sync_job_registry.active_count() > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A trial sync is already running.  Wait for it to finish.",
        )

    job = sync_job_registry.create()
    kwargs = dict(
        conditions=payload.conditions,
        max_trials_per_condition=payload.max_trials_per_condition,
        parse_criteria=payload.parse_criteria,
        rematch_active_patients=payload.rematch_active_patients,
        fetch_all=payload.fetch_all,
    )
    # Run via the Celery task so production (USE_CELERY=true) still routes
    # through the broker; the daemon thread does the waiting instead of the
    # request handler.  In eager mode the task body executes inline on the
    # thread and ``.get()`` returns immediately.
    sync_job_registry.run_in_background(
        job,
        lambda: run_manual_sync_task.delay(**kwargs).get(timeout=1800),
    )
    return SyncJobAccepted(job_id=job.id, status=job.status)


@router.get(
    "/trial-sync/{job_id}",
    summary="Poll the status / result of a background trial-sync job.",
)
def get_trial_sync_status(job_id: str) -> dict:
    job = sync_job_registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sync job not found or expired.",
        )
    return job.to_dict()


# ---------------------------------------------------------------------------
# Config introspection
# ---------------------------------------------------------------------------

class LLMUsageBreakdown(BaseModel):
    label: str
    calls: int
    tokens: int


class LLMDailyPoint(BaseModel):
    date: str          # ISO YYYY-MM-DD (UTC)
    tokens: int
    calls: int


class LLMUsageSummary(BaseModel):
    """Aggregate LLM-call telemetry for the admin dashboard."""

    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    success_rate: float          # 0..1
    avg_latency_ms: Optional[float]
    calls_last_7d: int
    tokens_last_7d: int
    last_call_at: Optional[datetime]
    per_model:     list[LLMUsageBreakdown]
    per_operation: list[LLMUsageBreakdown]
    daily: list[LLMDailyPoint]   # last 7 days, oldest first


@router.get(
    "/usage",
    response_model=LLMUsageSummary,
    summary="Aggregate LLM-call telemetry (tokens, latency, success rate).",
)
def llm_usage(
    db: Session = Depends(get_db),
) -> LLMUsageSummary:
    """Roll up :class:`~app.models.llm_usage.LLMUsage` rows.

    Returns:
        Aggregate metrics suitable for the admin dashboard's "LLM usage"
        card — totals, success rate, average latency, per-model +
        per-operation breakdowns, and a 7-day daily series for the
        sparkline chart.
    """
    from sqlalchemy import func
    from datetime import timedelta, timezone
    from app.models.llm_usage import LLMUsage

    # Totals over the whole table.
    totals_row = db.execute(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
            func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
            func.avg(LLMUsage.latency_ms),
            func.sum(
                # success boolean → 1/0 for rate calc
                func.cast(LLMUsage.success, sa.Integer)
            ),
            func.max(LLMUsage.created_at),
        )
    ).one()
    total_calls, total_prompt, total_completion, total_tokens, avg_latency, success_count, last_call_at = totals_row
    success_rate = (float(success_count) / float(total_calls)) if total_calls else 0.0

    # Last 7 days totals
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    last_7d_row = db.execute(
        select(
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        ).where(LLMUsage.created_at >= seven_days_ago)
    ).one()
    calls_7d, tokens_7d = last_7d_row

    # Per-model + per-operation breakdowns.
    per_model_rows = db.execute(
        select(
            LLMUsage.model,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        )
        .group_by(LLMUsage.model)
        .order_by(func.count(LLMUsage.id).desc())
        .limit(8)
    ).all()
    per_op_rows = db.execute(
        select(
            LLMUsage.operation,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        )
        .group_by(LLMUsage.operation)
        .order_by(func.count(LLMUsage.id).desc())
        .limit(8)
    ).all()

    # 7-day daily series.  Bucket explicitly in UTC — ``date_trunc`` on a
    # timestamptz otherwise uses the DB session's TimeZone, which would
    # disagree with the frontend's UTC day keys and shift the chart by a
    # day for any non-UTC deployment.  ``timezone('UTC', ts)`` yields the
    # UTC wall-clock as a naive timestamp, so the truncated ``.date()`` is
    # an unambiguous UTC calendar day.
    daily_rows = db.execute(
        select(
            func.date_trunc(
                'day', func.timezone('UTC', LLMUsage.created_at)
            ).label('day'),
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.total_tokens), 0),
        )
        .where(LLMUsage.created_at >= seven_days_ago)
        .group_by('day')
        .order_by('day')
    ).all()
    daily = [
        LLMDailyPoint(
            date=row[0].date().isoformat(),
            calls=int(row[1] or 0),
            tokens=int(row[2] or 0),
        )
        for row in daily_rows
    ]

    return LLMUsageSummary(
        total_calls=int(total_calls or 0),
        total_prompt_tokens=int(total_prompt or 0),
        total_completion_tokens=int(total_completion or 0),
        total_tokens=int(total_tokens or 0),
        success_rate=success_rate,
        avg_latency_ms=float(avg_latency) if avg_latency is not None else None,
        calls_last_7d=int(calls_7d or 0),
        tokens_last_7d=int(tokens_7d or 0),
        last_call_at=last_call_at,
        per_model=[
            LLMUsageBreakdown(label=name or 'unknown', calls=int(calls), tokens=int(tokens))
            for name, calls, tokens in per_model_rows
        ],
        per_operation=[
            LLMUsageBreakdown(label=op or 'unknown', calls=int(calls), tokens=int(tokens))
            for op, calls, tokens in per_op_rows
        ],
        daily=daily,
    )


class ConfigUpdate(BaseModel):
    """Runtime-editable settings.  Only the parse knobs are exposed; the
    rest of ``/config`` is read-only (lives in ``.env``)."""

    trial_parse_categories: Optional[list[str]] = Field(
        default=None,
        description="Categories eligible for LLM criteria parsing.  Empty list = all.",
    )
    trial_parse_max_per_sync: Optional[int] = Field(
        default=None, ge=0, le=500,
        description="Max trials parsed per sync run.  0 = no cap.",
    )


@router.get(
    "/config",
    summary="Return non-secret runtime configuration.",
)
def read_config() -> dict:
    from app.services import runtime_config
    from app.services.trial_category import all_categories

    settings = get_settings()
    return {
        "app_name": settings.APP_NAME,
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "use_celery": settings.USE_CELERY,
        "trial_sync_conditions": settings.trial_sync_conditions,
        "trial_sync_interval_hours": settings.TRIAL_SYNC_INTERVAL_HOURS,
        # Effective (override-aware) values — these reflect live edits.
        "trial_parse_categories": runtime_config.parse_categories(),
        "trial_parse_max_per_sync": runtime_config.parse_max_per_sync(),
        # The full category vocabulary so the dashboard can render an editor.
        "available_categories": all_categories(),
        "ner_model_name": settings.NER_MODEL_NAME,
        "diversity_rank_alpha": settings.DIVERSITY_RANK_ALPHA,
        "diversity_rank_beta": settings.DIVERSITY_RANK_BETA,
        "enable_phi_deidentification": settings.ENABLE_PHI_DEIDENTIFICATION,
        "audit_log_enabled": settings.AUDIT_LOG_ENABLED,
    }


@router.patch(
    "/config",
    summary="Update runtime-editable settings (parse categories / cap).",
)
def update_config(payload: ConfigUpdate) -> dict:
    from app.services import runtime_config
    from app.services.trial_category import all_categories

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No editable fields supplied.",
        )

    cats = updates.get("trial_parse_categories")
    if cats is not None:
        valid = set(all_categories())
        unknown = [c for c in cats if c not in valid]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown categories: {unknown}.",
            )

    runtime_config.set_overrides(updates)
    return read_config()
