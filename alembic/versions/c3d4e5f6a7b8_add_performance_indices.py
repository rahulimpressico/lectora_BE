"""add performance indices for SSE polling and job status queries

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-27 00:00:00.000000

Why these indices:

  ix_job_logs_job_id_cursor  — composite (job_id, id) on job_logs
      The SSE endpoint calls get_logs_since() every 2 seconds:
        WHERE job_id = ? AND id > ? ORDER BY id
      A single-column index on job_id forces a range scan on id afterward.
      The composite covering index satisfies both predicates and the ORDER BY
      in a single B-tree traversal.

  ix_jobs_status  — on jobs.status
      Enables O(log n) lookups when querying for all PROCESSING / PENDING jobs
      (health checks, admin dashboards, cleanup jobs).

  ix_jobs_created_at  — on jobs.created_at
      Speeds up list/sort queries ordered by creation time.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Composite covering index for cursor-based SSE log queries.
    # Replaces the single-column ix_job_logs_job_id for this access pattern.
    op.create_index(
        "ix_job_logs_job_id_cursor",
        "job_logs",
        ["job_id", "id"],
        unique=False,
    )

    # Allows efficient filtering of jobs by status.
    op.create_index(
        "ix_jobs_status",
        "jobs",
        ["status"],
        unique=False,
    )

    # Allows efficient sorting of jobs by creation time.
    op.create_index(
        "ix_jobs_created_at",
        "jobs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_job_logs_job_id_cursor", table_name="job_logs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
