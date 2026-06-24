"""Clinician feedback model for the learning / reinforcement loop.

Each :class:`ClinicianFeedback` row captures a clinician's judgment on a
match result (or on a specific criterion evaluation within that result).
These signals are later consumed by the fine-tuning pipeline to improve
the matching model's accuracy.
"""

import enum
import uuid

from sqlalchemy import Boolean, Column, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class FeedbackAction(str, enum.Enum):
    """Actions a clinician can take on a match result.

    Values are stored as lowercase strings in the ``action`` column.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    OVERRIDDEN = "overridden"
    DEFERRED = "deferred"


class OverrideStatus(str, enum.Enum):
    """Possible override statuses when a clinician overrides a criterion.

    Only populated when ``FeedbackAction.OVERRIDDEN`` is selected.
    """

    MET = "met"
    NOT_MET = "not_met"
    UNCERTAIN = "uncertain"


class ClinicianFeedback(Base):
    """Clinician feedback entry tied to a match result.

    Attributes:
        id:                      UUID primary key.
        match_result_id:         FK to ``match_results.id``.
        criterion_evaluation_id: FK to ``criterion_evaluations.id``; ``NULL``
                                 when the feedback applies to the overall match
                                 rather than a single criterion.
        user_id:                 FK to ``users.id`` — the clinician who
                                 submitted the feedback.
        action:                  One of :class:`FeedbackAction` values.
        override_status:         New status assigned by the clinician when the
                                 action is ``OVERRIDDEN``.
        reason:                  Optional free-text explanation.
        is_used_for_training:    Whether this feedback record has already been
                                 consumed by the fine-tuning pipeline.
        created_at:              Row creation timestamp (via TimestampMixin).
        updated_at:              Last modification timestamp (via TimestampMixin).

    Relationships:
        match_result:           The :class:`~app.models.matching.MatchResult`
                                this feedback pertains to.
        criterion_evaluation:   The specific
                                :class:`~app.models.matching.CriterionEvaluation`,
                                if applicable.
        user:                   The clinician who submitted feedback.
    """

    __tablename__ = "clinician_feedbacks"
    __table_args__ = (
        {"comment": "Clinician feedback for the learning loop"},
    )

    # ── Primary key ───────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique feedback identifier",
    )

    # ── Foreign keys ──────────────────────────────────────────────────
    match_result_id = Column(
        UUID(as_uuid=True),
        ForeignKey("match_results.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="FK to the match result being reviewed",
    )
    criterion_evaluation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("criterion_evaluations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="FK to a specific criterion evaluation, if applicable",
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="FK to the clinician who submitted the feedback",
    )

    # ── Feedback fields ───────────────────────────────────────────────
    action = Column(
        String(20),
        nullable=False,
        comment="Feedback action (accepted | rejected | overridden | deferred)",
    )
    override_status = Column(
        String(20),
        nullable=True,
        comment="New criterion status if action is 'overridden' (met | not_met | uncertain)",
    )
    reason = Column(
        Text,
        nullable=True,
        comment="Free-text explanation for the decision",
    )
    is_used_for_training = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="True once consumed by the fine-tuning pipeline",
    )

    # ── Relationships ─────────────────────────────────────────────────
    match_result = relationship(
        "MatchResult",
        back_populates="feedbacks",
        lazy="selectin",
    )
    criterion_evaluation = relationship(
        "CriterionEvaluation",
        back_populates="feedbacks",
        lazy="selectin",
    )
    user = relationship(
        "User",
        back_populates="feedbacks",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # noqa: D401
        """Concise representation of the feedback entry."""
        return (
            f"<ClinicianFeedback {self.action} match={self.match_result_id} "
            f"by user={self.user_id}>"
        )
