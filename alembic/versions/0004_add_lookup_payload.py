"""add lookup_payload to lookup_bot_responses

Revision ID: 0004_add_lookup_payload
Revises: 0003_add_airlines
Create Date: 2026-02-08 18:51:00
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_add_lookup_payload"
down_revision = "0003_add_airlines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("lookup_bot_responses") as batch_op:
        batch_op.add_column(sa.Column("lookup_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("lookup_bot_responses") as batch_op:
        batch_op.drop_column("lookup_payload")
