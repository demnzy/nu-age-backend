"""revoke_tobs_refresh_tokens

Security remediation: revoke all active refresh tokens for the @tobs account
so that any sessions held by an unauthorized party are immediately invalidated.
The unauthorized party will be forced to re-authenticate, which they cannot do
without @tobs's password.

Uses UPDATE (set revoked_at) rather than DELETE so that:
 - The reuse-detection logic still sees them as revoked (not missing).
 - There's an audit trail of when the revocation happened.

Revision ID: b0e800a8ef10
Revises: fbe425c52d15
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone


# revision identifiers, used by Alembic.
revision = 'b0e800a8ef10'
down_revision = 'fbe425c52d15'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revoke every active (non-revoked) refresh token belonging to @tobs.
    # This is a data-only migration — no schema changes.
    now = datetime.now(timezone.utc).isoformat()
    op.execute(
        sa.text(
            """
            UPDATE refresh_tokens
            SET    revoked_at = :now
            WHERE  revoked_at IS NULL
              AND  user_id = (
                     SELECT id FROM "user"
                     WHERE  username = 'tobs'
                     LIMIT 1
                   )
            """
        ).bindparams(now=now)
    )


def downgrade() -> None:
    # Irreversible security action — downgrade is a no-op.
    # Re-activating revoked tokens would be a security risk.
    pass
