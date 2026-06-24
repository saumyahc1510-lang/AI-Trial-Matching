"""Trial catalog endpoints — ``/api/v1/trials``.

Routes
------
``GET    /``                       — list trials (filterable by status, condition).
``GET    /{trial_id}``             — full trial + criteria + sites.
``POST   /``                       — manually add a trial (sponsor-supplied).
``PATCH  /{trial_id}``             — admin-only status / metadata edits.
``GET    /{trial_id}/criteria``    — parsed inclusion / exclusion criteria.
``GET    /{trial_id}/sites``       — trial sites with status.
``GET    /{trial_id}/summary``     — plain-language patient summary
                                     (optionally translated).

Trial sync is *not* here — it's an admin operation under
``/api/v1/admin/trial-sync`` because it touches global state.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import get_current_user, require_role
from app.database import get_db
from app.models.trial import ClinicalTrial, TrialCriterion, TrialSite
from app.models.user import User, UserRole
from app.schemas.trial import (
    ClinicalTrialCreate,
    ClinicalTrialDetailRead,
    ClinicalTrialRead,
    ClinicalTrialUpdate,
    TrialCriterionRead,
    TrialSiteRead,
)
from app.services.plain_language import (
    SUPPORTED_LANGUAGES,
    summarise_trial,
)
from app.services.trial_category import all_categories

router = APIRouter(prefix="/trials", tags=["Trials"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class TrialSummaryResponse(BaseModel):
    """Plain-language summary (and optional translation) for a trial."""

    trial_id: uuid.UUID
    nct_id: str
    language: str = Field(..., description="ISO 639-1 code of the returned summary.")
    text: str
    english_text: str
    from_cache: bool = Field(
        ..., description="True when the summary was served from cache."
    )

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_trial_or_404(db: Session, trial_id: uuid.UUID, eager: bool = False) -> ClinicalTrial:
    stmt = select(ClinicalTrial).where(ClinicalTrial.id == trial_id)
    if eager:
        stmt = stmt.options(
            selectinload(ClinicalTrial.criteria),
            selectinload(ClinicalTrial.sites),
        )
    trial = db.execute(stmt).scalar_one_or_none()
    if trial is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trial {trial_id} not found.",
        )
    return trial


# ---------------------------------------------------------------------------
# Listing + detail
# ---------------------------------------------------------------------------

@router.get(
    "/",
    response_model=list[ClinicalTrialRead],
    summary="List trials in the catalog (paginated, filterable).",
)
def list_trials(
    db: Session = Depends(get_db),
    overall_status: Optional[str] = Query(None, description="Filter by overall_status."),
    condition: Optional[str] = Query(None, description="Case-insensitive substring match."),
    category: Optional[str] = Query(
        None,
        description=(
            "Restrict to one top-level clinical specialty.  Use "
            "``GET /trials/categories`` to enumerate the canonical list."
        ),
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _user: User = Depends(get_current_user),
) -> list[ClinicalTrialRead]:
    stmt = select(ClinicalTrial).order_by(ClinicalTrial.last_synced_at.desc().nullslast())
    if overall_status:
        stmt = stmt.where(ClinicalTrial.overall_status == overall_status)
    if condition:
        # JSONB containment via a contains-any check — use string ILIKE on
        # the title as a portable fallback since the conditions column
        # is JSONB[].
        stmt = stmt.where(ClinicalTrial.title.ilike(f"%{condition}%"))
    if category:
        stmt = stmt.where(ClinicalTrial.category == category)
    stmt = stmt.limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [ClinicalTrialRead.model_validate(t) for t in rows]


class CategoryInfo(BaseModel):
    """One entry in the category-list response."""

    name: str
    trial_count: int


@router.get(
    "/categories",
    response_model=list[CategoryInfo],
    summary="Enumerate every category + the trial count per category.",
)
def list_categories(
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[CategoryInfo]:
    """Return the canonical category list plus a per-category trial count.

    The UI dropdown uses this so it can both label and badge-count each
    option — categories with zero trials still appear (they're part of
    the canonical taxonomy) but render with ``trial_count=0`` so the UI
    can grey them out.
    """
    from sqlalchemy import func

    counts_rows = db.execute(
        select(ClinicalTrial.category, func.count(ClinicalTrial.id))
        .where(ClinicalTrial.category.is_not(None))
        .group_by(ClinicalTrial.category)
    ).all()
    counts = {name: count for name, count in counts_rows}

    out: list[CategoryInfo] = []
    seen: set[str] = set()
    for name in all_categories():
        out.append(CategoryInfo(name=name, trial_count=counts.get(name, 0)))
        seen.add(name)
    # Trials may carry a non-canonical category (e.g. if the rules ship
    # an update and existing rows haven't been re-classified yet).
    # Surface those at the end so they're filterable.
    for name, c in counts.items():
        if name and name not in seen:
            out.append(CategoryInfo(name=name, trial_count=c))
    return out


@router.get(
    "/{trial_id}",
    response_model=ClinicalTrialDetailRead,
    summary="Return a trial + criteria + sites.",
)
def get_trial(
    trial_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> ClinicalTrialDetailRead:
    trial = _load_trial_or_404(db, trial_id, eager=True)
    response = ClinicalTrialDetailRead.model_validate(trial)
    response.criteria = [TrialCriterionRead.model_validate(c) for c in sorted(trial.criteria, key=lambda c: c.order_index)]
    response.sites = [TrialSiteRead.model_validate(s) for s in trial.sites]
    return response


# ---------------------------------------------------------------------------
# Sponsor-supplied trial
# ---------------------------------------------------------------------------

@router.post(
    "/",
    response_model=ClinicalTrialRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add a trial manually (sponsor / admin).",
    dependencies=[Depends(require_role(UserRole.ADMIN, UserRole.SPONSOR))],
)
def create_trial(
    payload: ClinicalTrialCreate,
    db: Session = Depends(get_db),
) -> ClinicalTrialRead:
    existing = db.execute(
        select(ClinicalTrial).where(ClinicalTrial.nct_id == payload.nct_id)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A trial with nct_id={payload.nct_id!r} already exists.",
        )
    trial = ClinicalTrial(
        nct_id=payload.nct_id,
        title=payload.title,
        brief_summary=payload.brief_summary,
        phase=payload.phase,
        overall_status=payload.overall_status,
        study_type=payload.study_type,
        conditions=payload.conditions,
        interventions=payload.interventions,
        sponsor=payload.sponsor,
        enrollment_count=payload.enrollment_count,
        enrollment_demographics=payload.enrollment_demographics,
        start_date=payload.start_date,
        completion_date=payload.completion_date,
        raw_eligibility_text=payload.raw_eligibility_text,
        source_url=payload.source_url,
        is_manually_added=True,
    )
    db.add(trial)
    db.commit()
    db.refresh(trial)
    return ClinicalTrialRead.model_validate(trial)


@router.patch(
    "/{trial_id}",
    response_model=ClinicalTrialRead,
    summary="Admin-only metadata / status edits.",
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
def update_trial(
    trial_id: uuid.UUID,
    payload: ClinicalTrialUpdate,
    db: Session = Depends(get_db),
) -> ClinicalTrialRead:
    trial = _load_trial_or_404(db, trial_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(trial, field, value)
    db.commit()
    db.refresh(trial)
    return ClinicalTrialRead.model_validate(trial)


# ---------------------------------------------------------------------------
# Sub-resources
# ---------------------------------------------------------------------------

@router.get(
    "/{trial_id}/criteria",
    response_model=list[TrialCriterionRead],
    summary="Parsed eligibility criteria.",
)
def list_criteria(
    trial_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[TrialCriterionRead]:
    _load_trial_or_404(db, trial_id)
    rows = (
        db.execute(
            select(TrialCriterion)
            .where(TrialCriterion.trial_id == trial_id)
            .order_by(TrialCriterion.order_index)
        )
        .scalars()
    )
    return [TrialCriterionRead.model_validate(c) for c in rows]


@router.get(
    "/{trial_id}/sites",
    response_model=list[TrialSiteRead],
    summary="Trial site list with per-site recruitment status.",
)
def list_sites(
    trial_id: uuid.UUID,
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> list[TrialSiteRead]:
    _load_trial_or_404(db, trial_id)
    rows = (
        db.execute(
            select(TrialSite)
            .where(TrialSite.trial_id == trial_id)
            .order_by(TrialSite.facility_name)
        )
        .scalars()
    )
    return [TrialSiteRead.model_validate(s) for s in rows]


# ---------------------------------------------------------------------------
# Plain-language summary (with translation)
# ---------------------------------------------------------------------------

@router.get(
    "/{trial_id}/summary",
    response_model=TrialSummaryResponse,
    summary="Plain-language summary, optionally translated to the patient's language.",
)
def trial_summary(
    trial_id: uuid.UUID,
    language: Optional[str] = Query(
        None,
        description=(
            f"ISO 639-1 language code (defaults to English). "
            f"Supported: {', '.join(SUPPORTED_LANGUAGES)}."
        ),
    ),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
) -> TrialSummaryResponse:
    trial = _load_trial_or_404(db, trial_id)
    summary = summarise_trial(trial, language=language)
    db.commit()  # persist cache update
    return TrialSummaryResponse(
        trial_id=trial.id,
        nct_id=trial.nct_id,
        language=summary.language,
        text=summary.text,
        english_text=summary.english_text,
        from_cache=summary.from_cache,
    )
