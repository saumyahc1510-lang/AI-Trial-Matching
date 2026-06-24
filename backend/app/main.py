"""FastAPI application entry point for the AI Clinical Trial Matching system."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.middleware.audit_middleware import install_audit_middleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup/shutdown events.

    Runs setup logic when the ASGI server starts and teardown logic
    when it shuts down.  The ``yield`` separates the two phases.
    """
    settings = get_settings()
    # ── Startup ───────────────────────────────────────────────────────
    # ASCII-only — emoji here would crash on Windows cp1252 consoles
    # when uvicorn prints these lines (TestClient never executes them,
    # which is why this only surfaced on a real `uvicorn app.main:app`).
    db_host = (
        settings.DATABASE_URL.split('@')[-1]
        if '@' in settings.DATABASE_URL else 'configured'
    )
    print(f"[startup] {settings.APP_NAME}")
    print(f"[startup] Debug mode: {settings.DEBUG}")
    print(f"[startup] LLM provider: {settings.LLM_PROVIDER} / model: {settings.LLM_MODEL}")
    print(f"[startup] Database: {db_host}")
    yield
    # ── Shutdown ──────────────────────────────────────────────────────
    print(f"[shutdown] {settings.APP_NAME}")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        A fully configured :class:`FastAPI` instance with CORS middleware,
        a ``/health`` endpoint, and (in future phases) the versioned API
        router mounted at ``/api/v1``.
    """
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description=(
            "AI-powered clinical trial matching with temporal reasoning, "
            "uncertainty-aware evaluation, and criterion-level explainability."
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # ── CORS middleware ───────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health-check endpoint ─────────────────────────────────────────
    @app.get("/health", tags=["System"])
    async def health_check() -> dict:
        """Return basic application health and configuration info."""
        return {
            "status": "healthy",
            "app": settings.APP_NAME,
            "version": "0.1.0",
            "llm_provider": settings.LLM_PROVIDER,
            "debug": settings.DEBUG,
        }

    # ── Audit middleware (Phase 7) ────────────────────────────────────
    # Installed *before* the routes so it wraps every endpoint, but
    # *after* CORS so preflight requests don't generate audit rows.
    install_audit_middleware(app)

    # ── API routes ────────────────────────────────────────────────────
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
