"""Per-criterion explainability rendering.

The matching engine writes all the *data* — :class:`MatchResult`,
:class:`CriterionEvaluation`, :class:`UncertaintyFlag` — but turning
that into something a clinician can scan in 30 seconds is a separate
concern.  This module renders a single match result as:

* :class:`ExplainabilityReport` — structured dataclass + dict export,
  consumed by the API layer's JSON responses.
* a **Markdown table** + summary, suitable for emails, slack messages,
  or an audit-grade printout.

Why split this from the matching engine
---------------------------------------
1. The matching engine should not care how the output is shaped — it
   already produces the canonical DB rows.  Two layers of "what does
   the explainability JSON look like" was making `matching_engine.py`
   harder to test.
2. The matching engine commits.  This module is *read-only* and never
   mutates the database; that makes it safe to call from any context
   (REST endpoint, Celery task, ad-hoc REPL inspection).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.matching import (
    CriterionEvaluation,
    CriterionStatusEnum,
    MatchResult,
    OverallMatchStatusEnum,
    UncertaintyFlag,
)
from app.models.patient import MedicalEvent
from app.models.trial import ClinicalTrial, TrialCriterion


# ---------------------------------------------------------------------------
# Pretty-print helpers
# ---------------------------------------------------------------------------

_STATUS_GLYPH: dict[str, str] = {
    CriterionStatusEnum.MET.value:       "PASS",
    CriterionStatusEnum.NOT_MET.value:   "FAIL",
    CriterionStatusEnum.UNCERTAIN.value: "????",
}

_OVERALL_GLYPH: dict[str, str] = {
    OverallMatchStatusEnum.ELIGIBLE.value:   "ELIGIBLE",
    OverallMatchStatusEnum.INELIGIBLE.value: "INELIGIBLE",
    OverallMatchStatusEnum.UNCERTAIN.value:  "UNCERTAIN",
}


def _truncate(text: Optional[str], limit: int = 120) -> str:
    """Trim long strings for tabular display; never break inside a word."""
    if not text:
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[: limit - 1]
    # Avoid leaving a partial word.
    last_space = cut.rfind(" ")
    if last_space > 40:
        cut = cut[:last_space]
    return cut + "…"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ExplainabilityRow:
    """One row of the explainability table — corresponds to one criterion."""

    order_index: int
    criterion_type: str           # inclusion | exclusion
    category: str
    criterion_text: str
    parsed_description: Optional[str]
    is_critical: bool
    status: str                   # met | not_met | uncertain
    confidence: float
    reasoning: str
    evidence_text: Optional[str]
    evidence_source: Optional[str]
    evaluator: str
    llm_model_used: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly representation used by the API layer."""
        return {
            "order_index": self.order_index,
            "criterion_type": self.criterion_type,
            "category": self.category,
            "criterion_text": self.criterion_text,
            "parsed_description": self.parsed_description,
            "is_critical": self.is_critical,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "reasoning": self.reasoning,
            "evidence_text": self.evidence_text,
            "evidence_source": self.evidence_source,
            "evaluator": self.evaluator,
            "llm_model_used": self.llm_model_used,
        }


@dataclass
class ExplainabilityFlag:
    """An :class:`UncertaintyFlag` flattened for output."""

    missing_data_type: str
    description: str
    resolution_action: Optional[str]
    priority: str
    resolved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "missing_data_type": self.missing_data_type,
            "description": self.description,
            "resolution_action": self.resolution_action,
            "priority": self.priority,
            "resolved": self.resolved,
        }


@dataclass
class ExplainabilityReport:
    """Full per-match explainability payload.

    The same instance backs both the structured JSON response *and* the
    Markdown render — :meth:`to_markdown` / :meth:`to_dict` consume the
    shared state so the two outputs can never drift.
    """

    match_result_id: str
    patient_id: str
    trial_id: str
    trial_nct_id: str
    trial_title: str
    overall_status: str
    match_score: float
    confidence_score: float
    total_criteria: int
    criteria_met: int
    criteria_not_met: int
    criteria_uncertain: int
    missing_data_summary: Optional[str]
    coordinator_status: str
    triggered_by: str
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    rows: list[ExplainabilityRow] = field(default_factory=list)
    flags: list[ExplainabilityFlag] = field(default_factory=list)

    # ── Export ───────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-serialisable dict."""
        return {
            "match_result_id": self.match_result_id,
            "patient_id": self.patient_id,
            "trial": {
                "id": self.trial_id,
                "nct_id": self.trial_nct_id,
                "title": self.trial_title,
            },
            "overall_status": self.overall_status,
            "scores": {
                "match_score": round(self.match_score, 3),
                "confidence_score": round(self.confidence_score, 3),
            },
            "criterion_counts": {
                "total": self.total_criteria,
                "met": self.criteria_met,
                "not_met": self.criteria_not_met,
                "uncertain": self.criteria_uncertain,
            },
            "missing_data_summary": self.missing_data_summary,
            "coordinator_status": self.coordinator_status,
            "triggered_by": self.triggered_by,
            "generated_at": self.generated_at.isoformat(),
            "rows": [row.to_dict() for row in self.rows],
            "uncertainty_flags": [flag.to_dict() for flag in self.flags],
        }

    def to_markdown(self) -> str:
        """Render a human-readable Markdown report.

        The render is deliberately conservative — escape pipe characters
        inside cells, keep the table to seven columns, and put the
        per-criterion reasoning in its own block under the table so the
        table itself stays readable in a terminal / GitHub view.
        """
        lines: list[str] = []
        lines.append(f"# Trial match: {self.trial_nct_id}")
        lines.append("")
        lines.append(f"**{self.trial_title}**")
        lines.append("")
        lines.append(
            f"- Overall: **{_OVERALL_GLYPH.get(self.overall_status, self.overall_status)}**"
        )
        lines.append(
            f"- Match score: {self.match_score:.0%}   "
            f"Confidence: {self.confidence_score:.0%}"
        )
        lines.append(
            f"- Criteria: {self.criteria_met} met / {self.criteria_not_met} not met / "
            f"{self.criteria_uncertain} uncertain  (out of {self.total_criteria})"
        )
        lines.append(f"- Coordinator status: `{self.coordinator_status}`")
        lines.append(f"- Triggered by: `{self.triggered_by}`")
        lines.append(f"- Generated: {self.generated_at.isoformat()}")
        lines.append("")

        if self.missing_data_summary:
            lines.append("## Missing data")
            lines.append("")
            lines.append(self.missing_data_summary)
            lines.append("")

        # ── Table ────────────────────────────────────────────────────
        lines.append("## Criteria")
        lines.append("")
        lines.append("| # | Type | Status | Critical | Criterion | Evidence |")
        lines.append("|---|------|--------|----------|-----------|----------|")
        for row in self.rows:
            crit_mark = "yes" if row.is_critical else "no"
            criterion_cell = _truncate(
                row.parsed_description or row.criterion_text, 80
            ).replace("|", "\\|")
            evidence_cell = _truncate(row.evidence_text, 60).replace("|", "\\|") or "—"
            lines.append(
                f"| {row.order_index} | {row.criterion_type} | "
                f"{_STATUS_GLYPH.get(row.status, row.status)} | {crit_mark} | "
                f"{criterion_cell} | {evidence_cell} |"
            )
        lines.append("")

        # ── Reasoning detail ─────────────────────────────────────────
        lines.append("## Reasoning detail")
        lines.append("")
        for row in self.rows:
            badge = _STATUS_GLYPH.get(row.status, row.status)
            lines.append(
                f"**{row.order_index}. [{badge}] "
                f"({row.criterion_type} · {row.category} · "
                f"conf {row.confidence:.2f} · via {row.evaluator})**  "
            )
            lines.append(row.criterion_text.strip())
            lines.append("")
            lines.append(f"> {row.reasoning.strip()}")
            if row.evidence_text:
                lines.append(f">")
                lines.append(f"> *Evidence:* {row.evidence_text.strip()}")
            if row.evidence_source:
                lines.append(f"> *Source:* `{row.evidence_source}`")
            lines.append("")

        # ── Flags ────────────────────────────────────────────────────
        if self.flags:
            lines.append("## Uncertainty flags")
            lines.append("")
            for f in self.flags:
                resolved_mark = " (resolved)" if f.resolved else ""
                lines.append(
                    f"- [{f.priority.upper()}] **{f.missing_data_type}**{resolved_mark}: "
                    f"{f.description}"
                )
                if f.resolution_action:
                    lines.append(f"  - Action: {f.resolution_action}")
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def _load_match_result(db: Session, match_result_id: uuid.UUID) -> MatchResult:
    """Fetch the match result with all the relationships we need.

    Uses ``selectinload`` to make the relationship loads a single batch
    of queries — cheaper than the default ``selectin`` eager strategy
    here because we want explicit control.
    """
    stmt = (
        select(MatchResult)
        .options(
            selectinload(MatchResult.trial).selectinload(ClinicalTrial.criteria),
            selectinload(MatchResult.criterion_evaluations),
            selectinload(MatchResult.uncertainty_flags),
        )
        .where(MatchResult.id == match_result_id)
    )
    result = db.execute(stmt).scalar_one_or_none()
    if result is None:
        raise ValueError(f"MatchResult {match_result_id!s} not found.")
    return result


def _row_for_evaluation(
    evaluation: CriterionEvaluation,
    criterion: TrialCriterion,
) -> ExplainabilityRow:
    return ExplainabilityRow(
        order_index=criterion.order_index,
        criterion_type=criterion.criterion_type,
        category=criterion.category,
        criterion_text=criterion.original_text,
        parsed_description=criterion.parsed_description,
        is_critical=bool(criterion.is_critical),
        status=evaluation.status,
        confidence=float(evaluation.confidence or 0.0),
        reasoning=evaluation.reasoning or "(no reasoning recorded)",
        evidence_text=evaluation.evidence_text,
        evidence_source=evaluation.evidence_source,
        evaluator=evaluation.evaluated_by,
        llm_model_used=evaluation.llm_model_used,
    )


def build_report(db: Session, match_result_id: uuid.UUID) -> ExplainabilityReport:
    """Assemble an :class:`ExplainabilityReport` from a persisted match.

    Reads-only — never mutates the database.  Safe to call from any
    request context.
    """
    match_result = _load_match_result(db, match_result_id)
    trial = match_result.trial
    criteria_by_id: dict[str, TrialCriterion] = {
        str(c.id): c for c in trial.criteria
    }

    rows: list[ExplainabilityRow] = []
    for ev in match_result.criterion_evaluations:
        criterion = criteria_by_id.get(str(ev.criterion_id))
        if criterion is None:
            continue  # criterion deleted out from under us — skip safely
        rows.append(_row_for_evaluation(ev, criterion))
    rows.sort(key=lambda r: r.order_index)

    flags = [
        ExplainabilityFlag(
            missing_data_type=f.missing_data_type,
            description=f.description,
            resolution_action=f.resolution_action,
            priority=f.priority,
            resolved=bool(f.resolved),
        )
        for f in match_result.uncertainty_flags
    ]

    return ExplainabilityReport(
        match_result_id=str(match_result.id),
        patient_id=str(match_result.patient_id),
        trial_id=str(trial.id),
        trial_nct_id=trial.nct_id,
        trial_title=trial.title,
        overall_status=match_result.overall_status,
        match_score=float(match_result.match_score or 0.0),
        confidence_score=float(match_result.confidence_score or 0.0),
        total_criteria=match_result.total_criteria,
        criteria_met=match_result.criteria_met,
        criteria_not_met=match_result.criteria_not_met,
        criteria_uncertain=match_result.criteria_uncertain,
        missing_data_summary=match_result.missing_data_summary,
        coordinator_status=match_result.coordinator_status,
        triggered_by=match_result.triggered_by,
        rows=rows,
        flags=flags,
    )
