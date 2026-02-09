"""add standby_bots_payload to standby_bot_responses

Revision ID: 0002_add_standby_bots_payload
Revises: 0001_initial
Create Date: 2026-02-01 00:00:00.000000
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_add_standby_bots_payload"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("standby_bot_responses", sa.Column("standby_bots_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("standby_bot_responses", "standby_bots_payload")
