"""Quick test: BM25+vector (RRF) + MMR vs BM25+vector (RRF) + bge-reranker.

Compares Recall@5 on 50 benchmark questions using the v6 index.

Usage:
    python pe/quicktest_rerank.py
"""

import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pymilvus import connections

from pe.eval_retrieval import _parse_gt_files, _find_relevant_indices


def main():
    t_start = time.time()

    # ── Config ──────────────────────────────────────────────────────────────
    PE_DIR = Path(__file__).resolve().parent
    questions_file = PROJECT_ROOT / "benchmark" / "questions.jsonl"
    milvus_db = PE_DIR / "milvus_lite.db"
    collection_name = "fastapi_chunks"
    search_top_k = 20       # candidates before rerank/MMR
    rerank_top_k = 5        # keep this many after rerank/MMR

    # ── Reset Milvus connections & BM25 cache to guarantee correct db ──────
    for alias in ["default", "bm25_default"]:
        try:
            connections.disconnect(alias)
        except Exception:
            pass
    from pe import rag
    rag._bm25_index = None
    rag._bm25_chunk_ids = []
    rag._bm25_id_to_chunk = {}
    rag.MILVUS_DB = milvus_db
    rag.COLLECTION_NAME = collection_name

    # ── Load Milvus collection ──────────────────────────────────────────────
    print("Loading Milvus collection...")
    collection = rag.load_milvus()
    print(f"  Collection loaded ({collection.num_entities} entities)\n")

    # ── Load questions ──────────────────────────────────────────────────────
    questions = []
    with open(questions_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    print(f"Loaded {len(questions)} questions\n")

    # ── Load bge-reranker-base ──────────────────────────────────────────────
    print("Loading bge-reranker-base...")
    t_model = time.time()
    from sentence_transformers import CrossEncoder
    model = None
    try:
        model = CrossEncoder('BAAI/bge-reranker-base', max_length=512)
    except Exception as e:
        print(f"  Default HF endpoint failed: {e}")
        print("  Retrying with hf-mirror...")
        os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
        try:
            model = CrossEncoder('BAAI/bge-reranker-base', max_length=512)
        except Exception as e2:
            print(f"  hf-mirror also failed: {e2}")
            print("  STOP — cannot download reranker model, exiting.")
            sys.exit(1)
    t_model_done = time.time()
    model_load_sec = t_model_done - t_model
    print(f"  Model loaded in {model_load_sec:.1f}s\n")

    # ── Evaluate each question ──────────────────────────────────────────────
    results = []

    for q in questions:
        qid = q["id"]
        question = q["question"]
        gt = q["ground_truth"]
        gt_files = _parse_gt_files(gt)
        total_gt = max(len(gt_files), 1)

        # ── Path A: BM25+vector+RRF → bge-reranker → top 5 ──────────────
        chunks = rag.hybrid_search(
            collection, question,
            top_k=search_top_k,
            adaptive_top_k=False,
            use_bm25=True,
            use_mmr=False,
        )
        # Score each (query, source) pair with cross-encoder
        pairs = [(question, c.get("source") or "") for c in chunks]
        scores = model.predict(pairs, show_progress_bar=False)
        # Sort descending by cross-encoder score
        scored = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
        reranked_top = [c for _, c in scored[:rerank_top_k]]

        covered_rerank = set()
        for c in reranked_top:
            covered_rerank.update(_find_relevant_indices(c, gt_files))
        recall_reranker = len(covered_rerank) / total_gt

        # ── Path B: BM25+vector+RRF → MMR → top 5 ──────────────────────
        chunks_mmr = rag.hybrid_search(
            collection, question,
            top_k=search_top_k,
            adaptive_top_k=False,
            use_bm25=True,
            use_mmr=True,
        )
        covered_mmr = set()
        for c in chunks_mmr[:rerank_top_k]:
            covered_mmr.update(_find_relevant_indices(c, gt_files))
        recall_mmr = len(covered_mmr) / total_gt

        results.append({
            "id": qid,
            "recall_reranker": recall_reranker,
            "recall_mmr": recall_mmr,
        })

    t_done = time.time()
    total_sec = t_done - t_start

    # ── Summarise ───────────────────────────────────────────────────────────
    recall_reranker_vals = [r["recall_reranker"] for r in results]
    recall_mmr_vals = [r["recall_mmr"] for r in results]
    avg_reranker = sum(recall_reranker_vals) / len(recall_reranker_vals)
    avg_mmr = sum(recall_mmr_vals) / len(recall_mmr_vals)

    wins_reranker = sum(1 for r in results if r["recall_reranker"] > r["recall_mmr"])
    wins_mmr = sum(1 for r in results if r["recall_mmr"] > r["recall_reranker"])
    ties = sum(1 for r in results if r["recall_reranker"] == r["recall_mmr"])

    # Worst 5 for reranker
    sorted_by_reranker = sorted(results, key=lambda r: r["recall_reranker"])
    worst_5 = [r["id"] for r in sorted_by_reranker[:5]]
    worst_5_zero = [r["id"] for r in sorted_by_reranker[:5] if r["recall_reranker"] == 0]

    # ── Print ───────────────────────────────────────────────────────────────
    print("=" * 64)
    print("  Reranker (bge-reranker-base) vs MMR — Recall@5 Comparison")
    print("=" * 64)
    print(f"  Avg R@5 (BM25+vector -> RRF -> reranker top 5):   {avg_reranker:.4f}")
    print(f"  Avg R@5 (BM25+vector -> RRF -> MMR top 5):         {avg_mmr:.4f}")
    print(f"  Reranker wins: {wins_reranker}  |  MMR wins: {wins_mmr}  |  Ties: {ties}")
    if worst_5_zero:
        print(f"  Worst 5 (reranker R@5 = 0): {', '.join(worst_5_zero)}")
    else:
        worst_detail = []
        for r in sorted_by_reranker[:5]:
            worst_detail.append(f"{r['id']}({r['recall_reranker']:.2f})")
        print(f"  Worst 5 reranker R@5: {', '.join(worst_detail)}")

    print(f"\n  Model load time:  {model_load_sec:.1f}s")
    print(f"  Total test time:  {total_sec:.1f}s")

    # Per-question table
    print(f"\n  {'QID':>6s}  {'Reranker':>8s}  {'MMR':>8s}  {'Winner':>9s}")
    print("  " + "-" * 36)
    for r in results:
        if r["recall_reranker"] > r["recall_mmr"]:
            winner = "reranker"
        elif r["recall_mmr"] > r["recall_reranker"]:
            winner = "MMR"
        else:
            winner = "  tie  "
        print(f"  {r['id']:>6s}  {r['recall_reranker']:>8.4f}  {r['recall_mmr']:>8.4f}  {winner:>9s}")

    # ── Save report ─────────────────────────────────────────────────────────
    report_dir = PE_DIR / "results"
    report_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "config": {
            "reranker_path": "BM25+vector(RRF) -> bge-reranker-base top5",
            "mmr_path": "BM25+vector(RRF) -> MMR top5",
            "num_questions": len(questions),
            "reranker_model": "BAAI/bge-reranker-base",
            "reranker_max_length": 512,
            "milvus_db": str(milvus_db),
            "collection_name": collection_name,
            "hybrid_search_top_k": search_top_k,
            "use_bm25": True,
            "mmr_lambda": rag.MMR_LAMBDA,
            "rrf_k": rag.RRF_K,
        },
        "summary": {
            "avg_recall_reranker": round(avg_reranker, 4),
            "avg_recall_mmr": round(avg_mmr, 4),
            "reranker_wins": wins_reranker,
            "mmr_wins": wins_mmr,
            "ties": ties,
            "worst_5_ids": worst_5,
            "worst_5_zero_ids": worst_5_zero if worst_5_zero else None,
        },
        "per_question": results,
        "timing": {
            "model_download_seconds": round(model_load_sec, 1),
            "test_run_seconds": round(total_sec, 1),
        },
    }
    report_path = report_dir / "quicktest_rerank.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n  Report saved to: {report_path}")


if __name__ == "__main__":
    main()
