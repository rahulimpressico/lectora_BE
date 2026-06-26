from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path

from lectora_backend.ingestion.chunking.models import ChunkMetadata, CourseChunk
from lectora_backend.ingestion.enrichment.prompts import ENRICHMENT_SYSTEM, ENRICHMENT_USER
from lectora_backend.pipeline.shared_llm_config.tracer import LLMTrace, write_trace

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
        t_start = time.perf_counter()
        raw_json = "{}"
        prompt_tokens = completion_tokens = total_tokens = 0
        error_msg: str | None = None
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
            if getattr(response, "usage", None):
                prompt_tokens = response.usage.prompt_tokens or 0
                completion_tokens = response.usage.completion_tokens or 0
                total_tokens = response.usage.total_tokens or 0
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
            error_msg = str(exc)
            logger.warning(
                "[metadata_enricher] Enrichment failed for chunk %s: %s", chunk.chunk_id, exc
            )
        finally:
            write_trace(LLMTrace(
                agent="INGEST_METADATA",
                deployment=self._deployment,
                system_prompt=ENRICHMENT_SYSTEM,
                user_msg=user_msg,
                response=raw_json,
                latency_ms=(time.perf_counter() - t_start) * 1000,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                error=error_msg,
                run_id=f"ingest:{chunk.document_id}",
                doc_name=Path(chunk.source_file or chunk.document_id).stem,
                source_refs=[chunk.source_file or chunk.document_id],
                model_parameters={
                    "temperature": 0.2,
                    "max_tokens": 1024,
                    "response_format": {"type": "json_object"},
                },
                prompt_metadata={
                    "step": "ingestion.metadata_enrichment",
                    "chunk_id": chunk.chunk_id,
                    "document_id": chunk.document_id,
                    "section_id": chunk.section_id,
                    "chunk_title": chunk.title,
                    "prompt_file": "lectora_backend/ingestion/enrichment/metadata_enricher.py",
                    "prompt_function": "MetadataEnricher.enrich",
                },
                observation_name="INGEST_METADATA | prompt -> output",
            ))
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
