"""Manifest-first indexing and validation for Volc raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ACQUISITION_SCOPES = (
    "pipeline_e2e",
    "pipeline_context",
    "snapshot_build",
    "operator_case_e2e",
    "operator_case_perf",
)


@dataclass(frozen=True)
class AcquisitionValidation:
    valid: bool
    errors: tuple[str, ...]


def build_acquisition_manifest(
    platform_dir: Path,
    *,
    platform: str,
) -> Path:
    """Index only current, named acquisition scopes and write atomically."""

    plan_path = platform_dir / "operators" / "operator-plan.json"
    plan = _read_json(plan_path)
    raw_root = platform_dir / "operators" / "raw"
    artifacts: list[dict[str, Any]] = []
    scopes: dict[str, dict[str, Any]] = {}
    for scope in ACQUISITION_SCOPES:
        scope_root = raw_root / scope
        paths = (
            sorted(path for path in scope_root.rglob("*") if path.is_file())
            if scope_root.is_dir()
            else []
        )
        for path in paths:
            artifacts.append(
                {
                    "path": path.relative_to(platform_dir).as_posix(),
                    "scope": scope,
                    "required": True,
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
        scopes[scope] = {
            "status": "complete" if paths else "missing",
            "artifactCount": len(paths),
        }
    unsupported: list[dict[str, Any]] = []
    for task in plan.get("tasks") or []:
        for operator in task.get("operators") or []:
            if operator.get("isolationStatus") == "supported":
                continue
            unsupported.append(
                {
                    "pipelineId": str(task.get("pipelineId") or ""),
                    "operatorCaseId": str(operator.get("operatorCaseId") or ""),
                    "operatorId": str(operator.get("operatorId") or ""),
                    "order": int(operator.get("order", -1)),
                    "status": "unsupported",
                    "reason": str(
                        operator.get("isolationReason") or "unspecified"
                    ),
                }
            )
    manifest = {
        "schemaVersion": 1,
        "runId": str(plan.get("runId") or ""),
        "platformId": platform,
        "sourceRevision": str(plan.get("sourceRevision") or ""),
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "complete"
        if all(value["status"] == "complete" for value in scopes.values())
        else "partial",
        "scopes": scopes,
        "unsupportedCases": unsupported,
        "artifactCount": len(artifacts),
        "artifacts": artifacts,
    }
    path = platform_dir / "operators" / "acquisition-manifest.json"
    _write_json_atomic(path, manifest)
    complete_path = platform_dir / "operators" / "COMPLETE.json"
    if manifest["status"] == "complete":
        _write_json_atomic(
            complete_path,
            {
                "schemaVersion": 1,
                "runId": manifest["runId"],
                "platformId": platform,
                "status": "complete",
                "manifestSha256": _sha256_file(path),
            },
        )
    elif complete_path.exists():
        complete_path.unlink()
    return path


def validate_acquisition_manifest(
    platform_dir: Path, manifest_path: Path
) -> AcquisitionValidation:
    """Verify every listed path, size, and digest without directory discovery."""

    manifest = _read_json(manifest_path)
    errors: list[str] = []
    if manifest.get("status") == "complete":
        complete_path = platform_dir / "operators" / "COMPLETE.json"
        if not complete_path.is_file():
            errors.append("complete_marker_missing")
        else:
            complete = _read_json(complete_path)
            if complete.get("status") != "complete":
                errors.append("complete_marker_invalid")
            if complete.get("manifestSha256") != _sha256_file(manifest_path):
                errors.append("complete_manifest_sha256_mismatch")
    for item in manifest.get("artifacts") or []:
        if not isinstance(item, Mapping):
            errors.append("invalid_artifact_entry")
            continue
        relative = str(item.get("path") or "")
        path = platform_dir / relative
        try:
            path.resolve().relative_to(platform_dir.resolve())
        except ValueError:
            errors.append(f"path_outside_platform:{relative}")
            continue
        if not path.is_file():
            errors.append(f"artifact_missing:{relative}")
            continue
        if path.stat().st_size != int(item.get("size", -1)):
            errors.append(f"size_mismatch:{relative}")
            continue
        if _sha256_file(path) != str(item.get("sha256") or ""):
            errors.append(f"sha256_mismatch:{relative}")
    return AcquisitionValidation(valid=not errors, errors=tuple(errors))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
