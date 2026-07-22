"""Pure normalization and quality rules for Volc operator evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ...contracts.operator import (
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
    build_operator_case_id,
)


@dataclass(frozen=True)
class ResourceWindowEstimate:
    window_cpu_time_ns_estimate: int
    window_mean_cores_busy_estimate: float
    window_peak_tree_rss_bytes_estimate: int
    sample_count: int
    sample_interval_ns: int


def attribute_resource_window(
    *,
    samples: Iterable[Mapping[str, Any]],
    timeline_t0_epoch: float,
    start_offset_s: float,
    end_offset_s: float,
    sample_interval_s: float,
) -> ResourceWindowEstimate | None:
    """Estimate CPU time and RSS for a Daft operator boundary.

    A proc-sampler row at ``t`` represents the interval ``(t-interval, t]``.
    Samples are therefore weighted by interval overlap instead of assigning a
    whole sample to whichever boundary happens to contain its timestamp.
    """

    if sample_interval_s <= 0 or end_offset_s <= start_offset_s:
        return None
    window_start = timeline_t0_epoch + start_offset_s
    window_end = timeline_t0_epoch + end_offset_s
    duration_s = window_end - window_start
    cpu_time_s = 0.0
    peak_rss_mb = 0.0
    sample_count = 0

    for sample in samples:
        try:
            sample_end = float(sample["t"])
            cpu_pct = float(sample["tree_cpu_pct"])
            rss_mb = float(sample["tree_rss_mb"])
        except (KeyError, TypeError, ValueError):
            continue
        sample_start = sample_end - sample_interval_s
        overlap_s = max(
            0.0,
            min(sample_end, window_end) - max(sample_start, window_start),
        )
        if overlap_s <= 0:
            continue
        cpu_time_s += cpu_pct / 100.0 * overlap_s
        peak_rss_mb = max(peak_rss_mb, rss_mb)
        sample_count += 1

    if sample_count == 0:
        return None
    cpu_time_ns = round(cpu_time_s * 1_000_000_000)
    duration_ns = round(duration_s * 1_000_000_000)
    return ResourceWindowEstimate(
        window_cpu_time_ns_estimate=cpu_time_ns,
        window_mean_cores_busy_estimate=cpu_time_ns / duration_ns,
        window_peak_tree_rss_bytes_estimate=round(peak_rss_mb * 1024 * 1024),
        sample_count=sample_count,
        sample_interval_ns=round(sample_interval_s * 1_000_000_000),
    )


def evaluate_operator_quality(
    *,
    timing_source: str,
    input_parity: bool | None,
    perf_lock_passed: bool,
    sample_count: int,
    min_perf_samples: int,
    unblock_perf: bool,
    representative_profile: bool,
    perf_lock_warned: bool = False,
) -> OperatorQuality:
    """Apply the operator evidence quality and conclusion gates."""

    grade = "A"
    flags: list[str] = []

    if timing_source == "estimated_even_split":
        grade = "C"
        flags.append("estimated_timing")
    elif timing_source == "data_juicer_log":
        grade = "B"
        flags.append("log_timing")
    elif timing_source not in {
        "daft_collect_boundary",
        "isolated_operator_timing",
    }:
        grade = "B"
        flags.append("timing_source_unverified")

    if input_parity is False:
        grade = "C"
        flags.append("input_parity_failed")
    elif input_parity is None:
        if grade == "A":
            grade = "B"
        flags.append("input_parity_unverified")
    if not perf_lock_passed:
        grade = "C"
        flags.append("perf_lock_failed")
    elif perf_lock_warned:
        flags.append("perf_lock_warn")
    if sample_count < min_perf_samples:
        if grade == "A":
            grade = "B"
        flags.append("insufficient_samples")

    if not unblock_perf or not representative_profile:
        flags.append("diagnostic_only")

    formal_allowed = (
        grade == "A"
        and input_parity is True
        and perf_lock_passed
        and sample_count >= min_perf_samples
        and unblock_perf
        and representative_profile
    )
    return OperatorQuality(
        grade=grade,
        flags=tuple(flags),
        formal_conclusion_allowed=formal_allowed,
    )


def parse_context_records(
    *,
    result: Mapping[str, Any],
    task_document: Mapping[str, Any],
    run_id: str,
    platform_id: str,
    pipeline_id: str,
    samples: Iterable[Mapping[str, Any]],
    sample_interval_s: float,
    unblock_perf: bool,
    representative_profile: bool,
) -> tuple[OperatorRecord, ...]:
    """Normalize target ``operator_timings`` into context-scoped records."""

    metrics = result.get("metrics") or {}
    engine_id = str(result.get("engine_id") or result.get("engineId") or "")
    arch = str((result.get("resources") or {}).get("arch") or "")
    task_spec_id = str(task_document.get("task_id") or pipeline_id)
    task_pipeline = task_document.get("pipeline") or []
    input_fingerprint = str(metrics.get("input_fingerprint") or "")
    perf_lock = result.get("perf_lock") or {}
    perf_lock_status = str(perf_lock.get("status") or "")
    perf_lock_passed = perf_lock_status in {"pass", "warn"}
    timeline_t0_epoch = _optional_float(metrics.get("timeline_t0_epoch"))
    sample_rows = tuple(samples)

    boundaries: dict[tuple[int, str], Mapping[str, Any]] = {}
    for raw_boundary in metrics.get("op_boundaries") or []:
        if not isinstance(raw_boundary, Mapping):
            continue
        key = (
            int(raw_boundary.get("order", -1)),
            str(raw_boundary.get("dj_ops") or ""),
        )
        boundaries[key] = raw_boundary

    records: list[OperatorRecord] = []
    for raw_item in metrics.get("operator_timings") or []:
        if not isinstance(raw_item, Mapping):
            continue
        order = int(raw_item.get("order", len(records)))
        operator_id = str(raw_item.get("dj_ops") or "")
        if not operator_id:
            continue
        step = task_pipeline[order] if 0 <= order < len(task_pipeline) else {}
        params = step.get("params") if isinstance(step, Mapping) else {}
        boundary = boundaries.get((order, operator_id), raw_item)
        start_offset_s = _optional_float(boundary.get("start_offset_s"))
        end_offset_s = _optional_float(boundary.get("end_offset_s"))
        boundary_valid = (
            start_offset_s is not None
            and end_offset_s is not None
            and end_offset_s > start_offset_s
        )

        declared_source = str(
            raw_item.get("source") or raw_item.get("timing_method") or ""
        )
        if engine_id == "daft_ray" and boundary_valid:
            timing_source = "daft_collect_boundary"
        else:
            timing_source = declared_source or "unknown"
        elapsed_ns = _seconds_to_ns(raw_item.get("elapsed_s"))
        is_estimated = timing_source == "estimated_even_split"

        window = None
        if engine_id == "daft_ray" and boundary_valid and timeline_t0_epoch is not None:
            window = attribute_resource_window(
                samples=sample_rows,
                timeline_t0_epoch=timeline_t0_epoch,
                start_offset_s=start_offset_s,
                end_offset_s=end_offset_s,
                sample_interval_s=sample_interval_s,
            )

        quality = evaluate_operator_quality(
            timing_source=timing_source,
            input_parity=True,
            perf_lock_passed=perf_lock_passed,
            sample_count=0,
            min_perf_samples=0,
            unblock_perf=unblock_perf,
            representative_profile=representative_profile,
            perf_lock_warned=perf_lock_status == "warn",
        )
        resources = OperatorResources()
        if window is not None:
            resources = OperatorResources(
                window_cpu_time_ns_estimate=window.window_cpu_time_ns_estimate,
                window_mean_cores_busy_estimate=(
                    window.window_mean_cores_busy_estimate
                ),
                window_peak_tree_rss_bytes_estimate=(
                    window.window_peak_tree_rss_bytes_estimate
                ),
                sample_count=window.sample_count,
                sample_interval_ns=window.sample_interval_ns,
            )
        metadata = {
            "category": str(raw_item.get("category") or "unknown"),
            "timingMethod": str(raw_item.get("timing_method") or ""),
            "executionGroup": str(raw_item.get("execution_group") or ""),
            "executionGroupLeader": raw_item.get("execution_group_leader"),
            "layerTimings": dict(raw_item.get("layer_timings") or {}),
            "perfLockStatus": perf_lock_status,
            "perfLockWarningCodes": _perf_lock_codes(
                perf_lock.get("warnings")
            ),
            "perfLockViolationCodes": _perf_lock_codes(
                perf_lock.get("violations")
            ),
        }
        records.append(
            OperatorRecord(
                run_id=run_id,
                platform_id=platform_id,
                arch=arch,
                pipeline_id=pipeline_id,
                task_spec_id=task_spec_id,
                engine_id=engine_id,
                operator_case_id=build_operator_case_id(
                    task_spec_id, order, operator_id, params or {}
                ),
                operator_id=operator_id,
                order=order,
                measurement_scope="pipeline_context",
                input_fingerprint=input_fingerprint,
                timing=OperatorTiming(
                    pipeline_context_ns=None if is_estimated else elapsed_ns,
                    estimated_elapsed_ns=elapsed_ns if is_estimated else None,
                    timing_source=timing_source,
                    start_offset_ns=(
                        _seconds_to_ns(start_offset_s) if boundary_valid else None
                    ),
                    end_offset_ns=(
                        _seconds_to_ns(end_offset_s) if boundary_valid else None
                    ),
                    rounds=1,
                ),
                resources=resources,
                quality=quality,
                metadata=metadata,
            )
        )
    return tuple(records)


def _perf_lock_codes(items: Any) -> list[str]:
    if not isinstance(items, (list, tuple)):
        return []
    return [
        str(item.get("code"))
        for item in items
        if isinstance(item, Mapping) and item.get("code")
    ]


def _seconds_to_ns(value: Any) -> int | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    return round(parsed * 1_000_000_000)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
