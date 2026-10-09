"""add_cbt_columns_and_study_bundles

Revision ID: 1025dffa04bc
Revises: a6df98058267
Create Date: 2026-10-09 20:11:14.214161

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1025dffa04bc'
down_revision: Union[str, Sequence[str], None] = 'a6df98058267'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Create study_bundles table
    op.create_table(
        'study_bundles',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=100), nullable=False),
        sa.Column('theme_gradient', sa.String(length=100), nullable=False),
        sa.Column('banner_url', sa.String(), nullable=True),
        sa.Column('price_coins', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('discount_percentage', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_official', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='approved'),
        sa.Column('downloads_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_study_bundles_category'), 'study_bundles', ['category'], unique=False)
    op.create_index(op.f('ix_study_bundles_created_at'), 'study_bundles', ['created_at'], unique=False)
    op.create_index(op.f('ix_study_bundles_id'), 'study_bundles', ['id'], unique=False)
    op.create_index(op.f('ix_study_bundles_is_official'), 'study_bundles', ['is_official'], unique=False)
    op.create_index(op.f('ix_study_bundles_price_coins'), 'study_bundles', ['price_coins'], unique=False)
    op.create_index(op.f('ix_study_bundles_status'), 'study_bundles', ['status'], unique=False)
    op.create_index(op.f('ix_study_bundles_title'), 'study_bundles', ['title'], unique=False)

    # 2. Create study_bundle_items table
    op.create_table(
        'study_bundle_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('bundle_id', sa.UUID(), nullable=False),
        sa.Column('pack_id', sa.UUID(), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['bundle_id'], ['study_bundles.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['pack_id'], ['study_packs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_study_bundle_items_bundle_id'), 'study_bundle_items', ['bundle_id'], unique=False)
    op.create_index(op.f('ix_study_bundle_items_id'), 'study_bundle_items', ['id'], unique=False)
    op.create_index(op.f('ix_study_bundle_items_pack_id'), 'study_bundle_items', ['pack_id'], unique=False)

    # 3. Add backward-compatible nullable CBT metadata & diagram columns to questions
    op.add_column('questions', sa.Column('image_url', sa.String(), nullable=True))
    op.add_column('questions', sa.Column('topic', sa.String(length=150), nullable=True))
    op.add_column('questions', sa.Column('exam_type', sa.String(length=50), nullable=True))
    op.add_column('questions', sa.Column('exam_year', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('questions', 'exam_year')
    op.drop_column('questions', 'exam_type')
    op.drop_column('questions', 'topic')
    op.drop_column('questions', 'image_url')

    op.drop_index(op.f('ix_study_bundle_items_pack_id'), table_name='study_bundle_items')
    op.drop_index(op.f('ix_study_bundle_items_id'), table_name='study_bundle_items')
    op.drop_index(op.f('ix_study_bundle_items_bundle_id'), table_name='study_bundle_items')
    op.drop_table('study_bundle_items')

    op.drop_index(op.f('ix_study_bundles_title'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_status'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_price_coins'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_is_official'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_id'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_created_at'), table_name='study_bundles')
    op.drop_index(op.f('ix_study_bundles_category'), table_name='study_bundles')
    op.drop_table('study_bundles')
