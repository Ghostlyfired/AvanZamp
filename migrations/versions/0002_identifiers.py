"""Durable monotonic identifiers; PO line contracts are additive JSON fields."""
from alembic import op
import sqlalchemy as sa

revision = '0002_identifiers'
down_revision = '0001_pilot'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('identifier_sequences', sa.Column('key', sa.String(40), primary_key=True),
                    sa.Column('value', sa.Integer, nullable=False))


def downgrade():
    raise RuntimeError('Identifier high-water marks must not be discarded.')
