"""Operator-level timing, resource, and quality contracts.

Operator facts are deliberately separate from pipeline timing and generic perf
records.  Each JSONL row carries an explicit measurement scope so context
timing, isolated E2E timing, and sampled CPU distributions cannot be mixed by
accident.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


def build_operator_case_id(
    task_spec_id: str,
    order: int,
    operator_id: str,
    params: Mapping[str, Any] | None,
) -> str:
    """Build the stable human-readable identity for one operator case."""

    _validate_identity("task_spec_id", task_spec_id)
    _validate_identity("operator_id", operator_id)
    if order < 0:
        raise ValueError("order must be non-negative")
    canonical = json.dumps(
        dict(params or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    params_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{task_spec_id}::{order:03d}::{operator_id}::{params_hash}"


def _validate_identity(name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{name} must not be empty")
    if not _IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{name} contains unsafe characters: {value!r}")


@dataclass(frozen=True)
class OperatorCapabilities:
    context_timing: bool = False
    isolated_timing: bool = False
    operator_perf: bool = False
    operator_flamegraph: bool = False
    operator_asm: bool = False
    stage_snapshot: bool = False


@dataclass(frozen=True)
class OperatorQuality:
    grade: str = "C"
    flags: tuple[str, ...] = ()
    formal_conclusion_allowed: bool = False

    def __post_init__(self) -> None:
        if self.grade not in {"A", "B", "C"}:
            raise ValueError(f"unsupported operator quality grade: {self.grade}")


@dataclass(frozen=True)
class OperatorTiming:
    pipeline_context_ns: int | None = None
    estimated_elapsed_ns: int | None = None
    timing_source: str = ""
    start_offset_ns: int | None = None
    end_offset_ns: int | None = None
    outer_process_wall_ns: int | None = None
    runner_elapsed_ns: int | None = None
    isolated_operator_ns: int | None = None
    residual_ns: int | None = None
    median_ns: int | None = None
    p95_ns: int | None = None
    stddev_ns: int | None = None
    cv: float | None = None
    rounds: int = 0


@dataclass(frozen=True)
class OperatorResources:
    tree_cpu_time_ns: int | None = None
    peak_tree_rss_bytes: int | None = None
    mean_cores_busy: float | None = None
    window_cpu_time_ns_estimate: int | None = None
    window_mean_cores_busy_estimate: float | None = None
    window_peak_tree_rss_bytes_estimate: int | None = None
    sample_count: int = 0
    sample_interval_ns: int | None = None


@dataclass(frozen=True)
class OperatorRecord:
    run_id: str = ""
    platform_id: str = ""
    arch: str = ""
    pipeline_id: str = ""
    task_spec_id: str = ""
    engine_id: str = ""
    operator_case_id: str = ""
    operator_id: str = ""
    order: int = 0
    measurement_scope: str = ""
    input_fingerprint: str = ""
    timing: OperatorTiming = field(default_factory=OperatorTiming)
    resources: OperatorResources = field(default_factory=OperatorResources)
    perf_stat: dict[str, int | float | None] = field(default_factory=dict)
    quality: OperatorQuality = field(default_factory=OperatorQuality)
    source_artifacts: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        allowed = {
            "pipeline_e2e",
            "pipeline_context",
            "operator_case_e2e",
            "operator_case_perf",
        }
        if self.measurement_scope and self.measurement_scope not in allowed:
            raise ValueError(
                f"unsupported operator measurement scope: {self.measurement_scope}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "runId": self.run_id,
            "platformId": self.platform_id,
            "arch": self.arch,
            "pipelineId": self.pipeline_id,
            "taskSpecId": self.task_spec_id,
            "engineId": self.engine_id,
            "operatorCaseId": self.operator_case_id,
            "operatorId": self.operator_id,
            "order": self.order,
            "measurementScope": self.measurement_scope,
            "inputFingerprint": self.input_fingerprint,
            "timing": {
                "pipelineContextNs": self.timing.pipeline_context_ns,
                "estimatedElapsedNs": self.timing.estimated_elapsed_ns,
                "timingSource": self.timing.timing_source,
                "startOffsetNs": self.timing.start_offset_ns,
                "endOffsetNs": self.timing.end_offset_ns,
                "outerProcessWallNs": self.timing.outer_process_wall_ns,
                "runnerElapsedNs": self.timing.runner_elapsed_ns,
                "isolatedOperatorNs": self.timing.isolated_operator_ns,
                "residualNs": self.timing.residual_ns,
                "medianNs": self.timing.median_ns,
                "p95Ns": self.timing.p95_ns,
                "stddevNs": self.timing.stddev_ns,
                "cv": self.timing.cv,
                "rounds": self.timing.rounds,
            },
            "resources": {
                "treeCpuTimeNs": self.resources.tree_cpu_time_ns,
                "peakTreeRssBytes": self.resources.peak_tree_rss_bytes,
                "meanCoresBusy": self.resources.mean_cores_busy,
                "windowCpuTimeNsEstimate": self.resources.window_cpu_time_ns_estimate,
                "windowMeanCoresBusyEstimate": self.resources.window_mean_cores_busy_estimate,
                "windowPeakTreeRssBytesEstimate": (
                    self.resources.window_peak_tree_rss_bytes_estimate
                ),
                "sampleCount": self.resources.sample_count,
                "sampleIntervalNs": self.resources.sample_interval_ns,
            },
            "perfStat": self.perf_stat,
            "quality": {
                "grade": self.quality.grade,
                "flags": list(self.quality.flags),
                "formalConclusionAllowed": self.quality.formal_conclusion_allowed,
            },
            "sourceArtifacts": list(self.source_artifacts),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OperatorRecord":
        timing = data.get("timing") or {}
        resources = data.get("resources") or {}
        quality = data.get("quality") or {}
        return cls(
            run_id=str(data.get("runId", "")),
            platform_id=str(data.get("platformId", "")),
            arch=str(data.get("arch", "")),
            pipeline_id=str(data.get("pipelineId", "")),
            task_spec_id=str(data.get("taskSpecId", "")),
            engine_id=str(data.get("engineId", "")),
            operator_case_id=str(data.get("operatorCaseId", "")),
            operator_id=str(data.get("operatorId", "")),
            order=int(data.get("order", 0)),
            measurement_scope=str(data.get("measurementScope", "")),
            input_fingerprint=str(data.get("inputFingerprint", "")),
            timing=OperatorTiming(
                pipeline_context_ns=timing.get("pipelineContextNs"),
                estimated_elapsed_ns=timing.get("estimatedElapsedNs"),
                timing_source=str(timing.get("timingSource", "")),
                start_offset_ns=timing.get("startOffsetNs"),
                end_offset_ns=timing.get("endOffsetNs"),
                outer_process_wall_ns=timing.get("outerProcessWallNs"),
                runner_elapsed_ns=timing.get("runnerElapsedNs"),
                isolated_operator_ns=timing.get("isolatedOperatorNs"),
                residual_ns=timing.get("residualNs"),
                median_ns=timing.get("medianNs"),
                p95_ns=timing.get("p95Ns"),
                stddev_ns=timing.get("stddevNs"),
                cv=timing.get("cv"),
                rounds=int(timing.get("rounds", 0)),
            ),
            resources=OperatorResources(
                tree_cpu_time_ns=resources.get("treeCpuTimeNs"),
                peak_tree_rss_bytes=resources.get("peakTreeRssBytes"),
                mean_cores_busy=resources.get("meanCoresBusy"),
                window_cpu_time_ns_estimate=resources.get(
                    "windowCpuTimeNsEstimate"
                ),
                window_mean_cores_busy_estimate=resources.get(
                    "windowMeanCoresBusyEstimate"
                ),
                window_peak_tree_rss_bytes_estimate=resources.get(
                    "windowPeakTreeRssBytesEstimate"
                ),
                sample_count=int(resources.get("sampleCount", 0)),
                sample_interval_ns=resources.get("sampleIntervalNs"),
            ),
            perf_stat=dict(data.get("perfStat") or {}),
            quality=OperatorQuality(
                grade=str(quality.get("grade", "C")),
                flags=tuple(str(flag) for flag in quality.get("flags", [])),
                formal_conclusion_allowed=bool(
                    quality.get("formalConclusionAllowed", False)
                ),
            ),
            source_artifacts=tuple(
                str(path) for path in data.get("sourceArtifacts", [])
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class OperatorDataset:
    records: tuple[OperatorRecord, ...] = ()

    def write_jsonl(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for record in self.records:
                handle.write(
                    json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True)
                    + "\n"
                )

    @classmethod
    def read_jsonl(cls, path: Path) -> "OperatorDataset":
        if not path.exists():
            return cls()
        records: list[OperatorRecord] = []
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid operator JSONL at {path}:{line_number}: {exc}"
                ) from exc
            records.append(OperatorRecord.from_dict(payload))
        return cls(records=tuple(records))
