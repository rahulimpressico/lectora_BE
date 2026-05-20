"""SQL index + Blob content fetch for rule packs."""
import logging
from lectora_backend.repositories.blob_repository import BlobRepository

logger = logging.getLogger(__name__)


class RulePackRepository:
    def __init__(self) -> None:
        self._blob = BlobRepository()

    async def get_rule_pack(self, rule_pack_id: str) -> dict:
        # TODO: fetch metadata from SQL, content from blob
        logger.info("Fetching rule pack: %s", rule_pack_id)
        return {}
