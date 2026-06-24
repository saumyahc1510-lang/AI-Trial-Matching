"""User and Role-Based Access Control (RBAC) model.

Defines the ``User`` table and the ``UserRole`` enumeration that drives
permission checks throughout the application.  A user whose role is
``PATIENT`` may optionally be linked to a :class:`~app.models.patient.Patient`
record via ``associated_patient_id``.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class UserRole(str, enum.Enum):
    """Permitted roles for system users.

    Roles are stored as lowercase strings in the database and used for
    authorization throughout the API layer.
    """

    PATIENT = "patient"
    COORDINATOR = "coordinator"
    CLINICIAN = "clinician"
    ADMIN = "admin"
    SPONSOR = "sponsor"


class User(Base):
    """Application user with role-based access control.

    Attributes:
        id:                    UUID primary key.
        email:                 Unique, indexed e-mail address used for login.
        hashed_password:       Bcrypt (or equivalent) password hash.
        full_name:             Display name.
        role:                  One of :class:`UserRole` values.
        is_active:             Soft-delete / deactivation flag.
        associated_patient_id: FK to ``patients.id`` when ``role == PATIENT``.
        last_login_at:         Timestamp of the most recent successful login.
        created_at:            Row creation timestamp (via TimestampMixin).
        updated_at:            Last modification timestamp (via TimestampMixin).

    Relationships:
        audit_logs:  All :class:`~app.models.audit.AuditLog` entries created
                     by this user.
        feedbacks:   All :class:`~app.models.feedback.ClinicianFeedback` entries
                     submitted by this user.
        patient:     The linked :class:`~app.models.patient.Patient` record, if
                     the user has ``role == PATIENT``.
    """

    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_email", "email", unique=True),
        {"comment": "Application users with RBAC roles"},
    )

    # ── Primary key ───────────────────────────────────────────────────
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="Unique user identifier",
    )

    # ── Core fields ───────────────────────────────────────────────────
    email = Column(
        String(320),
        unique=True,
        nullable=False,
        index=True,
        comment="Unique login e-mail address",
    )
    hashed_password = Column(
        String(1024),
        nullable=False,
        comment="Bcrypt-hashed password",
    )
    full_name = Column(
        String(256),
        nullable=False,
        comment="User display name",
    )
    role = Column(
        String(20),
        nullable=False,
        default=UserRole.PATIENT.value,
        comment="RBAC role (patient | coordinator | clinician | admin | sponsor)",
    )
    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="False disables login without deleting the account",
    )

    # ── Optional patient link ─────────────────────────────────────────
    associated_patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("patients.id", ondelete="SET NULL"),
        nullable=True,
        comment="FK to patients.id when role is PATIENT",
    )

    # ── Timestamps ────────────────────────────────────────────────────
    last_login_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of most recent login",
    )

    # ── Relationships ─────────────────────────────────────────────────
    patient = relationship(
        "Patient",
        foreign_keys=[associated_patient_id],
        lazy="selectin",
    )
    audit_logs = relationship(
        "AuditLog",
        back_populates="user",
        lazy="dynamic",
    )
    feedbacks = relationship(
        "ClinicianFeedback",
        back_populates="user",
        lazy="dynamic",
    )

    def __repr__(self) -> str:  # noqa: D401
        """Concise representation including role and e-mail."""
        return f"<User {self.email} role={self.role}>"
