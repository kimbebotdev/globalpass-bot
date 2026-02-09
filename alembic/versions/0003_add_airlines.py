"""add airlines table

Revision ID: 0003_add_airlines
Revises: 0002_add_standby_bots_payload
Create Date: 2026-02-07 00:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_add_airlines"
down_revision = "0002_add_standby_bots_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "airlines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_airlines_code", "airlines", ["code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_airlines_code", table_name="airlines")
    op.drop_table("airlines")
