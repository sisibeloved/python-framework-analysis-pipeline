"""Deterministic operator plan and derived task generation.

The module never imports or copies target operator implementations.  It only
transforms frozen task documents into overlay tasks that are later executed by
the target repository's own ``run_perf_suite.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping

from ...contracts.operator import build_operator_case_id
from .snapshot import expected_snapshot_identity


SNAPSHOT_BUILDER_VERSION = "4"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_stage_snapshot_id(
    *,
    task_spec_id: str,
    task_document: Mapping[str, Any],
    through_order: int,
    source_revision: str,
    input_fingerprint: str,
) -> str:
    """Return the cache namespace for one reference-prefix snapshot."""

    if not task_spec_id:
        raise ValueError("task_spec_id must not be empty")
    if through_order < 0:
        raise ValueError("through_order must be non-negative")
    payload = {
        "taskSpecId": task_spec_id,
        "taskDocumentHash": _sha256(task_document),
        "throughOrder": through_order,
        "sourceRevision": source_revision,
        "inputFingerprint": input_fingerprint,
    }
    return _sha256(payload)[:16]


def build_operator_plan(
    *,
    formal_config: Mapping[str, Any],
    task_documents: Mapping[str, Mapping[str, Any]],
    group: str,
    run_id: str,
    platform: str,
    source_revision: str,
    selected_pipelines: tuple[str, ...] | list[str] | None = None,
    reference_producer: str = "daft_ray",
) -> dict[str, Any]:
    """Expand one formal group into deterministic operator and snapshot cases."""

    groups = formal_config.get("groups") or {}
    group_config = groups.get(group)
    if not isinstance(group_config, Mapping):
        raise ValueError(f"formal pipeline group not found: {group}")
    pipeline_configs = formal_config.get("pipelines") or {}
    group_pipelines = [str(value) for value in (group_config.get("tasks") or [])]
    selected = group_pipelines
    if selected_pipelines:
        requested = list(dict.fromkeys(str(value) for value in selected_pipelines))
        unknown = [value for value in requested if value not in group_pipelines]
        if unknown:
            raise ValueError(
                "selected pipeline is not in formal group "
                f"{group}: {', '.join(unknown)}"
            )
        requested_set = set(requested)
        selected = [value for value in group_pipelines if value in requested_set]
    tasks: list[dict[str, Any]] = []

    for pipeline_id_raw in selected:
        pipeline_id = str(pipeline_id_raw)
        task_document = task_documents.get(pipeline_id)
        if task_document is None:
            raise ValueError(f"task document not found for pipeline: {pipeline_id}")
        task = copy.deepcopy(dict(task_document))
        task_spec_id = str(task.get("task_id") or pipeline_id)
        body, sinks = _split_trailing_sinks(task.get("pipeline") or [])
        if not body:
            raise ValueError(f"task has no executable operators: {pipeline_id}")

        pipeline_config = pipeline_configs.get(pipeline_id) or {}
        engines = [str(value) for value in pipeline_config.get("engines", [])]
        canonical_input = copy.deepcopy(task.get("input") or {})
        modality = str(
            pipeline_config.get("modality") or canonical_input.get("modality") or ""
        )
        input_fingerprint = str(
            canonical_input.get("input_fingerprint")
            or canonical_input.get("dataset_fingerprint")
            or f"sha256:{_sha256(canonical_input)}"
        )
        canonical_input.setdefault("input_fingerprint", input_fingerprint)

        operators: list[dict[str, Any]] = []
        snapshots: list[dict[str, Any]] = []
        current_input: dict[str, Any] = {
            "kind": "canonical",
            "spec": canonical_input,
            "fingerprint": input_fingerprint,
        }
        for order, step in enumerate(body):
            operator_id = str(step.get("dj_ops") or step.get("operator") or "")
            if not operator_id:
                raise ValueError(
                    f"operator id missing in {pipeline_id} at order {order}"
                )
            case_id = build_operator_case_id(
                task_spec_id,
                order,
                operator_id,
                step.get("params") or {},
            )
            isolation_status = str(
                step.get("isolation")
                or step.get("isolation_status")
                or "supported"
            )
            supported = step.get("supported_engines")
            if isinstance(supported, list):
                case_engines = [
                    engine for engine in engines if engine in map(str, supported)
                ]
            else:
                unsupported = {
                    str(engine)
                    for engine in (step.get("unsupported_engines") or [])
                }
                case_engines = [
                    engine for engine in engines if engine not in unsupported
                ]
            if isolation_status != "supported":
                case_engines = []
            input_snapshot_after_order: int | None = (
                order - 1 if order > 0 else None
            )
            operator_input = copy.deepcopy(current_input)
            input_routing = {
                "mode": "sequential_predecessor",
                "reason": "operator input is the immediately preceding stage snapshot",
            }
            if (
                operator_id == "video_motion_score_filter"
                and order > 0
                and str(body[order - 1].get("dj_ops") or "")
                == "video_extract_frames_mapper"
            ):
                input_snapshot_after_order = order - 2 if order >= 2 else None
                if input_snapshot_after_order is None:
                    operator_input = {
                        "kind": "canonical",
                        "spec": copy.deepcopy(canonical_input),
                        "fingerprint": input_fingerprint,
                    }
                else:
                    source_snapshot = next(
                        item
                        for item in snapshots
                        if int(item["afterOrder"])
                        == input_snapshot_after_order
                    )
                    operator_input = {
                        "kind": "snapshot",
                        "snapshotId": str(source_snapshot["snapshotId"]),
                        "manifestPath": str(source_snapshot["manifestPath"]),
                        "fingerprint": f"sha256:{source_snapshot['snapshotId']}",
                    }
                input_routing = {
                    "mode": "object_layer_before_frame_extraction",
                    "reason": (
                        "video_motion_score_filter consumes object-layer video "
                        "paths; video_extract_frames_mapper produces a derived "
                        "frame directory"
                    ),
                }
            operators.append(
                {
                    "operatorCaseId": case_id,
                    "order": order,
                    "operatorId": operator_id,
                    "category": str(step.get("category") or "unknown"),
                    "paramsHash": case_id.rsplit("::", 1)[-1],
                    "params": copy.deepcopy(step.get("params") or {}),
                    "engines": case_engines,
                    "input": operator_input,
                    "inputSnapshotAfterOrder": input_snapshot_after_order,
                    "inputRouting": input_routing,
                    "isolationStatus": isolation_status,
                    "isolationReason": str(
                        step.get("isolation_reason")
                        or ("engine policy excludes all engines" if not case_engines else "")
                    ),
                }
            )

            if order + 1 < len(body):
                parent_fingerprint = str(current_input["fingerprint"])
                snapshot_id = build_stage_snapshot_id(
                    task_spec_id=task_spec_id,
                    task_document=task,
                    through_order=order,
                    source_revision=source_revision,
                    input_fingerprint=parent_fingerprint,
                )
                snapshot = {
                    **expected_snapshot_identity(
                        snapshot_id=snapshot_id,
                        source_revision=source_revision,
                        producer=reference_producer,
                        parent_fingerprint=parent_fingerprint,
                        operator_spec=step,
                        builder_version=SNAPSHOT_BUILDER_VERSION,
                    ),
                    "afterOrder": order,
                    "logicalField": str(canonical_input.get("field", "text")),
                    "partitionPolicy": "inherit_if_declared",
                    "mediaCompatibility": (
                        {"modality": modality}
                        if modality in {"image", "audio", "video"}
                        else None
                    ),
                    "cachePath": f"operator-cache/{snapshot_id}",
                    "manifestPath": f"operator-cache/{snapshot_id}/manifest.json",
                }
                snapshots.append(snapshot)
                current_input = {
                    "kind": "snapshot",
                    "snapshotId": snapshot_id,
                    "manifestPath": snapshot["manifestPath"],
                    "fingerprint": f"sha256:{snapshot_id}",
                }

        pseudo_stages = [
            {
                "operatorId": "__write_lance__"
                if str(step.get("dj_ops")) == "write_lance"
                else "__finalize__",
                "sourceOperatorId": str(step.get("dj_ops") or ""),
                "category": "sink",
            }
            for step in sinks
        ]
        tasks.append(
            {
                "pipelineId": pipeline_id,
                "taskSpecId": task_spec_id,
                "modality": modality,
                "engines": engines,
                "operators": operators,
                "snapshots": snapshots,
                "pseudoStages": pseudo_stages,
            }
        )

    return {
        "schemaVersion": 1,
        "runId": run_id,
        "platform": platform,
        "group": group,
        "sourceRevision": source_revision,
        "referenceProducer": reference_producer,
        "tasks": tasks,
    }


def render_isolated_task(
    task_document: Mapping[str, Any],
    *,
    order: int,
    input_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Render a one-operator overlay task for the target runner."""

    task = copy.deepcopy(dict(task_document))
    body, _ = _split_trailing_sinks(task.get("pipeline") or [])
    if order < 0 or order >= len(body):
        raise ValueError(f"operator order out of range: {order}")
    task_spec_id = str(task.get("task_id") or "task")
    task["task_id"] = f"{task_spec_id}__operator_{order:03d}"
    task["input"] = copy.deepcopy(dict(input_spec))
    isolated_operator = copy.deepcopy(body[order])
    task["pipeline"] = [isolated_operator]
    overrides = copy.deepcopy(task.get("engine_overrides") or {})
    overrides.update(
        {
            "materialize_policy": "per_op",
            "timing_tier": "p1",
            "fuse_mappers": False,
            "perf_lock_profile": "attribution",
        }
    )
    empty_output_is_valid = str(isolated_operator.get("category") or "") == "filter"
    if empty_output_is_valid:
        # A filter is allowed to reject every row in its case-local input.  Keep
        # the upstream runner's empty-output guard enabled for every other
        # category so missing models/resources cannot become silent successes.
        overrides["allow_empty_output"] = True
    task["engine_overrides"] = overrides
    metadata = copy.deepcopy(task.get("metadata") or {})
    metadata.update(
        {
            "measurementScope": "operator_case_e2e",
            "sourceTaskSpecId": task_spec_id,
            "operatorOrder": order,
            "emptyOutputIsValid": empty_output_is_valid,
        }
    )
    task["metadata"] = metadata
    return task


def render_full_task(
    task_document: Mapping[str, Any],
    *,
    measurement_scope: str,
    output_uri: str,
    engine_overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Render a full-pipeline task with a case-local persistent sink.

    Scaled attribution inputs must not mutate the target checkout's canonical
    task or overwrite its canonical output.  This derivative keeps every
    executable operator intact while moving ``write_lance`` to the current
    pyframework run namespace.
    """

    if not measurement_scope:
        raise ValueError("measurement_scope must not be empty")
    if not output_uri:
        raise ValueError("output_uri must not be empty")
    task = copy.deepcopy(dict(task_document))
    task_spec_id = str(task.get("task_id") or "task")
    task["task_id"] = f"{task_spec_id}__{measurement_scope}"
    redirected = False
    pipeline = copy.deepcopy(task.get("pipeline") or [])
    for step in pipeline:
        if str(step.get("dj_ops") or "") != "write_lance":
            continue
        params = copy.deepcopy(step.get("params") or {})
        params["output_uri"] = output_uri
        params.setdefault("mode", "overwrite")
        step["params"] = params
        redirected = True
    if not redirected:
        pipeline.append(
            {
                "dj_ops": "write_lance",
                "category": "sink",
                "params": {"output_uri": output_uri, "mode": "overwrite"},
            }
        )
    task["pipeline"] = pipeline
    merged_overrides = copy.deepcopy(task.get("engine_overrides") or {})
    merged_overrides.update(copy.deepcopy(dict(engine_overrides)))
    task["engine_overrides"] = merged_overrides
    metadata = copy.deepcopy(task.get("metadata") or {})
    metadata.update(
        {
            "measurementScope": measurement_scope,
            "sourceTaskSpecId": task_spec_id,
        }
    )
    task["metadata"] = metadata
    return task


def render_snapshot_task(
    task_document: Mapping[str, Any],
    *,
    through_order: int,
    output_uri: str,
) -> dict[str, Any]:
    """Render a reference-prefix task ending in the target write_lance sink."""

    task = copy.deepcopy(dict(task_document))
    body, _ = _split_trailing_sinks(task.get("pipeline") or [])
    if through_order < 0 or through_order >= len(body):
        raise ValueError(f"snapshot order out of range: {through_order}")
    task_spec_id = str(task.get("task_id") or "task")
    task["task_id"] = f"{task_spec_id}__snapshot_{through_order:03d}"
    task["pipeline"] = copy.deepcopy(body[: through_order + 1]) + [
        {
            "dj_ops": "write_lance",
            "category": "sink",
            "params": {"output_uri": output_uri, "mode": "overwrite"},
        }
    ]
    overrides = copy.deepcopy(task.get("engine_overrides") or {})
    overrides.update(
        {
            "materialize_policy": "end",
            "timing_tier": "p0",
            "fuse_mappers": False,
            "include_write_lance_in_elapsed": True,
            "perf_lock_profile": "attribution",
        }
    )
    task["engine_overrides"] = overrides
    metadata = copy.deepcopy(task.get("metadata") or {})
    metadata.update(
        {
            "measurementScope": "snapshot_build",
            "sourceTaskSpecId": task_spec_id,
            "throughOrder": through_order,
        }
    )
    task["metadata"] = metadata
    return task


def _split_trailing_sinks(
    pipeline: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    body: list[dict[str, Any]] = []
    sinks: list[dict[str, Any]] = []
    seen_sink = False
    for raw_step in pipeline:
        step = copy.deepcopy(dict(raw_step))
        is_sink = (
            str(step.get("category") or "") == "sink"
            or str(step.get("dj_ops") or "") == "write_lance"
        )
        if is_sink:
            seen_sink = True
            sinks.append(step)
            continue
        if seen_sink:
            raise ValueError("sink operators must be trailing")
        body.append(step)
    return body, sinks
