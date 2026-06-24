"""Clinical-note NER engine using HuggingFace transformers.

Wraps a biomedical token-classification model (BioClinicalBERT by
default) behind a small, mocking-friendly interface so the matching
engine can extract entities — diseases, drugs, procedures, durations —
from unstructured clinical notes.

Design choices
--------------
* **Lazy + cached load.**  The model is loaded the first time
  :meth:`NEREngine.extract` is called, never at import.  This keeps test
  start-up fast and lets environments without ``torch`` import the
  module without crashing (calls just raise a clear error).
* **Graceful degradation.**  When ``transformers`` or ``torch`` is
  unavailable, :func:`get_ner_engine` returns a stub that yields zero
  entities and logs a warning.  The rest of the matching pipeline keeps
  working — NER is an enrichment, not a hard dependency.
* **Character offsets preserved.**  Every :class:`NEREntity` records
  the ``start`` and ``end`` indices of the entity in the source text so
  downstream explainability can quote evidence verbatim.
* **Aggregated tokens.**  We use the pipeline's ``simple`` aggregation
  strategy to merge BIO-tagged sub-word tokens back into whole entity
  spans.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NEREntity:
    """A single entity extracted from a clinical note.

    Attributes:
        text: The exact substring from the source.
        label: Model-reported entity label (e.g. "PROBLEM", "DRUG").
        start: Character offset (inclusive) where the entity begins.
        end:   Character offset (exclusive) where the entity ends.
        score: Confidence in [0, 1].
    """

    text: str
    label: str
    start: int
    end: int
    score: float


@dataclass
class NERResult:
    """All entities extracted from one document, plus diagnostics."""

    source_text: str
    entities: list[NEREntity] = field(default_factory=list)
    model_name: Optional[str] = None
    skipped: bool = False
    skipped_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Label normalisation
# ---------------------------------------------------------------------------

# BioClinicalBERT / i2b2-style labels → our normalised set.  Anything not
# in this map passes through unchanged (uppercased).
_LABEL_NORMALISATION: dict[str, str] = {
    "B-PROBLEM": "PROBLEM",
    "I-PROBLEM": "PROBLEM",
    "B-TREATMENT": "TREATMENT",
    "I-TREATMENT": "TREATMENT",
    "B-TEST": "TEST",
    "I-TEST": "TEST",
    "DISEASE": "PROBLEM",
    "DISEASE_DISORDER": "PROBLEM",
    "SIGN_SYMPTOM": "PROBLEM",
    "MEDICATION": "TREATMENT",
    "DRUG": "TREATMENT",
    "CHEMICAL": "TREATMENT",
    "LAB_VALUE": "TEST",
    "LAB_TEST": "TEST",
    "PROCEDURE": "TREATMENT",
    "DATE": "DATE",
    "DURATION": "DURATION",
    "DOSAGE": "DOSAGE",
}


def _normalise_label(raw: str) -> str:
    """Map a model label to our consistent set; default to upper-case raw."""
    if not raw:
        return "UNKNOWN"
    upper = raw.upper()
    return _LABEL_NORMALISATION.get(upper, upper)


# ---------------------------------------------------------------------------
# Engine implementations
# ---------------------------------------------------------------------------

# Default model — small, openly licensed, returns useful biomedical
# entities.  Override via Settings.NER_MODEL_NAME (added in config).
_DEFAULT_NER_MODEL = "d4data/biomedical-ner-all"


class NEREngine:
    """Real NER engine backed by a HuggingFace token-classification pipeline."""

    def __init__(self, model_name: Optional[str] = None) -> None:
        self._model_name = model_name or _DEFAULT_NER_MODEL
        self._pipeline: Any = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def _ensure_pipeline(self) -> Any:
        """Lazily build the pipeline; cached for subsequent calls."""
        if self._pipeline is not None:
            return self._pipeline
        with self._lock:
            if self._pipeline is not None:
                return self._pipeline
            try:
                from transformers import pipeline  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - depends on env
                raise RuntimeError(
                    "The 'transformers' package is required for NER. "
                    "Install with: pip install transformers torch"
                ) from exc

            logger.info("Loading NER model: %s (first call may take 30-60s)", self._model_name)
            self._pipeline = pipeline(
                task="token-classification",
                model=self._model_name,
                aggregation_strategy="simple",
            )
            return self._pipeline

    def extract(self, text: str) -> NERResult:
        """Extract entities from ``text``.

        Returns an empty :class:`NERResult` for empty input rather than
        raising — callers often pass optional free-text fields.
        """
        if not text or not text.strip():
            return NERResult(source_text=text or "", model_name=self._model_name)

        try:
            pipe = self._ensure_pipeline()
        except RuntimeError as exc:
            return NERResult(
                source_text=text,
                model_name=self._model_name,
                skipped=True,
                skipped_reason=str(exc),
            )

        raw_entities = pipe(text)
        entities: list[NEREntity] = []
        for item in raw_entities or []:
            try:
                start = int(item["start"])
                end = int(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            entities.append(
                NEREntity(
                    text=text[start:end],
                    label=_normalise_label(item.get("entity_group") or item.get("entity") or ""),
                    start=start,
                    end=end,
                    score=float(item.get("score", 0.0)),
                )
            )

        return NERResult(
            source_text=text,
            entities=entities,
            model_name=self._model_name,
        )


class _NullNEREngine:
    """Fallback used when transformers/torch are not installed."""

    model_name = "none"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def extract(self, text: str) -> NERResult:
        return NERResult(
            source_text=text or "",
            entities=[],
            model_name="none",
            skipped=True,
            skipped_reason=self._reason,
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_engine: Optional[Any] = None
_engine_lock = threading.Lock()


def _probe_transformers() -> tuple[bool, Optional[str]]:
    """Return ``(available, reason_if_not)`` for the transformers stack."""
    try:
        import transformers  # noqa: F401 — import probe
        import torch  # noqa: F401 — import probe
    except ImportError as exc:
        return False, f"transformers/torch not installed ({exc.name})"
    return True, None


def get_ner_engine() -> Any:
    """Return the process-wide NER engine, building it on first call.

    When ``transformers`` / ``torch`` are missing, this returns a stub
    that always reports ``skipped=True`` so callers don't have to
    branch.  The stub preserves the contract of :meth:`NEREngine.extract`.
    """
    global _engine  # noqa: PLW0603
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        available, reason = _probe_transformers()
        if not available:
            logger.warning(
                "NER disabled: %s.  Install with `pip install transformers torch` "
                "to enable clinical-note entity extraction.",
                reason,
            )
            _engine = _NullNEREngine(reason or "unknown")
        else:
            settings = get_settings()
            model_name = getattr(settings, "NER_MODEL_NAME", None) or _DEFAULT_NER_MODEL
            _engine = NEREngine(model_name=model_name)
        return _engine


def reset_ner_engine() -> None:
    """Discard the cached engine — used by tests that want a fresh build."""
    global _engine  # noqa: PLW0603
    with _engine_lock:
        _engine = None
