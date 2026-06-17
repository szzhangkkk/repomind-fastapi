"""Evaluate retrieval quality using the 50 benchmark questions as ground-truth.

Computes Recall@5, Recall@10, and MRR@10 based on whether the RAG-retrieved
chunks contain the source files referenced in the ground truth answers.

Usage:
    python pe/eval_retrieval.py
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from pe.rag import hybrid_search
QUESTIONS_FILE = PROJECT_ROOT / "benchmark" / "questions.jsonl"


def _parse_gt_files(ground_truth: str) -> list[tuple[str, int]]:
    """Parse ground truth to extract (file, line) references.

    Matches patterns like:
        func_name()(file.py:123)
        dependencies/utils.py line 257
    """
    files = []
    # Pattern 1: func_name()(file.py:123)
    for m in re.finditer(r"(\w+\.py)[：:](\d+)", ground_truth):
        files.append((m.group(1), int(m.group(2))))
    # Pattern 2: file.py 第XXX行
    for m in re.finditer(r"(\w+\.py)[，,]\s*第(\d+)行", ground_truth):
        files.append((m.group(1), int(m.group(2))))
    # Pattern 3: file.py line XXX
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


def _load_collection(milvus_db: str, collection_name: str = "fastapi_chunks"):
    """Load Milvus collection from a specific db file, resetting caches."""
    from pe import rag
    from pymilvus import connections
    for alias in ["default", "bm25_default"]:
        try:
            connections.disconnect(alias)
        except Exception:
            pass
    # Reset BM25 cache so it rebuilds from the correct db on next use
    rag._bm25_index = None
    rag._bm25_chunk_ids = []
    rag._bm25_id_to_chunk = {}
    rag.MILVUS_DB = Path(milvus_db)
    rag.COLLECTION_NAME = collection_name
    return rag.load_milvus()


def evaluate_retrieval(config: dict) -> dict:
    """Evaluate retrieval quality for a single config.

    Args:
        config: {
            "top_k": 10,
            "adaptive_top_k": False,
            "use_mmr": False,
            "use_bm25": False,
            "collection_name": "fastapi_chunks",
            "milvus_db": "pe/milvus_lite.db",
        }

    Returns:
        dict with keys: recall_at_5, recall_at_10, mrr_at_10,
                        per_category (dict of {cat: recall_at_10}),
                        per_question (list of detail dicts).
    """
    questions = []
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))

    collection = _load_collection(
        config["milvus_db"],
        config.get("collection_name", "fastapi_chunks"),
    )

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

        # MRR@10: reciprocal rank of first chunk matching ANY ground truth file
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


if __name__ == "__main__":
    # ── 4 RAG configurations ──────────────────────────────────────
    CONFIGS = [
        ("v6_baseline",       dict(top_k=10, adaptive_top_k=False, use_mmr=False, use_bm25=False)),
        ("adaptive",           dict(top_k=10, adaptive_top_k=True,  use_mmr=False, use_bm25=False)),
        ("adaptive_bm25",      dict(top_k=10, adaptive_top_k=True,  use_mmr=False, use_bm25=True)),
        ("adaptive_bm25_mmr",  dict(top_k=10, adaptive_top_k=True,  use_mmr=True,  use_bm25=True)),
    ]
    INDEXES = {
        "v6_index":    str(PE_DIR / "milvus_lite.db"),
        "v7min_index": str(PE_DIR / "milvus_lite_v7min.db"),
    }

    # ── Run 8 evaluations (4 configs × 2 indexes) ────────────────
    all_metrics: dict[str, dict[str, dict]] = {}
    per_cat_all: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    worst_10 = []

    for config_name, base_cfg in CONFIGS:
        all_metrics[config_name] = {}
        for idx_name, milvus_db in INDEXES.items():
            cfg = {**base_cfg, "milvus_db": milvus_db, "collection_name": "fastapi_chunks"}
            print(f"\n{'─' * 50}")
            print(f"Evaluating: {config_name} on {idx_name}")
            print(f"  adaptive={cfg['adaptive_top_k']}, MMR={cfg['use_mmr']}, BM25={cfg['use_bm25']}")
            sys.stdout.flush()

            metrics = evaluate_retrieval(cfg)
            all_metrics[config_name][idx_name] = {
                "recall_at_5":  round(metrics["recall_at_5"], 4),
                "recall_at_10": round(metrics["recall_at_10"], 4),
                "mrr_at_10":    round(metrics["mrr_at_10"], 4),
            }
            for cat, r10 in metrics["per_category"].items():
                per_cat_all[cat][config_name][idx_name] = round(r10, 4)

            if config_name == "adaptive_bm25_mmr" and idx_name == "v6_index":
                worst_10 = sorted(metrics["per_question"], key=lambda x: x["recall_at_10"])[:10]

    # ── Print comparison table ────────────────────────────────────
    TABLE_HEADERS = [
        ("v6_index", "v6 索引 (text_for_embedding 含源码)"),
        ("v7min_index", "v7min 索引 (text_for_embedding 仅签名)"),
    ]
    for idx_name, idx_label in TABLE_HEADERS:
        print(f"\n\n=== {idx_label} ===")
        print(f"{'Config':25s} | {'R@5':>6s} | {'R@10':>6s} | {'MRR@10':>7s}")
        print("-" * 52)
        for config_name, _ in CONFIGS:
            m = all_metrics[config_name][idx_name]
            print(f"{config_name:25s} | {m['recall_at_5']:.4f} | {m['recall_at_10']:.4f} | {m['mrr_at_10']:.4f}")

    # ── Save report ───────────────────────────────────────────────
    REPORT_DIR = PE_DIR / "results"
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "configs": {},
        "per_category": {},
        "per_question_worst10": worst_10,
    }
    for config_name, base_cfg in CONFIGS:
        report["configs"][config_name] = {
            "config": base_cfg,
            "v6_index": all_metrics[config_name]["v6_index"],
            "v7min_index": all_metrics[config_name]["v7min_index"],
        }
    for cat in sorted(per_cat_all):
        report["per_category"][cat] = {
            cn: {ik: per_cat_all[cat][cn][ik] for ik in INDEXES}
            for cn, _ in CONFIGS
        }

    report_path = REPORT_DIR / "v7_retrieval_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n\nReport saved to: {report_path}")
    sys.exit(0)
