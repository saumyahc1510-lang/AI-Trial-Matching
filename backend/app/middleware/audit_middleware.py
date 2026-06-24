"""Append-only audit logging middleware.

Every HTTP request that lands on the app produces one
:class:`~app.models.audit.AuditLog` row, regardless of whether the
request succeeded.  Combined with the database-level constraint that
``audit_logs`` has no UPDATE / DELETE grants (enforced by the model
config and verified in production via a DB-side rule), this gives us a
HIPAA-ready, queryable audit trail.

What we capture
---------------
* ``timestamp``        — when the response was produced.
* ``user_id``          — from the bearer token if present, else ``None``.
* ``action``           — derived from the HTTP method
  (``GET → read``, ``POST → create``, …).  Specific resource handlers
  may overwrite this via ``request.state.audit_action`` for richer
  semantics (e.g. ``match_triggered``).
* ``resource_type``    — first path segment after the API prefix
  (``patients``, ``trials``, …).
* ``resource_id``      — second path segment if present (the row UUID).
* ``ip_address`` / ``user_agent`` / ``request_path`` / ``request_method`` /
  ``response_status``.
* ``phi_accessed``     — heuristic: ``True`` whenever the path touches
  patient-scoped data (``patients``, ``matching``,
  ``feedback``, ``notifications``).

Design notes
------------
* Logging happens **after** the response so we know the status code.
* Failures inside the middleware are swallowed + logged — an audit-log
  bug must never take the API down.
* We open our own DB session because the request's ``get_db`` scope is
  already closed by the time the response is fully sent.
* Endpoints that handle their own bulk audit (e.g. a match run that
  writes a ``MATCH_TRIGGERED`` row inside the engine transaction) can
  set ``request.state.skip_audit = True`` to avoid a double-entry.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import Request, Response
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings
from app.database import _get_session_factory
from app.models.audit import AuditAction, AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path *segments* whose presence flips the ``phi_accessed`` flag on.
_PHI_PATH_SEGMENTS: frozenset[str] = frozenset({
    "patients",
    "matching",
    "feedback",
    "notifications",
})

# HTTP method → default :class:`AuditAction`.  Endpoints can override.
_METHOD_TO_ACTION: dict[str, AuditAction] = {
    "GET":     AuditAction.READ,
    "HEAD":    AuditAction.READ,
    "OPTIONS": AuditAction.READ,
    "POST":    AuditAction.CREATE,
    "PUT":     AuditAction.UPDATE,
    "PATCH":   AuditAction.UPDATE,
    "DELETE":  AuditAction.DELETE,
}

# Paths we deliberately skip (health checks, swagger UI assets, etc.) —
# these run continuously and have zero security value in the audit log.
_SKIP_PATHS: frozenset[str] = frozenset({
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_resource(path: str, api_prefix: str) -> tuple[str, Optional[str]]:
    """Return ``(resource_type, resource_id?)`` parsed from ``path``.

    Strips the API prefix first so ``/api/v1/patients/<uuid>`` becomes
    ``("patients", "<uuid>")``.  Paths shorter than two segments
    surface ``("system", None)``.
    """
    if api_prefix and path.startswith(api_prefix):
        rest = path[len(api_prefix):]
    else:
        rest = path
    rest = rest.strip("/")
    if not rest:
        return ("system", None)
    parts = rest.split("/")
    resource = parts[0]
    rid: Optional[str] = parts[1] if len(parts) > 1 else None
    if rid and "?" in rid:
        rid = rid.split("?", 1)[0]
    return (resource, rid)


def _path_touches_phi(path: str) -> bool:
    """Heuristic: does ``path`` plausibly access patient-scoped data?"""
    lowered = path.lower()
    return any(seg in lowered for seg in _PHI_PATH_SEGMENTS)


def _resolve_user_id(request: Request) -> Optional[uuid.UUID]:
    """Pull the authenticated user's UUID off ``request.state``.

    Set by the auth flow (or by other middleware) so the audit row is
    correlated with the caller.  The audit middleware **never** decodes
    the JWT itself — that's the auth dependency's job and we don't want
    to do double work on the hot path.
    """
    candidate = getattr(request.state, "user_id", None)
    if isinstance(candidate, uuid.UUID):
        return candidate
    if isinstance(candidate, str):
        try:
            return uuid.UUID(candidate)
        except ValueError:
            return None
    user_obj = getattr(request.state, "current_user", None)
    if isinstance(user_obj, User):
        return user_obj.id
    return None


def _client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP — prefers the X-Forwarded-For header.

    In a real deployment behind a load balancer the LB sets X-F-F;
    falling back to ``request.client.host`` keeps direct hits working.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Take the first IP in the chain.
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

class AuditMiddleware(BaseHTTPMiddleware):
    """Write one :class:`AuditLog` row per HTTP request.

    Honours ``settings.AUDIT_LOG_ENABLED`` (False during high-throughput
    test runs) and ``request.state.skip_audit`` (set by endpoints that
    audit themselves with richer semantics).
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()

        # Skip noise paths immediately — no body, no DB.
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        started = time.monotonic()
        response: Optional[Response] = None
        try:
            response = await call_next(request)
            return response
        finally:
            if settings.AUDIT_LOG_ENABLED and not getattr(
                request.state, "skip_audit", False
            ):
                try:
                    self._write_log(
                        request=request,
                        response=response,
                        api_prefix=settings.API_V1_PREFIX,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )
                except Exception as exc:  # noqa: BLE001 - never break the response
                    logger.warning("Audit log write failed: %s", exc)

    # ── Persistence ──────────────────────────────────────────────────

    def _write_log(
        self,
        *,
        request: Request,
        response: Optional[Response],
        api_prefix: str,
        duration_ms: int,
    ) -> None:
        """Open a short-lived session and append one audit row."""
        path = request.url.path
        method = request.method
        resource_type, resource_id = _extract_resource(path, api_prefix)
        action_override = getattr(request.state, "audit_action", None)
        action = (
            action_override
            if isinstance(action_override, AuditAction)
            else _METHOD_TO_ACTION.get(method, AuditAction.API_CALL)
        )
        status_code = response.status_code if response is not None else 500

        session: Session = _get_session_factory()()
        try:
            session.add(
                AuditLog(
                    user_id=_resolve_user_id(request),
                    action=action.value,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    details={"duration_ms": duration_ms},
                    ip_address=_client_ip(request),
                    user_agent=request.headers.get("user-agent"),
                    request_path=str(request.url.path),
                    request_method=method,
                    response_status=status_code,
                    phi_accessed=_path_touches_phi(path),
                )
            )
            session.commit()
        finally:
            session.close()


def install_audit_middleware(app) -> None:
    """Convenience wiring used by :mod:`app.main`."""
    app.add_middleware(AuditMiddleware)
