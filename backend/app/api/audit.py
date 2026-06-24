"""Audit-log query endpoint — ``/api/v1/audit``.

Admin-only.  The ``audit_logs`` table is append-only at the application
layer; this endpoint is the read interface that compliance officers use
for IRB / HIPAA spot-checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.database import get_db
from app.models.audit import AuditLog
from app.models.user import UserRole

router = APIRouter(
    prefix="/audit",
    tags=["Audit"],
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)


class AuditLogRead(BaseModel):
    """Audit log row shaped for JSON output."""

    id: uuid.UUID
    timestamp: datetime
    user_id: Optional[uuid.UUID] = None
    action: str
    resource_type: str
    resource_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_path: Optional[str] = None
    request_method: Optional[str] = None
    response_status: Optional[int] = None
    phi_accessed: bool
    details: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class AuditStats(BaseModel):
    """Aggregate counters for the admin dashboard stat card.

    Unlike the row listing (which is capped at ``limit``), these are true
    counts over the whole table so the dashboard can show a real number
    rather than ``min(total, limit)``.
    """

    total: int
    last_24h: int
    phi_last_24h: int


@router.get(
    "/stats",
    response_model=AuditStats,
    summary="Aggregate audit-log counters (total + last 24h).",
)
def audit_stats(db: Session = Depends(get_db)) -> AuditStats:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    total = db.execute(
        select(func.count()).select_from(AuditLog)
    ).scalar_one()
    last_24h = db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.timestamp >= since)
    ).scalar_one()
    phi_last_24h = db.execute(
        select(func.count())
        .select_from(AuditLog)
        .where(AuditLog.timestamp >= since, AuditLog.phi_accessed.is_(True))
    ).scalar_one()
    return AuditStats(total=total, last_24h=last_24h, phi_last_24h=phi_last_24h)


@router.get(
    "/",
    response_model=list[AuditLogRead],
    summary="Query audit-log rows (filterable).",
)
def query_audit(
    db: Session = Depends(get_db),
    user_id: Optional[uuid.UUID] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    phi_only: bool = Query(False, description="Restrict to PHI-touching calls."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[AuditLogRead]:
    stmt = (
        select(AuditLog)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
        .offset(offset)
    )
    if user_id is not None:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditLog.resource_id == resource_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if phi_only:
        stmt = stmt.where(AuditLog.phi_accessed.is_(True))
    rows = db.execute(stmt).scalars().all()
    return [AuditLogRead.model_validate(r) for r in rows]
