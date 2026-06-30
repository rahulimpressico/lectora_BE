"""Compatibility facade for `generate_to.py` route module.

This module re-exports the implementation moved to API services to keep
public imports and the `router` object stable.
"""

from lectora_backend.api.services.route_impl.generate_to_routes_impl import *  # noqa: F401,F403
