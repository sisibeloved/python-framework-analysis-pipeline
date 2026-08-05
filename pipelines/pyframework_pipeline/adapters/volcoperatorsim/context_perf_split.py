"""Split one pipeline-context ``perf record`` by exact operator boundaries.

The target runner publishes epoch-based operator boundaries while Linux perf
stores CLOCK_MONOTONIC timestamps.  A clock-sync record captured immediately
before the run supplies the stable offset between those clocks.  The resulting
period reports retain the normal isolated-case names, so existing normalization
and reporting code can consume them without pretending that the operators were
replayed in isolation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class OperatorWindow:
    order: int
    operator_id: str
    operator_case_id: str
    case_hash: str
    start_seconds: float
    end_seconds: float


def clock_offset_seconds(clock_sync: Mapping[str, Any]) -> float:
    """Return ``epoch - monotonic`` from one adjacent clock read pair."""

    epoch = float(clock_sync["epochSeconds"])
    monotonic = float(clock_sync["monotonicSeconds"])
    if epoch <= 0 or monotonic < 0:
        raise ValueError("invalid context perf clock sync")
    return epoch - monotonic


def build_operator_windows(
    *,
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
    clock_sync: Mapping[str, Any],
    pipeline_id: str,
    engine: str,
) -> list[OperatorWindow]:
    """Join plan identities to runner boundaries and map them to perf time."""

    task = next(
        (
            item
            for item in plan.get("tasks") or []
            if str(item.get("pipelineId") or "") == pipeline_id
        ),
        None,
    )
    if not isinstance(task, Mapping):
        raise ValueError(f"pipeline not found in operator plan: {pipeline_id}")
    metrics = result.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("runner result has no metrics")
    timeline_epoch = float(metrics.get("timeline_t0_epoch") or 0.0)
    if timeline_epoch <= 0:
        raise ValueError("runner result has no timeline_t0_epoch")
    boundaries = {
        int(item["order"]): item
        for item in metrics.get("op_boundaries") or []
        if isinstance(item, Mapping) and item.get("order") is not None
    }
    perf_t0 = timeline_epoch - clock_offset_seconds(clock_sync)
    windows: list[OperatorWindow] = []
    for operator in task.get("operators") or []:
        if not isinstance(operator, Mapping):
            continue
        engines = {str(value) for value in operator.get("engines") or []}
        if engines and engine not in engines:
            continue
        order = int(operator["order"])
        boundary = boundaries.get(order)
        if not isinstance(boundary, Mapping):
            raise ValueError(f"missing boundary for operator order {order}")
        operator_id = str(operator.get("operatorId") or "")
        boundary_id = str(boundary.get("dj_ops") or "")
        if boundary_id and operator_id and boundary_id != operator_id:
            raise ValueError(
                f"boundary operator mismatch at order {order}: "
                f"plan={operator_id} runner={boundary_id}"
            )
        start = perf_t0 + float(boundary["start_offset_s"])
        end = perf_t0 + float(boundary["end_offset_s"])
        if end <= start:
            raise ValueError(f"invalid boundary for operator order {order}")
        operator_case_id = str(operator["operatorCaseId"])
        windows.append(
            OperatorWindow(
                order=order,
                operator_id=operator_id,
                operator_case_id=operator_case_id,
                case_hash=hashlib.sha256(operator_case_id.encode("utf-8")).hexdigest()[:12],
                start_seconds=start,
                end_seconds=end,
            )
        )
    if not windows:
        raise ValueError(f"operator plan has no windows for {pipeline_id}/{engine}")
    return windows


def _latest(paths: Sequence[Path], label: str) -> Path:
    if not paths:
        raise FileNotFoundError(label)
    return max(paths, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def _runner_result(context_root: Path, pipeline_id: str, engine: str) -> Path:
    candidates: list[Path] = []
    for path in context_root.glob("runner/**/*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = str(payload.get("task_id") or "")
        if (
            payload.get("engine_id") == engine
            and (task_id == pipeline_id or task_id.startswith(pipeline_id + "@"))
            and payload.get("status") == "ok"
            and isinstance(payload.get("metrics"), Mapping)
        ):
            candidates.append(path)
    return _latest(candidates, "pipeline-context runner result")


def _context_perf_data(
    context_root: Path, pipeline_id: str, engine: str
) -> Path:
    """Select the capture produced by this exact pipeline/engine pair."""

    expected_case = f"pipeline_context__{pipeline_id}__{engine}"
    candidates = [
        path
        for path in context_root.glob("**/perf.data")
        if path.parent.name == expected_case
        and path.parent.parent.name == engine
    ]
    return _latest(
        candidates,
        f"context perf.data for {pipeline_id}/{engine}",
    )


def _sample_count(report: Path) -> int:
    count = 0
    for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "|" not in line:
            continue
        fields = line.split("|")
        if len(fields) < 3:
            continue
        value = re.sub(r"[^0-9]", "", fields[2])
        if value:
            count += int(value)
    return count


def compact_period_report(source: Path, output: Path) -> dict[str, int]:
    """Aggregate resolved perf rows at the report consumer's exact grain."""

    grouped: dict[tuple[str, str, str, str], list[int]] = {}
    rows_before = 0
    total_period = 0
    total_samples = 0
    for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
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
        key = (fields[3], fields[4], fields[5], "|".join(fields[6:]))
        totals = grouped.setdefault(key, [0, 0])
        totals[0] += period
        totals[1] += samples
        rows_before += 1
        total_period += period
        total_samples += samples
    temporary = output.with_name("." + output.name + ".partial")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write("# compacted resolved perf period report\n")
        handle.write("# overhead|period|sample|comm|pid|dso|symbol\n")
        for key, totals in sorted(
            grouped.items(), key=lambda item: (-item[1][0], item[0])
        ):
            share = totals[0] / total_period * 100 if total_period else 0.0
            handle.write(
                "|".join(
                    (
                        f"{share:.6f}%",
                        str(totals[0]),
                        str(totals[1]),
                        *key,
                    )
                )
                + "\n"
            )
    os.replace(temporary, output)
    return {
        "rowsBefore": rows_before,
        "rowsAfter": len(grouped),
        "totalPeriod": total_period,
        "totalSamples": total_samples,
    }


def _perf_report_command(
    *,
    real_perf: str,
    perf_data: Path,
    start_seconds: float,
    end_seconds: float,
    buildid_dir: Path | None,
) -> list[str]:
    command = [real_perf]
    if buildid_dir is not None:
        command.extend(("--buildid-dir", str(buildid_dir)))
    command.extend(
        (
            "report",
            "--stdio",
            "--no-children",
            "--show-total-period",
            f"--time={start_seconds:.9f},{end_seconds:.9f}",
            "--field-separator=|",
            "--fields=overhead,period,sample,comm,pid,dso,symbol,addr",
            "-i",
            str(perf_data),
        )
    )
    return command


def split_context_perf(
    *,
    context_root: Path,
    output_root: Path,
    plan_path: Path,
    clock_sync_path: Path,
    pipeline_id: str,
    engine: str,
    python: str,
    symbolizer: Path,
    real_perf: str = "/usr/bin/perf",
    buildid_dir: Path | None = None,
    arch: str = "",
) -> list[Path]:
    """Materialize symbol-resolved per-operator reports from one perf.data."""

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result_path = _runner_result(context_root, pipeline_id, engine)
    result = json.loads(result_path.read_text(encoding="utf-8"))
    clock_sync = json.loads(clock_sync_path.read_text(encoding="utf-8"))
    windows = build_operator_windows(
        plan=plan,
        result=result,
        clock_sync=clock_sync,
        pipeline_id=pipeline_id,
        engine=engine,
    )
    perf_data = _context_perf_data(context_root, pipeline_id, engine)
    dso_manifest = perf_data.with_name("perf-dso-manifest.json")
    if not dso_manifest.is_file():
        raise FileNotFoundError(dso_manifest)
    timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    arch_id = arch or platform.machine()
    raw_index_cache = (
        buildid_dir.parent
        / (
            "raw-sample-index-"
            + hashlib.sha256(
                (
                    str(perf_data.resolve())
                    + f":{perf_data.stat().st_size}:{perf_data.stat().st_mtime_ns}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            + ".json"
        )
        if buildid_dir is not None
        else None
    )
    outputs: list[Path] = []
    for window in windows:
        case = f"operator_case_perf__{window.case_hash}__context_window_001"
        directory = output_root / timestamp / arch_id / engine / case
        directory.mkdir(parents=True, exist_ok=True)
        report = directory / "perf-report-period.txt"
        command = _perf_report_command(
            real_perf=real_perf,
            perf_data=perf_data,
            start_seconds=window.start_seconds,
            end_seconds=window.end_seconds,
            buildid_dir=buildid_dir,
        )
        completed = subprocess.run(command, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RuntimeError(
                f"perf report failed for {window.operator_id}: {completed.stderr[-1000:]}"
            )
        temporary = report.with_name("." + report.name + ".partial")
        temporary.write_text(completed.stdout, encoding="utf-8")
        os.replace(temporary, report)
        resolved = directory / "perf-report-period-resolved.txt"
        resolve_command = [
            python,
            str(symbolizer),
            "resolve",
            "--source",
            str(report),
            "--manifest",
            str(dso_manifest),
            "--output",
            str(resolved),
            "--perf-data",
            str(perf_data),
            "--real-perf",
            real_perf,
            "--require-complete",
        ]
        if buildid_dir is not None:
            resolve_command.extend(("--buildid-dir", str(buildid_dir)))
        if raw_index_cache is not None:
            resolve_command.extend(
                ("--raw-index-cache", str(raw_index_cache))
            )
        subprocess.run(resolve_command, check=True)
        full_resolved = directory / "perf-report-period-resolved-full.txt"
        os.replace(resolved, full_resolved)
        compaction = compact_period_report(full_resolved, resolved)
        buildids = perf_data.with_name("perf-buildid-list.txt")
        if buildids.is_file():
            shutil.copy2(buildids, directory / buildids.name)
        summary = {
            "case": case,
            "engine": engine,
            "arch": arch_id,
            "timestamp": timestamp,
            "returncode": 0,
            "status": "ok",
            "operatorOrder": window.order,
            "operatorId": window.operator_id,
            "operatorCaseId": window.operator_case_id,
            "sampleCount": _sample_count(resolved),
            "captureMode": "single_pass_context_perf_window",
            "sourcePerfData": str(perf_data),
            "sourceRunnerResult": str(result_path),
            "perfTimeWindow": {
                "startSeconds": window.start_seconds,
                "endSeconds": window.end_seconds,
            },
            "reportCompaction": compaction,
        }
        (directory / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        outputs.append(directory)
    return outputs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--clock-sync", type=Path, required=True)
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--symbolizer", type=Path, required=True)
    parser.add_argument("--real-perf", default="/usr/bin/perf")
    parser.add_argument("--buildid-dir", type=Path)
    parser.add_argument("--arch", default="")
    args = parser.parse_args(argv)
    outputs = split_context_perf(
        context_root=args.context_root,
        output_root=args.output_root,
        plan_path=args.plan,
        clock_sync_path=args.clock_sync,
        pipeline_id=args.pipeline_id,
        engine=args.engine,
        python=args.python,
        symbolizer=args.symbolizer,
        real_perf=args.real_perf,
        buildid_dir=args.buildid_dir,
        arch=args.arch,
    )
    print(json.dumps({"status": "complete", "operatorWindows": len(outputs)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
