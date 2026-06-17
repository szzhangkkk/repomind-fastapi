"""RepoMind Benchmark v8 — evaluation pipeline with v8 RAG extensions.

Fork of benchmark/run_eval.py. The only differences are:
  1. Imports from pe.v8.rag_extended instead of pe.rag
  2. Two extra CLI flags: --use-reranker, --use-query-rewrite
  3. hybrid_search() call passes use_reranker / use_query_rewrite
  4. Default output filename auto-prefixed with v8_

Otherwise identical to run_eval.py — same PE loading, judge, output format.
"""

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from collections import defaultdict

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = Path(__file__).resolve().parent
QUESTIONS_FILE = BENCHMARK_DIR / "questions.jsonl"
JUDGE_PROMPT_FILE = BENCHMARK_DIR / "judge_prompts" / "score.txt"
JUDGE_RELAXED_FILE = BENCHMARK_DIR / "judge_prompts" / "score_relaxed.txt"
RESULTS_DIR = BENCHMARK_DIR / "results"
PE_DIR = PROJECT_ROOT / "pe"

# ---------------------------------------------------------------------------
# PE (Prompt Engineering) version configs
# ---------------------------------------------------------------------------
PE_CONFIGS = {
    1: {
        "system_prompt_file": PE_DIR / "v1_system.txt",
        "fewshot_file": None,
        "cot_file": None,
        "postprocess": False,
    },
    2: {
        "system_prompt_file": PE_DIR / "v1_system.txt",
        "fewshot_file": PE_DIR / "fewshot_examples.jsonl",
        "cot_file": None,
        "postprocess": False,
    },
    3: {
        "system_prompt_file": PE_DIR / "v1_system.txt",
        "fewshot_file": PE_DIR / "fewshot_examples.jsonl",
        "cot_file": PE_DIR / "v3_cot.txt",
        "postprocess": False,
    },
    4: {
        "system_prompt_file": PE_DIR / "v1_system.txt",
        "fewshot_file": PE_DIR / "fewshot_examples.jsonl",
        "cot_file": PE_DIR / "v3_cot.txt",
        "postprocess": True,
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_judge_prompt() -> str:
    """Load the judge prompt template."""
    return JUDGE_PROMPT_FILE.read_text(encoding="utf-8")


def load_questions(limit: int | None = None) -> list[dict]:
    """Load questions from JSONL file."""
    questions = []
    with open(QUESTIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    if limit is not None:
        questions = questions[:limit]
    return questions


def call_llm(client, model: str, system: str, user: str, max_retries: int = 3, seed: int = 42) -> str:
    """Call LLM with retry logic. temperature=0 for reproducibility."""
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
            wait = 2 ** attempt
            print(f"  [retry {attempt + 1}/{max_retries}] API error: {e} (wait {wait}s)")
            if attempt < max_retries - 1:
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Local model inference (FT model)
# ---------------------------------------------------------------------------

_local_model_pipeline = None


def _init_local_llm(model_name_or_path: str, lora_path: str | None = None):
    """Initialize local model for inference. Caches globally for reuse."""
    global _local_model_pipeline

    print(f"[Local] Loading model from {model_name_or_path}")
    if lora_path:
        print(f"[Local] Will load LoRA adapter from {lora_path}")

    from transformers import AutoTokenizer  # noqa: E402
    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)

    use_vllm = False
    if not lora_path:
        try:
            from vllm import LLM  # noqa: E402
            llm = LLM(model=model_name_or_path, tokenizer=model_name_or_path)
            _local_model_pipeline = ("vllm", llm, tokenizer)
            use_vllm = True
            print(f"[Local] Using vLLM backend")
        except ImportError:
            pass

    if not use_vllm:
        from transformers import AutoModelForCausalLM  # noqa: E402
        import torch  # noqa: E402
        model = AutoModelForCausalLM.from_pretrained(
            model_name_or_path,
            torch_dtype=torch.bfloat16,
            device_map="auto",

        )
        if lora_path:
            from peft import PeftModel  # noqa: E402
            model = PeftModel.from_pretrained(model, lora_path)
            print(f"[Local] Applied LoRA from {lora_path}")
        model.eval()
        _local_model_pipeline = ("transformers", model, tokenizer)
        print(f"[Local] Using transformers backend")


def call_local_llm(client, model: str, system: str, user: str, max_retries: int = 3, seed: int = 42) -> str:
    """Local model replacement for call_llm. Same signature, ignores client."""
    global _local_model_pipeline
    if _local_model_pipeline is None:
        raise RuntimeError("Local model not initialized. Call _init_local_llm() first.")

    backend, model_obj, tokenizer = _local_model_pipeline

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )

    if backend == "vllm":
        from vllm import SamplingParams  # noqa: E402
        sampling_params = SamplingParams(
            temperature=0.0,
            max_tokens=2048,
            seed=seed,
        )
        outputs = model_obj.generate([prompt], sampling_params)
        return outputs[0].outputs[0].text.strip()
    else:
        import torch  # noqa: E402
        inputs = tokenizer(prompt, return_tensors="pt").to(model_obj.device)
        with torch.no_grad():
            outputs = model_obj.generate(
                **inputs,
                max_new_tokens=256,
                temperature=0.0,
                do_sample=False,
            )
        response = tokenizer.decode(
            outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True
        )
        return response.strip()


def trim_context(snippet: str) -> str:
    """Trim context_snippet to function signature only (first 3 lines)."""
    lines = snippet.strip().split("\n")
    kept = []
    for line in lines:
        kept.append(line)
        if len(kept) >= 3:
            break
        if line.strip().startswith('"""') or line.strip().startswith("'''"):
            break
    return "\n".join(kept)


def load_fewshot_examples(path: Path) -> list[dict]:
    """Load few-shot examples from JSONL file."""
    examples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def load_postprocessor():
    """Load v4 postprocessor module dynamically."""
    spec = importlib.util.spec_from_file_location(
        "v4_postprocess", PE_DIR / "v4_postprocess.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.postprocess_answer


def build_answer_prompt(
    question: dict,
    context_mode: str = "full",
    system_prompt: str | None = None,
    fewshot_examples: list[dict] | None = None,
    add_cot: bool = False,
) -> tuple[str, str]:
    """Build system and user prompts for answering a question.

    Returns (system_content, user_content).
    """
    if system_prompt:
        sys_content = system_prompt
    else:
        sys_content = "你是一个专业的 Python 源码分析助手。请根据提供的源码片段和上下文，回答以下关于 FastAPI 源码的问题。请尽可能精确地给出文件名、行号、函数调用链等信息。"

    parts = []

    if fewshot_examples:
        parts.append("## 示例\n")
        for ex in fewshot_examples:
            parts.append(f"### 问题: {ex['question']}")
            parts.append(f"### 回答:\n{ex['answer']}")
            parts.append("")

    parts.append(f"## 问题")
    parts.append(question["question"])

    snippet = question.get("context_snippet", "")
    if snippet:
        if context_mode == "minimal":
            snippet = trim_context(snippet)
        parts += [
            "",
            "## 相关源码片段",
            f"```python\n{snippet}\n```",
        ]
    parts += [
        "",
        "## 请回答",
    ]

    return sys_content, "\n".join(parts)


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
        import re
        m = re.search(r'"score"\s*:\s*(\d)', text)
        score = int(m.group(1)) if m else 0
        m2 = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
        reason = m2.group(1) if m2 else raw[:300]
        return {"score": max(0, min(3, score)), "reason": reason}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="RepoMind benchmark evaluation (v8 RAG)")
    parser.add_argument("--model", default="deepseek-chat", help="Model name (default: deepseek-chat)")
    parser.add_argument("--limit", type=int, default=None, help="Only run first N questions")
    parser.add_argument("--context-mode", choices=["full", "minimal"], default="full",
                        help="Context snippet mode: full (entire snippet) or minimal (function signature only)")
    parser.add_argument("--relaxed", action="store_true",
                        help="Use relaxed scoring rubric (looser line tolerances, liberal 0-point threshold)")
    parser.add_argument("--pe-version", type=int, choices=[0, 1, 2, 3, 4], default=0,
                        help="PE optimization version (0=baseline, 1=system prompt, 2=fewshot, 3=cot, 4=postprocess)")
    parser.add_argument("--output-name", default=None,
                        help="Custom output file name (default: auto from pe-version with v8_ prefix)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for LLM sampling (used to set temperature=0 for reproducibility)")
    parser.add_argument("--enable-rag", action="store_true", default=False,
                        help="Enable RAG injection (retrieve relevant code chunks from Milvus)")
    parser.add_argument("--rag-top-k", type=int, default=3,
                        help="Number of RAG chunks to inject (default: 3)")
    parser.add_argument("--rag-collection", type=str, default="fastapi_chunks",
                        help="Milvus collection name for RAG (default: fastapi_chunks)")
    parser.add_argument("--rag-db-path", type=str, default=None,
                        help="Path to Milvus Lite db file (default: pe/milvus_lite.db)")
    parser.add_argument("--adaptive-top-k", action="store_true", default=False,
                        help="Override --rag-top-k with category-specific values (see pe.rag.ADAPTIVE_TOP_K)")
    parser.add_argument("--use-mmr", action="store_true", default=False,
                        help="Apply MMR reranking for chunk diversity")
    parser.add_argument("--use-bm25", action="store_true", default=False,
                        help="Enable BM25 + RRF hybrid fusion (parallel keyword+vector retrieval)")
    # ── v8: new CLI flags ──
    parser.add_argument("--use-reranker", action="store_true", default=False,
                        help="Apply Cross-Encoder reranker (BAAI/bge-reranker-v2-m3) after retrieval")
    parser.add_argument("--use-query-rewrite", action="store_true", default=False,
                        help="Rewrite short queries with category-specific cues for better embedding retrieval")
    # Local FT model args
    parser.add_argument("--local-model", default=None,
                        help="Local model name or path for FT evaluation (e.g. 'Qwen/Qwen2.5-Coder-7B-Instruct' or /path/to/model)")
    parser.add_argument("--lora-path", default=None,
                        help="LoRA adapter path for finetuned model (e.g. /path/to/lora-adapter)")
    args = parser.parse_args()

    # Setup
    sys.path.insert(0, str(PROJECT_ROOT))
    from config.load_config import get_deepseek_client

    client = get_deepseek_client()
    judge_file = JUDGE_RELAXED_FILE if args.relaxed else JUDGE_PROMPT_FILE
    judge_prompt_template = judge_file.read_text(encoding="utf-8")
    scoring_mode = "relaxed" if args.relaxed else "strict"

    # Initialize local model if requested (only used for answers, judge still uses DeepSeek)
    _use_local_model = False
    if args.local_model:
        _init_local_llm(args.local_model, args.lora_path)
        _use_local_model = True
        print(f"[Local] Local model active for answers (judge still uses DeepSeek)")
        if args.lora_path:
            print(f"[Local] LoRA loaded from: {args.lora_path}")

    questions = load_questions(limit=args.limit)

    # Load PE components based on version
    pe_version = args.pe_version
    system_prompt = None
    fewshot_examples = None
    add_cot = False
    postprocess_answer = None

    if pe_version > 0 and pe_version in PE_CONFIGS:
        cfg = PE_CONFIGS[pe_version]
        sp_file = cfg["system_prompt_file"]
        if sp_file and sp_file.exists():
            system_prompt = sp_file.read_text(encoding="utf-8")
            print(f"[PE] Loaded system prompt: {sp_file.name}")
        fs_file = cfg["fewshot_file"]
        if fs_file and fs_file.exists():
            fewshot_examples = load_fewshot_examples(fs_file)
            print(f"[PE] Loaded {len(fewshot_examples)} few-shot examples")
        cot_file = cfg["cot_file"]
        if cot_file and cot_file.exists():
            cot_text = cot_file.read_text(encoding="utf-8")
            system_prompt = (system_prompt or "") + "\n\n" + cot_text
            add_cot = True
            print(f"[PE] Loaded CoT: {cot_file.name}")
        if cfg["postprocess"]:
            try:
                postprocess_answer = load_postprocessor()
                print(f"[PE] Loaded postprocessor")
            except Exception as e:
                print(f"[PE] WARNING: Failed to load postprocessor: {e}")

    # ── v8: Load RAG from pe.v8.rag_extended instead of pe.rag ──
    rag_collection = None
    if args.enable_rag:
        print(f"[RAG v8] Loading Milvus collection '{args.rag_collection}'...")
        try:
            from pe.v8.rag_extended import load_milvus, hybrid_search, format_context
            rag_collection = load_milvus(
                    db_uri=args.rag_db_path,
                    collection_name=args.rag_collection,
                )
            rag_hybrid_search = hybrid_search
            rag_format_context = format_context
            print(f"[RAG v8] Collection loaded. Entities: {rag_collection.num_entities}")
            if args.use_reranker:
                print(f"[RAG v8] Cross-Encoder reranker enabled")
            if args.use_query_rewrite:
                print(f"[RAG v8] Query rewrite enabled")
        except Exception as e:
            print(f"[RAG v8] WARNING: Failed to load RAG: {e}")
            rag_collection = None

    # Determine output file name (auto v8_ prefix)
    if args.output_name:
        output_name = args.output_name
    elif pe_version > 0:
        output_name = f"v8_pe_v{pe_version}.json"
    else:
        output_name = "v8_baseline.json"
    output_file = RESULTS_DIR / output_name

    print(f"=== RepoMind Benchmark v8 ===")
    print(f"Model: {args.model}")
    print(f"Scoring: {scoring_mode}")
    print(f"Context mode: {args.context_mode}")
    print(f"PE version: v{pe_version}")
    print(f"RAG: {'enabled' if args.enable_rag else 'disabled'}")
    if args.enable_rag:
        flags = []
        if args.adaptive_top_k:
            flags.append("adaptive")
        if args.use_bm25:
            flags.append("BM25")
        if args.use_mmr:
            flags.append("MMR")
        if args.use_reranker:
            flags.append("reranker")
        if args.use_query_rewrite:
            flags.append("rewrite")
        print(f"  RAG flags: {'+'.join(flags) if flags else 'none'}")
    print(f"Questions: {len(questions)}")
    print(f"Output: {output_file}")
    print()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    results = []
    category_scores = defaultdict(list)

    for i, q in enumerate(questions):
        qid = q["id"]
        cat = q["category"]
        print(f"[{i + 1}/{len(questions)}] {qid} ({cat})")

        # Step 1: Get model answer
        sys_content, user_content = build_answer_prompt(
            q, args.context_mode,
            system_prompt=system_prompt,
            fewshot_examples=fewshot_examples,
            add_cot=add_cot,
        )
        # RAG injection: append retrieved code context
        if rag_collection is not None:
            try:
                # ── v8: pass use_reranker and use_query_rewrite ──
                rag_chunks = rag_hybrid_search(
                    rag_collection, q["question"],
                    top_k=args.rag_top_k,
                    category=q.get("category"),
                    adaptive_top_k=args.adaptive_top_k,
                    use_mmr=args.use_mmr,
                    use_bm25=args.use_bm25,
                    use_reranker=args.use_reranker,
                    use_query_rewrite=args.use_query_rewrite,
                )
                if rag_chunks:
                    rag_context = rag_format_context(rag_chunks)
                    if rag_context:
                        user_content += (
                            "\n\n## 相关代码片段 (RAG 注入)\n"
                            "请参考以下与问题相关的 FastAPI 源码片段回答问题：\n"
                            f"{rag_context}"
                        )
                        print(f"  → RAG injected {len(rag_chunks)} chunks ({len(rag_context)} chars)")
            except Exception as e:
                print(f"  → RAG injection failed: {e}")
        print("  → Getting model answer...")
        try:
            if _use_local_model:
                model_answer = call_local_llm(None, args.model, system=sys_content, user=user_content, seed=args.seed)
            else:
                model_answer = call_llm(
                    client, args.model,
                    system=sys_content,
                    user=user_content,
                    seed=args.seed,
                )
        except Exception as e:
            print(f"  ✗ Failed to get answer: {e}")
            results.append({
                "id": qid,
                "category": cat,
                "question": q["question"],
                "model_answer": f"[ERROR: {e}]",
                "score": 0,
                "reason": f"API call failed: {e}",
            })
            category_scores[cat].append(0)
            time.sleep(0.5)
            continue

        # Step 1.5: Post-process answer (v4)
        if postprocess_answer:
            model_answer = postprocess_answer(model_answer)

        # Step 2: Judge the answer
        judge_prompt = judge_prompt_template.format(
            question=q["question"],
            ground_truth=q["ground_truth"],
            model_answer=model_answer,
        )
        print("  → Judging answer...")
        time.sleep(0.5)

        try:
            judge_raw = call_llm(
                client, args.model,
                system="你是一个严格的评测裁判。请严格按照要求的 JSON 格式输出评分结果。",
                user=judge_prompt,
                seed=args.seed,
            )
            judge_result = parse_judge_response(judge_raw)
        except Exception as e:
            print(f"  ✗ Judge failed: {e}")
            judge_result = {"score": 0, "reason": f"Judge API failed: {e}"}

        score = judge_result["score"]
        reason = judge_result["reason"]
        print(f"  ✓ Score: {score}/3 — {reason[:100]}")

        results.append({
            "id": qid,
            "category": cat,
            "question": q["question"],
            "model_answer": model_answer,
            "score": score,
            "reason": reason,
        })
        category_scores[cat].append(score)

        time.sleep(0.5)

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    total_score = sum(r["score"] for r in results)
    max_score = len(results) * 3
    avg_score = total_score / len(results) if results else 0

    category_summary = {}
    for cat, scores in sorted(category_scores.items()):
        cat_total = sum(scores)
        cat_max = len(scores) * 3
        cat_avg = cat_total / len(scores) if scores else 0
        category_summary[cat] = {
            "count": len(scores),
            "total": cat_total,
            "max": cat_max,
            "average": round(cat_avg, 2),
        }

    summary = {
        "model": args.model,
        "total_questions": len(results),
        "total_score": total_score,
        "max_score": max_score,
        "average_score": round(avg_score, 2),
        "by_category": category_summary,
    }

    output = {
        "summary": summary,
        "results": results,
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # Print summary
    print()
    print("=" * 50)
    print("BENCHMARK RESULTS (v8)")
    print("=" * 50)
    print(f"Model:       {args.model}")
    print(f"Questions:   {len(results)}")
    print(f"Total score: {total_score}/{max_score} (avg {avg_score:.2f}/3)")
    print()
    print("By category:")
    for cat, info in category_summary.items():
        print(f"  {cat:20s}  {info['total']}/{info['max']}  (avg {info['average']}/3, n={info['count']})")
    print()
    print(f"Results saved to: {output_file}")


if __name__ == "__main__":
    main()
