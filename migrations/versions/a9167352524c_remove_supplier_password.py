
"""Remove supplier password

Revision ID: a9167352524c
Revises: c4c18c27be96
"""

from alembic import op
import sqlalchemy as sa


revision = 'a9167352524c'
down_revision = 'c4c18c27be96'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('supplier', schema=None) as batch_op:
        batch_op.drop_column('password')


def downgrade():
    with op.batch_alter_table('supplier', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'password',
                sa.String(length=255),
                nullable=True
            )
        )
