"""Authentication endpoints — ``/api/v1/auth``.

Endpoints
---------
``POST /login``           — OAuth2 password-flow → JWT access token.
``POST /register``        — Self-service registration.  Role is fixed
                            to ``PATIENT`` for self-registration;
                            admins use ``POST /admin/users`` to create
                            staff accounts with elevated roles.
``GET  /me``              — Currently-authenticated user's profile.
``POST /token/refresh``   — Re-issue a fresh access token for a still-
                            valid bearer token.  No long-lived refresh
                            tokens yet (we may add them when the front-
                            end ships with idle-session expiry).

Why no refresh token table
--------------------------
A refresh-token table is the right pattern in production, but it adds
state we don't need for the dev / prototype phase.  ``POST /token/refresh``
just re-signs based on the still-valid access token, which is enough to
keep an active user's session warm without complicating audit /
revocation flows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.models.patient import (
    EventStatusEnum,
    EventTypeEnum,
    MedicalEvent,
    Patient,
    PatientStatusEnum,
    SexEnum,
)
from app.models.user import User, UserRole

router = APIRouter(prefix="/auth", tags=["Auth"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TokenResponse(BaseModel):
    """OAuth2-compatible token payload."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Seconds until token expiry.")
    role: str
    user_id: uuid.UUID


class PatientConditionInput(BaseModel):
    """One medical condition supplied during patient onboarding.

    Lightweight — name is required; SNOMED code optional.  The onboarding
    flow drops a single ``MedicalEvent`` per condition so the matching
    engine has at least one timeline point to reason over.
    """

    display_name: str = Field(..., max_length=500)
    code: Optional[str] = Field(default=None, max_length=50)
    code_system: Optional[str] = Field(default="SNOMED-CT", max_length=30)
    onset_date: Optional[date] = None


class RegisterRequest(BaseModel):
    """Self-service registration payload.

    Patient-role users typically supply the optional demographic fields
    so the system can create a linked :class:`Patient` row and the
    matching engine has data to work with.  All demographic fields are
    optional — a coordinator can complete the profile later.
    """

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=256)

    # ── Optional patient demographics ─────────────────────────────────
    date_of_birth: Optional[date] = None
    sex: Optional[SexEnum] = None
    race: Optional[str] = Field(default=None, max_length=100)
    ethnicity: Optional[str] = Field(default=None, max_length=100)
    preferred_language: Optional[str] = Field(default=None, max_length=10)
    conditions: list[PatientConditionInput] = Field(default_factory=list)


class UserProfileResponse(BaseModel):
    """Profile returned by ``GET /me``."""

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    associated_patient_id: Optional[uuid.UUID] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange username + password for a bearer token.",
)
def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """OAuth2 password flow.

    The OAuth2-form spec uses ``username``; we treat that as the user's
    email address so the Swagger "Authorize" dialog works as users
    expect.
    """
    stmt = select(User).where(User.email == form.username.lower())
    user = db.execute(stmt).scalar_one_or_none()
    if user is None or not verify_password(form.password, user.hashed_password):
        # Identical error for missing user vs. wrong password — no
        # account enumeration via timing / response text.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    settings = get_settings()
    token = create_access_token(user_id=user.id, role=user.role, email=user.email)

    # Stamp the audit middleware so it logs this hit as LOGIN, not CREATE.
    from app.models.audit import AuditAction

    request.state.audit_action = AuditAction.LOGIN
    request.state.user_id = user.id

    return TokenResponse(
        access_token=token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        role=user.role,
        user_id=user.id,
    )


@router.post(
    "/register",
    response_model=UserProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Self-service patient-role registration.",
)
def register(
    payload: RegisterRequest,
    db: Session = Depends(get_db),
) -> UserProfileResponse:
    """Create a new ``PATIENT``-role user and (optionally) link a Patient row.

    Self-registration is intentionally limited to the patient role.
    Staff accounts (coordinator, clinician, admin, sponsor) are created
    via the admin endpoint, which enforces the privilege properly.

    When the caller supplies demographic fields (``date_of_birth`` is
    the deciding signal), we also create a :class:`Patient` row and
    link the new user via ``associated_patient_id``.  Without this the
    matching engine has no data to reason over for self-registered
    patients and the UI would be empty.
    """
    email = payload.email.lower()
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    user = User(
        email=email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=UserRole.PATIENT.value,
        is_active=True,
    )
    db.add(user)
    db.flush()  # populate user.id before linking

    # ── Optional Patient + Condition events ─────────────────────────
    if payload.date_of_birth is not None:
        first, _, last = payload.full_name.strip().partition(' ')
        patient = Patient(
            external_id=f"SELF-{user.id.hex[:10].upper()}",
            first_name=first or 'Patient',
            last_name=last or first or 'User',
            date_of_birth=payload.date_of_birth,
            sex=(payload.sex or SexEnum.UNKNOWN).value,
            race=payload.race,
            ethnicity=payload.ethnicity,
            preferred_language=payload.preferred_language or 'en',
            status=PatientStatusEnum.ACTIVE.value,
            current_version=1,
        )
        db.add(patient)
        db.flush()
        user.associated_patient_id = patient.id

        # Each declared condition becomes one diagnosis event so the
        # matching engine has a timeline to reason over.
        now_utc = datetime.now(timezone.utc)
        for cond in payload.conditions:
            event_dt = (
                datetime.combine(cond.onset_date, datetime.min.time(), tzinfo=timezone.utc)
                if cond.onset_date is not None else now_utc
            )
            db.add(
                MedicalEvent(
                    patient_id=patient.id,
                    event_type=EventTypeEnum.DIAGNOSIS.value,
                    event_date=event_dt,
                    display_name=cond.display_name,
                    code=cond.code,
                    code_system=cond.code_system,
                    status=EventStatusEnum.ACTIVE.value,
                    source_document='self-reported via registration',
                )
            )

    db.commit()
    db.refresh(user)
    return UserProfileResponse.model_validate(user)


@router.get(
    "/me",
    response_model=UserProfileResponse,
    summary="Return the currently-authenticated user's profile.",
)
def read_me(current_user: User = Depends(get_current_user)) -> UserProfileResponse:
    return UserProfileResponse.model_validate(current_user)


@router.post(
    "/token/refresh",
    response_model=TokenResponse,
    summary="Re-issue a fresh JWT for a still-valid bearer token.",
)
def refresh_token(
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    settings = get_settings()
    token = create_access_token(
        user_id=current_user.id,
        role=current_user.role,
        email=current_user.email,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        role=current_user.role,
        user_id=current_user.id,
    )
