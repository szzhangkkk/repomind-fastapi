"""Extended RAG pipeline with optional Cross-Encoder reranker and query rewrite.

Wraps pe.rag hybrid_search to add v8 features without modifying the original
module. The base retrieval (BM25 + vector + RRF + MMR) runs unchanged; this
module adds pre-retrieval query expansion and post-retrieval reranking.

Usage:
    from pe.v8.rag_extended import hybrid_search, load_milvus, format_context

    collection = load_milvus(db_uri="pe/milvus_lite_v7min.db")
    chunks = hybrid_search(
        collection, "get_dependant()",
        use_reranker=True,
        use_query_rewrite=True,
    )
"""

from typing import Any

# Re-export base functions unchanged
from pe.rag import format_context, load_milvus  # noqa: F401

# Internal import — aliased to avoid shadowing the public name
from pe.rag import hybrid_search as _base_hybrid_search

# v8 components
from pe.v8.query_rewrite import rewrite_query
from pe.v8.reranker import rerank

# Default candidate multiplier when reranker is enabled.
# Fetch top_k * MULTIPLIER candidates from the base retriever so the
# Cross-Encoder has enough material to select from.
RERANK_CANDIDATE_MULT = 3


def hybrid_search(
    collection,
    query: str,
    top_k: int = 10,
    category: str | None = None,
    adaptive_top_k: bool = False,
    use_mmr: bool = False,
    use_bm25: bool = False,
    use_reranker: bool = False,
    use_query_rewrite: bool = False,
    file_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Extended hybrid search with optional query rewrite and Cross-Encoder rerank.

    Pipeline:
        1. (optional) Rewrite query for better embedding retrieval.
        2. Run base hybrid_search (BM25 + vector + RRF + optional MMR).
           When use_reranker=True, top_k is multiplied so the reranker
           can select from a larger candidate pool.
        3. (optional) Rerank candidates with Cross-Encoder and truncate to top_k.

    Args:
        collection: Loaded Milvus Collection.
        query: Natural language query string.
        top_k: Final number of results to return.
        category: Question category (function_locate, call_chain, etc.).
        adaptive_top_k: If True, override top_k per ADAPTIVE_TOP_K[category].
        use_mmr: Apply MMR diversity reranking in the base pipeline.
        use_bm25: Enable BM25 + vector hybrid fusion in the base pipeline.
        use_reranker: If True, apply Cross-Encoder reranking after base retrieval.
        use_query_rewrite: If True, rewrite query before embedding.
        file_hint: Optional file filter for vector search.

    Returns:
        List of chunk dicts with at least: id, file, line, name, source,
        context, score. When use_reranker=True, each chunk also has
        'rerank_score'.
    """
    # ── Step 1: Optional query rewrite ──
    effective_query = query
    if use_query_rewrite:
        rewritten = rewrite_query(query, category)
        if rewritten != query:
            effective_query = rewritten

    # ── Step 2: Base hybrid search ──
    # When reranker is enabled, fetch more candidates so the Cross-Encoder
    # has a larger pool to select from.
    base_top_k = top_k * RERANK_CANDIDATE_MULT if use_reranker else top_k

    chunks = _base_hybrid_search(
        collection,
        effective_query,
        file_hint=file_hint,
        top_k=base_top_k,
        category=category,
        adaptive_top_k=adaptive_top_k,
        use_mmr=use_mmr,
        use_bm25=use_bm25,
    )

    # ── Step 3: Optional rerank ──
    if use_reranker and chunks:
        # Rerank with the ORIGINAL query (not the rewritten one), since
        # Cross-Encoders benefit from the user's precise intent.
        chunks = rerank(effective_query, chunks, top_k)

    return chunks
