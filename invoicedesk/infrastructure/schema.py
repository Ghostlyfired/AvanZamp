"""Pilot schema: PostgreSQL NUMERIC money and immutable JSON artifacts."""
import sqlalchemy as sa


class ExactMoney(sa.TypeDecorator):
    """NUMERIC in PostgreSQL; decimal text in the explicit SQLite test adapter."""
    impl = sa.Numeric(24, 6)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(sa.Text() if dialect.name == 'sqlite' else sa.Numeric(24, 6))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from decimal import Decimal
        if isinstance(value, float):
            raise ValueError('Money must not use floating-point values.')
        amount = Decimal(value)
        if not amount.is_finite():
            raise ValueError('Money must be finite.')
        return str(amount) if dialect.name == 'sqlite' else amount

    def process_result_value(self, value, dialect):
        from decimal import Decimal
        return Decimal(value) if value is not None else None

metadata = sa.MetaData()
sequences = sa.Table('identifier_sequences', metadata,
    sa.Column('key', sa.String(40), primary_key=True),
    sa.Column('value', sa.Integer, nullable=False))
company = sa.Table('company', metadata,
    sa.Column('id', sa.Integer, primary_key=True), sa.Column('revision', sa.Integer, nullable=False),
    sa.Column('policy', sa.JSON, nullable=False))
vendors = sa.Table('pilot_vendors', metadata,
    sa.Column('vendor_id', sa.String(80), primary_key=True), sa.Column('data', sa.JSON, nullable=False))
pos = sa.Table('pilot_pos', metadata,
    sa.Column('po_number', sa.String(80), primary_key=True), sa.Column('data', sa.JSON, nullable=False),
    sa.Column('net_amount', ExactMoney(), nullable=False))
documents = sa.Table('documents', metadata,
    sa.Column('id', sa.String(64), primary_key=True), sa.Column('sha256', sa.String(64), nullable=False),
    sa.Column('owner', sa.String(255), nullable=False), sa.Column('filename', sa.String(255), nullable=False),
    sa.Column('media_type', sa.String(80), nullable=False), sa.Column('created_at', sa.Float, nullable=False))
jobs = sa.Table('jobs', metadata,
    sa.Column('id', sa.String(64), primary_key=True),
    sa.Column('document_id', sa.String(64), sa.ForeignKey('documents.id'), nullable=False),
    sa.Column('owner', sa.String(255), nullable=False), sa.Column('state', sa.String(40), nullable=False),
    sa.Column('created_at', sa.Float, nullable=False), sa.Column('started_at', sa.Float),
    sa.Column('lease_token', sa.String(64)), sa.Column('lease_until', sa.Float),
    sa.Column('heartbeat_at', sa.Float), sa.Column('attempt', sa.Integer, nullable=False, default=0),
    sa.Column('revision', sa.Integer, nullable=False, default=0), sa.Column('error', sa.String(80)),
    sa.Column('review_state', sa.String(40), nullable=False, default='pending'),
    sa.Column('provider_started', sa.Boolean, nullable=False, default=False))
sa.Index('ix_jobs_claim', jobs.c.state, jobs.c.created_at)
artifacts = sa.Table('artifacts', metadata,
    sa.Column('job_id', sa.String(64), sa.ForeignKey('jobs.id'), primary_key=True),
    sa.Column('revision', sa.Integer, primary_key=True), sa.Column('kind', sa.String(40), primary_key=True),
    sa.Column('data', sa.JSON, nullable=False), sa.Column('sha256', sa.String(64), nullable=False),
    sa.Column('created_at', sa.Float, nullable=False))
events = sa.Table('stage_events', metadata,
    sa.Column('job_id', sa.String(64), sa.ForeignKey('jobs.id'), primary_key=True),
    sa.Column('seq', sa.Integer, primary_key=True), sa.Column('data', sa.JSON, nullable=False))
ledger = sa.Table('approval_ledger', metadata,
    sa.Column('job_id', sa.String(64), sa.ForeignKey('jobs.id'), primary_key=True),
    sa.Column('revision', sa.Integer, nullable=False), sa.Column('po_number', sa.String(80), nullable=False),
    sa.Column('vendor_id', sa.String(80), nullable=False), sa.Column('invoice_key', sa.String(255), nullable=False),
    sa.Column('amount', ExactMoney(), nullable=False), sa.Column('currency', sa.String(3), nullable=False),
    sa.Column('actor', sa.String(255), nullable=False), sa.Column('reason', sa.Text, nullable=False),
    sa.Column('context', sa.JSON, nullable=False), sa.Column('created_at', sa.Float, nullable=False),
    sa.UniqueConstraint('vendor_id', 'invoice_key', name='uq_approved_invoice'),
    sa.CheckConstraint('amount > 0', name='positive_approval'))
audit = sa.Table('audit_events', metadata,
    sa.Column('id', sa.String(64), primary_key=True), sa.Column('actor', sa.String(255), nullable=False),
    sa.Column('action', sa.String(80), nullable=False), sa.Column('target', sa.String(80), nullable=False),
    sa.Column('data', sa.JSON, nullable=False), sa.Column('created_at', sa.Float, nullable=False))
commands = sa.Table('commands', metadata,
    sa.Column('actor', sa.String(255), primary_key=True), sa.Column('key', sa.String(128), primary_key=True),
    sa.Column('fingerprint', sa.String(64), nullable=False), sa.Column('result', sa.JSON, nullable=False))
sessions = sa.Table('sessions', metadata,
    sa.Column('token_hash', sa.String(64), primary_key=True), sa.Column('data', sa.JSON, nullable=False),
    sa.Column('expires_at', sa.Float, nullable=False))
