"""Patient-driven matching intake — ``/api/v1/intake/*``.

Endpoints
---------
``GET   /intake/start``     — discover candidate trials based on the
    current user's known conditions.
``POST  /intake/questions`` — ask the LLM what to ask the patient to
    evaluate the most criteria across the candidate pool.
``POST  /intake/answers``   — record the patient's answers as
    :class:`MedicalEvent` rows on their chart.
``POST  /intake/finalize``  — run the standard matching engine against
    the now-enriched chart and return the results.

Why this lives outside ``api/matching.py``
------------------------------------------
The endpoints under ``/matching/*`` are coordinator/clinician-facing —
they accept a target patient id, support trial-subset selection, and
trigger the rematch worker.  The intake flow is patient-self-service:
every call is implicitly scoped to ``current_user.associated_patient_id``
and the only access role permitted is ``PATIENT``.  Keeping them in
separate routers makes the URL surface, OpenAPI grouping, and access
model match the audience.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import get_current_user, require_role
from app.database import get_db
from app.models.matching import (
    MatchResult,
    MatchTriggerEnum,
    OverallMatchStatusEnum,
)
from app.models.patient import EventTypeEnum, Patient
from app.models.trial import ClinicalTrial
from app.models.user import User, UserRole
from app.services.matching_engine import match_patient_against_trials
from app.services.patient_intake import (
    CANDIDATE_POOL_SIZE,
    IntakeQuestion,
    find_candidate_trials,
    generate_questions,
    record_answers,
)

router = APIRouter(
    prefix="/intake",
    tags=["Patient intake"],
    # Patient-only at the router level — no per-handler band-aid.
    dependencies=[Depends(require_role(UserRole.PATIENT))],
)


# ---------------------------------------------------------------------------
# Patient resolver
# ---------------------------------------------------------------------------

def _get_current_patient(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Patient:
    """Return the Patient row linked to the authenticated user.

    Used as a FastAPI dependency so each handler receives a ready-to-use
    Patient.  Raises 400 if the user finished registering but skipped
    the demographic / conditions step, and 404 if the linked row has
    been deleted out from under them.
    """
    if not current_user.associated_patient_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Your account is not yet linked to a patient record.  "
                "Complete your profile before starting an intake."
            ),
        )
    patient = db.get(
        Patient, current_user.associated_patient_id,
        options=[selectinload(Patient.medical_events)],
    )
    if patient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Linked patient record not found.",
        )
    return patient


def _patient_diagnoses(patient: Patient) -> list[str]:
    """Return the patient's diagnosis display names, deduped + sorted."""
    return sorted({
        e.display_name for e in patient.medical_events
        if e.event_type == EventTypeEnum.DIAGNOSIS.value and e.display_name
    })


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CandidateTrialView(BaseModel):
    """One trial in the candidate pool returned to the patient."""

    trial_id: uuid.UUID
    nct_id: str
    title: str
    category: Optional[str] = None
    matched_conditions: list[str] = Field(default_factory=list)
    score: float


class IntakeStartResponse(BaseModel):
    """Initial summary shown to the patient before they answer anything."""

    patient_id: uuid.UUID
    known_conditions: list[str]
    candidate_count: int
    candidates: list[CandidateTrialView]


class IntakeQuestionSchema(BaseModel):
    """Wire shape for one :class:`IntakeQuestion` — keeps the round-trip type-safe."""

    id: str
    question: str
    type: str
    options: list[str] = Field(default_factory=list)
    unit: Optional[str] = None
    helper: Optional[str] = None
    event_template: dict = Field(default_factory=dict)
    helps_evaluate: list[str] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class IntakeQuestionsResponse(BaseModel):
    """The LLM-generated question list."""

    questions: list[IntakeQuestionSchema]
    candidate_trial_ids: list[uuid.UUID]


class IntakeAnswersRequest(BaseModel):
    """Patient answers + the question list they were responding to.

    Echoing the questions back lets the server map each answer to the
    right ``event_template`` without a server-side session cache.
    """

    questions: list[IntakeQuestionSchema]
    answers: dict[str, object] = Field(
        ...,
        description="Map of question_id → answer (string / number / 'yes'|'no').",
    )


class IntakeAnswersResponse(BaseModel):
    events_created: int
    new_event_ids: list[uuid.UUID]


class IntakeFinalizeRequest(BaseModel):
    candidate_trial_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description=(
            "Restrict matching to these specific trials.  When empty, the "
            "engine re-derives the candidate pool from the patient's chart."
        ),
    )


class IntakeFinalizeResponse(BaseModel):
    patient_id: uuid.UUID
    trials_considered: int
    trials_matched: int
    eligible_count: int
    uncertain_count: int
    match_result_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/start",
    response_model=IntakeStartResponse,
    summary="Begin a patient-driven intake — show candidate trials.",
)
def intake_start(
    patient: Patient = Depends(_get_current_patient),
    db: Session = Depends(get_db),
) -> IntakeStartResponse:
    candidates = find_candidate_trials(db, patient, limit=CANDIDATE_POOL_SIZE)
    return IntakeStartResponse(
        patient_id=patient.id,
        known_conditions=_patient_diagnoses(patient),
        candidate_count=len(candidates),
        candidates=[
            CandidateTrialView(
                trial_id=c.trial.id,
                nct_id=c.trial.nct_id,
                title=c.trial.title,
                category=c.trial.category,
                matched_conditions=c.matched_conditions,
                score=round(c.score, 3),
            )
            for c in candidates
        ],
    )


@router.post(
    "/questions",
    response_model=IntakeQuestionsResponse,
    summary="Ask the LLM what to ask the patient.",
)
def intake_questions(
    patient: Patient = Depends(_get_current_patient),
    db: Session = Depends(get_db),
) -> IntakeQuestionsResponse:
    candidates = find_candidate_trials(db, patient, limit=CANDIDATE_POOL_SIZE)
    if not candidates:
        return IntakeQuestionsResponse(questions=[], candidate_trial_ids=[])
    questions = generate_questions(patient, candidates)
    return IntakeQuestionsResponse(
        questions=[IntakeQuestionSchema(**q.to_dict()) for q in questions],
        candidate_trial_ids=[c.trial.id for c in candidates],
    )


@router.post(
    "/answers",
    response_model=IntakeAnswersResponse,
    summary="Persist the patient's answers as MedicalEvents.",
)
def intake_answers(
    payload: IntakeAnswersRequest,
    patient: Patient = Depends(_get_current_patient),
    db: Session = Depends(get_db),
) -> IntakeAnswersResponse:
    # Pydantic already validated the wire shape; convert each schema row
    # to the dataclass the service expects.
    questions = [IntakeQuestion(**q.model_dump()) for q in payload.questions]
    new_events = record_answers(db, patient, questions, payload.answers)
    db.commit()
    return IntakeAnswersResponse(
        events_created=len(new_events),
        new_event_ids=[e.id for e in new_events],
    )


@router.post(
    "/finalize",
    response_model=IntakeFinalizeResponse,
    summary="Run the matching engine against the candidate pool.",
)
def intake_finalize(
    payload: IntakeFinalizeRequest,
    patient: Patient = Depends(_get_current_patient),
    db: Session = Depends(get_db),
) -> IntakeFinalizeResponse:
    # Resolve which trials to match against — explicit list takes
    # priority; otherwise re-derive the candidate pool so the result
    # set is consistent with what /start showed.
    if payload.candidate_trial_ids:
        trials = list(db.execute(
            select(ClinicalTrial)
            .where(ClinicalTrial.id.in_(payload.candidate_trial_ids))
            .options(selectinload(ClinicalTrial.criteria))
        ).scalars())
    else:
        trials = [c.trial for c in find_candidate_trials(db, patient, limit=CANDIDATE_POOL_SIZE)]

    if not trials:
        return IntakeFinalizeResponse(
            patient_id=patient.id,
            trials_considered=0, trials_matched=0,
            eligible_count=0, uncertain_count=0,
            match_result_ids=[],
        )

    stats = match_patient_against_trials(
        db, patient, trials,
        triggered_by=MatchTriggerEnum.CHART_UPDATE,
    )

    # One DB round-trip for the eligible / uncertain tallies instead of
    # iterating every row.
    rows = db.execute(
        select(MatchResult.overall_status)
        .where(MatchResult.id.in_(stats.match_result_ids))
    ).scalars().all()
    eligible  = sum(1 for s in rows if s == OverallMatchStatusEnum.ELIGIBLE.value)
    uncertain = sum(1 for s in rows if s == OverallMatchStatusEnum.UNCERTAIN.value)

    return IntakeFinalizeResponse(
        patient_id=patient.id,
        trials_considered=stats.trials_considered,
        trials_matched=stats.trials_matched,
        eligible_count=eligible,
        uncertain_count=uncertain,
        match_result_ids=[uuid.UUID(mid) for mid in stats.match_result_ids],
    )
