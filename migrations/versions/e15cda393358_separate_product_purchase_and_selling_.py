"""Separate product purchase and selling prices

Revision ID: e15cda393358
Revises: d2d698096c6a
Create Date: 2026-10-09 17:36:19.672912

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e15cda393358'
down_revision = 'd2d698096c6a'
branch_labels = None
depends_on = None


def upgrade():
    # Add the new price columns without breaking existing products
    with op.batch_alter_table('product', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('purchase_price', sa.Float(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('selling_price', sa.Float(), nullable=True)
        )

    # Preserve the old price as the selling price
    op.execute(
        "UPDATE product SET selling_price = price"
    )

    # Remove the old price column after copying its values
    with op.batch_alter_table('product', schema=None) as batch_op:
        batch_op.drop_column('price')

def downgrade():
    # Restore the old price column
    with op.batch_alter_table('product', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('price', sa.Float(), nullable=True)
        )

    # Restore selling prices to the old price column
    op.execute(
        "UPDATE product SET price = selling_price"
    )

    # Remove the new price columns
    with op.batch_alter_table('product', schema=None) as batch_op:
        batch_op.drop_column('selling_price')
        batch_op.drop_column('purchase_price')
