"""HIPAA de-identification utilities for outbound LLM prompts.

The eligibility reasoner, criteria parser, and plain-language services
all send chunks of text to a third-party LLM (Groq today).  Even though
those prompts are *mostly* already PHI-free (the reasoner sends an
age/sex/race block, not patient names), there are still leakage
vectors:

* Source documents quoted in evidence chains may include patient
  identifiers if upstream EHRs put them there.
* Free-text criterion descriptions sometimes echo back fragments of
  the patient chart.
* Dates more specific than "year" are PHI under the HIPAA Safe-Harbor
  Method when combined with patient context.

This module gives the rest of the codebase a single helper —
:func:`deidentify_text` — that applies the Safe-Harbor mask + a
patient-specific replacement map, plus :func:`relative_date` for the
date-redaction case.

The companion :class:`PHIScrubber` keeps a session-scoped re-
identification map so downstream code can pretty-print the LLM's
response with real names restored.

When to call this
-----------------
Today only the LLM-touching services should call it; structured
database writes do not.  Toggle the whole system off via
``settings.ENABLE_PHI_DEIDENTIFICATION``.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.config import get_settings
from app.models.patient import Patient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Static patterns
# ---------------------------------------------------------------------------

# SSN — XXX-XX-XXXX with optional spaces / unseparated forms.
_SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")

# US phone numbers — keeps toll-free + extension shapes too.  We use
# ``(?<!\w)`` / ``(?!\w)`` instead of ``\b`` because ``\b`` fails to
# fire next to ``(`` and ``)`` (non-word chars), which would leave
# orphaned parentheses behind on ``(555) 123-4567``-style numbers.
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\w)"
)

# E-mail addresses.
_EMAIL_RE = re.compile(r"\b[\w._%+\-]+@[\w.\-]+\.[A-Za-z]{2,}\b")

# Common medical-record-number patterns: MRN: 1234567, MRN 1234567,
# "Medical Record Number 12345678", etc.
_MRN_RE = re.compile(
    r"\b(?:MRN|Medical\s+Record\s+Number)[\s:#]*([A-Z0-9\-]{4,})\b",
    re.IGNORECASE,
)

# ISO-style absolute dates (YYYY-MM-DD).  Matches dates more specific
# than year — the Safe-Harbor threshold.
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")

# US-style absolute dates (MM/DD/YYYY).
_DATE_SLASH_RE = re.compile(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(\d{4})\b")


# ---------------------------------------------------------------------------
# Relative-date helper
# ---------------------------------------------------------------------------

def relative_date(value: date | datetime, *, reference: Optional[date | datetime] = None) -> str:
    """Render a date as a Safe-Harbor-compliant relative duration.

    Examples:
        - 30 days ago → ``"about 4 weeks ago"``
        - 200 days ago → ``"about 7 months ago"``
        - 800 days ago → ``"about 2 years ago"``
        - future → ``"in about 3 weeks"``
    """
    today = (reference or datetime.now(timezone.utc)).date() if isinstance(reference, datetime) \
        else reference or date.today()
    actual = value.date() if isinstance(value, datetime) else value
    delta = (today - actual).days

    suffix = "ago" if delta >= 0 else "from now"
    abs_delta = abs(delta)

    if abs_delta < 7:
        amount, unit = abs_delta, "day"
    elif abs_delta < 60:
        amount, unit = round(abs_delta / 7), "week"
    elif abs_delta < 730:
        amount, unit = round(abs_delta / 30.4375), "month"
    else:
        amount, unit = round(abs_delta / 365.25), "year"

    plural = "" if amount == 1 else "s"
    return f"about {amount} {unit}{plural} {suffix}"


# ---------------------------------------------------------------------------
# Replacement map for patient-specific identifiers
# ---------------------------------------------------------------------------

@dataclass
class _Replacement:
    """One de-identification substitution + the token it maps to."""

    original: str
    token: str


@dataclass
class PHIScrubber:
    """Session-scoped scrubber that *remembers* its substitutions.

    Patterns:

    * The class is created once per LLM-touching operation
      (eligibility evaluation, plain-language summary, …) seeded with
      the patient being discussed.
    * Calls to :meth:`scrub` return a redacted version of the text.
    * Calls to :meth:`restore` reverse the substitution on text the LLM
      sends back — useful when we want to display the model's reasoning
      with the patient's real identifiers re-inserted server-side.

    The map is in-memory only; it never crosses process boundaries.
    """

    patient_id: Optional[uuid.UUID] = None
    _replacements: list[_Replacement] = field(default_factory=list)

    @classmethod
    def for_patient(cls, patient: Optional[Patient]) -> "PHIScrubber":
        """Pre-populate the scrubber with patient-specific tokens."""
        scrubber = cls(patient_id=patient.id if patient else None)
        if patient is None:
            return scrubber
        # Build the most-specific substitutions first so longer
        # multi-word names don't get clobbered by single-word ones.
        if patient.first_name and patient.last_name:
            scrubber._add(
                f"{patient.first_name} {patient.last_name}",
                "[PATIENT_FULL_NAME]",
            )
        if patient.last_name:
            scrubber._add(patient.last_name, "[PATIENT_LAST_NAME]")
        if patient.first_name:
            scrubber._add(patient.first_name, "[PATIENT_FIRST_NAME]")
        if patient.external_id:
            scrubber._add(patient.external_id, "[PATIENT_EXTERNAL_ID]")
        if patient.date_of_birth:
            scrubber._add(
                patient.date_of_birth.isoformat(),
                "[PATIENT_DOB_REDACTED]",
            )
        return scrubber

    # ── Public API ───────────────────────────────────────────────────

    def scrub(self, text: Optional[str]) -> Optional[str]:
        """Apply every replacement + static regex to ``text``.

        Returns ``None`` unchanged so callers can pipe Optional values.
        """
        if not text:
            return text
        if not get_settings().ENABLE_PHI_DEIDENTIFICATION:
            return text

        out = text
        for r in self._replacements:
            if r.original and r.original in out:
                out = out.replace(r.original, r.token)

        out = _SSN_RE.sub("[SSN]", out)
        out = _PHONE_RE.sub("[PHONE]", out)
        out = _EMAIL_RE.sub("[EMAIL]", out)
        out = _MRN_RE.sub(r"[MRN]", out)
        out = _DATE_ISO_RE.sub(self._iso_date_token, out)
        out = _DATE_SLASH_RE.sub(self._slash_date_token, out)
        return out

    def restore(self, text: Optional[str]) -> Optional[str]:
        """Reverse the replacement substitutions (not the static regex).

        Use this on LLM output before persisting / displaying so the
        clinician's view shows the real patient name.  Generic tokens
        like ``[SSN]`` are intentionally *not* restored — the LLM was
        only ever shown the token, so its response can't contain an
        SSN we'd need to restore.
        """
        if not text:
            return text
        out = text
        for r in self._replacements:
            if r.token in out:
                out = out.replace(r.token, r.original)
        return out

    # ── Internal ─────────────────────────────────────────────────────

    def _add(self, original: str, token: str) -> None:
        """Add a substitution, longest-first ordering preserved."""
        cleaned = original.strip()
        if not cleaned:
            return
        self._replacements.append(_Replacement(original=cleaned, token=token))
        # Re-sort so longest patterns get matched first on the next scrub.
        self._replacements.sort(key=lambda r: len(r.original), reverse=True)

    @staticmethod
    def _iso_date_token(match: "re.Match[str]") -> str:
        try:
            parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return "[DATE]"
        return relative_date(parsed)

    @staticmethod
    def _slash_date_token(match: "re.Match[str]") -> str:
        try:
            parsed = date(int(match.group(3)), int(match.group(1)), int(match.group(2)))
        except ValueError:
            return "[DATE]"
        return relative_date(parsed)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def deidentify_text(text: Optional[str], patient: Optional[Patient] = None) -> Optional[str]:
    """One-shot helper for the common "scrub this once" case.

    For long-lived LLM operations that bounce text back and forth (and
    therefore need :meth:`PHIScrubber.restore`) instantiate the scrubber
    explicitly via :meth:`PHIScrubber.for_patient`.
    """
    return PHIScrubber.for_patient(patient).scrub(text)
