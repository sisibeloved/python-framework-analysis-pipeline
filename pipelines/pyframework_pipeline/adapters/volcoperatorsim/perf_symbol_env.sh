# shellcheck shell=bash
# Sourced through BASH_ENV inside the pinned target's bench_capture.sh.
perf() {
  local real_perf="${PYFRAMEWORK_REAL_PERF:-/usr/bin/perf}"
  local cache="${PYFRAMEWORK_PERF_SYMBOL_CACHE:?missing symbol cache}"
  if [[ "${1:-}" == "record" ]]; then
    "${PYFRAMEWORK_PERF_SYMBOL_PYTHON:-python3}" \
      "${PYFRAMEWORK_PERF_SYMBOL_HELPER:?missing symbol helper}" \
      record --real-perf "$real_perf" --cache-root "$cache" -- "$@"
  else
    "$real_perf" --buildid-dir "$cache/buildid" "$@"
  fi
}
export -f perf
