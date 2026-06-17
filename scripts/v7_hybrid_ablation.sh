#!/usr/bin/env bash
# v7 hybrid 消融实验
# 6 跑：1 baseline (peonly) + 5 RAG 变体
# seed=42，单点配对对比
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
  /usr/bin/python3.12 benchmark/run_eval.py --seed 42 --output-name "$out" "$@" 2>&1 | tail -8
  echo ""
}

# 1. PE only (baseline, 已知 3 次均值 88.7)
run pe_v7_peonly_s42.json --pe-version 3

# 2. PE + RAG (v6 原策略重跑做配对基线)
run pe_v7_perag_v6_s42.json --pe-version 3 --enable-rag

# 3. PE + RAG + adaptive top_k
run pe_v7_perag_adaptive_s42.json --pe-version 3 --enable-rag --adaptive-top-k

# 4. PE + RAG + adaptive + MMR
run pe_v7_perag_adaptive_mmr_s42.json --pe-version 3 --enable-rag --adaptive-top-k --use-mmr

# 5. PE + RAG + adaptive + BM25/RRF (不开 MMR, 验证真 hybrid)
run pe_v7_perag_adaptive_bm25_s42.json --pe-version 3 --enable-rag --adaptive-top-k --use-bm25

# 6. PE + RAG + adaptive + BM25 + MMR (全叠加)
run pe_v7_perag_all_s42.json --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr

echo "=== ALL DONE ==="
