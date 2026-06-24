"""Re-judge existing benchmark results against updated ground_truth.

Usage:
    python benchmark/rejudge.py                    # re-judge ALL result files
    python benchmark/rejudge.py --files baseline.json pe_v3.json  # specific files
    python benchmark/rejudge.py --dry-run          # preview what would change
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BENCHMARK_DIR / "questions.jsonl"
JUDGE_PROMPT_FILE = BENCHMARK_DIR / "judge_prompts" / "score.txt"
RESULTS_DIR = BENCHMARK_DIR / "results"

sys.path.insert(0, str(PROJECT_ROOT))


def load_questions() -> dict[str, dict]:
    """Load questions.jsonl, indexed by id."""
    qs = {}
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                q = json.loads(line)
                qs[q["id"]] = q
    return qs


def call_llm(client, model: str, system: str, user: str, max_retries: int = 3, seed: int = 42) -> str:
    """Call LLM with retry logic."""
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                seed=seed,
                max_tokens=2048,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt >= max_retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  [retry {attempt + 1}/{max_retries}] API error: {e} (wait {wait}s)")
            time.sleep(wait)


def parse_judge_response(raw: str) -> dict:
    """Extract {score, reason} from judge LLM output."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        inner = []
        for line in lines[1:]:
            if line.strip() == "```":
                break
            inner.append(line)
        text = "\n".join(inner)

    try:
        obj = json.loads(text)
        score = int(obj.get("score", 0))
        reason = str(obj.get("reason", ""))
        score = max(0, min(3, score))
        return {"score": score, "reason": reason}
    except (json.JSONDecodeError, ValueError, TypeError):
        m = re.search(r'"score"\s*:\s*(\d)', text)
        score = int(m.group(1)) if m else 0
        score = max(0, min(3, score))
        return {"score": score, "reason": f"[parse fallback] {text[:200]}"}


def rejudge_file(
    result_path: Path,
    questions: dict[str, dict],
    client,
    model: str,
    judge_template: str,
    dry_run: bool = False,
) -> dict | None:
    """Re-judge one result file. Returns None if no changes needed."""
    with open(result_path, encoding="utf-8") as f:
        data = json.load(f)

    results = data.get("results", [])
    old_summary = data.get("summary", {})
    changes = []

    for r in results:
        qid = r["id"]
        if qid not in questions:
            continue

        q = questions[qid]
        model_answer = r["model_answer"]
        new_gt = q["ground_truth"]

        # Build judge prompt
        judge_prompt = judge_template.format(
            question=q["question"],
            ground_truth=new_gt,
            model_answer=model_answer,
        )

        judge_raw = call_llm(
            client, model,
            system="你是一个严格的评测裁判。请严格按照要求的 JSON 格式输出评分结果。",
            user=judge_prompt,
        )
        judge_result = parse_judge_response(judge_raw)
        new_score = judge_result["score"]
        new_reason = judge_result["reason"]

        old_score = r.get("score", -1)
        if new_score != old_score:
            changes.append({
                "qid": qid,
                "old_score": old_score,
                "new_score": new_score,
                "old_reason": r.get("reason", "")[:100],
                "new_reason": new_reason[:100],
            })

        r["score"] = new_score
        r["reason"] = new_reason
        time.sleep(0.5)  # Rate limit

    if not changes:
        print(f"  No score changes.")
        return None

    # Recalculate summary
    category_scores = defaultdict(list)
    for r in results:
        cat = r.get("category", "unknown")
        category_scores[cat].append(r["score"])

    total_score = sum(r["score"] for r in results)
    max_score = len(results) * 3
    avg_score = total_score / len(results) if results else 0

    category_summary = {}
    for cat, scores in sorted(category_scores.items()):
        cat_sum = sum(scores)
        cat_max = len(scores) * 3
        cat_avg = cat_sum / len(scores) if scores else 0
        category_summary[cat] = {
            "count": len(scores),
            "total": cat_sum,
            "max": cat_max,
            "average": round(cat_avg, 2),
        }

    new_summary = {
        "model": old_summary.get("model", model),
        "total_questions": len(results),
        "total_score": total_score,
        "max_score": max_score,
        "average_score": round(avg_score, 2),
        "by_category": category_summary,
    }

    if not dry_run:
        # Backup and save
        bak_path = result_path.with_suffix(".json.bak")
        with open(bak_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        output = {"summary": new_summary, "results": results}
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"  Backup: {bak_path.name}")

    # Print changes
    old_total = old_summary.get("total_score", "?")
    delta = new_summary["total_score"] - old_total if isinstance(old_total, int) else 0
    sign = "+" if delta > 0 else ""
    print(f"  Score: {old_total} → {new_summary['total_score']} ({sign}{delta})")
    for ch in changes:
        print(f"    {ch['qid']}: {ch['old_score']} → {ch['new_score']}")

    return {"old": old_summary, "new": new_summary}


def main():
    parser = argparse.ArgumentParser(description="Re-judge benchmark results against updated ground_truth")
    parser.add_argument("--files", nargs="*", default=None,
                        help="Specific result files to re-judge (default: all JSON files in results/)")
    parser.add_argument("--model", default="deepseek-chat", help="Judge model (default: deepseek-chat)")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without saving")
    args = parser.parse_args()

    # Load questions (with fixed ground_truth)
    questions = load_questions()
    print(f"Loaded {len(questions)} questions (fixed ground_truth)")

    # Load judge template
    judge_template = JUDGE_PROMPT_FILE.read_text(encoding="utf-8")
    print(f"Judge prompt: {JUDGE_PROMPT_FILE.name}")

    # Init API client
    from config.load_config import get_deepseek_client
    client = get_deepseek_client()

    # Find result files
    if args.files:
        result_files = [RESULTS_DIR / f for f in args.files]
        missing = [f for f in result_files if not f.exists()]
        if missing:
            print(f"ERROR: Files not found: {missing}")
            sys.exit(1)
    else:
        # Collect all JSON files including those without .json suffix (e.g. qwen_*)
        json_files = list(RESULTS_DIR.glob("*.json"))
        json_files += [p for p in RESULTS_DIR.iterdir()
                       if p.is_file() and not p.suffix and not p.name.endswith(".bak")
                       and not p.name.startswith("_") and p.name != "__pycache__"]
        result_files = sorted(json_files, key=lambda p: p.stat().st_mtime)

    print(f"Result files to re-judge: {len(result_files)}")
    if args.dry_run:
        print("DRY RUN — no files will be modified")
    print()

    all_changes = {}
    for i, rp in enumerate(result_files):
        print(f"[{i + 1}/{len(result_files)}] {rp.name}")
        ch = rejudge_file(rp, questions, client, args.model, judge_template, dry_run=args.dry_run)
        if ch:
            all_changes[rp.name] = ch

    # Summary
    print()
    print("=" * 60)
    print("REJUDGE SUMMARY")
    print("=" * 60)
    for fname, ch in all_changes.items():
        old_total = ch["old"].get("total_score", "?")
        new_total = ch["new"]["total_score"]
        delta = new_total - old_total if isinstance(old_total, int) else 0
        sign = "+" if delta > 0 else ""
        print(f"  {fname:45s}  {old_total} → {new_total}  ({sign}{delta})")
    print()

    if args.dry_run:
        print("Dry run complete. Run without --dry-run to apply changes.")
    else:
        print(f"Re-judged {len(result_files)} files. Backups saved as .json.bak.")


if __name__ == "__main__":
    main()
