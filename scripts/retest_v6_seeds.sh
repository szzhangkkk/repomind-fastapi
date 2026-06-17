#!/usr/bin/env bash
# v6 复测脚本：3 个核心配置 × 3 个 seed = 9 跑
# 输出名加 _s{seed} 后缀，不污染原 v6 结果
set -uo pipefail
cd /mnt/d/repomind-fastapi
SEEDS=(42 1 7)
CONFIGS=(
  "3 0 peonly"      # PE v3 only
  "3 1 perag"       # PE v3 + RAG
  "0 1 ragonly"     # RAG only (PE v0)
)
for cfg in "${CONFIGS[@]}"; do
  read pe rag suffix <<< "$cfg"
  for seed in "${SEEDS[@]}"; do
    out="pe_v6_${suffix}_s${seed}.json"
    if [ -f "benchmark/results/$out" ]; then
      echo "[SKIP] $out already exists"
      continue
    fi
    echo "=== Running: --pe-version $pe --enable-rag=$rag --seed $seed -> $out ==="
    python3 benchmark/run_eval.py --pe-version "$pe" $([ "$rag" -eq 1 ] && echo --enable-rag) --seed "$seed" --output-name "$out" 2>&1 | tail -20
    echo ""
  done
done
echo "=== ALL DONE ==="
