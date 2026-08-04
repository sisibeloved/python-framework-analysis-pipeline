#!/usr/bin/env bash
set -euo pipefail

EXP=${EXP:-/root/codex-datajuicer-cinderx-20260708T023124Z}
OUTPUT_ROOT=${OUTPUT_ROOT:-/work/output/codex-txt1-ab-20260708T023124Z/whitespace-jit-probe}
SCRIPT=${SCRIPT:-/work/src/data-juicer-codex/benchmarks/fineweb_edu_txt1/whitespace_jit_probe.py}
INPUT=${INPUT:-/datasets/huggingface/fineweb-edu/sample/10BT/013_00000.parquet}
SOURCE_ROOT=${SOURCE_ROOT:-/work/src/data-juicer-codex}
STUB_PATH=${STUB_PATH:-$SOURCE_ROOT/benchmarks/fineweb_edu_txt1/stubs}
PROBE_PYTHONPATH=${PROBE_PYTHONPATH:-$STUB_PATH:$SOURCE_ROOT}

BASELINE_CONTAINER=${BASELINE_CONTAINER:-datajuicer-cpython-jit-v101}
CANDIDATE_CONTAINER=${CANDIDATE_CONTAINER:-datajuicer-cinderx-jit-v101}
BASELINE_PY=${BASELINE_PY:-/work/venvs/cpython314-txt1-20260708T023124Z/bin/python}
CANDIDATE_PY=${CANDIDATE_PY:-/work/venvs/cinderx314-txt1-20260708T023124Z/bin/python}

run_baseline_timing() {
  docker exec -e PYTHONPATH="$PROBE_PYTHONPATH" "$BASELINE_CONTAINER" bash -lc "
    set -euxo pipefail
    mkdir -p '$OUTPUT_ROOT'
    taskset -c 0-3 '$BASELINE_PY' '$SCRIPT' \
      --input-parquet '$INPUT' \
      --batch-size 256 \
      --warmup 6 \
      --loops 6 \
      --json-output '$OUTPUT_ROOT/baseline_timing.json' \
      > '$OUTPUT_ROOT/baseline_timing.stdout' 2>&1
    tail -n 20 '$OUTPUT_ROOT/baseline_timing.stdout'
  "
}

run_candidate_timing_with_jitlog() {
  docker exec \
    -e PYTHONJITAUTO=auto:2 \
    -e PYTHONJITLOGFILE="$OUTPUT_ROOT/candidate-timing.{pid}.jit.log" \
    -e PYTHONJITDUMPSTATS=1 \
    -e PYTHONPATH="$PROBE_PYTHONPATH" \
    "$CANDIDATE_CONTAINER" bash -lc "
      set -euxo pipefail
      mkdir -p '$OUTPUT_ROOT'
      taskset -c 4-7 '$CANDIDATE_PY' '$SCRIPT' \
        --input-parquet '$INPUT' \
        --batch-size 256 \
        --warmup 6 \
        --loops 6 \
        --json-output '$OUTPUT_ROOT/candidate_timing.json' \
        > '$OUTPUT_ROOT/candidate_timing.stdout' 2>&1
      tail -n 40 '$OUTPUT_ROOT/candidate_timing.stdout'
      ls -lh '$OUTPUT_ROOT'/candidate-timing*.jit.log || true
    "
}

run_candidate_hir_probe() {
  docker exec \
    -e PYTHONJITAUTO=auto:2 \
    -e PYTHONJITLOGFILE="$OUTPUT_ROOT/candidate-hir.{pid}.jit.log" \
    -e PYTHONJITDUMPFINALHIR=1 \
    -e PYTHONJITDUMPSTATS=1 \
    -e PYTHONPATH="$PROBE_PYTHONPATH" \
    "$CANDIDATE_CONTAINER" bash -lc "
      set -euxo pipefail
      mkdir -p '$OUTPUT_ROOT'
      taskset -c 4-7 '$CANDIDATE_PY' '$SCRIPT' \
        --batch-size 64 \
        --text-repeat 10 \
        --warmup 6 \
        --loops 2 \
        --json-output '$OUTPUT_ROOT/candidate_hir_probe.json' \
        > '$OUTPUT_ROOT/candidate_hir_probe.stdout' 2>&1
      tail -n 80 '$OUTPUT_ROOT/candidate_hir_probe.stdout'
      ls -lh '$OUTPUT_ROOT'/candidate-hir*.jit.log || true
    "
}

run_baseline_timing
run_candidate_timing_with_jitlog
run_candidate_hir_probe
