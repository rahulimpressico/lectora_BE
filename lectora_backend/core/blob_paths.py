"""Shared Azure Blob path helpers for uploaded source documents."""

# Dedicated Azure container for user uploads (Documents library).
UPLOADED_DOCUMENTS_CONTAINER = "uploaded-documents"

# Virtual prefix clients may still send (stripped before blob ops in that container).
UPLOADED_DOCUMENTS_PREFIX = "uploaded-documents"
