#!/usr/bin/env bash
# Qwen 3B 完整消融 — 8 配置 × 1 seed
set -uo pipefail
cd /mnt/d/repomind-fastapi
mkdir -p logs

VENV=/mnt/d/repomind-fastapi/.venv-ft/bin/python
BASE="/mnt/d/repomind-fastapi/models/qwen2.5-coder-3b-instruct/models--Qwen--Qwen2.5-Coder-3B-Instruct/snapshots/488639f1ff808d1d3d0ba301aef8c11461451ec5"
FT="/mnt/d/repomind-fastapi/models/qwen2.5-coder-3b-repomind-lora-v2/merged"

run() {
  local tag="$1"; shift
  local out="benchmark/results/qwen_$tag.json"
  shift
  if [ -f "$out" ]; then echo "[SKIP] $out"; return 0; fi
  echo "=== qwen_$tag ==="
  HF_ENDPOINT=https://hf-mirror.com $VENV benchmark/run_eval.py --output-name "qwen_$tag" --seed 42 "$@" 2>&1 | grep -E "BENCHMARK|Total score|Local|by category" | head -10
  echo ""
}

# 1-4: 无 FT (base Qwen3B)
run baseline --local-model "$BASE"
run peonly   --local-model "$BASE" --pe-version 3
run ragonly  --local-model "$BASE" --enable-rag --adaptive-top-k --use-bm25 --use-mmr
run perag    --local-model "$BASE" --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr

# 5-8: FT v2
run ftonly  --local-model "$FT"
run ft_pe   --local-model "$FT" --pe-version 3
run ft_rag  --local-model "$FT" --enable-rag --adaptive-top-k --use-bm25 --use-mmr
run ft_all  --local-model "$FT" --pe-version 3 --enable-rag --adaptive-top-k --use-bm25 --use-mmr

echo "=== ALL DONE ==="
