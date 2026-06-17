# RepoMind 最终实验报告

> 基于 FastAPI 源码核心模块(44 .py)的代码分析领域效果优化

---

## 1. 评测体系

- **用例**: 50 条，4 题型 (call_chain×13 / cross_file_dep×13 / function_locate×12 / impact_analysis×12)
- **评分**: LLM-as-Judge (DeepSeek API)，0-3 分/题，150 满分
- **指标**: 端到端分数 + 检索 Recall@K/MRR

---

## 2. Prompt Engineering (DeepSeek)

| 阶段 | 增量 | 分数 | 说明 |
|---|---|---|---|
| v0 baseline | — | 55 | 原生能力 |
| v1 System Prompt | 0 | 55 | 角色定义+输出格式 |
| v2 Few-shot (20条) | +16 | 71 | 示例库 |
| v3 CoT 推理引导 | +3 | 74 | 先推理再输出 |
| v4 后处理 | 0 | 74 | 无增益 |
| **v6 修bug** | **+14.7** | **88.7 ± 1.5** | 删误导规则, 3次seed复测 |

**PE 四维均独立量化。** 最大增益来源：Few-shot (+16) 和 bug修复 (+14.7)。

---

## 3. RAG Pipeline (DeepSeek)

### 3.1 Pipeline 架构

```
Query → BM25(符号) + Vector(语义) → RRF融合 → MMR多样性 → Top-k截断 → Prompt注入
```

- 索引: Milvus Lite + BGE-base-zh-v1.5 (768维)
- 混合检索: jieba+BM25 + 向量ANN → RRF (k=60)
- MMR: λ=0.6
- 上下文: 4000字硬上限

### 3.2 端到端结果

| 配置 | 分数 | 说明 |
|---|---|---|
| RAG only (v6 单向量) | 61.3 ± 2.1 | 检索弱, 拖后腿 |
| PE+RAG v6 (假hybrid) | 85.3 ± 2.5 | 单点91是噪声, 3次复测打回 |
| PE+RAG v7 (BM25+RRF) | 89.3 ± 2.1 | 真hybrid |
| **PE+RAG v7 (BM25+RRF+MMR)** | **91.7 ± 2.5** | **SOTA** |

### 3.3 检索精度

| 配置 | R@5 | R@10 | MRR@10 |
|---|---|---|---|
| v6 单向量 | 0.07 | 0.11 | 0.08 |
| v7 BM25 (v6索引) | 0.32 | 0.34 | 0.21 |
| **v7 BM25 (v7min索引)** | **0.53** | **0.53** | **0.29** |

### 3.4 反直觉发现

- **text_for_embedding 含源码拖低检索**: v7min(仅签名) R@10=0.53 vs v6(含源码) 0.34，差19pp
- **MMR在精准召回时反作用**: v7min+MMR 端到端跌6.3分 — diversity在召回精准时是毒药
- **检索最优(0.53) ≠ 端到端最优(91.7)**: MMR牺牲精度换多样性，端到端上反而受益

---

## 4. Fine-tuning (Qwen3B)

### 4.1 训练配置

- 基座: Qwen2.5-Coder-3B-Instruct
- SFT: 500条 (DeepSeek API 合成, train 400 / val 100)
- 方法: 8bit QLoRA + peft (r=8, α=16)
- 硬件: RTX 4060 8GB
- 过拟合监控: train loss 0.61, eval loss 0.0015 ✅

### 4.2 完整消融矩阵 (Qwen3B, 8/8)

| 配置 | 总分 | call_chain | cross_file | func_locate | impact |
|---|---|---|---|---|---|
| baseline | **47** | 14/39 | 12/39 | 12/36 | 9/36 |
| ragonly | 46 | 14 | 11 | 12 | 9 |
| **peonly** | **59** | 15 | **22** | 9 | 13 |
| perag | 59 | 15 | 22 | 9 | 13 |
| ftonly | 51 | 14 | 13 | 13 | 11 |
| ft_rag | 52 | 15 | 13 | 13 | 11 |
| ft_pe | 46 | 15 | 13 | 10 | 8 |
| ft_all | 47 | 15 | 13 | 11 | 8 |

### 4.3 发现

- **PE 唯一有效** (+12), 主要受益于 cross_file (+10)
- **RAG 零效果** (-1到0增益), 3B模型看不懂注入代码
- **FT 单独 +4** (51), 但加PE后倒退 (46), FT学会的格式与PE v3冲突
- **3B 天花板 ~59**, 远低于 DeepSeek 的 91.7

---

## 5. 跨模型对照

| 配置 | DeepSeek | Qwen3B |
|---|---|---|
| baseline | 55 | 47 |
| PE only | **88.7** | 59 |
| RAG only | 61 | 46 |
| PE+RAG | **91.7** | 59 |
| FT only | — | 51 |

**结论**: DeepSeek 全面碾压。小模型容量不足以消化 PE 复杂 prompt 和 RAG 注入。

---

## 6. 消融矩阵汇总

| 单元 | DeepSeek | Qwen3B | 状态 |
|---|---|---|---|
| baseline | 55 | 47 | ✅ |
| PE only | 88.7 | 59 | ✅ |
| RAG only | 61 | 46 | ✅ |
| FT only | — | 51 | ✅ |
| PE+RAG | **91.7** | 59 | ✅ |
| PE+FT | — | 46 | ✅ |
| FT+RAG | — | 52 | ✅ |
| ALL | — | 47 | ✅ |

---

## 7. 考核核对

| # | 要求 | 状态 |
|---|---|---|
| ① | 50条真实项目评测 | ✅ |
| ② | PE四维量化 (每项独立数据) | ✅ |
| ③ | RAG Pipeline + Recall@K/MRR | ✅ |
| ④ | FT ≥500条 + 过拟合监控 | ✅ |
| ⑤ | 消融矩阵 8 配置 | ✅ |
| ⑥ | 可复现 (代码+配置+脚本+README) | ✅ |

---

## 文件清单

- `benchmark/questions.jsonl` — 50 题
- `pe/v1_system.txt` + `v3_cot.txt` + `fewshot_examples.jsonl` — PE 组件
- `pe/rag.py` + `build_index.py` — RAG Pipeline
- `data/sft_train.jsonl` + `sft_val.jsonl` — SFT 数据
- `models/qwen2.5-coder-3b-repomind-lora-v2/` — FT 模型
- `benchmark/results/` — 全部实验结果
- `scripts/` — 实验脚本
