#!/usr/bin/env bash
set -uo pipefail
cd /mnt/d/repomind-fastapi
V=.venv-ft/bin/python
export HF_ENDPOINT=https://hf-mirror.com
BASE="models/qwen2.5-coder-3b-instruct/models--Qwen--Qwen2.5-Coder-3B-Instruct/snapshots/488639f1ff808d1d3d0ba301aef8c11461451ec5"
FT="models/qwen2.5-coder-3b-repomind-lora-v2/merged"
RAG="--enable-rag --adaptive-top-k --use-bm25 --use-mmr"
PE="--pe-version 3"

run() { local n="$1"; shift
  local out="benchmark/results/${n}.json"
  [ -f "$out" ] && { echo "[SKIP] $n"; return; }
  echo "=== $n $(date +%H:%M) ==="
  $V -u benchmark/run_eval.py --output-name "$n" --seed 42 "$@" 2>&1 | tee -a logs/qwen_${n}.log | grep -E "Total score|\[Local\]" | tail -3
  echo "  -> $(date +%H:%M)"
  echo ""; }

run qwen_peonly   --local-model "$BASE" $PE
run qwen_ragonly  --local-model "$BASE" $RAG
run qwen_ft_pe    --local-model "$FT" $PE
run qwen_ft_rag   --local-model "$FT" $RAG

echo "=== ALL DONE ==="