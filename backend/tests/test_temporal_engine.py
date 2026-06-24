"""Unit tests for :mod:`app.services.temporal_engine`.

Pure-Python — no DB, no LLM, no fixtures from ``conftest`` needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.patient import EventStatusEnum, EventTypeEnum
from app.services.temporal_engine import (
    PatientTimeline,
    TemporalVerdict,
    TimelineEntry,
    evaluate_temporal_constraint,
    reconstruct_timeline,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _entry(
    name: str,
    days_ago: int,
    event_type: EventTypeEnum = EventTypeEnum.MEDICATION,
    status: EventStatusEnum = EventStatusEnum.ACTIVE,
) -> TimelineEntry:
    """Build a :class:`TimelineEntry` with the date relative to now."""
    source = type("FakeEvent", (), {})()
    return TimelineEntry(
        id=str(uuid.uuid4()),
        event_type=event_type,
        event_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
        end_date=None,
        code=None,
        code_system=None,
        display_name=name,
        value=None,
        unit=None,
        status=status,
        source_event=source,
    )


# ---------------------------------------------------------------------------
# within / exclusion
# ---------------------------------------------------------------------------

def test_within_exclusion_finds_recent_event() -> None:
    """No chemo within 6 months: chemo 4mo ago → NOT_MET."""
    tl = PatientTimeline(patient_id="p", entries=[_entry("Cisplatin", 120)])
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "within", "duration_value": 6, "duration_unit": "months"},
        criterion_type="exclusion",
        subject_category="medication",
        subject_keywords=["cisplatin"],
    )
    assert verdict.verdict == TemporalVerdict.NOT_MET
    assert verdict.evidence_entries  # cited the chemo event
    assert "6.0 months" in verdict.reasoning or "6 months" in verdict.reasoning


def test_within_exclusion_passes_when_event_too_old() -> None:
    """Chemo > 6 months ago: exclusion satisfied → MET."""
    tl = PatientTimeline(patient_id="p", entries=[_entry("Cisplatin", 365)])
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "within", "duration_value": 6, "duration_unit": "months"},
        criterion_type="exclusion",
        subject_category="medication",
        subject_keywords=["cisplatin"],
    )
    assert verdict.verdict == TemporalVerdict.MET


def test_within_uncertain_when_no_data() -> None:
    """No medication data → UNCERTAIN, not MET / NOT_MET."""
    tl = PatientTimeline(
        patient_id="p",
        entries=[_entry("HbA1c", 30, EventTypeEnum.LAB_RESULT)],
    )
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "within", "duration_value": 6, "duration_unit": "months"},
        criterion_type="exclusion",
        subject_category="medication",
    )
    assert verdict.verdict == TemporalVerdict.UNCERTAIN
    assert "no medication events" in verdict.reasoning.lower()


# ---------------------------------------------------------------------------
# at_least_ago / inclusion
# ---------------------------------------------------------------------------

def test_at_least_ago_inclusion_met() -> None:
    """Diagnosis ≥ 12 weeks ago when actual dx is 200 days old → MET."""
    tl = PatientTimeline(
        patient_id="p",
        entries=[_entry("Breast cancer", 200, EventTypeEnum.DIAGNOSIS)],
    )
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "at_least_ago", "duration_value": 12, "duration_unit": "weeks"},
        criterion_type="inclusion",
        subject_category="diagnosis",
    )
    assert verdict.verdict == TemporalVerdict.MET


def test_at_least_ago_inclusion_not_met() -> None:
    """Diagnosis 30 days old but criterion requires 12 weeks → NOT_MET."""
    tl = PatientTimeline(
        patient_id="p",
        entries=[_entry("Breast cancer", 30, EventTypeEnum.DIAGNOSIS)],
    )
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "at_least_ago", "duration_value": 12, "duration_unit": "weeks"},
        criterion_type="inclusion",
        subject_category="diagnosis",
    )
    assert verdict.verdict == TemporalVerdict.NOT_MET


# ---------------------------------------------------------------------------
# Timeline reconstruction
# ---------------------------------------------------------------------------

def test_reconstruct_timeline_sorts_chronologically() -> None:
    """Events come back sorted by ``event_date`` ascending."""

    class FakeEvent:
        def __init__(self, **kw: object) -> None:
            for k, v in kw.items():
                setattr(self, k, v)

    events = [
        FakeEvent(
            id=uuid.uuid4(), event_type=EventTypeEnum.LAB_RESULT.value,
            event_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            end_date=None, code=None, code_system=None,
            display_name="HbA1c", value="7.2", unit="%",
            status=EventStatusEnum.ACTIVE.value,
        ),
        FakeEvent(
            id=uuid.uuid4(), event_type=EventTypeEnum.DIAGNOSIS.value,
            event_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
            end_date=None, code=None, code_system=None,
            display_name="T2DM", value=None, unit=None,
            status=EventStatusEnum.ACTIVE.value,
        ),
    ]
    tl = reconstruct_timeline("p", events)
    assert [e.display_name for e in tl.entries] == ["T2DM", "HbA1c"]


def test_timeline_filters_by_type() -> None:
    tl = PatientTimeline(
        patient_id="p",
        entries=[
            _entry("Cisplatin", 30, EventTypeEnum.MEDICATION),
            _entry("HbA1c", 30, EventTypeEnum.LAB_RESULT),
        ],
    )
    meds = tl.of_type(EventTypeEnum.MEDICATION)
    assert len(meds) == 1
    assert meds[0].display_name == "Cisplatin"


def test_timeline_finds_gaps() -> None:
    tl = PatientTimeline(
        patient_id="p",
        entries=[
            _entry("Event A", 400, EventTypeEnum.LAB_RESULT),
            _entry("Event B", 30, EventTypeEnum.LAB_RESULT),
        ],
    )
    gaps = tl.find_gaps(min_gap_days=90.0)
    assert len(gaps) == 1
    # Gap is ~370 days — well above the threshold.
    assert gaps[0].days > 350


# ---------------------------------------------------------------------------
# Malformed constraint shapes
# ---------------------------------------------------------------------------

def test_unknown_constraint_type_returns_uncertain() -> None:
    tl = PatientTimeline(patient_id="p", entries=[_entry("X", 5)])
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "frobnicate", "duration_value": 3, "duration_unit": "days"},
        criterion_type="inclusion",
        subject_category="medication",
    )
    assert verdict.verdict == TemporalVerdict.UNCERTAIN
    assert "unknown" in verdict.reasoning.lower()


def test_missing_duration_returns_uncertain() -> None:
    tl = PatientTimeline(patient_id="p", entries=[_entry("X", 5)])
    verdict = evaluate_temporal_constraint(
        tl,
        constraint={"type": "within"},  # no duration
        criterion_type="inclusion",
    )
    assert verdict.verdict == TemporalVerdict.UNCERTAIN
