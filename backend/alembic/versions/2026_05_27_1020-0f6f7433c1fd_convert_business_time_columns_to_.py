"""convert business time columns to timestamptz

Revision ID: 0f6f7433c1fd
Revises: daa879540492
Create Date: 2026-05-27 10:20:07.035819

All business-time columns (event_date, recorded_at, reviewed_at, …) move
from ``timestamp without time zone`` to ``timestamp with time zone`` so
the application can store and reason over true instants.

The crucial bit is the ``USING`` clause.  Postgres' default conversion
treats the existing naive value as being in the **session's timezone** —
which on this developer's machine is IST (+5:30).  That would silently
shift every event by 5h30m.  We instead pass ``USING column AT TIME ZONE
'UTC'`` so existing values are interpreted as UTC, which matches what
the EHR parser has been storing (``_to_naive_utc`` always normalised to
UTC before persisting).

The downgrade path is symmetric: ``column AT TIME ZONE 'UTC'`` against a
``timestamptz`` returns the naive value at the UTC instant, restoring
the original semantics.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0f6f7433c1fd'
down_revision: Union[str, None] = 'daa879540492'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ----------------------------------------------------------------------
# Columns being converted.
#   (table, column, nullable)
# ----------------------------------------------------------------------
_COLUMNS: list[tuple[str, str, bool]] = [
    ('clinical_trials',   'last_synced_at', True),
    ('match_results',     'reviewed_at',    True),
    ('medical_events',    'event_date',     False),
    ('medical_events',    'end_date',       True),
    ('uncertainty_flags', 'resolved_at',    True),
    ('vital_readings',    'recorded_at',    False),
    ('wearable_devices',  'last_sync_at',   True),
]


def upgrade() -> None:
    """Convert naive ``timestamp`` columns to ``timestamptz``.

    Existing naive values are interpreted as UTC (which matches the
    ehr_parser's normalisation policy), producing the correct instant
    regardless of the database server's local timezone.
    """
    for table, column, nullable in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=postgresql.TIMESTAMP(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=nullable,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    """Revert ``timestamptz`` columns back to naive ``timestamp``.

    The reverse ``AT TIME ZONE 'UTC'`` strips the offset while preserving
    the underlying UTC wall-clock value, so a round-trip upgrade →
    downgrade is lossless.
    """
    for table, column, nullable in reversed(_COLUMNS):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=postgresql.TIMESTAMP(),
            existing_nullable=nullable,
            postgresql_using=f"{column} AT TIME ZONE 'UTC'",
        )
