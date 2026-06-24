"""Notification service — write durable notifications + dispatch optional side-channels.

Use cases (driven by Phase 6 workers):

* **Coordinator alert** when a patient newly matches a trial.
* **Resolution alert** when an uncertain match becomes resolved (new
  lab result, manual override, …).
* **Trial status alert** when a trial transitions to ``RECRUITING`` or
  ``COMPLETED``.

The service:

1. Always persists a :class:`~app.models.notification.Notification` row
   per recipient — that's the durable, queryable inbox.
2. Optionally dispatches side-channel deliveries (email via SMTP,
   webhook POST) when ``channel`` is set to one of those.  Each
   side-channel call is wrapped in a try/except and degrades to a
   no-op + warning log — a flaky SMTP server must never block the
   in-app notification.

The dispatcher is intentionally tiny and dependency-light: no Jinja
templates, no markdown rendering, no rate limiting.  Those belong in
the API layer or a dedicated mailer service.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Iterable, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.matching import MatchResult
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationType,
)
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatch result + draft
# ---------------------------------------------------------------------------

@dataclass
class NotificationDraft:
    """Builder for one notification — mirror of the SQLAlchemy row.

    Held separately from the model so the workers can prepare a batch
    in memory and persist them in one ``add_all`` + commit.
    """

    user_id: uuid.UUID
    notification_type: NotificationType
    title: str
    message: str
    channel: NotificationChannel = NotificationChannel.IN_APP
    patient_id: Optional[uuid.UUID] = None
    trial_id: Optional[uuid.UUID] = None
    match_result_id: Optional[uuid.UUID] = None
    details: Optional[dict] = None


@dataclass
class DispatchStats:
    """Aggregate of one :func:`send_batch` call."""

    persisted: int = 0
    in_app_delivered: int = 0
    email_delivered: int = 0
    webhook_delivered: int = 0
    failures: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Recipient discovery
# ---------------------------------------------------------------------------

# Roles that should receive coordinator-style notifications (new match,
# resolution, etc.).  Patients receive their own data-request /
# match-update notifications via a separate fan-out below.
_COORDINATOR_ROLES: tuple[str, ...] = (
    UserRole.COORDINATOR.value,
    UserRole.CLINICIAN.value,
)


def recipients_for_patient(
    db: Session, patient_id: uuid.UUID
) -> list[User]:
    """Coordinators / clinicians who should hear about this patient.

    Today this returns everyone with a coordinator / clinician role —
    site assignment is a future enhancement.  The patient themselves is
    *not* included; patient self-notifications are a different fan-out
    (:func:`recipients_for_patient_self`).
    """
    stmt = (
        select(User)
        .where(User.role.in_(_COORDINATOR_ROLES), User.is_active.is_(True))
    )
    return list(db.execute(stmt).scalars())


def recipients_for_patient_self(
    db: Session, patient_id: uuid.UUID
) -> list[User]:
    """The patient-role user(s) linked to this patient, if any."""
    stmt = (
        select(User)
        .where(
            User.role == UserRole.PATIENT.value,
            User.associated_patient_id == patient_id,
            User.is_active.is_(True),
        )
    )
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Persistence + dispatch
# ---------------------------------------------------------------------------

def send_batch(
    db: Session,
    drafts: Iterable[NotificationDraft],
    *,
    smtp_settings: Optional[dict] = None,
    webhook_url: Optional[str] = None,
) -> DispatchStats:
    """Persist drafts as :class:`Notification` rows + fire side-channels.

    All rows are inserted in one transaction; side-channel failures do
    not roll back the inserts.  Pass ``smtp_settings`` /
    ``webhook_url`` to enable email / webhook delivery — both default
    to disabled for safety.
    """
    stats = DispatchStats()
    drafts_list = list(drafts)
    if not drafts_list:
        return stats

    rows: list[Notification] = []
    for d in drafts_list:
        row = Notification(
            user_id=d.user_id,
            notification_type=d.notification_type.value,
            channel=d.channel.value,
            title=d.title,
            message=d.message,
            read=False,
            delivered=(d.channel == NotificationChannel.IN_APP),
            patient_id=d.patient_id,
            trial_id=d.trial_id,
            match_result_id=d.match_result_id,
            details=d.details,
        )
        rows.append(row)

    db.add_all(rows)
    db.commit()
    stats.persisted = len(rows)
    stats.in_app_delivered = sum(
        1 for r in rows if r.channel == NotificationChannel.IN_APP.value
    )

    # ── Side-channel dispatch ────────────────────────────────────────
    for row in rows:
        if row.channel == NotificationChannel.EMAIL.value:
            if not smtp_settings:
                stats.failures.append(
                    f"email skipped (no smtp_settings): {row.title}"
                )
                continue
            try:
                _dispatch_email(db, row, smtp_settings=smtp_settings)
                stats.email_delivered += 1
            except Exception as exc:  # noqa: BLE001 - never crash on email
                logger.warning("Email dispatch failed: %s", exc)
                stats.failures.append(f"email: {exc}")

        elif row.channel == NotificationChannel.WEBHOOK.value:
            if not webhook_url:
                stats.failures.append(
                    f"webhook skipped (no webhook_url): {row.title}"
                )
                continue
            try:
                _dispatch_webhook(db, row, webhook_url=webhook_url)
                stats.webhook_delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Webhook dispatch failed: %s", exc)
                stats.failures.append(f"webhook: {exc}")

    return stats


def _dispatch_email(
    db: Session,
    notification: Notification,
    *,
    smtp_settings: dict,
) -> None:
    """Send the notification via SMTP and mark it delivered.

    ``smtp_settings`` keys: ``host`` (required), ``port`` (default 587),
    ``username``, ``password``, ``from_address`` (required), ``use_tls``
    (default True).
    """
    user = db.get(User, notification.user_id)
    if user is None or not user.email:
        raise ValueError("Recipient user has no email address.")

    msg = EmailMessage()
    msg["Subject"] = notification.title
    msg["From"] = smtp_settings["from_address"]
    msg["To"] = user.email
    msg.set_content(notification.message)

    host = smtp_settings["host"]
    port = int(smtp_settings.get("port", 587))
    use_tls = bool(smtp_settings.get("use_tls", True))
    with smtplib.SMTP(host, port) as smtp:
        if use_tls:
            smtp.starttls()
        if smtp_settings.get("username"):
            smtp.login(smtp_settings["username"], smtp_settings.get("password", ""))
        smtp.send_message(msg)

    notification.delivered = True
    db.commit()


def _dispatch_webhook(
    db: Session,
    notification: Notification,
    *,
    webhook_url: str,
) -> None:
    """POST a JSON payload to ``webhook_url`` and mark the row delivered."""
    payload = {
        "id": str(notification.id),
        "user_id": str(notification.user_id),
        "type": notification.notification_type,
        "title": notification.title,
        "message": notification.message,
        "patient_id": str(notification.patient_id) if notification.patient_id else None,
        "trial_id": str(notification.trial_id) if notification.trial_id else None,
        "match_result_id": (
            str(notification.match_result_id)
            if notification.match_result_id
            else None
        ),
        "details": notification.details or {},
    }
    response = httpx.post(webhook_url, json=payload, timeout=10.0)
    response.raise_for_status()
    notification.delivered = True
    db.commit()


# ---------------------------------------------------------------------------
# High-level helpers used by workers
# ---------------------------------------------------------------------------

def notify_new_match(
    db: Session,
    match: MatchResult,
    *,
    trial_title: str,
    nct_id: str,
) -> DispatchStats:
    """Fan out a "new eligible match" notification to coordinators.

    Workers call this after the matching engine has produced an
    ``ELIGIBLE`` or ``UNCERTAIN`` result a coordinator should review.
    """
    coordinators = recipients_for_patient(db, match.patient_id)
    if not coordinators:
        logger.info(
            "No coordinator users found — skipping new-match notification."
        )
        return DispatchStats()

    drafts = [
        NotificationDraft(
            user_id=user.id,
            notification_type=NotificationType.NEW_MATCH,
            title=f"New trial match: {nct_id}",
            message=(
                f"Patient {match.patient_id} matched trial {nct_id} "
                f"({trial_title}) with status {match.overall_status} "
                f"(score {match.match_score:.0%}, confidence "
                f"{match.confidence_score:.0%})."
            ),
            patient_id=match.patient_id,
            trial_id=match.trial_id,
            match_result_id=match.id,
            details={
                "overall_status": match.overall_status,
                "match_score": float(match.match_score or 0.0),
                "confidence_score": float(match.confidence_score or 0.0),
            },
        )
        for user in coordinators
    ]
    return send_batch(db, drafts)


def notify_uncertainty_resolved(
    db: Session,
    match: MatchResult,
    resolved_flag_count: int,
    *,
    trial_title: str,
    nct_id: str,
) -> DispatchStats:
    """Tell coordinators that one or more uncertain criteria have flipped.

    Triggered by the re-match worker after a chart update closes flags.
    """
    coordinators = recipients_for_patient(db, match.patient_id)
    if not coordinators:
        return DispatchStats()

    drafts = [
        NotificationDraft(
            user_id=user.id,
            notification_type=NotificationType.MATCH_RESOLVED,
            title=f"Match resolved: {nct_id}",
            message=(
                f"{resolved_flag_count} previously uncertain criterion(s) "
                f"have been resolved for patient {match.patient_id} on "
                f"trial {nct_id} ({trial_title}).  Current status: "
                f"{match.overall_status}."
            ),
            patient_id=match.patient_id,
            trial_id=match.trial_id,
            match_result_id=match.id,
            details={"resolved_flag_count": resolved_flag_count},
        )
        for user in coordinators
    ]
    return send_batch(db, drafts)


def notify_trial_status_change(
    db: Session,
    trial_id: uuid.UUID,
    nct_id: str,
    previous_status: str,
    new_status: str,
) -> DispatchStats:
    """System-wide notification when a trial flips status.

    Routed only to coordinators / clinicians today; the API can decide
    later whether sponsors should subscribe to the same firehose.
    """
    stmt = (
        select(User)
        .where(User.role.in_(_COORDINATOR_ROLES), User.is_active.is_(True))
    )
    users = list(db.execute(stmt).scalars())
    if not users:
        return DispatchStats()

    type_ = (
        NotificationType.TRIAL_OPENED
        if new_status.upper() == "RECRUITING"
        else NotificationType.TRIAL_CLOSED
    )
    drafts = [
        NotificationDraft(
            user_id=user.id,
            notification_type=type_,
            title=f"Trial {nct_id} status: {previous_status} → {new_status}",
            message=(
                f"Trial {nct_id} transitioned from {previous_status} to "
                f"{new_status}.  Active matches against this trial may "
                f"need review."
            ),
            trial_id=trial_id,
            details={"previous_status": previous_status, "new_status": new_status},
        )
        for user in users
    ]
    return send_batch(db, drafts)


# ---------------------------------------------------------------------------
# Inbox helpers (used by the API layer in Phase 7)
# ---------------------------------------------------------------------------

def mark_as_read(
    db: Session, user_id: uuid.UUID, notification_id: uuid.UUID
) -> bool:
    """Mark one notification as read.  Returns ``True`` if a row updated."""
    result = db.execute(
        update(Notification)
        .where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
        .values(read=True)
    )
    db.commit()
    return result.rowcount > 0


def unread_count_for_user(db: Session, user_id: uuid.UUID) -> int:
    """Return the count of unread notifications for ``user_id``."""
    stmt = (
        select(Notification.id)
        .where(Notification.user_id == user_id, Notification.read.is_(False))
    )
    return len(db.execute(stmt).scalars().all())
