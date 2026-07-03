"""UDF_Benchmarking framework adapter.

Implements the framework-specific acquisition strategies for the
UDF_Benchmarking benchmark. The deploy + benchmark + flamegraph + timing logic
here was moved verbatim out of the orchestrator (Phase 3 OOP refactor) so the
adapter is the single source for UDF_Benchmarking behaviour, not a shim.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ...contracts.adapter import DisassemblySpec, PerfAttachSpec, WorkloadHandle
from ...contracts.step import StepError
from ..registry import register_adapter

logger = logging.getLogger(__name__)


def _container(env_config: dict[str, Any]) -> str:
    """Container name running the UDF_Benchmarking benchmark."""
    return str(
        env_config.get("software", {}).get(
            "udfBenchmarkingContainer",
            "udf-benchmarking-bench",
        )
    )


def _config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _benchmark_name(env_config: dict[str, Any]) -> str:
    return str(env_config.get("software", {}).get("benchmarkName", "MockVideoE2EUDF"))


def _config_file(env_config: dict[str, Any]) -> str:
    config_file = str(
        env_config.get("software", {}).get("benchmarkConfigFile", "config.yaml")
    )
    if config_file.startswith("/"):
        raise StepError(
            "UDF_Benchmarking benchmarkConfigFile must be relative to /workspace/benchmark"
        )
    return config_file


def _python_flamegraph_config(env_config: dict[str, Any]) -> dict[str, Any]:
    raw = env_config.get("software", {}).get("pythonFlamegraph", {})
    if isinstance(raw, dict):
        enabled = _config_bool(raw.get("enabled", False))
        rate = int(raw.get("rate", 100) or 100)
        subprocesses = _config_bool(raw.get("subprocesses", True))
    else:
        enabled = _config_bool(raw)
        rate = 100
        subprocesses = True
    return {"enabled": enabled, "rate": rate, "subprocesses": subprocesses}


def _numeric(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _seconds_to_ns(value: float) -> int:
    return int(round(float(value) * 1_000_000_000))


def _write_timing_artifacts(
    raw_output_dir: Path,
    platform_run_dir: Path,
    platform: str,
    benchmark_name: str,
) -> None:
    """Normalize the UDF_Benchmarking CSV/summary into timing-normalized.json."""
    import csv
    import statistics

    csv_path = raw_output_dir / f"{benchmark_name}.csv"
    summary_path = raw_output_dir / f"{benchmark_name}_summary.json"
    if not csv_path.exists() and not summary_path.exists():
        raise StepError(
            f"UDF_Benchmarking output did not include {benchmark_name}.csv "
            f"or {benchmark_name}_summary.json under {raw_output_dir}"
        )

    iterations: list[dict[str, Any]] = []
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                iterations.append(dict(row))

    summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StepError(f"Invalid UDF_Benchmarking summary JSON: {summary_path}: {exc}") from exc

    def numeric_from_summary_or_rows(key: str) -> float | None:
        value = _numeric(summary.get(key))
        if value is not None:
            return value
        values = [
            row_value
            for row in iterations
            if (row_value := _numeric(row.get(key))) is not None
        ]
        if values:
            return float(statistics.fmean(values))
        return None

    wall_seconds = numeric_from_summary_or_rows("time_seconds")
    if wall_seconds is None:
        raise StepError(
            f"UDF_Benchmarking output missing time_seconds in {csv_path} or {summary_path}"
        )
    collect_seconds = numeric_from_summary_or_rows("collect_time_seconds")
    postprocess_seconds = numeric_from_summary_or_rows("postprocess_time_seconds")
    records = numeric_from_summary_or_rows("records_processed")
    record_count = int(round(records or 0))

    timing_dir = platform_run_dir / "timing"
    timing_dir.mkdir(parents=True, exist_ok=True)
    raw_path = timing_dir / "timing-raw.json"
    normalized_path = timing_dir / "timing-normalized.json"

    wall_ns = _seconds_to_ns(wall_seconds)
    metrics: dict[str, Any] = {
        "wallClockTime": {"wall_clock_ns": wall_ns},
        "tmE2eTime": {"wall_clock_ns": wall_ns},
    }
    if postprocess_seconds is not None:
        metrics["frameworkCallTime"] = {"total_ns": _seconds_to_ns(postprocess_seconds)}
    else:
        metrics["frameworkCallTime"] = {"total_ns": 0}
    if collect_seconds is not None:
        metrics["businessOperatorTime"] = {"total_ns": _seconds_to_ns(collect_seconds)}
    else:
        metrics["businessOperatorTime"] = {"total_ns": wall_ns}

    raw_payload = {
        "schemaVersion": 1,
        "framework": "udfbenchmarking",
        "platform": platform,
        "benchmark": benchmark_name,
        "timingSource": "udfbenchmarking_csv",
        "rawOutputDir": str(raw_output_dir.relative_to(platform_run_dir)),
        "summary": summary,
        "iterations": iterations,
    }
    normalized_payload = {
        "schemaVersion": 1,
        "platform": platform,
        "cases": [
            {
                "caseId": benchmark_name,
                "platform": "",
                "recordCount": record_count,
                "timingSource": "udfbenchmarking_csv",
                "metrics": metrics,
                "ops": [],
            }
        ],
    }

    raw_path.write_text(
        json.dumps(raw_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    normalized_path.write_text(
        json.dumps(normalized_payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "Wrote UDF_Benchmarking timing for %s to %s",
        benchmark_name,
        normalized_path.relative_to(platform_run_dir),
    )


@register_adapter
class UdfBenchmarkingAdapter:
    framework_id = "udfbenchmarking"

    def describe(self) -> str:
        return "UDF_Benchmarking adapter"

    def deploy_workload(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        yes: bool = False,
    ) -> WorkloadHandle:
        """Deploy the UDF_Benchmarking workload into its container."""
        from ...config import get_workload_config, load_environment_config
        from ...remote import build_executor, get_platform_host_ref

        workload = get_workload_config(project_path)
        local_dir = project_path.parent / workload["localDir"]
        if not local_dir.exists():
            raise StepError(f"Workload directory not found: {local_dir}")

        env_config = load_environment_config(project_path)
        host_ref = get_platform_host_ref(env_config, platform)
        executor = build_executor(host_ref, env_config)

        container = _container(env_config)
        remote_dir = "/tmp/pyframework-workload"
        executor.run(f"rm -rf {remote_dir}", timeout=15)
        logger.info("Uploading UDF_Benchmarking workload %s to %s", local_dir, remote_dir)
        ok = executor.push_dir(local_dir, remote_dir)
        if not ok:
            raise StepError(
                f"Failed to upload UDF_Benchmarking workload:\n"
                f"  Local: {local_dir}\n"
                f"  Remote: {remote_dir}"
            )

        clean_result = executor.run(
            f"docker exec -u root {container} bash -lc "
            "'rm -rf /workspace/benchmark && mkdir -p /workspace/benchmark && "
            "cp -a /opt/UDF_Benchmarking/. /workspace/benchmark/'",
            timeout=120,
            stream=True,
        )
        if clean_result.returncode != 0:
            raise StepError(
                f"Failed to prepare UDF_Benchmarking benchmark directory "
                f"(exit {clean_result.returncode}):\n"
                f"  stdout: {clean_result.stdout[:500]}\n"
                f"  stderr: {clean_result.stderr[:500]}"
            )

        cp_result = executor.run(
            f"docker cp {remote_dir}/. {container}:/workspace/benchmark",
            timeout=120,
            stream=True,
        )
        if cp_result.returncode != 0:
            raise StepError(
                f"Failed to copy workload to {container} (exit {cp_result.returncode}):\n"
                f"  stdout: {cp_result.stdout[:500]}\n"
                f"  stderr: {cp_result.stderr[:500]}"
            )
        executor.run(
            f"docker exec -u root {container} chown -R root:root /workspace/benchmark",
            timeout=15,
        )
        return WorkloadHandle(container=container, host=str(host_ref), env_dir=run_dir / platform)

    def run_benchmark(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        """Run the UDF_Benchmarking benchmark (perf + timing + flamegraph).

        Owns the framework-specific logic, moved verbatim from the orchestrator's
        _run_udfbenchmarking_benchmark / _run_udfbenchmarking_python_flamegraph /
        _write_udfbenchmarking_timing_artifacts (Phase 3 extraction).
        """
        import shlex
        import shutil

        from ...config import get_workload_config, load_environment_config
        from ...remote import build_executor, get_platform_host_ref

        workload = get_workload_config(project_path)
        env_config = load_environment_config(project_path)
        host_ref = get_platform_host_ref(env_config, platform)
        executor = build_executor(host_ref, env_config)

        container = _container(env_config)
        benchmark_name = _benchmark_name(env_config)
        config_file = _config_file(env_config)
        platform_run_dir = run_dir / platform
        platform_run_dir.mkdir(parents=True, exist_ok=True)
        timing_path = platform_run_dir / "timing" / "timing-normalized.json"
        raw_output_dir = platform_run_dir / "raw" / "udfbenchmarking"
        python_dir = platform_run_dir / "python"

        if force:
            from ... import orchestrator

            orchestrator._run_workload_deploy(project_path, run_dir, platform)
            for old in [
                timing_path,
                platform_run_dir / "timing" / "timing-raw.json",
                platform_run_dir / "perf" / "data" / f"perf-{platform}.data",
            ]:
                if old.exists():
                    old.unlink()
                    logger.info("[5a] Force: removed old artifact %s", old.relative_to(run_dir))
            for old_dir in [raw_output_dir, python_dir]:
                if old_dir.exists():
                    shutil.rmtree(old_dir)
                    logger.info("[5a] Force: removed old artifact %s", old_dir.relative_to(run_dir))

        if timing_path.exists() and timing_path.stat().st_size > 0:
            logger.info("[5a] UDF_Benchmarking timing exists, skipping benchmark on %s", platform)
        else:
            timeout = int(
                workload.get(
                    "timeout",
                    env_config.get("software", {}).get("benchmarkTimeout", 1800),
                )
            )
            remote_output = f"/tmp/pyframework-udfbenchmarking-run-{platform}"
            host_output = f"/tmp/pyframework-udfbenchmarking-output-{platform}"
            runner_args = (
                f"--config-file {shlex.quote(config_file)} "
                f"--output {shlex.quote(remote_output)}"
            )
            script = (
                "set -euo pipefail; "
                f"rm -rf {shlex.quote(remote_output)} /tmp/perf-udf.data; "
                f"mkdir -p {shlex.quote(remote_output)}; "
                "cd /workspace/benchmark; "
                f"test -f {shlex.quote(config_file)}; "
                "command -v perf >/dev/null; "
                # Pre-generate the random video dataset OUTSIDE the perf record
                # window. Video generation (cv2/numpy MP4 writes) is test-fixture
                # preparation, not the UDF pipeline under measurement, so it must
                # not pollute perf.data sampling nor the step-5a wall-clock. The
                # generator skips videos that already exist, so on repeat runs
                # this is a cheap no-op cache check.
                f"python3 -m datasets.random_video --config-file {shlex.quote(config_file)} >/dev/null 2>&1 || true; "
                "perf record -F 999 -g -e task-clock -o /tmp/perf-udf.data -- "
                f"python3 main.py {runner_args}; "
                "test -s /tmp/perf-udf.data; "
                f"test -f {shlex.quote(remote_output)}/{shlex.quote(benchmark_name)}.csv"
            )
            logger.info("[5a] Running UDF_Benchmarking %s on %s", benchmark_name, platform)
            result = executor.run(
                f"docker exec {container} bash -lc {shlex.quote(script)}",
                timeout=timeout,
                stream=True,
            )
            if result.returncode != 0:
                raise StepError(
                    f"UDF_Benchmarking benchmark failed (exit {result.returncode}):\n"
                    f"  Container: {container}\n"
                    f"  Benchmark: {benchmark_name}\n"
                    f"  output: {result.stdout[-2000:]}\n"
                    f"  stderr: {result.stderr[-1000:]}"
                )

            executor.run(f"rm -rf {host_output}", timeout=15)
            cp_result = executor.run(
                f"docker cp {container}:{remote_output}/. {host_output}",
                timeout=120,
                stream=True,
            )
            if cp_result.returncode != 0:
                raise StepError(
                    f"Failed to copy UDF_Benchmarking output (exit {cp_result.returncode}):\n"
                    f"  stdout: {cp_result.stdout[:500]}\n"
                    f"  stderr: {cp_result.stderr[:500]}"
                )
            if not executor.fetch_dir(host_output, raw_output_dir):
                raise StepError(
                    f"Failed to fetch UDF_Benchmarking output from {host_output}:\n"
                    f"  docker cp stdout: {cp_result.stdout[:500]}\n"
                    f"  docker cp stderr: {cp_result.stderr[:500]}"
                )
            executor.run(f"rm -rf {host_output}", timeout=15)

            _write_timing_artifacts(raw_output_dir, platform_run_dir, platform, benchmark_name)

        self._run_python_flamegraph(
            executor, container, platform, platform_run_dir,
            benchmark_name, config_file, env_config, workload,
        )
        return timing_path

    def _run_python_flamegraph(
        self,
        executor: Any,
        container: str,
        platform: str,
        platform_run_dir: Path,
        benchmark_name: str,
        config_file: str,
        env_config: dict[str, Any],
        workload: dict[str, Any],
    ) -> None:
        """Optional py-spy flamegraph collection (moved from orchestrator)."""
        import shlex

        config = _python_flamegraph_config(env_config)
        if not config["enabled"]:
            return

        flamegraph_dir = platform_run_dir / "python" / "flamegraphs"
        flamegraph_path = flamegraph_dir / f"{benchmark_name}.svg"
        manifest_path = platform_run_dir / "python" / "manifest.json"
        if flamegraph_path.exists() and flamegraph_path.stat().st_size > 0:
            logger.info("[5a] UDF_Benchmarking Python flamegraph exists, skipping py-spy on %s", platform)
            return

        timeout = int(
            workload.get(
                "timeout",
                env_config.get("software", {}).get("benchmarkTimeout", 1800),
            )
        )
        remote_profile = f"/tmp/pyframework-udfbenchmarking-python-{platform}"
        remote_output = f"/tmp/pyframework-udfbenchmarking-python-run-{platform}"
        host_output = f"/tmp/pyframework-udfbenchmarking-python-output-{platform}"
        runner_args = (
            f"--config-file {shlex.quote(config_file)} "
            f"--output {shlex.quote(remote_output)}"
        )
        subprocesses_arg = "--subprocesses " if config["subprocesses"] else ""
        script = (
            "set -euo pipefail; "
            f"rm -rf {shlex.quote(remote_output)} {shlex.quote(remote_profile)}; "
            f"mkdir -p {shlex.quote(remote_profile)}/flamegraphs; "
            "cd /workspace/benchmark; "
            f"test -f {shlex.quote(config_file)}; "
            "command -v py-spy >/dev/null; "
            "py-spy record "
            f"--rate {int(config['rate'])} "
            f"{subprocesses_arg}"
            "--format flamegraph "
            f"-o {shlex.quote(remote_profile)}/flamegraphs/{shlex.quote(benchmark_name)}.svg -- "
            f"python3 main.py {runner_args}; "
            f"test -s {shlex.quote(remote_profile)}/flamegraphs/{shlex.quote(benchmark_name)}.svg"
        )
        logger.info("[5a] Running UDF_Benchmarking Python flamegraph on %s", platform)
        result = executor.run(
            f"docker exec {container} bash -lc {shlex.quote(script)}",
            timeout=timeout,
            stream=True,
        )
        if result.returncode != 0:
            raise StepError(
                f"UDF_Benchmarking Python flamegraph failed (exit {result.returncode}):\n"
                f"  Container: {container}\n"
                f"  Benchmark: {benchmark_name}\n"
                f"  output: {result.stdout[-2000:]}\n"
                f"  stderr: {result.stderr[-1000:]}"
            )

        executor.run(f"rm -rf {host_output}", timeout=15)
        cp_result = executor.run(
            f"docker cp {container}:{remote_profile}/. {host_output}",
            timeout=120,
            stream=True,
        )
        if cp_result.returncode != 0:
            raise StepError(
                f"Failed to copy UDF_Benchmarking Python flamegraph output "
                f"(exit {cp_result.returncode}):\n"
                f"  stdout: {cp_result.stdout[:500]}\n"
                f"  stderr: {cp_result.stderr[:500]}"
            )
        local_python_dir = platform_run_dir / "python"
        if not executor.fetch_dir(host_output, local_python_dir):
            raise StepError(
                f"Failed to fetch UDF_Benchmarking Python flamegraph output from {host_output}:\n"
                f"  docker cp stdout: {cp_result.stdout[:500]}\n"
                f"  docker cp stderr: {cp_result.stderr[:500]}"
            )
        flamegraph_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "framework": "udfbenchmarking",
                    "platform": platform,
                    "profiler": "py-spy",
                    "cases": [
                        {
                            "caseId": benchmark_name,
                            "format": "flamegraph",
                            "rate": int(config["rate"]),
                            "subprocesses": bool(config["subprocesses"]),
                            "path": str(flamegraph_path.relative_to(local_python_dir)),
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        executor.run(f"rm -rf {host_output}", timeout=15)

    def perf_attach_strategy(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
    ) -> PerfAttachSpec:
        return PerfAttachSpec(
            command="perf",
            output_path=run_dir / platform / "perf.data",
        )

    def normalize_timing(
        self,
        timing_path: Path,
        *,
        platform: str,
    ) -> dict[str, Any]:
        if timing_path.exists():
            return json.loads(timing_path.read_text(encoding="utf-8"))
        return {}

    def collect_flamegraph(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        enabled: bool = False,
    ) -> Path | None:
        # Flamegraph collection is integrated into run_benchmark (reads the
        # pythonFlamegraph env config). This method exists for the contract.
        if not enabled:
            return None
        from ...config import load_environment_config

        env_config = load_environment_config(project_path)
        if not _python_flamegraph_config(env_config)["enabled"]:
            return None
        benchmark_name = _benchmark_name(env_config)
        out = run_dir / platform / "python" / "flamegraphs" / f"{benchmark_name}.svg"
        return out if out.exists() else None

    def disassembly_source(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
    ) -> DisassemblySpec:
        return DisassemblySpec(
            source_path=run_dir / platform,
            output_dir=run_dir / platform / "asm",
        )
