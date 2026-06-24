"""Alembic migration environment for the AI Clinical Trial Matching system.

This module configures Alembic to discover all SQLAlchemy models via
``app.models`` and read the database URL from the environment (through
``python-dotenv`` and ``app.config``).
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Ensure the project root (backend/) is on sys.path so that ``app.*``
# imports work when Alembic is invoked from the backend/ directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env before any app imports so that DATABASE_URL is available.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ---------------------------------------------------------------------------
# Import all models so that Base.metadata is fully populated before
# Alembic inspects it for autogenerate diffs.
# ---------------------------------------------------------------------------
from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402, F401
    AuditLog,
    ClinicianFeedback,
    ClinicalTrial,
    CriterionEvaluation,
    LLMUsage,
    MatchResult,
    MedicalEvent,
    Notification,
    Patient,
    PatientVersion,
    TrialCriterion,
    TrialSite,
    UncertaintyFlag,
    User,
)

# Alembic Config object — provides access to values in alembic.ini.
config = context.config

# Configure Python logging from the .ini file.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target metadata for autogenerate support.
target_metadata = Base.metadata

# ---------------------------------------------------------------------------
# Override sqlalchemy.url with the runtime DATABASE_URL if available.
# This lets us keep alembic.ini free of real credentials.
# ---------------------------------------------------------------------------
from app.config import get_settings  # noqa: E402

_settings = get_settings()
# ConfigParser uses ``%`` as its interpolation character, which collides
# with percent-encoded characters in DB URLs (e.g. an URL-encoded ``@`` is
# ``%40``).  Escape every ``%`` to ``%%`` so ConfigParser leaves the URL
# untouched; SQLAlchemy then receives the original value.
config.set_main_option("sqlalchemy.url", _settings.DATABASE_URL.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    In this mode Alembic emits the SQL statements to stdout (or a file)
    without requiring a live database connection.  This is useful for
    generating SQL scripts for review before applying.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Creates a real database connection and runs each migration inside a
    transaction that is committed on success or rolled back on failure.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
