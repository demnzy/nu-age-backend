"""migrate_course_public_to_string

Revision ID: be7580fc89aa
Revises: e18037ca7371
Create Date: 2026-08-13 22:15:31.703999

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'be7580fc89aa'
down_revision: Union[str, Sequence[str], None] = 'e18037ca7371'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('courses', 'public', type_=sa.String(), postgresql_using="CASE WHEN public=True THEN 'true' ELSE 'false' END")


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('courses', 'public', type_=sa.Boolean(), postgresql_using="public='true'")
