"""Compatibility facade for `local_jobs.py` route module.

This module re-exports the implementation moved to API services to keep
public imports and the `router` object stable.
"""

from lectora_backend.api.services.route_impl.local_jobs_routes_impl import *  # noqa: F401,F403
