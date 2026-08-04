#!/usr/bin/env bash
set -euxo pipefail

EXP=${EXP:?EXP is required}
BASELINE_CONTAINER=${BASELINE_CONTAINER:-datajuicer-cpython-jit-v101}
CANDIDATE_CONTAINER=${CANDIDATE_CONTAINER:-datajuicer-cinderx-jit-v101}
BASELINE_VENV=${BASELINE_VENV:-/work/venvs/cpython314-txt1-20260708T023124Z}
CANDIDATE_VENV=${CANDIDATE_VENV:-/work/venvs/cinderx314-txt1-20260708T023124Z}
UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}

mkdir -p "$EXP/src"
docker cp "$CANDIDATE_CONTAINER:/opt/python314/bin/uv" "$EXP/src/uv"
docker cp "$EXP/src/uv" "$BASELINE_CONTAINER:/opt/python314/bin/uv"
docker exec "$BASELINE_CONTAINER" chmod +x /opt/python314/bin/uv

create_and_install() {
  local container=$1
  local venv=$2
  docker exec \
    -e VENV="$venv" \
    -e UV_INDEX_URL="$UV_INDEX_URL" \
    "$container" \
    bash -lc '
      set -euxo pipefail
      /opt/python314/bin/uv --version
      if [ ! -x "$VENV/bin/python" ]; then
        /opt/python314/bin/uv venv --system-site-packages --python /opt/python314/bin/python3.14 "$VENV"
      fi
      /opt/python314/bin/uv pip install --python "$VENV/bin/python" "py-data-juicer==1.5.2"
      "$VENV/bin/python" -V
      "$VENV/bin/python" -c "import sys, sysconfig; print(sys.executable); print(sysconfig.get_config_var(\"SOABI\"))"
      "$VENV/bin/python" -c "import data_juicer; print(\"data_juicer\", data_juicer.__version__, data_juicer.__file__)"
      "$VENV/bin/python" -c "import dill; print(\"dill\", dill.__version__, dill.__file__)"
    '
}

create_and_install "$BASELINE_CONTAINER" "$BASELINE_VENV"
create_and_install "$CANDIDATE_CONTAINER" "$CANDIDATE_VENV"
