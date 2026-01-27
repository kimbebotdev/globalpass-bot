"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-26 00:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_dir", sa.String(), nullable=True),
        sa.Column("slack_channel", sa.String(), nullable=True),
        sa.Column("slack_thread_ts", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_runs_id", "runs", ["id"], unique=True)

    op.create_table(
        "bot_responses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("bot_name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("output_path", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bot_responses_run_id", "bot_responses", ["run_id"], unique=False)

    op.create_table(
        "myidtravel_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_name", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("airport", sa.String(), nullable=True),
        sa.Column("position", sa.String(), nullable=True),
        sa.Column("travellers", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_myidtravel_accounts_username", "myidtravel_accounts", ["username"], unique=False)

    op.create_table(
        "stafftraveler_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_name", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("password", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stafftraveler_accounts_username", "stafftraveler_accounts", ["username"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stafftraveler_accounts_username", table_name="stafftraveler_accounts")
    op.drop_table("stafftraveler_accounts")
    op.drop_index("ix_myidtravel_accounts_username", table_name="myidtravel_accounts")
    op.drop_table("myidtravel_accounts")
    op.drop_index("ix_bot_responses_run_id", table_name="bot_responses")
    op.drop_table("bot_responses")
    op.drop_index("ix_runs_id", table_name="runs")
    op.drop_table("runs")
