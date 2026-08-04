#!/usr/bin/env bash
set -euo pipefail

EXP=${EXP:?EXP is required}
BASELINE_CONTAINER=${BASELINE_CONTAINER:-datajuicer-cpython-jit-v101}
CANDIDATE_CONTAINER=${CANDIDATE_CONTAINER:-datajuicer-cinderx-jit-v101}
BASELINE_VENV=${BASELINE_VENV:-/work/venvs/cpython314-txt1-20260708T023124Z}
CANDIDATE_VENV=${CANDIDATE_VENV:-/work/venvs/cinderx314-txt1-20260708T023124Z}
UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
KENLM_HOST_SRC=${KENLM_HOST_SRC:-$EXP/src/kenlm-0.3.0-clean-$$}

install_basic_deps() {
  local container=$1
  local venv=$2
  docker exec \
    -e VENV="$venv" \
    -e UV_INDEX_URL="$UV_INDEX_URL" \
    "$container" \
    bash -lc '
      set -euxo pipefail
      /opt/python314/bin/uv pip install --python "$VENV/bin/python" fasttext-wheel sentencepiece
      "$VENV/bin/python" - <<'"'"'PY'"'"'
import importlib
for name in ("fasttext", "sentencepiece"):
    module = importlib.import_module(name)
    print(name, getattr(module, "__file__", ""))
PY
    '
}

copy_candidate_kenlm_source() {
  local host_dst="$KENLM_HOST_SRC"
  if docker exec "$CANDIDATE_CONTAINER" test -d /tmp/kenlm-inspect/kenlm-0.3.0; then
    mkdir -p "$host_dst"
    docker exec "$CANDIDATE_CONTAINER" \
      tar -C /tmp/kenlm-inspect/kenlm-0.3.0 \
      --exclude=./build \
      -cf - . | tar -C "$host_dst" -xf -
  fi
  [ -f "$host_dst/setup.py" ]
}

install_kenlm_from_source() {
  local container=$1
  local venv=$2
  local host_src="$KENLM_HOST_SRC"
  local remote_src="/tmp/kenlm-src-$(basename "$EXP")-$$"
  docker cp "$host_src" "$container:$remote_src"
  docker exec \
    -e VENV="$venv" \
    -e UV_INDEX_URL="$UV_INDEX_URL" \
    "$container" \
    bash -lc "
      set -euxo pipefail
      /opt/python314/bin/uv pip install --python \"\$VENV/bin/python\" \"$remote_src\"
      \"\$VENV/bin/python\" - <<'PY'
import kenlm
print('kenlm', kenlm.__file__)
PY
    "
}

copy_candidate_kenlm_binary() {
  local host_dst="$EXP/src/kenlm-binary"
  mkdir -p "$host_dst"

  docker exec "$CANDIDATE_CONTAINER" python - <<'PY' > "$host_dst/paths.txt"
import glob
import importlib.util
import pathlib
import site

spec = importlib.util.find_spec("kenlm")
if spec and spec.origin:
    print("module", spec.origin)

for base in site.getsitepackages():
    for path in glob.glob(str(pathlib.Path(base) / "kenlm*.dist-info")):
        print("distinfo", path)
PY

  awk '$1=="module"{print $2}' "$host_dst/paths.txt" | while read -r path; do
    docker cp "$CANDIDATE_CONTAINER:$path" "$host_dst/"
  done
  awk '$1=="distinfo"{print $2}' "$host_dst/paths.txt" | while read -r path; do
    docker cp "$CANDIDATE_CONTAINER:$path" "$host_dst/"
  done
  test -n "$(find "$host_dst" -maxdepth 1 -name 'kenlm*.so' -print -quit)"
}

install_kenlm_from_binary() {
  local container=$1
  local venv=$2
  local host_src="$EXP/src/kenlm-binary"
  local pyver
  pyver=$(docker exec -e VENV="$venv" "$container" bash -lc '"$VENV/bin/python" -c "import sys; print(f\"python{sys.version_info.major}.{sys.version_info.minor}\")"')
  local site_dir="$venv/lib/$pyver/site-packages"
  docker cp "$host_src/." "$container:$site_dir/"
  docker exec -e VENV="$venv" "$container" bash -lc '
    set -euxo pipefail
    "$VENV/bin/python" - <<'"'"'PY'"'"'
import kenlm
print("kenlm", kenlm.__file__)
PY
  '
}

echo "[audit] candidate global KenLM"
docker exec "$CANDIDATE_CONTAINER" python - <<'PY'
try:
    import kenlm
except Exception as exc:
    print("kenlm ERROR", repr(exc))
else:
    print("kenlm OK", kenlm.__file__)
PY

install_basic_deps "$BASELINE_CONTAINER" "$BASELINE_VENV"
install_basic_deps "$CANDIDATE_CONTAINER" "$CANDIDATE_VENV"

if copy_candidate_kenlm_source; then
  install_kenlm_from_source "$BASELINE_CONTAINER" "$BASELINE_VENV"
  install_kenlm_from_source "$CANDIDATE_CONTAINER" "$CANDIDATE_VENV"
else
  copy_candidate_kenlm_binary
  install_kenlm_from_binary "$BASELINE_CONTAINER" "$BASELINE_VENV"
  install_kenlm_from_binary "$CANDIDATE_CONTAINER" "$CANDIDATE_VENV"
fi
