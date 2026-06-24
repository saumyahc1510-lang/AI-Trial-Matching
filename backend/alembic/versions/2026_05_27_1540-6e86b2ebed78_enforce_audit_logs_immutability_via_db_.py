"""enforce audit_logs immutability via DB triggers

Revision ID: 6e86b2ebed78
Revises: 588f16c0d71d
Create Date: 2026-05-27 15:40:54.458866

Background
----------
``audit_logs`` is declared *append-only* at the application layer — no
ORM code path ever issues UPDATE or DELETE.  But "we promise we won't"
isn't sufficient evidence for a HIPAA / IRB auditor: a buggy
migration, a manual ``psql`` session, or a future feature could mutate
audit rows undetected.

This migration adds Postgres triggers that ``RAISE EXCEPTION`` whenever
anyone (including the application user) attempts UPDATE or DELETE on
``audit_logs``.  TRUNCATE is forbidden via a second trigger because
DELETE-blocking alone leaves the truncate path open as a workaround.

Inserts continue to work normally — the audit middleware still writes
one row per request.

Why triggers (not a CHECK or a role grant)
------------------------------------------
* A CHECK constraint can't reference "old row vs new row" — that's
  exactly what a trigger does.
* A role grant (``REVOKE UPDATE, DELETE ON audit_logs FROM appuser``)
  works in production but is brittle: the developer running migrations
  is usually the table owner, and ownership trumps grants.  Triggers
  fire for **everyone**, including the table owner.

The downgrade path drops both triggers.  No data is touched in either
direction.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '6e86b2ebed78'
down_revision: Union[str, None] = '588f16c0d71d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

# Two trigger functions: one blocks UPDATE + DELETE, the other blocks
# TRUNCATE.  Both raise a ``feature_not_supported`` (SQLSTATE 0A000)
# so callers can detect this specific failure programmatically.
_CREATE_BLOCK_MUTATIONS_FN = """
CREATE OR REPLACE FUNCTION audit_logs_block_mutations()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'feature_not_supported';
END;
$$;
"""

_CREATE_BLOCK_TRUNCATE_FN = """
CREATE OR REPLACE FUNCTION audit_logs_block_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only; TRUNCATE is not permitted'
        USING ERRCODE = 'feature_not_supported';
END;
$$;
"""

# Row-level trigger for UPDATE + DELETE.
_CREATE_ROW_TRIGGER = """
CREATE TRIGGER audit_logs_no_update_delete
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW
EXECUTE FUNCTION audit_logs_block_mutations();
"""

# Statement-level trigger for TRUNCATE (which doesn't have row context).
_CREATE_STATEMENT_TRIGGER = """
CREATE TRIGGER audit_logs_no_truncate
BEFORE TRUNCATE ON audit_logs
FOR EACH STATEMENT
EXECUTE FUNCTION audit_logs_block_truncate();
"""

# Downgrade — drop triggers first, then their functions.
_DROP_STATEMENTS = [
    "DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs;",
    "DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs;",
    "DROP FUNCTION IF EXISTS audit_logs_block_mutations();",
    "DROP FUNCTION IF EXISTS audit_logs_block_truncate();",
]


def upgrade() -> None:
    """Install both trigger functions and attach the triggers."""
    op.execute(_CREATE_BLOCK_MUTATIONS_FN)
    op.execute(_CREATE_BLOCK_TRUNCATE_FN)
    op.execute(_CREATE_ROW_TRIGGER)
    op.execute(_CREATE_STATEMENT_TRIGGER)


def downgrade() -> None:
    """Remove triggers + functions — restores ordinary table semantics."""
    for stmt in _DROP_STATEMENTS:
        op.execute(stmt)
