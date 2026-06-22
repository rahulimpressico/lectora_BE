"""Add CANCELLED to jobstatus enum and ON DELETE CASCADE to job_logs FK

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-07 00:00:00.000000

Changes:
  1. Adds 'CANCELLED' value to the jobstatus PostgreSQL enum (SQLite has no
     native enum type so the ORM model change is sufficient there).
  2. Adds ON DELETE CASCADE to job_logs.job_id foreign key so that deleting
     a job automatically removes all its log entries, avoiding N+1 DELETEs.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == 'postgresql':
        # 1. Add CANCELLED to the enum (IF NOT EXISTS prevents idempotency errors)
        op.execute("ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")

        # 2. Re-create the job_logs FK with ON DELETE CASCADE
        op.drop_constraint('job_logs_job_id_fkey', 'job_logs', type_='foreignkey')
        op.create_foreign_key(
            None, 'job_logs', 'jobs',
            ['job_id'], ['job_id'],
            ondelete='CASCADE',
        )
    # SQLite: no native enum enforcement; the ORM model already includes CANCELLED.
    # SQLite FK ON DELETE CASCADE requires PRAGMA foreign_keys=ON at connection time;
    # adding it here via DDL is not supported — the ORM cascade handles it instead.


def downgrade() -> None:
    # PostgreSQL enums cannot remove values in older versions; no-op downgrade.
    # For the FK, reversing ON DELETE CASCADE is rarely needed in practice.
    pass
