class JobNotFoundError(Exception):
    """Raised when a queued message references a job that no longer exists."""
