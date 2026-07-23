"""Step-by-step pipeline orchestrator with resumability.

Chains steps 3 through 7 for all configured platforms, tracking state
in pipeline-run.json for resume-from-failure.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts.step import RunContext, StepError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step registry
# ---------------------------------------------------------------------------

STEP_DEFS: list[dict[str, str]] = [
    {"step": "3",      "name": "environment deploy"},
    {"step": "4",      "name": "workload deploy"},
    {"step": "5a",     "name": "benchmark run"},
    {"step": "5a.1",   "name": "operator context timing"},
    {"step": "5a.2",   "name": "isolated operator timing"},
    {"step": "5b.1",   "name": "collect perf.data"},
    {"step": "5b.2",   "name": "run perf-kits"},
    {"step": "5b.2b",  "name": "extract CPython source"},
    {"step": "5b.3",   "name": "collect objdump ASM"},
    {"step": "5c",     "name": "acquire all"},
    {"step": "5c.1",   "name": "operator readable reports"},
    {"step": "5d",     "name": "operator platform compare"},
    {"step": "6",      "name": "backfill run"},
    {"step": "6b",     "name": "platform compare"},
    {"step": "7",      "name": "bridge publish"},
]

# Steps that run per-platform (need --platform).
PER_PLATFORM_STEPS = {
    "3", "4", "5a", "5a.1", "5a.2", "5b.1", "5b.2", "5b.2b", "5b.3"
}

# Steps that run once after all platforms.
GLOBAL_STEPS = {"5c", "5c.1", "5d", "6", "6b", "7"}

# Mapping from old "5b" to its sub-steps (for resume-from backward compat).
_STEP_ALIASES: dict[str, list[str]] = {
    "5b": ["5b.1", "5b.2", "5b.2b", "5b.3"],
}

_PERF_RECORD_REQUIRED_FIELDS = (
    "platform_id",
    "arch",
    "python_version",
    "benchmark",
    "event",
    "children",
    "self",
    "period",
    "shared_object",
    "symbol",
    "category_top",
    "source_report",
    "sample_count",
)


def _resolve_step_alias(step: str) -> str:
    """Resolve step aliases to their first sub-step (e.g. '5b' -> '5b.1')."""
    if step in _STEP_ALIASES:
        return _STEP_ALIASES[step][0]
    return step


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
            return
        merged_platforms = list(
            dict.fromkeys([*self.data.get("platforms", []), *platforms])
        )
        if merged_platforms != self.data.get("platforms"):
            self.data["platforms"] = merged_platforms
            self._save()

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

    def clear_from(
        self, step: str, platforms: list[str] | None = None
    ) -> None:
        """Remove state entries for *step* and all subsequent steps."""
        step_ids = [d["step"] for d in STEP_DEFS]
        # Resolve aliases (e.g. "5b" -> "5b.1").
        resolved = _resolve_step_alias(step)
        if resolved not in step_ids:
            return
        cutoff = step_ids.index(resolved)
        clear_ids = set(step_ids[cutoff:])
        selected_platforms = set(platforms) if platforms is not None else None
        self.data["steps"] = [
            s for s in self.data.get("steps", [])
            if s["step"] not in clear_ids
            or (
                s["step"] in PER_PLATFORM_STEPS
                and selected_platforms is not None
                and s.get("platform") not in selected_platforms
            )
        ]
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Orchestrator helpers
# ---------------------------------------------------------------------------

def _init_submodules(repo_root: Path) -> None:
    """No-op stub: vendor submodules were removed in Phase 1 of the refactor.

    The kits code now lives in ``pyframework_pipeline/analyze/``. Kept as a
    stub so any lingering references degrade gracefully rather than
    NameError; safe to remove once all call sites are confirmed gone.
    """
    return


def _framework_id(env_config: dict[str, Any]) -> str:
    return str(env_config.get("framework", "pyflink"))


def _datajuicer_container(env_config: dict[str, Any]) -> str:
    return str(
        env_config.get("software", {}).get("dataJuicerContainer", "data-juicer-bench")
    )


def _datajuicer_modalities(
    workload: dict[str, Any],
    env_config: dict[str, Any],
) -> list[str]:
    raw = workload.get("modalities")
    if raw in ("", None):
        raw = env_config.get("software", {}).get("benchmarkModalities", ["text"])
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw]
    else:
        values = [item.strip() for item in str(raw).replace(",", " ").split()]
    selected = [item for item in values if item == "text"]
    return selected or ["text"]


def _datajuicer_benchmark_name(env_config: dict[str, Any]) -> str:
    return str(
        env_config.get("software", {}).get("benchmarkName", "data-juicer-text")
    )


def _datajuicer_python_flamegraph_config(env_config: dict[str, Any]) -> dict[str, Any]:
    raw = env_config.get("software", {}).get("pythonFlamegraph", {})
    if isinstance(raw, dict):
        enabled = _config_bool(raw.get("enabled", False))
        rate = int(raw.get("rate", 100) or 100)
        subprocesses = _config_bool(raw.get("subprocesses", True))
    else:
        enabled = _config_bool(raw)
        rate = 100
        subprocesses = True
    return {
        "enabled": enabled,
        "rate": rate,
        "subprocesses": subprocesses,
    }


def _udfbenchmarking_container(env_config: dict[str, Any]) -> str:
    return str(
        env_config.get("software", {}).get(
            "udfBenchmarkingContainer",
            "udf-benchmarking-bench",
        )
    )


def _udfbenchmarking_benchmark_name(env_config: dict[str, Any]) -> str:
    return str(
        env_config.get("software", {}).get("benchmarkName", "MockVideoE2EUDF")
    )


def _udfbenchmarking_config_file(env_config: dict[str, Any]) -> str:
    config_file = str(
        env_config.get("software", {}).get("benchmarkConfigFile", "config.yaml")
    )
    if config_file.startswith("/"):
        raise StepError(
            "UDF_Benchmarking benchmarkConfigFile must be relative to /workspace/benchmark"
        )
    return config_file


def _udfbenchmarking_python_flamegraph_config(env_config: dict[str, Any]) -> dict[str, Any]:
    return _datajuicer_python_flamegraph_config(env_config)


def _config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    project_path: Path,
    run_dir: Path,
    *,
    resume_from: str | None = None,
    stop_before: str | None = None,
    platforms: list[str] | None = None,
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
    configured_platforms = list(run_config.get("platforms", []))
    if platforms is None:
        selected_platforms = configured_platforms
    else:
        selected_platforms = list(dict.fromkeys(platforms))
        unknown = [p for p in selected_platforms if p not in configured_platforms]
        if unknown:
            logger.error(
                "Selected platform(s) not configured in run.platforms: %s",
                ", ".join(unknown),
            )
            return 1
        if not selected_platforms:
            logger.error("At least one platform must be selected")
            return 1

    state_path = run_dir / "pipeline-run.json"
    state = PipelineRunState(state_path)

    if force:
        state.data = {}

    state.init(project_id, selected_platforms)

    # Determine start point.
    step_ids = [d["step"] for d in STEP_DEFS]
    start_idx = 0
    if resume_from:
        resolved = _resolve_step_alias(resume_from)
        if resolved in step_ids:
            start_idx = step_ids.index(resolved)
            state.clear_from(resume_from, selected_platforms)
            logger.info("Resuming from step %s — cleared downstream state", resume_from)
        else:
            logger.error("Unknown step: %s. Valid: %s", resume_from, step_ids)
            return 1

    stop_idx = len(STEP_DEFS)
    if stop_before:
        resolved_stop = _resolve_step_alias(stop_before)
        if resolved_stop in step_ids:
            stop_idx = step_ids.index(resolved_stop)
        else:
            logger.error("Unknown step: %s. Valid: %s", stop_before, step_ids)
            return 1

    for idx in range(start_idx, stop_idx):
        step_def = STEP_DEFS[idx]
        step_id = step_def["step"]
        step_name = step_def["name"]

        if step_id in PER_PLATFORM_STEPS:
            # Run for each platform.
            for plat in selected_platforms:
                if state.is_completed(step_id, plat):
                    logger.info("[S%s/%s] Already completed, skipping", step_id, plat)
                    continue

                state.mark_running(step_id, plat)
                logger.info("[S%s/%s] >> ENTER %s", step_id, plat, step_name)

                try:
                    _execute_step(
                        step_id, project_path, run_dir, plat,
                        yes=yes, force=force, platforms=selected_platforms,
                    )
                    state.mark_completed(step_id, plat)
                    logger.info("[S%s/%s] << EXIT %s (ok)", step_id, plat, step_name)
                except StepError as exc:
                    state.mark_failed(step_id, plat, str(exc))
                    _print_resume_hint(step_id, plat, project_path, error=str(exc))
                    return 1

        elif step_id in GLOBAL_STEPS:
            if state.is_completed(step_id):
                logger.info("[S%s] Already completed, skipping", step_id)
                continue

            state.mark_running(step_id)
            logger.info("[S%s] >> ENTER %s", step_id, step_name)

            try:
                _execute_step(
                    step_id, project_path, run_dir, None,
                    yes=yes, force=force, platforms=selected_platforms,
                )
                state.mark_completed(step_id)
                logger.info("[S%s] << EXIT %s (ok)", step_id, step_name)
            except StepError as exc:
                state.mark_failed(step_id, error=str(exc))
                _print_resume_hint(step_id, None, project_path, error=str(exc))
                return 1

    logger.info("Pipeline completed successfully")
    return 0


def _run_environment_deploy(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    yes: bool = False,
    force: bool = False,
) -> None:
    from .environment.deploy import deploy_plan

    plan_path = run_dir / platform / "environment-plan.json"
    # On resume/force, clear old plan + records so deploy re-generates
    # and re-executes everything (including the --privileged check).
    if force:
        for old_file in [
            plan_path,
            run_dir / platform / "deploy-record.json",
            run_dir / platform / "environment-record.json",
            project_path.parent / "output" / platform / "deploy-record.json",
        ]:
            if old_file.exists():
                old_file.unlink()
                logger.info("Cleared old file: %s", old_file)

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


def _project_adapter(project_path: Path) -> tuple[Any, dict[str, Any]]:
    from .adapters.registry import get_adapter
    from .config import load_environment_config

    try:
        env_config = load_environment_config(project_path)
    except FileNotFoundError:
        env_config = {}

    framework_id = str(env_config.get("framework", "") or "pyflink")
    return get_adapter(framework_id), env_config


def _execute_registered_step(
    step_id: str,
    project_path: Path,
    run_dir: Path,
    platform: str | None,
    *,
    yes: bool = False,
    force: bool = False,
    platforms: list[str] | None = None,
) -> bool:
    from .registry import get_registry
    from .steps import register_builtin_steps

    register_builtin_steps()
    registry = get_registry()
    if step_id not in registry.names():
        return False

    adapter, env_config = _project_adapter(project_path)
    ctx = RunContext(
        adapter=adapter,
        project_path=project_path,
        run_dir=run_dir,
        platform=platform,
        config={
            "yes": yes,
            "force": force,
            "environment": env_config,
            "framework_id": env_config.get("framework") if env_config else None,
            "platforms": platforms,
        },
    )
    registry.get(step_id)().run(ctx)
    return True


def _execute_step(
    step_id: str,
    project_path: Path,
    run_dir: Path,
    platform: str | None,
    *,
    yes: bool = False,
    force: bool = False,
    platforms: list[str] | None = None,
) -> None:
    """Execute a single pipeline step."""
    if _execute_registered_step(
        step_id,
        project_path,
        run_dir,
        platform,
        yes=yes,
        force=force,
        platforms=platforms,
    ):
        return

    if step_id == "3":
        if platform is None:
            raise StepError("step requires a platform")
        _run_environment_deploy(
            project_path,
            run_dir,
            platform,
            yes=yes,
            force=force,
        )

    elif step_id == "4":
        _run_workload_deploy(project_path, run_dir, platform, yes=yes)

    elif step_id == "5a":
        _run_benchmark(project_path, run_dir, platform, force=force)

    elif step_id == "5a.1":
        _run_operator_stage(
            "context", project_path, run_dir, platform, force=force
        )

    elif step_id == "5a.2":
        _run_operator_stage(
            "isolated", project_path, run_dir, platform, force=force
        )

    elif step_id == "5b.1":
        _run_collect_substep(project_path, run_dir, platform, "5b.1", force=force)
    elif step_id == "5b.2":
        _run_collect_substep(project_path, run_dir, platform, "5b.2", force=force)
    elif step_id == "5b.2b":
        _run_collect_substep(project_path, run_dir, platform, "5b.2b", force=force)
    elif step_id == "5b.3":
        _run_collect_substep(project_path, run_dir, platform, "5b.3", force=force)

    elif step_id == "5c":
        _run_acquire_all(
            project_path, run_dir, force=force, platforms=platforms
        )

    elif step_id == "5c.1":
        _run_operator_reports(
            project_path,
            run_dir,
            force=force,
            platforms=platforms,
        )

    elif step_id == "5d":
        _run_operator_compare(project_path, run_dir)

    elif step_id == "6":
        _run_backfill(project_path, run_dir, force=force)

    elif step_id == "6b":
        _run_compare(project_path, run_dir)

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
    """Deploy the workload via the framework's adapter (single source).

    Thin dispatcher: resolves the adapter for the configured framework
    (defaulting to pyflink) and delegates. The per-framework deploy logic lives
    in the adapters (PyFlinkAdapter.deploy_workload etc.), not here.
    """
    from .adapters.registry import get_adapter

    framework = _framework_id_from_project(project_path)
    get_adapter(framework).deploy_workload(project_path, run_dir, platform, yes=yes)


def _framework_id_from_project(project_path: Path) -> str:
    try:
        from .config import load_environment_config
        env_config = load_environment_config(
            project_path,
            allow_example=True,
        )
    except FileNotFoundError:
        env_config = {}
    return str(env_config.get("framework", "") or "pyflink")


def _run_benchmark(
    project_path: Path, run_dir: Path, platform: str,
    *, force: bool = False,
) -> None:
    """Run the benchmark via the framework's adapter (single source).

    Thin dispatcher: resolves the adapter and delegates run_benchmark. The
    per-framework benchmark logic (perf-wrapped queries for pyflink, modalities
    for datajuicer, etc.) lives in the adapters, not here.
    """
    from .adapters.registry import get_adapter

    framework = _framework_id_from_project(project_path)
    get_adapter(framework).run_benchmark(project_path, run_dir, platform, force=force)
    return


def _run_operator_stage(
    mode: str,
    project_path: Path,
    run_dir: Path,
    platform: str | None,
    *,
    force: bool = False,
) -> None:
    """Run an optional operator scope without affecting generic adapters."""

    if platform is None:
        raise StepError("operator analysis step requires a platform")
    from .adapters.registry import get_adapter
    from .config import get_workload_config
    from .contracts.adapter import OperatorAnalysisAdapter

    adapter = get_adapter(_framework_id_from_project(project_path))
    if not isinstance(adapter, OperatorAnalysisAdapter):
        logger.info("Operator analysis is not supported; skipping %s", mode)
        return
    workload = get_workload_config(project_path)
    operator = workload.get("operatorAnalysis") or {}
    if isinstance(operator, dict) and not bool(operator.get("enabled", True)):
        logger.info("Operator analysis is disabled; skipping %s", mode)
        return
    methods = {
        "context": adapter.collect_context_timing,
        "isolated": adapter.collect_operator_timing,
        "profile": adapter.collect_operator_profiles,
    }
    try:
        method = methods[mode]
    except KeyError as exc:
        raise StepError(f"unknown operator stage: {mode}") from exc
    method(project_path, run_dir, platform, force=force)


def _run_operator_compare(project_path: Path, run_dir: Path) -> None:
    """Compare normalized operator evidence when the adapter supports it."""

    if _framework_id_from_project(project_path) != "volcoperatorsim":
        logger.info("No operator comparison configured; skipping")
        return
    from .adapters.registry import get_adapter
    from .adapters.volcoperatorsim.operator_compare import (
        compare_operator_platforms,
    )

    adapter = get_adapter("volcoperatorsim")
    for platform in ("arm", "x86"):
        adapter.normalize_operator_artifacts(
            project_path, run_dir, platform, force=False
        )

    compare_operator_platforms(
        run_dir / "arm", run_dir / "x86", run_dir / "compare" / "operators"
    )


def _run_operator_reports(
    project_path: Path,
    run_dir: Path,
    *,
    force: bool = False,
    platforms: list[str] | None = None,
) -> None:
    """Normalize acquired Volc evidence and render self-contained reports."""

    if _framework_id_from_project(project_path) != "volcoperatorsim":
        logger.info("No operator readable report configured; skipping")
        return
    from .adapters.registry import get_adapter
    from .adapters.volcoperatorsim.operator_report import render_operator_reports

    adapter = get_adapter("volcoperatorsim")
    for platform in platforms or ["arm", "x86"]:
        adapter.normalize_operator_artifacts(
            project_path, run_dir, platform, force=force
        )
        report = render_operator_reports(run_dir / platform)
        logger.info("[%s] Operator readable report: %s", platform, report)


# Pyflink benchmark helpers now live in adapters/pyflink/benchmark.py; these
# aliases keep the orchestrator's collect/remote paths working without
# duplicating the implementations.
from .adapters.pyflink.benchmark import (
    find_container_perf as _find_container_perf,
    find_container_python as _find_container_python,
    parse_tm_count as _parse_tm_count,
)



def _perf_records_csv_is_complete(path: Path) -> bool:
    """Return True when perf_records.csv has a complete header and rows."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            if any(field not in fieldnames for field in _PERF_RECORD_REQUIRED_FIELDS):
                return False
            row_count = 0
            for row in reader:
                row_count += 1
                if None in row:
                    return False
                if any(row.get(field) is None for field in fieldnames):
                    return False
            return row_count > 0
    except (OSError, UnicodeDecodeError, csv.Error):
        return False


def _run_collect_substep(
    project_path: Path, run_dir: Path, platform: str,
    substep: str,
    *,
    force: bool = False,
) -> None:
    """Execute a single 5b sub-step.

    Sub-steps: 5b.1 (perf.data), 5b.2 (perf-kits), 5b.2b (source extraction),
    5b.3 (objdump ASM).  Each checks its own output artifact and skips if present.
    """
    from .config import load_environment_config

    env_config = load_environment_config(project_path)
    if _framework_id(env_config) == "volcoperatorsim":
        if substep == "5b.2":
            _run_operator_stage(
                "profile", project_path, run_dir, platform, force=force
            )
            return
        _validate_volc_collect_checkpoint(run_dir / platform, substep)
        return

    from .remote import build_executor, get_platform_host_ref

    host_ref = get_platform_host_ref(env_config, platform)
    executor = build_executor(host_ref, env_config)

    platform_run_dir = run_dir / platform
    perf_dir = platform_run_dir / "perf" / "data"
    perf_dir.mkdir(parents=True, exist_ok=True)
    perf_data_local = perf_dir / f"perf-{platform}.data"
    perf_csv = perf_dir / "perf_records.csv"

    if substep == "5b.1":
        if perf_data_local.exists() and perf_data_local.stat().st_size > 0:
            logger.info("[5b.1] perf.data already exists (%d bytes), skipping",
                         perf_data_local.stat().st_size)
            return
        perf_container = _find_perf_container(executor, env_config)
        logger.info("[5b.1] Collecting perf.data from %s on %s...", perf_container, platform)
        _collect_binary_from_container(
            executor, perf_container, "/tmp/perf-udf.data", perf_data_local,
        )
        return

    # Sub-steps 5b.2+ need perf.data to exist.
    if not perf_data_local.exists() or perf_data_local.stat().st_size == 0:
        raise StepError(f"[{substep}] perf.data not found — run 5b.1 first")

    if substep == "5b.2":
        if perf_csv.exists() and perf_csv.stat().st_size > 0:
            if _perf_records_csv_is_complete(perf_csv):
                logger.info("[5b.2] perf_records.csv exists, skipping perf-kits on %s", platform)
                return
            logger.warning(
                "[5b.2] perf_records.csv is incomplete on %s, rerunning perf-kits",
                platform,
            )
        perf_container = _find_perf_container(executor, env_config)
        logger.info("[5b.2] Running perf-kits analysis pipeline on %s (timeout=600s)...", platform)
        benchmark = "tpch"
        if _framework_id(env_config) == "datajuicer":
            benchmark = _datajuicer_benchmark_name(env_config)
        elif _framework_id(env_config) == "udfbenchmarking":
            benchmark = _udfbenchmarking_benchmark_name(env_config)
        _run_perf_kits_on_remote(
            executor, perf_data_local, perf_dir, platform, project_path,
            perf_container, benchmark=benchmark, env_config=env_config,
        )
        return

    # Sub-steps 5b.2b+ need perf_records.csv.
    if not perf_csv.exists() or perf_csv.stat().st_size == 0:
        raise StepError(f"[{substep}] perf_records.csv not found — run 5b.2 first")

    if substep == "5b.2b":
        source_map_path = perf_dir / "symbol_source_map.json"
        if source_map_path.exists() and source_map_path.stat().st_size > 10:
            logger.info("[5b.2b] CPython source map exists, skipping extraction on %s", platform)
            return
        perf_container = _find_perf_container(executor, env_config)
        logger.info("[5b.2b] Extracting CPython source for hotspot symbols on %s...", platform)
        _extract_cpython_sources(executor, perf_csv, source_map_path, perf_container)
        return

    if substep == "5b.3":
        asm_dir = platform_run_dir / "asm" / ("arm64" if platform == "arm" else "x86_64")
        asm_dir.mkdir(parents=True, exist_ok=True)
        if list(asm_dir.glob("*.s")):
            logger.info("[5b.3] ASM files exist (%d), skipping objdump on %s",
                         len(list(asm_dir.glob("*.s"))), platform)
            return
        perf_container = _find_perf_container(executor, env_config)
        logger.info("[5b.3] Collecting objdump for hotspot symbols on %s...", platform)
        _collect_asm_from_all_libs(executor, perf_dir, asm_dir, platform, perf_container)
        return

    raise StepError(f"Unknown 5b sub-step: {substep}")


def _validate_volc_collect_checkpoint(
    platform_run_dir: Path, substep: str
) -> None:
    """Validate adapter-owned Volc outputs without invoking PyFlink collectors."""

    expected = {
        "5b.1": (
            platform_run_dir
            / "operators"
            / "manifests"
            / "pipeline_e2e-COMPLETE.json"
        ),
        "5b.2b": (
            platform_run_dir
            / "operators"
            / "manifests"
            / "operator_case_perf-COMPLETE.json"
        ),
        "5b.3": (
            platform_run_dir
            / "operators"
            / "manifests"
            / "operator_case_perf-COMPLETE.json"
        ),
    }
    artifact = expected.get(substep)
    if artifact is None:
        raise StepError(f"Unknown 5b sub-step: {substep}")
    if not artifact.is_file():
        raise StepError(
            f"[{substep}] Volc adapter-owned artifact not found: {artifact}"
        )
    logger.info(
        "[%s] Volc operator acquisition already completed: %s",
        substep,
        artifact.relative_to(platform_run_dir),
    )


def _find_perf_container(executor: "SshExecutor", env_config: dict) -> str:
    """Find which container has perf.data."""
    if _framework_id(env_config) == "datajuicer":
        return _datajuicer_container(env_config)
    if _framework_id(env_config) == "udfbenchmarking":
        return _udfbenchmarking_container(env_config)
    _tm_count = _parse_tm_count(env_config)
    for c in ["flink-jm"] + [f"flink-tm{i}" for i in range(1, _tm_count + 1)]:
        check = executor.run(
            f"docker exec {c} bash -c "
            "'test -s /tmp/perf-udf.data && echo found'",
            timeout=15,
        )
        if "found" in check.stdout:
            logger.info("perf.data found in %s", c)
            return c
    logger.warning("Could not locate perf.data in any container, using JM as fallback")
    return "flink-jm"



def _load_symbol_map(asm_dir: Path) -> dict[str, str]:
    """Load hash→symbol mapping from symbol_map.json."""
    map_path = asm_dir / "symbol_map.json"
    if map_path.exists():
        try:
            return json.loads(map_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _repair_symbol_map_from_collected_files(
    asm_dir: Path,
    so_to_syms: dict[str, list[str]],
) -> None:
    """Ensure local symbol_map.json matches fetched .s files."""
    symbol_map = _load_symbol_map(asm_dir)
    updated = False
    for syms in so_to_syms.values():
        for sym in syms:
            sym_hash = hashlib.md5(sym.encode()).hexdigest()[:8]
            asm_file = asm_dir / f"{sym_hash}.s"
            if asm_file.exists() and asm_file.stat().st_size > 0 and symbol_map.get(sym_hash) != sym:
                symbol_map[sym_hash] = sym
                updated = True
    if updated:
        (asm_dir / "symbol_map.json").write_text(
            json.dumps(symbol_map, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _extract_cpython_sources(
    executor: "SshExecutor",
    perf_csv: Path,
    output_path: Path,
    container: str = "flink-jm",
) -> None:
    """Extract C source code for hotspot symbols from CPython source in container.

    Writes a Python script + symbol list to local temp dir, transfers into the
    container via docker cp, runs the script, and collects the result back via
    docker cp.  Avoids base64 encoding and stdout-through-SSH issues.
    """
    import csv as _csv
    import shlex
    import tempfile

    # Read symbols from perf CSV.
    symbols: set[str] = set()
    try:
        with open(perf_csv, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                if sym and not sym.startswith("0x") and sym != "[unknown]":
                    if len(sym) >= 8 and all(c in "0123456789abcdef" for c in sym.lower()):
                        continue
                    symbols.add(sym)
    except Exception:
        return

    if not symbols:
        return

    # Write extraction script and symbol list to a local temp directory,
    # then docker cp into the container.
    extract_script = textwrap.dedent("""\
        import sys, os, re, json
        src = sys.argv[1]
        sym_file = sys.argv[2]
        output = sys.argv[3]
        with open(sym_file) as f:
            symbols = [l.strip() for l in f if l.strip()]
        result = {}
        c_dirs = [os.path.join(src, d) for d in ['Objects', 'Python', 'Modules', 'Parser']]
        c_files = []
        for d in c_dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if f.endswith('.c'):
                    c_files.append(os.path.join(d, f))
        sym_patterns = {s: re.compile(r'\\b' + re.escape(s) + r'\\s*\\(') for s in symbols}
        for cf in c_files:
            try:
                with open(cf) as fh:
                    lines = fh.readlines()
            except Exception:
                continue
            rel = os.path.relpath(cf, src)
            for i, line in enumerate(lines):
                if '{' in line or line.strip().startswith('//') or line.strip().startswith('#'):
                    continue
                for sym, pat in list(sym_patterns.items()):
                    if sym in result:
                        continue
                    if not pat.search(line):
                        continue
                    depth = 0
                    body_lines = []
                    started = False
                    for j in range(max(0, i-2), min(len(lines), i+200)):
                        body_lines.append(lines[j].rstrip())
                        depth += lines[j].count('{') - lines[j].count('}')
                        if '{' in lines[j]:
                            started = True
                        if started and depth <= 0:
                            break
                    if not started:
                        continue
                    result[sym] = {'sourceFile': rel, 'snippet': '\\n'.join(body_lines)}
                    break
                if len(result) == len(symbols):
                    break
            if len(result) == len(symbols):
                break
        with open(output, 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"extracted:{len(result)}/{len(symbols)}")
    """)

    with tempfile.TemporaryDirectory(prefix="pyframework_src_") as tmp_dir:
        tmp = Path(tmp_dir)
        (tmp / "_extract_src.py").write_text(extract_script, encoding="utf-8")
        (tmp / "_symbols.txt").write_text("\n".join(sorted(symbols)) + "\n", encoding="utf-8")

        # Push to remote host, then docker cp into container.
        host_staging = "/tmp/pyframework-src-extract"
        executor.run(f"rm -rf {host_staging} && mkdir -p {host_staging}", timeout=15)
        executor.push_file(tmp / "_extract_src.py", f"{host_staging}/_extract_src.py")
        executor.push_file(tmp / "_symbols.txt", f"{host_staging}/_symbols.txt")
        executor.run(f"docker cp {host_staging}/. {container}:/tmp/_src_extract/", timeout=30)
        executor.run(
            f"docker exec -u root {container} sh -c "
            "'chown -R flink:flink /tmp/_src_extract 2>/dev/null || true'",
            timeout=15,
        )

    # Ensure CPython source is available (extract from pyenv cache if needed).
    src_prep_script = (
        "if test -d /tmp/cpython-src/Objects; then "
        "echo ok; "
        "else "
        "TB=$(ls /root/.pyenv/cache/Python-*.tar.xz 2>/dev/null | head -1); "
        "if test -n \"$TB\"; then "
        "mkdir -p /tmp/cpython-src && "
        "tar xf \"$TB\" -C /tmp/cpython-src --strip-components=1 && "
        "echo extracted; "
        "else "
        "echo missing; "
        "fi; "
        "fi"
    )
    src_prep = executor.run(
        f"docker exec {container} bash -c {shlex.quote(src_prep_script)}",
        timeout=60,
        stream=True,
    )
    if "ok" not in src_prep.stdout and "extracted" not in src_prep.stdout:
        logger.warning("CPython source not available in container, skipping source extraction")
        executor.run(f"docker exec {container} rm -rf /tmp/_src_extract", timeout=15)
        executor.run(f"rm -rf {host_staging}", timeout=15)
        return

    # Run the extraction script inside the container.
    result = executor.run(
        f"docker exec {container} python3 /tmp/_src_extract/_extract_src.py "
        f"/tmp/cpython-src /tmp/_src_extract/_symbols.txt /tmp/_src_extract/_result.json",
        timeout=180,
        stream=True,
    )
    logger.info("Source extraction: %s", result.stdout.strip() if result.stdout else result.stderr[:200])

    # Collect result via docker cp.
    host_result = "/tmp/pyframework-src-result.json"
    executor.run(f"docker cp {container}:/tmp/_src_extract/_result.json {host_result}", timeout=30)
    local_result = output_path.parent / "_result_tmp.json"
    local_result.parent.mkdir(parents=True, exist_ok=True)
    executor.fetch_file(host_result, local_result)

    try:
        source_map = json.loads(local_result.read_text(encoding="utf-8"))
        if source_map:
            output_path.write_text(
                json.dumps(source_map, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            logger.info("Extracted CPython source for %d/%d symbols", len(source_map), len(symbols))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read source extraction result: %s", e)
    finally:
        local_result.unlink(missing_ok=True)

    # Cleanup.
    executor.run(f"docker exec {container} rm -rf /tmp/_src_extract", timeout=15)
    executor.run(f"rm -rf {host_staging} {host_result}", timeout=15)


def _collect_asm_from_all_libs(
    executor: "SshExecutor",
    perf_dir: Path,
    asm_dir: Path,
    platform: str,
    container: str = "flink-jm",
) -> None:
    """Collect objdump for top hotspot symbols from ALL shared libraries.

    Writes a Python script that runs entirely inside the container — finds
    libraries, runs objdump, extracts symbols, writes .s files and
    symbol_map.json into an output directory.  Then docker cp the results
    back.  Avoids per-library round trips through SSH.
    """
    import tempfile

    from .acquisition.machine_code import _discover_libs_from_perf

    perf_csv = perf_dir / "perf_records.csv"
    if not perf_csv.exists():
        logger.warning("perf_records.csv not found, skipping multi-lib ASM collection")
        return

    so_to_syms = _discover_libs_from_perf(perf_csv)
    if not so_to_syms:
        logger.warning("No real shared-library symbols found for ASM collection")
        return

    # Load existing symbol_map so the in-container script can skip collected symbols.
    existing_map = _load_symbol_map(asm_dir)

    # Write a single Python script + JSON manifest to run inside the container.
    asm_script = textwrap.dedent("""\
        import sys, os, re, json, subprocess, hashlib

        manifest = sys.argv[1]
        output_dir = sys.argv[2]

        with open(manifest) as f:
            data = json.load(f)
        so_to_syms = data['so_to_syms']
        existing_map = data.get('existing_map', {})

        collected_hashes = set(existing_map.keys()) if existing_map else set()

        symbol_map = dict(existing_map)
        search_dirs = '/usr/lib /usr/local/lib /opt /lib /root/.pyenv /root'.split()
        total_collected = 0

        def find_so(so_name):
            base = os.path.basename(so_name)
            stem = base.split('.so')[0]
            for d in search_dirs:
                for root, dirs, files in os.walk(d):
                    for fn in files:
                        if fn == base:
                            return os.path.join(root, fn)
                        if '.so' in fn and stem in fn:
                            return os.path.join(root, fn)
            return None

        for so_name, syms in sorted(so_to_syms.items()):
            so_path = find_so(so_name)
            if not so_path:
                print(f"skip:{so_name}:not_found")
                continue

            remaining = {}
            for sym in syms:
                h = hashlib.md5(sym.encode()).hexdigest()[:8]
                if h not in collected_hashes:
                    remaining[sym] = h

            if not remaining:
                print(f"{so_name}: already_collected/{len(syms)}")
                continue

            # Generate awk script: for each symbol, match /<symbol.*>:/ to /^$/
            awk_file = os.path.join(output_dir, '_extract.awk')
            with open(awk_file, 'w') as f:
                for sym, h in remaining.items():
                    f.write('/<' + sym + '.*>:/ { file="' + output_dir + '/' + h + '.s"; printing=1 }\\n')
                f.write('/^$/ { if (printing) { close(file); printing=0 }; next }\\n')
                f.write('printing { print > file }\\n')
                f.write('END { if (printing) close(file) }\\n')

            # objdump -d (no -S) + awk extracts each function's disassembly
            cmd = 'objdump -d ' + so_path + ' | awk -f ' + awk_file
            print(f"CMD:{so_name}: {cmd}")
            subprocess.run(cmd, shell=True, timeout=300)

            # Check results
            for sym, h in remaining.items():
                out_file = os.path.join(output_dir, h + '.s')
                if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
                    symbol_map[h] = sym
                    collected_hashes.add(h)
                elif os.path.exists(out_file):
                    os.unlink(out_file)

            if os.path.exists(awk_file):
                os.unlink(awk_file)

            collected = sum(1 for s in syms
                           if hashlib.md5(s.encode()).hexdigest()[:8] in collected_hashes)
            no_addr = [s for s in remaining
                       if hashlib.md5(s.encode()).hexdigest()[:8] not in collected_hashes]
            if no_addr:
                print(f"{so_name}: no_addr symbols: {no_addr}")
            print(f"{so_name}: collected={collected},no_addr={len(no_addr)}/{len(syms)}")
            total_collected += collected

        map_path = os.path.join(output_dir, 'symbol_map.json')
        with open(map_path, 'w') as f:
            json.dump(symbol_map, f, ensure_ascii=False, indent=2)
        print(f"done:total={total_collected},map_entries={len(symbol_map)}")
    """)

    manifest = {
        "so_to_syms": so_to_syms,
        "existing_map": existing_map,
    }

    with tempfile.TemporaryDirectory(prefix="pyframework_asm_") as tmp_dir:
        tmp = Path(tmp_dir)
        (tmp / "_asm_collect.py").write_text(asm_script, encoding="utf-8")
        (tmp / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8",
        )

        # Push to remote host, then docker cp into container.
        host_staging = "/tmp/pyframework-asm-collect"
        executor.run(f"rm -rf {host_staging} && mkdir -p {host_staging}", timeout=15)
        executor.push_file(tmp / "_asm_collect.py", f"{host_staging}/_asm_collect.py")
        executor.push_file(tmp / "_manifest.json", f"{host_staging}/_manifest.json")
        executor.run(f"docker cp {host_staging}/. {container}:/tmp/_asm_collect/", timeout=30)
        executor.run(
            f"docker exec -u root {container} sh -c "
            "'chown -R flink:flink /tmp/_asm_collect 2>/dev/null || true'",
            timeout=15,
        )

    # Create output directory inside container and run the script.
    executor.run(
        f"docker exec {container} mkdir -p /tmp/_asm_output", timeout=15,
    )
    executor.run(
        f"docker exec -u root {container} sh -c "
        "'chown flink:flink /tmp/_asm_output 2>/dev/null || true'",
        timeout=15,
    )
    result = executor.run(
        f"docker exec {container} python3 /tmp/_asm_collect/_asm_collect.py "
        f"/tmp/_asm_collect/_manifest.json /tmp/_asm_output",
        timeout=600,
        stream=True,
    )
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            logger.info("  ASM: %s", line)

    # Collect results: docker cp output dir → remote host → scp → local.
    host_output = "/tmp/pyframework-asm-output"
    executor.run(f"rm -rf {host_output}", timeout=15)
    executor.run(f"docker cp {container}:/tmp/_asm_output/. {host_output}", timeout=120)
    executor.fetch_dir(host_output, asm_dir)
    _repair_symbol_map_from_collected_files(asm_dir, so_to_syms)

    # Cleanup container + remote host.
    executor.run(f"docker exec {container} rm -rf /tmp/_asm_collect /tmp/_asm_output", timeout=15)
    executor.run(f"rm -rf {host_staging} {host_output}", timeout=15)

    logger.info("ASM collection: %d files in %s", len(list(asm_dir.glob("*.s"))), asm_dir)


def _run_perf_kits_on_remote(
    executor: "SshExecutor",
    perf_data_local: Path,
    perf_dir: Path,
    platform: str,
    project_path: Path | None = None,
    container: str = "flink-jm",
    *,
    benchmark: str = "tpch",
    env_config: dict | None = None,
) -> None:
    """Run the analyze/perf pipeline inside the container.

    Running inside the container gives perf report access to the exact
    binaries (libpython3.14.so, etc.) so symbols resolve correctly.

    The scripts ship as flat files into the container (the container cannot
    import this repo's package); they live in pyframework_pipeline/analyze/.
    """
    # Resolve analyze scripts dir: project_path is projects/<id>/project.yaml,
    # repo root is project_path.parent.parent.parent.
    if project_path:
        repo_root = project_path.parent.parent.parent
    else:
        repo_root = Path(__file__).resolve().parents[2]
    scripts_dir = repo_root / "pipelines" / "pyframework_pipeline" / "analyze"
    if not scripts_dir.exists():
        logger.warning("analyze scripts not found at %s, skipping remote pipeline", scripts_dir)
        return

    container_kits = "/tmp/pyframework-perf-kits-scripts"
    container_output = "/tmp/pyframework-perf-kits-output"
    perf_data_container = "/tmp/perf-udf.data"
    python_bin = _find_container_python(executor, env_config, container=container)
    perf_bin = _find_container_perf(executor, container=container)

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
        f"docker exec {container} rm -rf {container_kits}",
        timeout=30,
    )
    executor.run(
        f"docker cp {host_staging}/. {container}:{container_kits}",
        timeout=30,
    )
    executor.run(f"rm -rf {host_staging}", timeout=30)

    # Run the pipeline inside the container.
    logger.info("Running python-performance-kits pipeline inside %s (%s)...", container, platform)
    result = executor.run(
        f"docker exec {container} {python_bin} "
        f"{container_kits}/run_single_platform_pipeline.py "
        f"{perf_data_container} -o {container_output} "
        f"--benchmark {benchmark} --platform {platform} "
        f"--perf-bin {perf_bin} "
        f"--skip-annotate --no-print-report",
        timeout=600,
        stream=True,
    )
    if result.returncode != 0:
        raise StepError(
            f"perf-kits pipeline failed inside {container} (exit {result.returncode}):\n"
            f"  Command: {python_bin} {container_kits}/run_single_platform_pipeline.py ...\n"
            f"  stderr: {result.stderr[:500]}\n"
            f"  stdout: {result.stdout[:500]}"
        )

    # Collect outputs from container via host staging.
    host_output = "/tmp/pyframework-perf-kits-output"
    executor.run(f"rm -rf {host_output}", timeout=30)
    cp_result = executor.run(
        f"docker cp {container}:{container_output}/ {host_output}",
        timeout=120,
        stream=True,
    )
    if cp_result.returncode != 0:
        raise StepError(
            f"docker cp perf-kits output failed: {cp_result.stdout}"
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

    # Cleanup host staging only (keep container output for inspection).
    executor.run(f"docker exec {container} rm -rf {container_kits}", timeout=30)
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
    staging = f"/tmp/_collect_{local_path.name}"
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
    executor.run(f"rm -f {host_tmp}", timeout=30)
    cp_result = executor.run(
        f"docker cp {container}:{staging} {host_tmp}",
        timeout=120,
        stream=True,
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


def _run_acquire_all(
    project_path: Path,
    run_dir: Path,
    *,
    force: bool = False,
    platforms: list[str] | None = None,
) -> None:
    from .acquisition.timing import collect_timing

    config = load_project_config(project_path)
    run_config = get_run_config(project_path)
    if platforms is None:
        platforms = run_config.get("platforms", [])

    framework = _framework_id_from_project(project_path)
    for plat in platforms:
        plat_dir = run_dir / plat
        if framework == "volcoperatorsim":
            from .adapters.volcoperatorsim.acquisition_manifest import (
                build_acquisition_manifest,
            )

            manifest = build_acquisition_manifest(plat_dir, platform=plat)
            logger.info("Volc acquisition manifest %s: %s", plat, manifest)
            continue
        # Step 5a's _merge_wall_clock_times writes timing-normalized.json
        # directly from container logs.  Only fall back to log-file parsing
        # if step 5a didn't produce the file.
        timing_file = plat_dir / "timing" / "timing-normalized.json"
        if timing_file.exists() and timing_file.stat().st_size > 10:
            import json as _json
            existing = _json.loads(timing_file.read_text(encoding="utf-8"))
            cases = existing.get("cases", [])
            logger.info("Timing %s: %d cases (from step 5a)", plat, len(cases))
        else:
            stdout_files = list(plat_dir.glob("tm-stdout-*.log"))
            timing_result = collect_timing(plat_dir, plat, stdout_files or None)
            logger.info("Timing %s: %d cases", plat, len(timing_result.get("cases", [])))

        # Perf-kits and objdump already ran on remote in step 5b.
        # The local collect_perf/collect_asm would fail because the binaries
        # (libpython3.14.so, etc.) live inside the Docker container, not locally.
        perf_data_dir = plat_dir / "perf" / "data"
        asm_dir = plat_dir / "asm" / ("arm64" if plat == "arm" else "x86_64")
        perf_csv = perf_data_dir / "perf_records.csv"
        asm_files = list(asm_dir.glob("*.s")) if asm_dir.exists() else []
        logger.info("Perf %s: %s", plat,
                     "collected" if perf_csv.exists() and perf_csv.stat().st_size > 0 else "missing")
        logger.info("ASM %s: %s", plat,
                     f"{len(asm_files)} files" if asm_files else "missing")


def _run_backfill(project_path: Path, run_dir: Path, *, force: bool = False) -> None:
    from .config import get_run_config

    run_config = get_run_config(project_path)
    platforms = run_config.get("platforms", [])

    if len(platforms) < 2:
        raise StepError("Need at least 2 platforms for backfill")

    arm_dir = run_dir / platforms[0]
    x86_dir = run_dir / platforms[1]

    if _framework_id_from_project(project_path) == "volcoperatorsim":
        for platform_dir in (arm_dir, x86_dir):
            records = platform_dir / "operators" / "operator-records.jsonl"
            if not records.is_file():
                raise StepError(
                    f"Volc normalized operator records missing: {records}"
                )
        logger.info(
            "[S6] Volc adapter normalization already completed during acquisition"
        )
        return

    from .backfill.pipeline import run_backfill

    rc = run_backfill(project_path, arm_dir, x86_dir)
    if rc != 0:
        raise StepError(
            f"Backfill failed (rc={rc}). Check logs above for which sub-module failed.\n"
            f"  ARM dir: {arm_dir}\n"
            f"  x86 dir: {x86_dir}"
        )


def _run_compare(project_path: Path, run_dir: Path) -> None:
    """Step 6b: run cross-platform comparison using python-performance-kits."""
    arm_dir = run_dir / "arm"
    x86_dir = run_dir / "x86"

    if not arm_dir.is_dir():
        raise StepError(f"ARM run directory not found: {arm_dir}")
    if not x86_dir.is_dir():
        raise StepError(f"x86 run directory not found: {x86_dir}")

    if _framework_id_from_project(project_path) == "volcoperatorsim":
        from .adapters.volcoperatorsim.operator_compare import (
            compare_operator_platforms,
        )

        compare_operator_platforms(arm_dir, x86_dir, run_dir / "compare")
        logger.info("[S6b] Volc operator comparison complete: %s", run_dir / "compare")
        return

    from .compare.pipeline import run_compare

    result = run_compare(project_path, arm_dir, x86_dir)
    if result.get("status") != "completed":
        raise StepError(f"Compare failed: {result}")
    logger.info("[S6b] Compare complete: %s", result.get("output_dir", ""))


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
