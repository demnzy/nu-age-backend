"""refresh_tokens user_id on delete cascade

Revision ID: a6df98058267
Revises: d5e82b7149a0
Create Date: 2026-10-07 00:39:49.404275

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6df98058267'
down_revision: Union[str, Sequence[str], None] = 'd5e82b7149a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
