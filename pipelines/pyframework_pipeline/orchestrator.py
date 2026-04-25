"""Step-by-step pipeline orchestrator with resumability.

Chains steps 3 through 7 for all configured platforms, tracking state
in pipeline-run.json for resume-from-failure.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

STEP_DEFS: list[dict[str, str]] = [
    {"step": "3",  "name": "environment deploy"},
    {"step": "4",  "name": "workload deploy"},
    {"step": "5a", "name": "benchmark run"},
    {"step": "5b", "name": "collect"},
    {"step": "5c", "name": "acquire all"},
    {"step": "6",  "name": "backfill run"},
    {"step": "7",  "name": "bridge publish"},
]

# Steps that run per-platform (need --platform).
PER_PLATFORM_STEPS = {"3", "4", "5a", "5b"}

# Steps that run once after all platforms.
GLOBAL_STEPS = {"5c", "6", "7"}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


class PipelineRunState:
    """Tracks pipeline run state in pipeline-run.json."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def init(self, project_id: str, platforms: list[str]) -> None:
        if not self.data or not self.data.get("steps"):
            self.data = {
                "runId": _run_id(),
                "projectId": project_id,
                "platforms": platforms,
                "steps": [],
            }

    def is_completed(self, step: str, platform: str | None = None) -> bool:
        for s in self.data.get("steps", []):
            if s["step"] == step and s.get("platform") == platform:
                return s["status"] == "completed"
        return False

    def mark_running(self, step: str, platform: str | None = None) -> None:
        self.data.setdefault("steps", []).append({
            "step": step,
            "name": next(
                (d["name"] for d in STEP_DEFS if d["step"] == step), step
            ),
            "platform": platform,
            "status": "running",
            "startedAt": _now_iso(),
        })
        self._save()

    def mark_completed(self, step: str, platform: str | None = None) -> None:
        for s in reversed(self.data.get("steps", [])):
            if s["step"] == step and s.get("platform") == platform and s["status"] == "running":
                s["status"] = "completed"
                s["completedAt"] = _now_iso()
                break
        self._save()

    def mark_failed(
        self, step: str, platform: str | None = None, error: str = "",
    ) -> None:
        for s in reversed(self.data.get("steps", [])):
            if s["step"] == step and s.get("platform") == platform and s["status"] == "running":
                s["status"] = "failed"
                s["error"] = error
                s["completedAt"] = _now_iso()
                break
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    project_path: Path,
    run_dir: Path,
    *,
    resume_from: str | None = None,
    stop_before: str | None = None,
    force: bool = False,
    yes: bool = False,
) -> int:
    """Execute the full pipeline from step 3 to 7.

    Returns 0 on success, 1 on failure.
    """
    from .config import (
        get_run_config,
        get_workload_config,
        get_bridge_config,
        load_project_config,
    )

    config = load_project_config(project_path)
    project_id = config.get("id", "unknown")
    run_config = get_run_config(project_path)
    platforms = run_config.get("platforms", [])

    state_path = run_dir / "pipeline-run.json"
    state = PipelineRunState(state_path)

    if force:
        state.data = {}

    state.init(project_id, platforms)

    # Determine start point.
    step_ids = [d["step"] for d in STEP_DEFS]
    start_idx = 0
    if resume_from:
        if resume_from in step_ids:
            start_idx = step_ids.index(resume_from)
        else:
            logger.error("Unknown step: %s. Valid: %s", resume_from, step_ids)
            return 1

    stop_idx = len(STEP_DEFS)
    if stop_before:
        if stop_before in step_ids:
            stop_idx = step_ids.index(stop_before)
        else:
            logger.error("Unknown step: %s. Valid: %s", stop_before, step_ids)
            return 1

    for idx in range(start_idx, stop_idx):
        step_def = STEP_DEFS[idx]
        step_id = step_def["step"]
        step_name = step_def["name"]

        if step_id in PER_PLATFORM_STEPS:
            # Run for each platform.
            for plat in platforms:
                if state.is_completed(step_id, plat):
                    logger.info("[S%s/%s] Already completed, skipping", step_id, plat)
                    continue

                state.mark_running(step_id, plat)
                logger.info("[S%s/%s] %s", step_id, plat, step_name)

                try:
                    _execute_step(
                        step_id, project_path, run_dir, plat, yes=yes,
                    )
                    state.mark_completed(step_id, plat)
                except StepError as exc:
                    state.mark_failed(step_id, plat, str(exc))
                    _print_resume_hint(step_id, plat, project_path, error=str(exc))
                    return 1

        elif step_id in GLOBAL_STEPS:
            if state.is_completed(step_id):
                logger.info("[S%s] Already completed, skipping", step_id)
                continue

            state.mark_running(step_id)
            logger.info("[S%s] %s", step_id, step_name)

            try:
                _execute_step(
                    step_id, project_path, run_dir, None, yes=yes,
                )
                state.mark_completed(step_id)
            except StepError as exc:
                state.mark_failed(step_id, error=str(exc))
                _print_resume_hint(step_id, None, project_path, error=str(exc))
                return 1

    logger.info("Pipeline completed successfully")
    return 0


class StepError(Exception):
    """Raised when a pipeline step fails."""


def _execute_step(
    step_id: str,
    project_path: Path,
    run_dir: Path,
    platform: str | None,
    *,
    yes: bool = False,
) -> None:
    """Execute a single pipeline step."""
    if step_id == "3":
        from .environment.deploy import deploy_plan
        plan_path = run_dir / platform / "environment-plan.json"
        result = deploy_plan(project_path, platform, plan_path, yes=yes)
        # Save record.
        record = result.get("record", {})
        if record:
            record_dir = run_dir / platform
            record_dir.mkdir(parents=True, exist_ok=True)
            (record_dir / "environment-record.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        if result.get("status") == "failed":
            fs = result.get("failedStep", {})
            cmd = fs.get("command", "")
            stderr = fs.get("stderr", "")
            desc = fs.get("description", "")
            exit_code = fs.get("exitCode", "")
            parts = [f"environment deploy failed: {result.get('failed', 0)} step(s) failed"]
            if desc:
                parts.append(f"  Step: {fs.get('id', '')} — {desc}")
            if cmd:
                parts.append(f"  Command: {cmd}")
            if exit_code:
                parts.append(f"  Exit code: {exit_code}")
            if stderr:
                parts.append(f"  stderr: {stderr[:500]}")
            raise StepError("\n".join(parts))

    elif step_id == "4":
        _run_workload_deploy(project_path, run_dir, platform, yes=yes)

    elif step_id == "5a":
        _run_benchmark(project_path, run_dir, platform)

    elif step_id == "5b":
        _run_collect(project_path, run_dir, platform)

    elif step_id == "5c":
        _run_acquire_all(project_path, run_dir)

    elif step_id == "6":
        _run_backfill(project_path, run_dir)

    elif step_id == "7":
        _run_bridge_publish(project_path)

    else:
        raise StepError(f"Unknown step: {step_id}")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _run_workload_deploy(
    project_path: Path, run_dir: Path, platform: str, *, yes: bool = False,
) -> None:
    from .config import get_workload_config, load_environment_config
    from .remote import build_executor, get_platform_host_ref

    workload = get_workload_config(project_path)
    local_dir = project_path.parent / workload["localDir"]

    if not local_dir.exists():
        raise StepError(f"Workload directory not found: {local_dir}")

    env_config = load_environment_config(project_path)
    host_ref = get_platform_host_ref(env_config, platform)
    executor = build_executor(host_ref, env_config)

    # Upload workload to remote.
    remote_dir = "/tmp/pyframework-workload"
    logger.info("Uploading %s to %s:%s", local_dir, host_ref, remote_dir)
    ok = executor.push_dir(local_dir, remote_dir)
    if not ok:
        raise StepError(f"Failed to upload workload to {host_ref}:\n  Local: {local_dir}\n  Remote: {remote_dir}")

    # If container build mode, compile JAR inside container.
    if workload.get("build") == "container":
        logger.info("Building JAR inside container...")
        result = executor.run(
            f"docker exec flink-jm bash -c "
            f"'cd {remote_dir} && ./build.sh'",
            timeout=120,
        )
        if result.returncode != 0:
            raise StepError(
                f"Container build failed (exit {result.returncode}):\n"
                f"  Command: docker exec flink-jm bash -c 'cd {remote_dir} && ./build.sh'\n"
                f"  stderr: {result.stderr[:500]}"
            )

    # Distribute to containers via docker cp.
    jm_result = executor.run(f"docker cp {remote_dir}/. flink-jm:/opt/flink/usrlib")
    if jm_result.returncode != 0:
        raise StepError(
            f"Failed to copy workload to JM (exit {jm_result.returncode}):\n"
            f"  Command: docker cp {remote_dir}/. flink-jm:/opt/flink/usrlib\n"
            f"  stderr: {jm_result.stderr[:500]}"
        )

    for i in range(1, 3):  # tm1, tm2
        tm_result = executor.run(
            f"docker cp {remote_dir}/. flink-tm{i}:/opt/flink/usrlib"
        )
        if tm_result.returncode != 0:
            raise StepError(
                f"Failed to copy workload to TM{i} (exit {tm_result.returncode}):\n"
                f"  Command: docker cp {remote_dir}/. flink-tm{i}:/opt/flink/usrlib\n"
                f"  stderr: {tm_result.stderr[:500]}"
            )


def _run_benchmark(
    project_path: Path, run_dir: Path, platform: str,
) -> None:
    import time

    from .config import get_workload_config, load_environment_config
    from .remote import build_executor, get_platform_host_ref

    workload = get_workload_config(project_path)
    queries = workload.get("queries", [])
    rows = workload.get("rows", 10_000_000)
    env_config = load_environment_config(project_path)
    host_ref = get_platform_host_ref(env_config, platform)
    executor = build_executor(host_ref, env_config)

    platform_run_dir = run_dir / platform
    platform_run_dir.mkdir(parents=True, exist_ok=True)

    tm_count = _parse_tm_count(env_config)

    # Ensure perf is available inside TM containers.
    _ensure_container_perf(executor, tm_count)

    # Clean stale perf data inside TM containers.
    for i in range(1, tm_count + 1):
        executor.run(
            f"docker exec flink-tm{i} rm -f /tmp/perf-udf.data",
            timeout=30,
        )

    # Start perf recording inside TM1 container (system-wide within container
    # PID namespace, captures Python worker subprocesses).
    perf_binary = _find_container_perf(executor)
    executor.run(
        f"nohup docker exec flink-tm1 {perf_binary} record "
        f"-F 999 -g -e task-clock -a -o /tmp/perf-udf.data "
        f">/dev/null 2>&1 &",
        timeout=30,
    )

    # Run all queries while perf is recording, capture wall-clock times.
    if not queries:
        raise StepError("No queries configured")

    import json as _json

    wall_clock_times: dict[str, dict] = {}

    for query in queries:
        logger.info("Running query %s on %s...", query, platform)
        result = executor.run(
            f"docker exec flink-jm /opt/flink/.pyenv/versions/3.14.3/bin/python3 "
            f"/opt/flink/usrlib/benchmark_runner.py "
            f"--query {query} --rows {rows}",
            timeout=300,
        )
        if result.returncode != 0:
            raise StepError(
                f"Benchmark {query} failed (exit {result.returncode}):\n"
                f"  Command: docker exec flink-jm python3 benchmark_runner.py --query {query} --rows {rows}\n"
                f"  stderr: {result.stderr[:500]}"
            )

        # Parse BENCHMARK_RESULT from stdout.
        wc = _parse_benchmark_result(result.stdout, query)
        if wc:
            wall_clock_times[query] = wc
            logger.info("  %s: wall-clock %.3fs, throughput %s rows/s",
                        query, wc["wallClockSeconds"], wc.get("throughputRowsPerSec", "-"))

        for i in range(1, tm_count + 1):
            logs = executor.docker_logs(f"flink-tm{i}", tail=50)
            (platform_run_dir / f"tm-stdout-tm{i}.log").write_text(logs, encoding="utf-8")

        # Collect per-invocation operator timing from TM worker stats file.
        _collect_operator_timing(executor, tm_count, query, wall_clock_times)

    # Write wall-clock timing to timing-normalized.json.
    _merge_wall_clock_times(platform_run_dir, platform, wall_clock_times)

    # Stop perf inside TM1.
    executor.run(
        "docker exec flink-tm1 bash -c 'kill -INT $(pidof perf) || true'",
        timeout=30,
    )
    time.sleep(2)


def _collect_operator_timing(
    executor: "SshExecutor",
    tm_count: int,
    query_id: str,
    wall_clock_times: dict[str, dict],
) -> None:
    """Collect operator/framework timing from PostUDF's [BENCHMARK_SUMMARY].

    PostUDF (CalcOverhead) accumulates per-record py_duration and framework
    overhead in AtomicLongs, then prints a JSON summary to stdout on close().

    System.out goes to the JVM process stdout, which Docker captures as
    container logs (not Flink's log4j files). We grep the actual TM log
    files (flink--taskexecutor-*.log) as a first attempt, then fall back
    to docker logs --tail if needed.
    """
    import json as _json

    for i in range(1, tm_count + 1):
        # Try Flink log4j log files (wildcard to match container-specific names).
        result = executor.run(
            f"docker exec flink-tm{i} bash -c "
            f"'grep BENCHMARK_SUMMARY /opt/flink/log/flink--taskexecutor-*.log "
            f"2>/dev/null | tail -1'",
            timeout=60,
        )
        # Fallback: grep docker container logs (System.out destination).
        if result.returncode != 0 or "BENCHMARK_SUMMARY" not in (result.stdout or ""):
            result = executor.run(
                f"docker logs flink-tm{i} --tail 500 2>&1 | "
                f"grep BENCHMARK_SUMMARY | tail -1",
                timeout=60,
            )
        if result.returncode == 0 and "BENCHMARK_SUMMARY" in (result.stdout or ""):
            try:
                # Extract JSON after [BENCHMARK_SUMMARY] marker
                line = result.stdout.strip()
                json_str = line.split("BENCHMARK_SUMMARY] ", 1)[1].strip()
                stats = _json.loads(json_str)
                wc = wall_clock_times.get(query_id, {})
                wc["recordCount"] = wc.get("recordCount", 0) + stats.get("recordCount", 0)
                wc["totalPyDurationNs"] = wc.get("totalPyDurationNs", 0) + stats.get("totalPyDurationNs", 0)
                wc["totalFrameworkOverheadNs"] = (
                    wc.get("totalFrameworkOverheadNs", 0)
                    + stats.get("totalFrameworkOverheadNs", 0)
                )
                wall_clock_times[query_id] = wc
                logger.info("  %s TM%d: %d records, py=%d ns, fw=%d ns",
                            query_id, i, stats.get("recordCount", 0),
                            stats.get("totalPyDurationNs", 0),
                            stats.get("totalFrameworkOverheadNs", 0))
            except (_json.JSONDecodeError, IndexError):
                pass


def _ensure_container_perf(
    executor: "SshExecutor",
    tm_count: int,
) -> str:
    """Install linux-tools inside TM containers if perf is not available."""
    # Check if perf already exists inside TM1.
    check = executor.run(
        "docker exec flink-tm1 bash -c "
        "'ls /usr/lib/linux-tools-*/perf 2>/dev/null | sort -V | tail -1'",
        timeout=30,
    )
    if check.returncode == 0 and check.stdout.strip():
        return check.stdout.strip()

    logger.info("Installing linux-tools inside TM containers...")
    for i in range(1, tm_count + 1):
        executor.run(
            f"docker exec -u root flink-tm{i} bash -c "
            f"'apt-get update -qq && apt-get install -y -qq "
            f"linux-tools-common linux-tools-generic 2>&1 | tail -1'",
            timeout=120,
        )

    # Verify.
    check = executor.run(
        "docker exec flink-tm1 bash -c "
        "'ls /usr/lib/linux-tools-*/perf 2>/dev/null | sort -V | tail -1'",
        timeout=30,
    )
    if check.returncode != 0 or not check.stdout.strip():
        raise StepError(
            f"Could not install perf inside TM container (exit {check.returncode}):\n"
            f"  stderr: {check.stderr[:500]}"
        )
    return check.stdout.strip()


def _parse_benchmark_result(stdout: str, query_id: str) -> dict | None:
    """Parse BENCHMARK_RESULT JSON from benchmark_runner.py stdout."""
    import json as _json
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if '"BENCHMARK_RESULT"' in line:
            try:
                data = _json.loads(line)
                if data.get("type") == "BENCHMARK_RESULT":
                    return data
            except _json.JSONDecodeError:
                continue
    return None


def _merge_wall_clock_times(
    platform_run_dir: Path,
    platform: str,
    wall_clock_times: dict[str, dict],
) -> None:
    """Merge wall-clock timing into timing/timing-normalized.json."""
    import json as _json

    timing_path = platform_run_dir / "timing" / "timing-normalized.json"
    timing_path.parent.mkdir(parents=True, exist_ok=True)

    if timing_path.exists():
        data = _json.loads(timing_path.read_text(encoding="utf-8"))
    else:
        data = {"schemaVersion": 1, "platform": platform, "cases": []}

    cases_by_id = {c["caseId"]: c for c in data.get("cases", [])}

    for query_id, wc in wall_clock_times.items():
        case = cases_by_id.get(query_id)
        if case is None:
            case = {"caseId": query_id, "metrics": {}}
            data.setdefault("cases", []).append(case)
            cases_by_id[query_id] = case

        wall_clock_ns = int(wc["wallClockSeconds"] * 1e9)
        case["metrics"]["wallClockTime"] = {"wall_clock_ns": wall_clock_ns}
        case["metrics"]["tmE2eTime"] = {"wall_clock_ns": wall_clock_ns}

        # Operator/framework timing from PostUDF's [BENCHMARK_SUMMARY].
        py_ns = wc.get("totalPyDurationNs", 0)
        fw_ns = wc.get("totalFrameworkOverheadNs", 0)
        if py_ns > 0:
            case["metrics"]["businessOperatorTime"] = {
                "total_ns": py_ns,
            }
        if fw_ns > 0:
            case["metrics"]["frameworkCallTime"] = {
                "total_ns": fw_ns,
            }

    timing_path.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Wrote wall-clock timing for %d queries to %s",
                len(wall_clock_times), timing_path.relative_to(platform_run_dir))


def _find_container_perf(executor: "SshExecutor") -> str:
    """Find the perf binary path inside the TM container."""
    result = executor.run(
        "docker exec flink-tm1 bash -c "
        "'ls /usr/lib/linux-tools-*/perf 2>/dev/null | sort -V | tail -1'",
        timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback to /usr/bin/perf.
    return "/usr/bin/perf"


def _parse_tm_count(env_config: dict) -> int:
    """Parse TM count from environment.yaml software.clusterTopology (e.g. '1jm-2tm')."""
    software = env_config.get("software", {})
    topology = software.get("clusterTopology", "")
    if "-" in topology:
        parts = topology.split("-")
        if len(parts) >= 2 and parts[-1].endswith("tm"):
            try:
                return int(parts[-1].rstrip("tm"))
            except ValueError:
                pass
    return 2  # fallback


def _run_collect(
    project_path: Path, run_dir: Path, platform: str,
) -> None:
    import json as _json

    from .config import load_environment_config
    from .remote import build_executor, get_platform_host_ref

    env_config = load_environment_config(project_path)
    host_ref = get_platform_host_ref(env_config, platform)
    executor = build_executor(host_ref, env_config)

    platform_run_dir = run_dir / platform

    # Find which TM is running the task via JM REST API.
    tm_container = _find_task_tm(executor)
    if not tm_container:
        tm_container = "flink-tm1"
        logger.warning("Could not determine task TM via JM, falling back to %s", tm_container)

    # Collect perf.data from TM container via docker cp + scp.
    perf_dir = platform_run_dir / "perf" / "data"
    perf_dir.mkdir(parents=True, exist_ok=True)
    perf_data_local = perf_dir / f"perf-{platform}.data"
    if not perf_data_local.exists() or perf_data_local.stat().st_size == 0:
        logger.info("Collecting perf.data from %s...", tm_container)
        _collect_binary_from_container(
            executor,
            tm_container,
            "/tmp/perf-udf.data",
            perf_data_local,
        )
    else:
        logger.info("perf.data already exists (%d bytes), skipping collection",
                     perf_data_local.stat().st_size)

    # Run python-performance-kits pipeline on remote host (same architecture as perf.data).
    _run_perf_kits_on_remote(executor, perf_data_local, perf_dir, platform)

    # Collect objdump for hotspot symbols across all shared libraries.
    asm_dir = platform_run_dir / "asm" / ("arm64" if platform == "arm" else "x86_64")
    asm_dir.mkdir(parents=True, exist_ok=True)
    _collect_asm_from_all_libs(executor, perf_dir, asm_dir, platform)

    logger.info("Collection complete for %s", platform)


def _collect_asm_from_all_libs(
    executor: "SshExecutor",
    perf_dir: Path,
    asm_dir: Path,
    platform: str,
) -> None:
    """Collect objdump for top hotspot symbols from ALL shared libraries.

    Reads perf_records.csv to discover which shared_object each hotspot
    symbol belongs to, then runs objdump inside the container for each
    library.  Skips kernel symbols and unresolved addresses.
    """
    import csv

    perf_csv = perf_dir / "data" / "perf_records.csv"
    if not perf_csv.exists():
        logger.warning("perf_records.csv not found, skipping multi-lib ASM collection")
        return

    # Group symbols by shared_object, filtering to meaningful ones.
    so_to_syms: dict[str, list[str]] = {}
    try:
        with open(perf_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                so = (row.get("shared_object") or "").strip()
                if not sym or sym.startswith("0x") or so in ("", "[unknown]"):
                    continue
                if so == "[kernel.kallsyms]":
                    continue
                so_to_syms.setdefault(so, []).append(sym)
    except Exception as e:
        logger.warning("Failed to read perf_records.csv: %s", e)
        return

    # Keep top symbols per library (by appearance count).
    from collections import Counter
    for so, syms in so_to_syms.items():
        counts = Counter(syms)
        top = [s for s, _ in counts.most_common(30)]
        so_to_syms[so] = top

    container = "flink-tm1"

    for so, syms in sorted(so_to_syms.items()):
        # Find the shared library inside the container.
        find_result = executor.run(
            f"docker exec {container} find / -name '{so}' -type f 2>/dev/null | head -1",
            timeout=30,
        )
        so_path = find_result.stdout.strip() if find_result.returncode == 0 else ""
        if not so_path:
            logger.warning("Shared library %s not found in container %s, skipping", so, container)
            continue

        logger.info("Collecting objdump from %s (%d symbols)", so, len(syms))

        # Batch objdump: extract all symbols from this library in one pass,
        # then split per-symbol with awk.  Much faster than one objdump per symbol.
        objdump_result = executor.run(
            f"docker exec {container} bash -c "
            f"'objdump -d -C {so_path} 2>/dev/null'",
            timeout=120,
        )
        if objdump_result.returncode != 0 or not objdump_result.stdout:
            logger.warning("objdump failed for %s", so_path)
            continue

        full_dump = objdump_result.stdout

        # Extract each symbol's disassembly from the full dump.
        for sym in syms:
            if (asm_dir / f"{sym}.s").exists() and (asm_dir / f"{sym}.s").stat().st_size > 0:
                continue  # Already collected (e.g. from a previous library)

            # Use awk-style extraction: lines between "<sym>:" and next empty line.
            import re
            pattern = re.compile(rf"^[0-9a-f]+ <{re.escape(sym)}>")
            lines = []
            capturing = False
            for line in full_dump.splitlines():
                if not capturing and pattern.match(line):
                    capturing = True
                if capturing:
                    if line.strip() == "" and lines:
                        break
                    lines.append(line)
                if len(lines) > 500:
                    lines = lines[:500]
                    break

            content = "\n".join(lines)
            if content.strip():
                (asm_dir / f"{sym}.s").write_text(content, encoding="utf-8")
                logger.info("  Collected %s (%d lines)", sym, len(lines))

    logger.info("ASM collection: %d files in %s", len(list(asm_dir.glob("*.s"))), asm_dir)


def _run_perf_kits_on_remote(
    executor: "SshExecutor",
    perf_data_local: Path,
    perf_dir: Path,
    platform: str,
) -> None:
    """Run python-performance-kits pipeline inside the TM container.

    Running inside the container gives perf report access to the exact
    binaries (libpython3.14.so, etc.) so symbols resolve correctly.
    """
    kits_local = Path(__file__).resolve().parents[2] / "vendor" / "python-performance-kits"
    scripts_dir = kits_local / "scripts" / "perf_insights"
    if not scripts_dir.exists():
        logger.warning("python-performance-kits not found at %s, skipping remote pipeline", kits_local)
        return

    container_kits = "/opt/flink/perf-kits-scripts"
    container_output = "/opt/flink/perf-kits-output"
    perf_data_container = "/tmp/perf-udf.data"
    python_bin = "/opt/flink/.pyenv/versions/3.14.3/bin/python3"
    perf_bin = _find_container_perf(executor)

    # Deploy scripts into container via host staging.
    host_staging = "/tmp/pyframework-perf-kits-scripts"
    executor.run(f"rm -rf {host_staging} && mkdir -p {host_staging}", timeout=30)
    for script_name in [
        "run_single_platform_pipeline.py",
        "perf_data_to_csv.py",
        "perf_script_to_csv.py",
        "normalize_perf_records.py",
        "summarize_platform_perf.py",
        "annotate_perf_hotspots.py",
        "perf_analysis_common.py",
        "render_platform_report.py",
        "render_platform_visuals.py",
        "render_platform_machine_code_report.py",
        "show_symbol_machine_code.py",
        "cpython_category_rules.json",
    ]:
        src = scripts_dir / script_name
        if src.exists():
            executor.push_file(src, f"{host_staging}/{script_name}")

    # Copy scripts into container.
    executor.run(
        f"docker exec flink-tm1 rm -rf {container_kits}",
        timeout=30,
    )
    executor.run(
        f"docker cp {host_staging}/. flink-tm1:{container_kits}",
        timeout=30,
    )
    executor.run(f"rm -rf {host_staging}", timeout=30)

    # Run the pipeline inside the container.
    logger.info("Running python-performance-kits pipeline inside TM container (%s)...", platform)
    result = executor.run(
        f"docker exec flink-tm1 {python_bin} "
        f"{container_kits}/run_single_platform_pipeline.py "
        f"{perf_data_container} -o {container_output} "
        f"--benchmark tpch --platform {platform} "
        f"--perf-bin {perf_bin} "
        f"--skip-annotate --no-print-report 2>&1 | tail -10",
        timeout=600,
    )
    if result.returncode != 0:
        raise StepError(
            f"perf-kits pipeline failed inside TM container (exit {result.returncode}):\n"
            f"  Command: {python_bin} {container_kits}/run_single_platform_pipeline.py ...\n"
            f"  stderr: {result.stderr[:500]}\n"
            f"  stdout: {result.stdout[:500]}"
        )

    # Collect outputs from container via host staging.
    host_output = "/tmp/pyframework-perf-kits-output"
    executor.run(f"rm -rf {host_output}", timeout=30)
    executor.run(
        f"docker cp flink-tm1:{container_output}/ {host_output}",
        timeout=60,
    )

    for remote_rel in [
        "data/perf_records.csv",
        "tables/category_summary.csv",
        "tables/shared_object_summary.csv",
        "tables/symbol_hotspots.csv",
    ]:
        remote_path = f"{host_output}/{remote_rel}"
        local_path = perf_dir.parent / remote_rel  # perf_dir is perf/data/
        local_path.parent.mkdir(parents=True, exist_ok=True)
        executor.fetch_file(remote_path, local_path)
        logger.info("Collected %s", remote_rel)

    # Cleanup.
    executor.run(f"docker exec flink-tm1 rm -rf {container_kits} {container_output}", timeout=30)
    executor.run(f"rm -rf {host_output}", timeout=30)


def _find_task_tm(executor: "SshExecutor") -> str | None:
    """Query JM REST API to find which TM container is running the task."""
    import json as _json

    # Get running jobs.
    result = executor.run(
        "docker exec flink-jm curl -sf http://localhost:8081/jobs",
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout:
        return None

    try:
        jobs = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return None

    jobs_data = jobs.get("jobs", [])
    # Flink REST API returns {"jobs": []} when no jobs exist, or
    # {"jobs": {"running": [...], "finished": [...]}} with running jobs.
    if isinstance(jobs_data, list):
        # All jobs listed directly (v2 API) — find recent finished job
        running = []
        finished = jobs_data
    else:
        running = jobs_data.get("running", [])
        finished = jobs_data.get("finished", [])

    job_id = None
    if running:
        job_id = running[0]
    elif finished:
        job_id = finished[-1]  # most recent finished job

    # Get job vertices to find task location.
    result = executor.run(
        f"docker exec flink-jm curl -sf http://localhost:8081/jobs/{job_id}",
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout:
        return None

    try:
        job_detail = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        return None

    vertices = job_detail.get("vertices", [])
    if not vertices:
        return None

    # Look for the vertex containing Python UDF (usually has "Python" or "CHAIN" in name).
    for vertex in vertices:
        subtasks = vertex.get("subtasks", [])
        if subtasks:
            host = subtasks[0].get("host", "")
            # The host is the container ID or hostname; map back to container name.
            # In Docker network mode, host is the container hostname (e.g., container ID).
            # Fallback: try to match by checking which TM has this host.
            for i in range(1, 10):
                r = executor.run(
                    f"docker exec flink-tm{i} hostname",
                    timeout=30,
                )
                if r.returncode == 0 and r.stdout.strip() == host:
                    return f"flink-tm{i}"

    return None


def _collect_binary_from_container(
    executor: "SshExecutor",
    container: str,
    remote_path: str,
    local_path: Path,
) -> bool:
    """Collect a binary file from a container via docker cp + scp."""
    staging = f"/opt/flink/_collect_{local_path.name}"
    host_tmp = f"/tmp/pyframework-collect-{container}-{local_path.name}"

    # Copy inside container to a path accessible by docker cp.
    executor.run(
        f"docker exec -u root {container} cp {remote_path} {staging} 2>/dev/null",
        timeout=30,
    )
    executor.run(
        f"docker exec -u root {container} chmod 644 {staging} 2>/dev/null",
        timeout=30,
    )

    # docker cp from container to host filesystem.
    cp_result = executor.run(
        f"docker cp {container}:{staging} {host_tmp}",
        timeout=60,
    )
    if cp_result.returncode != 0:
        logger.warning("docker cp failed for %s:%s: %s", container, staging, cp_result.stderr)
        return False

    # scp from host to local (binary-safe).
    ok = executor.fetch_file(host_tmp, local_path)

    # Cleanup.
    executor.run(f"docker exec {container} rm -f {staging}", timeout=30)
    executor.run(f"rm -f {host_tmp}", timeout=30)

    return ok


def _run_acquire_all(project_path: Path, run_dir: Path) -> None:
    from .acquisition.timing import collect_timing
    from .acquisition.perf_profile import collect_perf
    from .acquisition.machine_code import collect_asm

    config = load_project_config(project_path)
    run_config = get_run_config(project_path)
    platforms = run_config.get("platforms", [])

    for plat in platforms:
        plat_dir = run_dir / plat
        stdout_files = list(plat_dir.glob("tm-stdout-*.log"))
        timing_result = collect_timing(plat_dir, plat, stdout_files or None)
        logger.info("Timing %s: %d cases", plat, len(timing_result.get("cases", [])))

        # Discover perf data files (perf-{platform}.data or perf-tm*.data).
        perf_data_dir = plat_dir / "perf" / "data"
        perf_data_files = sorted(perf_data_dir.glob("perf-*.data"))
        if not perf_data_files:
            legacy = perf_data_dir / "perf.data"
            if legacy.exists():
                perf_data_files = [legacy]
        primary_perf = perf_data_files[0] if perf_data_files else None

        perf_result = collect_perf(
            plat_dir, plat,
            primary_perf,
            None,
        )
        logger.info("Perf %s: %s", plat, perf_result.get("status", "unknown"))

        asm_result = collect_asm(
            plat_dir, plat,
            primary_perf,
            None,
            [],
        )
        logger.info("ASM %s: %s", plat, asm_result.get("status", "unknown"))


def _run_backfill(project_path: Path, run_dir: Path) -> None:
    from .backfill.pipeline import run_backfill
    from .config import get_run_config

    run_config = get_run_config(project_path)
    platforms = run_config.get("platforms", [])

    if len(platforms) < 2:
        raise StepError("Need at least 2 platforms for backfill")

    arm_dir = run_dir / platforms[0]
    x86_dir = run_dir / platforms[1]

    rc = run_backfill(project_path, arm_dir, x86_dir)
    if rc != 0:
        raise StepError(
            f"Backfill failed (rc={rc}). Check logs above for which sub-module failed.\n"
            f"  ARM dir: {arm_dir}\n"
            f"  x86 dir: {x86_dir}"
        )


def _run_bridge_publish(project_path: Path) -> None:
    from .bridge.analysis import publish
    from .config import get_bridge_config

    bridge_config = get_bridge_config(project_path)
    result = publish(
        project_path,
        repo=bridge_config["repo"],
        platform=bridge_config["platform"],
        token=bridge_config["token"],
        bridge_type=bridge_config.get("type", "discussion"),
        discussion_category=bridge_config.get("category", "General"),
    )
    if result.get("errors", 0) > 0:
        raise StepError(
            f"Bridge publish had {result['errors']} error(s):\n"
            f"  repo: {bridge_config['repo']}\n"
            f"  platform: {bridge_config['platform']}\n"
            f"  type: {bridge_config.get('type', 'discussion')}\n"
            f"  details: {result.get('error_details', 'see logs above')}"
        )


import sys

from .config import load_project_config, get_run_config


def _print_resume_hint(
    step_id: str, platform: str | None, project_path: Path,
    error: str = "",
) -> None:
    plat_str = f' on platform "{platform}"' if platform else ""
    step_name = next((d["name"] for d in STEP_DEFS if d["step"] == step_id), step_id)
    print(
        f"\nERROR: Step {step_id} \"{step_name}\" failed{plat_str}",
        file=sys.stderr,
    )
    if error:
        print(error, file=sys.stderr)
    print(
        f"\nResume from this step:\n"
        f"  pyframework-pipeline run {project_path} --resume-from {step_id}",
        file=sys.stderr,
    )
