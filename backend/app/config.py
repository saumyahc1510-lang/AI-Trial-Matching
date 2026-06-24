"""Application configuration using Pydantic Settings.

Loads environment variables from a .env file and provides a cached
singleton Settings instance via ``get_settings()``.  Comma-separated
environment variables (e.g. CORS_ORIGINS, TRIAL_SYNC_CONDITIONS) are
automatically parsed into Python lists.
"""

from enum import Enum
from functools import lru_cache

from pydantic_settings import BaseSettings


class LLMProvider(str, Enum):
    """Supported LLM provider backends."""

    GROQ = "groq"
    OPENAI = "openai"
    LOCAL = "local"


class Settings(BaseSettings):
    """Central application settings populated from environment variables.

    Attributes are grouped by subsystem.  The ``model_config`` block
    tells Pydantic-Settings to read a ``.env`` file located next to the
    project root and to treat variable names case-insensitively.
    """

    # ── Database ──────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://postgres:yourpassword@localhost:5432/trial_matching"

    # ── Groq / LLM ───────────────────────────────────────────────────
    GROQ_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.3-70b-versatile"
    LLM_PROVIDER: LLMProvider = LLMProvider.GROQ

    # ── NER ──────────────────────────────────────────────────────────
    # HuggingFace model used by :mod:`app.services.ner_engine`.  Override
    # if you want to swap in BioClinicalBERT, BioBERT-NER, etc.
    NER_MODEL_NAME: str = "d4data/biomedical-ner-all"

    # ── Diversity ranking ────────────────────────────────────────────
    # final_rank_score = α · (match * confidence) + β · diversity.
    # α + β need not sum to 1 — the result is clamped to [0, 1] anyway.
    DIVERSITY_RANK_ALPHA: float = 0.85
    DIVERSITY_RANK_BETA: float = 0.15

    # ── Redis / Celery ────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    USE_CELERY: bool = False

    # ── JWT Authentication ────────────────────────────────────────────
    JWT_SECRET_KEY: str = "change-this-to-a-random-secret-key-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # ── Trial Sync ────────────────────────────────────────────────────
    # Stored as a comma-separated string in .env to avoid pydantic-settings'
    # default JSON-decode of complex fields. Use the ``trial_sync_conditions``
    # property to get a parsed list.
    TRIAL_SYNC_CONDITIONS: str = "cancer,diabetes,cardiovascular disease"
    TRIAL_SYNC_INTERVAL_HOURS: int = 6

    # Criteria parsing is the LLM-expensive part of a sync.  Restrict it to
    # these categories (canonical names from app.services.trial_category) so
    # a sync doesn't burn the whole token budget parsing every trial in the
    # catalog.  Empty string = parse all categories.  Access via the
    # ``trial_parse_categories`` property.
    TRIAL_PARSE_CATEGORIES: str = "Endocrine & Metabolic,Oncology,Cardiovascular"
    # Hard cap on how many trials get their criteria parsed per sync run — a
    # second guard so a single sync can't exhaust the daily LLM quota.
    # Set to 0 to disable the cap (parse all pending in the allowed set).
    TRIAL_PARSE_MAX_PER_SYNC: int = 20

    # ── Application ───────────────────────────────────────────────────
    APP_NAME: str = "AI Clinical Trial Matcher"
    DEBUG: bool = True
    # SQLAlchemy ``echo`` is *separate* from DEBUG so we can keep the
    # Swagger UI enabled without flooding stdout (and tripping cp1252
    # encoding errors on Windows when SQL contains non-ASCII text).
    SQL_ECHO: bool = False
    API_V1_PREFIX: str = "/api/v1"
    # Comma-separated; access via ``cors_origins_list``.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8000"

    # ── HIPAA ─────────────────────────────────────────────────────────
    ENABLE_PHI_DEIDENTIFICATION: bool = True
    AUDIT_LOG_ENABLED: bool = True

    # ── Pydantic-Settings configuration ───────────────────────────────
    model_config = {
        "env_file": ".env",
        "case_sensitive": False,
        "extra": "ignore",
    }

    # ── Computed helpers ──────────────────────────────────────────────
    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        """Return CORS origins as a list (used by the ASGI app)."""
        return self._split_csv(self.CORS_ORIGINS)

    @property
    def trial_sync_conditions(self) -> list[str]:
        """Return the configured trial-sync conditions as a list."""
        return self._split_csv(self.TRIAL_SYNC_CONDITIONS)

    @property
    def trial_parse_categories(self) -> list[str]:
        """Return the categories whose trials are eligible for LLM criteria
        parsing.  Empty list means "all categories"."""
        return self._split_csv(self.TRIAL_PARSE_CATEGORIES)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton :class:`Settings` instance.

    Using ``lru_cache`` ensures the ``.env`` file is read only once and
    the same ``Settings`` object is reused across the application.
    """
    return Settings()
