# RepoMind 项目规划

> 基于微调/PE/RAG 的代码分析领域效果优化
> 目标:中难度(PE + RAG + LoRA 微调 + 完整消融实验)
> 算力:本地 4070 (8GB) + DeepSeek API + 可选 Colab
> 周期:5 天(可压缩到 3-4 天)

---

## 一、核心思路

**为什么选 FastAPI**:跨文件调用关系密集,有 200+ 模块,AST 解析成熟,pydeps 工具链完善,自动出题成本最低。

**为什么用 1.5B 模型**:4070 8GB 跑 7B 4bit 推理勉强,LoRA 微调 7B 直接 OOM。1.5B + Unsloth 4bit 是这套硬件的甜点,微调后效果接近 7B 基线 80% 水平。

**为什么用 API 做 baseline**:DeepSeek-V3 单次调用 ¥0.001,跑 200 题评测不到 2 块钱,质量远超本地小模型。报告里写"我们对比了商业 SOTA 与本地微调方案的差距",叙事更有力。

---

## 二、目录结构

```
/mnt/d/repomind-fastapi/
├── PLAN.md                    # 本文件
├── README.md                  # 项目说明(完成后写)
├── fastapi/                   # 目标项目 clone 下来
├── benchmark/
│   ├── generate_questions.py  # 自动出题脚本
│   ├── questions.jsonl        # 50+ 评测题
│   ├── judge_prompts/         # judge 用的 prompt
│   └── run_eval.py            # 跑评测统一入口
├── pe/
│   ├── v0_baseline.txt        # 裸 prompt
│   ├── v1_system.txt          # 加 System Prompt
│   ├── v2_fewshot.txt         # 加 Few-shot
│   ├── v3_cot.txt             # 加 CoT
│   ├── v4_postprocess.txt     # 加后处理
│   └── results/               # 每版得分
├── rag/
│   ├── build_index.py         # 构建向量索引
│   ├── retrieve.py            # 混合检索
│   ├── prompts/               # RAG prompt 模板
│   └── results/
├── finetune/
│   ├── data_gen.py            # 500 条微调数据生成
│   ├── train_lora.py          # Unsloth 训练脚本
│   ├── data/                  # 训练数据
│   └── outputs/               # LoRA 权重
├── ablation/
│   ├── matrix.py              # 消融实验矩阵
│   └── results/               # 6 组实验结果
├── reports/
│   ├── PE_report.md           # PE 优化报告
│   ├── RAG_report.md          # RAG 方案报告
│   ├── ablation_report.md     # 消融实验报告
│   └── final_summary.md       # 总结
└── scripts/
    ├── env_check.sh           # 环境检查
    ├── setup.sh               # 依赖安装
    └── run_all.sh             # 一键跑全套
```

---

## 三、5 天时间线(每天 4-6 小时有效产出)

### Day 1:环境 + 评测基建(关键路径)

**目标**:50 道题自动生成,本地模型 + API baseline 跑出来,看到第一组数字。

| 步骤 | 任务 | 产出 | 预计耗时 |
|------|------|------|----------|
| 1.1 | `nvidia-smi` + 装 CUDA/Python 环境检查 | 环境就绪 | 15min |
| 1.2 | `pip install unsloth vllm langchain chromadb` | 依赖装好 | 30min |
| 1.3 | clone FastAPI 到 `./fastapi/` | 源码就位 | 10min |
| 1.4 | 写 `generate_questions.py`:用 AST + import graph 自动出 4 类题(调用链、跨文件依赖、函数定义、修改影响) | 50 题 | 2h |
| 1.5 | 写 `judge_prompts/score.txt`:让 LLM judge 按 0/1/2/3 评分,带 rubric | judge prompt | 30min |
| 1.6 | `run_eval.py` 跑通:支持本地模型 + API 两种模式 | 评测脚本 | 1h |
| 1.7 | 跑 baseline(DeepSeek-V3 + Qwen2.5-Coder-1.5B) | `results/baseline.json` | 1h |

**Day 1 验收**:看到两个模型的 baseline 分数,知道低分集中在哪类题。

**容易卡的地方**:
- Unsloth 装失败 → 用 `pip install "unsloth[colab-new]@git+..."` 或换容器
- vLLM 装失败 → 推理用 transformers + bitsandbytes,慢一点但稳定
- AST 解析复杂度过高 → 限制只分析 `fastapi/routing/`,规模可控

---

### Day 2:PE 四维优化

**目标**:完成 v0→v1→v2→v3→v4 五版,每版独立量化增益。

| 步骤 | 任务 | 量化指标 |
|------|------|----------|
| 2.1 | v0:裸 prompt `"请分析这段代码:{code}\n问题:{q}"` | baseline 得分 |
| 2.2 | v1:加 System Prompt(角色 + 输出格式 + 长度约束) | Δscore |
| 2.3 | v2:加 5 个 Few-shot(从 baseline 错题里挑,人改写) | Δscore |
| 2.4 | v3:加 CoT("先列涉及文件→列调用顺序→给答案") | Δscore |
| 2.5 | v4:加后处理(提取代码块、过滤无关内容、统一格式) | Δscore |
| 2.6 | 写 PE 报告,画增益柱状图 | `reports/PE_report.md` |

**关键纪律**:每次只动一个维度,跑 3 次取平均,记录标准差。否则增益归因不清。

**Few-shot 怎么挑**:
- 从 baseline 错题里选 5 道
- 每道人工改写标准答案 + 推理过程
- 这 5 道要覆盖 4 类题(调用链/依赖/定义/影响)

**容易卡的地方**:
- 增益不明显 → Few-shot 质量不够,人工改写要写推理过程,不只给答案
- 后处理反而降分 → 检查是不是过滤太狠,把正确内容也砍了

---

### Day 3:RAG 管线(重点投入,差异化核心)

**目标**:混合检索 + 上下文管理 + 端到端提升。

| 步骤 | 任务 | 产出 |
|------|------|------|
| 3.1 | `build_index.py`:用 tree-sitter 切 FastAPI 源码为函数级 chunk,embed 用 BAAI/bge-small-en-v1.5(本地可跑) | 向量索引 |
| 3.2 | `retrieve.py`:BM25(关键词) + dense 混合,RRF 融合排序 | top-K 检索 |
| 3.3 | 评估 Recall@5/10/MRR | 检索精度报告 |
| 3.4 | 上下文窗口管理:超过 4K token 时按相关度截断 | 上下文组装 |
| 3.5 | 融合 prompt:把检索结果 + 问题一起给 LLM | RAG 端到端 |
| 3.6 | 跑 PE+RAG vs PE only 对比 | 量化 RAG 增益 |

**为什么必须混合检索**:论文反复验证,代码场景下纯 dense embedding 漏检率高(变量名/函数名/字符串都是关键词信号)。BM25 补这部分,RRF 融合权重 0.5/0.5 起步调。

**为什么选 bge-small**:本地 CPU 跑得动,索引 200 个文件 < 2 分钟。bge-large 质量高 5% 但慢 4 倍,不值。

**容易卡的地方**:
- 切 chunk 太碎 → 按函数切,带 3 行上下文;不按行切
- 检索召回低 → 加 ngram BM25,捕捉 CamelCase
- 上下文超长 → 用 LongContext 重新排序模型(bge-reranker-base)

---

### Day 4:微调数据 + LoRA 训练

**目标**:500 条微调数据 + LoRA 训练 + 部署。

| 步骤 | 任务 | 产出 |
|------|------|------|
| 4.1 | `data_gen.py`:用 GPT-4o-mini/DeepSeek 给 baseline 错题生成"标准 reasoning + 答案"配对 | 500 条 |
| 4.2 | 数据清洗:去重 + 人工抽检 20 条 | 干净数据 |
| 4.3 | `train_lora.py`:Unsloth + Qwen2.5-Coder-1.5B + 4bit + LoRA(r=16) | LoRA 权重 |
| 4.4 | 训练监控:每 50 step 看 loss,防过拟合 | 训练曲线 |
| 4.5 | 部署:合并 LoRA 权重,导出 GGUF 或 transformers 格式 | 可加载模型 |
| 4.6 | 跑 PE+FT 评测,对比 PE only | FT 增益 |

**数据配比**:
- 300 条:从 baseline 错题反向生成(针对性提升弱项)
- 150 条:从 FastAPI 测试代码提炼(泛化能力)
- 50 条:人工编写 high-quality 样本(质量锚点)

**训练超参**(4070 8GB 经验值):
- batch_size: 2, grad_accum: 8(等效 16)
- lr: 2e-4, epochs: 3
- max_seq_length: 2048
- LoRA: r=16, alpha=32, target=q,k,v,o,gate,up,down
- 预计 1.5-2 小时

**容易卡的地方**:
- 显存 OOM → 降 batch_size 到 1,加 gradient_checkpointing
- Loss 不降 → 检查数据格式,alpaca 格式字段要对
- 过拟合 → 训练集 loss ↓ 但测试集 ↓,减 epoch 到 2

---

### Day 5:消融实验 + 报告

**目标**:6 组实验 + 完整报告 + 项目可复现性。

| 步骤 | 任务 | 产出 |
|------|------|------|
| 5.1 | 写 `matrix.py`:6 组实验自动跑(PE/RAG/FT 排列组合) | 消融结果 |
| 5.2 | 每组跑 3 次取平均 + 标准差 | 统计显著性 |
| 5.3 | 画热力图 / 柱状图 / 表格 | 可视化 |
| 5.4 | 写 `final_summary.md`:瓶颈分析 + 最优策略 + 适用边界 | 总结报告 |
| 5.5 | 写 `README.md`:环境、运行、复现步骤 | 项目主页 |
| 5.6 | 录 demo:挑 3-5 道典型题,展示优化前后对比 | demo 截图 |

**6 组消融**:
1. baseline(无优化)
2. PE only
3. RAG only
4. FT only
5. PE + RAG
6. PE + RAG + FT(全量)

**报告必含元素**:
- 每个瓶颈要有 3+ 错题佐证(贴原始回答)
- 每个优化要有"独立增益"和"边际增益"两个数字
- 适用边界要说清楚:RAG 在小项目收益低、FT 在跨项目泛化差等

---

## 四、风险预案

| 风险 | 触发条件 | 应对 |
|------|----------|------|
| Unsloth 装不上 | CUDA 版本不对 | 用 Colab A100 跑微调,本地只做推理 |
| DeepSeek API 限流 | 短时间跑太多 | 加 retry + 限速 5 req/s |
| FastAPI 代码量太大 | 索引 > 1 小时 | 只索引 `fastapi/routing/` 子树,500 个文件足够 |
| 微调效果差 | < 5% 提升 | 优先做 RAG,微调只算"试过" |
| 时间不够 | Day 4 没跑完微调 | 跳过微调,做 PE+RAG 消融也算中难度完成 |

**最低完成线**(中难度要求):
- 50 题评测 + baseline
- PE 四维优化 + 量化报告
- RAG 管线 + 对比
- 6 组消融(FT 用占位符/失败说明也行)

**理想完成线**:
- 上面全部 + LoRA 微调成功 + 报告带可视化 + README 完整

---

## 五、关键命令速查

**环境检查**:
```bash
nvidia-smi
python --version  # 要 3.10+
pip list | grep -E "torch|unsloth|transformers"
```

**Day 1 一键启动**:
```bash
cd /mnt/d/repomind-fastapi
git clone https://github.com/tiangolo/fastapi.git
pip install tree-sitter tree-sitter-python rank-bm25 sentence-transformers
```

**训练(后续)**:
```bash
cd /mnt/d/repomind-fastapi/finetune
python train_lora.py --data data/train.jsonl --output outputs/qwen-coder-lora
```

**评测**:
```bash
cd /mnt/d/repomind-fastapi/benchmark
python run_eval.py --config configs/ablation.yaml
```

---

## 六、立即执行清单(今天就要做)

- [ ] 跑 `nvidia-smi`,把显存/CUDA 版本截图发我
- [ ] 创建目录 `D:\repomind-fastapi\` (已建)
- [ ] 确认 Python 版本 `python --version`
- [ ] 注册/充值 DeepSeek API(充 10 块够用)
- [ ] 把这个 PLAN.md 读一遍,有疑问直接提

确认后我就开始写 Day 1 的 `generate_questions.py` 和 `run_eval.py`。
