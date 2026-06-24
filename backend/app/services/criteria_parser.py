"""LLM-powered eligibility-criteria parser.

ClinicalTrials.gov stores eligibility as a single free-text block:

    Inclusion Criteria:
    * Age ≥ 18 years.
    * Histologically confirmed invasive ductal carcinoma.
    * ECOG performance status 0–1.

    Exclusion Criteria:
    * Prior chemotherapy within 6 months.
    * Pregnancy or lactation.

This service splits that block by section, then asks an LLM to convert
each bullet into a structured :class:`TrialCriterion` row with optional
``temporal_constraint`` and ``value_constraint`` payloads.

Why an LLM and not regex
------------------------
Regex covers maybe 60% of criteria cleanly.  The remaining 40% phrase
the same constraints in 5+ different ways (``ECOG 0-1`` vs.
``Performance status of 0 or 1`` vs. ``Eastern Cooperative Oncology Group
score ≤ 1``).  An LLM with a few-shot prompt handles these uniformly.

Cost shape
----------
Free tier: ~28 requests/minute.  We parse criteria one bullet at a time
so a 30-criterion trial takes ~65 s.  Acceptable for an async Celery
job; the matching engine never calls this on the request path.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.trial import (
    ClinicalTrial,
    CriterionCategoryEnum,
    CriterionTypeEnum,
    TrialCriterion,
)
from app.services.llm_client import (
    LLMClient,
    LLMError,
    LLMResponseParseError,
    get_llm_client,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sectioning the raw eligibility block
# ---------------------------------------------------------------------------

# Section headers commonly used by trial protocols.  Matched case-
# insensitively and tolerant of trailing punctuation.
_SECTION_RE = re.compile(
    r"^\s*(?:[*\-•]\s*)?(?P<label>inclusion criteria|exclusion criteria|criteria)\s*[:：]?\s*$",
    re.IGNORECASE,
)

# Bullet markers that prefix individual criteria.
_BULLET_RE = re.compile(r"^\s*(?:[*\-•·]|\d+\.)\s+")


@dataclass
class _Section:
    kind: CriterionTypeEnum
    bullets: list[str] = field(default_factory=list)


def _split_into_sections(text: str) -> list[_Section]:
    """Split a CT.gov eligibility block into inclusion / exclusion lists.

    The function is deliberately lenient: if no explicit section header
    is found, the entire block is treated as inclusion criteria.  Bullets
    that span multiple lines are joined back together.
    """
    if not text:
        return []

    current: Optional[_Section] = None
    sections: list[_Section] = []
    buffer: list[str] = []

    def _flush_buffer() -> None:
        """Push the in-progress bullet into the current section."""
        if not buffer or current is None:
            return
        joined = " ".join(buffer).strip()
        if joined:
            current.bullets.append(joined)
        buffer.clear()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            _flush_buffer()
            continue

        section_match = _SECTION_RE.match(line)
        if section_match:
            _flush_buffer()
            label = section_match.group("label").lower()
            if "exclusion" in label:
                kind = CriterionTypeEnum.EXCLUSION
            else:
                kind = CriterionTypeEnum.INCLUSION
            current = _Section(kind=kind)
            sections.append(current)
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            _flush_buffer()
            if current is None:
                current = _Section(kind=CriterionTypeEnum.INCLUSION)
                sections.append(current)
            buffer.append(line[bullet_match.end():].strip())
        else:
            # Continuation of the previous bullet — accumulate.
            if current is None:
                current = _Section(kind=CriterionTypeEnum.INCLUSION)
                sections.append(current)
            buffer.append(line.strip())

    _flush_buffer()
    return sections


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert clinical-trial coordinator who reads eligibility criteria \
and converts them into structured JSON.  You always return a single JSON \
object that conforms exactly to the requested schema — no commentary, no \
markdown fences.
"""

_USER_PROMPT_TEMPLATE = """\
Convert the following clinical-trial eligibility criterion into a JSON object.

Criterion text:
\"\"\"{text}\"\"\"

Criterion type: {criterion_type}

Return a JSON object with EXACTLY these keys:

{{
  "category": one of [
      "demographic", "diagnosis", "lab_value", "medication", "procedure",
      "temporal", "lifestyle", "genetic", "organ_function", "other"
  ],
  "parsed_description": short plain-English restatement of the criterion,
  "is_critical": true if failing this criterion is a hard disqualifier, else false,
  "temporal_constraint": null OR object with shape
      {{
        "type": "within" | "at_least_ago" | "stable_for" | "duration" | "since",
        "duration_value": number,
        "duration_unit": "days" | "weeks" | "months" | "years",
        "reference": "now" | "enrollment" | "diagnosis" | "treatment_start"
      }},
  "value_constraint": null OR object with shape
      {{
        "metric": short metric name (e.g. "HbA1c", "ECOG", "age"),
        "operator": "<" | "<=" | ">" | ">=" | "==" | "!=" | "range",
        "value": number or array of two numbers when operator is "range",
        "unit": measurement unit or null
      }}
}}

If the criterion has no temporal component, set "temporal_constraint" to null.
If it has no numeric threshold, set "value_constraint" to null.
Return only the JSON object — no additional text.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ParsedCriterion:
    """Structured criterion produced by the LLM."""

    original_text: str
    criterion_type: CriterionTypeEnum
    category: CriterionCategoryEnum
    parsed_description: Optional[str]
    is_critical: bool
    temporal_constraint: Optional[dict[str, Any]]
    value_constraint: Optional[dict[str, Any]]


@dataclass
class ParseStats:
    """Stats returned to the caller after :func:`parse_trial_criteria`."""

    trial_id: str
    bullets_seen: int = 0
    criteria_parsed: int = 0
    criteria_failed: int = 0
    inclusion_count: int = 0
    exclusion_count: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_ALLOWED_CATEGORIES = {c.value for c in CriterionCategoryEnum}


def _coerce_category(raw: Any) -> CriterionCategoryEnum:
    """Coerce a free-form category string into a known enum value."""
    if isinstance(raw, str):
        candidate = raw.strip().lower().replace("-", "_").replace(" ", "_")
        if candidate in _ALLOWED_CATEGORIES:
            return CriterionCategoryEnum(candidate)
    return CriterionCategoryEnum.OTHER


def _sanitize_constraint(value: Any) -> Optional[dict[str, Any]]:
    """Return ``value`` if it's a non-empty dict, else None."""
    if isinstance(value, dict) and value:
        return value
    return None


# ---------------------------------------------------------------------------
# Single-bullet parse
# ---------------------------------------------------------------------------

def _parse_bullet(
    bullet: str,
    criterion_type: CriterionTypeEnum,
    *,
    client: LLMClient,
) -> ParsedCriterion:
    """Ask the LLM to structure a single eligibility bullet."""
    prompt = _USER_PROMPT_TEMPLATE.format(
        text=bullet,
        criterion_type=criterion_type.value,
    )
    payload = client.complete_json(
        prompt,
        system=_SYSTEM_PROMPT,
        temperature=0.0,
        max_tokens=400,
        operation="criteria_parser",
    )

    return ParsedCriterion(
        original_text=bullet,
        criterion_type=criterion_type,
        category=_coerce_category(payload.get("category")),
        parsed_description=(payload.get("parsed_description") or None),
        is_critical=bool(payload.get("is_critical", True)),
        temporal_constraint=_sanitize_constraint(payload.get("temporal_constraint")),
        value_constraint=_sanitize_constraint(payload.get("value_constraint")),
    )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def parse_eligibility_text(
    text: str,
    *,
    client: Optional[LLMClient] = None,
    max_bullets: Optional[int] = None,
) -> list[ParsedCriterion]:
    """Split ``text`` into bullets and convert each via the LLM.

    Returns parsed criteria in the order they appear in the source text.
    Bullets that the LLM fails to parse are silently skipped — the
    caller can rerun the parser to retry transient failures.
    """
    if client is None:
        client = get_llm_client()

    sections = _split_into_sections(text)
    bullets: list[tuple[CriterionTypeEnum, str]] = []
    for section in sections:
        for bullet in section.bullets:
            bullets.append((section.kind, bullet))

    if max_bullets is not None:
        bullets = bullets[:max_bullets]

    parsed: list[ParsedCriterion] = []
    for kind, bullet in bullets:
        try:
            parsed.append(_parse_bullet(bullet, kind, client=client))
        except (LLMError, LLMResponseParseError) as exc:
            logger.warning(
                "Failed to parse criterion bullet (skipping): %s — %s",
                bullet[:80],
                exc,
            )
            continue
    return parsed


def parse_trial_criteria(
    db: Session,
    trial: ClinicalTrial,
    *,
    client: Optional[LLMClient] = None,
    replace_existing: bool = True,
    max_bullets: Optional[int] = None,
) -> ParseStats:
    """Parse ``trial.raw_eligibility_text`` and persist :class:`TrialCriterion` rows.

    By default, existing criteria are replaced — this guarantees a clean
    state when the CT.gov sync brings down updated text.  Set
    ``replace_existing=False`` to append (rare; useful for incremental
    fix-ups).
    """
    stats = ParseStats(trial_id=str(trial.id))

    if not trial.raw_eligibility_text:
        stats.errors.append("Trial has no raw_eligibility_text to parse.")
        return stats

    client = client or get_llm_client()
    sections = _split_into_sections(trial.raw_eligibility_text)
    bullets: list[tuple[CriterionTypeEnum, str]] = []
    for section in sections:
        for bullet in section.bullets:
            bullets.append((section.kind, bullet))
    stats.bullets_seen = len(bullets)

    if max_bullets is not None:
        bullets = bullets[:max_bullets]

    if replace_existing:
        db.execute(
            delete(TrialCriterion).where(TrialCriterion.trial_id == trial.id)
        )
        db.flush()

    for order_index, (kind, bullet) in enumerate(bullets):
        try:
            parsed = _parse_bullet(bullet, kind, client=client)
        except (LLMError, LLMResponseParseError) as exc:
            stats.criteria_failed += 1
            stats.errors.append(f"bullet #{order_index}: {exc}")
            continue

        db.add(
            TrialCriterion(
                trial_id=trial.id,
                criterion_type=parsed.criterion_type.value,
                category=parsed.category.value,
                original_text=parsed.original_text,
                parsed_description=parsed.parsed_description,
                temporal_constraint=parsed.temporal_constraint,
                value_constraint=parsed.value_constraint,
                is_critical=parsed.is_critical,
                order_index=order_index,
            )
        )
        stats.criteria_parsed += 1
        if parsed.criterion_type == CriterionTypeEnum.INCLUSION:
            stats.inclusion_count += 1
        else:
            stats.exclusion_count += 1

    db.commit()
    return stats
