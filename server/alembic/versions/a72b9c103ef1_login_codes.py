"""Private, expiring email login challenges (excluded from event replay)."""
from alembic import op
import sqlalchemy as sa

revision = "a72b9c103ef1"
down_revision = "e38cbd5c4a71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_codes",
        sa.Column("email", sa.Text(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("login_codes")
