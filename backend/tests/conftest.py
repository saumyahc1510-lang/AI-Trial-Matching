"""Shared test fixtures for the AI Clinical Trial Matching backend.

Design goals
------------
* **No network calls.**  The :class:`FakeLLMClient` fixture replaces
  the real Groq client so tests never hit the wire — they run in
  milliseconds and are deterministic.
* **Real Postgres, real transactions.**  Tests run against the same
  ``trial_matching`` database the app uses, but every test is wrapped
  in a transaction that's rolled back on teardown.  We get full
  schema fidelity (JSONB, triggers, foreign keys) without the
  per-test data leakage SQLite would introduce.
* **One TestClient.**  Shared across module-scoped fixtures via
  ``app.dependency_overrides`` so fixtures don't fight over the auth
  + db dependency graph.

To run the suite::

    pytest -v
    pytest -v -k temporal
    pytest --cov=app
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Make ``import app.*`` work when pytest is invoked from anywhere.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Force eager Celery + audit-off for tests *before* anything imports app.config.
os.environ.setdefault("USE_CELERY", "false")
os.environ.setdefault("AUDIT_LOG_ENABLED", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth import hash_password  # noqa: E402
from app.database import _get_engine, _get_session_factory, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Patient, User  # noqa: E402
from app.models.patient import (  # noqa: E402
    EventStatusEnum,
    EventTypeEnum,
    PatientStatusEnum,
    SexEnum,
)
from app.models.user import UserRole  # noqa: E402
from app.services import llm_client as _llm_module  # noqa: E402
from app.services.llm_client import LLMResult  # noqa: E402


# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------

class FakeLLMClient:
    """Stand-in for :class:`app.services.llm_client.LLMClient`.

    Every call returns a canned response sourced from one of:

    * ``json_responses`` — a dict keyed by a substring of the user
      prompt (first match wins).  Most tests use this.
    * ``text_responses`` — same idea for plain-text completions.

    Tests can swap responses mid-run by assigning to the dict — the
    same fixture instance is reused.
    """

    model = "fake-llm"

    def __init__(self) -> None:
        # Default JSON shape mirrors what the eligibility reasoner expects.
        self.json_responses: dict[str, dict] = {}
        self.text_responses: dict[str, str] = {}
        self.calls: list[tuple[str, str | None]] = []
        # Sensible default verdicts so tests don't have to enumerate
        # every criterion they touch.
        self.default_json: dict = {
            "status": "uncertain",
            "reasoning": "FakeLLMClient default response.",
            "confidence": 0.5,
            "evidence_text": None,
            "evidence_event_index": None,
            "missing_data": "Default fake stub.",
        }
        self.default_text: str = "Fake summary text."

    # ── LLMClient interface ──────────────────────────────────────────

    # ``operation`` was added to LLMClient so the admin dashboard can
    # break out token usage by service.  The fake accepts it (and any
    # other forward-compatible kwargs) without inspecting it.
    def complete(self, prompt: str, *, system: str | None = None,
                 temperature: float = 0.0, max_tokens: int = 1024,
                 operation: str | None = None, **_kw) -> LLMResult:
        self.calls.append((prompt, system))
        for key, text in self.text_responses.items():
            if key in prompt:
                return LLMResult(text=text, model=self.model)
        return LLMResult(text=self.default_text, model=self.model)

    def complete_json(self, prompt: str, *, system: str | None = None,
                      temperature: float = 0.0, max_tokens: int = 2048,
                      operation: str | None = None, **_kw) -> dict:
        self.calls.append((prompt, system))
        for key, payload in self.json_responses.items():
            if key in prompt:
                return payload
        return dict(self.default_json)


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLMClient:
    """Replace ``get_llm_client`` with a :class:`FakeLLMClient` for the test."""
    client = FakeLLMClient()
    monkeypatch.setattr(_llm_module, "_client", client, raising=False)
    monkeypatch.setattr(_llm_module, "get_llm_client", lambda: client)

    # Many services capture the client at function start via the default
    # ``client=None`` argument; patching the module-level accessor covers
    # those.  Services that explicitly import ``get_llm_client`` re-bound
    # to a local reference need their own monkeypatch in the test.
    return client


# ---------------------------------------------------------------------------
# Per-test transactional DB
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session() -> Iterator[Session]:
    """Yield a SQLAlchemy session whose work is rolled back on teardown.

    Pattern lifted from the SQLAlchemy docs' "joining a session into an
    external transaction" recipe: we open a connection, start a top-
    level transaction, then bind a session to the connection.  Every
    nested ``session.commit()`` inside the test body fires a SAVEPOINT
    via the after-transaction listener instead of a real commit, so the
    final ``connection.rollback()`` wipes everything cleanly.
    """
    engine = _get_engine()
    connection = engine.connect()
    trans = connection.begin()

    SessionFactory = _get_session_factory()
    # Bind the session to our pre-opened connection so it shares the
    # outer transaction.
    session = SessionFactory(bind=connection)

    # Convert in-test commits to savepoint releases.
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction_obj):  # noqa: ANN001
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Patient / user factories
# ---------------------------------------------------------------------------

@pytest.fixture
def make_patient(db_session: Session) -> Callable[..., Patient]:
    """Factory that returns saved :class:`Patient` rows with sane defaults."""

    def _make(
        *,
        external_id: str | None = None,
        first_name: str = "Test",
        last_name: str = "Patient",
        date_of_birth: date = date(1970, 1, 1),
        sex: SexEnum = SexEnum.FEMALE,
        race: str | None = None,
        ethnicity: str | None = None,
        status: PatientStatusEnum = PatientStatusEnum.ACTIVE,
    ) -> Patient:
        patient = Patient(
            external_id=external_id or f"TEST-{uuid.uuid4().hex[:10]}",
            first_name=first_name,
            last_name=last_name,
            date_of_birth=date_of_birth,
            sex=sex.value,
            race=race,
            ethnicity=ethnicity,
            preferred_language="en",
            status=status.value,
            current_version=1,
        )
        db_session.add(patient)
        db_session.flush()
        return patient

    return _make


@pytest.fixture
def make_user(db_session: Session) -> Callable[..., User]:
    """Factory for :class:`User` rows.  Auto-generates a unique email."""

    def _make(
        *,
        role: UserRole = UserRole.COORDINATOR,
        email: str | None = None,
        password: str = "TestPass!1",
        full_name: str = "Test User",
        associated_patient_id: uuid.UUID | None = None,
    ) -> User:
        # ``example.com`` is the IETF-reserved test domain (RFC 2606);
        # ``.local`` would be rejected by ``email-validator`` as a
        # special-use reserved name (RFC 6762).
        user = User(
            email=email or f"u-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password=hash_password(password),
            full_name=full_name,
            role=role.value,
            is_active=True,
            associated_patient_id=associated_patient_id,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make


# ---------------------------------------------------------------------------
# TestClient with dependency override
# ---------------------------------------------------------------------------

@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """FastAPI :class:`TestClient` whose ``get_db`` returns ``db_session``."""

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def auth_token(client: TestClient, make_user: Callable[..., User]) -> dict:
    """Returns ``(user, headers)`` for a freshly-created coordinator login.

    The yielded headers can be merged into any request::

        r = client.get("/api/v1/auth/me", headers=auth_token["headers"])
    """
    user = make_user(role=UserRole.COORDINATOR, password="TestPass!1")
    response = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "TestPass!1"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {
        "user": user,
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
        "token": body["access_token"],
    }
