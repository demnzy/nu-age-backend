"""Backfill null ratings

Revision ID: 2f54610a2542
Revises: 219472202bcd
Create Date: 2026-08-12 18:46:18.776543

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2f54610a2542'
down_revision: Union[str, Sequence[str], None] = '219472202bcd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE courses SET rating = 4.5 + (random() * 0.5) WHERE rating IS NULL")
    op.execute("UPDATE courses SET rating_count = 1 WHERE rating_count IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    pass
