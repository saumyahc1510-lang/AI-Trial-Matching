"""Append-only audit log model for HIPAA compliance.

Every state-changing operation (and every access to PHI) is recorded in
the ``audit_logs`` table.  Rows are **never** updated or deleted — the
``updated_at`` column inherited from :class:`~app.database.TimestampMixin`
is explicitly set to ``None`` so that Alembic will not generate it.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base


class AuditAction(str, enum.Enum):
    """Enumeration of auditable actions.

    Values are stored as lowercase strings in the ``action`` column.
    """

    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    MATCH_TRIGGERED = "match_triggered"
    MATCH_COMPLETED = "match_completed"
    FEEDBACK_SUBMITTED = "feedback_submitted"
    LOGIN = "login"
    LOGOUT = "logout"
    EXPORT = "export"
    API_CALL = "api_call"


class AuditLog(Base):
    """Immutable audit-trail entry for HIPAA compliance.

    .. important::
        This table is **append-only**.  The ``updated_at`` column from
        ``TimestampMixin`` is overridden to ``None`` to prevent any
        accidental mutation semantics.

    Attributes:
        id:              UUID primary key.
        timestamp:       When the event occurred (indexed for range scans).
        user_id:         FK to ``users.id``; ``NULL`` for system-initiated
                         actions.
        action:          One of :class:`AuditAction` values.
        resource_type:   Logical entity name, e.g. ``"patient"``, ``"trial"``.
        resource_id:     Stringified identifier of the affected resource.
        details:         Arbitrary JSON payload with additional context.
        ip_address:      Client IP that initiated the request.
        user_agent:      ``User-Agent`` header value.
        request_path:    HTTP request path.
        request_method:  HTTP method (``GET``, ``POST``, …).
        response_status: HTTP response status code.
        phi_accessed:    Flag indicating whether PHI was involved.
        created_at:      Alias for ``timestamp`` (via TimestampMixin).
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_timestamp", "timestamp"),
        Index("ix_audit_logs_user_id", "user_id"),
        Index("ix_audit_logs_action", "action"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
        {"comment": "Append-only audit log for HIPAA compliance"},
    )

    # ── Override TimestampMixin's updated_at ───────────────────────────
    # Audit rows are immutable; suppress the mixin column entirely.
    updated_at = None  # type: ignore[assignment]

    # ── Primary key ───────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique audit entry identifier",
    )

    # ── Timestamp (replaces created_at semantically) ──────────────────
    timestamp = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
        comment="When the auditable event occurred",
    )

    # ── Actor ─────────────────────────────────────────────────────────
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="FK to users.id; NULL for system-initiated actions",
    )

    # ── Action & resource ─────────────────────────────────────────────
    action = Column(
        String(30),
        nullable=False,
        comment="Auditable action type (see AuditAction enum)",
    )
    resource_type = Column(
        String(64),
        nullable=False,
        comment="Logical entity type, e.g. 'patient', 'trial'",
    )
    resource_id = Column(
        String(128),
        nullable=True,
        comment="Stringified ID of the affected resource",
    )

    # ── Context payload ───────────────────────────────────────────────
    details = Column(
        JSONB,
        nullable=True,
        comment="Arbitrary JSON context for the event",
    )

    # ── Request metadata ──────────────────────────────────────────────
    ip_address = Column(
        String(45),
        nullable=True,
        comment="Client IP address (IPv4 or IPv6)",
    )
    user_agent = Column(
        String(512),
        nullable=True,
        comment="HTTP User-Agent header",
    )
    request_path = Column(
        String(2048),
        nullable=True,
        comment="HTTP request path",
    )
    request_method = Column(
        String(10),
        nullable=True,
        comment="HTTP method (GET, POST, …)",
    )
    response_status = Column(
        Integer,
        nullable=True,
        comment="HTTP response status code",
    )

    # ── PHI flag ──────────────────────────────────────────────────────
    phi_accessed = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="True if Protected Health Information was accessed",
    )

    # ── Relationships ─────────────────────────────────────────────────
    user = relationship(
        "User",
        back_populates="audit_logs",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # noqa: D401
        """Concise representation of the audit entry."""
        return (
            f"<AuditLog {self.action} on {self.resource_type}"
            f"/{self.resource_id} at {self.timestamp}>"
        )
