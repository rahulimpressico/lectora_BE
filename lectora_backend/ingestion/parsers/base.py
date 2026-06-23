from __future__ import annotations
from abc import ABC, abstractmethod
from lectora_backend.ingestion.chunking.models import DocumentNode


class BaseDocumentParser(ABC):
    @abstractmethod
    def parse(self, path: str) -> list[DocumentNode]:
        ...
