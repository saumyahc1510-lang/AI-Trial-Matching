"""Aggregate router for ``/api/v1``.

Imports each sub-router module and stitches them together so
``app.main`` can ``include_router(api_router, prefix=API_V1_PREFIX)``
with one line.

Order matters only for OpenAPI grouping in Swagger UI — the visual
order in the generated docs follows the inclusion order here.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    admin,
    audit,
    auth,
    feedback,
    intake,
    matching,
    notifications,
    patients,
    trials,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(patients.router)
api_router.include_router(trials.router)
api_router.include_router(matching.router)
api_router.include_router(intake.router)
api_router.include_router(feedback.router)
api_router.include_router(notifications.router)
api_router.include_router(admin.router)
api_router.include_router(audit.router)
