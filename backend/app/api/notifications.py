"""Notification inbox endpoints — ``/api/v1/notifications``.

Routes
------
``GET   /``                 — list this user's notifications.
``GET   /unread/count``     — unread badge counter.
``POST  /{id}/read``        — mark one as read.
``POST  /read-all``         — mark every unread notification for the user as read.
"""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.notification import Notification, NotificationType
from app.models.user import User
from datetime import datetime

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class NotificationRead(BaseModel):
    """Notification payload shaped for the inbox UI."""

    id: uuid.UUID
    notification_type: NotificationType
    channel: str
    title: str
    message: str
    read: bool
    delivered: bool
    patient_id: Optional[uuid.UUID] = None
    trial_id: Optional[uuid.UUID] = None
    match_result_id: Optional[uuid.UUID] = None
    details: Optional[dict] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


@router.get(
    "/",
    response_model=list[NotificationRead],
    summary="List notifications for the current user (newest first).",
)
def list_notifications(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[NotificationRead]:
    stmt = (
        select(Notification)
        .where(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if unread_only:
        stmt = stmt.where(Notification.read.is_(False))
    rows = db.execute(stmt).scalars().all()
    return [NotificationRead.model_validate(n) for n in rows]


@router.get(
    "/unread/count",
    response_model=int,
    summary="Unread notification count for the badge.",
)
def unread_count(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> int:
    return (
        db.execute(
            select(Notification.id).where(
                Notification.user_id == current_user.id,
                Notification.read.is_(False),
            )
        ).scalars().all()
    ).__len__()


@router.post(
    "/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Mark one notification as read.",
)
def mark_one_read(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notif = db.get(Notification, notification_id)
    if notif is None or notif.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )
    notif.read = True
    db.commit()


@router.post(
    "/read-all",
    summary="Mark every unread notification for the user as read.",
)
def mark_all_read(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = db.execute(
        update(Notification)
        .where(
            Notification.user_id == current_user.id,
            Notification.read.is_(False),
        )
        .values(read=True)
    )
    db.commit()
    return {"updated": result.rowcount}
