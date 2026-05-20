"""add job_logs table for real-time pipeline log streaming

Revision ID: b2c3d4e5f6a7
Revises: f66880bc965d
Create Date: 2026-05-19 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'f66880bc965d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=False),
        sa.Column('stage_id', sa.String(length=16), nullable=True),
        sa.Column('level', sa.String(length=16), nullable=False, server_default='info'),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.job_id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_job_logs_job_id'), 'job_logs', ['job_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_job_logs_job_id'), table_name='job_logs')
    op.drop_table('job_logs')
