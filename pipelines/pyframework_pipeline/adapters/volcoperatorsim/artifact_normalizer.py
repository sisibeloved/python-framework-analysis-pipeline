"""Normalize target-owned Volc artifacts into stable operator contracts."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...contracts.operator import (
    OperatorDataset,
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
)
from .normalize import evaluate_operator_quality, parse_context_records
from .perf_symbol_bundle import IDENTITY_POLICY


PERF_FIELDS = [
    "run_id",
    "task_id",
    "engine_id",
    "operator_case_id",
    "operator_id",
    "operator_order",
    "measurement_scope",
    "input_fingerprint",
    "quality_grade",
    "platform_id",
    "arch",
    "benchmark",
    "event",
    "period",
    "sample_count",
    "period_share",
    "estimated_cpu_time_ns",
    "pid",
    "command",
    "shared_object",
    "symbol",
    "category_top",
    "category_sub",
    "category_reason",
    "source_report",
]


def normalize_operator_artifacts(
    platform_dir: Path,
    *,
    platform: str,
    min_perf_samples: int = 5000,
    unblock_perf: bool = False,
    representative_profile: bool = False,
    top_symbols: int = 20,
) -> Path:
    """Normalize context, isolated timing, and perf evidence for one platform."""

    plan_path = platform_dir / "operators" / "operator-plan.json"
    plan = _read_json(plan_path)
    source_revision = str(plan.get("sourceRevision") or "")
    if not source_revision:
        raise ValueError("operator plan sourceRevision must not be empty")
    run_id = str(plan.get("runId") or "")
    raw_root = platform_dir / "operators" / "raw"
    manifest_path = platform_dir / "operators" / "acquisition-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"acquisition manifest is required: {manifest_path}")
    from .acquisition_manifest import validate_acquisition_manifest

    validation = validate_acquisition_manifest(platform_dir, manifest_path)
    if not validation.valid:
        raise ValueError(
            "invalid acquisition manifest: " + ", ".join(validation.errors)
        )
    acquisition_manifest = _read_json(manifest_path)
    if acquisition_manifest.get("status") != "complete":
        raise ValueError("acquisition manifest is not complete")
    if str(acquisition_manifest.get("runId") or "") != run_id:
        raise ValueError("acquisition manifest runId does not match operator plan")
    if str(acquisition_manifest.get("sourceRevision") or "") != source_revision:
        raise ValueError(
            "acquisition manifest sourceRevision does not match operator plan"
        )
    allowed_paths = {
        (platform_dir / str(item["path"])).resolve()
        for item in acquisition_manifest.get("artifacts") or []
    }
    index = _build_operator_index(plan)

    perf_rows, perf_by_key = _collect_perf_rows(
        raw_root=raw_root,
        index=index,
        run_id=run_id,
        platform=platform,
        allowed_paths=allowed_paths,
    )
    records: list[OperatorRecord] = []
    context_records = _normalize_context_records(
        raw_root=raw_root,
        index=index,
        run_id=run_id,
        platform=platform,
        unblock_perf=unblock_perf,
        representative_profile=representative_profile,
        allowed_paths=allowed_paths,
    )
    records.extend(context_records)
    isolated_by_key = _normalize_isolated_records(
        raw_root=raw_root,
        index=index,
        run_id=run_id,
        platform=platform,
        perf_by_key=perf_by_key,
        min_perf_samples=min_perf_samples,
        unblock_perf=unblock_perf,
        representative_profile=representative_profile,
        allowed_paths=allowed_paths,
    )
    records.extend(isolated_by_key.values())
    records.extend(
        _normalize_perf_scope_records(
            index=index,
            perf_by_key=perf_by_key,
            isolated_by_key=isolated_by_key,
            run_id=run_id,
            platform=platform,
            min_perf_samples=min_perf_samples,
            unblock_perf=unblock_perf,
            representative_profile=representative_profile,
            top_symbol_limit=max(0, top_symbols),
        )
    )
    fingerprint_metadata = {
        "environmentFingerprintSha256": str(
            plan.get("environmentFingerprintSha256") or ""
        ),
        "runFingerprintSha256": str(plan.get("runFingerprintSha256") or ""),
    }
    records = [
        replace(
            record,
            metadata={**record.metadata, **fingerprint_metadata},
        )
        for record in records
    ]

    records.sort(
        key=lambda record: (
            record.pipeline_id,
            record.order,
            record.engine_id,
            record.measurement_scope,
        )
    )
    records_path = platform_dir / "operators" / "operator-records.jsonl"
    OperatorDataset(records=tuple(records)).write_jsonl(records_path)
    _write_perf_csv(
        platform_dir / "operators" / "perf" / "operator-perf-records.csv",
        perf_rows,
        isolated_by_key,
    )
    _write_operator_summary(
        platform_dir / "operators" / "operator-summary.csv", records
    )
    _write_operator_coverage(
        platform_dir / "operators" / "operator-coverage.json",
        index=index,
        context_records=context_records,
        isolated_by_key=isolated_by_key,
        perf_by_key=perf_by_key,
    )
    _write_json(
        platform_dir / "timing" / "operator-timing-normalized.json",
        {
            "schemaVersion": 1,
            "platformId": platform,
            "sourceRevision": source_revision,
            "records": [
                record.to_dict()
                for record in records
                if record.measurement_scope
                in {"pipeline_context", "operator_case_e2e"}
            ],
        },
    )
    return records_path


def _build_operator_index(
    plan: Mapping[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for task in plan.get("tasks") or []:
        pipeline_id = str(task.get("pipelineId") or "")
        task_spec_id = str(task.get("taskSpecId") or pipeline_id)
        for operator in task.get("operators") or []:
            case_id = str(operator.get("operatorCaseId") or "")
            case_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
            for engine in operator.get("engines") or task.get("engines") or []:
                key = (case_hash, str(engine))
                index[key] = {
                    **dict(operator),
                    "caseHash": case_hash,
                    "pipelineId": pipeline_id,
                    "taskSpecId": task_spec_id,
                    "engineId": str(engine),
                    "modality": str(task.get("modality") or ""),
                }
    return index


def _write_operator_coverage(
    path: Path,
    *,
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    context_records: Iterable[OperatorRecord],
    isolated_by_key: Mapping[tuple[str, str], OperatorRecord],
    perf_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> None:
    expected = set(index)
    context_keys = {
        (
            hashlib.sha256(record.operator_case_id.encode("utf-8")).hexdigest()[:12],
            record.engine_id,
        )
        for record in context_records
    }
    actual_by_scope = {
        "pipeline_context": context_keys,
        "operator_case_e2e": set(isolated_by_key),
        "operator_case_perf": set(perf_by_key),
    }

    def describe(key: tuple[str, str]) -> dict[str, Any]:
        spec = index[key]
        return {
            "pipelineId": str(spec["pipelineId"]),
            "taskSpecId": str(spec["taskSpecId"]),
            "engineId": key[1],
            "operatorCaseId": str(spec["operatorCaseId"]),
            "operatorId": str(spec["operatorId"]),
            "order": int(spec["order"]),
        }

    scopes: dict[str, dict[str, Any]] = {}
    for scope, actual in actual_by_scope.items():
        missing = [describe(key) for key in sorted(expected - actual)]
        scopes[scope] = {
            "actual": len(expected & actual),
            "missing": missing,
        }
    _write_json(
        path,
        {
            "schemaVersion": 1,
            "status": (
                "complete"
                if all(not value["missing"] for value in scopes.values())
                else "partial"
            ),
            "expectedCaseCount": len(expected),
            "scopes": scopes,
        },
    )


def _normalize_context_records(
    *,
    raw_root: Path,
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    run_id: str,
    platform: str,
    unblock_perf: bool,
    representative_profile: bool,
    allowed_paths: set[Path],
) -> list[OperatorRecord]:
    task_documents = _task_documents_from_index(index)
    records: list[OperatorRecord] = []
    for summary_path in _allowed_glob(
        raw_root / "pipeline_context", "**/summary.json", allowed_paths
    ):
        summary = _read_json_optional(summary_path)
        if not summary or int(summary.get("returncode", 0)) != 0:
            continue
        engine = str(summary.get("engine") or "")
        case = str(summary.get("case") or summary_path.parent.name)
        pipeline_id = _pipeline_from_context_case(case, task_documents)
        if not pipeline_id:
            continue
        artifact_name = str(
            (summary.get("artifacts") or {}).get("result_json") or ""
        )
        result_path = summary_path.parent / artifact_name if artifact_name else None
        if (
            result_path is None
            or not result_path.is_file()
            or result_path.resolve() not in allowed_paths
        ):
            result_path = _find_runner_result(
                raw_root / "pipeline_context" / "runner" / case,
                allowed_paths,
                engine=engine,
            )
        if result_path is None:
            continue
        result = _read_json(result_path)
        if engine and not result.get("engine_id"):
            result["engine_id"] = engine
        samples_path = summary_path.parent / "samples.jsonl"
        samples = (
            _read_jsonl(samples_path)
            if samples_path.resolve() in allowed_paths
            else ()
        )
        sampler_path = summary_path.parent / "sampler-summary.json"
        sampler = (
            _read_json_optional(sampler_path)
            if sampler_path.resolve() in allowed_paths
            else None
        )
        interval = float((sampler or {}).get("interval_s") or 0.2)
        parsed = parse_context_records(
            result=result,
            task_document=task_documents[pipeline_id],
            run_id=run_id,
            platform_id=platform,
            pipeline_id=pipeline_id,
            samples=samples,
            sample_interval_s=interval,
            unblock_perf=unblock_perf,
            representative_profile=representative_profile,
        )
        sources = tuple(
            str(path.relative_to(raw_root.parent.parent))
            for path in (summary_path, result_path, samples_path)
            if path.is_file() and path.resolve() in allowed_paths
        )
        records.extend(replace(record, source_artifacts=sources) for record in parsed)
    return records


def _task_documents_from_index(
    index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    task_ids: dict[str, str] = {}
    for item in index.values():
        pipeline_id = str(item["pipelineId"])
        grouped.setdefault(pipeline_id, []).append(item)
        task_ids[pipeline_id] = str(item["taskSpecId"])
    result: dict[str, dict[str, Any]] = {}
    for pipeline_id, operators in grouped.items():
        unique = {
            int(item["order"]): item
            for item in operators
        }
        result[pipeline_id] = {
            "task_id": task_ids[pipeline_id],
            "pipeline": [
                {
                    "dj_ops": item["operatorId"],
                    "category": item.get("category", "unknown"),
                    "params": dict(item.get("params") or {}),
                }
                for _order, item in sorted(unique.items())
            ],
        }
    return result


def _normalize_isolated_records(
    *,
    raw_root: Path,
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    run_id: str,
    platform: str,
    perf_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    min_perf_samples: int,
    unblock_perf: bool,
    representative_profile: bool,
    allowed_paths: set[Path],
) -> dict[tuple[str, str], OperatorRecord]:
    summaries: dict[tuple[str, str], list[Path]] = {}
    measured = raw_root / "operator_case_e2e" / "measured"
    for summary_path in _allowed_glob(
        measured, "**/summary.json", allowed_paths
    ):
        summary = _read_json_optional(summary_path)
        if not summary or int(summary.get("returncode", 0)) != 0:
            continue
        parsed = _parse_case(str(summary.get("case") or summary_path.parent.name))
        engine = str(summary.get("engine") or "")
        if parsed is None or (parsed[0], engine) not in index:
            continue
        summaries.setdefault((parsed[0], engine), []).append(summary_path)

    result: dict[tuple[str, str], OperatorRecord] = {}
    for key, summary_paths in summaries.items():
        spec = index[key]
        rounds: list[dict[str, Any]] = []
        sources: list[str] = []
        for summary_path in summary_paths:
            summary = _read_json(summary_path)
            case = str(summary.get("case") or summary_path.parent.name)
            runner_dirs = (
                measured / "runner" / case,
                raw_root / "operator_case_e2e" / "runner" / case,
            )
            result_path = next(
                (
                    candidate
                    for runner_dir in runner_dirs
                    if (
                        candidate := _find_runner_result(
                            runner_dir, allowed_paths, engine=key[1]
                        )
                    )
                    is not None
                ),
                None,
            )
            sampler_path = summary_path.parent / "sampler-summary.json"
            if (
                result_path is None
                or not sampler_path.is_file()
                or sampler_path.resolve() not in allowed_paths
            ):
                continue
            target = _read_json(result_path)
            sampler = _read_json(sampler_path)
            if str(target.get("status") or "ok") != "ok":
                continue
            metrics = target.get("metrics") or {}
            op_timings = metrics.get("operator_timings") or []
            op_timing = op_timings[0] if op_timings else {}
            outer_ns = _seconds_to_ns(sampler.get("duration_s"))
            runner_ns = _seconds_to_ns(metrics.get("elapsed_s"))
            operator_ns = _seconds_to_ns(op_timing.get("elapsed_s"))
            residual_ns = (
                max(runner_ns - operator_ns, 0)
                if runner_ns is not None and operator_ns is not None
                else None
            )
            cpu_total = sampler.get("tree_cpu_time_s") or {}
            cpu_ns = _seconds_to_ns(
                cpu_total.get("total")
                if isinstance(cpu_total, Mapping)
                else cpu_total
            )
            perf_lock = target.get("perf_lock") or {}
            perf_lock_status = str(perf_lock.get("status") or "")
            rounds.append(
                {
                    "outerNs": outer_ns,
                    "runnerNs": runner_ns,
                    "operatorNs": operator_ns,
                    "residualNs": residual_ns,
                    "cpuNs": cpu_ns,
                    "rssBytes": _mb_to_bytes(sampler.get("peak_tree_rss_mb")),
                    "meanCores": _percent_to_cores(
                        sampler.get("mean_tree_cpu_pct")
                    ),
                    "inputRows": _optional_row_count(metrics.get("input_rows")),
                    "outputRows": _optional_row_count(metrics.get("output_rows")),
                    "fingerprint": _runtime_input_fingerprint(target, metrics),
                    "perfLockStatus": perf_lock_status,
                    "perfLockPassed": perf_lock_status in {"pass", "warn"},
                    "perfLockWarningCodes": _issue_codes(
                        perf_lock.get("warnings")
                    ),
                    "perfLockViolationCodes": _issue_codes(
                        perf_lock.get("violations")
                    ),
                    "arch": str((target.get("resources") or {}).get("arch") or ""),
                    "result": target,
                }
            )
            for path in (
                summary_path,
                sampler_path,
                summary_path.parent / "perf-stat.txt",
                result_path,
            ):
                if path.is_file() and path.resolve() in allowed_paths:
                    sources.append(str(path.relative_to(raw_root.parent.parent)))
        if not rounds:
            continue
        sample_count = int((perf_by_key.get(key) or {}).get("sampleCount", 0))
        fingerprints = {row["fingerprint"] for row in rounds if row["fingerprint"]}
        logical_fingerprint = str(
            (spec.get("input") or {}).get("fingerprint") or ""
        )
        if not logical_fingerprint or len(fingerprints) > 1:
            input_parity: bool | None = False
        elif not fingerprints:
            input_parity = None
        else:
            input_parity = True
        quality = evaluate_operator_quality(
            timing_source="isolated_operator_timing",
            input_parity=input_parity,
            perf_lock_passed=all(row["perfLockPassed"] for row in rounds),
            sample_count=sample_count,
            min_perf_samples=min_perf_samples,
            unblock_perf=unblock_perf,
            representative_profile=representative_profile,
            perf_lock_warned=any(
                row["perfLockStatus"] == "warn" for row in rounds
            ),
        )
        runner_values = _present(row["runnerNs"] for row in rounds)
        timing = OperatorTiming(
            outer_process_wall_ns=_median_int(
                _present(row["outerNs"] for row in rounds)
            ),
            runner_elapsed_ns=_median_int(runner_values),
            isolated_operator_ns=_median_int(
                _present(row["operatorNs"] for row in rounds)
            ),
            residual_ns=_median_int(
                _present(row["residualNs"] for row in rounds)
            ),
            median_ns=_median_int(runner_values),
            p95_ns=_percentile_int(runner_values, 0.95),
            stddev_ns=_stddev_int(runner_values),
            cv=_cv(runner_values),
            rounds=len(rounds),
            timing_source="isolated_operator_timing",
        )
        resources = OperatorResources(
            tree_cpu_time_ns=_median_int(
                _present(row["cpuNs"] for row in rounds)
            ),
            peak_tree_rss_bytes=max(
                _present(row["rssBytes"] for row in rounds), default=None
            ),
            mean_cores_busy=_median_float(
                _present(row["meanCores"] for row in rounds)
            ),
            sample_count=sample_count,
        )
        perf_stat = _aggregate_perf_stat(summary_paths)
        result[key] = OperatorRecord(
            run_id=run_id,
            platform_id=platform,
            arch=next((row["arch"] for row in rounds if row["arch"]), ""),
            pipeline_id=str(spec["pipelineId"]),
            task_spec_id=str(spec["taskSpecId"]),
            engine_id=str(spec["engineId"]),
            operator_case_id=str(spec["operatorCaseId"]),
            operator_id=str(spec["operatorId"]),
            order=int(spec["order"]),
            measurement_scope="operator_case_e2e",
            input_fingerprint=logical_fingerprint,
            timing=timing,
            resources=resources,
            perf_stat=perf_stat,
            quality=quality,
            source_artifacts=tuple(sorted(set(sources))),
            metadata={
                "category": str(spec.get("category") or "unknown"),
                "modality": str(spec.get("modality") or ""),
                "runtimeInputFingerprints": sorted(fingerprints),
                "inputRows": sorted(
                    _present(row["inputRows"] for row in rounds)
                ),
                "outputRows": sorted(
                    _present(row["outputRows"] for row in rounds)
                ),
                "emptyOutputObserved": any(
                    row["inputRows"] is not None
                    and row["inputRows"] > 0
                    and row["outputRows"] == 0
                    for row in rounds
                ),
                "perfLockStatuses": sorted(
                    {row["perfLockStatus"] for row in rounds}
                ),
                "perfLockWarningCodes": sorted(
                    {
                        code
                        for row in rounds
                        for code in row["perfLockWarningCodes"]
                    }
                ),
                "perfLockViolationCodes": sorted(
                    {
                        code
                        for row in rounds
                        for code in row["perfLockViolationCodes"]
                    }
                ),
                "rawRounds": [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"result"}
                    }
                    for row in rounds
                ],
            },
        )
    return result


def _issue_codes(items: Any) -> list[str]:
    if not isinstance(items, (list, tuple)):
        return []
    return [
        str(item.get("code"))
        for item in items
        if isinstance(item, Mapping) and item.get("code")
    ]


def _collect_perf_rows(
    *,
    raw_root: Path,
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    run_id: str,
    platform: str,
    allowed_paths: set[Path],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    candidates: dict[
        tuple[str, str], tuple[Path, list[dict[str, Any]], int]
    ] = {}
    resolved_reports = tuple(
        report
        for report in _allowed_glob(
            raw_root / "operator_case_perf",
            "**/perf-report-period-resolved.txt",
            allowed_paths,
        )
        if _read_allowed_json(
            report.parent / "perf-symbol-resolution.json", allowed_paths
        ).get("identityPolicy")
        == IDENTITY_POLICY
    )
    resolved_directories = {path.parent for path in resolved_reports}
    original_reports = tuple(
        path
        for path in _allowed_glob(
            raw_root / "operator_case_perf",
            "**/perf-report-period.txt",
            allowed_paths,
        )
        if path.parent not in resolved_directories
    )
    for report in (*resolved_reports, *original_reports):
        case = report.parent.name
        parsed = _parse_case(case)
        engine = report.parent.parent.name
        if parsed is None or (parsed[0], engine) not in index:
            continue
        key = (parsed[0], engine)
        parsed_rows = _parse_period_report(report)
        sample_count = sum(row["sampleCount"] for row in parsed_rows)
        previous = candidates.get(key)
        if previous is None or (sample_count, str(report)) > (
            previous[2],
            str(previous[0]),
        ):
            candidates[key] = (report, parsed_rows, sample_count)

    for key, (report, parsed_rows, sample_count) in sorted(candidates.items()):
        engine = key[1]
        spec = index[key]
        total_period = sum(row["period"] for row in parsed_rows)
        selected_rows: list[dict[str, Any]] = []
        for row in parsed_rows:
            category_top, category_sub, reason = _classify_symbol(
                row["symbol"], row["sharedObject"]
            )
            period_share = row["period"] / total_period if total_period else 0.0
            normalized = {
                "run_id": run_id,
                "task_id": spec["taskSpecId"],
                "engine_id": engine,
                "operator_case_id": spec["operatorCaseId"],
                "operator_id": spec["operatorId"],
                "operator_order": spec["order"],
                "measurement_scope": "operator_case_perf",
                "input_fingerprint": "",
                "quality_grade": "",
                "platform_id": platform,
                "arch": report.parts[-4] if len(report.parts) >= 4 else "",
                "benchmark": spec["pipelineId"],
                "event": "cycles",
                "period": row["period"],
                "sample_count": row["sampleCount"],
                "period_share": period_share,
                "estimated_cpu_time_ns": "",
                "pid": row["pid"],
                "command": row["command"],
                "shared_object": row["sharedObject"],
                "symbol": row["symbol"],
                "category_top": category_top,
                "category_sub": category_sub,
                "category_reason": reason,
                "source_report": str(report.relative_to(raw_root.parent.parent)),
                "_key": key,
            }
            rows.append(normalized)
            selected_rows.append(normalized)
        source_artifacts = _perf_source_artifacts(
            raw_root=raw_root,
            perf_directory=report.parent,
            case_hash=key[0],
            engine=engine,
            allowed_paths=allowed_paths,
        )
        grouped[key] = {
            "sampleCount": sample_count,
            "totalPeriod": total_period,
            "rows": selected_rows,
            "sourceArtifacts": source_artifacts,
            "buildIds": _parse_build_ids(
                report.parent / "perf-buildid-list.txt", allowed_paths
            ),
            "asmArtifacts": tuple(
                path
                for path in source_artifacts
                if path.endswith((".s", ".asm", "perf-annotate.txt"))
            ),
            "symbolResolution": _read_allowed_json(
                report.parent / "perf-symbol-resolution.json", allowed_paths
            ),
        }
    return rows, grouped


def _perf_source_artifacts(
    *,
    raw_root: Path,
    perf_directory: Path,
    case_hash: str,
    engine: str,
    allowed_paths: set[Path],
) -> tuple[str, ...]:
    paths = [
        path
        for path in perf_directory.iterdir()
        if path.is_file() and path.resolve() in allowed_paths
    ]
    flamegraph_case = f"operator_case_perf__{case_hash}__flamegraph*"
    paths.extend(
        _allowed_glob(
            raw_root / "operator_case_perf",
            f"**/{engine}/{flamegraph_case}/cpu.svg"
            , allowed_paths
        )
    )
    platform_dir = raw_root.parent.parent
    return tuple(
        str(path.relative_to(platform_dir))
        for path in sorted(set(paths))
    )


def _normalize_perf_scope_records(
    *,
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    perf_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    isolated_by_key: Mapping[tuple[str, str], OperatorRecord],
    run_id: str,
    platform: str,
    min_perf_samples: int,
    unblock_perf: bool,
    representative_profile: bool,
    top_symbol_limit: int,
) -> list[OperatorRecord]:
    records: list[OperatorRecord] = []
    for key, perf in perf_by_key.items():
        spec = index[key]
        isolated = isolated_by_key.get(key)
        sample_count = int(perf.get("sampleCount", 0))
        quality = evaluate_operator_quality(
            timing_source="isolated_operator_timing",
            input_parity=isolated is not None and bool(isolated.input_fingerprint),
            perf_lock_passed=(
                isolated is not None
                and "perf_lock_failed" not in isolated.quality.flags
            ),
            sample_count=sample_count,
            min_perf_samples=min_perf_samples,
            unblock_perf=unblock_perf,
            representative_profile=representative_profile,
            perf_lock_warned=(
                isolated is not None
                and "perf_lock_warn" in isolated.quality.flags
            ),
        )
        symbol_resolution = dict(perf.get("symbolResolution") or {})
        resolution_policy_ok = (
            symbol_resolution.get("identityPolicy") == IDENTITY_POLICY
        )
        resolution_complete = symbol_resolution.get("status") == "complete"
        if not resolution_policy_ok or not resolution_complete:
            resolution_flag = (
                "symbol_identity_policy_legacy"
                if not resolution_policy_ok
                else "symbol_resolution_incomplete"
            )
            quality = replace(
                quality,
                grade="C" if not resolution_complete else quality.grade,
                flags=tuple(dict.fromkeys((*quality.flags, resolution_flag))),
                formal_conclusion_allowed=False,
            )
        category_period: dict[str, int] = {}
        top_symbols: list[dict[str, Any]] = []
        for row in perf.get("rows") or []:
            category = str(row["category_top"])
            category_period[category] = category_period.get(category, 0) + int(
                row["period"]
            )
            top_symbols.append(
                {
                    "symbol": row["symbol"],
                    "sharedObject": row["shared_object"],
                    "period": int(row["period"]),
                    "periodShare": float(row["period_share"]),
                }
            )
        total_period = int(perf.get("totalPeriod", 0))
        category_distribution = {
            category: period / total_period if total_period else 0.0
            for category, period in sorted(category_period.items())
        }
        base_resources = (
            isolated.resources if isolated is not None else OperatorResources()
        )
        resources = replace(base_resources, sample_count=sample_count)
        records.append(
            OperatorRecord(
                run_id=run_id,
                platform_id=platform,
                arch=isolated.arch if isolated is not None else "",
                pipeline_id=str(spec["pipelineId"]),
                task_spec_id=str(spec["taskSpecId"]),
                engine_id=str(spec["engineId"]),
                operator_case_id=str(spec["operatorCaseId"]),
                operator_id=str(spec["operatorId"]),
                order=int(spec["order"]),
                measurement_scope="operator_case_perf",
                input_fingerprint=(isolated.input_fingerprint if isolated else ""),
                resources=resources,
                quality=quality,
                source_artifacts=tuple(perf.get("sourceArtifacts") or ()),
                metadata={
                    "event": "cycles",
                    "periodSemantics": "sampled_cpu_time_distribution",
                    "totalPeriod": total_period,
                    "categoryPeriodShare": category_distribution,
                    "topSymbols": sorted(
                        top_symbols, key=lambda item: item["period"], reverse=True
                    )[:top_symbol_limit],
                    "buildIds": list(perf.get("buildIds") or ()),
                    "asmArtifacts": list(perf.get("asmArtifacts") or ()),
                    "symbolResolution": dict(
                        symbol_resolution
                    ),
                    "perfLockStatuses": (
                        list(isolated.metadata.get("perfLockStatuses") or ())
                        if isolated is not None
                        else []
                    ),
                    "perfLockWarningCodes": (
                        list(
                            isolated.metadata.get("perfLockWarningCodes") or ()
                        )
                        if isolated is not None
                        else []
                    ),
                    "perfLockViolationCodes": (
                        list(
                            isolated.metadata.get("perfLockViolationCodes") or ()
                        )
                        if isolated is not None
                        else []
                    ),
                },
            )
        )
    return records


def _write_perf_csv(
    path: Path,
    rows: list[dict[str, Any]],
    isolated_by_key: Mapping[tuple[str, str], OperatorRecord],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PERF_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: int(item["period"]), reverse=True):
            key = row.pop("_key")
            isolated = isolated_by_key.get(key)
            if isolated is not None:
                row["input_fingerprint"] = isolated.input_fingerprint
                row["quality_grade"] = isolated.quality.grade
                cpu_ns = isolated.resources.tree_cpu_time_ns
                if cpu_ns is not None:
                    row["estimated_cpu_time_ns"] = round(
                        cpu_ns * float(row["period_share"])
                    )
            row["period_share"] = _format_float(float(row["period_share"]))
            writer.writerow(row)


def _write_operator_summary(path: Path, records: Iterable[OperatorRecord]) -> None:
    fields = [
        "pipeline_id",
        "task_spec_id",
        "engine_id",
        "operator_case_id",
        "operator_id",
        "operator_order",
        "measurement_scope",
        "median_ns",
        "p95_ns",
        "tree_cpu_time_ns",
        "peak_tree_rss_bytes",
        "sample_count",
        "quality_grade",
        "formal_conclusion_allowed",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "pipeline_id": record.pipeline_id,
                    "task_spec_id": record.task_spec_id,
                    "engine_id": record.engine_id,
                    "operator_case_id": record.operator_case_id,
                    "operator_id": record.operator_id,
                    "operator_order": record.order,
                    "measurement_scope": record.measurement_scope,
                    "median_ns": record.timing.median_ns,
                    "p95_ns": record.timing.p95_ns,
                    "tree_cpu_time_ns": record.resources.tree_cpu_time_ns,
                    "peak_tree_rss_bytes": record.resources.peak_tree_rss_bytes,
                    "sample_count": record.resources.sample_count,
                    "quality_grade": record.quality.grade,
                    "formal_conclusion_allowed": record.quality.formal_conclusion_allowed,
                }
            )


def _write_acquisition_manifest(
    *,
    platform_dir: Path,
    raw_root: Path,
    run_id: str,
    source_revision: str,
    platform: str,
) -> None:
    artifacts: list[dict[str, Any]] = []
    if raw_root.is_dir():
        for path in sorted(item for item in raw_root.rglob("*") if item.is_file()):
            artifacts.append(
                {
                    "path": str(path.relative_to(platform_dir)),
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    _write_json(
        platform_dir / "operators" / "acquisition-manifest.json",
        {
            "schemaVersion": 1,
            "runId": run_id,
            "platformId": platform,
            "sourceRevision": source_revision,
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "artifactCount": len(artifacts),
            "artifacts": artifacts,
        },
    )


def _aggregate_perf_stat(summary_paths: Iterable[Path]) -> dict[str, Any]:
    observations: dict[str, list[dict[str, Any]]] = {}
    for summary_path in summary_paths:
        path = summary_path.parent / "perf-stat.txt"
        if not path.is_file():
            continue
        for key, value in _parse_perf_stat(path).items():
            observations.setdefault(key, []).append(value)
    events: dict[str, dict[str, Any]] = {}
    for key, items in sorted(observations.items()):
        values = [float(item["value"]) for item in items if item.get("value") is not None]
        coverages = [
            float(item.get("coveragePct", 100.0))
            for item in items
            if item.get("value") is not None
        ]
        coverage = min(coverages) if coverages else None
        status = (
            "unsupported"
            if not values
            else "multiplexed"
            if coverage is not None and coverage < 100.0
            else "ok"
        )
        events[key] = {
            "value": _counter_value(_median_float(values)),
            "status": status,
            "coveragePct": coverage,
        }
    counters = {
        key: value["value"]
        for key, value in events.items()
        if value["value"] is not None
    }
    cycles = counters.get("cycles")
    instructions = counters.get("instructions")
    l1_loads = counters.get("L1-dcache-loads")
    l1_misses = counters.get("L1-dcache-load-misses")
    llc_loads = counters.get("LLC-loads")
    llc_misses = counters.get("LLC-load-misses")
    branches = counters.get("branches")
    branch_misses = counters.get("branch-misses")
    return {
        "cycles": _counter_value(cycles),
        "instructions": _counter_value(instructions),
        "ipc": _safe_ratio(instructions, cycles),
        "l1dMissRate": _safe_ratio(l1_misses, l1_loads),
        "llcMissRate": _safe_ratio(llc_misses, llc_loads),
        "branchMissRate": _safe_ratio(branch_misses, branches),
        "events": events,
        "unsupportedEvents": [
            key for key, value in events.items() if value["status"] == "unsupported"
        ],
        "multiplexedEvents": [
            key for key, value in events.items() if value["status"] == "multiplexed"
        ],
        "eventCoverageMinPct": min(
            (
                float(value["coveragePct"])
                for value in events.values()
                if value["coveragePct"] is not None
            ),
            default=None,
        ),
    }


def _parse_perf_stat(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    pattern = re.compile(
        r"^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s+([^\s#]+)"
        r"(?:\s+\(([0-9]+(?:\.[0-9]+)?)%\))?"
    )
    unsupported = re.compile(r"^\s*<not (?:supported|counted)>\s+([^\s#]+)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        missing = unsupported.match(line)
        if missing:
            result[missing.group(1)] = {
                "value": None,
                "status": "unsupported",
                "coveragePct": None,
            }
            continue
        match = pattern.match(line)
        if not match:
            continue
        coverage = float(match.group(3)) if match.group(3) else 100.0
        result[match.group(2)] = {
            "value": float(match.group(1).replace(",", "")),
            "status": "multiplexed" if coverage < 100.0 else "ok",
            "coveragePct": coverage,
        }
    return result


def _allowed_glob(
    root: Path, pattern: str, allowed_paths: set[Path]
) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.glob(pattern)
        if path.is_file() and path.resolve() in allowed_paths
    )


def _parse_build_ids(
    path: Path, allowed_paths: set[Path]
) -> tuple[dict[str, str], ...]:
    if not path.is_file() or path.resolve() not in allowed_paths:
        return ()
    result: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and re.fullmatch(r"[0-9a-fA-F]+", fields[0]):
            result.append({"buildId": fields[0], "path": fields[1]})
    return tuple(result)


def _parse_period_report(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) < 7:
            continue
        try:
            period = int(float(fields[1].replace(",", "")))
            samples = int(float(fields[2].replace(",", "")))
        except ValueError:
            continue
        result.append(
            {
                "period": period,
                "sampleCount": samples,
                "command": fields[3],
                "pid": fields[4],
                "sharedObject": fields[5],
                "symbol": "|".join(fields[6:]),
            }
        )
    return result


def _classify_symbol(symbol: str, shared_object: str) -> tuple[str, str, str]:
    value = f"{symbol} {shared_object}".lower()
    if "libpython" in value or re.search(
        r"(?:^|[/\s])python(?:\d+(?:\.\d+)*)?(?:\s|$)", value
    ):
        return "Python/CPython runtime", "interpreter", "heuristic:python"
    if "daft" in value or "ray" in value:
        return "Daft/Ray control and execution", "framework", "heuristic:daft-ray"
    if "data_juicer" in value or "data-juicer" in value:
        return "Data-Juicer framework", "framework", "heuristic:data-juicer"
    if any(token in value for token in ("ffmpeg", "avcodec", "avformat", "libav", "x264", "x265")):
        return "codec/media subprocess", "native", "heuristic:codec"
    if any(token in value for token in ("torch", "numpy", "openblas", "mkl", "lapack")):
        return "model/math libraries", "native", "heuristic:model-math"
    if any(token in value for token in ("[kernel", "vmlinux", "libc.so", "libpthread")):
        return "libc/kernel", "system", "heuristic:system"
    if not symbol or symbol in {"[unknown]", "unknown"}:
        return "I/O/wait/unknown", "unknown", "heuristic:unknown"
    return "operator native libraries", "native", "heuristic:native"


def _pipeline_from_context_case(
    case: str, task_documents: Mapping[str, Any]
) -> str | None:
    for pipeline_id in sorted(task_documents, key=len, reverse=True):
        if f"__{pipeline_id}__" in case:
            return pipeline_id
    return None


def _parse_case(case: str) -> tuple[str, str] | None:
    match = re.search(
        r"operator_case_(?:e2e|perf)__([0-9a-f]{12})__(.+)$", case
    )
    return (match.group(1), match.group(2)) if match else None


def _find_runner_result(
    directory: Path,
    allowed_paths: set[Path],
    *,
    engine: str = "",
) -> Path | None:
    if not directory.is_dir():
        return None
    candidates = [
        path
        for path in directory.glob("*.json")
        if path.name not in {"summary.json", "sampler-summary.json"}
        and path.resolve() in allowed_paths
    ]
    if not candidates:
        return None
    successful = []
    for path in candidates:
        payload = _read_json_optional(path)
        if (
            payload
            and "metrics" in payload
            and (not engine or str(payload.get("engine_id") or "") == engine)
        ):
            successful.append(path)
    return sorted(successful)[-1] if successful else None


def _canonical_fingerprint(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (Mapping, list)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return str(value)


def _runtime_input_fingerprint(
    target: Mapping[str, Any], metrics: Mapping[str, Any]
) -> str:
    """Read runtime parity evidence from metrics or the executed task payload.

    Snapshot readers emit a metrics-level fingerprint, which remains the
    preferred evidence.  Canonical readers in the pinned target revision put
    the runtime capture (actual path and task/manifest hashes) under
    ``perf_lock.input_fingerprint``.  Older results may only retain the
    generated task specification, so that remains the final fallback.
    """

    fingerprint = _canonical_fingerprint(metrics.get("input_fingerprint"))
    if fingerprint:
        return fingerprint
    perf_lock = target.get("perf_lock") or {}
    if isinstance(perf_lock, Mapping):
        fingerprint = _canonical_fingerprint(
            perf_lock.get("input_fingerprint")
        )
        if fingerprint:
            return fingerprint
    task_spec = target.get("task_spec") or {}
    task_input = task_spec.get("input") if isinstance(task_spec, Mapping) else {}
    if not isinstance(task_input, Mapping):
        return ""
    return _canonical_fingerprint(task_input.get("input_fingerprint"))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_json_optional(path: Path) -> dict[str, Any] | None:
    try:
        return _read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_allowed_json(
    path: Path, allowed_paths: set[Path]
) -> dict[str, Any]:
    if path.resolve() not in allowed_paths:
        return {}
    return _read_json_optional(path) or {}


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.is_file():
        return ()
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return tuple(rows)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seconds_to_ns(value: Any) -> int | None:
    try:
        return round(float(value) * 1_000_000_000)
    except (TypeError, ValueError):
        return None


def _mb_to_bytes(value: Any) -> int | None:
    try:
        return round(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _percent_to_cores(value: Any) -> float | None:
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _present(values: Iterable[Any]) -> list[Any]:
    return [value for value in values if value is not None]


def _optional_row_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _median_int(values: list[int]) -> int | None:
    return round(statistics.median(values)) if values else None


def _median_float(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _percentile_int(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return round(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


def _stddev_int(values: list[int]) -> int | None:
    return round(statistics.pstdev(values)) if values else None


def _cv(values: list[int]) -> float | None:
    if not values:
        return None
    mean = statistics.mean(values)
    return statistics.pstdev(values) / mean if mean else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _counter_value(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def _format_float(value: float) -> str:
    return f"{value:.12f}".rstrip("0").rstrip(".") or "0"
