"""Pydantic schemas for clinician feedback."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.feedback import FeedbackAction, OverrideStatus


class ClinicianFeedbackCreate(BaseModel):
    """Payload for submitting clinician feedback on a match or criterion.

    When ``action`` is ``OVERRIDDEN`` the ``override_status`` MUST be set;
    the validator below enforces that pairing.
    """

    match_result_id: uuid.UUID
    criterion_evaluation_id: Optional[uuid.UUID] = None
    action: FeedbackAction
    override_status: Optional[OverrideStatus] = None
    reason: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _require_override_status_when_overridden(self) -> "ClinicianFeedbackCreate":
        if self.action == FeedbackAction.OVERRIDDEN and self.override_status is None:
            raise ValueError(
                "override_status is required when action is 'overridden'."
            )
        if self.action != FeedbackAction.OVERRIDDEN and self.override_status is not None:
            raise ValueError(
                "override_status may only be set when action is 'overridden'."
            )
        return self


class ClinicianFeedbackRead(BaseModel):
    """Feedback record as returned by the API."""

    id: uuid.UUID
    match_result_id: uuid.UUID
    criterion_evaluation_id: Optional[uuid.UUID] = None
    user_id: uuid.UUID
    action: FeedbackAction
    override_status: Optional[OverrideStatus] = None
    reason: Optional[str] = None
    is_used_for_training: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FeedbackStatsResponse(BaseModel):
    """Aggregate stats over collected feedback."""

    total_feedbacks: int
    accepted: int
    rejected: int
    overridden: int
    deferred: int
    acceptance_rate: float = Field(..., ge=0.0, le=1.0)
    per_trial: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description="Map of trial_id (string) → per-action counts.",
    )
