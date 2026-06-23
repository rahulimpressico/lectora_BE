from __future__ import annotations

_VECTOR_DIMS = 3072

# Semantic ranker configuration.
# Semantic ranking applies a cross-encoder ML model on top of the BM25 + vector
# results, surfacing results that are semantically equivalent even when they share
# no overlapping keywords.  Requires Azure AI Search Standard tier (S1+).
# The configuration below uses the three semantic field priorities supported by
# the API: title (heading), content (body text + summary), keywords (term list).
_SEMANTIC_CONFIG = {
    "defaultConfiguration": "default",
    "configurations": [
        {
            "name": "default",
            "prioritizedFields": {
                "titleField":               {"fieldName": "title"},
                "prioritizedContentFields": [
                    {"fieldName": "raw_text"},
                    {"fieldName": "summary"},
                ],
                "prioritizedKeywordsFields": [
                    {"fieldName": "keywords"},
                ],
            },
        }
    ],
}

_VECTOR_SEARCH_CONFIG = {
    "algorithms": [
        {
            "name": "hnsw-config",
            "kind": "hnsw",
            "hnswParameters": {
                "metric": "cosine",
                "m": 4,
                "efConstruction": 400,
                "efSearch": 500,
            },
        }
    ],
    "profiles": [
        {
            "name": "vector-profile",
            "algorithm": "hnsw-config",
        }
    ],
}


def get_index_definition(index_name: str) -> dict:
    """Return a full Azure AI Search index definition compatible with the REST API."""
    return {
        "name": index_name,
        "fields": [
            # ── Identity ──────────────────────────────────────────────────────
            {
                "name": "chunk_id",
                "type": "Edm.String",
                "key": True,
                "filterable": True,
                "retrievable": True,
                "searchable": False,
            },
            {
                "name": "document_id",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
                "searchable": False,
            },
            {
                "name": "section_id",
                "type": "Edm.String",
                "filterable": True,
                "retrievable": True,
                "searchable": False,
            },
            # ── Source provenance (metadata) ──────────────────────────────────
            {
                "name": "source_file",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
                "searchable": False,
            },
            {
                "name": "page_num",
                "type": "Edm.Int32",
                "filterable": True,
                "retrievable": True,
                "searchable": False,
                "sortable": True,
            },
            # ── Titles ────────────────────────────────────────────────────────
            {
                "name": "title",
                "type": "Edm.String",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
                "analyzer": "en.microsoft",
            },
            {
                "name": "parent_title",
                "type": "Edm.String",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
                "analyzer": "en.microsoft",
            },
            # ── Raw text (verbatim chunk content, always retrievable) ─────────
            {
                "name": "raw_text",
                "type": "Edm.String",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
                "analyzer": "en.microsoft",
            },
            # ── LLM-enriched fields ───────────────────────────────────────────
            {
                "name": "summary",
                "type": "Edm.String",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
                "analyzer": "en.microsoft",
            },
            {
                "name": "keywords",
                "type": "Collection(Edm.String)",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "skills",
                "type": "Collection(Edm.String)",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "learning_concepts",
                "type": "Collection(Edm.String)",
                "filterable": True,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "learning_outcomes",
                "type": "Collection(Edm.String)",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "prerequisites",
                "type": "Collection(Edm.String)",
                "filterable": False,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "entities",
                "type": "Collection(Edm.String)",
                "filterable": True,
                "retrievable": True,
                "searchable": True,
            },
            {
                "name": "difficulty",
                "type": "Edm.String",
                "filterable": True,
                "facetable": True,
                "retrievable": True,
                "searchable": False,
            },
            # ── Numeric metadata ──────────────────────────────────────────────
            {
                "name": "token_count",
                "type": "Edm.Int32",
                "filterable": True,
                "retrievable": True,
                "searchable": False,
                "sortable": True,
            },
            {
                "name": "estimated_read_min",
                "type": "Edm.Double",
                "filterable": False,
                "retrievable": True,
                "searchable": False,
                "sortable": True,
            },
            {
                "name": "upload_date",
                "type": "Edm.DateTimeOffset",
                "filterable": True,
                "retrievable": True,
                "searchable": False,
                "sortable": True,
            },
            # ── BM25 catch-all (not retrievable — search only) ────────────────
            {
                "name": "searchable_text",
                "type": "Edm.String",
                "filterable": False,
                "retrievable": False,
                "searchable": True,
                "analyzer": "en.microsoft",
            },
            # ── Embedding vectors (not retrievable — search only) ─────────────
            {
                "name": "embedding_title",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": _VECTOR_DIMS,
                "vectorSearchProfile": "vector-profile",
            },
            {
                "name": "embedding_summary",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": _VECTOR_DIMS,
                "vectorSearchProfile": "vector-profile",
            },
            {
                "name": "embedding_content",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": _VECTOR_DIMS,
                "vectorSearchProfile": "vector-profile",
            },
            {
                "name": "embedding_keywords",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "dimensions": _VECTOR_DIMS,
                "vectorSearchProfile": "vector-profile",
            },
        ],
        "vectorSearch": _VECTOR_SEARCH_CONFIG,
        "semantic":     _SEMANTIC_CONFIG,
    }
