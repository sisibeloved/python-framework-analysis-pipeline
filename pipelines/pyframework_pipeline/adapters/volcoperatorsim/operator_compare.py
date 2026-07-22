"""Cross-platform comparison and reporting for Volc operator evidence."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...contracts.operator import OperatorDataset, OperatorRecord


def compare_operator_platforms(
    arm_dir: Path, x86_dir: Path, output_dir: Path
) -> dict[str, Any]:
    """Compare like-for-like operator records with explicit conclusion gates."""

    arm_records = OperatorDataset.read_jsonl(
        arm_dir / "operators" / "operator-records.jsonl"
    ).records
    x86_records = OperatorDataset.read_jsonl(
        x86_dir / "operators" / "operator-records.jsonl"
    ).records
    arm_index = _index_records(arm_records)
    x86_index = _index_records(x86_records)
    operator_keys = sorted(
        {
            _operator_key(record)
            for record in (*arm_records, *x86_records)
            if record.measurement_scope == "operator_case_e2e"
        }
        | _plan_operator_keys(arm_dir)
        | _plan_operator_keys(x86_dir)
    )

    operators: list[dict[str, Any]] = []
    for key in operator_keys:
        arm_isolated = arm_index.get((*key, "operator_case_e2e"))
        x86_isolated = x86_index.get((*key, "operator_case_e2e"))
        arm_context = arm_index.get((*key, "pipeline_context"))
        x86_context = x86_index.get((*key, "pipeline_context"))
        arm_perf = arm_index.get((*key, "operator_case_perf"))
        x86_perf = x86_index.get((*key, "operator_case_perf"))
        fingerprints = (
            arm_isolated.input_fingerprint if arm_isolated else "",
            x86_isolated.input_fingerprint if x86_isolated else "",
        )
        input_parity = bool(fingerprints[0]) and fingerprints[0] == fingerprints[1]
        arm_ns = _median_ns(arm_isolated)
        x86_ns = _median_ns(x86_isolated)
        diagnostic_ratio = (
            x86_ns / arm_ns
            if input_parity and arm_ns not in (None, 0) and x86_ns is not None
            else None
        )
        formal_allowed = bool(
            input_parity
            and arm_isolated is not None
            and x86_isolated is not None
            and arm_isolated.quality.formal_conclusion_allowed
            and x86_isolated.quality.formal_conclusion_allowed
        )
        flags: list[str] = []
        if not input_parity:
            flags.append("input_fingerprint_mismatch")
        if not formal_allowed:
            flags.append("diagnostic_only")
        if arm_isolated is None or x86_isolated is None:
            flags.append("platform_record_missing")

        operators.append(
            {
                "pipelineId": key[0],
                "taskSpecId": key[1],
                "engineId": key[2],
                "operatorCaseId": key[3],
                "operatorId": key[4],
                "operatorOrder": key[5],
                "inputParity": input_parity,
                "armInputFingerprint": fingerprints[0],
                "x86InputFingerprint": fingerprints[1],
                "armContextNs": _context_ns(arm_context),
                "x86ContextNs": _context_ns(x86_context),
                "armIsolatedMedianNs": arm_ns,
                "x86IsolatedMedianNs": x86_ns,
                "diagnosticX86OverArm": diagnostic_ratio,
                "formalSpeedup": diagnostic_ratio if formal_allowed else None,
                "comparisonFlags": sorted(set(flags)),
                "armQuality": _quality(arm_isolated),
                "x86Quality": _quality(x86_isolated),
                "armResources": _resources(arm_isolated),
                "x86Resources": _resources(x86_isolated),
                "diagnosticX86OverArmPeakRss": _resource_ratio(
                    arm_isolated, x86_isolated, "peak_tree_rss_bytes"
                ),
                "diagnosticX86OverArmCpuTime": _resource_ratio(
                    arm_isolated, x86_isolated, "tree_cpu_time_ns"
                ),
                "armPerfStat": dict(arm_isolated.perf_stat) if arm_isolated else {},
                "x86PerfStat": dict(x86_isolated.perf_stat) if x86_isolated else {},
                "armPerfCategoryPeriodShare": _perf_distribution(arm_perf),
                "x86PerfCategoryPeriodShare": _perf_distribution(x86_perf),
                "armPerfTopSymbols": _perf_top_symbols(arm_perf),
                "x86PerfTopSymbols": _perf_top_symbols(x86_perf),
                "armPerfArtifacts": _perf_artifacts(arm_perf),
                "x86PerfArtifacts": _perf_artifacts(x86_perf),
                "perfSemantics": "sampled_cpu_time_distribution",
            }
        )

    model = {
        "schemaVersion": 1,
        "comparisonSemantics": {
            "diagnosticX86OverArm": "x86 median wall-clock / ARM median wall-clock",
            "formalSpeedupGate": (
                "input parity and formalConclusionAllowed on both platforms"
            ),
            "perfPeriodShare": "sampled CPU-time distribution, not wall-clock",
        },
        "pipelineE2E": _compare_pipeline_timing(arm_dir, x86_dir),
        "operators": operators,
        "engineComparisons": _compare_engines(
            {"arm": arm_records, "x86": x86_records}
        ),
        "qualitySummary": _quality_summary(operators),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "operator-compare.json").write_text(
        json.dumps(model, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_compare_csv(output_dir / "operator-compare.csv", operators)
    _write_report(output_dir / "operator-compare.md", model)
    return model


def _operator_key(record: OperatorRecord) -> tuple[str, str, str, str, str, int]:
    return (
        record.pipeline_id,
        record.task_spec_id,
        record.engine_id,
        record.operator_case_id,
        record.operator_id,
        record.order,
    )


def _plan_operator_keys(
    platform_dir: Path,
) -> set[tuple[str, str, str, str, str, int]]:
    path = platform_dir / "operators" / "operator-plan.json"
    if not path.is_file():
        return set()
    plan = json.loads(path.read_text(encoding="utf-8"))
    keys: set[tuple[str, str, str, str, str, int]] = set()
    for task in plan.get("tasks") or []:
        pipeline_id = str(task.get("pipelineId") or "")
        task_spec_id = str(task.get("taskSpecId") or pipeline_id)
        task_engines = task.get("engines") or []
        for operator in task.get("operators") or []:
            if str(operator.get("isolationStatus") or "supported") != "supported":
                continue
            engines = (
                operator.get("engines")
                if isinstance(operator.get("engines"), list)
                else task_engines
            )
            for engine in engines:
                keys.add(
                    (
                        pipeline_id,
                        task_spec_id,
                        str(engine),
                        str(operator.get("operatorCaseId") or ""),
                        str(operator.get("operatorId") or ""),
                        int(operator.get("order", -1)),
                    )
                )
    return keys


def _index_records(
    records: Iterable[OperatorRecord],
) -> dict[tuple[str, str, str, str, str, int, str], OperatorRecord]:
    return {
        (*_operator_key(record), record.measurement_scope): record
        for record in records
    }


def _median_ns(record: OperatorRecord | None) -> int | None:
    if record is None:
        return None
    return record.timing.median_ns or record.timing.runner_elapsed_ns


def _context_ns(record: OperatorRecord | None) -> int | None:
    return record.timing.pipeline_context_ns if record is not None else None


def _quality(record: OperatorRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "grade": record.quality.grade,
        "flags": list(record.quality.flags),
        "formalConclusionAllowed": record.quality.formal_conclusion_allowed,
    }


def _resources(record: OperatorRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "treeCpuTimeNs": record.resources.tree_cpu_time_ns,
        "peakTreeRssBytes": record.resources.peak_tree_rss_bytes,
        "meanCoresBusy": record.resources.mean_cores_busy,
        "sampleCount": record.resources.sample_count,
    }


def _resource_ratio(
    arm: OperatorRecord | None,
    x86: OperatorRecord | None,
    field: str,
) -> float | None:
    if arm is None or x86 is None:
        return None
    arm_value = getattr(arm.resources, field)
    x86_value = getattr(x86.resources, field)
    if arm_value in (None, 0) or x86_value is None:
        return None
    return float(x86_value) / float(arm_value)


def _compare_engines(
    platforms: Mapping[str, Iterable[OperatorRecord]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for platform, records_value in platforms.items():
        records = [
            record
            for record in records_value
            if record.measurement_scope == "operator_case_e2e"
        ]
        grouped: dict[
            tuple[str, str, str, str, int], dict[str, OperatorRecord]
        ] = {}
        for record in records:
            key = (
                record.pipeline_id,
                record.task_spec_id,
                record.operator_case_id,
                record.operator_id,
                record.order,
            )
            grouped.setdefault(key, {})[record.engine_id] = record
        for key, engines in sorted(grouped.items()):
            daft = engines.get("daft_ray")
            datajuicer = engines.get("datajuicer_native")
            daft_ns = _median_ns(daft)
            datajuicer_ns = _median_ns(datajuicer)
            parity = bool(
                daft
                and datajuicer
                and daft.input_fingerprint
                and daft.input_fingerprint == datajuicer.input_fingerprint
            )
            diagnostic = (
                datajuicer_ns / daft_ns
                if parity
                and daft_ns not in (None, 0)
                and datajuicer_ns is not None
                else None
            )
            formal = bool(
                parity
                and daft
                and datajuicer
                and daft.quality.formal_conclusion_allowed
                and datajuicer.quality.formal_conclusion_allowed
            )
            flags: list[str] = []
            if daft is None or datajuicer is None:
                flags.append("engine_record_missing")
            if not parity:
                flags.append("input_fingerprint_mismatch")
            if not formal:
                flags.append("diagnostic_only")
            rows.append(
                {
                    "platformId": platform,
                    "pipelineId": key[0],
                    "taskSpecId": key[1],
                    "operatorCaseId": key[2],
                    "operatorId": key[3],
                    "operatorOrder": key[4],
                    "inputParity": parity,
                    "daftMedianNs": daft_ns,
                    "dataJuicerMedianNs": datajuicer_ns,
                    "diagnosticDataJuicerOverDaft": diagnostic,
                    "formalDataJuicerOverDaft": diagnostic if formal else None,
                    "comparisonFlags": sorted(set(flags)),
                }
            )
    return rows


def _quality_summary(operators: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operatorRows": len(operators),
        "inputParityPassed": sum(bool(row["inputParity"]) for row in operators),
        "formalSpeedups": sum(row["formalSpeedup"] is not None for row in operators),
        "diagnosticRatios": sum(
            row["diagnosticX86OverArm"] is not None for row in operators
        ),
        "missingPlatformRows": sum(
            "platform_record_missing" in row["comparisonFlags"]
            for row in operators
        ),
    }


def _perf_distribution(record: OperatorRecord | None) -> dict[str, float]:
    if record is None:
        return {}
    raw = record.metadata.get("categoryPeriodShare") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): float(value) for key, value in raw.items()}


def _perf_top_symbols(record: OperatorRecord | None) -> list[dict[str, Any]]:
    if record is None:
        return []
    raw = record.metadata.get("topSymbols") or []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _perf_artifacts(record: OperatorRecord | None) -> list[str]:
    if record is None:
        return []
    return list(record.source_artifacts)


def _compare_pipeline_timing(
    arm_dir: Path, x86_dir: Path
) -> list[dict[str, Any]]:
    arm = _timing_cases(arm_dir / "timing" / "timing-normalized.json")
    x86 = _timing_cases(x86_dir / "timing" / "timing-normalized.json")
    rows: list[dict[str, Any]] = []
    for case_id in sorted(set(arm) | set(x86)):
        arm_ns = arm.get(case_id)
        x86_ns = x86.get(case_id)
        rows.append(
            {
                "caseId": case_id,
                "armWallClockNs": arm_ns,
                "x86WallClockNs": x86_ns,
                "diagnosticX86OverArm": (
                    x86_ns / arm_ns
                    if arm_ns not in (None, 0) and x86_ns is not None
                    else None
                ),
                "formalSpeedup": None,
                "flags": ["diagnostic_only"],
            }
        )
    return rows


def _timing_cases(path: Path) -> dict[str, int]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for case in payload.get("cases") or []:
        value = (
            (case.get("metrics") or {})
            .get("wallClockTime", {})
            .get("wall_clock_ns")
        )
        if value is not None:
            result[str(case.get("caseId") or case.get("label") or "")] = int(value)
    return result


def _write_compare_csv(path: Path, operators: list[dict[str, Any]]) -> None:
    fields = [
        "pipelineId",
        "taskSpecId",
        "engineId",
        "operatorCaseId",
        "operatorId",
        "operatorOrder",
        "inputParity",
        "armContextNs",
        "x86ContextNs",
        "armIsolatedMedianNs",
        "x86IsolatedMedianNs",
        "diagnosticX86OverArm",
        "formalSpeedup",
        "comparisonFlags",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for source in operators:
            row = dict(source)
            row["comparisonFlags"] = ";".join(source["comparisonFlags"])
            writer.writerow(row)


def _write_report(path: Path, model: Mapping[str, Any]) -> None:
    lines = [
        "# Volc Operator Sim cross-platform report",
        "",
        "> No formal speedup is emitted unless input parity and both platform quality gates pass. Perf shares below are sampled CPU-time distributions, not wall-clock shares.",
        "",
        "## Pipeline E2E",
        "",
        "| Case | ARM ns | x86 ns | diagnostic x86/ARM |",
        "|---|---:|---:|---:|",
    ]
    for row in model.get("pipelineE2E") or []:
        lines.append(
            f"| {row['caseId']} | {_cell(row['armWallClockNs'])} | "
            f"{_cell(row['x86WallClockNs'])} | {_ratio(row['diagnosticX86OverArm'])} |"
        )

    operators = model.get("operators") or []
    lines.extend(
        [
            "",
            "## Pipeline context operator timing",
            "",
            "| Operator | Engine | ARM ns | x86 ns |",
            "|---|---|---:|---:|",
        ]
    )
    for row in operators:
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | "
            f"{_cell(row['armContextNs'])} | {_cell(row['x86ContextNs'])} |"
        )

    lines.extend(
        [
            "",
            "## Daft vs Data-Juicer",
            "",
            "| Platform | Operator | Daft median ns | Data-Juicer median ns | diagnostic DJ/Daft | formal | flags |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in model.get("engineComparisons") or []:
        lines.append(
            f"| {row['platformId']} | {row['operatorId']} | "
            f"{_cell(row['daftMedianNs'])} | {_cell(row['dataJuicerMedianNs'])} | "
            f"{_ratio(row['diagnosticDataJuicerOverDaft'])} | "
            f"{_ratio(row['formalDataJuicerOverDaft'])} | "
            f"{', '.join(row['comparisonFlags']) or '—'} |"
        )

    lines.extend(
        [
            "",
            "## Hardware counters and resources",
            "",
            "| Operator | Engine | ARM IPC | x86 IPC | x86/ARM peak RSS | x86/ARM CPU time |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in operators:
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | "
            f"{_cell(row['armPerfStat'].get('ipc'))} | "
            f"{_cell(row['x86PerfStat'].get('ipc'))} | "
            f"{_ratio(row['diagnosticX86OverArmPeakRss'])} | "
            f"{_ratio(row['diagnosticX86OverArmCpuTime'])} |"
        )

    quality = model.get("qualitySummary") or {}
    lines.extend(
        [
            "",
            "## Quality gate summary",
            "",
            f"- Operator rows: {quality.get('operatorRows', 0)}",
            f"- Input parity passed: {quality.get('inputParityPassed', 0)}",
            f"- Diagnostic ratios: {quality.get('diagnosticRatios', 0)}",
            f"- Formal speedups: {quality.get('formalSpeedups', 0)}",
            f"- Missing platform rows: {quality.get('missingPlatformRows', 0)}",
        ]
    )

    lines.extend(
        [
            "",
            "## Isolated operator E2E",
            "",
            "| Operator | Engine | ARM median ns | x86 median ns | diagnostic x86/ARM | formal speedup | flags |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in operators:
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | "
            f"{_cell(row['armIsolatedMedianNs'])} | {_cell(row['x86IsolatedMedianNs'])} | "
            f"{_ratio(row['diagnosticX86OverArm'])} | {_ratio(row['formalSpeedup'])} | "
            f"{', '.join(row['comparisonFlags']) or '—'} |"
        )

    lines.extend(
        [
            "",
            "## Perf CPU-time distribution",
            "",
            "| Operator | Engine | ARM category period share | x86 category period share |",
            "|---|---|---|---|",
        ]
    )
    for row in operators:
        arm_dist = _distribution_cell(row["armPerfCategoryPeriodShare"])
        x86_dist = _distribution_cell(row["x86PerfCategoryPeriodShare"])
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | {arm_dist} | {x86_dist} |"
        )

    lines.extend(
        [
            "",
            "## Top perf symbols",
            "",
            "| Operator | Engine | ARM top symbols | x86 top symbols |",
            "|---|---|---|---|",
        ]
    )
    for row in operators:
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | "
            f"{_symbols_cell(row['armPerfTopSymbols'])} | "
            f"{_symbols_cell(row['x86PerfTopSymbols'])} |"
        )

    lines.extend(
        [
            "",
            "## Perf / ASM / flamegraph evidence",
            "",
            "| Operator | Engine | ARM artifacts | x86 artifacts |",
            "|---|---|---|---|",
        ]
    )
    for row in operators:
        lines.append(
            f"| {row['operatorId']} | {row['engineId']} | "
            f"{_artifacts_cell(row['armPerfArtifacts'], '../arm/')} | "
            f"{_artifacts_cell(row['x86PerfArtifacts'], '../x86/')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cell(value: Any) -> str:
    return "—" if value is None else str(value)


def _ratio(value: Any) -> str:
    return "—" if value is None else f"{float(value):.4f}"


def _distribution_cell(value: Mapping[str, float]) -> str:
    if not value:
        return "—"
    return "; ".join(
        f"{key}: {float(share):.2%}"
        for key, share in sorted(value.items(), key=lambda item: item[1], reverse=True)
    )


def _symbols_cell(value: Iterable[Mapping[str, Any]]) -> str:
    symbols = list(value)
    if not symbols:
        return "—"
    return "; ".join(
        f"{item.get('symbol') or '[unknown]'}: "
        f"{float(item.get('periodShare') or 0.0):.2%}"
        for item in symbols
    )


def _artifacts_cell(value: Iterable[str], prefix: str) -> str:
    artifacts = list(value)
    if not artifacts:
        return "—"
    return "; ".join(
        f"[{Path(artifact).name}]({prefix}{artifact})" for artifact in artifacts
    )
