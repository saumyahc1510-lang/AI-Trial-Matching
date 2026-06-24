"""Per-call LLM usage record.

Every prompt the system sends to Groq (and any future provider) lands
here as one row.  Append-only at the application layer — no service
ever ``UPDATE``s or ``DELETE``s an existing row; the admin dashboard's
"LLM usage" card just aggregates this table.

We deliberately keep the model lean:

* ``model``                — provider model id (e.g. ``llama-3.3-70b-versatile``)
* ``operation``            — short tag identifying which service made the call
                             (``"criteria_parser"`` / ``"eligibility_reasoner"`` /
                             ``"plain_language"`` / ``"plain_language_translate"``).
* ``prompt_tokens`` /
  ``completion_tokens``    — straight from the provider's usage object
                             (``None`` when the provider didn't return it).
* ``total_tokens``         — denormalised sum for fast aggregation.
* ``latency_ms``           — wall-clock latency of the single call.
* ``success`` /
  ``error_class``          — on failure we still record the row so the
                             dashboard's success-rate metric is honest.

This is **not** PHI-bearing — we don't store prompts or responses,
just the metrics.  That keeps the table cheap to query and removes
any audit-log overlap.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class LLMUsage(Base):
    """One row per outbound LLM call (whether it succeeded or not)."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        Index("ix_llm_usage_created_at", "created_at"),
        Index("ix_llm_usage_model", "model"),
        Index("ix_llm_usage_operation", "operation"),
        {"comment": "Append-only LLM call telemetry — feeds the admin usage card."},
    )

    # Audit-style: no updated_at, no mutations.  ``created_at`` from
    # :class:`~app.database.TimestampMixin` doubles as the call timestamp.
    updated_at = None  # type: ignore[assignment]

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique usage-row identifier.",
    )

    model = Column(
        String(100),
        nullable=False,
        comment="Provider model id (e.g. llama-3.3-70b-versatile).",
    )
    operation = Column(
        String(50),
        nullable=True,
        comment=(
            "Short tag identifying the calling service "
            "(criteria_parser / eligibility_reasoner / plain_language / …)."
        ),
    )
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(
        Integer,
        nullable=True,
        comment="Denormalised prompt + completion tokens for fast aggregation.",
    )
    latency_ms = Column(
        Integer,
        nullable=True,
        comment="Wall-clock latency in milliseconds.",
    )
    success = Column(
        Boolean,
        nullable=False,
        default=True,
        comment="False if the LLM call raised; the row is still written for visibility.",
    )
    error_class = Column(
        String(120),
        nullable=True,
        comment="Exception class name when success=False.",
    )

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<LLMUsage {self.model} op={self.operation} "
            f"tokens={self.total_tokens} latency={self.latency_ms}ms "
            f"success={self.success}>"
        )
