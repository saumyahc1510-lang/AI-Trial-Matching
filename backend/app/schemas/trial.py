"""Pydantic schemas for clinical trial resources.

These schemas validate request bodies and shape API responses for the
``/trials`` endpoint group. The structured ``temporal_constraint`` and
``value_constraint`` JSON shapes are intentionally typed as ``dict`` —
the criteria parser service will populate them with arbitrary keys that
evolve as we learn what the LLM produces.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.trial import (
    CriterionCategoryEnum,
    CriterionTypeEnum,
    SiteStatusEnum,
)


# ---------------------------------------------------------------------------
# TrialCriterion
# ---------------------------------------------------------------------------

class TrialCriterionBase(BaseModel):
    """Common fields for inclusion / exclusion criterion records."""

    criterion_type: CriterionTypeEnum
    category: CriterionCategoryEnum = CriterionCategoryEnum.OTHER
    original_text: str
    parsed_description: Optional[str] = None
    temporal_constraint: Optional[dict[str, Any]] = None
    value_constraint: Optional[dict[str, Any]] = None
    is_critical: bool = True
    order_index: int = 0


class TrialCriterionCreate(TrialCriterionBase):
    """Payload for attaching a criterion to a trial."""


class TrialCriterionRead(TrialCriterionBase):
    """Criterion as returned by the API."""

    id: uuid.UUID
    trial_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# TrialSite
# ---------------------------------------------------------------------------

class TrialSiteBase(BaseModel):
    """Common fields for a trial site."""

    facility_name: str = Field(..., max_length=500)
    city: Optional[str] = Field(default=None, max_length=200)
    state: Optional[str] = Field(default=None, max_length=100)
    country: Optional[str] = Field(default=None, max_length=100)
    zip_code: Optional[str] = Field(default=None, max_length=20)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    site_status: SiteStatusEnum = SiteStatusEnum.RECRUITING
    contact_name: Optional[str] = Field(default=None, max_length=300)
    contact_email: Optional[str] = Field(default=None, max_length=300)
    contact_phone: Optional[str] = Field(default=None, max_length=50)


class TrialSiteCreate(TrialSiteBase):
    """Payload for adding a site to a trial."""


class TrialSiteRead(TrialSiteBase):
    """Trial site as returned by the API."""

    id: uuid.UUID
    trial_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# ClinicalTrial
# ---------------------------------------------------------------------------

class ClinicalTrialBase(BaseModel):
    """Common fields for trial create / read."""

    nct_id: str = Field(..., max_length=20)
    title: str
    brief_summary: Optional[str] = None
    phase: Optional[str] = Field(default=None, max_length=30)
    overall_status: str = Field(..., max_length=30)
    study_type: Optional[str] = Field(default=None, max_length=30)
    conditions: Optional[list[str]] = None
    interventions: Optional[list[dict[str, Any]]] = None
    sponsor: Optional[str] = Field(default=None, max_length=500)
    category: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Derived clinical specialty (see GET /trials/categories).",
    )
    enrollment_count: Optional[int] = None
    enrollment_demographics: Optional[dict[str, Any]] = None
    start_date: Optional[date] = None
    completion_date: Optional[date] = None
    raw_eligibility_text: Optional[str] = None
    source_url: Optional[str] = Field(default=None, max_length=500)
    is_manually_added: bool = False


class ClinicalTrialCreate(ClinicalTrialBase):
    """Payload for adding a trial manually (typically sponsor-supplied)."""


class ClinicalTrialUpdate(BaseModel):
    """Partial update — every field optional."""

    title: Optional[str] = None
    brief_summary: Optional[str] = None
    phase: Optional[str] = None
    overall_status: Optional[str] = None
    study_type: Optional[str] = None
    conditions: Optional[list[str]] = None
    interventions: Optional[list[dict[str, Any]]] = None
    sponsor: Optional[str] = None
    enrollment_count: Optional[int] = None
    enrollment_demographics: Optional[dict[str, Any]] = None
    start_date: Optional[date] = None
    completion_date: Optional[date] = None
    raw_eligibility_text: Optional[str] = None
    source_url: Optional[str] = None


class ClinicalTrialRead(ClinicalTrialBase):
    """Trial resource as returned by the API."""

    id: uuid.UUID
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ClinicalTrialDetailRead(ClinicalTrialRead):
    """Trial with all parsed criteria and sites embedded."""

    criteria: list[TrialCriterionRead] = Field(default_factory=list)
    sites: list[TrialSiteRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sync trigger
# ---------------------------------------------------------------------------

class TrialSyncRequest(BaseModel):
    """Trigger a manual sync from ClinicalTrials.gov."""

    conditions: Optional[list[str]] = Field(
        default=None,
        description=(
            "Override the configured TRIAL_SYNC_CONDITIONS list. "
            "When omitted, the configured defaults are used."
        ),
    )
    max_trials: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional cap on the number of trials fetched per condition.",
    )


class TrialSyncResult(BaseModel):
    """Summary returned after a sync run."""

    trials_fetched: int
    trials_created: int
    trials_updated: int
    trials_status_changed: int
    duration_seconds: float
    errors: list[str] = Field(default_factory=list)
