"""Pydantic schemas for API request/response validation.

Re-exports the most commonly used schemas so callers can do::

    from app.schemas import PatientCreate, MatchResultRead
"""

from app.schemas.feedback import (
    ClinicianFeedbackCreate,
    ClinicianFeedbackRead,
    FeedbackStatsResponse,
)
from app.schemas.matching import (
    CoordinatorReviewUpdate,
    CriterionEvaluationRead,
    ExplainabilityExport,
    ExplainabilityRow,
    MatchResultDetailRead,
    MatchResultRead,
    MatchTriggerRequest,
    MatchTriggerResponse,
    PatientMatchesResponse,
    UncertaintyFlagRead,
)
from app.schemas.patient import (
    FHIRBundleIngest,
    IngestionResult,
    MedicalEventCreate,
    MedicalEventRead,
    PatientCreate,
    PatientDetailRead,
    PatientRead,
    PatientUpdate,
    PatientVersionRead,
)
from app.schemas.trial import (
    ClinicalTrialCreate,
    ClinicalTrialDetailRead,
    ClinicalTrialRead,
    ClinicalTrialUpdate,
    TrialCriterionCreate,
    TrialCriterionRead,
    TrialSiteCreate,
    TrialSiteRead,
    TrialSyncRequest,
    TrialSyncResult,
)

__all__ = [
    # patient
    "PatientCreate",
    "PatientUpdate",
    "PatientRead",
    "PatientDetailRead",
    "PatientVersionRead",
    "MedicalEventCreate",
    "MedicalEventRead",
    "FHIRBundleIngest",
    "IngestionResult",
    # trial
    "ClinicalTrialCreate",
    "ClinicalTrialUpdate",
    "ClinicalTrialRead",
    "ClinicalTrialDetailRead",
    "TrialCriterionCreate",
    "TrialCriterionRead",
    "TrialSiteCreate",
    "TrialSiteRead",
    "TrialSyncRequest",
    "TrialSyncResult",
    # matching
    "MatchTriggerRequest",
    "MatchTriggerResponse",
    "MatchResultRead",
    "MatchResultDetailRead",
    "CriterionEvaluationRead",
    "UncertaintyFlagRead",
    "CoordinatorReviewUpdate",
    "PatientMatchesResponse",
    "ExplainabilityRow",
    "ExplainabilityExport",
    # feedback
    "ClinicianFeedbackCreate",
    "ClinicianFeedbackRead",
    "FeedbackStatsResponse",
]
