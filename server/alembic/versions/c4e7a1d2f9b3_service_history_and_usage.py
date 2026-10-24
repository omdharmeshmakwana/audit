"""Service history and usage captures (Higgsfield exporter integration, DECISIONS #59).

Additive only: two new derived tables, filled by the projectors from the new
`service_generation.observed` and `usage.page_captured` events. No existing table or row
is touched, so an existing log keeps verifying and replays unchanged.

Revision ID: c4e7a1d2f9b3
Revises: b91c4d2e7f30
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c4e7a1d2f9b3"
down_revision = "b91c4d2e7f30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_generations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("service_job_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("service_status", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("credits_cost", sa.Numeric(), nullable=True),
        sa.Column("credits_currency", sa.Text(), nullable=False),
        sa.Column("fail_reason", sa.Text(), nullable=True),
        sa.Column("service_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("service_user_id", sa.Text(), nullable=True),
        sa.Column("service_workspace", sa.Text(), nullable=True),
        sa.Column("login_email", sa.Text(), nullable=True),
        sa.Column("login_label", sa.Text(), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("first_observed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("service", "service_job_id"),
    )
    op.create_index(
        "ix_service_generations_created", "service_generations", ["service_created_at"]
    )
    op.create_table(
        "usage_captures",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captured_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("page_url", sa.Text(), nullable=False),
        sa.Column("page_title", sa.Text(), nullable=True),
        sa.Column("login_email", sa.Text(), nullable=True),
        sa.Column("login_label", sa.Text(), nullable=True),
        sa.Column("service_user_id", sa.Text(), nullable=True),
        sa.Column("response_count", sa.Integer(), nullable=False),
        sa.Column("tables", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("text_chars", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # Derived tables only: `faible rebuild` refills them from the log after an upgrade.
    op.drop_table("usage_captures")
    op.drop_index("ix_service_generations_created", table_name="service_generations")
    op.drop_table("service_generations")
