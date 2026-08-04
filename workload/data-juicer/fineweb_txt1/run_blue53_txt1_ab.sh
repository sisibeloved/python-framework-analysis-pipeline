#!/usr/bin/env bash
set -euo pipefail

EXP=${EXP:?EXP is required}
MODE=${MODE:-smoke}
CPUSET=${CPUSET:-}
BASELINE_CPUSET=${BASELINE_CPUSET:-${CPUSET:-0-3}}
CANDIDATE_CPUSET=${CANDIDATE_CPUSET:-${CPUSET:-4-7}}
NP_VALUES=${NP_VALUES:-1}
OP_NP_ARGS=${OP_NP_ARGS:-}
INPUT=${INPUT:-/work/input/fineweb_edu_sample_128.parquet}
OUTPUT_ROOT=${OUTPUT_ROOT:-/work/output/codex-txt1-ab-20260708T023124Z}
SOURCE_ROOT=${SOURCE_ROOT:-/work/src/data-juicer-codex}

BASELINE_CONTAINER=${BASELINE_CONTAINER:-datajuicer-cpython-jit-v101}
CANDIDATE_CONTAINER=${CANDIDATE_CONTAINER:-datajuicer-cinderx-jit-v101}
BASELINE_VENV=${BASELINE_VENV:-/work/venvs/cpython314-txt1-20260708T023124Z}
CANDIDATE_VENV=${CANDIDATE_VENV:-/work/venvs/cinderx314-txt1-20260708T023124Z}

run_one() {
  local label=$1
  local container=$2
  local venv=$3
  local cpuset=$4
  local out="$OUTPUT_ROOT/$MODE/$label"
  local tag="${label}_${MODE}"

  docker exec \
    -e VENV="$venv" \
    -e INPUT="$INPUT" \
    -e OUT="$out" \
    -e TAG="$tag" \
    -e NP_VALUES="$NP_VALUES" \
    -e OP_NP_ARGS="$OP_NP_ARGS" \
    -e SOURCE_ROOT="$SOURCE_ROOT" \
    -e CPUSET="$cpuset" \
    "$container" \
    bash -lc '
      set -euxo pipefail
      mkdir -p "$OUT"
      echo "[fingerprint]"
      hostname || true
      nproc || true
      taskset -pc $$ || true
      grep Cpus_allowed_list /proc/self/status || true
      "$VENV/bin/python" -V
      "$VENV/bin/python" -c "import data_juicer; print(data_juicer.__version__, data_juicer.__file__)"
      "$VENV/bin/python" -c "import sysconfig; print(sysconfig.get_config_var(\"SOABI\"))"
      "$VENV/bin/python" - <<'"'"'PY'"'"'
try:
    import cinderx
    import cinderx.jit as jit
    print("cinderx", cinderx.is_initialized(), cinderx.get_import_error(), "jit", jit.is_enabled())
except Exception as exc:
    print("cinderx", repr(exc))
PY
      echo "[run]"
      taskset -c "$CPUSET" "$VENV/bin/python" \
        "$SOURCE_ROOT/benchmarks/fineweb_edu_txt1/run_txt1_benchmark.py" \
        --input "$INPUT" \
        --output-root "$OUT" \
        --python "$VENV/bin/python" \
        --source-root "$SOURCE_ROOT" \
        --tag "$TAG" \
        --np $NP_VALUES \
        --repeat 1 \
        ${OP_NP_ARGS:+--op-np $OP_NP_ARGS} \
        --clean
    '
}

run_one baseline "$BASELINE_CONTAINER" "$BASELINE_VENV" "$BASELINE_CPUSET"
run_one candidate "$CANDIDATE_CONTAINER" "$CANDIDATE_VENV" "$CANDIDATE_CPUSET"
