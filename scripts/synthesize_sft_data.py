"""Synthesize SFT training data from benchmark questions using DeepSeek API.

For each benchmark question, generates N SFT variants to improve model's
FastAPI code analysis capability.

Output:
  data/sft_train.jsonl  (train_ratio of total, default 80%)
  data/sft_val.jsonl    (1 - train_ratio, default 20%)

Instruction field uses PE v3 system prompt (pe/v1_system.txt + pe/v3_cot.txt).
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def call_llm(client, model: str, system: str, user: str, max_retries: int = 3, seed: int = 42) -> str:
    """Call DeepSeek API with retry logic. temperature=0.7 for diversity."""
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
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


def _get_variant_instruction(idx: int) -> str:
    """Return a varied instruction based on variant index for diversity."""
    instructions = [
        "使用不同的措辞重新组织推理和答案，举一个不同的源码调用链例子。",
        "从另一个分析角度出发，关注错误处理和边界条件，然后给出答案。",
        "换一种解释方式，用更通俗的语言先解释原理，再给出精确的源代码定位。",
        "假设读者是初级 Python 开发者，先解释相关概念，再给出精确的调用链。",
        "从性能角度分析，先讨论代码执行路径，再列出函数调用链。",
        "用对比的方式，将相关功能与其他类似函数比较，再给出答案。",
        "从源码阅读的角度，先描述如何找到相关代码，再列出具体调用链。",
        "采用逆向思维：先列出可能的调用链，再逐一验证每个函数的作用。",
        "从设计模式角度分析，先说明涉及的架构模式，再映射到具体函数调用。",
        "用问答对话风格，先自问自答几个关键点，再给出最终答案。",
    ]
    return instructions[idx % len(instructions)]


def _load_pe_v3_system() -> str:
    """Load PE v3 system prompt = pe/v1_system.txt + \\n\\n + pe/v3_cot.txt."""
    v1_path = PROJECT_ROOT / "pe" / "v1_system.txt"
    v3_path = PROJECT_ROOT / "pe" / "v3_cot.txt"
    v1 = v1_path.read_text(encoding="utf-8").strip()
    v3 = v3_path.read_text(encoding="utf-8").strip()
    return v1 + "\n\n" + v3


def _build_generation_system() -> str:
    """Build system prompt for the LLM during SFT data generation.

    Tells DeepSeek to produce a *new training example* with the same category
    format, rather than answering the question directly.
    """
    return (
        "你是一个 FastAPI 源码分析专家。请根据以下题目和参考答案，生成一条**同题型、同格式**的新训练数据。"
        "要求：\n"
        "1. 严格使用题目对应题型的输出格式（call_chain用步骤格式、cross_file_dep用列表格式、function_locate用字段格式、impact_analysis用列表格式）\n"
        "2. 先输出推理过程（## 推理步骤），再输出最终答案\n"
        "3. 文件名、行号必须与原参考答案一致（可补充额外信息但不可编造）\n"
        "4. 使用不同的措辞和推理角度"
    )


def stratified_split(entries, train_ratio, seed=42):
    """Split entries by category-stratified sampling.

    Ensures each category appears in both train and val splits,
    handling edge cases where a category has very few entries.
    """
    by_category = {}
    for e in entries:
        by_category.setdefault(e["category"], []).append(e)

    rng = random.Random(seed)
    train_entries = []
    val_entries = []

    for cat, cat_entries in sorted(by_category.items()):
        rng.shuffle(cat_entries)
        n = len(cat_entries)
        split_idx = max(1, min(n - 1, int(n * train_ratio)))
        # If train_ratio gives all entries to one split, force at least 1 in each
        if n >= 2:
            split_idx = max(1, min(n - 1, split_idx))
        train_entries.extend(cat_entries[:split_idx])
        val_entries.extend(cat_entries[split_idx:])

    return train_entries, val_entries


def main():
    parser = argparse.ArgumentParser(description="Synthesize SFT data from benchmark questions")
    parser.add_argument("--limit", type=int, default=500, help="Total SFT entries to generate (default: 500)")
    parser.add_argument("--variants", type=int, default=10, help="Variants per question (default: 10)")
    parser.add_argument("--train-ratio", type=float, default=0.8, help="Train split ratio (default: 0.8)")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory (default: data/)")
    args = parser.parse_args()

    if not (0 < args.train_ratio < 1):
        print("Error: --train-ratio must be between 0 and 1")
        sys.exit(1)

    sys.path.insert(0, str(PROJECT_ROOT))
    from config.load_config import get_deepseek_client

    client = get_deepseek_client()
    model_name = "deepseek-chat"

    # Paths
    questions_file = PROJECT_ROOT / "benchmark" / "questions.jsonl"
    output_dir = Path(args.output_dir) if args.output_dir else PROJECT_ROOT / "data"
    output_dir.mkdir(parents=True, exist_ok=True)

    train_file = output_dir / "sft_train.jsonl"
    val_file = output_dir / "sft_val.jsonl"
    temp_file = output_dir / "sft_temp.jsonl"

    # === PE v3 system prompt = instruction field ===
    instruction_prompt = _load_pe_v3_system()

    # === Generation system prompt (tells DeepSeek what to produce) ===
    generation_system = _build_generation_system()

    # Load questions
    questions = []
    with open(questions_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))

    print(f"Loaded {len(questions)} questions from {questions_file}")
    print(f"Target: {args.limit} SFT entries ({args.variants} variants per question)")
    print(f"Train ratio: {args.train_ratio}")
    print()

    # Clear temp file from any previous run
    if temp_file.exists():
        temp_file.unlink()

    total_generated = 0
    all_entries = []

    for q_idx, q in enumerate(questions):
        if total_generated >= args.limit:
            break

        question_text = q["question"]
        ground_truth = q.get("ground_truth", "")
        snippet = q.get("context_snippet", "")
        category = q.get("category", "unknown")

        # Generate N variants for this question
        variants_remaining = min(args.variants, args.limit - total_generated)
        for v_idx in range(variants_remaining):
            print(f"[Q{q_idx + 1}/{len(questions)}][V{v_idx + 1}/{variants_remaining}] {category}")

            # Build user prompt with question details
            user_prompt_parts = [
                f"## 原始题目\n{question_text}",
                f"## 参考答案\n{ground_truth}",
            ]
            if snippet:
                user_prompt_parts.append(
                    f"## 源码片段\n```python\n{snippet}\n```"
                )
            user_prompt_parts.append(
                f"\n请生成第 {v_idx + 1} 条变体。\n"
                f"要求：{_get_variant_instruction(v_idx)}"
            )
            user_prompt = "\n\n".join(user_prompt_parts)

            try:
                answer = call_llm(
                    client, model_name,
                    system=generation_system,
                    user=user_prompt,
                    seed=42 + q_idx * 100 + v_idx,
                )

                entry = {
                    "instruction": instruction_prompt,
                    "input": question_text,
                    "output": answer,
                    "category": category,
                }
                all_entries.append(entry)
                total_generated += 1
                print(f"  + Generated ({total_generated}/{args.limit})")

                # Real-time flush to temp file (in case of crash)
                with open(temp_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            except Exception as e:
                print(f"  x Failed: {e}")
                continue

            time.sleep(0.5)  # Rate limit

    # === Stratified Train / Val split ===
    train_entries, val_entries = stratified_split(all_entries, args.train_ratio)

    # Write train (strip category from output)
    with open(train_file, "w", encoding="utf-8") as f:
        for e in train_entries:
            entry_out = {k: v for k, v in e.items() if k != "category"}
            f.write(json.dumps(entry_out, ensure_ascii=False) + "\n")

    # Write val
    with open(val_file, "w", encoding="utf-8") as f:
        for e in val_entries:
            entry_out = {k: v for k, v in e.items() if k != "category"}
            f.write(json.dumps(entry_out, ensure_ascii=False) + "\n")

    # Clean up temp file
    if temp_file.exists():
        temp_file.unlink()

    # === Report ===
    print(f"\n{'=' * 60}")
    print(f"Generation complete!")
    print(f"{'=' * 60}")
    print(f"Train: {len(train_entries)} entries -> {train_file}")
    print(f"Val:   {len(val_entries)} entries -> {val_file}")
    print()

    # Category distribution
    print("--- Category Distribution ---")
    all_cats = {}
    train_cats = {}
    val_cats = {}
    for e in all_entries:
        all_cats[e["category"]] = all_cats.get(e["category"], 0) + 1
    for e in train_entries:
        train_cats[e["category"]] = train_cats.get(e["category"], 0) + 1
    for e in val_entries:
        val_cats[e["category"]] = val_cats.get(e["category"], 0) + 1

    print(f"{'Category':<20} {'Total':<8} {'Train':<8} {'Val':<8}")
    print("-" * 44)
    for c in sorted(all_cats):
        print(f"{c:<20} {all_cats[c]:<8} {train_cats.get(c, 0):<8} {val_cats.get(c, 0):<8}")

    # Random samples
    print()
    print("--- Random Samples ---")
    rng = random.Random(42)
    samples = rng.sample(all_entries, min(3, len(all_entries)))
    for i, e in enumerate(samples):
        print(f"\n{'=' * 60}")
        print(f"SAMPLE {i + 1}")
        print(f"{'=' * 60}")
        print(f"Category: {e['category']}")
        print(f"--- instruction (first 150 chars) ---")
        print(e["instruction"][:150] + "...")
        print(f"--- input (first 150 chars) ---")
        inp = e["input"]
        print(inp[:150] + "..." if len(inp) > 150 else inp)
        print(f"--- output (first 300 chars) ---")
        out = e["output"]
        print(out[:300] + "..." if len(out) > 300 else out)

    # Instruction validation
    print()
    print("--- Instruction Validation ---")
    keyword = "FastAPI 源码调用链分析专家"
    all_ok = True
    for i, e in enumerate(all_entries):
        if keyword not in e["instruction"]:
            print(f"  ERROR: entry {i} instruction missing '{keyword}'")
            all_ok = False
    if all_ok:
        print(f"  [PASS] All {len(all_entries)} instructions contain '{keyword}'")

    # Verify non-empty input/output
    empty_input = [e for e in all_entries if not e["input"]]
    empty_output = [e for e in all_entries if not e["output"]]
    if empty_input:
        print(f"  WARNING: {len(empty_input)} entries have empty input")
    if empty_output:
        print(f"  WARNING: {len(empty_output)} entries have empty output")
    if not empty_input and not empty_output:
        print("  [PASS] all entries have non-empty input/output")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
