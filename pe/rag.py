"""RAG retrieval module for RepoMind.

Provides hybrid search (BM25 + vector + RRF + MMR) over the FastAPI code chunks
stored in the Milvus Lite collection.

Usage:
    from pe.rag import load_milvus, hybrid_search, format_context

    collection = load_milvus()
    chunks = hybrid_search(collection, "get_dependant() call chain", category="call_chain")
    context = format_context(chunks)
"""

import math
import os
import re
from pathlib import Path
from typing import Any

from pymilvus import Collection, CollectionSchema, connections, utility
from sentence_transformers import SentenceTransformer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PE_DIR.parent
MILVUS_DB = PE_DIR / "milvus_lite.db"
COLLECTION_NAME = "fastapi_chunks"
EMBED_DIM = 768
DEFAULT_TOP_K = 10
MAX_CONTEXT_CHARS = 4000

# Per-category adaptive top_k (override --rag-top-k when --adaptive-top-k is set)
ADAPTIVE_TOP_K = {
    "call_chain": 5,        # need to assemble a chain
    "cross_file_dep": 4,    # need files across modules
    "function_locate": 2,   # single hit usually suffices
    "impact_analysis": 6,   # wide coverage of affected files
}
MMR_LAMBDA = 0.6  # relevance weight in MMR; (1-lambda) = diversity penalty

# RRF (Reciprocal Rank Fusion) constant for combining BM25 + vector rankings
RRF_K = 60

# How many candidates each retrieval path returns before fusion
VECTOR_FETCH_K_MULT = 2  # vector path fetches top_k * this
BM25_FETCH_K_MULT = 2    # BM25 path fetches top_k * this

# ---------------------------------------------------------------------------
# Model cache
# ---------------------------------------------------------------------------
_embedding_model: SentenceTransformer | None = None
_bm25_index = None
_bm25_chunk_ids: list[str] = []
_bm25_id_to_chunk: dict[str, dict] = {}


def _get_embedding_model() -> SentenceTransformer:
    """Load the BGE embedding model (cached singleton, 768 dim)."""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(
            str(PE_DIR / "model_cache" / "bge-base-zh")
        )
    return _embedding_model


def _ensure_bm25_index():
    """Lazy-build a BM25 index over all chunks for keyword retrieval.

    Loads the Milvus collection once, pulls every chunk's text_for_embedding,
    tokenizes with jieba (Chinese-friendly) + identifier split (Python symbols),
    and builds a rank_bm25 BM25Okapi index. Cached in module globals.
    """
    global _bm25_index, _bm25_chunk_ids, _bm25_id_to_chunk
    if _bm25_index is not None:
        return

    import jieba
    from rank_bm25 import BM25Okapi

    print("[BM25] Building index from Milvus collection...")
    connections.connect(alias="bm25_default", uri=str(MILVUS_DB))
    col = Collection(COLLECTION_NAME)
    col.load()

    # Stream all chunks (Milvus returns iterator)
    iterator = col.query_iterator(
        expr="id != ''",
        output_fields=["id", "file", "line", "name", "source", "context", "text_for_embedding"],
        batch_size=500,
    )
    raw_chunks: list[dict] = []
    while True:
        batch = iterator.next()
        if not batch:
            break
        raw_chunks.extend(batch)
    iterator.close()

    print(f"[BM25] Loaded {len(raw_chunks)} chunks from Milvus")

    def tokenize(text: str) -> list[str]:
        if not text:
            return []
        # 1) jieba for CJK segmentation
        cjk_tokens = [t for t in jieba.cut(text) if t.strip()]
        # 2) split identifiers on snake_case / CamelCase to surface symbols
        id_tokens: list[str] = []
        for t in cjk_tokens:
            id_tokens.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", t))
        # also keep the raw token for exact match
        return cjk_tokens + id_tokens

    texts = [c.get("text_for_embedding", "") or "" for c in raw_chunks]
    tokenized_corpus = [tokenize(t) for t in texts]
    _bm25_index = BM25Okapi(tokenized_corpus)
    _bm25_chunk_ids = [c["id"] for c in raw_chunks]
    _bm25_id_to_chunk = {c["id"]: c for c in raw_chunks}
    print(f"[BM25] Index built ({len(tokenized_corpus)} docs)")


def _bm25_search(query: str, top_k: int) -> list[dict]:
    """Return top-k BM25 hits as chunk dicts (with 'score' field, higher=better)."""
    import jieba

    def tokenize(text: str) -> list[str]:
        if not text:
            return []
        cjk_tokens = [t for t in jieba.cut(text) if t.strip()]
        id_tokens: list[str] = []
        for t in cjk_tokens:
            id_tokens.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", t))
        return cjk_tokens + id_tokens

    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    scores = _bm25_index.get_scores(q_tokens)
    # Get top-k indices by score
    top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    results = []
    for i in top_idx:
        if scores[i] <= 0:
            continue
        cid = _bm25_chunk_ids[i]
        chunk = _bm25_id_to_chunk[cid]
        results.append({
            "id": chunk["id"],
            "file": chunk["file"],
            "line": chunk["line"],
            "name": chunk["name"],
            "source": chunk["source"],
            "context": chunk.get("context", ""),
            "score": float(scores[i]),
        })
    return results


# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------

def load_milvus(db_uri: str | None = None, collection_name: str | None = None) -> Collection:
    """Load the Milvus Lite collection, rehydrating from db file if needed.

    Args:
        db_uri: Path to Milvus Lite db file (default: pe/milvus_lite.db).
        collection_name: Milvus collection name (default: fastapi_chunks).

    Returns:
        Collection ready for search (loaded into memory).
    """
    db = db_uri or str(MILVUS_DB)
    cn = collection_name or COLLECTION_NAME
    connections.connect(alias="default", uri=db)
    if not utility.has_collection(cn):
        raise RuntimeError(
            f"Collection '{cn}' not found in {db}. "
            "Run pe/build_index.py first."
        )
    collection = Collection(cn)
    collection.load()
    return collection


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------

_KEYWORD_PATTERN = re.compile(r"(\w+\.py)[：:]\s*(\d+)")


def _keyword_match(query: str, chunks: list[dict]) -> dict | None:
    """If query contains 'file.py:NNN', return the matching chunk by exact id match.

    Returns None if no file:line reference found or no chunk matches.
    """
    m = _KEYWORD_PATTERN.search(query)
    if not m:
        return None
    file_part = m.group(1)
    line_part = m.group(2)
    target_id = f"{file_part}:{line_part}"
    for c in chunks:
        if c["id"] == target_id:
            return c
    return None


def mmr_rerank(
    query_emb: list[float],
    chunks: list[dict],
    lambda_rel: float = MMR_LAMBDA,
) -> list[dict]:
    """Maximal Marginal Relevance reranking for diversity.

    Iteratively picks chunks that maximize:
        lambda * sim(chunk, query) - (1 - lambda) * max(sim(chunk, selected))

    Args:
        query_emb: Query embedding (768-dim, L2-normalized).
        chunks: Candidate chunks, each with 'embedding' field (or we re-embed).
        lambda_rel: Weight for relevance vs diversity. 0.6 = mostly relevant.

    Returns:
        Reordered list of chunks (no chunks removed, just reordered).
    """
    import numpy as np
    if not chunks:
        return chunks
    # Each chunk needs a vector. We re-encode 'name + docstring + signature' to
    # keep cost low and avoid storing 768-dim floats in Milvus payload.
    model = _get_embedding_model()
    texts = []
    for c in chunks:
        # Use id-like text: file:name(line). Short and discriminative.
        texts.append(f"{c.get('file','')}:{c.get('line','')} — {c.get('name','')}")
    cand_embs = model.encode(
        [f"为这个句子生成表示以用于检索相关文章：{t}" for t in texts],
        normalize_embeddings=True,
    )
    q = np.asarray(query_emb)
    cand = np.asarray(cand_embs)  # (N, 768)
    # Cosine sim (already L2-normalized -> dot product)
    rel_scores = cand @ q  # (N,)
    selected_idx: list[int] = []
    remaining = set(range(len(chunks)))
    while remaining:
        if not selected_idx:
            # First pick: highest relevance
            best = max(remaining, key=lambda i: rel_scores[i])
        else:
            sel_embs = cand[selected_idx]  # (k, 768)
            # Max similarity to any already-selected
            sim_to_selected = (cand @ sel_embs.T).max(axis=1)  # (N,)
            mmr = lambda_rel * rel_scores - (1 - lambda_rel) * sim_to_selected
            # Only consider remaining
            best = max(remaining, key=lambda i: mmr[i])
        selected_idx.append(best)
        remaining.remove(best)
    return [chunks[i] for i in selected_idx]


def _rrf_fuse(
    vector_chunks: list[dict],
    bm25_chunks: list[dict],
    top_k: int,
    rrf_k: int = RRF_K,
) -> list[dict]:
    """Reciprocal Rank Fusion over two ranked lists.

    score = sum( 1 / (rrf_k + rank) ) for each list that contains the chunk.
    Chunks not in BM25 still get their vector score; chunks not in vector
    still get their BM25 score. Output is sorted by fused score desc.
    """
    fused: dict[str, float] = {}
    by_id: dict[str, dict] = {}
    for rank, c in enumerate(vector_chunks):
        cid = c["id"]
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        by_id[cid] = c
    for rank, c in enumerate(bm25_chunks):
        cid = c["id"]
        fused[cid] = fused.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
        by_id[cid] = c
    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [by_id[cid] for cid, _ in ranked]


def hybrid_search(
    collection: Collection,
    query: str,
    file_hint: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    category: str | None = None,
    adaptive_top_k: bool = False,
    use_mmr: bool = False,
    use_bm25: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search over Milvus chunks.

    Path A (use_bm25=False, default): vector ANN + optional post-hoc file:line
        keyword promotion. Same as v6.
    Path B (use_bm25=True): parallel BM25 + vector retrieval, fused via RRF,
        then optional MMR. This is the "real" hybrid.

    Args:
        collection: Loaded Milvus Collection.
        query: Natural language query.
        file_hint: Optional file path to restrict vector search to.
        top_k: Number of results to return.
        category: Question category. Used only if adaptive_top_k=True.
        adaptive_top_k: Override top_k with ADAPTIVE_TOP_K[category].
        use_mmr: Apply MMR reranking for diversity.
        use_bm25: Enable BM25 + RRF hybrid fusion.

    Returns:
        List of chunk dicts (id, file, line, name, source, context, score).
    """
    # Resolve effective top_k
    effective_top_k = top_k
    if adaptive_top_k and category and category in ADAPTIVE_TOP_K:
        effective_top_k = ADAPTIVE_TOP_K[category]

    # ---- Vector path ----
    model = _get_embedding_model()
    query_with_prefix = f"为这个句子生成表示以用于检索相关文章：{query}"
    query_emb = model.encode([query_with_prefix], normalize_embeddings=True)[0].tolist()
    search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
    expr = f'file == "{file_hint}"' if file_hint else None

    results = collection.search(
        data=[query_emb],
        anns_field="embedding",
        param=search_params,
        limit=effective_top_k * VECTOR_FETCH_K_MULT,
        expr=expr,
        output_fields=["id", "file", "line", "name", "source", "context"],
    )
    vector_chunks: list[dict] = []
    for hits in results:
        for hit in hits:
            entity = hit.entity
            vector_chunks.append({
                "id": entity.get("id"),
                "file": entity.get("file"),
                "line": entity.get("line"),
                "name": entity.get("name"),
                "source": entity.get("source"),
                "context": entity.get("context"),
                "score": hit.score,
            })

    # ---- BM25 path (optional) ----
    bm25_chunks: list[dict] = []
    if use_bm25:
        try:
            _ensure_bm25_index()
            bm25_chunks = _bm25_search(query, top_k=effective_top_k * BM25_FETCH_K_MULT)
        except Exception as e:
            print(f"  [BM25] search failed, falling back to vector-only: {e}")
            bm25_chunks = []

    # ---- Merge ----
    if use_bm25 and bm25_chunks:
        # RRF fusion: only chunks that appear in at least one list
        merged = _rrf_fuse(vector_chunks, bm25_chunks, effective_top_k)
    else:
        # Vector + post-hoc keyword promotion (v6 behaviour)
        keyword_chunk = _keyword_match(query, vector_chunks)
        seen_ids: set[str] = set()
        merged = []
        if keyword_chunk:
            merged.append(keyword_chunk)
            seen_ids.add(keyword_chunk["id"])
        for chunk in vector_chunks:
            if chunk["id"] not in seen_ids:
                merged.append(chunk)
                seen_ids.add(chunk["id"])
                if len(merged) >= effective_top_k:
                    break

    # ---- Optional MMR ----
    if use_mmr and merged:
        merged = mmr_rerank(query_emb, merged)[:effective_top_k]

    return merged[:effective_top_k]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_context(chunks: list[dict], max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """Format chunks into a markdown string for prompt injection.

    Args:
        chunks: List of chunk dicts from hybrid_search.
        max_chars: Max characters for the rendered context.

    Returns:
        String like:
        # dependencies/utils.py:257 — get_dependant()
        ```python
        def get_dependant(...):
            ...
        ```
    """
    parts = []
    remaining = max_chars

    for chunk in chunks:
        header = f"# {chunk['file']}:{chunk['line']} — {chunk['name']}()"
        code = chunk["source"]
        block = f"{header}\n```python\n{code}\n```"

        if remaining - len(block) < 0 and parts:
            break

        # Truncate first block if it alone exceeds max_chars
        if not parts and len(block) > remaining:
            truncated_code = code[:max(1, remaining - len(header) - 20)] + "\n# ... [truncated]"
            block = f"{header}\n```python\n{truncated_code}\n```"
            parts.append(block)
            break

        parts.append(block)
        remaining -= len(block)

    if not parts:
        return ""

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 50)
    print("RAG Module Self-Test")
    print("=" * 50)

    print("\n[1] Loading Milvus...")
    col = load_milvus()
    print(f"    Collection loaded. Num entities: {col.num_entities}")

    print("\n[2] Testing hybrid_search...")
    tests = [
        "get_dependant() dependencies/utils.py:257 被调用后的步骤",
        "analyze_param 在 dependencies/utils.py 中的实现",
        "get_body_field",
        "RequestValidationError 定义在哪里",
    ]
    for q in tests:
        print(f"\n  Query: {q[:60]}...")
        chunks = hybrid_search(col, q, top_k=3)
        for c in chunks:
            print(f"    [{c['score']:.4f}] {c['id']} — {c['name']}()")
        print(f"    → context chars: {len(format_context(chunks))}")

    print("\n[Done]")
