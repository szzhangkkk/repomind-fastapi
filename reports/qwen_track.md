# RepoMind 完整实验报告：DeepSeek + Qwen3B 消融矩阵

**日期**: 2026-06-15 ~ 06-18
**前置文档**: v7_report.md（DeepSeek RAG 维度）
**数据清洗**: 2026-06-18 ground_truth 修复 11 条，全部重新打分

---

## 1. 实验环境

| | DeepSeek | Qwen3B |
|---|---|---|
| 模型 | deepseek-chat (API) | Qwen2.5-Coder-3B-Instruct (本地) |
| 硬件 | 云端 | RTX 4060 8GB + torch 2.5.1+cu121 |
| FT | ❌ API 不支持 | ✅ bitsandbytes 8bit + peft LoRA r=8 |
| SFT 数据 | — | 500 条 (train 400 / val 100) |
| Judge | DeepSeek API | DeepSeek API（本地模型只答题）|

---

## 2. DeepSeek 实验结果

### 2.1 PE 四维优化

| 阶段 | 配置 | 分数 | 增量 |
|---|---|---|---|
| v0 | baseline（无优化）| 52 | — |
| v1 | System Prompt | 54 | +2 |
| v2 | + Few-shot (20条) | 73 | +21 |
| v3 | + CoT 推理引导 | 72 | +20 |
| v4 | + 后处理 | 72 | +20 |
| **v6** | **删误导规则 + 3次复测** | **88.0 ± 1.0** | **+36.0** |

### 2.2 RAG Pipeline（v6 → v7）

| 阶段 | 配置 | 分数 | 检索 R@10 |
|---|---|---|---|
| v6 ragonly | 单路向量 | 59.0 ± 1.7 | 0.11 |
| v6 perag | PE+向量RAG | 85.7 ± 3.5 | — |
| **v7 perag_bm25** | **PE+BM25+RRF** | **92.0 ± 3.0** 🔴 **SOTA** | 0.34 |
| v7 perag_all | PE+BM25+RRF+MMR | 88.7 ± 1.5 | 0.34 |
| v7min perag_bm25 | v7min索引(仅签名) | 83.0 | **0.53** |

### 2.3 反直觉发现

- **text_for_embedding 含源码拖低检索**：v7min(仅签名) R@10=0.53 vs v6(含源码) 0.34
- **MMR效果逆转**: v7索引 MMR 拖低 3.3 (92.0→88.7), v7min索引 MMR 反升 5.0 (83.0→88.0) — 检索不准时多样性有价值
- **检索最优(0.53) ≠ 端到端最优(92.0)**: BM25+RRF 不加MMR反而端到端最高

---

## 3. Qwen3B 完整消融矩阵（8/8）

| 配置 | 总分 | call_chain | cross_file | func_locate | impact |
|---|---|---|---|---|---|
| baseline | **45** | 13/39 | 11/39 | 12/36 | 9/36 |
| ragonly | 45 | 13 | 11 | 12 | 9 |
| peonly | **58** | 14 | **22** | 9 | 13 |
| perag | 58 | 14 | 22 | 9 | 13 |
| ftonly | 51 | 14 | 13 | 13 | 11 |
| ft_rag | 50 | 13 | 13 | 13 | 11 |
| ft_pe | 45 | 14 | 13 | 10 | 8 |
| ft_all | 45 | 14 | 13 | 10 | 8 |

### 3.1 核心发现

| 发现 | 数据 |
|---|---|
| **PE 是唯一有效手段** | +13 分，cross_file 从 11→22 |
| **RAG 零效果** | 单独 0，叠加PE后 0 增益 |
| **FT 单独 +6** | 51 vs 45，但远不及 PE |
| **FT+PE 倒退** | 45 vs 58，FT学会的格式与PE v3冲突 |
| **3B 天花板 ~58** | 最优配置略超 DeepSeek baseline(52) |

---

## 4. 跨模型对照

| 配置 | DeepSeek | Qwen3B | Δ |
|---|---|---|---|
| baseline | 52 | 45 | -7 |
| PE only | **88.0** | 58 | -30 |
| RAG only | 59 | 45 | -14 |
| PE+RAG | **92.0** | 58 | -34 |
| FT only | — | 51 | — |
| FT+RAG | — | 50 | — |

**DeepSeek 全面碾压**。3B 小模型在代码分析任务上与 200B+ 大模型差距不可弥补——PE 只在 DeepSeek 上 +36，Qwen 上仅 +13。

---

## 5. 考核 4 项核对

| 维度 | 状态 | 证据 |
|---|---|---|
| ① 瓶颈诊断 | ✅ | baseline 52/45，低分题型分析 |
| ② PE 方案 | ✅ | 4 维量化 v0→v6，独立效果数据 |
| ③ RAG Pipeline | ✅ | 完整 hybrid + Recall@K/MRR |
| ④ 消融矩阵 | ✅ | PE/RAG/FT 全维度 8 配置跑完 |

---

## 6. 文件清单

- `reports/v7_report.md` — DeepSeek RAG 详细报告
- `reports/qwen_track.md` — 本文件
- `data/sft_train.jsonl` — SFT 训练集 400 条
- `data/sft_val.jsonl` — SFT 验证集 100 条
- `models/qwen2.5-coder-3b-repomind-lora-v2/` — LoRA adapter + merged
- `benchmark/results/qwen_*` — 8 个 Qwen 实验结果
- `benchmark/results/pe_v7_perag_all_s*.json` — DeepSeek SOTA
