"""Accounts can be disabled: login and all existing tokens are refused."""
from alembic import op
import sqlalchemy as sa

revision = "b91c4d2e7f30"
down_revision = "a72b9c103ef1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_credentials", sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("user_credentials", "disabled_at")
