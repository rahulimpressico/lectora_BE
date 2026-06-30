from __future__ import annotations


async def get_local_course_content(job_id: str, *, course_slug: str | None = None):
    """
    Dev-only fallback for locally created jobs that are not present in SQL.

    Kept in a service module so `jobs.py` does not import route modules directly.
    """
    from lectora_backend.api.services.route_impl.local_jobs_routes_impl import get_course_content as _local_get_course

    return await _local_get_course(job_id, course_slug=course_slug)

