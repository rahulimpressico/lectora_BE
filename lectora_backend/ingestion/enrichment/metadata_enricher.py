from __future__ import annotations
import asyncio
import logging

from lectora_backend.ingestion.chunking.models import ChunkMetadata, CourseChunk
from lectora_backend.ingestion.enrichment.prompts import ENRICHMENT_SYSTEM, ENRICHMENT_USER

logger = logging.getLogger(__name__)

_MAX_CONCURRENT = 5
_CONTENT_PREVIEW_CHARS = 3000


class MetadataEnricher:
    """Enrich CourseChunk metadata using an Azure OpenAI LLM."""

    def __init__(self, client, deployment: str) -> None:
        self._client = client
        self._deployment = deployment

    async def enrich(self, chunk: CourseChunk) -> CourseChunk:
        """Call LLM to enrich a single chunk's metadata."""
        user_msg = ENRICHMENT_USER.format(
            title=chunk.title,
            parent_title=chunk.parent_title or "",
            content=chunk.raw_text[:_CONTENT_PREVIEW_CHARS],
        )
        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": ENRICHMENT_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
                max_tokens=1024,
                temperature=0.2,
            )
            raw_json = response.choices[0].message.content or "{}"
            data = _parse_json_safe(raw_json)
            chunk = chunk.model_copy(
                update={
                    "metadata": ChunkMetadata(
                        learning_concepts=_ensure_list(data.get("learning_concepts")),
                        skills=_ensure_list(data.get("skills")),
                        keywords=_ensure_list(data.get("keywords")),
                        entities=_ensure_list(data.get("entities")),
                        summary=str(data.get("summary") or ""),
                        prerequisites=_ensure_list(data.get("prerequisites")),
                        difficulty=str(data.get("difficulty") or "introductory"),
                        learning_outcomes=_ensure_list(data.get("learning_outcomes")),
                    )
                }
            )
        except Exception as exc:
            logger.warning(
                "[metadata_enricher] Enrichment failed for chunk %s: %s", chunk.chunk_id, exc
            )
        return chunk

    async def enrich_batch(self, chunks: list[CourseChunk]) -> list[CourseChunk]:
        """Enrich multiple chunks concurrently with a semaphore cap."""
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)

        async def _guarded(chunk: CourseChunk) -> CourseChunk:
            async with semaphore:
                return await self.enrich(chunk)

        results = await asyncio.gather(*[_guarded(c) for c in chunks])
        return list(results)


def _parse_json_safe(text: str) -> dict:
    try:
        import json
        return json.loads(text)
    except Exception:
        pass
    try:
        from json_repair import repair_json
        import json
        repaired = repair_json(text)
        return json.loads(repaired)
    except Exception as exc:
        logger.warning("[metadata_enricher] JSON repair failed: %s", exc)
        return {}


def _ensure_list(val) -> list:
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return []
