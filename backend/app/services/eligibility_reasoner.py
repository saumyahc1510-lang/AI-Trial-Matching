"""LLM-powered eligibility reasoning — the second half of Phase 3's core.

For a given ``(Patient, TrialCriterion)`` pair this service produces an
:class:`EligibilityVerdict` with:

* ``status`` — ``eligible`` / ``ineligible`` / ``uncertain``.
* ``reasoning`` — natural-language explanation.
* ``evidence_text`` — exact quote from the patient timeline backing the
  decision (or ``None`` when nothing in the chart speaks to the rule).
* ``evidence_entry`` — the :class:`TimelineEntry` cited as evidence, so
  the matching engine can link back to its source :class:`MedicalEvent`.
* ``missing_data`` — when status is ``uncertain``, a short description
  of what information would resolve the ambiguity.

How it works
------------

1. **Hard short-circuits before calling the LLM.**  Demographic
   criteria (age, sex) are handled deterministically — sending the LLM a
   "patient is 47, criterion says ≥ 18" prompt is wasteful and adds
   latency.  Temporal criteria with structured ``temporal_constraint``
   are first sent through :mod:`app.services.temporal_engine`; if the
   engine returns ``met`` / ``not_met`` we accept it.  Only ``uncertain``
   verdicts are kicked up to the LLM.

2. **Compact prompt construction.**  We do *not* dump the entire
   timeline into the prompt.  Instead we send:

   * The criterion (original text + LLM-structured form).
   * Patient demographics (already trimmed of PHI by the API layer).
   * A category-filtered slice of the timeline (e.g. only medications
     when the criterion is a medication rule).

   This keeps token usage bounded for patients with hundreds of events.

3. **Strict JSON output.**  The system prompt enforces a single JSON
   object; :meth:`LLMClient.complete_json` parses + recovers when the
   model adds prose.  Invalid responses raise :class:`LLMResponseParseError`
   which the matching engine catches and converts into an ``uncertain``
   verdict with the parse error in ``missing_data``.

The reasoner is the **only** module that talks to the LLM during matching
— keeping that surface small means the audit trail captures every
PHI-touching call in one place.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.middleware.hipaa import PHIScrubber
from app.models.matching import CriterionStatusEnum, EvaluatorEnum
from app.models.patient import EventTypeEnum, Patient
from app.models.trial import CriterionCategoryEnum, CriterionTypeEnum, TrialCriterion
from app.services.llm_client import (
    LLMClient,
    LLMError,
    LLMResponseParseError,
    get_llm_client,
)
from app.services.temporal_engine import (
    PatientTimeline,
    TemporalEvaluation,
    TemporalVerdict,
    TimelineEntry,
    evaluate_temporal_constraint,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class EligibilityStatus(str, Enum):
    """Three-state outcome for one criterion against one patient."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNCERTAIN = "uncertain"

    @classmethod
    def from_criterion_status(cls, status: CriterionStatusEnum) -> "EligibilityStatus":
        mapping = {
            CriterionStatusEnum.MET: cls.ELIGIBLE,
            CriterionStatusEnum.NOT_MET: cls.INELIGIBLE,
            CriterionStatusEnum.UNCERTAIN: cls.UNCERTAIN,
        }
        return mapping[status]

    def to_criterion_status(self) -> CriterionStatusEnum:
        mapping = {
            EligibilityStatus.ELIGIBLE: CriterionStatusEnum.MET,
            EligibilityStatus.INELIGIBLE: CriterionStatusEnum.NOT_MET,
            EligibilityStatus.UNCERTAIN: CriterionStatusEnum.UNCERTAIN,
        }
        return mapping[self]


@dataclass
class EligibilityVerdict:
    """The reasoner's output for one criterion.

    Attributes:
        status:         Three-state verdict.
        reasoning:      Plain-English explanation suitable for audit.
        confidence:     Self-reported confidence in ``[0, 1]``.
        evidence_text:  Verbatim chart quote, when available.
        evidence_entry: Timeline entry cited; ``None`` if no specific
                        record drove the decision.
        missing_data:   For ``uncertain`` verdicts only — short hint
                        describing what would resolve the ambiguity.
        evaluator:      Which engine produced the verdict — used by the
                        matching engine when persisting to
                        :class:`~app.models.matching.CriterionEvaluation`.
        llm_model_used: Identifier of the LLM model, if any.
    """

    status: EligibilityStatus
    reasoning: str
    confidence: float = 0.5
    evidence_text: Optional[str] = None
    evidence_entry: Optional[TimelineEntry] = None
    missing_data: Optional[str] = None
    evaluator: EvaluatorEnum = EvaluatorEnum.LLM
    llm_model_used: Optional[str] = None


# ---------------------------------------------------------------------------
# Demographic short-circuits
# ---------------------------------------------------------------------------

def _patient_age_years(patient: Patient, *, at: Optional[date] = None) -> Optional[int]:
    """Return the patient's age in completed years, or ``None`` if unknown."""
    if not patient.date_of_birth:
        return None
    ref = at or date.today()
    years = ref.year - patient.date_of_birth.year
    if (ref.month, ref.day) < (patient.date_of_birth.month, patient.date_of_birth.day):
        years -= 1
    return years


def _evaluate_demographic(
    criterion: TrialCriterion,
    patient: Patient,
) -> Optional[EligibilityVerdict]:
    """Try to resolve a demographic criterion deterministically.

    Returns ``None`` to defer to the LLM — only the most common,
    unambiguous shapes (age comparisons, sex matches) are handled here.
    """
    constraint = criterion.value_constraint or {}
    metric = (constraint.get("metric") or "").lower()
    is_exclusion = criterion.criterion_type == CriterionTypeEnum.EXCLUSION.value

    # ── Age ──────────────────────────────────────────────────────────
    if metric == "age" and constraint.get("operator") in {"<", "<=", ">", ">=", "==", "range"}:
        age = _patient_age_years(patient)
        if age is None:
            return EligibilityVerdict(
                status=EligibilityStatus.UNCERTAIN,
                reasoning="Patient date_of_birth is missing.",
                missing_data="Patient date of birth",
                evaluator=EvaluatorEnum.RULE_ENGINE,
                confidence=1.0,
            )
        passes = _numeric_compare(age, constraint)
        if passes is None:
            return None  # Unhandled shape — fall through to LLM.
        # Inclusion: passes==True → eligible.  Exclusion inverts.
        eligible = passes if not is_exclusion else not passes
        status = EligibilityStatus.ELIGIBLE if eligible else EligibilityStatus.INELIGIBLE
        return EligibilityVerdict(
            status=status,
            reasoning=(
                f"Patient is {age} years old; criterion requires "
                f"age {constraint.get('operator')} {constraint.get('value')}."
            ),
            confidence=1.0,
            evidence_text=f"Date of birth: {patient.date_of_birth.isoformat()}",
            evaluator=EvaluatorEnum.RULE_ENGINE,
        )

    # ── Sex ──────────────────────────────────────────────────────────
    if criterion.category == CriterionCategoryEnum.DEMOGRAPHIC.value and patient.sex:
        text = (criterion.original_text or "").lower()
        if "female" in text and "male" not in text.replace("female", ""):
            wanted = "female"
        elif "male only" in text or text.strip().endswith("male"):
            wanted = "male"
        else:
            wanted = None
        if wanted is not None:
            matches = patient.sex.lower() == wanted
            eligible = matches if not is_exclusion else not matches
            status = EligibilityStatus.ELIGIBLE if eligible else EligibilityStatus.INELIGIBLE
            return EligibilityVerdict(
                status=status,
                reasoning=(
                    f"Patient sex is {patient.sex!r}; criterion targets "
                    f"{wanted!r}."
                ),
                confidence=0.95,
                evidence_text=f"Patient sex: {patient.sex}",
                evaluator=EvaluatorEnum.RULE_ENGINE,
            )

    return None


def _numeric_compare(value: float, constraint: dict[str, Any]) -> Optional[bool]:
    """Apply a structured value constraint to ``value``.

    Returns ``None`` when the operator / value can't be parsed so the
    caller can fall back to the LLM.
    """
    op = constraint.get("operator")
    expected = constraint.get("value")
    try:
        if op == "range" and isinstance(expected, (list, tuple)) and len(expected) == 2:
            return float(expected[0]) <= value <= float(expected[1])
        if op in {"<", "<=", ">", ">=", "==", "!="}:
            expected_num = float(expected)
            return {
                "<":  lambda v, e: v < e,
                "<=": lambda v, e: v <= e,
                ">":  lambda v, e: v > e,
                ">=": lambda v, e: v >= e,
                "==": lambda v, e: v == e,
                "!=": lambda v, e: v != e,
            }[op](value, expected_num)
    except (TypeError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Temporal short-circuit
# ---------------------------------------------------------------------------

def _evaluate_via_temporal_engine(
    criterion: TrialCriterion,
    timeline: PatientTimeline,
) -> Optional[EligibilityVerdict]:
    """Run the temporal engine when the criterion has a temporal_constraint.

    Returns a verdict only for ``met`` / ``not_met`` — ``uncertain``
    verdicts are returned to the caller as ``None`` so the LLM gets a
    chance to look at the wider chart context.
    """
    constraint = criterion.temporal_constraint
    if not constraint:
        return None

    # Carve-out criteria ("...other than insulin", "non-insulin
    # medications") can't be judged by the keyword-based temporal scan —
    # it matches the very drug being excepted and fires a false verdict.
    # Defer those to the LLM, which reads the drug names in context.
    if _subject_has_exception(criterion.original_text):
        return None

    evaluation: TemporalEvaluation = evaluate_temporal_constraint(
        timeline,
        constraint=constraint,
        criterion_type=criterion.criterion_type,
        subject_category=criterion.category,
        subject_keywords=_keywords_from_text(criterion.original_text),
    )

    if evaluation.verdict == TemporalVerdict.UNCERTAIN:
        return None

    status = (
        EligibilityStatus.ELIGIBLE
        if evaluation.verdict == TemporalVerdict.MET
        else EligibilityStatus.INELIGIBLE
    )
    evidence_entry = evaluation.evidence_entries[0] if evaluation.evidence_entries else None
    return EligibilityVerdict(
        status=status,
        reasoning=evaluation.reasoning,
        confidence=0.9,
        evidence_text=(
            f"{evidence_entry.event_date.date()} — {evidence_entry.display_name}"
            if evidence_entry
            else None
        ),
        evidence_entry=evidence_entry,
        evaluator=EvaluatorEnum.TEMPORAL_ENGINE,
    )


# Words we strip before keyword-matching the criterion's display.
_STOPWORDS = {
    "the", "a", "an", "of", "in", "with", "without", "no", "any",
    "and", "or", "for", "to", "at", "on", "by", "from", "as", "is",
    "must", "should", "have", "has", "had", "history", "prior",
    "criterion", "criteria", "patient", "patients", "subject", "subjects",
}


# Carve-out / exception markers: when a criterion's subject is qualified
# by one of these, a plain keyword scan can't honour the exception (it
# matches the excepted term itself), so the temporal fast-path defers to
# the LLM.  ``non-`` is hyphen-anchored to avoid "none"/"nonetheless".
_SUBJECT_EXCEPTION_RE = re.compile(
    r"\bother than\b|\bnon-\w+|\bexcept(?:ing|ed)?\b|\bexcluding\b"
    r"|\bbesides\b|\bapart from\b|\brather than\b",
    re.IGNORECASE,
)


def _subject_has_exception(text: Optional[str]) -> bool:
    """True when the criterion names a carve-out (e.g. "drugs other than
    insulin", "non-steroidal").  Such criteria are routed to the LLM
    instead of the keyword-based temporal scan."""
    return bool(text and _SUBJECT_EXCEPTION_RE.search(text))


def _keywords_from_text(text: Optional[str]) -> list[str]:
    """Extract simple subject keywords from a criterion's original text.

    Used to narrow the timeline scan when the criterion category alone
    isn't specific enough (e.g. "chemotherapy" vs "anticoagulants" — both
    are ``medication`` events).
    """
    if not text:
        return []
    cleaned = "".join(c if c.isalnum() else " " for c in text.lower())
    tokens = [t for t in cleaned.split() if len(t) > 3 and t not in _STOPWORDS]
    # Cap at 8 to avoid spurious matches.
    return tokens[:8]


# ---------------------------------------------------------------------------
# LLM prompting
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a clinical-trial coordinator evaluating whether a patient meets a \
specific eligibility criterion.  You reason carefully from the supplied \
patient data and return a single JSON object — no commentary, no markdown \
fences.

Verdicts:
  - "eligible"   : criterion is satisfied
  - "ineligible" : criterion is clearly violated
  - "uncertain"  : the chart does not contain enough information to decide

Choose "uncertain" rather than guessing.  Cite an exact quote from the \
patient data when one is available.

Important — the trial's own target condition does NOT disqualify the \
patient.  When an exclusion refers to "other", "additional", "concurrent", \
or "second" diseases / conditions (e.g. "other autoimmune diseases", "any \
other chronic disease", "a clinically significant disorder"), it means \
conditions APART FROM the trial's target condition listed under "Trial \
context".  Do not treat the qualifying diagnosis itself as the excluded \
"other" condition.
"""


_USER_PROMPT_TEMPLATE = """\
## Trial context
{trial_context}

## Criterion
Type:       {criterion_type}
Category:   {category}
Critical:   {is_critical}
Text:       {original_text}
Parsed:     {parsed_description}
Temporal:   {temporal_constraint}
Value:      {value_constraint}

## Patient demographics
{demographics}

## Relevant chart events ({event_count} of {timeline_total})
{events_block}

## Output schema
Return a JSON object with EXACTLY these keys:

{{
  "status":        "eligible" | "ineligible" | "uncertain",
  "reasoning":     short plain-English explanation (1-3 sentences),
  "confidence":    float between 0 and 1,
  "evidence_text": exact quote from the chart that drove the decision (or null),
  "evidence_event_index": index (0-based) of the cited event in the list above (or null),
  "missing_data":  for "uncertain" only - what info would resolve it (or null)
}}

Return only the JSON object.
"""


_MAX_EVENTS_IN_PROMPT = 25


def _trial_context_block(criterion: TrialCriterion) -> str:
    """Render the trial's target condition + title so the LLM can anchor
    relative exclusions ("other" diseases) to the qualifying condition.

    Falls back gracefully when the criterion's trial relationship isn't
    loaded or carries no condition list.
    """
    trial = getattr(criterion, "trial", None)
    if trial is None:
        return "- target_condition: (unknown)"
    conditions = trial.conditions or []
    cond_str = ", ".join(str(c) for c in conditions) if conditions else "(unspecified)"
    lines = [f"- target_condition(s): {cond_str}"]
    if trial.title:
        lines.append(f"- trial_title: {trial.title}")
    return "\n".join(lines)


def _demographics_block(patient: Patient) -> str:
    """Render the demographic mini-block included in the LLM prompt."""
    age = _patient_age_years(patient)
    return "\n".join(
        f"- {label}: {value}"
        for label, value in [
            ("age_years", age if age is not None else "unknown"),
            ("sex", patient.sex or "unknown"),
            ("race", patient.race or "unspecified"),
            ("ethnicity", patient.ethnicity or "unspecified"),
            ("preferred_language", patient.preferred_language or "en"),
        ]
    )


def _relevant_entries(
    timeline: PatientTimeline,
    criterion: TrialCriterion,
) -> list[TimelineEntry]:
    """Pick the timeline slice most likely to be relevant to ``criterion``.

    Strategy: category-based pre-filter, then keyword-based intersection,
    then truncate to the most-recent ``_MAX_EVENTS_IN_PROMPT`` items.
    """
    by_category = {
        CriterionCategoryEnum.MEDICATION.value:  (EventTypeEnum.MEDICATION,),
        CriterionCategoryEnum.DIAGNOSIS.value:   (EventTypeEnum.DIAGNOSIS,),
        CriterionCategoryEnum.PROCEDURE.value:   (EventTypeEnum.PROCEDURE,),
        CriterionCategoryEnum.LAB_VALUE.value:   (EventTypeEnum.LAB_RESULT,),
        CriterionCategoryEnum.ORGAN_FUNCTION.value: (EventTypeEnum.LAB_RESULT,),
    }
    filtered: list[TimelineEntry]
    type_tuple = by_category.get(criterion.category or "")
    if type_tuple:
        filtered = timeline.of_type(*type_tuple)
    else:
        filtered = list(timeline.entries)

    keywords = _keywords_from_text(criterion.original_text)
    if keywords and filtered:
        kw_hits = [
            e for e in filtered
            if any(k in (e.display_name or "").lower() for k in keywords)
        ]
        # Prefer keyword hits but never starve the LLM of context — if
        # nothing matches, fall back to the category slice.
        if kw_hits:
            filtered = kw_hits

    # Most-recent N entries.
    return filtered[-_MAX_EVENTS_IN_PROMPT:]


def _format_events(entries: list[TimelineEntry]) -> str:
    """Render timeline entries as a numbered list for the prompt."""
    if not entries:
        return "(no events available in this category)"
    lines: list[str] = []
    for idx, e in enumerate(entries):
        value_part = ""
        if e.value:
            value_part = f"  value={e.value}{(' ' + e.unit) if e.unit else ''}"
        lines.append(
            f"[{idx}] {e.event_date.date()}  {e.event_type.value:11s}  "
            f"{e.display_name}{value_part}  status={e.status.value}"
        )
    return "\n".join(lines)


def _build_user_prompt(
    criterion: TrialCriterion,
    patient: Patient,
    relevant: list[TimelineEntry],
    timeline_total: int,
) -> str:
    return _USER_PROMPT_TEMPLATE.format(
        trial_context=_trial_context_block(criterion),
        criterion_type=criterion.criterion_type,
        category=criterion.category,
        is_critical=criterion.is_critical,
        original_text=criterion.original_text.strip(),
        parsed_description=criterion.parsed_description or "(none)",
        temporal_constraint=json.dumps(criterion.temporal_constraint or None),
        value_constraint=json.dumps(criterion.value_constraint or None),
        demographics=_demographics_block(patient),
        event_count=len(relevant),
        timeline_total=timeline_total,
        events_block=_format_events(relevant),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate_criterion(
    criterion: TrialCriterion,
    patient: Patient,
    timeline: PatientTimeline,
    *,
    client: Optional[LLMClient] = None,
) -> EligibilityVerdict:
    """Decide whether ``patient`` satisfies ``criterion``.

    Pipeline (each step can short-circuit on a confident verdict):

    1. Demographic rule engine (age, sex).
    2. Structured temporal engine, when ``temporal_constraint`` is set
       and the criterion is purely temporal.
    3. LLM fallback for everything else, with a chart slice selected by
       category + keyword.

    Errors talking to the LLM degrade to ``uncertain`` rather than
    raising — the matching engine wants to record *something* for every
    criterion so the explainability UI can render the missing-data
    state.
    """
    # 1. Deterministic demographic short-circuits.
    if criterion.category == CriterionCategoryEnum.DEMOGRAPHIC.value:
        verdict = _evaluate_demographic(criterion, patient)
        if verdict is not None:
            return verdict

    # 2. Structured temporal evaluation.
    if criterion.temporal_constraint and not criterion.value_constraint:
        verdict = _evaluate_via_temporal_engine(criterion, timeline)
        if verdict is not None:
            return verdict

    # 3. LLM fallback.
    return _evaluate_via_llm(criterion, patient, timeline, client=client)


def _evaluate_via_llm(
    criterion: TrialCriterion,
    patient: Patient,
    timeline: PatientTimeline,
    *,
    client: Optional[LLMClient] = None,
) -> EligibilityVerdict:
    """Ask the LLM for a verdict, with safe fallbacks on parse / API errors."""
    client = client or get_llm_client()
    relevant = _relevant_entries(timeline, criterion)
    raw_prompt = _build_user_prompt(
        criterion, patient, relevant, timeline_total=len(timeline.entries)
    )

    # De-identify the prompt before it leaves the process.  The scrubber
    # is patient-scoped so it can later restore identifiers in the LLM
    # response for clinician-facing display.
    scrubber = PHIScrubber.for_patient(patient)
    prompt = scrubber.scrub(raw_prompt) or raw_prompt

    try:
        payload = client.complete_json(
            prompt,
            system=_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=400,
            operation="eligibility_reasoner",
        )
    except (LLMError, LLMResponseParseError) as exc:
        logger.warning("LLM eligibility call failed: %s", exc)
        return EligibilityVerdict(
            status=EligibilityStatus.UNCERTAIN,
            reasoning="LLM call failed; criterion left unresolved.",
            confidence=0.0,
            missing_data=f"LLM error: {exc}",
            evaluator=EvaluatorEnum.LLM,
            llm_model_used=getattr(client, "model", None),
        )

    # Restore patient-specific tokens in any free-text fields the model
    # echoed back, so downstream callers / audit trails see the real
    # identifiers.  Generic ``[SSN]`` / ``[PHONE]`` tokens stay tokenised.
    for key in ("reasoning", "evidence_text", "missing_data"):
        if isinstance(payload.get(key), str):
            payload[key] = scrubber.restore(payload[key])

    return _verdict_from_payload(payload, relevant, client_model=getattr(client, "model", None))


def _verdict_from_payload(
    payload: dict[str, Any],
    relevant: list[TimelineEntry],
    *,
    client_model: Optional[str],
) -> EligibilityVerdict:
    """Coerce the LLM's JSON output into an :class:`EligibilityVerdict`.

    Unknown / malformed values are gracefully demoted to ``uncertain``
    rather than crashing the match pipeline.
    """
    raw_status = (payload.get("status") or "").lower()
    try:
        status = EligibilityStatus(raw_status)
    except ValueError:
        status = EligibilityStatus.UNCERTAIN

    confidence_raw = payload.get("confidence", 0.5)
    try:
        confidence = max(0.0, min(1.0, float(confidence_raw)))
    except (TypeError, ValueError):
        confidence = 0.5

    evidence_entry: Optional[TimelineEntry] = None
    idx = payload.get("evidence_event_index")
    if isinstance(idx, int) and 0 <= idx < len(relevant):
        evidence_entry = relevant[idx]

    reasoning = (payload.get("reasoning") or "").strip()
    if not reasoning:
        reasoning = "LLM returned no reasoning text."

    return EligibilityVerdict(
        status=status,
        reasoning=reasoning,
        confidence=confidence,
        evidence_text=payload.get("evidence_text") or None,
        evidence_entry=evidence_entry,
        missing_data=(payload.get("missing_data") if status == EligibilityStatus.UNCERTAIN else None),
        evaluator=EvaluatorEnum.LLM,
        llm_model_used=client_model,
    )
