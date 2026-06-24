"""Authentication & authorisation primitives.

This module is consumed by every API route module.  It deliberately
lives at the top level of ``app/`` (not under ``app/middleware/``)
because the FastAPI ``Depends`` graph treats these as injectable
*dependencies*, not request-pipeline middleware.

What's here
-----------
* **Password hashing** — bcrypt via Passlib's CryptContext.  We use
  Passlib so the algorithm is upgradeable without touching call-sites.
* **JWT helpers** — encode / decode using HS256 with the
  ``JWT_SECRET_KEY`` from settings.  Tokens carry the user's ``sub``
  (UUID), ``role``, ``email``, and an ``exp`` claim.
* **FastAPI dependencies** —
    - :func:`get_current_user` extracts the bearer token, decodes it,
      loads the user, and validates ``is_active``.
    - :func:`require_role` / :func:`require_any_role` enforce RBAC at
      the endpoint signature, so the router itself stays declarative.

The token-creation helper is also used by tests + the seed script, so
no API-only logic leaks in here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# Bcrypt enforces a 72-byte ceiling on the password input.  We hash any
# longer passphrase down to a fixed-length SHA-256 digest *first*,
# encoded base64 (~44 chars), so users with very long passphrases still
# work safely.  This is the same prehashing strategy Django uses.
def _prepare_password_bytes(plain_password: str) -> bytes:
    """Convert ``plain_password`` to ≤72 bytes for bcrypt.

    Short passwords pass straight through; long ones get a SHA-256
    prehash so we never trip bcrypt's 72-byte limit.
    """
    raw = plain_password.encode("utf-8")
    if len(raw) <= 72:
        return raw
    import base64
    import hashlib

    digest = hashlib.sha256(raw).digest()
    return base64.b64encode(digest)  # 44 bytes — well under the limit


def hash_password(plain_password: str) -> str:
    """Return a bcrypt hash (cost 12) of ``plain_password`` as a UTF-8 string."""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(_prepare_password_bytes(plain_password), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Constant-time compare; safe against malformed hashes."""
    try:
        return bcrypt.checkpw(
            _prepare_password_bytes(plain_password),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed hash — treat as failed verification, not crash.
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    email: str,
    expires_minutes: Optional[int] = None,
) -> str:
    """Sign a JWT for the given user.

    ``expires_minutes`` overrides the configured default — useful for
    short-lived links (password resets, magic logins).
    """
    settings = get_settings()
    minutes = (
        expires_minutes
        if expires_minutes is not None
        else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode + verify a JWT.  Raises :class:`HTTPException` 401 on failure.

    The HTTPException is raised here (not at the dependency layer) so
    that *all* code paths produce a consistent error shape.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return payload


# ---------------------------------------------------------------------------
# OAuth2 bearer + dependencies
# ---------------------------------------------------------------------------

# ``tokenUrl`` is the path the Swagger UI calls when the user clicks
# "Authorize".  It's relative to the app root.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{get_settings().API_V1_PREFIX}/auth/login",
    auto_error=False,
)


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the current bearer token to a :class:`User`.

    Raises 401 on missing / invalid / expired token, or when the user
    has been deactivated since the token was issued.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing subject.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id = uuid.UUID(sub)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token subject is not a valid user id.",
        ) from exc

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )
    return user


def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Same as :func:`get_current_user` but returns ``None`` instead of raising.

    Used by middleware (audit) that wants to *record* anonymous calls
    rather than reject them.
    """
    if not token:
        return None
    try:
        return get_current_user(token=token, db=db)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# RBAC dependencies
# ---------------------------------------------------------------------------

def require_role(*allowed: UserRole) -> Callable[..., User]:
    """Dependency factory: require the caller's role be one of ``allowed``.

    Usage::

        @router.post(
            "/something",
            dependencies=[Depends(require_role(UserRole.ADMIN))],
        )
    """
    allowed_values = {r.value for r in allowed}

    def _dep(current: User = Depends(get_current_user)) -> User:
        if current.role not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Requires one of roles: {sorted(allowed_values)}; "
                    f"you have {current.role!r}."
                ),
            )
        return current

    return _dep


def require_any_role(*allowed: UserRole) -> Callable[..., User]:
    """Alias for readability when the calling code lists multiple roles."""
    return require_role(*allowed)


# ---------------------------------------------------------------------------
# Patient-scoped authorisation
# ---------------------------------------------------------------------------

def can_user_access_patient(user: User, patient_id: uuid.UUID) -> bool:
    """Decide whether ``user`` may access the given patient record.

    Today's policy:

    * ADMIN, CLINICIAN, COORDINATOR — full access to every patient.
    * PATIENT — only their own ``associated_patient_id``.
    * SPONSOR — no per-patient access (only aggregate views).
    """
    role = user.role
    if role in {
        UserRole.ADMIN.value,
        UserRole.CLINICIAN.value,
        UserRole.COORDINATOR.value,
    }:
        return True
    if role == UserRole.PATIENT.value:
        return user.associated_patient_id == patient_id
    return False


def ensure_can_access_patient(user: User, patient_id: uuid.UUID) -> None:
    """Raise 403 unless :func:`can_user_access_patient` returns ``True``."""
    if not can_user_access_patient(user, patient_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this patient.",
        )
