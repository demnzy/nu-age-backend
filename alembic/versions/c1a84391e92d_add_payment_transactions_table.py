"""add_payment_transactions_table

Revision ID: c1a84391e92d
Revises: b0e800a8ef10
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = 'c1a84391e92d'
down_revision = 'b0e800a8ef10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create payment_transactions table
    op.create_table(
        'payment_transactions',
        sa.Column('id', UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('reference', sa.String(100), nullable=False),
        sa.Column('gateway', sa.String(50), server_default='paystack', nullable=False),
        sa.Column('user_id', UUID(as_uuid=True), sa.ForeignKey('user.id', ondelete='SET NULL'), nullable=True),
        sa.Column('organisation_id', UUID(as_uuid=True), sa.ForeignKey('Organisations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(10), server_default='NGN', nullable=False),
        sa.Column('status', sa.String(30), server_default='pending', nullable=False),
        sa.Column('purpose', sa.String(50), nullable=False),
        sa.Column('metadata_payload', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('gateway_response', JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # 2. Indexes
    op.create_index('ix_payment_transactions_reference', 'payment_transactions', ['reference'], unique=True)
    op.create_index('ix_payment_transactions_user_id', 'payment_transactions', ['user_id'])
    op.create_index('ix_payment_transactions_organisation_id', 'payment_transactions', ['organisation_id'])
    op.create_index('ix_payment_transactions_status', 'payment_transactions', ['status'])
    op.create_index('ix_payment_transactions_purpose', 'payment_transactions', ['purpose'])
    op.create_index('ix_payment_transactions_created_at', 'payment_transactions', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_payment_transactions_created_at', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_purpose', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_status', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_organisation_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_user_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_reference', table_name='payment_transactions')
    op.drop_table('payment_transactions')
