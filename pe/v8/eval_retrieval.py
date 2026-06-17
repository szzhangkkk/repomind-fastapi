"""Evaluate v8 retrieval quality on the v7min index.

Compares 4 configurations:
    v7_baseline       — adaptive + BM25 (current baseline)
    v8_rerank         — adaptive + BM25 + reranker
    v8_rewrite        — adaptive + BM25 + query_rewrite
    v8_rerank_rewrite — adaptive + BM25 + reranker + query_rewrite

Usage:
    python pe/v8/eval_retrieval.py
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pe.v8.rag_extended import hybrid_search

QUESTIONS_FILE = PROJECT_ROOT / "benchmark" / "questions.jsonl"

# ── Only the v7min index ──
V7MIN_DB = str(PE_DIR / "milvus_lite_v7min.db")
COLLECTION_NAME = "fastapi_chunks"

# ── Evaluation configurations ──
CONFIGS = [
    {
        "name": "v7_baseline",
        "top_k": 10,
        "adaptive_top_k": True,
        "use_mmr": False,
        "use_bm25": True,
        "use_reranker": False,
        "use_query_rewrite": False,
    },
    {
        "name": "v8_rerank",
        "top_k": 10,
        "adaptive_top_k": True,
        "use_mmr": False,
        "use_bm25": True,
        "use_reranker": True,
        "use_query_rewrite": False,
    },
    {
        "name": "v8_rewrite",
        "top_k": 10,
        "adaptive_top_k": True,
        "use_mmr": False,
        "use_bm25": True,
        "use_reranker": False,
        "use_query_rewrite": True,
    },
    {
        "name": "v8_rerank_rewrite",
        "top_k": 10,
        "adaptive_top_k": True,
        "use_mmr": False,
        "use_bm25": True,
        "use_reranker": True,
        "use_query_rewrite": True,
    },
]


# ---------------------------------------------------------------------------
# Ground-truth parsing (same logic as pe/eval_retrieval.py)
# ---------------------------------------------------------------------------

def _parse_gt_files(ground_truth: str) -> list[tuple[str, int]]:
    """Parse ground truth to extract (file, line) references."""
    files = []
    for m in re.finditer(r"(\w+\.py)[：:](\d+)", ground_truth):
        files.append((m.group(1), int(m.group(2))))
    for m in re.finditer(r"(\w+\.py)[，,]\s*第(\d+)行", ground_truth):
        files.append((m.group(1), int(m.group(2))))
    for m in re.finditer(r"(\w+\.py)[，,]?\s*line\s+(\d+)", ground_truth):
        files.append((m.group(1), int(m.group(2))))
    return files


def _find_relevant_indices(
    chunk: dict, gt_files: list[tuple[str, int]]
) -> list[int]:
    """Return indices of gt_files that this chunk matches."""
    chunk_file = chunk.get("file", "")
    chunk_line = chunk.get("line", 0)
    matched = []
    for i, (gt_file, gt_line) in enumerate(gt_files):
        if gt_file in chunk_file and abs(chunk_line - gt_line) <= 20:
            matched.append(i)
    return matched


# ---------------------------------------------------------------------------
# Collection loading (resets BM25 cache for the v7min db)
# ---------------------------------------------------------------------------

def _load_v7min_collection():
    """Load the v7min Milvus collection, resetting caches."""
    from pe import rag
    from pymilvus import connections

    for alias in ["default", "bm25_default"]:
        try:
            connections.disconnect(alias)
        except Exception:
            pass

    # Reset BM25 cache so it rebuilds from the correct db
    rag._bm25_index = None
    rag._bm25_chunk_ids = []
    rag._bm25_id_to_chunk = {}
    rag.MILVUS_DB = Path(V7MIN_DB)
    rag.COLLECTION_NAME = COLLECTION_NAME
    return rag.load_milvus()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_retrieval(config: dict) -> dict:
    """Evaluate retrieval quality for a single config on the v7min index.

    Returns:
        dict with recall_at_5, recall_at_10, mrr_at_10, per_category, per_question.
    """
    questions = []
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))

    collection = _load_v7min_collection()

    recall_at_k = {k: [] for k in [5, 10]}
    mrr_at_10 = []
    category_at_k = defaultdict(lambda: {k: [] for k in [5, 10]})
    per_question = []

    for q in questions:
        qid = q["id"]
        cat = q["category"]
        question = q["question"]
        gt = q["ground_truth"]
        gt_files = _parse_gt_files(gt)

        chunks = hybrid_search(
            collection, question,
            top_k=config["top_k"],
            category=q.get("category"),
            adaptive_top_k=config["adaptive_top_k"],
            use_mmr=config["use_mmr"],
            use_bm25=config["use_bm25"],
            use_reranker=config["use_reranker"],
            use_query_rewrite=config["use_query_rewrite"],
        )

        total_gt = max(len(gt_files), 1)
        for k in [5, 10]:
            covered = set()
            for c in chunks[:k]:
                matched = _find_relevant_indices(c, gt_files)
                covered.update(matched)
            recall = len(covered) / total_gt
            recall_at_k[k].append(recall)
            category_at_k[cat][k].append(recall)

        # MRR@10
        mrr_found = False
        for rank, c in enumerate(chunks[:10], 1):
            if _find_relevant_indices(c, gt_files):
                mrr_at_10.append(1.0 / rank)
                mrr_found = True
                break
        if not mrr_found:
            mrr_at_10.append(0.0)

        per_question.append({
            "id": qid,
            "category": cat,
            "gt_files": [f"{f}:{l}" for f, l in gt_files],
            "recall_at_5": recall_at_k[5][-1],
            "recall_at_10": recall_at_k[10][-1],
        })

    per_category = {}
    for cat in sorted(category_at_k):
        vals = category_at_k[cat][10]
        per_category[cat] = sum(vals) / len(vals)

    return {
        "recall_at_5": sum(recall_at_k[5]) / len(recall_at_k[5]),
        "recall_at_10": sum(recall_at_k[10]) / len(recall_at_k[10]),
        "mrr_at_10": sum(mrr_at_10) / len(mrr_at_10),
        "per_category": per_category,
        "per_question": per_question,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    all_metrics: dict[str, dict] = {}
    per_cat_all: dict[str, dict[str, float]] = defaultdict(dict)

    for config in CONFIGS:
        name = config["name"]
        print(f"\n{'─' * 50}")
        print(f"Evaluating: {name}")
        flags = []
        if config["use_bm25"]:
            flags.append("BM25")
        if config["use_reranker"]:
            flags.append("reranker")
        if config["use_query_rewrite"]:
            flags.append("rewrite")
        print(f"  adaptive={config['adaptive_top_k']}, {'+'.join(flags) if flags else 'vector-only'}")
        sys.stdout.flush()

        metrics = evaluate_retrieval(config)
        all_metrics[name] = {
            "recall_at_5": round(metrics["recall_at_5"], 4),
            "recall_at_10": round(metrics["recall_at_10"], 4),
            "mrr_at_10": round(metrics["mrr_at_10"], 4),
        }
        for cat, r10 in metrics["per_category"].items():
            per_cat_all[cat][name] = round(r10, 4)

    # ── Print comparison table ──
    print(f"\n\n=== v7min Index — Retrieval Comparison ===")
    print(f"{'Config':25s} | {'R@5':>6s} | {'R@10':>6s} | {'MRR@10':>7s}")
    print("-" * 52)
    for config in CONFIGS:
        name = config["name"]
        m = all_metrics[name]
        print(f"{name:25s} | {m['recall_at_5']:.4f} | {m['recall_at_10']:.4f} | {m['mrr_at_10']:.4f}")

    # ── Per-category breakdown ──
    print(f"\n\n=== Per-Category R@10 ===")
    cats = sorted(per_cat_all)
    header = f"{'Category':20s}"
    for config in CONFIGS:
        header += f" | {config['name']:>16s}"
    print(header)
    print("-" * (20 + 19 * len(CONFIGS)))
    for cat in cats:
        row = f"{cat:20s}"
        for config in CONFIGS:
            val = per_cat_all[cat].get(config["name"], 0.0)
            row += f" | {val:16.4f}"
        print(row)

    # ── Save report ──
    REPORT_DIR = PE_DIR / "v8"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "index": "v7min",
        "description": "v8 retrieval evaluation: baseline vs reranker vs rewrite vs combined",
        "configs": {},
        "per_category": {},
    }
    for config in CONFIGS:
        name = config["name"]
        cfg_clean = {k: v for k, v in config.items() if k != "name"}
        report["configs"][name] = {
            "config": cfg_clean,
            "metrics": all_metrics[name],
        }
    for cat in cats:
        report["per_category"][cat] = dict(per_cat_all[cat])

    report_path = REPORT_DIR / "v8_retrieval_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n\nReport saved to: {report_path}")
    sys.exit(0)
