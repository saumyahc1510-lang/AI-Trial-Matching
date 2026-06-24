"""Temporal reasoning over patient timelines.

The temporal engine is the first half of Phase 3's intelligence core.
It does two things:

1. **Timeline reconstruction.**  Given a patient's :class:`MedicalEvent`
   rows, it produces a chronologically-ordered, gap-aware view —
   :class:`PatientTimeline` — that downstream services treat as a single
   source of truth instead of poking at the DB directly.

2. **Temporal criterion evaluation.**  Given a parsed
   :class:`TrialCriterion` whose ``temporal_constraint`` field is set
   (e.g. *"no chemotherapy within 6 months"*), it scans the timeline and
   returns one of :class:`TemporalVerdict` — ``met`` / ``not_met`` /
   ``uncertain`` — along with the events used as evidence.

Supported constraint shapes
---------------------------
The criteria parser emits ``temporal_constraint`` blobs of this shape::

    {
      "type": "within"          # event must have occurred within window
            | "at_least_ago"    # event must be older than window
            | "stable_for"      # status unchanged across window
            | "duration"        # event spanned at least N units
            | "since",          # event recorded since reference date
      "duration_value": <number>,
      "duration_unit": "days" | "weeks" | "months" | "years",
      "reference":      "now" | "enrollment" | "diagnosis" | "treatment_start"
    }

The engine pairs that with a *subject* — either the criterion's
``category`` (e.g. ``medication``, ``diagnosis``) or an explicit
``code``/``display`` keyword — and decides whether the rule holds.

Design notes
------------
* **No LLM calls here.**  The temporal engine is fast, deterministic,
  and unit-testable.  It feeds the LLM later when the eligibility
  reasoner needs a hint about what evidence exists.
* **Three-state output.**  We are explicit about *uncertain* —
  e.g. "no chemo in last 6 months" is uncertain (not "met") when we
  have *no* medication data at all for that window.  This is critical
  for the matching engine's confidence scoring.
* **Gap detection.**  Long stretches without records are surfaced so the
  reasoner can flag them as missing-data uncertainty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Optional

from app.models.patient import EventStatusEnum, EventTypeEnum, MedicalEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict enum
# ---------------------------------------------------------------------------

class TemporalVerdict(str, Enum):
    """Three-state outcome of a temporal criterion evaluation.

    Mirrors :class:`~app.models.matching.CriterionStatusEnum` so the
    matching engine can copy it straight through without translation.
    """

    MET = "met"
    NOT_MET = "not_met"
    UNCERTAIN = "uncertain"


# ---------------------------------------------------------------------------
# Timeline reconstruction
# ---------------------------------------------------------------------------

@dataclass
class TimelineEntry:
    """A single normalised event on the patient timeline.

    Wraps a :class:`MedicalEvent` row so downstream callers don't have to
    know about SQLAlchemy.  ``event_date`` is always tz-aware UTC.
    """

    id: str
    event_type: EventTypeEnum
    event_date: datetime
    end_date: Optional[datetime]
    code: Optional[str]
    code_system: Optional[str]
    display_name: str
    value: Optional[str]
    unit: Optional[str]
    status: EventStatusEnum
    source_event: MedicalEvent

    @property
    def days_ago(self) -> Optional[float]:
        """Days between ``event_date`` and the current instant."""
        now = datetime.now(timezone.utc)
        delta = now - self.event_date
        return delta.total_seconds() / 86400.0


@dataclass
class TimelineGap:
    """A period with no recorded events.

    The reasoner uses these to detect "missing data" uncertainty —
    e.g. a 14-month gap in lab results when the criterion needs the
    "most recent HbA1c".
    """

    start: datetime
    end: datetime

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0


@dataclass
class PatientTimeline:
    """Patient's medical history as a chronologically-ordered timeline."""

    patient_id: str
    entries: list[TimelineEntry] = field(default_factory=list)
    reconstructed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ── Convenience filters ──────────────────────────────────────────

    def of_type(self, *types: EventTypeEnum) -> list[TimelineEntry]:
        """Return entries whose ``event_type`` is in ``types``."""
        wanted = {t.value if isinstance(t, EventTypeEnum) else t for t in types}
        return [e for e in self.entries if e.event_type.value in wanted]

    def matching(self, keywords: Iterable[str]) -> list[TimelineEntry]:
        """Return entries whose ``display_name`` or ``code`` matches any keyword.

        Case-insensitive substring match.  Empty keyword list returns
        every entry — callers should pre-filter when that isn't useful.
        """
        terms = [k.lower() for k in keywords if k]
        if not terms:
            return list(self.entries)
        out: list[TimelineEntry] = []
        for entry in self.entries:
            hay = " ".join(
                filter(None, [entry.display_name or "", entry.code or ""])
            ).lower()
            if any(term in hay for term in terms):
                out.append(entry)
        return out

    def latest(
        self,
        types: Optional[Iterable[EventTypeEnum]] = None,
    ) -> Optional[TimelineEntry]:
        """Return the most-recent entry (optionally filtered by type)."""
        entries = self.of_type(*types) if types else self.entries
        return entries[-1] if entries else None

    def find_gaps(
        self,
        *,
        types: Optional[Iterable[EventTypeEnum]] = None,
        min_gap_days: float = 90.0,
    ) -> list[TimelineGap]:
        """Detect runs of ``>= min_gap_days`` between consecutive events."""
        entries = self.of_type(*types) if types else self.entries
        gaps: list[TimelineGap] = []
        for prev, curr in zip(entries, entries[1:]):
            delta = (curr.event_date - prev.event_date).total_seconds() / 86400.0
            if delta >= min_gap_days:
                gaps.append(TimelineGap(start=prev.event_date, end=curr.event_date))
        return gaps


def reconstruct_timeline(
    patient_id: str,
    events: Iterable[MedicalEvent],
) -> PatientTimeline:
    """Build a :class:`PatientTimeline` from a patient's MedicalEvent rows.

    The reconstruction is order-preserving and side-effect-free, so it
    can be reused by tests that don't touch the database.
    """
    entries: list[TimelineEntry] = []
    for evt in events:
        if evt.event_date is None:
            # Defensive — schema requires non-null but bad data happens.
            continue
        entries.append(
            TimelineEntry(
                id=str(evt.id),
                event_type=EventTypeEnum(evt.event_type),
                event_date=_ensure_aware(evt.event_date),
                end_date=_ensure_aware(evt.end_date),
                code=evt.code,
                code_system=evt.code_system,
                display_name=evt.display_name,
                value=evt.value,
                unit=evt.unit,
                status=EventStatusEnum(evt.status) if evt.status else EventStatusEnum.ACTIVE,
                source_event=evt,
            )
        )
    entries.sort(key=lambda e: e.event_date)
    return PatientTimeline(patient_id=patient_id, entries=entries)


def _ensure_aware(value: Optional[datetime]) -> Optional[datetime]:
    """Treat naive datetimes as UTC; pass through aware values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Temporal-constraint evaluation
# ---------------------------------------------------------------------------

# Map ``duration_unit`` → days-per-unit.  Months/years are approximations
# — clinical criteria like "within 6 months" don't need calendar
# precision; ±2 days is well within trial protocol tolerance.
_UNIT_DAYS: dict[str, float] = {
    "day":   1.0,
    "days":  1.0,
    "week":  7.0,
    "weeks": 7.0,
    "month":  30.4375,
    "months": 30.4375,
    "year":  365.25,
    "years": 365.25,
}


@dataclass
class TemporalEvaluation:
    """Result of evaluating one temporal criterion against the timeline.

    Attributes:
        verdict:           ``met`` / ``not_met`` / ``uncertain``.
        reasoning:         Plain-English explanation suitable for audit.
        evidence_entries:  Timeline entries that drove the decision.
        evaluated_at:      Reference instant used as "now".
    """

    verdict: TemporalVerdict
    reasoning: str
    evidence_entries: list[TimelineEntry] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _window_days(constraint: dict[str, Any]) -> Optional[float]:
    """Convert a constraint's duration to a day count."""
    value = constraint.get("duration_value")
    unit = (constraint.get("duration_unit") or "days").lower()
    if value is None:
        return None
    try:
        return float(value) * _UNIT_DAYS.get(unit, 1.0)
    except (TypeError, ValueError):
        return None


# Category → which event types are relevant when the criterion doesn't
# specify a more precise subject.  Used by ``evaluate_temporal_constraint``
# to pre-filter the timeline before applying the window.
_CATEGORY_TO_TYPES: dict[str, tuple[EventTypeEnum, ...]] = {
    "medication": (EventTypeEnum.MEDICATION,),
    "diagnosis":  (EventTypeEnum.DIAGNOSIS,),
    "procedure":  (EventTypeEnum.PROCEDURE,),
    "lab_value":  (EventTypeEnum.LAB_RESULT,),
    "vital_sign": (EventTypeEnum.VITAL_SIGN,),
}


def evaluate_temporal_constraint(
    timeline: PatientTimeline,
    constraint: dict[str, Any],
    *,
    criterion_type: str = "inclusion",
    subject_category: Optional[str] = None,
    subject_keywords: Optional[list[str]] = None,
    reference_now: Optional[datetime] = None,
) -> TemporalEvaluation:
    """Decide whether a temporal constraint holds against the timeline.

    Args:
        timeline:           Reconstructed patient timeline.
        constraint:         The criterion's ``temporal_constraint`` JSON.
        criterion_type:     ``"inclusion"`` or ``"exclusion"``.  Used to
            interpret negative-shape rules — e.g. "no chemo within 6
            months" is encoded as ``type=within`` *exclusion*.
        subject_category:   The criterion's category (e.g.
            ``"medication"``) — used to narrow the timeline.
        subject_keywords:   Optional list of display-name keywords for a
            tighter subject filter (e.g. ``["chemo", "cisplatin"]``).
        reference_now:      Override the reference instant (handy for
            tests).  Defaults to :func:`datetime.now`.

    The verdict semantics:

    +----------------+------------------------------+--------------------------+
    | constraint     | "matches found in window"    | "no matches in window"   |
    +================+==============================+==========================+
    | inclusion      | MET                          | NOT_MET / UNCERTAIN      |
    | exclusion      | NOT_MET                      | MET / UNCERTAIN          |
    +----------------+------------------------------+--------------------------+

    The UNCERTAIN branch is taken when we have *no relevant data at all*
    for the subject — the criterion can't be resolved without more
    information.
    """
    now = _ensure_aware(reference_now) or datetime.now(timezone.utc)
    ctype = (constraint.get("type") or "").lower()
    days = _window_days(constraint)
    if days is None or not ctype:
        return TemporalEvaluation(
            verdict=TemporalVerdict.UNCERTAIN,
            reasoning="Temporal constraint is missing a duration or type.",
        )

    # Narrow the timeline to the criterion's subject.
    relevant: list[TimelineEntry]
    if subject_keywords:
        relevant = timeline.matching(subject_keywords)
    elif subject_category and subject_category in _CATEGORY_TO_TYPES:
        relevant = timeline.of_type(*_CATEGORY_TO_TYPES[subject_category])
    else:
        relevant = list(timeline.entries)

    # ── No data at all for the subject — uncertain ────────────────────
    if not relevant:
        scope = subject_category or "any relevant"
        return TemporalEvaluation(
            verdict=TemporalVerdict.UNCERTAIN,
            reasoning=(
                f"No {scope} events found in the timeline; cannot evaluate "
                f"the '{ctype}' constraint."
            ),
        )

    window_start = now - timedelta(days=days)
    is_exclusion = criterion_type.lower() == "exclusion"

    in_window = [e for e in relevant if window_start <= e.event_date <= now]
    older_than_window = [e for e in relevant if e.event_date < window_start]

    if ctype == "within":
        # "X happened within the last N units."
        return _verdict_for_within(in_window, days, is_exclusion, subject_category)
    if ctype == "at_least_ago":
        # "X happened at least N units ago" → matching entries must be
        # OLDER than the window.
        return _verdict_for_at_least_ago(
            older_than_window, in_window, days, is_exclusion, subject_category
        )
    if ctype == "since":
        # "X happened since [reference date]" — for now treat as "within".
        return _verdict_for_within(in_window, days, is_exclusion, subject_category)
    if ctype == "stable_for":
        return _verdict_for_stable_for(relevant, window_start, days, subject_category)
    if ctype == "duration":
        return _verdict_for_duration(relevant, days, is_exclusion, subject_category)

    return TemporalEvaluation(
        verdict=TemporalVerdict.UNCERTAIN,
        reasoning=f"Unknown temporal constraint type: {ctype!r}.",
    )


# ── Per-shape verdict helpers ────────────────────────────────────────────

def _format_days(days: float) -> str:
    """Render a day count as a human-friendly duration string."""
    if days >= 365:
        return f"{days / 365.25:.1f} years"
    if days >= 30:
        return f"{days / 30.4375:.1f} months"
    if days >= 7:
        return f"{days / 7:.1f} weeks"
    return f"{days:.0f} days"


def _verdict_for_within(
    in_window: list[TimelineEntry],
    days: float,
    is_exclusion: bool,
    subject: Optional[str],
) -> TemporalEvaluation:
    """Evaluate a *within N units* constraint.

    Inclusion: passes when there's at least one matching event in window.
    Exclusion: passes when there's *no* matching event in window.
    """
    subject_label = subject or "event"
    duration = _format_days(days)
    if in_window:
        if is_exclusion:
            return TemporalEvaluation(
                verdict=TemporalVerdict.NOT_MET,
                reasoning=(
                    f"Found {len(in_window)} {subject_label}(s) within the "
                    f"last {duration}: violates the 'no … within {duration}' "
                    f"exclusion."
                ),
                evidence_entries=in_window,
            )
        return TemporalEvaluation(
            verdict=TemporalVerdict.MET,
            reasoning=(
                f"Found {len(in_window)} {subject_label}(s) within the last "
                f"{duration}."
            ),
            evidence_entries=in_window,
        )
    # No events in window.
    if is_exclusion:
        return TemporalEvaluation(
            verdict=TemporalVerdict.MET,
            reasoning=(
                f"No {subject_label} events recorded in the last {duration}; "
                f"exclusion satisfied."
            ),
        )
    return TemporalEvaluation(
        verdict=TemporalVerdict.NOT_MET,
        reasoning=(
            f"No {subject_label} events recorded in the last {duration}; "
            f"inclusion not satisfied."
        ),
    )


def _verdict_for_at_least_ago(
    older: list[TimelineEntry],
    in_window: list[TimelineEntry],
    days: float,
    is_exclusion: bool,
    subject: Optional[str],
) -> TemporalEvaluation:
    """Evaluate an *at least N units ago* constraint."""
    subject_label = subject or "event"
    duration = _format_days(days)
    if older:
        verdict = TemporalVerdict.NOT_MET if is_exclusion else TemporalVerdict.MET
        return TemporalEvaluation(
            verdict=verdict,
            reasoning=(
                f"Found {len(older)} {subject_label}(s) older than {duration}; "
                f"satisfies the 'at least {duration} ago' rule."
            ),
            evidence_entries=older[-3:],  # most-recent of the qualifying set
        )
    # No older events.  If we do have recent events the rule fails;
    # if we have no events at all, uncertainty is more honest.
    if in_window:
        verdict = TemporalVerdict.MET if is_exclusion else TemporalVerdict.NOT_MET
        return TemporalEvaluation(
            verdict=verdict,
            reasoning=(
                f"All {subject_label} events occurred within the last "
                f"{duration}; the criterion required an event older than that."
            ),
            evidence_entries=in_window,
        )
    return TemporalEvaluation(
        verdict=TemporalVerdict.UNCERTAIN,
        reasoning=(
            f"No {subject_label} events recorded; cannot confirm whether any "
            f"is at least {duration} old."
        ),
    )


def _verdict_for_stable_for(
    relevant: list[TimelineEntry],
    window_start: datetime,
    days: float,
    subject: Optional[str],
) -> TemporalEvaluation:
    """Evaluate a *stable for N units* constraint.

    Interpreted as: no *status-changing* events in the window.  A
    resolved/relapse/recurrence entry breaks stability.
    """
    duration = _format_days(days)
    breakers = [
        e for e in relevant
        if e.event_date >= window_start
        and e.status in (
            EventStatusEnum.RESOLVED,
            EventStatusEnum.INACTIVE,
            EventStatusEnum.ENTERED_IN_ERROR,
        )
    ]
    if breakers:
        return TemporalEvaluation(
            verdict=TemporalVerdict.NOT_MET,
            reasoning=(
                f"Status-changing event(s) detected within the last {duration}; "
                f"stability requirement not met."
            ),
            evidence_entries=breakers,
        )
    subject_label = subject or "subject"
    return TemporalEvaluation(
        verdict=TemporalVerdict.MET,
        reasoning=(
            f"No status changes recorded for {subject_label} in the last "
            f"{duration}; stability requirement satisfied."
        ),
        evidence_entries=relevant[-3:],
    )


def _verdict_for_duration(
    relevant: list[TimelineEntry],
    days: float,
    is_exclusion: bool,
    subject: Optional[str],
) -> TemporalEvaluation:
    """Evaluate a *spans at least N units* constraint.

    Looks for an event with a long enough ``event_date → end_date`` span,
    or — failing that — a series of contiguous active entries that
    together cover the requested duration.
    """
    duration = _format_days(days)
    subject_label = subject or "event"
    for entry in relevant:
        if entry.end_date is None:
            continue
        span = (entry.end_date - entry.event_date).total_seconds() / 86400.0
        if span >= days:
            verdict = TemporalVerdict.NOT_MET if is_exclusion else TemporalVerdict.MET
            return TemporalEvaluation(
                verdict=verdict,
                reasoning=(
                    f"{subject_label!r} event spans {_format_days(span)}, "
                    f"meeting the ≥ {duration} duration requirement."
                ),
                evidence_entries=[entry],
            )
    return TemporalEvaluation(
        verdict=TemporalVerdict.UNCERTAIN,
        reasoning=(
            f"Could not confirm any {subject_label} event lasting at least "
            f"{duration}; check end-dates / continuous coverage."
        ),
    )
