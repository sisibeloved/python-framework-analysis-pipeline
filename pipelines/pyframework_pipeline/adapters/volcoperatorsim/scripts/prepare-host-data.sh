#!/usr/bin/env bash
set -euo pipefail

: "${HOST_DATA_ROOT:?HOST_DATA_ROOT is required}"
: "${DATA_MANIFEST_B64:?DATA_MANIFEST_B64 is required}"

mkdir -p \
  "$HOST_DATA_ROOT/raw" \
  "$HOST_DATA_ROOT/models" \
  "$HOST_DATA_ROOT/fixtures" \
  "$HOST_DATA_ROOT/fixtures/raw" \
  "$HOST_DATA_ROOT/operator-cache" \
  "$HOST_DATA_ROOT/bench-results/pyframework" \
  "$HOST_DATA_ROOT/manifests" \
  "$HOST_DATA_ROOT/quarantine"

python3 - "$HOST_DATA_ROOT" "$DATA_MANIFEST_B64" <<'PY'
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import pathlib
import shutil
import sys
import time
import urllib.parse
import urllib.request


root = pathlib.Path(sys.argv[1]).resolve()
manifest = json.loads(base64.b64decode(sys.argv[2]).decode("utf-8"))
if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("entries"), list):
    raise SystemExit("invalid data source manifest")
minimum_free = int(os.environ.get("MIN_HOST_FREE_BYTES", "0") or 0)
available_free = shutil.disk_usage(root).free
if available_free < minimum_free:
    raise SystemExit(
        f"insufficient Host storage: available={available_free} required={minimum_free}"
    )

complete_path = root / "manifests" / "host-data-COMPLETE.json"
complete_path.unlink(missing_ok=True)
allowed_schemes = {"https", "file"}


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_target(relative: str) -> pathlib.Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"manifest path escapes HOST_DATA_ROOT: {relative}") from exc
    return candidate


def download(entry: dict) -> dict:
    source_id = str(entry.get("sourceId") or "")
    url = str(entry.get("url") or "")
    relative = str(entry.get("path") or "")
    expected = str(entry.get("sha256") or "").lower()
    if not source_id or not url or not relative or len(expected) != 64:
        raise ValueError(f"invalid manifest entry: {entry!r}")
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in allowed_schemes:
        raise ValueError(
            f"URL scheme is not allowlisted for {source_id}: {scheme or '[empty]'}"
        )
    target = safe_target(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.is_file() and sha256(target) == expected:
            return {**entry, "status": "reused", "actualSha256": expected}
        if target.exists():
            quarantine = root / "quarantine" / (
                target.name + f".{time.time_ns()}.existing-checksum-mismatch"
            )
            os.replace(target, quarantine)
        partial = target.with_name(target.name + ".partial")
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(url)
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                append = offset > 0 and getattr(response, "status", None) == 206
                mode = "ab" if append else "wb"
                with partial.open(mode) as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            actual = sha256(partial)
            if actual != expected:
                quarantine = root / "quarantine" / (
                    target.name + f".{int(time.time())}.sha256-mismatch"
                )
                os.replace(partial, quarantine)
                raise ValueError(
                    f"checksum mismatch for {source_id}: expected={expected} actual={actual}"
                )
            expected_size = entry.get("size")
            if expected_size is not None and partial.stat().st_size != int(expected_size):
                quarantine = root / "quarantine" / (
                    target.name + f".{time.time_ns()}.size-mismatch"
                )
                actual_size = partial.stat().st_size
                os.replace(partial, quarantine)
                raise ValueError(
                    f"size mismatch for {source_id}: "
                    f"expected={int(expected_size)} actual={actual_size}"
                )
            os.replace(partial, target)
            return {
                **entry,
                "status": "complete",
                "actualSha256": actual,
                "actualSize": target.stat().st_size,
            }
        except Exception:
            raise


results = []
required_failures = []
for raw_entry in manifest["entries"]:
    entry = dict(raw_entry)
    try:
        results.append(download(entry))
    except Exception as exc:
        required = bool(entry.get("required", True))
        status = "failed" if required else "optional_failed"
        results.append({**entry, "status": status, "error": str(exc)})
        if required:
            required_failures.append(str(exc))
record = {
    "schemaVersion": 1,
    "sourceManifest": manifest,
    "hostDataRoot": str(root),
    "minimumFreeBytes": minimum_free,
    "availableFreeBytesAtStart": available_free,
    "generatedAtEpochNs": time.time_ns(),
    "entries": results,
}
record_path = root / "manifests" / "host-data-manifest.json"
temporary = record_path.with_suffix(".json.partial")
temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, record_path)
if required_failures:
    raise SystemExit("; ".join(required_failures))
complete = {
    "schemaVersion": 1,
    "status": "complete",
    "manifest": str(record_path),
    "manifestSha256": sha256(record_path),
    "completedAtEpochNs": time.time_ns(),
}
complete_tmp = complete_path.with_suffix(".json.partial")
complete_tmp.write_text(
    json.dumps(complete, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
os.replace(complete_tmp, complete_path)
print(json.dumps({"manifest": str(record_path), "entries": len(results)}))
PY
