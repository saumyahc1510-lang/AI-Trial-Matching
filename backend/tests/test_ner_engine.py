"""Unit tests for :mod:`app.services.ner_engine`.

These tests deliberately don't load the heavy HuggingFace model — they
just verify the **engine wiring + null fallback** contract.  The full
model is exercised in a separate slow integration suite (not included
here) because pulling ~700 MB of weights every CI run is overkill.
"""

from __future__ import annotations

import pytest

from app.services.ner_engine import (
    NEREntity,
    NERResult,
    _NullNEREngine,
    _normalise_label,
    get_ner_engine,
    reset_ner_engine,
)


# ---------------------------------------------------------------------------
# Null engine contract
# ---------------------------------------------------------------------------

def test_null_engine_reports_skipped() -> None:
    """The fallback never raises; always reports ``skipped=True``."""
    engine = _NullNEREngine(reason="transformers not installed")
    result = engine.extract("The patient has hypertension.")
    assert isinstance(result, NERResult)
    assert result.skipped is True
    assert result.entities == []
    assert result.model_name == "none"
    assert result.skipped_reason


def test_null_engine_handles_empty_input() -> None:
    engine = _NullNEREngine(reason="x")
    result = engine.extract("")
    assert result.entities == []
    assert result.skipped is True


# ---------------------------------------------------------------------------
# Label normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("B-PROBLEM", "PROBLEM"),
        ("I-PROBLEM", "PROBLEM"),
        ("DISEASE_DISORDER", "PROBLEM"),
        ("DRUG", "TREATMENT"),
        ("MEDICATION", "TREATMENT"),
        ("LAB_VALUE", "TEST"),
        ("DATE", "DATE"),
        ("Unknown_Type", "UNKNOWN_TYPE"),  # passthrough uppercased
        ("", "UNKNOWN"),
    ],
)
def test_label_normalisation(raw: str, expected: str) -> None:
    assert _normalise_label(raw) == expected


# ---------------------------------------------------------------------------
# Singleton accessor (fallback path)
# ---------------------------------------------------------------------------

def test_get_ner_engine_returns_null_when_torch_missing(monkeypatch) -> None:
    """When ``transformers``/``torch`` aren't installed, we get the null engine."""
    reset_ner_engine()

    # Force the probe to report unavailable regardless of the real env.
    from app.services import ner_engine as ner_mod
    monkeypatch.setattr(
        ner_mod, "_probe_transformers", lambda: (False, "torch missing for test")
    )
    reset_ner_engine()

    engine = get_ner_engine()
    assert isinstance(engine, _NullNEREngine)
    result = engine.extract("Patient has diabetes.")
    assert result.skipped is True
    assert "torch missing" in (result.skipped_reason or "")


# ---------------------------------------------------------------------------
# NEREntity dataclass
# ---------------------------------------------------------------------------

def test_ner_entity_is_frozen() -> None:
    e = NEREntity(text="diabetes", label="PROBLEM", start=10, end=18, score=0.95)
    with pytest.raises(Exception):
        e.text = "altered"  # type: ignore[misc]
