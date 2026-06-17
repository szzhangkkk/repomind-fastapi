"""Cross-Encoder reranker module for the v8 RAG pipeline.

Uses BAAI/bge-reranker-v2-m3 to rescore candidate chunks with a Cross-Encoder,
which jointly encodes (query, document) pairs for more accurate relevance scoring
than bi-encoder cosine similarity alone.

Usage:
    from pe.v8.reranker import rerank

    chunks = rerank("get_dependant call chain", candidate_chunks, top_k=5)
"""

import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PE_DIR = Path(__file__).resolve().parent.parent
MODEL_CACHE_DIR = PE_DIR / "model_cache"
MODEL_NAME = "BAAI/bge-reranker-v2-m3"
LOCAL_MODEL_DIR = MODEL_CACHE_DIR / "bge-reranker-v2-m3"

# ---------------------------------------------------------------------------
# Cached singleton
# ---------------------------------------------------------------------------
_reranker_model = None


def _get_reranker_model():
    """Load the Cross-Encoder reranker model (cached singleton).

    Loads from pe/model_cache/bge-reranker-v2-m3/ if present,
    otherwise downloads from HuggingFace Hub and caches locally.
    """
    global _reranker_model
    if _reranker_model is not None:
        return _reranker_model

    from sentence_transformers import CrossEncoder

    if LOCAL_MODEL_DIR.exists() and (LOCAL_MODEL_DIR / "config.json").exists():
        print(f"[Reranker] Loading from local cache: {LOCAL_MODEL_DIR}")
        model_path = str(LOCAL_MODEL_DIR)
    else:
        print(f"[Reranker] Model not found locally, downloading {MODEL_NAME} ...")
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Download to cache directory
        _reranker_model = CrossEncoder(
            MODEL_NAME,
            cache_folder=str(MODEL_CACHE_DIR),
        )
        # Note: sentence_transformers may store under its own cache layout;
        # the model is functional regardless of the exact local path.
        print(f"[Reranker] Model loaded: {MODEL_NAME}")
        return _reranker_model

    _reranker_model = CrossEncoder(model_path)
    print(f"[Reranker] Model loaded from: {model_path}")
    return _reranker_model


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rerank(query: str, chunks: list[dict], top_k: int) -> list[dict]:
    """Rerank candidate chunks with a Cross-Encoder for more accurate ordering.

    Args:
        query: The user query string.
        chunks: Candidate chunk dicts, each must have a 'source' field.
        top_k: Number of top results to return after reranking.

    Returns:
        List of chunk dicts (same structure as input), sorted by reranker
        score descending, truncated to top_k. Each chunk gains a
        'rerank_score' field (float).
    """
    if not chunks:
        return []

    model = _get_reranker_model()

    # Build (query, source) pairs for the Cross-Encoder
    pairs = [(query, chunk.get("source", "") or "") for chunk in chunks]

    # Batch inference — returns list of float scores
    scores = model.predict(pairs)

    # Attach scores to chunks
    for chunk, score in zip(chunks, scores):
        chunk["rerank_score"] = float(score)

    # Sort by score descending, take top_k
    ranked = sorted(chunks, key=lambda c: c.get("rerank_score", 0.0), reverse=True)
    return ranked[:top_k]
