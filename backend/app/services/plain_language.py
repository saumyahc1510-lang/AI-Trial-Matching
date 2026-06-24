"""Plain-language + multilingual trial summaries.

Clinical-trial protocols are written for medical professionals; what
patients see in the matching UI should be a short, accurate, plain-
language description of *what the trial is studying, what participation
involves, what the risks are, and how long it takes*.

This service produces that text via the LLM and — when the patient's
preferred language is anything other than ``"en"`` — translates it on
the fly.  Both stages share a per-trial JSONB cache so each
``(trial, language)`` pair is generated at most once.

Pipeline
--------
::

    raw protocol text ─┐
                       ├─► LLM #1 (rewrite) ─► English summary
    trial.title + ...  ┘                          │
                                                  ▼
                                       LLM #2 (translate)
                                                  │
                                                  ▼
                                      Localised summary
                                                  │
                                                  └─► stored in
                                                       ClinicalTrial.summary_cache
                                                       (cache key = lang code)

When the patient prefers English we skip the second LLM call entirely
— the English summary is the final output.

Language support
----------------
Anything Llama-3 can translate.  We expose
:data:`SUPPORTED_LANGUAGES` so the API can show a dropdown; passing a
code not in the list still works (the LLM is asked to attempt a
translation).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models.matching import MatchResult
from app.models.trial import ClinicalTrial
from app.services.llm_client import (
    LLMClient,
    LLMError,
    LLMResponseParseError,
    get_llm_client,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Language registry
# ---------------------------------------------------------------------------

# ISO 639-1 codes → display names.  The set the API surfaces in the
# language-preference dropdown.  We deliberately keep this small and
# curated rather than auto-translating to anything users type — bad
# translations are worse than a polite "we don't support that yet".
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "zh": "Mandarin Chinese",
    "hi": "Hindi",
    "ar": "Arabic",
    "pt": "Portuguese",
    "fr": "French",
    "ko": "Korean",
    "vi": "Vietnamese",
    "ru": "Russian",
}


def _canonical_lang_code(raw: Optional[str]) -> str:
    """Coerce a free-form language preference to a 2-letter base code.

    Accepts ``"es-MX"`` / ``"ES"`` / ``""`` / ``None`` — anything missing
    falls back to ``"en"``.
    """
    if not raw:
        return "en"
    return raw.strip().split("-")[0].lower()


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = """\
You are a patient-education writer.  You translate dense clinical-trial \
protocol text into a short, plain-English summary that a non-medical \
adult can understand at a 6th-grade reading level.  Be accurate and \
neutral — never embellish risks or benefits.
"""

_SUMMARY_USER_PROMPT = """\
Write a plain-English summary of the following clinical trial.  Aim for \
80-120 words.  Cover, in this order, with concise sentences:

  1. What disease or condition is being studied.
  2. What the trial is testing (drug, device, or approach).
  3. What participation involves (visits, procedures, frequency).
  4. Approximate duration of participation.
  5. The main category of risks or side effects (if mentioned).

Write the summary in normal paragraph form — no bullet points, no \
headers, no markdown.  Do not add disclaimers.  Do not invent details \
that are not in the source.

---
TRIAL TITLE: {title}

PHASE: {phase}
STUDY TYPE: {study_type}
CONDITIONS: {conditions}
INTERVENTIONS: {interventions}

PROTOCOL SUMMARY:
{brief_summary}

ELIGIBILITY (for context — do NOT include in the summary):
{eligibility}
---

Write the plain-English summary now.  Output only the summary text.
"""


_TRANSLATE_SYSTEM_PROMPT = """\
You are a medical-translation specialist.  You translate patient-facing \
health text into the requested language, preserving meaning exactly.  \
Do not add commentary, headers, or markdown.
"""


_TRANSLATE_USER_PROMPT = """\
Translate the following plain-English text into {language_name}.  \
Keep the same number of sentences, the same factual content, and the \
same neutral tone.  Output only the translated text — no quotes, no \
preface.

ENGLISH:
\"\"\"{english_text}\"\"\"
"""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class PlainLanguageSummary:
    """Output of :func:`summarise_trial`.

    Attributes:
        text:        The final localised summary the patient sees.
        language:    ISO 639-1 code of ``text``.
        english_text: The base English summary that ``text`` was
            translated from (equal to ``text`` when language is ``"en"``).
        from_cache:  ``True`` if no LLM call was made.
    """

    text: str
    language: str
    english_text: str
    from_cache: bool


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def summarise_trial(
    trial: ClinicalTrial,
    *,
    language: Optional[str] = None,
    client: Optional[LLMClient] = None,
    force_refresh: bool = False,
) -> PlainLanguageSummary:
    """Produce (and cache) a plain-language summary in ``language``.

    The trial's ``summary_cache`` JSONB column is used as a write-through
    cache keyed by language code.  ``force_refresh=True`` bypasses the
    cache; useful when the upstream protocol text has materially changed
    and the caller doesn't want stale translations.
    """
    lang = _canonical_lang_code(language)
    cache = dict(trial.summary_cache or {})
    if not force_refresh and lang in cache and cache[lang]:
        english = cache.get("en") or cache[lang]
        return PlainLanguageSummary(
            text=cache[lang],
            language=lang,
            english_text=english,
            from_cache=True,
        )

    client = client or get_llm_client()

    # Always ensure we have an English baseline cached — it's the source
    # for every future translation.
    english_text = cache.get("en") if not force_refresh else None
    if not english_text:
        english_text = _generate_english_summary(trial, client=client)
        cache["en"] = english_text

    if lang == "en":
        final_text = english_text
    else:
        final_text = _translate_summary(english_text, lang, client=client)
        cache[lang] = final_text

    trial.summary_cache = cache
    # JSONB mutations need to be flagged so SQLAlchemy emits an UPDATE.
    flag_modified(trial, "summary_cache")

    return PlainLanguageSummary(
        text=final_text,
        language=lang,
        english_text=english_text,
        from_cache=False,
    )


def summarise_for_match(
    db: Session,
    match: MatchResult,
    trial: ClinicalTrial,
    *,
    language: Optional[str] = None,
    client: Optional[LLMClient] = None,
    force_refresh: bool = False,
) -> PlainLanguageSummary:
    """Generate or fetch a summary and stamp it onto ``match``.

    Writes :attr:`MatchResult.plain_language_summary` and
    :attr:`MatchResult.summary_language` so the matching/explainability
    layers can surface the patient-facing text without re-running the
    LLM.  Commits the session — callers passing their own transaction
    can wrap this in a savepoint if needed.
    """
    summary = summarise_trial(
        trial,
        language=language,
        client=client,
        force_refresh=force_refresh,
    )
    match.plain_language_summary = summary.text
    match.summary_language = summary.language
    db.commit()
    return summary


# ---------------------------------------------------------------------------
# Internal LLM helpers
# ---------------------------------------------------------------------------

def _conditions_str(trial: ClinicalTrial) -> str:
    conditions = trial.conditions if isinstance(trial.conditions, list) else []
    if not conditions:
        return "(none listed)"
    return ", ".join(str(c) for c in conditions[:6])


def _interventions_str(trial: ClinicalTrial) -> str:
    """Render the interventions list as a compact, prompt-safe string."""
    items = trial.interventions if isinstance(trial.interventions, list) else []
    if not items:
        return "(none listed)"
    rendered: list[str] = []
    for iv in items[:6]:
        if not isinstance(iv, dict):
            continue
        type_ = iv.get("type") or "Intervention"
        name = iv.get("name") or "(unnamed)"
        rendered.append(f"{type_}: {name}")
    return "; ".join(rendered) if rendered else "(none listed)"


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return "(not provided)"
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"


def _generate_english_summary(trial: ClinicalTrial, *, client: LLMClient) -> str:
    """Call the LLM to rewrite the protocol into English plain language."""
    prompt = _SUMMARY_USER_PROMPT.format(
        title=trial.title or "(untitled)",
        phase=trial.phase or "not specified",
        study_type=trial.study_type or "not specified",
        conditions=_conditions_str(trial),
        interventions=_interventions_str(trial),
        brief_summary=_truncate(trial.brief_summary, 2500),
        eligibility=_truncate(trial.raw_eligibility_text, 1500),
    )
    try:
        result = client.complete(
            prompt,
            system=_SUMMARY_SYSTEM_PROMPT,
            temperature=0.3,
            max_tokens=400,
            operation="plain_language",
        )
    except (LLMError, LLMResponseParseError) as exc:
        logger.warning("LLM plain-language summary failed: %s", exc)
        # Fall back to a deterministic stub so the UI always has *something*.
        return _fallback_summary(trial)
    text = (result.text or "").strip()
    return text or _fallback_summary(trial)


def _translate_summary(
    english_text: str,
    lang_code: str,
    *,
    client: LLMClient,
) -> str:
    """Translate an English summary into ``lang_code``.

    Uses the friendly language name in the prompt so the LLM is less
    likely to get confused by raw ISO codes ("zh" vs "Mandarin Chinese").
    """
    language_name = SUPPORTED_LANGUAGES.get(lang_code, lang_code)
    prompt = _TRANSLATE_USER_PROMPT.format(
        language_name=language_name,
        english_text=english_text,
    )
    try:
        result = client.complete(
            prompt,
            system=_TRANSLATE_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=500,
            operation="plain_language_translate",
        )
    except (LLMError, LLMResponseParseError) as exc:
        logger.warning("LLM translation to %s failed: %s", lang_code, exc)
        # Fall back to the English text — better than a blank string.
        return english_text
    return (result.text or "").strip() or english_text


def _fallback_summary(trial: ClinicalTrial) -> str:
    """Hand-written stub used when the LLM call fails entirely."""
    conditions = _conditions_str(trial)
    title = (trial.title or "an untitled trial").strip()
    return (
        f"This clinical trial ({title}) is studying {conditions}. "
        f"The protocol summary is not yet available in plain language. "
        f"A coordinator can answer questions about what participation "
        f"involves and how long the trial lasts."
    )
