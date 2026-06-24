"""add trial category column

Revision ID: b4601bb48f78
Revises: 6e86b2ebed78
Create Date: 2026-05-28 20:42:16.263548

Adds the ``clinical_trials.category`` column + index and backfills it
for every existing trial using ``derive_category()`` on the row's
``conditions`` JSON.  Subsequent syncs populate the column at upsert
time, so this is a one-time data migration.

Importing the category service here is intentional — duplicating the
keyword rules in the migration would silently drift the moment we add
a new specialty.  Migrations run with the app on PYTHONPATH (via
``env.py``), so the import is safe.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4601bb48f78'
down_revision: Union[str, None] = '6e86b2ebed78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add column + index, then backfill ``category`` for existing rows."""
    op.add_column(
        'clinical_trials',
        sa.Column('category', sa.String(length=50), nullable=True),
    )
    op.create_index(
        op.f('ix_clinical_trials_category'),
        'clinical_trials',
        ['category'],
        unique=False,
    )

    # ── Backfill ────────────────────────────────────────────────────
    # Pull every row's id + conditions JSON, classify in Python, and
    # batch the updates.  We can't use ``WHERE category IS NULL`` to
    # skip already-set rows here because we *just* added the column —
    # everything is NULL.
    from app.services.trial_category import derive_category

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT id, conditions FROM clinical_trials"
    )).all()

    update_stmt = sa.text(
        "UPDATE clinical_trials SET category = :category WHERE id = :id"
    )
    for row_id, conditions in rows:
        category = derive_category(
            conditions if isinstance(conditions, list) else None
        )
        bind.execute(update_stmt, {"category": category, "id": row_id})


def downgrade() -> None:
    """Drop the column + index."""
    op.drop_index(op.f('ix_clinical_trials_category'), table_name='clinical_trials')
    op.drop_column('clinical_trials', 'category')
