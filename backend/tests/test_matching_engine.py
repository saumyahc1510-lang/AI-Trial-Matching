"""Integration tests for :mod:`app.services.matching_engine`.

These tests use the real DB (via the transactional ``db_session``
fixture) and a ``FakeLLMClient`` so they're deterministic + offline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import ClinicalTrial, MatchResult, MedicalEvent, TrialCriterion
from app.models.matching import (
    CriterionStatusEnum,
    MatchTriggerEnum,
    OverallMatchStatusEnum,
)
from app.models.patient import EventStatusEnum, EventTypeEnum, SexEnum
from app.services.matching_engine import (
    match_patient_against_trial,
    recompute_match_counters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_event(db, patient_id, *, name: str, days_ago: int, etype: EventTypeEnum):
    """Persist a medical event for the patient."""
    evt = MedicalEvent(
        patient_id=patient_id,
        event_type=etype.value,
        event_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        end_date=None,
        display_name=name,
        status=EventStatusEnum.ACTIVE.value,
    )
    db.add(evt)
    db.flush()
    return evt


def _make_trial(db, *, nct_id: str = "T-MATCH-1") -> ClinicalTrial:
    trial = ClinicalTrial(
        nct_id=nct_id,
        title="Test trial",
        overall_status="RECRUITING",
        is_manually_added=True,
    )
    db.add(trial)
    db.flush()
    return trial


def _add_criterion(db, trial_id, *, original_text, category, criterion_type="inclusion",
                   is_critical=True, order_index=0, value_constraint=None,
                   temporal_constraint=None) -> TrialCriterion:
    crit = TrialCriterion(
        trial_id=trial_id,
        criterion_type=criterion_type,
        category=category,
        original_text=original_text,
        parsed_description=original_text,
        value_constraint=value_constraint,
        temporal_constraint=temporal_constraint,
        is_critical=is_critical,
        order_index=order_index,
    )
    db.add(crit)
    db.flush()
    return crit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_match_age_criterion_via_rule_engine(db_session, make_patient, fake_llm) -> None:
    """Age >= 18 is handled by the deterministic rule engine — no LLM call."""
    patient = make_patient(date_of_birth=date(1970, 1, 1), sex=SexEnum.FEMALE)
    trial = _make_trial(db_session, nct_id="T-AGE")
    _add_criterion(
        db_session, trial.id,
        original_text="Age >= 18 years.",
        category="demographic",
        value_constraint={"metric": "age", "operator": ">=", "value": 18, "unit": "years"},
    )

    result = match_patient_against_trial(db_session, patient, trial)

    assert result.overall_status == OverallMatchStatusEnum.ELIGIBLE.value
    assert result.criteria_met == 1
    assert result.criteria_not_met == 0
    assert result.criteria_uncertain == 0
    # Rule engine short-circuit means the fake LLM never got called.
    assert len(fake_llm.calls) == 0


def test_match_temporal_criterion_via_temporal_engine(
    db_session, make_patient, fake_llm
) -> None:
    """``No chemo within 6 months`` resolves via temporal engine.

    Patient has Cisplatin 365 days ago, well outside the 6-month window,
    so the exclusion is satisfied (criterion status MET) without needing
    the LLM.
    """
    patient = make_patient()
    _add_event(db_session, patient.id, name="Cisplatin", days_ago=365,
               etype=EventTypeEnum.MEDICATION)
    trial = _make_trial(db_session, nct_id="T-TEMPORAL")
    _add_criterion(
        db_session, trial.id,
        original_text="No cisplatin within 6 months.",
        category="medication",
        criterion_type="exclusion",
        temporal_constraint={
            "type": "within", "duration_value": 6, "duration_unit": "months",
        },
    )

    result = match_patient_against_trial(db_session, patient, trial)
    assert result.overall_status == OverallMatchStatusEnum.ELIGIBLE.value
    assert result.criteria_met == 1
    # No LLM call needed.
    assert len(fake_llm.calls) == 0


def test_match_falls_back_to_llm_for_freeform_criterion(
    db_session, make_patient, fake_llm
) -> None:
    """A criterion the rule + temporal engines can't decide gets sent to the LLM."""
    fake_llm.default_json = {
        "status": "eligible",
        "reasoning": "Fake says yes.",
        "confidence": 0.9,
        "evidence_text": "Documented in chart.",
        "evidence_event_index": None,
        "missing_data": None,
    }
    patient = make_patient()
    trial = _make_trial(db_session, nct_id="T-LLM")
    _add_criterion(
        db_session, trial.id,
        original_text="Histologically confirmed adenocarcinoma.",
        category="diagnosis",
    )

    result = match_patient_against_trial(db_session, patient, trial)
    assert result.overall_status == OverallMatchStatusEnum.ELIGIBLE.value
    assert len(fake_llm.calls) == 1


def test_critical_exclusion_violation_forces_ineligible(
    db_session, make_patient, fake_llm
) -> None:
    """One critical-exclusion-MET evaluation is a hard fail for the whole match."""
    fake_llm.default_json = {
        "status": "ineligible",
        "reasoning": "Excluded.",
        "confidence": 1.0,
        "evidence_text": None,
        "evidence_event_index": None,
        "missing_data": None,
    }
    patient = make_patient()
    trial = _make_trial(db_session, nct_id="T-HARDFAIL")
    # Critical exclusion that the LLM rejects -> hard fail
    _add_criterion(
        db_session, trial.id,
        original_text="No pregnancy.",
        category="other",
        criterion_type="exclusion",
        is_critical=True,
        order_index=0,
    )
    # Also a passing inclusion
    _add_criterion(
        db_session, trial.id,
        original_text="Age >= 18 years.",
        category="demographic",
        value_constraint={"metric": "age", "operator": ">=", "value": 18, "unit": "years"},
        order_index=1,
    )

    result = match_patient_against_trial(db_session, patient, trial)
    assert result.overall_status == OverallMatchStatusEnum.INELIGIBLE.value


def test_is_latest_flips_on_re_run(db_session, make_patient, fake_llm) -> None:
    """Re-matching the same pair leaves only one row with ``is_latest=True``."""
    patient = make_patient(date_of_birth=date(1970, 1, 1))
    trial = _make_trial(db_session, nct_id="T-LATEST")
    _add_criterion(
        db_session, trial.id,
        original_text="Age >= 18 years.",
        category="demographic",
        value_constraint={"metric": "age", "operator": ">=", "value": 18, "unit": "years"},
    )

    first = match_patient_against_trial(
        db_session, patient, trial, triggered_by=MatchTriggerEnum.INITIAL_MATCH
    )
    second = match_patient_against_trial(
        db_session, patient, trial, triggered_by=MatchTriggerEnum.CHART_UPDATE
    )

    rows = db_session.execute(
        select(MatchResult).where(
            MatchResult.patient_id == patient.id,
            MatchResult.trial_id == trial.id,
        )
    ).scalars().all()

    assert len(rows) == 2
    assert sum(1 for r in rows if r.is_latest) == 1
    latest = next(r for r in rows if r.is_latest)
    assert latest.id == second.id
    assert latest.id != first.id


def test_recompute_match_counters_reflects_evaluation_updates(
    db_session, make_patient, fake_llm
) -> None:
    """Manual evaluation edit + recompute correctly updates aggregate counts."""
    fake_llm.default_json = {
        "status": "uncertain",
        "reasoning": "Need more info.",
        "confidence": 0.0,
        "evidence_text": None,
        "evidence_event_index": None,
        "missing_data": "Need labs.",
    }
    patient = make_patient()
    trial = _make_trial(db_session, nct_id="T-RECOMP")
    _add_criterion(
        db_session, trial.id,
        original_text="Lab requirement.",
        category="lab_value",
        value_constraint={"metric": "x", "operator": "<", "value": 5},
    )

    match = match_patient_against_trial(db_session, patient, trial)
    assert match.criteria_uncertain == 1
    assert match.overall_status == OverallMatchStatusEnum.UNCERTAIN.value

    # Simulate an out-of-band update (e.g. clinician override).
    ev = match.criterion_evaluations[0]
    ev.status = CriterionStatusEnum.MET.value
    ev.confidence = 0.9

    recompute_match_counters(db_session, match)
    db_session.flush()

    assert match.criteria_met == 1
    assert match.criteria_uncertain == 0
    assert match.overall_status == OverallMatchStatusEnum.ELIGIBLE.value
    assert match.match_score == pytest.approx(1.0)
