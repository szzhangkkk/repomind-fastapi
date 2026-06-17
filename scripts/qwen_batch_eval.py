"""Qwen 3B 关键消融 — 4 配置 (baseline/pe+rag/ft/ft+all)
"""
import subprocess, sys, time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV = str(PROJECT_ROOT / ".venv-ft" / "bin" / "python")
BASE_MODEL = "/mnt/d/repomind-fastapi/models/qwen2.5-coder-3b-instruct/models--Qwen--Qwen2.5-Coder-3B-Instruct/snapshots/488639f1ff808d1d3d0ba301aef8c11461451ec5"
FT_MODEL = str(PROJECT_ROOT / "models" / "qwen2.5-coder-3b-repomind-lora-v2" / "merged")
RAG = "--enable-rag --adaptive-top-k --use-bm25 --use-mmr"
PE = "--pe-version 3"

configs = [
    ("qwen_baseline", f'--local-model "{BASE_MODEL}"'),
    ("qwen_perag",    f'--local-model "{BASE_MODEL}" {PE} {RAG}'),
    ("qwen_ftonly",   f'--local-model "{FT_MODEL}"'),
    ("qwen_ft_all",   f'--local-model "{FT_MODEL}" {PE} {RAG}'),
]

for tag, args in configs:
    out_file = PROJECT_ROOT / "benchmark" / "results" / f"{tag}.json"
    if out_file.exists():
        print(f"[SKIP] {tag}")
        continue
    t0 = time.time()
    print(f"\n{'='*60}\n  {tag}  ({time.strftime('%H:%M:%S')})\n{'='*60}")
    cmd = f'HF_ENDPOINT=https://hf-mirror.com {VENV} benchmark/run_eval.py --output-name {tag} --seed 42 {args}'
    result = subprocess.run(cmd, shell=True, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=5400)
    elapsed = time.time() - t0
    if result.returncode == 0:
        for line in result.stdout.split('\n'):
            if any(k in line for k in ['Total score', 'BENCHMARK', 'by category']):
                print(line)
    else:
        print(f"[FAILED] exit={result.returncode}")
        err = result.stderr
        if err:
            print(err[-500:])
    print(f"  Time: {elapsed/60:.1f} min")

print("\n=== ALL DONE ===")
