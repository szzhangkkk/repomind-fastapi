# RepoMind 完整实验报告：DeepSeek + Qwen3B 消融矩阵

**日期**: 2026-06-15 ~ 06-16
**前置文档**: v7_report.md（DeepSeek RAG 维度）

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
| v1 | System Prompt | 55 | +3 |
| v2 | + Few-shot (20条) | 71 | +16 |
| v3 | + CoT 推理引导 | 74 | +3 |
| v4 | + 后处理 | 74 | 0 |
| **v5a** | **删误导规则 + 3次复测** | **85.7 ± 2.1** | **+11.7** |

### 2.2 RAG Pipeline（v6 → v7）

| 阶段 | 配置 | 分数 | 检索 R@10 |
|---|---|---|---|
| v6 ragonly | 单路向量 | 61.3 ± 2.1 | 0.11 |
| v6 perag | PE+向量RAG | 85.3 ± 2.5 | — |
| v7 perag_bm25 | PE+BM25+RRF | 89.3 ± 2.1 | 0.34 |
| **v7 perag_all** | **PE+BM25+RRF+MMR** | **91.7 ± 2.5** | 0.34 |
| v7min perag_bm25 | v7min索引(仅签名) | 90.0 ± 1.0 | **0.53** |

### 2.3 反直觉发现

- **v6 "PE+RAG+8分" 是单点噪声**，3次复测跌至 85.3
- **v7 min索引+MMR 跌 6.3 分**：召回精准时 diversity 是毒药
- **text_for_embedding 含源码拖低检索**：v7min(仅签名) R@10=0.53 vs v6(含源码) 0.34

---

## 3. Qwen3B 完整消融矩阵（8/8）

| 配置 | 总分 | call_chain | cross_file | func_locate | impact |
|---|---|---|---|---|---|
| baseline | **47** | 14/39 | 12/39 | 12/36 | 9/36 |
| ragonly | 46 | 14 | 11 | 12 | 9 |
| peonly | **59** | 15 | **22** | 9 | 13 |
| perag | 59 | 15 | 22 | 9 | 13 |
| ftonly | 51 | 14 | 13 | 13 | 11 |
| ft_rag | 52 | 15 | 13 | 13 | 11 |
| ft_pe | 46 | 15 | 13 | 10 | 8 |
| ft_all | 47 | 15 | 13 | 11 | 8 |

### 3.1 核心发现

| 发现 | 数据 |
|---|---|
| **PE 是唯一有效手段** | +12 分，cross_file 从 12→22 |
| **RAG 零效果** | 单独-1，叠加PE后0增益 |
| **FT 单独 +4** | 51 vs 47，但远不及 PE |
| **FT+PE 倒退** | 46 vs 59，FT学会的格式与PE v3冲突 |
| **3B 天花板 ~59** | 最优配置仅略超 DeepSeek baseline(52) |

---

## 4. 跨模型对照

| 配置 | DeepSeek | Qwen3B | Δ |
|---|---|---|---|
| baseline | 52 | 47 | -5 |
| PE only | **86** | 59 | -27 |
| RAG only | 61 | 46 | -15 |
| PE+RAG | **92** | 59 | -33 |
| FT only | — | 51 | — |
| FT+RAG | — | 52 | — |

**DeepSeek 全面碾压**。3B 小模型在代码分析任务上与 200B+ 大模型差距不可弥补——PE 只在 DeepSeek 上 +34，Qwen 上仅 +12。

---

## 5. 考核 4 项核对

| 维度 | 状态 | 证据 |
|---|---|---|
| ① 瓶颈诊断 | ✅ | baseline 52/47，低分题型分析 |
| ② PE 方案 | ✅ | 4 维量化 v0→v5a，独立效果数据 |
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
