"""Patient-driven matching intake.

The standard matching pipeline (``matching_engine.py``) assumes a chart
is already populated and runs the LLM against existing events.  For a
patient who self-registered with just a couple of conditions, most
criteria come back ``uncertain`` because the engine has nothing to
reason over.

This service flips the loop:

1. **Discover candidate trials.**  Score every recruiting trial on
   how well its ``conditions`` list overlaps the patient's known
   conditions, plus the category-level match.  Pick the top N.

2. **Ask the LLM what it needs.**  Send the LLM the patient's current
   chart + the criteria for the candidate pool, and ask it to draft a
   short list of patient-facing questions whose answers would resolve
   the most criteria.  Returns a structured JSON list with ``type``,
   ``unit``, and an ``event_template`` describing how to materialise
   the answer as a :class:`MedicalEvent`.

3. **Persist answers as events.**  When the patient submits answers,
   each one becomes a :class:`MedicalEvent` row, then the standard
   matching engine runs against the now-enriched chart.

Why an LLM-generated question set rather than a fixed form?
-----------------------------------------------------------
Eligibility criteria are heterogeneous — oncology trials care about
biopsy + staging, diabetes trials care about HbA1c + medication list,
respiratory trials care about FEV1 + steroid use.  A static intake
would have to ask every patient about everything.  A model-generated
intake asks only the questions that move the needle for *this
patient's* candidate pool.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.patient import (
    EventStatusEnum,
    EventTypeEnum,
    MedicalEvent,
    Patient,
)
from app.models.trial import ClinicalTrial, TrialCriterion
from app.services.eligibility_reasoner import _patient_age_years
from app.services.llm_client import (
    LLMClient,
    LLMError,
    LLMResponseParseError,
    get_llm_client,
)

logger = logging.getLogger(__name__)


# How many trials we surface as the "candidate pool" — small enough that
# the LLM's prompt stays under the context window, big enough that the
# patient has something to choose between.
CANDIDATE_POOL_SIZE = 8

# How many questions we ask the patient in one round.  More than ~6 and
# completion rates drop steeply; fewer than ~3 and we don't resolve
# enough criteria.
QUESTION_COUNT_HINT = 6


# ---------------------------------------------------------------------------
# Candidate trial selection
# ---------------------------------------------------------------------------

@dataclass
class CandidateTrial:
    """One trial we think the patient might qualify for, pre-eligibility-check."""

    trial: ClinicalTrial
    matched_conditions: list[str]   # which of the patient's conditions matched
    score: float                    # 0..1 — heuristic match strength


def _normalise(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _candidate_score(patient_conditions: set[str], trial: ClinicalTrial) -> tuple[float, list[str]]:
    """Cheap heuristic — overlap of patient conditions vs trial's stated conditions.

    Substring match in both directions so "Type 2 Diabetes Mellitus" still
    matches a patient whose recorded condition is "Type 2 diabetes".
    """
    trial_conditions = [_normalise(c) for c in (trial.conditions or [])]
    if not trial_conditions or not patient_conditions:
        return 0.0, []
    hits: list[str] = []
    for pc in patient_conditions:
        if not pc:
            continue
        for tc in trial_conditions:
            if pc in tc or tc in pc:
                hits.append(pc)
                break
    if not hits:
        return 0.0, []
    return len(hits) / max(len(trial_conditions), len(patient_conditions)), hits


def find_candidate_trials(
    db: Session,
    patient: Patient,
    *,
    limit: int = CANDIDATE_POOL_SIZE,
    statuses: Optional[list[str]] = None,
) -> list[CandidateTrial]:
    """Return the top ``limit`` trials matching ``patient``'s conditions.

    Considers recruiting / not-yet-recruiting trials whose ``conditions``
    list overlaps any of the patient's diagnoses.  Trials *with* parsed
    criteria are preferred (the LLM can ask better questions about
    them) but we still surface trials without parsed criteria so the
    catalog isn't empty when criteria-parsing is still queued.
    """
    statuses = statuses or ["RECRUITING", "NOT_YET_RECRUITING"]

    # Pull the patient's diagnosis names from the timeline.
    patient_conditions: set[str] = set()
    for event in patient.medical_events:
        if event.event_type == EventTypeEnum.DIAGNOSIS.value and event.display_name:
            patient_conditions.add(_normalise(event.display_name))
    if not patient_conditions:
        return []

    # Pull a wide-ish slice of recruiting trials, score, then sort.  We
    # over-fetch (1000) because the JSON containment match here is
    # client-side; the LIMIT on the query is just a pre-filter on the
    # status / conditions-non-null columns.
    stmt = (
        select(ClinicalTrial)
        .where(
            ClinicalTrial.overall_status.in_(statuses),
            ClinicalTrial.conditions.is_not(None),
        )
        .options(selectinload(ClinicalTrial.criteria))
        .limit(1000)
    )
    pool = db.execute(stmt).scalars().all()

    scored: list[CandidateTrial] = []
    for trial in pool:
        score, hits = _candidate_score(patient_conditions, trial)
        if score <= 0:
            continue
        # Bias trials with parsed criteria up so the LLM has something
        # to ask questions about — but don't drop the unparsed ones.
        if trial.criteria:
            score += 0.001  # tiebreaker
        scored.append(CandidateTrial(trial=trial, matched_conditions=hits, score=score))

    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

_VALID_QUESTION_TYPES: frozenset[str] = frozenset({"text", "number", "choice", "yes_no"})


@dataclass
class IntakeQuestion:
    """One question to put in front of the patient."""

    id: str
    question: str
    type: str                          # 'text' | 'number' | 'choice' | 'yes_no'
    options: list[str] = field(default_factory=list)
    unit: Optional[str] = None
    helper: Optional[str] = None       # short hint shown under the field
    event_template: dict[str, Any] = field(default_factory=dict)
    helps_evaluate: list[str] = field(default_factory=list)  # NCT IDs

    def to_dict(self) -> dict[str, Any]:
        return {
            "id":           self.id,
            "question":     self.question,
            "type":         self.type,
            "options":      self.options,
            "unit":         self.unit,
            "helper":       self.helper,
            "event_template": self.event_template,
            "helps_evaluate": self.helps_evaluate,
        }

    @classmethod
    def from_dict(cls, raw: Any, *, default_id: Optional[str] = None) -> Optional["IntakeQuestion"]:
        """Construct from a (possibly LLM-produced) dict, dropping malformed input.

        Returns ``None`` when the dict can't be coerced into a valid
        question — callers filter those out instead of raising.
        """
        if not isinstance(raw, dict):
            return None
        text = str(raw.get("question") or "").strip()
        if not text:
            return None
        qtype = str(raw.get("type") or "text").strip().lower()
        if qtype not in _VALID_QUESTION_TYPES:
            qtype = "text"
        options_raw = raw.get("options")
        options = (
            [str(o) for o in options_raw if str(o).strip()]
            if isinstance(options_raw, list) else []
        )
        helps_raw = raw.get("helps_evaluate")
        helps = [str(h) for h in helps_raw] if isinstance(helps_raw, list) else []
        template = raw.get("event_template")
        return cls(
            id=str(raw.get("id") or default_id or ""),
            question=text,
            type=qtype,
            options=options,
            unit=raw.get("unit") or None,
            helper=raw.get("helper") or None,
            event_template=dict(template) if isinstance(template, dict) else {},
            helps_evaluate=helps,
        )


_QUESTION_SYSTEM_PROMPT = """\
You are a clinical-trial intake assistant.  Given a patient's known data \
and the eligibility criteria for several candidate trials, you draft a \
short list of patient-facing questions that, if answered, would resolve \
the most criteria.

Rules:
- Ask only what's NOT already known about the patient.
- Prefer questions that unblock multiple trials.
- Use simple, plain language (6th-grade reading level).
- Each question must be answerable without medical training.
- Return only the JSON object — no commentary, no markdown fences.
"""


def _build_question_prompt(
    patient: Patient,
    candidates: list[CandidateTrial],
) -> str:
    """Compose the question-generation prompt.

    Keeps the per-trial criterion list short (~5 critical inclusion +
    exclusion items) so the prompt stays under a few thousand tokens
    even for 8 candidate trials.
    """
    # Patient summary — reuse the shared age helper so the reasoner and
    # the intake prompt agree on the arithmetic.
    age_years = _patient_age_years(patient)
    age_line = f"- Age: {age_years} years\n" if age_years is not None else ""
    conditions = [
        e.display_name for e in patient.medical_events
        if e.event_type == EventTypeEnum.DIAGNOSIS.value
    ]
    meds = [
        e.display_name for e in patient.medical_events
        if e.event_type == EventTypeEnum.MEDICATION.value
    ]
    labs = [
        f"{e.display_name} = {e.value} {e.unit or ''}".strip()
        for e in patient.medical_events
        if e.event_type == EventTypeEnum.LAB_RESULT.value and e.value
    ]

    patient_block = (
        f"{age_line}"
        f"- Sex: {patient.sex or 'unknown'}\n"
        f"- Conditions: {', '.join(conditions) or '(none recorded)'}\n"
        f"- Medications: {', '.join(meds) or '(none recorded)'}\n"
        f"- Lab values: {', '.join(labs) or '(none recorded)'}\n"
    )

    trials_block = ""
    for c in candidates:
        crit = sorted(c.trial.criteria, key=lambda x: x.order_index)
        # Keep it short — top inclusion + a couple of exclusion.
        critical_inc = [k for k in crit if k.is_critical and k.criterion_type == "inclusion"][:5]
        critical_exc = [k for k in crit if k.criterion_type == "exclusion"][:3]
        trials_block += (
            f"\n## {c.trial.nct_id} — {c.trial.title}\n"
            f"Conditions: {', '.join(c.trial.conditions or [])}\n"
            f"Inclusion:\n"
            + "\n".join(f"  - {k.parsed_description or k.original_text}" for k in critical_inc)
            + "\nExclusion:\n"
            + "\n".join(f"  - {k.parsed_description or k.original_text}" for k in critical_exc)
            + "\n"
        )

    return f"""\
The patient has agreed to answer questions to help find clinical trials they qualify for.

## Patient's known data
{patient_block}

## Candidate trials ({len(candidates)})
{trials_block}

## Your job
Draft up to {QUESTION_COUNT_HINT} questions that would help evaluate the most criteria across the candidate trials.

Return a JSON object with this exact shape:

{{
  "questions": [
    {{
      "id": "q1",
      "question": "Plain-English question to show the patient.",
      "type": "text" | "number" | "choice" | "yes_no",
      "options": ["..."],          // required when type=choice; empty otherwise
      "unit": "%" or "mg/dL" etc., // optional, for type=number
      "helper": "optional one-line hint shown under the input",
      "event_template": {{
          "event_type": "lab_result" | "medication" | "diagnosis" | "procedure" | "note",
          "display_name": "Name to put on the timeline event",
          "code": "LOINC/RxNorm/SNOMED code if you know it, else null",
          "code_system": "LOINC" | "RxNorm" | "SNOMED-CT" | null
      }},
      "helps_evaluate": ["NCT01234567", "..."]   // which trials this answer unblocks
    }}
  ]
}}

Examples of high-value questions:
- "What was your most recent HbA1c result, if you know it?" (number, %, lab_result)
- "Are you currently taking metformin?" (yes_no, medication)
- "How would you describe the severity of your asthma?" (choice ["mild","moderate","severe"], diagnosis)

Output only the JSON object.
"""


def generate_questions(
    patient: Patient,
    candidates: list[CandidateTrial],
    *,
    client: Optional[LLMClient] = None,
) -> list[IntakeQuestion]:
    """Ask the LLM what to ask the patient.

    Falls back to a small hand-written question set when the LLM call
    fails — keeps the patient experience moving even when Groq is down.
    """
    if not candidates:
        return []
    client = client or get_llm_client()
    prompt = _build_question_prompt(patient, candidates)

    try:
        payload = client.complete_json(
            prompt,
            system=_QUESTION_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=1500,
            operation="patient_intake",
        )
    except (LLMError, LLMResponseParseError) as exc:
        logger.warning("Intake question generation failed: %s", exc)
        return _fallback_questions(candidates)

    raw_questions = payload.get("questions") or []
    out = [
        q for q in (
            IntakeQuestion.from_dict(raw, default_id=f"q{i + 1}")
            for i, raw in enumerate(raw_questions)
        )
        if q is not None
    ]
    return out or _fallback_questions(candidates)


def _fallback_questions(candidates: list[CandidateTrial]) -> list[IntakeQuestion]:
    """Conservative fallback when the LLM intake-prompt is unavailable.

    Better than nothing — covers the highest-yield gaps across the
    common condition families.
    """
    return [
        IntakeQuestion(
            id="q1",
            question="What is your current weight?",
            type="number", unit="kg",
            helper="Used for dose-based eligibility rules.",
            event_template={"event_type": "vital_sign", "display_name": "Weight"},
        ),
        IntakeQuestion(
            id="q2",
            question="What is your typical resting blood pressure?",
            type="text",
            helper="Format: systolic/diastolic, e.g. 130/85.",
            event_template={"event_type": "vital_sign", "display_name": "Blood pressure"},
        ),
        IntakeQuestion(
            id="q3",
            question="Are you currently taking any prescription medications?",
            type="text",
            helper="List the drug names you take regularly.",
            event_template={"event_type": "medication", "display_name": "Current medications"},
        ),
    ]


# ---------------------------------------------------------------------------
# Answer materialisation
# ---------------------------------------------------------------------------

def record_answers(
    db: Session,
    patient: Patient,
    questions: list[IntakeQuestion],
    answers: dict[str, Any],
) -> list[MedicalEvent]:
    """Convert ``{question_id: answer}`` into :class:`MedicalEvent` rows.

    The ``event_template`` on each question tells us how to shape the
    row.  Yes/no answers only persist on a ``yes`` (a ``no`` means the
    patient does *not* have that thing; we leave the chart silent
    rather than recording absence as a fact).
    """
    by_id = {q.id: q for q in questions}
    now_utc = datetime.now(timezone.utc)
    new_events: list[MedicalEvent] = []

    for qid, raw_answer in answers.items():
        q = by_id.get(qid)
        if q is None:
            continue
        value, persist = _normalise_answer(q, raw_answer)
        if not persist:
            continue

        template = q.event_template or {}
        event_type_raw = (template.get("event_type") or "note").lower()
        try:
            event_type = EventTypeEnum(event_type_raw)
        except ValueError:
            event_type = EventTypeEnum.NOTE

        # A numeric self-reported measurement (weight, height, BP, HbA1c)
        # is a vital sign / lab, not a free-text note.  The question-
        # generation LLM sometimes types these as ``note``, which would
        # otherwise drop the number entirely — upgrade so the value is
        # kept and the reasoner can slice it by category.
        if q.type == "number" and event_type == EventTypeEnum.NOTE:
            event_type = (
                EventTypeEnum.LAB_RESULT if (q.unit or "") in {"%", "mg/dL", "mmol/L"}
                else EventTypeEnum.VITAL_SIGN
            )

        display_name = (
            template.get("display_name")
            or q.question[:120]
        )
        # Persist the answer's value for every typed answer (number /
        # choice / text / yes-no), not only lab/vital events — otherwise a
        # weight answer recorded as a note loses its number.
        event = MedicalEvent(
            patient_id=patient.id,
            event_type=event_type.value,
            event_date=now_utc,
            display_name=display_name,
            code=template.get("code"),
            code_system=template.get("code_system"),
            value=value,
            unit=q.unit or None,
            status=EventStatusEnum.ACTIVE.value,
            source_text=f"Patient self-reported via intake (Q: {q.question})\nA: {value}",
            source_document="patient-intake",
        )
        db.add(event)
        new_events.append(event)

    if new_events:
        db.flush()
    return new_events


def _normalise_answer(question: IntakeQuestion, raw: Any) -> tuple[Optional[str], bool]:
    """Return ``(value, persist)`` for an answer.

    * For ``yes_no`` answers we persist only on a positive response.
    * Empty / None answers are skipped so the patient can leave optional
      questions blank without polluting the chart.
    """
    if raw is None:
        return None, False
    text = str(raw).strip()
    if not text:
        return None, False

    if question.type == "yes_no":
        positive = text.lower() in {"yes", "y", "true", "1"}
        if not positive:
            return None, False
        # Persist a marker text so the LLM downstream sees the event
        return "yes", True

    if question.type == "number":
        # Validate it parses but keep the original string for display.
        try:
            float(text)
        except ValueError:
            return None, False
        return text, True

    # text / choice
    return text, True
