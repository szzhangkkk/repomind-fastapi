# RepoMind — 基于 PE/RAG/FT 的代码分析领域效果优化

> 对 FastAPI 源码核心模块(44个.py)构建 50 条评测用例，系统性优化 Prompt Engineering、RAG Pipeline、模型微调(FT)，完成完整消融矩阵。

## 环境

```bash
# Python 3.12 + venv
cd /mnt/d/repomind-fastapi
uv venv .venv-ft --python 3.12
source .venv-ft/bin/activate

# CUDA + PyTorch (RTX 4060, driver 577.02)
pip install torch==2.5.1+cu121 --index-url https://download.pytorch.org/whl/cu121

# 依赖 (用清华源)
pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  transformers==4.48.3 peft bitsandbytes datasets accelerate trl==0.15.2 \
  pymilvus sentence-transformers openai python-dotenv

# 配置文件
cp config/.env.example config/.env  # 填入 DEEPSEEK_API_KEY
```

## 项目结构

```
repomind-fastapi/
├── benchmark/
│   ├── questions.jsonl          # 50 条评测题 (call_chain/cross_file_dep/function_locate/impact_analysis)
│   ├── run_eval.py              # 端到端评测 (支持 DeepSeek API + 本地模型)
│   ├── rejudge.py               # 批量重打分 (基于新 ground_truth 重评旧结果)
│   ├── judge_prompts/           # LLM-as-Judge 评分规则
│   └── results/                 # 所有实验结果
├── pe/
│   ├── v1_system.txt            # System Prompt 角色+格式 (40行)
│   ├── v3_cot.txt               # CoT 推理引导
│   ├── fewshot_examples.jsonl   # 20 条 Few-shot 示例
│   ├── v4_postprocess.py        # 后处理规则
│   ├── rag.py                   # Hybrid RAG (BM25+Vector+RRF+MMR)
│   ├── build_index.py           # v6 索引构建 (text含源码)
│   ├── build_index_v7min.py     # v7min 索引构建 (仅签名)
│   ├── eval_retrieval.py        # 检索精度评估 (Recall@K/MRR)
│   └── milvus_lite*.db          # Milvus Lite 向量索引
├── scripts/
│   ├── synthesize_sft_data.py   # SFT 数据合成 (DeepSeek API生成)
│   ├── train_qlora.py           # QLoRA 训练 (bitsandbytes+peft)
│   ├── qwen_final.sh            # Qwen 消融实验脚本
│   └── qwen_missing.sh          # Qwen 补充实验脚本
├── data/
│   ├── sft_train.jsonl          # SFT 训练集 (400条)
│   └── sft_val.jsonl            # SFT 验证集 (100条)
├── models/
│   ├── qwen2.5-coder-3b-instruct/   # 基座模型缓存
│   └── qwen2.5-coder-3b-repomind-lora-v2/  # LoRA adapter + merged
├── reports/
│   ├── v7_report.md             # DeepSeek RAG 详细报告
│   └── qwen_track.md            # Qwen3B 完整消融报告
└── fastapi/                     # FastAPI 源码 (44 .py)
```

## 快速开始

### 1. 评测 DeepSeek (API)

```bash
# baseline (无优化)
python benchmark/run_eval.py --seed 42

# PE v3 (System Prompt + Few-shot + CoT + 后处理)
python benchmark/run_eval.py --pe-version 3 --seed 42

# PE + RAG hybrid (SOTA = BM25+RRF, 不加MMR)
python benchmark/run_eval.py --pe-version 3 --enable-rag \
  --adaptive-top-k --use-bm25 --seed 42
```

### 2. 检索精度评估

```bash
python pe/eval_retrieval.py  # Recall@5/10 + MRR@10
```

### 3. SFT 数据生成

```bash
python scripts/synthesize_sft_data.py --limit 500 --variants 10 --train-ratio 0.8
# 输出: data/sft_train.jsonl (400条) + data/sft_val.jsonl (100条)
```

### 4. QLoRA 微调 (Qwen2.5-Coder-3B)

```bash
HF_ENDPOINT=https://hf-mirror.com python scripts/train_qlora.py \
  --data-path data/sft_train.jsonl \
  --val-data-path data/sft_val.jsonl \
  --model-dir models/qwen2.5-coder-3b-instruct \
  --lora-dir models/qwen2.5-coder-3b-repomind-lora-v2 \
  --epochs 3 --batch-size 1 --lr 2e-4
# 训练完自动保存 LoRA adapter + merged 模型
# 输出 train/val loss 对比 (监控过拟合)
```

### 5. Qwen3B 本地评测

```bash
# baseline (基座模型)
HF_ENDPOINT=https://hf-mirror.com python benchmark/run_eval.py \
  --local-model models/qwen2.5-coder-3b-instruct/.../snapshots/<hash> \
  --seed 42 --output-name qwen_baseline

# FT 模型
HF_ENDPOINT=https://hf-mirror.com python benchmark/run_eval.py \
  --local-model models/qwen2.5-coder-3b-repomind-lora-v2/merged \
  --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr \
  --seed 42 --output-name qwen_ft_all
# (judge 始终用 DeepSeek API，本地模型只答题)
```

### 6. 完整消融实验 (Qwen)

```bash
bash scripts/qwen_final.sh    # baseline/perag/ftonly/ft_all
bash scripts/qwen_missing.sh  # peonly/ragonly/ft_pe/ft_rag
```

## 主要结果

> **2026-06-18 数据清洗**: ground_truth 修复 11 条（清除泄漏的 docstring/功能描述），全部结果重新打分。
> 分数波动 ±1-4 分，消融差值不变，结论一致。

### DeepSeek PE 四维优化 (temperature=0, seed复测)

| 阶段 | 配置 | 分数 | 增量 |
|---|---|---|---|
| v0 | baseline（无优化）| 52 | — |
| v1 | + System Prompt（角色+格式）| 54 | +2 |
| v2 | + Few-shot (20条) | 73 | +21 |
| v3 | + CoT 推理引导 | 72 | +20 |
| v4 | + 后处理 | 72 | +20 |
| **v6** | **删误导规则 + 3次seed复测** | **88.0 ± 1.0** | **+36.0** |

### DeepSeek RAG 消融矩阵 (3-seed mean±std, v7min索引)

| 配置 | 分数 | 检索 R@10 |
|---|---|---|
| baseline | 52 | — |
| PE only | 88.0 ± 1.0 | — |
| RAG only (v6 单向量) | 59.0 ± 1.7 | 0.11 |
| RAG only (v7 BM25+RRF) | 65.7 ± 5.9 | 0.33 |
| PE+RAG v6 (向量) | 85.7 ± 3.5 | — |
| **PE+RAG v7 (BM25+RRF)** | **92.0 ± 3.0** 🔴 **SOTA** | 0.33 |
| PE+RAG v7 (BM25+RRF+MMR) | 88.7 ± 1.5 | 0.33 |
| v7min (仅签名) PE+RAG (BM25+RRF) | 83.0 | **0.52** |
| v7min (仅签名) PE+RAG (BM25+RRF+MMR) | 88.0 | 0.52 |

### 检索精度对比 (4 config × 2 index)

| 配置 | v6索引 R@10 | v7min索引 R@10 | v7min MRR@10 |
|---|---|---|---|
| 纯向量 | 0.11 | 0.01 | 0.01 |
| 向量+自适应top_k | 0.07 | 0.01 | 0.01 |
| +BM25 | 0.33 | **0.52** | 0.29 |
| +BM25+MMR | 0.33 | 0.52 | 0.23 |

> **反直觉发现**:
> - **text_for_embedding**: v7min(仅签名) R@10=0.52 碾压 v6(含源码) R@10=0.33
> - **检索最优 ≠ 端到端最优**: v7min R@10=0.52 但端到端仅 83, v7 R@10=0.33 但端到端 92 — 召回太准时 MMR 反而是毒药
> - **MMR 双刃剑（新结论）**: v7 索引上 MMR 拖低端到端 -3.3 (92.0→88.7), v7min 索引上 MMR 反而提升 +5.0 (83.0→88.0) — 检索不准时多样性有价值，检索准了别加

### Qwen3B 完整消融矩阵 (8/8, Qwen2.5-Coder-3B, QLoRA 8bit)

| 配置 | 总分 | call_chain | cross_file | func_locate | impact |
|---|---|---|---|---|---|
| baseline | **45** | 13/39 | 11/39 | 12/36 | 9/36 |
| ragonly | 45 | 13 | 11 | 12 | 9 |
| **peonly** | **58** | 14 | **22** | 9 | 13 |
| perag | 58 | 14 | 22 | 9 | 13 |
| ftonly | 51 | 14 | 13 | 13 | 11 |
| ft_rag | 50 | 13 | 13 | 13 | 11 |
| ft_pe | 45 | 14 | 13 | 10 | 8 |
| ft_all | 45 | 14 | 13 | 10 | 8 |

> PE 唯一有效 (+13), RAG 零效果, FT+PE 倒退 (FT格式与PE冲突), 3B天花板~58。

### 跨模型对照

| 配置 | DeepSeek | Qwen3B |
|---|---|---|
| baseline | 52 | 45 |
| PE only | **88.0** | 58 |
| RAG only | 65.7 | 45 |
| PE+RAG | **92.0** | 58 |
| FT only | — | 51 |
| FT+PE | — | 45 |
| FT+RAG | — | 50 |
| FT+PE+RAG | — | 45 |

### v8 改进实验 (2026-06-17, 负面/混合结论)

| 实验 | 分数 | vs v7 baseline (92) |
|---|---|---|
| query rewrite | 78 | -14 🔴 |
| Cross-Encoder reranker | 88 | -4 |
| reranker + rewrite | 89 | -3 |

> **教训**: 代码RAG的query越精确越好——自然语言扩展稀释函数名匹配。v7 hybrid (BM25+RRF, 92分) 维持当前最优。reranker 单独用有小幅提升但不及 SOTA。v8代码保留在 `pe/v8/`。

## 考核核对

| 维度 | 状态 |
|---|---|
| ① 瓶颈诊断 (50题, 4题型) | ✅ |
| ② PE 四维量化 (v0→v6) | ✅ |
| ③ RAG Pipeline + Recall@K/MRR | ✅ |
| ④ FT ≥500条 + 过拟合监控 | ✅ |
| ⑤ 消融矩阵 8 配置 | ✅ |
| ⑥ 可复现性 | ✅ (本 README) |

## 报告

- **`reports/final_report.md`** — 最终综合报告 (包含所有 DeepSeek + Qwen 结果、消融矩阵、考核核对)
- 历史报告: `reports/v7_report.md` (RAG 详细), `reports/qwen_track.md` (FT 详细)
