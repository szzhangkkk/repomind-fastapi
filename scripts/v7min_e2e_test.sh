#!/usr/bin/env bash
# v7min 索引端到端复测
# 2 配置 × 3 seed = 6 跑
# 验证 v7min 检索优势 (R@10=0.53) 是否传导到端到端
# 跟 v6 索引的 6 跑配对比较
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

for seed in 42 1 7; do
  run pe_v7min_perag_adaptive_bm25_s${seed}.json \
    --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 \
    --rag-db-path pe/milvus_lite_v7min.db \
    --seed $seed

  run pe_v7min_perag_all_s${seed}.json \
    --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr \
    --rag-db-path pe/milvus_lite_v7min.db \
    --seed $seed
done

echo "=== ALL DONE ==="
