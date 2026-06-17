"""PE v4: Post-processing rules for model answers before judge evaluation.

Usage:
    from pe.v4_postprocess import postprocess_answer
    cleaned = postprocess_answer(raw_answer)
"""

import re


def postprocess_answer(answer: str) -> str:
    """Clean and normalize model answer before sending to judge.

    Rules:
    1. Strip markdown explanation blocks (## headers, bullet lists, bold text)
    2. Normalize function reference format to func_name()(file:line)
    3. Remove duplicate lines
    4. Mark uncertain language
    """
    if not answer:
        return answer

    lines = answer.strip().split("\n")
    cleaned = []

    for line in lines:
        stripped = line.strip()

        # Rule 1: Skip markdown headers
        if stripped.startswith("#"):
            continue

        # Rule 1: Skip markdown bold explanation lines (standalone bold = section header)
        if re.match(r"^\*\*[^*]+\*\*\s*$", stripped):
            continue

        # Rule 1: Skip blank lines
        if not stripped:
            continue

        # Rule 1: Skip bullet-point markers (keep content)
        if stripped.startswith("- "):
            stripped = stripped[2:]
        elif stripped.startswith("* "):
            stripped = stripped[2:]

        # Rule 1: Remove inline bold markers
        stripped = stripped.replace("**", "")

        # Rule 2: Normalize function reference format
        # Pattern: `func_name` (file.py, line XX) → func_name()(file.py:XX)
        # Pattern: func_name() 在 file.py 第XX行 → func_name()(file.py:XX)
        stripped = _normalize_func_refs(stripped)

        # Rule 3: Dedup - skip if this exact line already added
        if stripped not in cleaned:
            cleaned.append(stripped)

    result = "\n".join(cleaned)

    # Rule 4: Mark uncertain language
    uncertain_markers = ["可能", "也许", "不确定", "或许", "大概", "应该"]
    for marker in uncertain_markers:
        if marker in result:
            # Prepend uncertainty warning if not already present
            if not result.startswith("[不确定]"):
                result = "[不确定] " + result
            break

    return result


def _normalize_func_refs(line: str) -> str:
    """Normalize various function reference formats to standard form.

    Target: func_name()(file.py:行号)
    """
    # Pattern: `func_name` (file.py, line 123)
    line = re.sub(
        r"`(\w+)`\s*\((\w+\.py),\s*(?:line|行)\s*(\d+)\)",
        r"\1()(\2:\3)",
        line,
    )

    # Pattern: func_name 在 file.py 第123行
    line = re.sub(
        r"(\w+)\s+在\s+(\w+\.py)\s+第(\d+)行",
        r"\1()(\2:\3)",
        line,
    )

    # Pattern: func_name()（file.py:123）(fullwidth parens)
    line = re.sub(
        r"(\w+)\(\)（(\w+\.py):(\d+)）",
        r"\1()(\2:\3)",
        line,
    )

    return line


if __name__ == "__main__":
    # Quick self-test
    test1 = "## 调用链分析\n\n步骤1: `get_path_param_names` (utils.py, line 59)\n步骤2: get_typed_signature()(dependencies/utils.py:223)\n\n**总结**: 以上是调用链。"
    print("Test 1:")
    print(postprocess_answer(test1))
    print()

    test2 = "该函数可能在 routing.py 第403行被调用。也许还影响了其他文件。"
    print("Test 2:")
    print(postprocess_answer(test2))
    print()

    test3 = "- 步骤1: 调用 func_a()(a.py:10)\n- 步骤2: 调用 func_b()(b.py:20)\n- 步骤1: 调用 func_a()(a.py:10)"
    print("Test 3 (dedup):")
    print(postprocess_answer(test3))
