#!/usr/bin/env bash
set -euo pipefail

: "${CONTAINER_NAME:?CONTAINER_NAME is required}"
: "${HOST_DATA_ROOT:?HOST_DATA_ROOT is required}"
: "${EXPECTED_REVISION:?EXPECTED_REVISION is required}"
: "${EXPECTED_ARCH:?EXPECTED_ARCH is required}"
: "${EXPECTED_PRIVILEGED:?EXPECTED_PRIVILEGED is required}"
REQUIRE_PERF="${REQUIRE_PERF:-false}"
EXPECTED_CPUSET_CPUS="${EXPECTED_CPUSET_CPUS:-}"
EXPECTED_CPUSET_MEMS="${EXPECTED_CPUSET_MEMS:-}"
EXPECTED_NOFILE_SOFT="${EXPECTED_NOFILE_SOFT:-65536}"
EXPECTED_NOFILE_HARD="${EXPECTED_NOFILE_HARD:-524288}"
EXPECTED_VIRTUALIZATION="${EXPECTED_VIRTUALIZATION:-}"

manifest_dir="$HOST_DATA_ROOT/manifests"
mkdir -p "$manifest_dir"

source_revision="$(docker exec "$CONTAINER_NAME" git -C /opt/volc_operator_sim rev-parse HEAD)"
actual_arch="$(docker exec "$CONTAINER_NAME" uname -m)"
image_id="$(docker inspect -f '{{.Image}}' "$CONTAINER_NAME")"
container_id="$(docker inspect -f '{{.Id}}' "$CONTAINER_NAME")"
configured_image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")"
actual_privileged="$(docker inspect -f '{{.HostConfig.Privileged}}' "$CONTAINER_NAME")"
actual_cpuset_cpus="$(docker inspect -f '{{.HostConfig.CpusetCpus}}' "$CONTAINER_NAME")"
actual_cpuset_mems="$(docker inspect -f '{{.HostConfig.CpusetMems}}' "$CONTAINER_NAME")"
actual_nofile_soft="$(docker exec "$CONTAINER_NAME" bash -lc 'ulimit -Sn')"
actual_nofile_hard="$(docker exec "$CONTAINER_NAME" bash -lc 'ulimit -Hn')"
actual_numa_policy="$(docker exec "$CONTAINER_NAME" \
  printenv PERF_LOCK_NUMA_POLICY 2>/dev/null || true)"
actual_virtualization="$(docker exec "$CONTAINER_NAME" \
  printenv PERF_LOCK_VIRTUALIZATION 2>/dev/null || true)"

test "$source_revision" = "$EXPECTED_REVISION"
test "$actual_arch" = "$EXPECTED_ARCH"
test "$actual_privileged" = "$EXPECTED_PRIVILEGED"
test "$actual_cpuset_cpus" = "$EXPECTED_CPUSET_CPUS"
test "$actual_cpuset_mems" = "$EXPECTED_CPUSET_MEMS"
test "$actual_nofile_soft" = "$EXPECTED_NOFILE_SOFT"
test "$actual_nofile_hard" = "$EXPECTED_NOFILE_HARD"
if [[ -n "$EXPECTED_CPUSET_CPUS" ]]; then
  test "$actual_numa_policy" = \
    "cpus=$EXPECTED_CPUSET_CPUS,mems=$EXPECTED_CPUSET_MEMS"
fi
test "$actual_virtualization" = "$EXPECTED_VIRTUALIZATION"

xarch_fingerprint="$(docker exec "$CONTAINER_NAME" sha256sum \
  /opt/volc_operator_sim/.pyframework/xarch-conda-explicit.txt | awk '{print $1}')"
xdj_fingerprint="$(docker exec "$CONTAINER_NAME" sha256sum \
  /opt/volc_operator_sim/.pyframework/xdj-conda-explicit.txt | awk '{print $1}')"
perf_version="$(docker exec "$CONTAINER_NAME" perf --version 2>&1 | head -1)"
kernel="$(uname -srmo)"

data_manifest="$manifest_dir/host-data-manifest.json"
data_manifest_sha256=""
if [[ -f "$data_manifest" ]]; then
  data_manifest_sha256="$(python3 - "$data_manifest" <<'PY'
import hashlib
import json
import pathlib
import sys

record = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
source_manifest = record.get("sourceManifest", record)
canonical = json.dumps(
    source_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
).encode("utf-8")
print(hashlib.sha256(canonical).hexdigest())
PY
)"
fi

probe_log="$manifest_dir/perf-probe-$actual_arch.log"
cycles_available=false
if docker exec "$CONTAINER_NAME" perf stat -e cycles -- true \
  >"$probe_log" 2>&1; then
  cycles_available=true
fi

call_graph_available=false
probe_data="/tmp/pyframework-perf-callgraph.data"
if docker exec "$CONTAINER_NAME" perf record -g -o "$probe_data" -- sleep 0.01 \
  >>"$probe_log" 2>&1; then
  call_graph_available=true
fi
docker exec "$CONTAINER_NAME" rm -f "$probe_data" >/dev/null 2>&1 || true

export source_revision actual_arch image_id container_id configured_image
export actual_privileged xarch_fingerprint xdj_fingerprint perf_version kernel
export data_manifest_sha256 cycles_available call_graph_available
export actual_cpuset_cpus actual_cpuset_mems actual_nofile_soft actual_nofile_hard
export actual_numa_policy actual_virtualization

fingerprint_path="$manifest_dir/environment-fingerprint-$actual_arch.json"
python3 - "$fingerprint_path" <<'PY'
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys
import time


path = pathlib.Path(sys.argv[1])
payload = {
    "schemaVersion": 1,
    "sourceRevision": os.environ["source_revision"],
    "arch": os.environ["actual_arch"],
    "imageId": os.environ["image_id"],
    "configuredImage": os.environ["configured_image"],
    "containerId": os.environ["container_id"],
    "privileged": os.environ["actual_privileged"] == "true",
    "resourceEnvelope": {
        "cpuSet": os.environ["actual_cpuset_cpus"],
        "memoryNodes": os.environ["actual_cpuset_mems"],
        "nofile": {
            "soft": int(os.environ["actual_nofile_soft"]),
            "hard": int(os.environ["actual_nofile_hard"]),
        },
        "numaPolicy": os.environ["actual_numa_policy"],
        "virtualization": os.environ["actual_virtualization"],
    },
    "dataManifestSha256": os.environ["data_manifest_sha256"],
    "condaFingerprints": {
        "xarch": os.environ["xarch_fingerprint"],
        "xdj": os.environ["xdj_fingerprint"],
    },
    "kernel": os.environ["kernel"],
    "perf": {
        "version": os.environ["perf_version"],
        "cyclesAvailable": os.environ["cycles_available"] == "true",
        "callGraphAvailable": os.environ["call_graph_available"] == "true",
    },
    "capturedAtEpochNs": time.time_ns(),
}
temporary = path.with_suffix(".json.partial")
raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
temporary.write_text(raw, encoding="utf-8")
os.replace(temporary, path)
compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
encoded = base64.b64encode(compact.encode("utf-8")).decode("ascii")
print("PYFRAMEWORK_ENV_FINGERPRINT=" + encoded)
PY

if [[ "$REQUIRE_PERF" == "true" ]] && \
   [[ "$cycles_available" != "true" || "$call_graph_available" != "true" ]]; then
  echo "required perf capability probe failed; see $probe_log" >&2
  exit 3
fi
