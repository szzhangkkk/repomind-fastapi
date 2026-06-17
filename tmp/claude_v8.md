请创建 5 个新文件，不要改任何已有文件（所有已有文件都是"已完成"的基线，不能碰）：

## 背景

项目 `/mnt/d/repomind-fastapi/` 的 RAG Pipeline 位于 `pe/rag.py`（465行），核心流程：
```
query → BGE embedding → BM25+向量并行检索 → RRF融合 → (可选MMR) → top_k截断 → format_context
```

当前基线（v7min索引 + adaptive + BM25）：R@10=0.530, MRR@10=0.285
问题：BM25 能召回但排序不准（MRR 低），query 原样扔给 embedding 无改写。

## 任务 1：pe/v8/reranker.py

创建 Cross-Encoder reranker 模块。

```python
# 要求：
# - 使用 sentence_transformers.CrossEncoder
# - 模型：BAAI/bge-reranker-v2-m3（从 pe/model_cache/ 加载，如果不存在则下载）
# - cached singleton 模式（参考 pe/rag.py 的 _get_embedding_model()）
# - 函数签名：rerank(query: str, chunks: list[dict], top_k: int) -> list[dict]
# - 输入每个 chunk 有 'source' 字段，构造 (query, chunk['source']) pair
# - 按 reranker 分数降序，取 top_k，chunk 添加 'rerank_score' 字段
# - batch inference 一次处理所有 pair
```

## 任务 2：pe/v8/query_rewrite.py

创建 query rewrite 模块。

```python
# 函数签名：rewrite_query(query: str, category: str | None = None) -> str
# 
# 规则（按顺序）：
# 1. 如果 query 已有 >30 字符，直接返回不做改写
# 2. function_locate 题型：query + " 在哪个文件定义 参数列表和类型 属于哪个类"
# 3. call_chain 题型：query + " 完整的调用链步骤 从入口函数到最终处理"
# 4. cross_file_dep 题型：query + " 被哪些文件导入 所有导入语句"
# 5. impact_analysis 题型：query + " 影响范围有哪些 所有受影响的文件和函数"
# 6. category 为 None 时：检测 query 是否含"调用链/依赖/定义/影响"关键词来推断
# 
# 改写后的 query 更接近自然语言，利于 BGE 向量检索
```

## 任务 3：pe/v8/rag_extended.py

创建包装层，import pe.rag 的已有函数，在外层添加 reranker 和 rewrite。

```python
from pe.rag import hybrid_search as _base_hybrid_search
from pe.rag import load_milvus, format_context  # 直接透传

def hybrid_search(collection, query, top_k=10, category=None,
                  adaptive_top_k=False, use_mmr=False, use_bm25=False,
                  use_reranker=False, use_query_rewrite=False,
                  file_hint=None):
    """
    Extended hybrid_search with optional reranker and query rewrite.
    
    调用流程：
    1. (可选) query_rewrite: rewrite_query(query, category)
    2. _base_hybrid_search(...)  — 走的仍然是 pe.rag 原始逻辑
       注意：当 use_reranker=True 时，top_k 需要倍增传给 base 以留候选
       （取 top_k * 3，reranker 从候选中精选 top_k）
    3. (可选) reranker: rerank(query, chunks, top_k)
    """
    # 实现细节：
    # - use_query_rewrite 时调用 pe.v8.query_rewrite.rewrite_query
    # - 传给 _base_hybrid_search 的 top_k 参数：
    #   如果 use_reranker → top_k * 3（多取候选）
    #   否则 → top_k
    # - use_reranker 时调用 pe.v8.reranker.rerank
    # - 其他参数（adaptive_top_k, use_mmr, use_bm25, file_hint）原样透传给 _base_hybrid_search
```

## 任务 4：pe/v8/eval_retrieval.py

创建 v8 专属的检索评估脚本。

```python
# 结构参考 pe/eval_retrieval.py，但：
# - import pe.v8.rag_extended 而非 pe.rag
# - 只测 v7min 索引（str(PE_DIR / "milvus_lite_v7min.db")）
# - CONFIGS 列表包含：
#   1. "v7_baseline"       — adaptive + BM25（当前基线）
#   2. "v8_rerank"         — adaptive + BM25 + reranker
#   3. "v8_rewrite"        — adaptive + BM25 + query_rewrite
#   4. "v8_rerank_rewrite" — adaptive + BM25 + reranker + query_rewrite
# - 输出表对比这 4 个配置的 R@5 / R@10 / MRR@10
# - 结果存到 pe/v8/v8_retrieval_report.json
```

## 任务 5：benchmark/run_eval_v8.py

Fork `benchmark/run_eval.py`，只改 RAG 加载部分：

```python
# 改动清单（只改 3 处）：
# 1. 约第 409 行：from pe.rag import ...  →  from pe.v8.rag_extended import ...
# 2. 增加两个 CLI 参数：
#    parser.add_argument("--use-reranker", action="store_true")
#    parser.add_argument("--use-query-rewrite", action="store_true")
# 3. 约第 459-466 行 hybrid_search() 调用：传入 use_reranker 和 use_query_rewrite
# 
# 其余代码（PE 加载、judge、输出格式）完全不变
# 默认输出文件名自动带 v8 前缀
```

## 约束（极其重要）

- **不碰任何已有文件**：pe/rag.py, pe/eval_retrieval.py, pe/build_index_v7min.py, benchmark/run_eval.py, pe/v1_system.txt, pe/v3_cot.txt, pe/fewshot_examples.jsonl, pe/v4_postprocess.py
- **只有新建**：上述 5 个文件全是新建
- **pe/rag.py 的已有行为不受影响**：rag_extended.py 只 import 不修改
- **不要跑**任何评测脚本（太耗时），只写代码
- **不要跑** pip install / 下载模型（用户自己跑）
- **不要 rebuild** 任何索引

## 完成后告诉我

- [ ] 你创建的 5 个文件路径
- [ ] 每个文件的关键函数/类名
- [ ] 确认没有改任何已有文件（给出你检查过的已有文件列表）
- [ ] 验证：`python3 -c "from pe.v8.rag_extended import hybrid_search, rerank, rewrite_query; print('import OK')"` 能通过
- [ ] 任何"附带创建"的文件都必须明确列出
