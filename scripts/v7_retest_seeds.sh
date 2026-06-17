#!/usr/bin/env bash
# v7 hybrid 关键 2 配置 × 3 seed = 6 跑
# 验证 perag_all (94) 和 perag_adaptive_bm25 (91) 哪个真 SOTA
set -uo pipefail
cd /mnt/d/repomind-fastapi
mkdir -p logs

run() {
  local out="$1"
  shift
  if [ -f "benchmark/results/$out" ]; then
    echo "[SKIP] $out already exists"
    return 0
  fi
  echo "=== Running: $out | $* ==="
  /usr/bin/python3.12 benchmark/run_eval.py --output-name "$out" "$@" 2>&1 | tail -8
  echo ""
}

# 种子 1, 7（42 已跑过，文件存在会被 SKIP）
for seed in 1 7; do
  run pe_v7_perag_adaptive_bm25_s${seed}.json  --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --seed $seed
  run pe_v7_perag_all_s${seed}.json              --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr --seed $seed
done

echo "=== ALL DONE ==="
