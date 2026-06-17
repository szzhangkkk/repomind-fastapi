"""Query rewrite module — expands short queries for better BGE vector retrieval.

Each rewrite appends category-specific natural-language cues to the query,
making it more descriptive for the embedding model without changing its intent.
"""

# ---------------------------------------------------------------------------
# Thresholds & templates
# ---------------------------------------------------------------------------
MIN_LEN_FOR_SKIP = 30  # queries longer than this are left unchanged

REWRITE_TEMPLATES = {
    "function_locate": "{query} 在哪个文件定义 参数列表和类型 属于哪个类",
    "call_chain": "{query} 完整的调用链步骤 从入口函数到最终处理",
    "cross_file_dep": "{query} 被哪些文件导入 所有导入语句",
    "impact_analysis": "{query} 影响范围有哪些 所有受影响的文件和函数",
}

# Keywords used to infer category when category is None
CATEGORY_KEYWORDS = {
    "call_chain": ["调用链", "调用链步骤", "调用流程", "call chain", "调用过程", "调用路径"],
    "cross_file_dep": ["依赖", "导入", "被哪些文件", "import", "跨文件", "依赖关系"],
    "function_locate": ["定义", "在哪里", "位于", "哪个文件", "defined", "located"],
    "impact_analysis": ["影响", "受影响", "影响范围", "impact", "改动影响"],
}


def _infer_category(query: str) -> str | None:
    """Infer the question category from keywords in the query.

    Returns the first matching category, or None if no keywords match.
    """
    q_lower = query.lower()
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in q_lower:
                return cat
    return None


def rewrite_query(query: str, category: str | None = None) -> str:
    """Rewrite a query for better BGE embedding retrieval.

    Args:
        query: The original user query string.
        category: Optional question category. One of:
            function_locate, call_chain, cross_file_dep, impact_analysis.
            If None, the category is inferred from keywords in the query.

    Returns:
        Rewritten query string (may be identical to input if >30 chars or
        no applicable rewrite rule).
    """
    # Rule 1: if query is already long enough, return as-is
    if len(query) > MIN_LEN_FOR_SKIP:
        return query

    # Rule 6: if category is None, try to infer from keywords
    if category is None:
        category = _infer_category(query)

    # If we have a category and a template for it, apply the rewrite
    if category and category in REWRITE_TEMPLATES:
        return REWRITE_TEMPLATES[category].format(query=query)

    # No applicable rewrite — return original
    return query
