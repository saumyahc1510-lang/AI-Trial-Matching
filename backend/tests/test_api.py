"""API-layer tests via FastAPI's :class:`TestClient`.

These tests exercise the HTTP surface — auth + RBAC + the
patient/trial/matching routes — using the :func:`client` fixture from
``conftest.py``.  No external network is touched (the LLM is faked
when the matching path requires it).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models.user import UserRole


# ---------------------------------------------------------------------------
# Health + auth basics
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert "version" in body


def test_unauthenticated_request_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_login_with_correct_password_returns_token(
    client: TestClient, make_user
) -> None:
    user = make_user(password="GoodPass!1")
    response = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "GoodPass!1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["role"] == user.role


def test_login_with_wrong_password_is_401(client: TestClient, make_user) -> None:
    user = make_user(password="Right!1pass")
    response = client.post(
        "/api/v1/auth/login",
        data={"username": user.email, "password": "Wrong!1pass"},
    )
    assert response.status_code == 401
    # Identical to "user not found" — no account enumeration.
    body = response.json()
    assert "invalid" in body["detail"].lower()


def test_login_unknown_email_is_401(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/login",
        data={"username": "noone@nowhere.test", "password": "anything"},
    )
    assert response.status_code == 401


def test_me_returns_profile(client: TestClient, auth_token) -> None:
    response = client.get("/api/v1/auth/me", headers=auth_token["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == auth_token["user"].email
    assert body["role"] == UserRole.COORDINATOR.value


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

def test_coordinator_cannot_access_admin_endpoints(
    client: TestClient, auth_token
) -> None:
    response = client.get("/api/v1/admin/users", headers=auth_token["headers"])
    assert response.status_code == 403


def test_admin_can_access_admin_endpoints(client: TestClient, make_user) -> None:
    admin = make_user(role=UserRole.ADMIN, password="AdminPass!1")
    login = client.post(
        "/api/v1/auth/login",
        data={"username": admin.email, "password": "AdminPass!1"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.get("/api/v1/admin/users", headers=headers)
    assert response.status_code == 200
    body = response.json()
    # Paginated envelope: { items, total, limit, offset }.
    assert isinstance(body["items"], list)
    assert body["total"] >= 1


def test_patient_cannot_access_other_patient_record(
    client: TestClient, make_patient, make_user
) -> None:
    """Patient-role users can only see their own ``associated_patient_id``."""
    other_patient = make_patient()
    pt_user = make_user(
        role=UserRole.PATIENT, password="PatientPw1!",
        # NOT linked to other_patient
        associated_patient_id=None,
    )
    login = client.post(
        "/api/v1/auth/login",
        data={"username": pt_user.email, "password": "PatientPw1!"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = client.get(
        f"/api/v1/patients/{other_patient.id}", headers=headers
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Patient + FHIR ingestion
# ---------------------------------------------------------------------------

def _days_ago_iso(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


def test_fhir_bootstrap_creates_patient_with_events(
    client: TestClient, auth_token
) -> None:
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "p-api-test",
                    "identifier": [{"value": "API-TEST-001"}],
                    "name": [{"family": "ApiTest", "given": ["Patient"]}],
                    "gender": "female",
                    "birthDate": "1972-05-10",
                }
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "c1",
                    "code": {
                        "coding": [{
                            "system": "http://snomed.info/sct",
                            "code": "254837009",
                            "display": "Invasive ductal breast carcinoma",
                        }]
                    },
                    "recordedDate": _days_ago_iso(180),
                }
            },
        ],
    }
    response = client.post(
        "/api/v1/patients/fhir",
        headers=auth_token["headers"],
        json={"bundle": bundle},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["events_created"] == 1
    assert body["patient_id"]

    # Re-ingestion is idempotent.
    response2 = client.post(
        "/api/v1/patients/fhir",
        headers=auth_token["headers"],
        json={"bundle": bundle},
    )
    body2 = response2.json()
    assert body2["events_created"] == 0
    assert body2["events_skipped"] == 1


def test_patient_timeline_returns_chronological_events(
    client: TestClient, auth_token, make_patient, db_session
) -> None:
    """Add events directly + verify the timeline endpoint returns them sorted."""
    from app.models import MedicalEvent
    from app.models.patient import EventStatusEnum, EventTypeEnum

    patient = make_patient()
    for days_ago, name, etype in [
        (200, "Older lab", EventTypeEnum.LAB_RESULT),
        (30, "Newer lab", EventTypeEnum.LAB_RESULT),
    ]:
        db_session.add(
            MedicalEvent(
                patient_id=patient.id,
                event_type=etype.value,
                event_date=datetime.now(timezone.utc) - timedelta(days=days_ago),
                display_name=name,
                status=EventStatusEnum.ACTIVE.value,
            )
        )
    db_session.flush()

    response = client.get(
        f"/api/v1/patients/{patient.id}/timeline",
        headers=auth_token["headers"],
    )
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 2
    # Older event comes first.
    assert events[0]["display_name"] == "Older lab"
    assert events[1]["display_name"] == "Newer lab"


# ---------------------------------------------------------------------------
# Trial routes
# ---------------------------------------------------------------------------

def test_list_trials_requires_auth(client: TestClient) -> None:
    """Listing trials still requires a valid token (any role)."""
    response = client.get("/api/v1/trials/")
    assert response.status_code == 401


def test_list_trials_authenticated(client: TestClient, auth_token) -> None:
    response = client.get(
        "/api/v1/trials/?limit=10", headers=auth_token["headers"]
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def test_notifications_empty_for_new_user(client: TestClient, auth_token) -> None:
    response = client.get(
        "/api/v1/notifications/", headers=auth_token["headers"]
    )
    assert response.status_code == 200
    assert response.json() == []


def test_unread_count_returns_zero_for_new_user(
    client: TestClient, auth_token
) -> None:
    response = client.get(
        "/api/v1/notifications/unread/count", headers=auth_token["headers"]
    )
    assert response.status_code == 200
    assert response.json() == 0
