"""add_study_marketplace_tables

Revision ID: d5e82b7149a0
Revises: c1a84391e92d
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = 'd5e82b7149a0'
down_revision = 'c1a84391e92d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. Create study_packs table if not exists
    if 'study_packs' not in existing_tables:
        op.create_table(
            'study_packs',
            sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('creator_id', UUID(as_uuid=True), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
            sa.Column('source_material_id', UUID(as_uuid=True), sa.ForeignKey('study_materials.id', ondelete='SET NULL'), nullable=True),
            sa.Column('title', sa.String(255), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('category', sa.String(100), server_default='General', nullable=False),
            sa.Column('theme_gradient', sa.String(100), server_default='purple_indigo', nullable=False),
            sa.Column('cover_image_url', sa.String(), nullable=True),
            sa.Column('price_coins', sa.Integer(), server_default='0', nullable=False),
            sa.Column('is_official', sa.Boolean(), server_default=sa.text('false'), nullable=False),
            sa.Column('status', sa.String(50), server_default='pending_review', nullable=False),
            sa.Column('admin_review_notes', sa.Text(), nullable=True),
            sa.Column('reward_coins_granted', sa.Integer(), server_default='0', nullable=False),
            sa.Column('downloads_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('likes_count', sa.Integer(), server_default='0', nullable=False),
            sa.Column('pack_data', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index('ix_study_packs_status_cat', 'study_packs', ['status', 'category'])
        op.create_index('ix_study_packs_creator', 'study_packs', ['creator_id'])
        op.create_index('ix_study_packs_created_at', 'study_packs', ['created_at'])

    # 2. Create study_pack_downloads table if not exists
    if 'study_pack_downloads' not in existing_tables:
        op.create_table(
            'study_pack_downloads',
            sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('pack_id', UUID(as_uuid=True), sa.ForeignKey('study_packs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
            sa.Column('coins_spent', sa.Integer(), server_default='0', nullable=False),
            sa.Column('imported_material_id', UUID(as_uuid=True), sa.ForeignKey('study_materials.id', ondelete='SET NULL'), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('pack_id', 'user_id', name='uq_study_pack_user_download'),
        )
        op.create_index('ix_study_pack_downloads_user', 'study_pack_downloads', ['user_id'])
        op.create_index('ix_study_pack_downloads_pack', 'study_pack_downloads', ['pack_id'])

    # 3. Create study_pack_likes table if not exists
    if 'study_pack_likes' not in existing_tables:
        op.create_table(
            'study_pack_likes',
            sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
            sa.Column('pack_id', UUID(as_uuid=True), sa.ForeignKey('study_packs.id', ondelete='CASCADE'), nullable=False),
            sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint('pack_id', 'user_id', name='uq_study_pack_user_like'),
        )
        op.create_index('ix_study_pack_likes_user', 'study_pack_likes', ['user_id'])
        op.create_index('ix_study_pack_likes_pack', 'study_pack_likes', ['pack_id'])


def downgrade() -> None:
    op.drop_table('study_pack_likes')
    op.drop_table('study_pack_downloads')
    op.drop_table('study_packs')
