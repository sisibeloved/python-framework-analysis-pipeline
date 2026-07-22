"""Stage snapshot cache identity and parity validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SnapshotValidation:
    cache_hit: bool
    reasons: tuple[str, ...]


def expected_snapshot_identity(
    *,
    snapshot_id: str,
    source_revision: str,
    producer: str,
    parent_fingerprint: str,
    operator_spec: Mapping[str, Any],
    builder_version: str,
) -> dict[str, Any]:
    """Build every field that must match before a snapshot may be reused."""

    operator_json = json.dumps(
        operator_spec,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schemaVersion": 1,
        "snapshotId": snapshot_id,
        "sourceRevision": source_revision,
        "producer": producer,
        "parentFingerprint": parent_fingerprint,
        "operatorSpecHash": hashlib.sha256(
            operator_json.encode("utf-8")
        ).hexdigest(),
        "builderVersion": builder_version,
    }


def validate_snapshot_manifest(
    manifest: Mapping[str, Any], expected: Mapping[str, Any]
) -> SnapshotValidation:
    """Reject incomplete, stale, or parity-failed cache entries."""

    reasons: list[str] = []
    fields = {
        "snapshotId": "snapshot_id_mismatch",
        "sourceRevision": "source_revision_mismatch",
        "producer": "producer_mismatch",
        "parentFingerprint": "parent_fingerprint_mismatch",
        "operatorSpecHash": "operator_spec_mismatch",
        "builderVersion": "builder_version_mismatch",
    }
    for field, reason in fields.items():
        if manifest.get(field) != expected.get(field):
            reasons.append(reason)
    if manifest.get("status") != "complete":
        reasons.append("snapshot_incomplete")
    parity = manifest.get("parity")
    if not isinstance(parity, Mapping) or parity.get("status") != "passed":
        reasons.append("parity_failed")
    elif not all(
        parity.get(field) is not None
        for field in ("orderedFingerprint", "contentFingerprint", "partitionSpec")
    ):
        reasons.append("parity_metadata_missing")
    expected_partition_policy = expected.get("partitionPolicy")
    if expected_partition_policy is not None and isinstance(parity, Mapping):
        partition_spec = parity.get("partitionSpec")
        if not isinstance(partition_spec, Mapping):
            reasons.append("partition_inheritance_missing")
        elif partition_spec.get("policy") != expected_partition_policy:
            reasons.append("partition_policy_mismatch")
        elif partition_spec.get("status") not in {"passed", "not_applicable"}:
            reasons.append("partition_inheritance_failed")
        elif (
            partition_spec.get("status") == "passed"
            and partition_spec.get("sourceFragments")
            != partition_spec.get("fragments")
        ):
            reasons.append("partition_inheritance_failed")
    representations = manifest.get("representations")
    if not isinstance(representations, Mapping) or not all(
        isinstance(representations.get(kind), Mapping)
        and all(
            representations[kind].get(field) is not None
            for field in ("rows", "files", "bytes", "schemaFingerprint")
        )
        for kind in ("lance", "jsonl")
    ):
        reasons.append("representation_metadata_missing")
    expected_logical_field = expected.get("logicalField")
    if expected_logical_field is not None:
        logical_field = manifest.get("logicalField")
        if not isinstance(logical_field, Mapping) or logical_field.get(
            "status"
        ) != "passed":
            reasons.append("logical_field_missing")
        elif logical_field.get("field") != expected_logical_field:
            reasons.append("logical_field_mismatch")
    expected_media = expected.get("mediaCompatibility")
    if isinstance(expected_media, Mapping):
        modality = expected_media.get("modality")
        required_by_modality = {
            "image": {"images", "source_file", "text"},
            "audio": {"audios", "source_file", "text"},
            "video": {"videos", "source_file", "text"},
        }
        media = manifest.get("mediaCompatibility")
        if not isinstance(media, Mapping) or media.get("status") != "passed":
            reasons.append("media_compatibility_missing")
        elif media.get("modality") != modality:
            reasons.append("media_compatibility_mismatch")
        elif not required_by_modality.get(str(modality), set()).issubset(
            set(media.get("requiredFields") or [])
        ):
            reasons.append("media_compatibility_fields_missing")
    return SnapshotValidation(cache_hit=not reasons, reasons=tuple(reasons))
