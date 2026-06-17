# RepoMind PE 优化报告

> Baseline: deepseek-chat, minimal context, strict judge
> 基线: 52/150 (avg 1.04) | call_chain:0.92 cross_file_dep:0.92 function_locate:1.25 impact_analysis:1.08

---

## 实验记录

| 版本 | 策略 | 总分 | Δ | 说明 |
|------|------|------|---|------|
| v0 | 裸 prompt (baseline) | 52/150 (1.04) | — | 基准线 |


## v1: System Prompt | 55/150 (1.10) | Δv0=+3 | Δv0=+3

### 策略
- 角色定义: 代码调用链分析专家
- 输出约束: `func_name()(file:行号)` 格式
- 禁止项: 禁止描述函数逻辑、禁止解释性文字

### 各类别
- call_chain: 8/39 (0.62) — ⬇️-0.30
- cross_file_dep: 12/39 (0.92) — 持平
- function_locate: 20/36 (1.67) — ⬆️+0.42
- impact_analysis: 15/36 (1.25) — ⬆️+0.17

### 分数分布
0分:8 1分:28 2分:14 3分:0

### 关键改善
- function_locate 大幅提升(+0.42): 模型学会了"先列文件→再列参数→再列职责"的格式
- impact_analysis 小幅提升(+0.17): 输出更结构化

### 仍然存在的瓶颈
- call_chain 严重退化(-0.30): 强格式约束迫使模型在没有源码时硬编函数名
- 0分从5→8: 格式约束导致模型在不确定时编造内容

---

## v2: System Prompt + Few-shot | 71/150 (1.42) | Δv0=+19 | Δv1=+16

### 策略
- 在 v1 System Prompt 基础上, 加入 20 条 Few-shot 示例(每类 5 条)
- 示例严格按 GT 格式: call_chain=步骤列表, cross_file_dep=定义+导入, function_locate=文件+行+参数+职责, impact_analysis=位置+原因+要点

### 各类别
- call_chain: 7/39 (0.54) — ⬇️-0.38
- cross_file_dep: 20/39 (1.54) — ⬆️+0.62
- function_locate: 29/36 (2.42) — ⬆️+1.17
- impact_analysis: 15/36 (1.25) — 持平

### 分数分布
0分:6 1分:20 2分:22 3分:2

### 关键改善
- function_locate 冲到 2.42: Few-shot 教会了模型"定位题"的标准答法
- 首次出现 3 分题(2道)!
- cross_file_dep 翻倍: 模型学会了"列文件+行号"的模板

### 仍然存在的瓶颈
- call_chain 持续恶化(0.92→0.62→0.54): PE 对调用链完全无效, 必须用 RAG 喂源码
- impact_analysis 停滞(1.25): 需要全局代码视野, PE 不能替代

---

## v3: System Prompt + Few-shot + CoT | 74/150 (1.48) | Δv0=+22 | Δv2=+3

### 策略
- 在 v2 基础上, System Prompt 末尾追加 CoT 引导
- CoT: "(1)识别问题类型 (2)列出涉及函数/文件 (3)按顺序排列 (4)标注文件名:行号"

### 各类别
- call_chain: 5/39 (0.38) — ⬇️-0.54
- cross_file_dep: 22/39 (1.69) — ⬆️+0.77
- function_locate: 29/36 (2.42) — 持平
- impact_analysis: 18/36 (1.50) — ⬆️+0.42

### 分数分布
0分:6 1分:17 2分:25 3分:2

### 关键改善
- impact_analysis 突破 1.50: CoT 引导模型按"位置→原因→要点"分步推理
- 2分题从22→25, 1分题从20→17: 分布右移

### 仍然存在的瓶颈
- call_chain 跌到谷底(0.38): PE 对它完全反向效果——越多约束越崩溃
- 结论: call_chain 必须 RAG

---

## v4: System Prompt + Few-shot + CoT + 后处理 | 74/150 (1.48) | Δv0=+22 | Δv3=0

### 策略
- 在 v3 基础上, 增加后处理: 过滤解释性文字、统一行号格式、去重

### 各类别
- call_chain: 6/39 (0.46) — 从谷底小幅反弹
- cross_file_dep: 22/39 (1.69) — 持平
- function_locate: 29/36 (2.42) — 持平
- impact_analysis: 17/36 (1.42) — 小幅回落

### 分数分布
0分:5 1分:18 2分:25 3分:2

### 关键改善
- 后处理对 call_chain 有微弱帮助(0.38→0.46): 过滤了解释性文字

### 仍然存在的瓶颈
- 后处理未带来净提升(v3=v4=74)
- PE 到达上限

---

## PE 优化总结

| 版本 | 总分 | call_chain | cross_file_dep | function_locate | impact_analysis |
|------|------|-----------|---------------|-----------------|-----------------|
| v0 | 52 (1.04) | 0.92 | 0.92 | 1.25 | 1.08 |
| v2 | 71 (1.42) | 0.54 | 1.54 | 2.42 | 1.25 |
| v3 | 74 (1.48) | 0.38 | 1.69 | 2.42 | 1.50 |
| v4 | 74 (1.48) | 0.46 | 1.69 | 2.42 | 1.42 |

### 核心发现
1. **function_locate 已被 PE 解决**(1.25→2.42, +94%): 模型学会"定位题"标准答法
2. **cross_file_dep 大幅改善**(0.92→1.69, +84%): Few-shot 教会了"列文件+行号"
3. **impact_analysis 中等改善**(1.08→1.50, +39%): CoT 有帮助但缺乏全局视野
4. **call_chain PE 完全失败**(0.92→0.38): 越多约束越崩溃, 必须用 RAG 喂源码

### 最优配置: v3 (74/150)
- System Prompt + Few-shot(20条) + CoT
- 后处理无增益, 可省略

---
