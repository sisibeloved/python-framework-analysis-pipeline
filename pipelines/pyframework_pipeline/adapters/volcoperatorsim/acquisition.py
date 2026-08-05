"""Black-box acquisition for the pinned Volc Operator Sim checkout.

The target checkout remains read-only.  Derived tasks are written below the
Host-persistent operator cache and executed by the target runner and capture
scripts inside the benchmark container.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import re
import shlex
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ...contracts.step import StepError
from .operator_plan import (
    build_operator_plan,
    render_full_task,
    render_isolated_task,
    render_snapshot_task,
)
from .perf_symbol_bundle import IDENTITY_POLICY
from .snapshot import validate_snapshot_manifest


logger = logging.getLogger(__name__)
_TARGET_ROOT = "/opt/volc_operator_sim"
_TARGET_INPUT_MARKER = "PYFRAMEWORK_TARGET_INPUTS="
_SNAPSHOT_MANIFEST_MARKER = "PYFRAMEWORK_SNAPSHOT_MANIFEST="
_CAPTURE_CASE_INVENTORY_MARKER = "PYFRAMEWORK_CAPTURE_CASE_INVENTORY="
_PYSPY_PROFILE_TIMEOUT_SECONDS = 90
_DEFAULT_PERF_EVENTS = (
    "cycles,instructions,cache-references,cache-misses,"
    "L1-dcache-loads,L1-dcache-load-misses,LLC-loads,LLC-load-misses,"
    "branches,branch-misses,context-switches,cpu-migrations,page-faults"
)


@dataclass(frozen=True)
class VolcAcquisitionSettings:
    container: str
    host_data_root: str
    revision: str
    daft_python: str
    datajuicer_python: str
    group: str
    profile: str
    rounds: int
    timeout: int
    operator_enabled: bool
    context_timing: bool
    isolated_timing: bool
    profiling: bool
    operator_warmup: int
    operator_rounds: int
    perf_frequency: int
    perf_events: str
    min_perf_samples: int = 5000
    context_perf_enabled: bool = False
    context_perf_split: bool = False
    context_perf_frequency: int = 99
    fast_operator_min_samples: int = 5000
    context_fast_operator_calls: Mapping[str, int] | None = None
    operator_group: str = ""
    operator_tasks: tuple[str, ...] = ()
    operator_engines: tuple[str, ...] = ()
    task_input_overrides: Mapping[str, Mapping[str, Any]] | None = None
    workload_cpu_set: str = ""
    observer_cpu_set: str = ""


def run_volc_acquisition(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> Path:
    """Collect only target-owned pipeline E2E evidence."""

    platform_dir = run_dir / platform
    timing_path = platform_dir / "timing" / "timing-normalized.json"
    if _prepare_stage_resume(platform_dir, "pipeline_e2e", force):
        return timing_path
    context = _prepare_volc_stage(project_path, run_dir, platform)
    settings = context[1]
    executor = context[2]
    platform_dir = context[5]
    remote_root = context[6]
    local_raw = context[7]
    recover_remote = _should_recover_remote(
        executor, remote_root, local_raw, "pipeline_e2e", force=force
    )
    if force:
        _archive_stage_scope(executor, remote_root, local_raw, "pipeline_e2e")

    def collect() -> None:
        if recover_remote:
            logger.info("Recovering remote COMPLETE scope pipeline_e2e")
            return
        if settings.operator_tasks or settings.task_input_overrides:
            overlays = _write_and_push_full_task_overlays(
                executor=executor,
                plan=context[3],
                task_documents=context[4],
                platform_dir=platform_dir,
                remote_root=remote_root,
                settings=settings,
                scope="pipeline_e2e",
            )
            profile = _full_task_profile("pipeline_e2e")
            for task in context[3]["tasks"]:
                pipeline_id = str(task["pipelineId"])
                for engine_value in task["engines"]:
                    engine = str(engine_value)
                    _run_checked(
                        executor,
                        _capture_overlay_command(
                            settings=settings,
                            remote_out=f"{remote_root}/pipeline_e2e",
                            task_path=overlays[pipeline_id],
                            engine=engine,
                            profile=profile,
                            case=pipeline_id,
                            mode="timing",
                            scope="pipeline_e2e",
                        ),
                        scope="pipeline_e2e",
                        timeout=settings.timeout,
                    )
        else:
            _run_checked(
                executor,
                _pipeline_e2e_command(settings, remote_root),
                scope="pipeline_e2e",
                timeout=settings.timeout,
            )

    _collect_and_fetch(
        executor=executor,
        remote_root=remote_root,
        local_raw=local_raw,
        scope="pipeline_e2e",
        collect=collect,
        fetch_timeout=settings.timeout,
    )
    _normalize_pipeline_timing(local_raw, timing_path, platform)
    _write_local_stage_complete(platform_dir, "pipeline_e2e")
    return timing_path


def plan_volc_operator_cases(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> Path:
    """Read pinned target inputs and build a plan without running a workload."""

    del force
    context = _prepare_volc_stage(project_path, run_dir, platform)
    return context[5] / "operators" / "operator-plan.json"


def collect_volc_context_timing(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> Path:
    """Collect pipeline-context operator timing as an independent stage."""

    output = run_dir / platform / "operators" / "raw" / "pipeline_context"
    if _prepare_stage_resume(run_dir / platform, "pipeline_context", force):
        return output
    context = _prepare_volc_stage(project_path, run_dir, platform)
    settings, executor, plan = context[1], context[2], context[3]
    remote_root, local_raw = context[6], context[7]
    recover_remote = _should_recover_remote(
        executor, remote_root, local_raw, "pipeline_context", force=force
    )
    if force:
        _archive_stage_scope(executor, remote_root, local_raw, "pipeline_context")

    def collect() -> None:
        if recover_remote:
            logger.info("Recovering remote COMPLETE scope pipeline_context")
            return
        if not settings.operator_enabled or not settings.context_timing:
            return
        completed_cases = _read_completed_capture_cases(
            executor, f"{remote_root}/pipeline_context"
        )
        full_overlays = (
            _write_and_push_full_task_overlays(
                executor=executor,
                plan=plan,
                task_documents=context[4],
                platform_dir=context[5],
                remote_root=remote_root,
                settings=settings,
                scope="pipeline_context",
            )
            if settings.task_input_overrides or settings.context_perf_enabled
            else {}
        )
        for task in plan["tasks"]:
            for engine in task["engines"]:
                pipeline_id = str(task["pipelineId"])
                engine_id = str(engine)
                case = (
                    f"pipeline_context__{_slug(pipeline_id)}__{_slug(engine_id)}"
                )
                if completed_cases.get((case, engine_id), {}).get("ok"):
                    logger.info(
                        "Recovering completed pipeline_context case %s/%s",
                        case,
                        engine_id,
                    )
                    continue
                context_mode = (
                    "perfrecord" if settings.context_perf_enabled else "timing"
                )
                command = (
                    _capture_overlay_command(
                        settings=settings,
                        remote_out=f"{remote_root}/pipeline_context",
                        task_path=full_overlays[pipeline_id],
                        engine=engine_id,
                        profile=_full_task_profile("pipeline_context"),
                        case=case,
                        mode=context_mode,
                        scope="pipeline_context",
                        perf_frequency=(
                            settings.context_perf_frequency
                            if settings.context_perf_enabled
                            else None
                        ),
                    )
                    if full_overlays
                    else _context_command(
                        settings,
                        remote_root,
                        pipeline_id,
                        engine_id,
                    )
                )
                def run_context_case() -> bool:
                    _run_checked(
                        executor,
                        command,
                        scope="pipeline_context",
                        timeout=settings.timeout,
                    )
                    return True

                if settings.context_perf_enabled:
                    _run_with_profile_cpu_envelope(
                        executor=executor,
                        settings=settings,
                        action=run_context_case,
                    )
                else:
                    run_context_case()

    _collect_and_fetch(
        executor=executor,
        remote_root=remote_root,
        local_raw=local_raw,
        scope="pipeline_context",
        collect=collect,
        fetch_timeout=settings.timeout,
        platform_dir=context[5],
        thin_context_perf=settings.context_perf_enabled,
    )
    return output


def collect_volc_operator_timing(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> Path:
    """Build snapshots and collect isolated operator timing only."""

    output = run_dir / platform / "operators" / "raw" / "operator_case_e2e"
    if _prepare_stage_resume(run_dir / platform, "operator_case_e2e", force):
        return output
    context = _prepare_volc_stage(project_path, run_dir, platform)
    settings, executor, plan, task_documents = context[1:5]
    platform_dir, remote_root, local_raw = context[5:8]
    if not settings.operator_enabled or not settings.isolated_timing:
        if settings.context_perf_enabled and settings.context_perf_split:
            _write_context_perf_skip_markers(platform_dir, settings)
        else:
            _write_json(
                output / "SKIPPED.json",
                {
                    "schemaVersion": 1,
                    "scope": "operator_case_e2e",
                    "status": "skipped",
                    "measurementPolicy": "isolated_timing_disabled",
                    "reason": (
                        "Operator analysis is disabled."
                        if not settings.operator_enabled
                        else "isolatedTiming is disabled for this project."
                    ),
                },
            )
        return output
    recover_remote = _should_recover_remote(
        executor, remote_root, local_raw, "operator_case_e2e", force=force
    )
    if force:
        _archive_stage_scope(executor, remote_root, local_raw, "operator_case_e2e")

    case_failures: list[dict[str, Any]] = []

    def collect() -> None:
        if recover_remote:
            logger.info("Recovering remote COMPLETE scope operator_case_e2e")
            return
        if not settings.operator_enabled or not settings.isolated_timing:
            return
        overlays = _write_and_push_overlays(
            executor=executor,
            plan=plan,
            task_documents=task_documents,
            platform_dir=platform_dir,
            remote_root=remote_root,
            settings=settings,
        )
        snapshots = _build_snapshots(
            executor=executor,
            plan=plan,
            overlay_paths=overlays,
            task_documents=task_documents,
            settings=settings,
            remote_root=remote_root,
            failures=case_failures,
        )
        case_failures.extend(_run_isolated_operators(
            executor=executor,
            plan=plan,
            overlay_paths=overlays,
            task_documents=task_documents,
            snapshot_inputs=snapshots,
            settings=settings,
            remote_root=remote_root,
            timing=True,
            profiling=False,
        ))
        _write_case_failures(
            local_raw, "operator_case_e2e", case_failures
        )

    _collect_and_fetch(
        executor=executor,
        remote_root=remote_root,
        local_raw=local_raw,
        scope="operator_case_e2e",
        collect=collect,
        fetch_timeout=settings.timeout,
        additional_scopes=("snapshot_build",),
        platform_dir=platform_dir,
    )
    return output


def collect_volc_operator_profiles(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> Path:
    """Collect isolated perf/flamegraph/ASM evidence without timing rounds."""

    output = run_dir / platform / "operators" / "raw" / "operator_case_perf"
    if _prepare_stage_resume(run_dir / platform, "operator_case_perf", force):
        return output
    context = _prepare_volc_stage(project_path, run_dir, platform)
    settings, executor, plan, task_documents = context[1:5]
    platform_dir, remote_root, local_raw = context[5:8]
    recover_remote = _should_recover_remote(
        executor, remote_root, local_raw, "operator_case_perf", force=force
    )
    if force:
        _archive_stage_scope(executor, remote_root, local_raw, "operator_case_perf")

    case_failures: list[dict[str, Any]] = []

    def collect() -> None:
        if recover_remote:
            logger.info("Recovering remote COMPLETE scope operator_case_perf")
            return
        if not settings.operator_enabled or not settings.profiling:
            return
        if settings.context_perf_enabled and settings.context_perf_split:
            support = _push_context_perf_support(
                executor=executor,
                plan=plan,
                platform_dir=platform_dir,
                settings=settings,
            )
            completed_windows = _read_completed_capture_cases(
                executor,
                f"{remote_root}/operator_case_perf",
            )
            for task in plan["tasks"]:
                pipeline_id = str(task["pipelineId"])
                for engine_value in task["engines"]:
                    engine = str(engine_value)
                    if _context_perf_windows_complete(
                        task,
                        engine,
                        completed_windows,
                    ):
                        logger.info(
                            "Reusing complete context perf windows for %s/%s",
                            pipeline_id,
                            engine,
                        )
                    else:
                        _run_checked(
                            executor,
                            _context_perf_split_command(
                                settings=settings,
                                remote_root=remote_root,
                                plan_path=support["plan"],
                                splitter_path=support["splitter"],
                                symbolizer_path=support["symbolizer"],
                                pipeline_id=pipeline_id,
                                engine=engine,
                            ),
                            scope="operator_case_perf",
                            timeout=settings.timeout,
                        )
                _run_context_fast_operator_profiles(
                    executor=executor,
                    settings=settings,
                    remote_root=remote_root,
                    plan=plan,
                    task=task,
                    support=support,
                )
            _write_case_failures(local_raw, "operator_case_perf", case_failures)
            return
        overlays = _write_and_push_overlays(
            executor=executor,
            plan=plan,
            task_documents=task_documents,
            platform_dir=platform_dir,
            remote_root=remote_root,
            settings=settings,
        )
        snapshots = _build_snapshots(
            executor=executor,
            plan=plan,
            overlay_paths=overlays,
            task_documents=task_documents,
            settings=settings,
            remote_root=remote_root,
            failures=case_failures,
        )
        case_failures.extend(_run_isolated_operators(
            executor=executor,
            plan=plan,
            overlay_paths=overlays,
            task_documents=task_documents,
            snapshot_inputs=snapshots,
            settings=settings,
            remote_root=remote_root,
            timing=False,
            profiling=True,
        ))
        _write_case_failures(
            local_raw, "operator_case_perf", case_failures
        )

    _collect_and_fetch(
        executor=executor,
        remote_root=remote_root,
        local_raw=local_raw,
        scope="operator_case_perf",
        collect=collect,
        fetch_timeout=settings.timeout,
        additional_scopes=(
            ()
            if settings.context_perf_enabled and settings.context_perf_split
            else ("snapshot_build",)
        ),
        platform_dir=platform_dir,
        thin_operator_perf=(
            settings.context_perf_enabled and settings.context_perf_split
        ),
    )
    _write_context_perf_skip_markers(platform_dir, settings)
    return output


def _write_context_perf_skip_markers(
    platform_dir: Path, settings: VolcAcquisitionSettings
) -> None:
    if not (settings.context_perf_enabled and settings.context_perf_split):
        return
    payload = {
        "schemaVersion": 1,
        "status": "skipped",
        "measurementPolicy": "single_pass_context_perf",
        "reason": (
            "The frozen per-op E2E run is also the perf source; replaying the "
            "OCR chain or building isolation snapshots would duplicate the "
            "dominant workload."
        ),
    }
    for scope in ("pipeline_e2e", "snapshot_build", "operator_case_e2e"):
        _write_json(
            platform_dir / "operators" / "raw" / scope / "SKIPPED.json",
            {**payload, "scope": scope},
        )


def _prepare_volc_stage(
    project_path: Path, run_dir: Path, platform: str
) -> tuple[
    Mapping[str, Any],
    VolcAcquisitionSettings,
    Any,
    dict[str, Any],
    dict[str, Mapping[str, Any]],
    Path,
    str,
    Path,
]:
    from ...config import get_workload_config, load_environment_config
    from ...remote import build_executor, get_platform_host_ref

    workload = get_workload_config(project_path)
    env_config = load_environment_config(project_path)
    settings = _load_settings(workload, env_config, platform=platform)
    executor = build_executor(
        get_platform_host_ref(env_config, platform, role="client"), env_config
    )
    operator_group = settings.operator_group or settings.group
    target_inputs = _read_target_inputs(
        executor, settings.container, operator_group, settings.timeout
    )
    actual_revision = str(target_inputs.get("revision") or "")
    if actual_revision != settings.revision:
        raise StepError(
            "Volc Operator Sim revision mismatch: "
            f"expected={settings.revision} actual={actual_revision}"
        )
    task_documents = {
        str(key): _mapping(value, f"taskDocuments.{key}")
        for key, value in _mapping(
            target_inputs.get("taskDocuments"), "taskDocuments"
        ).items()
    }
    task_documents = _apply_task_input_overrides(
        task_documents, settings.task_input_overrides or {}
    )
    task_documents = _normalize_real_task_contracts(task_documents)
    platform_dir = run_dir / platform
    environment_path = platform_dir / "environment-record.json"
    environment_payload = (
        environment_path.read_bytes() if environment_path.is_file() else b"unavailable"
    )
    environment_sha = _environment_identity_sha256(environment_payload)
    run_id = _stable_run_id(
        run_dir,
        platform,
        settings,
        environment_fingerprint_sha256=environment_sha,
    )
    plan = build_operator_plan(
        formal_config=_mapping(target_inputs.get("formalConfig"), "formalConfig"),
        task_documents=task_documents,
        group=operator_group,
        run_id=run_id,
        platform=platform,
        source_revision=settings.revision,
        selected_pipelines=settings.operator_tasks or None,
        selected_engines=settings.operator_engines or None,
    )
    plan_identity = hashlib.sha256(
        json.dumps(
            plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    run_fingerprint = {
        "schemaVersion": 1,
        "platformId": platform,
        "sourceRevision": settings.revision,
        "environmentFingerprintSha256": environment_sha,
        "operatorPlanIdentitySha256": plan_identity,
        "group": settings.group,
        "operatorGroup": operator_group,
        "operatorTasks": list(settings.operator_tasks),
        "operatorEngines": list(settings.operator_engines),
        "taskInputOverrides": settings.task_input_overrides or {},
        "profile": settings.profile,
        "workloadCpuSet": settings.workload_cpu_set,
        "observerCpuSet": settings.observer_cpu_set,
        "contextPerf": {
            "enabled": settings.context_perf_enabled,
            "splitByOperatorBoundary": settings.context_perf_split,
            "perfFrequency": settings.context_perf_frequency,
            "fastOperatorMinSamples": settings.fast_operator_min_samples,
            "fastOperatorCalls": settings.context_fast_operator_calls or {},
        },
    }
    run_fingerprint_sha = hashlib.sha256(
        json.dumps(
            run_fingerprint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    run_fingerprint["runFingerprintSha256"] = run_fingerprint_sha
    plan["environmentFingerprintSha256"] = environment_sha
    plan["runFingerprintSha256"] = run_fingerprint_sha
    _write_json(platform_dir / "operators" / "operator-plan.json", plan)
    _write_json(
        platform_dir / "operators" / "run-fingerprint.json", run_fingerprint
    )
    remote_root = (
        f"{settings.host_data_root}/bench-results/pyframework/{run_id}/{platform}"
    )
    local_raw = platform_dir / "operators" / "raw"
    local_raw.mkdir(parents=True, exist_ok=True)
    return (
        workload,
        settings,
        executor,
        plan,
        task_documents,
        platform_dir,
        remote_root,
        local_raw,
    )


def _collect_and_fetch(
    *,
    executor: Any,
    remote_root: str,
    local_raw: Path,
    scope: str,
    collect: Any,
    fetch_timeout: int,
    additional_scopes: tuple[str, ...] = (),
    platform_dir: Path | None = None,
    thin_context_perf: bool = False,
    thin_operator_perf: bool = False,
) -> None:
    failure: Exception | None = None
    try:
        collect()
        _write_json(
            local_raw / f"collection-succeeded-{scope}.json",
            {
                "schemaVersion": 1,
                "scope": scope,
                "remoteRoot": remote_root,
                "status": "collection-succeeded",
            },
        )
        _write_remote_stage_complete(executor, remote_root, scope)
    except Exception as exc:
        failure = exc
        _write_json(
            local_raw / f"acquisition-failure-{scope}.json",
            {
                "schemaVersion": 1,
                "scope": scope,
                "errorType": type(exc).__name__,
                "error": str(exc),
                "remoteRoot": remote_root,
            },
        )
    finally:
        fetched_artifacts = []
        for artifact_scope in (*additional_scopes, scope):
            remote_scope = f"{remote_root}/{artifact_scope}"
            if thin_context_perf and artifact_scope == "pipeline_context":
                remote_scope = _prepare_remote_thin_context_view(
                    executor,
                    source=remote_scope,
                    destination=(
                        f"{remote_root}/transfer-views/pipeline_context-"
                        f"{time.time_ns()}"
                    ),
                )
            if thin_operator_perf and artifact_scope == "operator_case_perf":
                remote_scope = _prepare_remote_thin_context_view(
                    executor,
                    source=remote_scope,
                    destination=(
                        f"{remote_root}/transfer-views/operator_case_perf-"
                        f"{time.time_ns()}"
                    ),
                )
            fetched_artifacts.append(
                executor.fetch_dir(
                    remote_scope,
                    local_raw / artifact_scope,
                    timeout=fetch_timeout,
                )
            )
        fetched_manifests = executor.fetch_dir(
            f"{remote_root}/manifests",
            local_raw / "manifests",
            timeout=fetch_timeout,
        )
        fetched = all(fetched_artifacts) and fetched_manifests
        if not fetched and failure is None:
            failure = StepError(
                "failed to fetch Volc Operator Sim artifacts from "
                f"{remote_root}/{scope}"
            )

    if failure is not None:
        if isinstance(failure, StepError):
            raise failure
        raise StepError(f"Volc acquisition failed in {scope}: {failure}") from failure
    if platform_dir is not None:
        _write_local_stage_complete(platform_dir, scope)
    (local_raw / f"acquisition-failure-{scope}.json").unlink(missing_ok=True)


def _remote_thin_context_view_command(source: str, destination: str) -> str:
    """Create a hard-linked transfer view without Host-only bulk evidence."""

    program = r'''import os,shutil,sys
from pathlib import Path
source=Path(sys.argv[1])
destination=Path(sys.argv[2])
skip_files={"perf.data","perf-script.txt","perf-report-period.txt","perf-report-period-resolved-full.txt"}
skip_dirs={"_symbol-cache","outputs"}
if not source.is_dir():
    raise SystemExit(f"context source missing: {source}")
destination.mkdir(parents=True,exist_ok=False)
for path in source.rglob("*"):
    relative=path.relative_to(source)
    if any(part in skip_dirs for part in relative.parts):
        continue
    if path.is_file() and path.name in skip_files:
        continue
    target=destination/relative
    if path.is_dir():
        target.mkdir(parents=True,exist_ok=True)
        continue
    if not path.is_file():
        continue
    target.parent.mkdir(parents=True,exist_ok=True)
    try:
        os.link(path,target)
    except OSError:
        shutil.copy2(path,target)
print("PYFRAMEWORK_THIN_CONTEXT_VIEW="+str(destination))
'''
    return " ".join(
        (
            "python3",
            "-c",
            shlex.quote(program),
            shlex.quote(source),
            shlex.quote(destination),
        )
    )


def _prepare_remote_thin_context_view(
    executor: Any, *, source: str, destination: str
) -> str:
    result = executor.run(
        _remote_thin_context_view_command(source, destination), timeout=300
    )
    if result.returncode != 0:
        raise StepError(
            "failed to prepare thin pipeline_context transfer view: "
            f"{result.stderr or result.stdout}"
        )
    marker = "PYFRAMEWORK_THIN_CONTEXT_VIEW="
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith(marker):
            return line[len(marker) :]
    raise StepError("thin pipeline_context transfer view marker missing")


def _write_remote_stage_complete(
    executor: Any,
    remote_root: str,
    scope: str,
    *,
    attempts: int = 3,
) -> None:
    """Write the idempotent scope marker, retrying transport-only failures."""

    command = _remote_stage_complete_command(remote_root, scope)
    result = None
    for attempt in range(1, attempts + 1):
        result = executor.run(command, timeout=60)
        if result.returncode == 0:
            return
        if result.returncode not in {124, 255} or attempt == attempts:
            break
        logger.warning(
            "Transient SSH failure writing %s COMPLETE marker; retry %d/%d",
            scope,
            attempt + 1,
            attempts,
        )
        time.sleep(1)

    output = ""
    if result is not None:
        output = str(result.stderr or result.stdout or "").strip()
    raise StepError(
        f"failed to write remote {scope} COMPLETE marker"
        + (f": {output}" if output else "")
    )


def _run_read_only_with_retry(
    executor: Any,
    command: str,
    *,
    timeout: int,
    attempts: int = 3,
) -> Any:
    """Retry a side-effect-free SSH probe only on transport timeouts."""

    result = None
    for attempt in range(1, attempts + 1):
        result = executor.run(command, timeout=timeout)
        if result.returncode not in {124, 255} or attempt == attempts:
            return result
        logger.warning(
            "Transient SSH failure in read-only Volc probe; retry %d/%d",
            attempt + 1,
            attempts,
        )
        time.sleep(1)
    return result


def _prepare_stage_resume(
    platform_dir: Path, scope: str, force: bool
) -> bool:
    manifests = platform_dir / "operators" / "manifests"
    complete = manifests / f"{scope}-COMPLETE.json"
    if complete.is_file() and not force:
        local_raw = platform_dir / "operators" / "raw"
        if _has_retryable_transport_failures(local_raw, scope):
            logger.info(
                "Volc %s COMPLETE contains transport-failed cases; resuming gaps",
                scope,
            )
            return False
        (local_raw / f"acquisition-failure-{scope}.json").unlink(missing_ok=True)
        logger.info("Volc %s COMPLETE exists, skipping", scope)
        return True
    return False


def _has_retryable_transport_failures(local_raw: Path, scope: str) -> bool:
    """Recognize current and legacy case records caused by SSH transport loss."""

    path = local_raw / f"case-failures-{scope}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return False
    for item in cases:
        if not isinstance(item, dict):
            continue
        if item.get("retryable") is True and item.get("reason") == "ssh_transport":
            return True
        error = str(item.get("error") or "")
        if re.search(r"\(exit (?:124|255)\)", error):
            return True
    return False


def _write_case_failures(
    local_raw: Path, scope: str, failures: list[dict[str, Any]]
) -> None:
    """Replace the attempt's failure set, removing stale failures on success."""

    path = local_raw / f"case-failures-{scope}.json"
    if failures:
        _write_json(path, {"schemaVersion": 1, "cases": failures})
    else:
        path.unlink(missing_ok=True)


def _archive_stage_scope(
    executor: Any,
    remote_root: str,
    local_raw: Path,
    scope: str,
) -> None:
    """Move one prior stage attempt aside before a forced collection.

    The archive is deliberately stage-scoped: Host inputs and the independent
    snapshot cache are never touched, and evidence from other completed stages
    remains available for resume and normalization.
    """

    token = f"{time.time_ns()}-{os.getpid()}"
    program = r'''import json,os,sys
from pathlib import Path
root=Path(sys.argv[1])
scope=sys.argv[2]
token=sys.argv[3]
archive=Path(str(root)+".previous-"+token)
moved=[]
sources=[(root/scope,archive/scope)]
for name in (scope+".json",scope+"-COMPLETE.json"):
    sources.append((root/"manifests"/name,archive/"manifests"/name))
for source,destination in sources:
    if not source.exists():
        continue
    destination.parent.mkdir(parents=True,exist_ok=True)
    os.replace(source,destination)
    moved.append(str(source.relative_to(root)))
print("PYFRAMEWORK_SCOPE_ARCHIVE="+json.dumps({"archive":str(archive),"moved":moved,"scope":scope},sort_keys=True))
'''
    command = " ".join(
        (
            "python3",
            "-c",
            shlex.quote(program),
            shlex.quote(remote_root),
            shlex.quote(scope),
            shlex.quote(token),
        )
    )
    result = executor.run(command, timeout=60)
    if result.returncode != 0:
        raise StepError(
            f"failed to archive prior Volc {scope} attempt: "
            f"{result.stderr or result.stdout}"
        )

    quarantine = local_raw.parent / "quarantine" / token
    local_sources = [
        (local_raw / scope, quarantine / scope),
        (
            local_raw.parent / "manifests" / f"{scope}.json",
            quarantine / "manifests" / f"{scope}.json",
        ),
        (
            local_raw.parent / "manifests" / f"{scope}-COMPLETE.json",
            quarantine / "manifests" / f"{scope}-COMPLETE.json",
        ),
        (
            local_raw / f"case-failures-{scope}.json",
            quarantine / f"case-failures-{scope}.json",
        ),
        (
            local_raw / f"acquisition-failure-{scope}.json",
            quarantine / f"acquisition-failure-{scope}.json",
        ),
        (
            local_raw / f"collection-succeeded-{scope}.json",
            quarantine / f"collection-succeeded-{scope}.json",
        ),
    ]
    for source, destination in local_sources:
        if not source.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)


def _remote_stage_complete_command(remote_root: str, scope: str) -> str:
    manifest = f"{remote_root}/manifests/{scope}.json"
    complete = f"{remote_root}/manifests/{scope}-COMPLETE.json"
    program = r'''import hashlib,json,os,sys
from pathlib import Path
manifest_path,complete_path,scope=sys.argv[1:4]
manifest=Path(manifest_path)
manifest.parent.mkdir(parents=True,exist_ok=True)
(manifest.parent.parent/scope).mkdir(parents=True,exist_ok=True)
payload={"schemaVersion":1,"scope":scope,"status":"complete"}
tmp=Path(str(manifest)+".partial")
tmp.write_text(json.dumps(payload,sort_keys=True)+"\n",encoding="utf-8")
os.replace(tmp,manifest)
digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
complete_payload={"schemaVersion":1,"scope":scope,"status":"complete","manifestSha256":digest}
tmp=Path(complete_path+".partial")
tmp.write_text(json.dumps(complete_payload,sort_keys=True)+"\n",encoding="utf-8")
os.replace(tmp,complete_path)
'''
    return " ".join(
        (
            "python3", "-c", shlex.quote(program), shlex.quote(manifest),
            shlex.quote(complete), shlex.quote(scope),
        )
    )


def _remote_stage_is_complete(
    executor: Any, remote_root: str, scope: str
) -> bool:
    """Verify a Host-persistent scope marker before attempting recovery."""

    manifest = f"{remote_root}/manifests/{scope}.json"
    complete = f"{remote_root}/manifests/{scope}-COMPLETE.json"
    program = r'''import hashlib,json,sys
from pathlib import Path
manifest_path,complete_path,scope=sys.argv[1:4]
manifest=Path(manifest_path)
complete=Path(complete_path)
if not manifest.is_file() or not complete.is_file():
    raise SystemExit(0)
try:
    manifest_payload=json.loads(manifest.read_text(encoding="utf-8"))
    complete_payload=json.loads(complete.read_text(encoding="utf-8"))
except (OSError,ValueError):
    raise SystemExit(0)
digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
valid=(
    manifest_payload.get("scope")==scope
    and manifest_payload.get("status")=="complete"
    and complete_payload.get("scope")==scope
    and complete_payload.get("status")=="complete"
    and complete_payload.get("manifestSha256")==digest
)
if valid:
    print("PYFRAMEWORK_REMOTE_SCOPE_COMPLETE=1")
'''
    command = " ".join(
        (
            "python3",
            "-c",
            shlex.quote(program),
            shlex.quote(manifest),
            shlex.quote(complete),
            shlex.quote(scope),
        )
    )
    result = _run_read_only_with_retry(executor, command, timeout=60)
    return (
        result.returncode == 0
        and "PYFRAMEWORK_REMOTE_SCOPE_COMPLETE=1" in (result.stdout or "")
    )


def _should_recover_remote(
    executor: Any,
    remote_root: str,
    local_raw: Path,
    scope: str,
    *,
    force: bool,
) -> bool:
    """Recover a completed collection after marker or transfer interruption."""

    if force:
        return False
    if _has_retryable_transport_failures(local_raw, scope):
        logger.info(
            "Ignoring remote %s COMPLETE because transport-failed cases need resume",
            scope,
        )
        return False
    if _remote_stage_is_complete(executor, remote_root, scope):
        return True

    receipt_path = local_raw / f"collection-succeeded-{scope}.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {}
    has_receipt = (
        receipt.get("schemaVersion") == 1
        and receipt.get("scope") == scope
        and receipt.get("remoteRoot") == remote_root
        and receipt.get("status") == "collection-succeeded"
    )
    if not has_receipt:
        legacy_path = local_raw / f"acquisition-failure-{scope}.json"
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            legacy = {}
        has_receipt = (
            legacy.get("schemaVersion") == 1
            and legacy.get("scope") == scope
            and legacy.get("remoteRoot") == remote_root
            and legacy.get("errorType") == "StepError"
            and legacy.get("error")
            == f"failed to write remote {scope} COMPLETE marker"
        )
    if not has_receipt:
        return False

    result = _run_read_only_with_retry(
        executor,
        f"test -d {shlex.quote(f'{remote_root}/{scope}')}",
        timeout=60,
    )
    if result.returncode != 0:
        return False
    logger.info(
        "Recovering %s from local collection receipt and Host scope", scope
    )
    return True


def _write_local_stage_complete(platform_dir: Path, scope: str) -> None:
    manifests = platform_dir / "operators" / "manifests"
    manifest_path = manifests / f"{scope}.json"
    scope_root = platform_dir / "operators" / "raw" / scope
    artifacts = []
    if scope_root.is_dir():
        for path in sorted(item for item in scope_root.rglob("*") if item.is_file()):
            artifacts.append(
                {
                    "path": path.relative_to(platform_dir).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
    _write_json(
        manifest_path,
        {"schemaVersion": 1, "scope": scope, "status": "complete", "artifacts": artifacts},
    )
    _write_json(
        manifests / f"{scope}-COMPLETE.json",
        {
            "schemaVersion": 1,
            "scope": scope,
            "status": "complete",
            "manifestSha256": _sha256_file(manifest_path),
        },
    )


def _expand_cpu_set(value: str) -> set[int]:
    result: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"invalid descending CPU range: {item}")
            result.update(range(start, end + 1))
        else:
            result.add(int(item))
    return result


def _format_cpu_set(cpus: Iterable[int]) -> str:
    values = sorted(set(int(value) for value in cpus))
    ranges: list[str] = []
    index = 0
    while index < len(values):
        start = end = values[index]
        index += 1
        while index < len(values) and values[index] == end + 1:
            end = values[index]
            index += 1
        ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)


def _profile_cpu_envelope(workload_cpu_set: str, observer_cpu_set: str) -> str:
    """Return the temporary container cpuset used only during perf record."""

    workload = _expand_cpu_set(workload_cpu_set)
    observer = _expand_cpu_set(observer_cpu_set)
    if not workload or not observer:
        raise ValueError("profile workload and observer CPU sets must be non-empty")
    if len(observer) != 1:
        raise ValueError("profile observer CPU set must contain exactly one CPU")
    if workload.intersection(observer):
        raise ValueError("profile observer CPU must not overlap workload CPUs")
    return _format_cpu_set(workload.union(observer))


def _load_settings(
    workload: Mapping[str, Any],
    env_config: Mapping[str, Any],
    *,
    platform: str = "",
) -> VolcAcquisitionSettings:
    software = _mapping(env_config.get("software"), "software")
    raw_operator = workload.get("operatorAnalysis") or {}
    operator = _mapping(raw_operator, "workload.operatorAnalysis")
    group = str(workload.get("group", "core_dual_engine"))
    raw_operator_tasks = operator.get("tasks") or []
    if not isinstance(raw_operator_tasks, list):
        raise ValueError("workload.operatorAnalysis.tasks must be a list")
    operator_tasks = tuple(
        dict.fromkeys(str(value) for value in raw_operator_tasks if str(value))
    )
    raw_operator_engines = operator.get("engines") or []
    if not isinstance(raw_operator_engines, list):
        raise ValueError("workload.operatorAnalysis.engines must be a list")
    operator_engines = tuple(
        dict.fromkeys(
            str(value) for value in raw_operator_engines if str(value)
        )
    )
    raw_context_perf = operator.get("contextPerf") or {}
    context_perf = _mapping(
        raw_context_perf, "workload.operatorAnalysis.contextPerf"
    )
    raw_fast_calls = context_perf.get("fastOperatorCalls") or {}
    if not isinstance(raw_fast_calls, Mapping):
        raise ValueError(
            "workload.operatorAnalysis.contextPerf.fastOperatorCalls "
            "must be a mapping"
        )
    context_fast_operator_calls = {
        str(name): max(1, int(calls))
        for name, calls in raw_fast_calls.items()
    }
    raw_input_overrides = operator.get("inputOverrides") or {}
    if not isinstance(raw_input_overrides, Mapping):
        raise ValueError("workload.operatorAnalysis.inputOverrides must be a mapping")
    task_input_overrides: dict[str, Mapping[str, Any]] = {}
    for pipeline_id, raw_spec in raw_input_overrides.items():
        if not isinstance(raw_spec, Mapping):
            raise ValueError(
                "workload.operatorAnalysis.inputOverrides."
                f"{pipeline_id} must be a mapping"
            )
        spec = copy.deepcopy(dict(raw_spec))
        if not str(spec.get("path") or ""):
            raise ValueError(
                "workload.operatorAnalysis.inputOverrides."
                f"{pipeline_id}.path must not be empty"
            )
        rows = spec.get("rows")
        if rows is not None and (
            not isinstance(rows, int) or isinstance(rows, bool) or rows <= 0
        ):
            raise ValueError(
                "workload.operatorAnalysis.inputOverrides."
                f"{pipeline_id}.rows must be a positive integer"
            )
        task_input_overrides[str(pipeline_id)] = spec
    daft_env = str(software.get("daftCondaEnv", "xarch"))
    dj_env = str(software.get("dataJuicerCondaEnv", "xdj"))
    workload_cpu_set = str(
        (software.get("volcCpuSets") or {}).get(platform) or ""
    )
    observer_cpu_set = str(
        (software.get("volcObserverCpuSets") or {}).get(platform) or ""
    )
    if observer_cpu_set:
        _profile_cpu_envelope(workload_cpu_set, observer_cpu_set)
    return VolcAcquisitionSettings(
        container=str(
            software.get("volcOperatorSimContainer", "volc-operator-sim-bench")
        ),
        host_data_root=str(
            software.get("hostDataRoot", "/home/lxy/de_bench_full")
        ).rstrip("/"),
        revision=str(software.get("volcOperatorSimRevision") or ""),
        daft_python=f"/opt/conda/envs/{daft_env}/bin/python",
        datajuicer_python=f"/opt/conda/envs/{dj_env}/bin/python",
        group=group,
        profile=str(workload.get("profile", "smoke")),
        rounds=max(1, int(workload.get("rounds", 1))),
        timeout=max(60, int(workload.get("timeout", software.get("benchmarkTimeout", 14400)))),
        operator_enabled=bool(operator.get("enabled", True)),
        context_timing=bool(operator.get("contextTiming", True)),
        isolated_timing=bool(operator.get("isolatedTiming", True)),
        profiling=bool(operator.get("profiling", True)),
        operator_warmup=max(0, int(operator.get("warmup", 1))),
        operator_rounds=max(1, int(operator.get("rounds", 3))),
        perf_frequency=max(1, int(software.get("perfFrequency", 99))),
        perf_events=str(software.get("perfEvents") or _DEFAULT_PERF_EVENTS),
        min_perf_samples=max(0, int(operator.get("minPerfSamples", 5000))),
        context_perf_enabled=bool(context_perf.get("enabled", False)),
        context_perf_split=bool(
            context_perf.get("splitByOperatorBoundary", False)
        ),
        context_perf_frequency=max(
            1,
            int(
                context_perf.get(
                    "perfFrequency", software.get("perfFrequency", 99)
                )
            ),
        ),
        fast_operator_min_samples=max(
            0, int(context_perf.get("fastOperatorMinSamples", 5000))
        ),
        context_fast_operator_calls=context_fast_operator_calls,
        operator_group=str(operator.get("group") or group),
        operator_tasks=operator_tasks,
        operator_engines=operator_engines,
        task_input_overrides=task_input_overrides,
        workload_cpu_set=workload_cpu_set,
        observer_cpu_set=observer_cpu_set,
    )


def _apply_task_input_overrides(
    task_documents: Mapping[str, Mapping[str, Any]],
    input_overrides: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Apply configured frozen inputs without mutating target documents."""

    documents = copy.deepcopy(dict(task_documents))
    unknown = sorted(set(input_overrides).difference(documents))
    if unknown:
        raise ValueError(
            "input override task document not found: " + ", ".join(unknown)
        )
    for pipeline_id, input_spec in input_overrides.items():
        document = copy.deepcopy(dict(documents[pipeline_id]))
        document["input"] = copy.deepcopy(dict(input_spec))
        documents[pipeline_id] = document
    return documents


def _normalize_real_task_contracts(
    task_documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Repair pinned upstream task contracts that still describe retired stubs."""

    documents = copy.deepcopy(dict(task_documents))
    pipeline_id = "pipeline_pdf_full_min"
    raw_document = documents.get(pipeline_id)
    if not isinstance(raw_document, Mapping):
        return documents
    document = copy.deepcopy(dict(raw_document))
    raw_pipeline = document.get("pipeline")
    if not isinstance(raw_pipeline, list):
        return documents
    pipeline = copy.deepcopy(raw_pipeline)
    legacy_vector = False
    for step in pipeline:
        if not isinstance(step, dict) or step.get("dj_ops") != "bge_vectorize_mapper":
            continue
        params = copy.deepcopy(step.get("params") or {})
        legacy_vector = int(params.get("dim") or 0) == 16 and not params.get(
            "emit_vector"
        )
        if legacy_vector:
            params.update(
                {
                    "dim": 384,
                    "emit_vector": True,
                    "model_name": "all-MiniLM-L6-v2",
                }
            )
            step["params"] = params
        break
    if not legacy_vector:
        return documents
    for step in pipeline:
        if not isinstance(step, dict) or step.get("dj_ops") != "write_lance":
            continue
        params = copy.deepcopy(step.get("params") or {})
        params.update({"field": "embedding", "vector_dim": 384})
        step["params"] = params
    document["pipeline"] = pipeline
    engine_overrides = copy.deepcopy(document.get("engine_overrides") or {})
    engine_overrides["into_partitions"] = 4
    document["engine_overrides"] = engine_overrides
    document["description"] = (
        "PDF full/min real CPU path: pdfplumber parse/table, Tesseract OCR, "
        "MiniLM embedding, and Lance sink."
    )
    expected = copy.deepcopy(document.get("expected") or {})
    expected["notes"] = "Real PDF parse/OCR/table and 384-dimensional MiniLM embedding."
    document["expected"] = expected
    documents[pipeline_id] = document
    return documents


def _stable_run_id(
    run_dir: Path,
    platform: str,
    settings: VolcAcquisitionSettings,
    *,
    environment_fingerprint_sha256: str,
) -> str:
    payload = "|".join(
        (
            str(run_dir.resolve()),
            platform,
            settings.revision,
            settings.group,
            settings.operator_group or settings.group,
            ",".join(settings.operator_tasks),
            ",".join(settings.operator_engines),
            json.dumps(
                settings.task_input_overrides or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            settings.profile,
            settings.workload_cpu_set,
            settings.observer_cpu_set,
            str(settings.context_perf_enabled),
            str(settings.context_perf_split),
            str(settings.context_perf_frequency),
            environment_fingerprint_sha256,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _environment_identity_sha256(payload: bytes) -> str:
    """Hash reproducibility inputs, excluding collection-instance metadata."""

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return hashlib.sha256(payload).hexdigest()
    if isinstance(document, Mapping) and isinstance(
        document.get("environmentFingerprint"), Mapping
    ):
        document = document["environmentFingerprint"]

    volatile = {
        "capturedAtEpochNs",
        "completedAtEpochNs",
        "containerId",
        "finishedAt",
        "generatedAtEpochNs",
        "startedAt",
    }

    def stable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): stable(item)
                for key, item in value.items()
                if str(key) not in volatile
            }
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value

    canonical = json.dumps(
        stable(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _read_target_inputs(
    executor: Any, container: str, group: str, timeout: int
) -> dict[str, Any]:
    program = r'''import base64,json,subprocess,sys
from pathlib import Path
root=Path("/opt/volc_operator_sim")
cfg=json.loads((root/"configs/pipelines/formal_pipelines.json").read_text(encoding="utf-8"))
group=sys.argv[1]
group_cfg=(cfg.get("groups") or {}).get(group)
if not isinstance(group_cfg,dict):
    raise SystemExit(f"unknown formal group: {group}")
docs={}
for task in group_cfg.get("tasks") or []:
    docs[str(task)]=json.loads((root/"tasks"/f"{task}.json").read_text(encoding="utf-8"))
payload={
    "revision":subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip(),
    "formalConfig":cfg,
    "taskDocuments":docs,
}
raw=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
print("PYFRAMEWORK_TARGET_INPUTS="+base64.b64encode(raw).decode("ascii"))
'''
    command = (
        f"docker exec {shlex.quote(container)} /opt/conda/bin/python -c "
        f"{shlex.quote(program)} {shlex.quote(group)}"
    )
    result = _run_read_only_with_retry(executor, command, timeout=timeout)
    if result.returncode != 0:
        raise StepError(
            "failed to read pinned Volc target inputs: "
            f"exit={result.returncode} stderr={result.stderr[-1000:]}"
        )
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith(_TARGET_INPUT_MARKER):
            try:
                return json.loads(
                    base64.b64decode(line[len(_TARGET_INPUT_MARKER) :]).decode(
                        "utf-8"
                    )
                )
            except (ValueError, json.JSONDecodeError) as exc:
                raise StepError(f"invalid Volc target input payload: {exc}") from exc
    raise StepError("Volc target input payload marker was not emitted")


def _pipeline_e2e_command(
    settings: VolcAcquisitionSettings, remote_root: str
) -> str:
    env = {
        "VOLC_DE_BENCH_ROOT": settings.host_data_root,
        "GROUP": settings.group,
        "PROFILE_NAME": settings.profile,
        "ROUNDS": settings.rounds,
        "OUT_ROOT": f"{remote_root}/pipeline_e2e",
        "DAFT_PY": settings.daft_python,
        "DJ_PY": settings.datajuicer_python,
        "PERF_EVENTS": settings.perf_events,
    }
    return _docker_shell(
        settings.container,
        env,
        f"cd {_TARGET_ROOT} && bash scripts/pipelines/run_all_pipelines.sh",
    )


def _context_command(
    settings: VolcAcquisitionSettings,
    remote_root: str,
    pipeline_id: str,
    engine: str,
) -> str:
    profile = json.dumps(
        {
            "materialize_policy": "per_op",
            "timing_tier": "p1",
            "fuse_mappers": False,
            "ray_num_cpus": 4,
            "dj_np": 4,
            "perf_lock_profile": "attribution",
        },
        separators=(",", ":"),
    )
    env = {
        "VOLC_DE_BENCH_ROOT": settings.host_data_root,
        "TASK": pipeline_id,
        "ENGINE": engine,
        "PY": _python_for(settings, engine),
        "PROFILE": profile,
        "CASE": f"pipeline_context__{_slug(pipeline_id)}__{_slug(engine)}",
        "OUT_ROOT": f"{remote_root}/pipeline_context",
        "MODE": "timing",
        "PERF_LOCK_PROFILE": "attribution",
        "PERF_EVENTS": settings.perf_events,
        "PYFRAMEWORK_SCOPE": "pipeline_context",
    }
    return _docker_shell(
        settings.container,
        env,
        f"cd {_TARGET_ROOT} && bash scripts/capture/bench_capture.sh",
    )


def _read_completed_capture_cases(
    executor: Any,
    remote_scope: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return successful capture cases already persisted in one Host scope."""

    program = r'''import base64,json,sys
from pathlib import Path
root=Path(sys.argv[1])
identity_policy=sys.argv[2]
records={}
def record(case,engine):
    return records.setdefault((case,engine),{"case":case,"engine":engine,"ok":False,"perfData":False,"cpuSvg":False,"annotate":False,"symbolized":False,"sampleCount":0,"_artifactMtime":-1.0})
if root.is_dir():
    for path in root.rglob("summary.json"):
        try:
            payload=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            continue
        case=str(payload.get("case") or "")
        engine=str(payload.get("engine") or "")
        if not case or not engine:
            continue
        current=record(case,engine)
        current["ok"] = current["ok"] or payload.get("status") == "ok"
        directory=path.parent
        artifact_mtime=max((item.stat().st_mtime for item in directory.iterdir() if item.is_file()),default=path.stat().st_mtime)
        if artifact_mtime >= current["_artifactMtime"]:
            current["_artifactMtime"]=artifact_mtime
            current["perfData"]=(directory/"perf.data").is_file()
            current["cpuSvg"]=(directory/"cpu.svg").is_file()
            current["annotate"]=(directory/"perf-annotate.txt").is_file()
            resolution=directory/"perf-symbol-resolution.json"
            resolved_report=directory/"perf-report-period-resolved.txt"
            resolution_payload={}
            if resolution.is_file() and resolved_report.is_file():
                try:
                    resolution_payload=json.loads(resolution.read_text(encoding="utf-8"))
                except (OSError,ValueError):
                    resolution_payload={}
            current["symbolized"]=(
                resolution_payload.get("status")=="complete"
                and resolution_payload.get("identityPolicy")==identity_policy
            )
            current["sampleCount"]=0
            if resolved_report.is_file():
                for line in resolved_report.read_text(encoding="utf-8",errors="replace").splitlines():
                    parts=line.split("|")
                    if len(parts) < 3:
                        continue
                    try:
                        current["sampleCount"] += int(parts[2].strip())
                    except ValueError:
                        continue
            if current["perfData"] and current["symbolized"]:
                current["ok"] = True
    for path in root.rglob("flamegraph-metadata.json"):
        try:
            payload=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            continue
        case=str(payload.get("outputCase") or "")
        engine=str(payload.get("engineId") or "")
        if not case or not engine or not (path.parent/"cpu.svg").is_file():
            continue
        current=record(case,engine)
        current["ok"] = True
        current["cpuSvg"] = True
    for path in root.rglob("*.json"):
        if path.name == "summary.json":
            continue
        try:
            payload=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError):
            continue
        engine=str(payload.get("engine_id") or "")
        if not engine or payload.get("status") != "ok" or "metrics" not in payload:
            continue
        try:
            parts=path.relative_to(root).parts
        except ValueError:
            continue
        runner_positions=[index for index,value in enumerate(parts) if value == "runner"]
        if not runner_positions:
            continue
        runner_index=runner_positions[-1]
        if runner_index + 1 >= len(parts):
            continue
        case=parts[runner_index + 1]
        record(case,engine)["ok"] = True
raw=json.dumps([{key:value for key,value in item.items() if not key.startswith("_")} for item in records.values() if item["ok"]],sort_keys=True,separators=(",",":")).encode("utf-8")
print("PYFRAMEWORK_CAPTURE_CASE_INVENTORY="+base64.b64encode(raw).decode("ascii"))
'''
    command = " ".join(
        (
            "python3",
            "-c",
            shlex.quote(program),
            shlex.quote(remote_scope),
            shlex.quote(IDENTITY_POLICY),
        )
    )
    result = _run_read_only_with_retry(executor, command, timeout=120)
    if result.returncode != 0:
        return {}
    for line in reversed((result.stdout or "").splitlines()):
        if not line.startswith(_CAPTURE_CASE_INVENTORY_MARKER):
            continue
        try:
            payload = json.loads(
                base64.b64decode(
                    line[len(_CAPTURE_CASE_INVENTORY_MARKER) :]
                ).decode("utf-8")
            )
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, list):
            return {}
        inventory: dict[tuple[str, str], dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            case = str(item.get("case") or "")
            engine = str(item.get("engine") or "")
            if case and engine and item.get("ok") is True:
                inventory[(case, engine)] = item
        return inventory
    return {}


def _full_task_profile(scope: str) -> str:
    if scope == "pipeline_e2e":
        profile = {
            "materialize_policy": "end",
            "timing_tier": "p0",
            "fuse_mappers": False,
            "include_write_lance_in_elapsed": True,
            "ray_num_cpus": 4,
            "dj_np": 4,
            "perf_lock_profile": "attribution",
        }
    elif scope == "pipeline_context":
        profile = {
            "materialize_policy": "per_op",
            "timing_tier": "p1",
            "fuse_mappers": False,
            "ray_num_cpus": 4,
            "dj_np": 4,
            "perf_lock_profile": "attribution",
        }
    else:
        raise ValueError(f"unsupported full task scope: {scope}")
    return json.dumps(profile, separators=(",", ":"))


def _write_and_push_full_task_overlays(
    *,
    executor: Any,
    plan: Mapping[str, Any],
    task_documents: Mapping[str, Mapping[str, Any]],
    platform_dir: Path,
    remote_root: str,
    settings: VolcAcquisitionSettings,
    scope: str,
) -> dict[str, str]:
    """Persist full-task derivatives used by scaled P0 and P1 runs."""

    local_dir = platform_dir / "operators" / "overlays"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = (
        f"{settings.host_data_root}/operator-cache/pyframework/"
        f"{plan['runId']}/overlays"
    )
    mkdir = executor.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
    if mkdir.returncode != 0:
        raise StepError(f"failed to create Host overlay directory: {remote_dir}")
    profile = json.loads(_full_task_profile(scope))
    paths: dict[str, str] = {}
    uploads: list[tuple[Path, str]] = []
    for task in plan["tasks"]:
        pipeline_id = str(task["pipelineId"])
        document = task_documents[pipeline_id]
        overlay = render_full_task(
            document,
            measurement_scope=scope,
            output_uri=(
                f"{remote_root}/{scope}/outputs/{_slug(pipeline_id)}.lance"
            ),
            engine_overrides=profile,
        )
        paths[pipeline_id] = _push_overlay(
            local_dir,
            remote_dir,
            f"{_slug(pipeline_id)}__full_{scope}.json",
            overlay,
            uploads,
        )
    if settings.context_perf_enabled:
        uploads.extend(
            _context_perf_support_uploads(
                plan=plan,
                platform_dir=platform_dir,
                remote_dir=remote_dir,
            ).values()
        )
    _push_generated_files_batch(executor, uploads)
    return paths


def _context_perf_support_uploads(
    *,
    plan: Mapping[str, Any],
    platform_dir: Path,
    remote_dir: str,
) -> dict[str, tuple[Path, str]]:
    del plan  # the canonical plan has already been persisted by preparation
    adapter_dir = Path(__file__).parent
    return {
        "plan": (
            platform_dir / "operators" / "operator-plan.json",
            f"{remote_dir}/operator-plan.json",
        ),
        "splitter": (
            adapter_dir / "context_perf_split.py",
            f"{remote_dir}/context_perf_split.py",
        ),
        "symbolizer": (
            adapter_dir / "perf_symbol_bundle.py",
            f"{remote_dir}/perf_symbol_bundle.py",
        ),
        "symbol_env": (
            adapter_dir / "perf_symbol_env.sh",
            f"{remote_dir}/perf_symbol_env.sh",
        ),
        "microprofile": (
            adapter_dir / "frozen_microprofile.py",
            f"{remote_dir}/frozen_microprofile.py",
        ),
    }


def _push_context_perf_support(
    *,
    executor: Any,
    plan: Mapping[str, Any],
    platform_dir: Path,
    settings: VolcAcquisitionSettings,
) -> dict[str, str]:
    remote_dir = (
        f"{settings.host_data_root}/operator-cache/pyframework/"
        f"{plan['runId']}/overlays"
    )
    mkdir = executor.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
    if mkdir.returncode != 0:
        raise StepError(f"failed to create Host overlay directory: {remote_dir}")
    entries = _context_perf_support_uploads(
        plan=plan,
        platform_dir=platform_dir,
        remote_dir=remote_dir,
    )
    _push_generated_files_batch(executor, list(entries.values()))
    return {name: remote for name, (_, remote) in entries.items()}


def _context_clock_sync_path(
    remote_root: str, pipeline_id: str, engine: str
) -> str:
    case = f"pipeline_context__{_slug(pipeline_id)}__{_slug(engine)}"
    return f"{remote_root}/pipeline_context/clock-sync-{_slug(case)}.json"


def _context_perf_split_command(
    *,
    settings: VolcAcquisitionSettings,
    remote_root: str,
    plan_path: str,
    splitter_path: str,
    symbolizer_path: str,
    pipeline_id: str,
    engine: str,
) -> str:
    python = _python_for(settings, engine)
    context_root = f"{remote_root}/pipeline_context"
    output_root = f"{remote_root}/operator_case_perf"
    command = " ".join(
        (
            shlex.quote(python),
            shlex.quote(splitter_path),
            "--context-root",
            shlex.quote(context_root),
            "--output-root",
            shlex.quote(output_root),
            "--plan",
            shlex.quote(plan_path),
            "--clock-sync",
            shlex.quote(_context_clock_sync_path(remote_root, pipeline_id, engine)),
            "--pipeline-id",
            shlex.quote(pipeline_id),
            "--engine",
            shlex.quote(engine),
            "--python",
            shlex.quote(python),
            "--symbolizer",
            shlex.quote(symbolizer_path),
            "--real-perf",
            "/usr/bin/perf",
            "--buildid-dir",
            shlex.quote(f"{context_root}/_symbol-cache/buildid"),
        )
    )
    return _docker_shell(
        settings.container,
        {"PYFRAMEWORK_SCOPE": "operator_case_perf"},
        command,
    )


def _host_input_path(settings: VolcAcquisitionSettings, value: str) -> str:
    path = str(value or "")
    if not path:
        raise ValueError("frozen context perf input path is empty")
    return path if path.startswith("/") else f"{settings.host_data_root}/{path}"


def _context_perf_windows_complete(
    task: Mapping[str, Any],
    engine: str,
    inventory: Mapping[tuple[str, str], Mapping[str, Any]],
) -> bool:
    expected: list[tuple[str, str]] = []
    for operator in task.get("operators") or []:
        if not isinstance(operator, Mapping):
            continue
        case_id = str(operator.get("operatorCaseId") or "")
        if not case_id:
            continue
        case_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
        expected.append(
            (
                f"operator_case_perf__{case_hash}__context_window_001",
                engine,
            )
        )
    return bool(expected) and all(
        (inventory.get(key) or {}).get("symbolized") is True
        for key in expected
    )


def _run_context_fast_operator_profiles(
    *,
    executor: Any,
    settings: VolcAcquisitionSettings,
    remote_root: str,
    plan: Mapping[str, Any],
    task: Mapping[str, Any],
    support: Mapping[str, str],
) -> None:
    calls_by_operator = settings.context_fast_operator_calls or {}
    if not calls_by_operator:
        return
    pipeline_id = str(task["pipelineId"])
    operators_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for item in task.get("operators") or []:
        if not isinstance(item, Mapping):
            continue
        operators_by_id.setdefault(
            str(item.get("operatorId") or ""), []
        ).append(item)
    selected_calls = {
        operator_id: total_calls
        for operator_id, total_calls in calls_by_operator.items()
        if operator_id in operators_by_id
    }
    if not selected_calls:
        return
    completed_fast_cases = _read_completed_capture_cases(
        executor,
        f"{remote_root}/operator_case_perf",
    )
    input_spec = (settings.task_input_overrides or {}).get(pipeline_id) or {}
    source_rows = max(1, int(input_spec.get("rows") or 1))
    manifest = _host_input_path(
        settings,
        str(input_spec.get("jsonl_mirror") or input_spec.get("manifest_path") or ""),
    )
    source_field = str(input_spec.get("field") or "file_path")
    requires_derived_text = any(
        operator_id in {"text_chunk_mapper", "bge_vectorize_mapper"}
        for operator_id in selected_calls
    )
    prepare_operator = (
        "pdf_table_extract_mapper"
        if source_field != "text" and requires_derived_text
        else "identity"
    )
    work_root = (
        f"{settings.host_data_root}/operator-cache/pyframework/"
        f"{plan['runId']}/frozen-profile"
    )
    table_texts = f"{work_root}/{_slug(pipeline_id)}-table-texts.json"
    table_summary = f"{work_root}/{_slug(pipeline_id)}-table-summary.json"
    prepare = " ".join(
        (
            "mkdir -p",
            shlex.quote(work_root),
            "&&",
            shlex.quote(settings.daft_python),
            shlex.quote(support["microprofile"]),
            "--operator",
            shlex.quote(prepare_operator),
            "--manifest",
            shlex.quote(manifest),
            "--field",
            shlex.quote(source_field),
            "--limit",
            str(source_rows),
            "--total-calls",
            str(source_rows),
            "--output-json",
            shlex.quote(table_texts),
            "--summary-json",
            shlex.quote(table_summary),
        )
    )
    _run_checked(
        executor,
        _docker_shell(
            settings.container,
            {
                "PYFRAMEWORK_SCOPE": "operator_case_perf",
                "PYTHONPATH": _TARGET_ROOT,
            },
            f"cd {_TARGET_ROOT} && {prepare}",
        ),
        scope="operator_case_perf",
        timeout=settings.timeout,
    )
    for operator_id, total_calls in selected_calls.items():
        for operator in operators_by_id[operator_id]:
            case_hash = hashlib.sha256(
                str(operator["operatorCaseId"]).encode("utf-8")
            ).hexdigest()[:12]
            engines = operator.get("engines") or task.get("engines") or []
            for engine_value in engines:
                engine = str(engine_value)
                # Data-Juicer operator boundaries come from log timing and are
                # intentionally diagnostic-only. More perf samples cannot make
                # those records formally attributable, so a fast retry would only
                # extend the run without changing its evidence grade.
                if engine == "datajuicer_native":
                    continue
                case = f"operator_case_perf__{case_hash}__context_fast_001"
                window_case = (
                    f"operator_case_perf__{case_hash}__context_window_001"
                )
                reusable = None
                for completed_case in (window_case, case):
                    completed = completed_fast_cases.get(
                        (completed_case, engine)
                    ) or {}
                    if (
                        completed.get("symbolized") is True
                        and int(completed.get("sampleCount") or 0)
                        >= settings.fast_operator_min_samples
                    ):
                        reusable = completed
                        break
                if reusable is not None:
                    logger.info(
                        "Reusing completed perf profile %s/%s with %s samples",
                        reusable["case"],
                        engine,
                        reusable["sampleCount"],
                    )
                    continue
                command = _context_fast_profile_command(
                    settings=settings,
                    remote_root=remote_root,
                    support=support,
                    engine=engine,
                    case=case,
                    operator_id=operator_id,
                    input_json=table_texts,
                    source_rows=source_rows,
                    total_calls=int(total_calls),
                    params=operator.get("params") or {},
                    summary_json=f"{work_root}/{_slug(operator_id)}-summary.json",
                )

                def run_fast_case() -> bool:
                    _run_checked(
                        executor,
                        command,
                        scope="operator_case_perf",
                        timeout=settings.timeout,
                    )
                    return True

                _run_with_profile_cpu_envelope(
                    executor=executor,
                    settings=settings,
                    action=run_fast_case,
                )
                _run_checked(
                    executor,
                    _perf_annotate_command(
                        settings,
                        f"{remote_root}/operator_case_perf",
                        case,
                        engine,
                        symbolizer_path=support["symbolizer"],
                    ),
                    scope="operator_case_perf",
                    timeout=settings.timeout,
                )


def _context_fast_profile_command(
    *,
    settings: VolcAcquisitionSettings,
    remote_root: str,
    support: Mapping[str, str],
    engine: str,
    case: str,
    operator_id: str,
    input_json: str,
    source_rows: int,
    total_calls: int,
    params: Mapping[str, Any],
    summary_json: str,
) -> str:
    python = _python_for(settings, engine)
    output_root = f"{remote_root}/operator_case_perf"
    raw = " ".join(
        (
            shlex.quote(python),
            shlex.quote(support["microprofile"]),
            "--operator",
            shlex.quote(operator_id),
            "--input-json",
            shlex.quote(input_json),
            "--limit",
            str(source_rows),
            "--total-calls",
            str(total_calls),
            "--params-json",
            shlex.quote(json.dumps(dict(params), sort_keys=True)),
            "--summary-json",
            shlex.quote(summary_json),
        )
    )
    env = {
        "ENGINE": engine,
        "PY": python,
        "CASE": case,
        "OUT_ROOT": output_root,
        "MODE": "perfrecord",
        "PERF_FREQ": settings.context_perf_frequency,
        "PERF_EVENTS": settings.perf_events,
        "PERF_LOCK_PROFILE": "attribution",
        "PYFRAMEWORK_SCOPE": "operator_case_perf",
        "PYTHONPATH": _TARGET_ROOT,
        "BASH_ENV": support["symbol_env"],
        "PYFRAMEWORK_REAL_PERF": "/usr/bin/perf",
        "PYFRAMEWORK_PERF_SYMBOL_PYTHON": python,
        "PYFRAMEWORK_PERF_SYMBOL_HELPER": support["symbolizer"],
        "PYFRAMEWORK_PERF_SYMBOL_CACHE": f"{output_root}/_symbol-cache",
        "PYFRAMEWORK_PERF_RECORD_EXTRA": "--buildid-all,--buildid-mmap",
        "PYFRAMEWORK_PERF_MAP_POLL_INTERVAL": "0.005",
        "VOLC_MEDIA_DERIVED_ROOT": (
            f"{str(Path(summary_json).parent)}/derived/"
            f"{_slug(case)}__{_slug(engine)}"
        ),
        "VOLC_MEDIA_DISABLE_CACHE": "1",
    }
    return _docker_shell(
        settings.container,
        env,
        (
            'trap \'rm -rf -- "$VOLC_MEDIA_DERIVED_ROOT"\' EXIT; '
            f"cd {_TARGET_ROOT} && bash scripts/capture/bench_capture.sh -- {raw}"
        ),
    )


def _write_and_push_overlays(
    *,
    executor: Any,
    plan: Mapping[str, Any],
    task_documents: Mapping[str, Mapping[str, Any]],
    platform_dir: Path,
    remote_root: str,
    settings: VolcAcquisitionSettings,
) -> dict[tuple[str, str, int], str]:
    local_dir = platform_dir / "operators" / "overlays"
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_dir = (
        f"{settings.host_data_root}/operator-cache/pyframework/"
        f"{plan['runId']}/overlays"
    )
    mkdir = executor.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
    if mkdir.returncode != 0:
        raise StepError(f"failed to create Host overlay directory: {remote_dir}")

    paths: dict[tuple[str, str, int], str] = {}
    uploads: list[tuple[Path, str]] = []
    for task in plan["tasks"]:
        pipeline_id = str(task["pipelineId"])
        document = task_documents[pipeline_id]
        previous_snapshot: Mapping[str, Any] | None = None
        for snapshot in sorted(
            task["snapshots"], key=lambda item: int(item["afterOrder"])
        ):
            order = int(snapshot["afterOrder"])
            snapshot_dir = (
                f"{settings.host_data_root}/operator-cache/{snapshot['snapshotId']}"
            )
            start_order = 0
            input_spec: Mapping[str, Any] | None = None
            if previous_snapshot is not None:
                start_order = int(previous_snapshot["afterOrder"]) + 1
                input_spec = _snapshot_input_spec(
                    settings.host_data_root,
                    str(previous_snapshot["snapshotId"]),
                    document,
                )
            overlay = render_snapshot_task(
                document,
                through_order=order,
                start_order=start_order,
                input_spec=input_spec,
                output_uri=f"{snapshot_dir}/snapshot.lance",
            )
            paths[(pipeline_id, "snapshot", order)] = _push_overlay(
                local_dir,
                remote_dir,
                f"{_slug(pipeline_id)}__snapshot_{order:03d}.json",
                overlay,
                uploads,
            )
            previous_snapshot = snapshot
        for operator in task["operators"]:
            if operator.get("isolationStatus") != "supported":
                continue
            order = int(operator["order"])
            snapshot_after_order = operator.get("inputSnapshotAfterOrder")
            if snapshot_after_order is None:
                planned_input = _mapping(
                    operator.get("input"),
                    f"{pipeline_id}.operators[{order}].input",
                )
                input_spec = _mapping(
                    planned_input.get("spec"),
                    f"{pipeline_id}.operators[{order}].input.spec",
                )
            else:
                snapshot = next(
                    value
                    for value in task["snapshots"]
                    if int(value["afterOrder"]) == int(snapshot_after_order)
                )
                input_spec = _snapshot_input_spec(
                    settings.host_data_root,
                    str(snapshot["snapshotId"]),
                    document,
                )
            overlay = render_isolated_task(
                document, order=order, input_spec=input_spec
            )
            paths[(pipeline_id, "operator", order)] = _push_overlay(
                local_dir,
                remote_dir,
                f"{_slug(pipeline_id)}__operator_{order:03d}.json",
                overlay,
                uploads,
            )
    plan_remote = f"{remote_dir}/operator-plan.json"
    plan_local = platform_dir / "operators" / "operator-plan.json"
    uploads.append((plan_local, plan_remote))
    renderer_local = Path(__file__).with_name("native_flamegraph.py")
    renderer_remote = f"{remote_dir}/native_flamegraph.py"
    uploads.append((renderer_local, renderer_remote))
    symbol_helper_local = Path(__file__).with_name("perf_symbol_bundle.py")
    symbol_helper_remote = f"{remote_dir}/perf_symbol_bundle.py"
    uploads.append((symbol_helper_local, symbol_helper_remote))
    symbol_env_local = Path(__file__).with_name("perf_symbol_env.sh")
    symbol_env_remote = f"{remote_dir}/perf_symbol_env.sh"
    uploads.append((symbol_env_local, symbol_env_remote))
    _push_generated_files_batch(executor, uploads)
    return paths


def _push_overlay(
    local_dir: Path,
    remote_dir: str,
    name: str,
    document: Mapping[str, Any],
    uploads: list[tuple[Path, str]],
) -> str:
    local = local_dir / name
    _write_json(local, document)
    remote = f"{remote_dir}/{name}"
    uploads.append((local, remote))
    return remote


def _push_generated_files_batch(
    executor: Any,
    uploads: list[tuple[Path, str]],
) -> None:
    """Upload all small generated files in one compressed, atomic SSH batch."""

    entries = [
        {
            "path": remote,
            "data": base64.b64encode(local.read_bytes()).decode("ascii"),
        }
        for local, remote in uploads
    ]
    raw = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    payload = base64.b64encode(zlib.compress(raw, level=9)).decode("ascii")
    program = r'''import base64,json,os,sys,zlib
from pathlib import Path
entries=json.loads(zlib.decompress(base64.b64decode(sys.argv[1])))
for item in entries:
    path=Path(item["path"])
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name("."+path.name+".partial")
    tmp.write_bytes(base64.b64decode(item["data"]))
    os.replace(tmp,path)
print("PYFRAMEWORK_ATOMIC_OVERLAY_WRITE=ok count="+str(len(entries)))
'''
    command = " ".join(
        (
            "python3",
            "-c",
            shlex.quote(program),
            shlex.quote(payload),
        )
    )
    result = None
    for attempt in range(1, 4):
        result = executor.run(command, timeout=120)
        if result.returncode == 0:
            return
        if result.returncode not in {124, 255} or attempt == 3:
            break
        logger.warning(
            "Transient SSH failure in atomic overlay upload; retry %d/3",
            attempt + 1,
        )
        time.sleep(1)
    if all(executor.push_file(local, remote) for local, remote in uploads):
        return
    output = "" if result is None else str(result.stderr or result.stdout or "")
    raise StepError(
        "failed to upload Volc generated-file batch: " + output.strip()
    )


def _build_snapshots(
    *,
    executor: Any,
    plan: Mapping[str, Any],
    overlay_paths: Mapping[tuple[str, str, int], str],
    task_documents: Mapping[str, Mapping[str, Any]],
    settings: VolcAcquisitionSettings,
    remote_root: str,
    failures: list[dict[str, Any]] | None = None,
) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    profile = json.dumps(
        {
            "materialize_policy": "end",
            "timing_tier": "p0",
            "fuse_mappers": False,
            "include_write_lance_in_elapsed": True,
            "perf_lock_profile": "attribution",
        },
        separators=(",", ":"),
    )
    for task in plan["tasks"]:
        pipeline_id = str(task["pipelineId"])
        document = task_documents[pipeline_id]
        for snapshot in task["snapshots"]:
            order = int(snapshot["afterOrder"])
            snapshot_id = str(snapshot["snapshotId"])
            cached = _read_snapshot_manifest(
                executor, settings, snapshot_id
            )
            if cached is not None and validate_snapshot_manifest(
                cached, snapshot
            ).cache_hit:
                logger.info("Reusing complete snapshot cache %s", snapshot_id)
                result[(pipeline_id, order)] = _snapshot_input_spec(
                    settings.host_data_root, snapshot_id, document
                )
                continue
            overlay = overlay_paths[(pipeline_id, "snapshot", order)]
            case = f"snapshot_build__{_slug(pipeline_id)}__{order:03d}"
            command = _capture_overlay_command(
                settings=settings,
                remote_out=f"{remote_root}/snapshot_build",
                task_path=overlay,
                engine="daft_ray",
                profile=profile,
                case=case,
                mode="timing",
                scope="snapshot_build",
            )
            try:
                _run_checked(
                    executor, command, scope="snapshot_build", timeout=settings.timeout
                )
                mirror_command = _snapshot_mirror_command(
                    settings, snapshot, document
                )
                _run_idempotent_checked(
                    executor,
                    mirror_command,
                    scope="snapshot_build",
                    timeout=settings.timeout,
                )
            except StepError as exc:
                if failures is None:
                    raise
                failure = {
                    "pipelineId": pipeline_id,
                    "snapshotId": snapshot_id,
                    "afterOrder": order,
                    "scope": "snapshot_build",
                    "case": case,
                    "status": "failed",
                    "error": str(exc),
                }
                _mark_transport_retryable(failure, exc)
                failures.append(failure)
                continue
            result[(pipeline_id, order)] = _snapshot_input_spec(
                settings.host_data_root, snapshot_id, document
            )
    return result


def _read_snapshot_manifest(
    executor: Any,
    settings: VolcAcquisitionSettings,
    snapshot_id: str,
) -> dict[str, Any] | None:
    root = f"{settings.host_data_root}/operator-cache/{snapshot_id}"
    program = r'''import base64,json,sys
from pathlib import Path
root=Path(sys.argv[1])
manifest=root/"manifest.json"
complete=root/"COMPLETE.json"
if manifest.is_file() and complete.is_file():
    payload=json.loads(manifest.read_text(encoding="utf-8"))
    raw=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode("utf-8")
    print("PYFRAMEWORK_SNAPSHOT_MANIFEST="+base64.b64encode(raw).decode("ascii"))
'''
    command = (
        f"docker exec {shlex.quote(settings.container)} "
        f"{shlex.quote(settings.daft_python)} -c {shlex.quote(program)} "
        f"{shlex.quote(root)}"
    )
    result = _run_read_only_with_retry(executor, command, timeout=60)
    if result.returncode != 0:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith(_SNAPSHOT_MANIFEST_MARKER):
            try:
                return json.loads(
                    base64.b64decode(
                        line[len(_SNAPSHOT_MANIFEST_MARKER) :]
                    ).decode("utf-8")
                )
            except (ValueError, json.JSONDecodeError):
                return None
    return None


def _run_isolated_operators(
    *,
    executor: Any,
    plan: Mapping[str, Any],
    overlay_paths: Mapping[tuple[str, str, int], str],
    task_documents: Mapping[str, Mapping[str, Any]],
    snapshot_inputs: Mapping[tuple[str, int], Mapping[str, Any]],
    settings: VolcAcquisitionSettings,
    remote_root: str,
    timing: bool = True,
    profiling: bool = True,
) -> list[dict[str, Any]]:
    del task_documents  # overlays already contain immutable inputs
    failures: list[dict[str, Any]] = []
    profile = json.dumps(
        {
            "materialize_policy": "per_op",
            "timing_tier": "p1",
            "fuse_mappers": False,
            "ray_num_cpus": 4,
            "dj_np": 4,
            "perf_lock_profile": "attribution",
        },
        separators=(",", ":"),
    )
    completed_timing_cases = (
        _read_completed_capture_cases(
            executor, f"{remote_root}/operator_case_e2e"
        )
        if timing
        else {}
    )
    completed_profile_cases = (
        _read_completed_capture_cases(
            executor, f"{remote_root}/operator_case_perf"
        )
        if profiling and settings.profiling
        else {}
    )
    native_flamegraph_engines = {
        engine
        for (case, engine), record in completed_profile_cases.items()
        if "__native_perf_fallback" in case and record.get("cpuSvg")
    }
    successful_pyspy_engines = {
        engine
        for (case, engine), record in completed_profile_cases.items()
        if "__flamegraph_attempt_" in case
        and "__native_perf_fallback" not in case
        and record.get("ok")
        and record.get("cpuSvg")
    }
    native_flamegraph_engines.difference_update(successful_pyspy_engines)
    for task in plan["tasks"]:
        pipeline_id = str(task["pipelineId"])
        for operator in task["operators"]:
            if operator.get("isolationStatus") != "supported":
                continue
            order = int(operator["order"])
            overlay = overlay_paths[(pipeline_id, "operator", order)]
            case_hash = hashlib.sha256(
                str(operator["operatorCaseId"]).encode("utf-8")
            ).hexdigest()[:12]
            snapshot_after_order = operator.get("inputSnapshotAfterOrder")
            if (
                snapshot_after_order is not None
                and (pipeline_id, int(snapshot_after_order)) not in snapshot_inputs
            ):
                for engine_value in operator["engines"]:
                    failures.append(
                        {
                            "pipelineId": pipeline_id,
                            "operatorCaseId": str(operator["operatorCaseId"]),
                            "operatorId": str(operator["operatorId"]),
                            "order": order,
                            "engineId": str(engine_value),
                            "scope": "operator_case_e2e"
                            if timing
                            else "operator_case_perf",
                            "status": "blocked",
                            "reason": "required_snapshot_failed",
                        }
                    )
                continue
            for engine in operator["engines"]:
                engine = str(engine)
                if timing:
                    failed = False
                    for warmup in range(settings.operator_warmup):
                        case = f"operator_case_e2e__{case_hash}__warmup_{warmup + 1:03d}"
                        if completed_timing_cases.get((case, engine), {}).get("ok"):
                            logger.info(
                                "Recovering completed operator timing case %s/%s",
                                case,
                                engine,
                            )
                            continue
                        command = _capture_overlay_command(
                            settings=settings,
                            remote_out=f"{remote_root}/operator_case_e2e/warmup",
                            task_path=overlay,
                            engine=engine,
                            profile=profile,
                            case=case,
                            mode="timing",
                            scope="operator_case_e2e",
                        )
                        if not _run_case_checked(
                            executor=executor,
                            command=command,
                            scope="operator_case_e2e",
                            timeout=settings.timeout,
                            failures=failures,
                            pipeline_id=pipeline_id,
                            operator=operator,
                            engine=engine,
                            case=case,
                        ):
                            failed = True
                            break
                    if failed:
                        continue
                    for round_number in range(settings.operator_rounds):
                        case = f"operator_case_e2e__{case_hash}__round_{round_number + 1:03d}"
                        if completed_timing_cases.get((case, engine), {}).get("ok"):
                            logger.info(
                                "Recovering completed operator timing case %s/%s",
                                case,
                                engine,
                            )
                            continue
                        command = _capture_overlay_command(
                            settings=settings,
                            remote_out=f"{remote_root}/operator_case_e2e/measured",
                            task_path=overlay,
                            engine=engine,
                            profile=profile,
                            case=case,
                            mode="timing",
                            scope="operator_case_e2e",
                        )
                        if not _run_case_checked(
                            executor=executor,
                            command=command,
                            scope="operator_case_e2e",
                            timeout=settings.timeout,
                            failures=failures,
                            pipeline_id=pipeline_id,
                            operator=operator,
                            engine=engine,
                            case=case,
                        ):
                            failed = True
                            break
                    if failed:
                        continue
                if profiling and settings.profiling:
                    used_native_flamegraph = _run_operator_profile_case(
                        executor=executor,
                        settings=settings,
                        remote_root=remote_root,
                        overlay=overlay,
                        profile=profile,
                        pipeline_id=pipeline_id,
                        operator=operator,
                        engine=engine,
                        case_hash=case_hash,
                        failures=failures,
                        completed_cases=completed_profile_cases,
                        native_flamegraph_only=(
                            engine in native_flamegraph_engines
                        ),
                    )
                    if used_native_flamegraph:
                        native_flamegraph_engines.add(engine)
                    else:
                        native_flamegraph_engines.discard(engine)
    return failures


def _run_case_checked(
    *,
    executor: Any,
    command: str,
    scope: str,
    timeout: int,
    failures: list[dict[str, Any]],
    pipeline_id: str,
    operator: Mapping[str, Any],
    engine: str,
    case: str,
) -> bool:
    try:
        _run_checked(executor, command, scope=scope, timeout=timeout)
        return True
    except StepError as exc:
        failure = {
            "pipelineId": pipeline_id,
            "operatorCaseId": str(operator.get("operatorCaseId") or ""),
            "operatorId": str(operator.get("operatorId") or ""),
            "order": int(operator.get("order", -1)),
            "engineId": engine,
            "scope": scope,
            "case": case,
            "status": "failed",
            "error": str(exc),
        }
        _mark_transport_retryable(failure, exc)
        failures.append(failure)
        return False


def _mark_transport_retryable(
    failure: dict[str, Any], error: Exception
) -> None:
    if re.search(r"\(exit (?:124|255)\)", str(error)):
        failure["retryable"] = True
        failure["reason"] = "ssh_transport"


def _run_operator_profile_case(
    *,
    executor: Any,
    settings: VolcAcquisitionSettings,
    remote_root: str,
    overlay: str,
    profile: str,
    pipeline_id: str,
    operator: Mapping[str, Any],
    engine: str,
    case_hash: str,
    failures: list[dict[str, Any]],
    completed_cases: Mapping[tuple[str, str], Mapping[str, Any]],
    native_flamegraph_only: bool = False,
) -> bool:
    perf_root = f"{remote_root}/operator_case_perf"
    selected_case = ""
    final_attempt_case = (
        f"operator_case_perf__{case_hash}__perf_attempt_002"
    )
    final_attempt = completed_cases.get((final_attempt_case, engine), {})
    if (
        final_attempt.get("ok")
        and final_attempt.get("perfData")
        and final_attempt.get("symbolized")
    ):
        selected_case = final_attempt_case
        logger.info(
            "Recovering completed final operator perf case %s/%s",
            final_attempt_case,
            engine,
        )
    attempts = () if selected_case else (
        (1, settings.perf_frequency),
        (2, settings.perf_frequency * 10),
    )
    for attempt, frequency in attempts:
        perf_case = f"operator_case_perf__{case_hash}__perf_attempt_{attempt:03d}"
        existing_perf = completed_cases.get((perf_case, engine), {})
        if (
            existing_perf.get("ok")
            and existing_perf.get("perfData")
            and existing_perf.get("symbolized")
        ):
            logger.info(
                "Recovering completed operator perf case %s/%s",
                perf_case,
                engine,
            )
        else:
            if not _run_with_profile_cpu_envelope(
                executor=executor,
                settings=settings,
                action=lambda: _run_case_checked(
                    executor=executor,
                    command=_capture_overlay_command(
                        settings=settings,
                        remote_out=perf_root,
                        task_path=overlay,
                        engine=engine,
                        profile=profile,
                        case=perf_case,
                        mode="perfrecord",
                        scope="operator_case_perf",
                        perf_frequency=frequency,
                    ),
                    scope="operator_case_perf",
                    timeout=settings.timeout,
                    failures=failures,
                    pipeline_id=pipeline_id,
                    operator=operator,
                    engine=engine,
                    case=perf_case,
                ),
            ):
                return native_flamegraph_only
        selected_case = perf_case
        sample_count = _read_perf_sample_count(
            executor, settings, perf_root, perf_case, engine
        )
        if (
            sample_count is not None
            and sample_count >= settings.min_perf_samples
        ):
            break
        if attempt == 2:
            break

    flame_case = f"operator_case_perf__{case_hash}__flamegraph_attempt_001"
    pyspy_error: StepError | None = None
    existing_flame = completed_cases.get((flame_case, engine), {})
    fallback_prefix = f"{flame_case}__native_perf_fallback"
    fallback_complete = any(
        case_engine == engine
        and case_name.startswith(fallback_prefix)
        and record.get("cpuSvg")
        for (case_name, case_engine), record in completed_cases.items()
    )
    used_native_flamegraph = native_flamegraph_only
    if existing_flame.get("ok") and existing_flame.get("cpuSvg"):
        used_native_flamegraph = False
        logger.info(
            "Recovering completed operator flamegraph case %s/%s",
            flame_case,
            engine,
        )
    elif fallback_complete:
        used_native_flamegraph = True
        logger.info(
            "Recovering completed native operator flamegraph %s/%s",
            flame_case,
            engine,
        )
    else:
        if used_native_flamegraph:
            logger.warning(
                "Skipping previously failed Volc py-spy for %s/%s; "
                "using native perf fallback",
                engine,
                case_hash,
            )
        else:
            try:
                _run_checked(
                    executor,
                    _capture_overlay_command(
                        settings=settings,
                        remote_out=perf_root,
                        task_path=overlay,
                        engine=engine,
                        profile=profile,
                        case=flame_case,
                        mode="profile",
                        scope="operator_case_perf",
                    ),
                    scope="operator_case_perf",
                    timeout=settings.timeout,
                )
            except StepError as exc:
                pyspy_error = exc
                used_native_flamegraph = True
                logger.warning(
                    "Volc py-spy flamegraph failed for %s/%s; "
                    "using native perf fallback",
                    engine,
                    case_hash,
                )
        if used_native_flamegraph:
            fallback_case = f"{flame_case}__native_perf_fallback"
            fallback_ok = _run_case_checked(
                executor=executor,
                command=_native_perf_flamegraph_command(
                    settings=settings,
                    perf_root=perf_root,
                    perf_case=selected_case,
                    engine=engine,
                    output_case=fallback_case,
                    renderer_path=str(Path(overlay).with_name("native_flamegraph.py")),
                ),
                scope="operator_case_perf",
                timeout=settings.timeout,
                failures=failures,
                pipeline_id=pipeline_id,
                operator=operator,
                engine=engine,
                case=fallback_case,
            )
            if not fallback_ok:
                failures[-1]["primaryFlamegraphError"] = str(pyspy_error)
    selected_state = completed_cases.get((selected_case, engine), {})
    if selected_state.get("annotate") and selected_state.get("symbolized"):
        logger.info(
            "Recovering completed operator perf annotate %s/%s",
            selected_case,
            engine,
        )
    else:
        _run_case_checked(
            executor=executor,
            command=_perf_annotate_command(
                settings,
                perf_root,
                selected_case,
                engine,
                symbolizer_path=str(Path(overlay).with_name("perf_symbol_bundle.py")),
            ),
            scope="operator_case_perf",
            timeout=settings.timeout,
            failures=failures,
            pipeline_id=pipeline_id,
            operator=operator,
            engine=engine,
            case=f"{selected_case}__annotate",
        )
    return used_native_flamegraph


def _run_with_profile_cpu_envelope(
    *,
    executor: Any,
    settings: VolcAcquisitionSettings,
    action: Callable[[], bool],
) -> bool:
    """Reserve one out-of-band CPU only while ``perf record`` is active."""

    if not settings.observer_cpu_set:
        return action()
    expanded = _profile_cpu_envelope(
        settings.workload_cpu_set, settings.observer_cpu_set
    )
    _run_checked(
        executor,
        " ".join(
            (
                "docker update --cpuset-cpus",
                shlex.quote(expanded),
                shlex.quote(settings.container),
            )
        ),
        scope="operator_case_perf",
        timeout=60,
    )
    try:
        return action()
    finally:
        _run_checked(
            executor,
            " ".join(
                (
                    "docker update --cpuset-cpus",
                    shlex.quote(settings.workload_cpu_set),
                    shlex.quote(settings.container),
                )
            ),
            scope="operator_case_perf",
            timeout=60,
        )


def _native_perf_flamegraph_command(
    *,
    settings: VolcAcquisitionSettings,
    perf_root: str,
    perf_case: str,
    engine: str,
    output_case: str,
    renderer_path: str,
) -> str:
    """Render a portable native SVG from the already collected perf callchains."""

    python = _python_for(settings, engine)
    command = " ".join(
        (
            shlex.quote(python),
            shlex.quote(renderer_path),
            "--perf-root",
            shlex.quote(perf_root),
            "--engine",
            shlex.quote(engine),
            "--source-case",
            shlex.quote(perf_case),
            "--output-case",
            shlex.quote(output_case),
        )
    )
    return _docker_shell(
        settings.container,
        {
            "PYFRAMEWORK_SCOPE": "operator_case_perf",
            "PYFRAMEWORK_NATIVE_FLAMEGRAPH": "fallback",
        },
        command,
    )


def _read_perf_sample_count(
    executor: Any,
    settings: VolcAcquisitionSettings,
    perf_root: str,
    perf_case: str,
    engine: str,
) -> int | None:
    program = r'''import re,subprocess,sys
from pathlib import Path
root,engine,case=sys.argv[1:4]
matches=[path for path in Path(root).rglob("perf.data") if f"/{engine}/{case}/" in str(path)]
if matches:
    data=sorted(matches)[-1]
    result=subprocess.run(["perf","report","--stdio","-g","none","--no-children","--field-separator=|","--fields","sample","-i",str(data)],text=True,capture_output=True)
    counts=[]
    for line in result.stdout.splitlines():
        match=re.fullmatch(r"\s*([0-9,]+)\s*",line)
        if match:
            counts.append(int(match.group(1).replace(",","")))
    if counts:
        print("PYFRAMEWORK_PERF_SAMPLE_COUNT="+str(sum(counts)))
        raise SystemExit(0)
    report=data.with_name("perf-report.txt")
    text=report.read_text(encoding="utf-8",errors="replace") if report.is_file() else ""
    match=re.search(r"#\s*Samples:\s*([0-9]+(?:\.[0-9]+)?)\s*([KMG]?)",text,re.I)
    if match:
        scale={"":1,"K":1000,"M":1000000,"G":1000000000}[match.group(2).upper()]
        print("PYFRAMEWORK_PERF_SAMPLE_COUNT="+str(int(float(match.group(1))*scale)))
'''
    command = (
        f"docker exec {shlex.quote(settings.container)} "
        f"{shlex.quote(_python_for(settings, engine))} -c {shlex.quote(program)} "
        f"{shlex.quote(perf_root)} {shlex.quote(engine)} {shlex.quote(perf_case)}"
    )
    result = _run_read_only_with_retry(executor, command, timeout=120)
    if result.returncode != 0:
        return None
    marker = "PYFRAMEWORK_PERF_SAMPLE_COUNT="
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith(marker):
            try:
                return int(line[len(marker) :])
            except ValueError:
                return None
    return None


def _capture_overlay_command(
    *,
    settings: VolcAcquisitionSettings,
    remote_out: str,
    task_path: str,
    engine: str,
    profile: str,
    case: str,
    mode: str,
    scope: str,
    perf_frequency: int | None = None,
) -> str:
    python = _python_for(settings, engine)
    tool_path = ":".join(
        (
            str(Path(python).parent),
            "/opt/conda/bin",
            "/usr/local/bin",
            "/usr/sbin",
            "/usr/bin",
            "/sbin",
            "/bin",
        )
    )
    runner_dir = f"{remote_out}/runner/{_slug(case)}"
    produced_data_dir = (
        f"{remote_out}/_intermediate/{_slug(case)}__{_slug(engine)}"
    )
    raw = " ".join(
        (
            shlex.quote(python),
            shlex.quote(f"{_TARGET_ROOT}/runner/run_perf_suite.py"),
            "--task",
            shlex.quote(task_path),
            "--engine",
            shlex.quote(engine),
            "--out_dir",
            shlex.quote(runner_dir),
            "--cluster_profile",
            shlex.quote(profile),
            "--perf_lock_profile",
            "attribution",
        )
    )
    env = {
        "VOLC_DE_BENCH_ROOT": settings.host_data_root,
        # bench_capture prepends the selected Conda environment's lib directory.
        # Put the matching bin directory first as well: otherwise system tools
        # such as /usr/bin/tesseract can load Conda libcurl/libstdc++ and fail
        # with unresolved symbols during perf collection.
        "PATH": tool_path,
        "ENGINE": engine,
        "PY": python,
        "SAMPLER_PY": python,
        "PROFILE": profile,
        "CASE": case,
        "OUT_ROOT": remote_out,
        "MODE": mode,
        "PERF_FREQ": perf_frequency or settings.perf_frequency,
        "PERF_EVENTS": settings.perf_events,
        "PERF_LOCK_PROFILE": "attribution",
        "PYFRAMEWORK_SCOPE": scope,
        "DJ_PRODUCED_DATA_DIR": produced_data_dir,
        # Media stand-ins used to write deterministic outputs beside the
        # frozen source and reuse them in later E2E/context passes.  Keep all
        # derived media inside this case-scoped directory instead: the shell
        # recreates it before capture and removes it on exit.
        "VOLC_MEDIA_DERIVED_ROOT": produced_data_dir,
    }
    if mode == "perfrecord":
        overlay_dir = str(Path(task_path).parent)
        env.update(
            {
                "BASH_ENV": f"{overlay_dir}/perf_symbol_env.sh",
                "PYFRAMEWORK_REAL_PERF": "/usr/bin/perf",
                "PYFRAMEWORK_PERF_SYMBOL_PYTHON": python,
                "PYFRAMEWORK_PERF_SYMBOL_HELPER": (
                    f"{overlay_dir}/perf_symbol_bundle.py"
                ),
                "PYFRAMEWORK_PERF_SYMBOL_CACHE": (
                    f"{remote_out}/_symbol-cache"
                ),
                "PYFRAMEWORK_PERF_RECORD_EXTRA": (
                    "--buildid-all,--buildid-mmap"
                ),
                "PYFRAMEWORK_PERF_MAP_POLL_INTERVAL": "0.005",
                # perf prepends $PERF_EXEC_PATH and /usr/bin to the sampled
                # process PATH.  Pin it to the matching Conda bin so Daft/Ray
                # workers keep the compatible tesseract/ffmpeg executables.
                "PERF_EXEC_PATH": str(Path(python).parent),
            }
        )
    validate_program = r'''import json,sys
from pathlib import Path
directory=Path(sys.argv[1])
engine=sys.argv[2]
statuses=[]
for path in sorted(directory.glob("*.json")):
    try:
        payload=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        continue
    if payload.get("engine_id") != engine or "metrics" not in payload:
        continue
    statuses.append(str(payload.get("status") or "missing"))
if "ok" in statuses:
    print("PYFRAMEWORK_RUNNER_RESULT_STATUS=ok")
    raise SystemExit(0)
status=statuses[-1] if statuses else "missing"
print("PYFRAMEWORK_RUNNER_RESULT_STATUS="+status,file=sys.stderr)
raise SystemExit(17)
'''
    capture = "bash scripts/capture/bench_capture.sh"
    if mode == "profile":
        capture = (
            "timeout --signal=TERM --kill-after=10s "
            f"{_PYSPY_PROFILE_TIMEOUT_SECONDS}s {capture}"
        )
    clock_prefix = ""
    if mode == "perfrecord" and scope == "pipeline_context":
        clock_path = f"{remote_out}/clock-sync-{_slug(case)}.json"
        clock_program = r'''import json,os,sys,time
from pathlib import Path
path=Path(sys.argv[1])
path.parent.mkdir(parents=True,exist_ok=True)
before=time.monotonic()
epoch=time.time()
after=time.monotonic()
payload={"schemaVersion":1,"epochSeconds":epoch,"monotonicSeconds":(before+after)/2,"maxPairSkewSeconds":after-before}
temporary=Path(str(path)+".partial")
temporary.write_text(json.dumps(payload,sort_keys=True)+"\n",encoding="utf-8")
os.replace(temporary,path)
'''
        clock_prefix = (
            f"{shlex.quote(python)} -c {shlex.quote(clock_program)} "
            f"{shlex.quote(clock_path)} && "
        )
    quoted_produced_data_dir = shlex.quote(produced_data_dir)
    cleanup_produced_data = shlex.quote(
        f"rm -rf -- {quoted_produced_data_dir}"
    )
    shell = (
        # ``bash -l`` resets PATH after docker applies ``-e PATH=...``.  Export
        # it inside the login shell so subprocess tools match PY/PYENV_LIB.
        f"export PATH={shlex.quote(tool_path)} && "
        f"cd {_TARGET_ROOT} && "
        f"rm -rf -- {quoted_produced_data_dir} && "
        f"mkdir -p {quoted_produced_data_dir} && "
        f"trap {cleanup_produced_data} EXIT; "
        f"{clock_prefix}{capture} -- {raw}; "
        "capture_rc=$?; "
        'if [ "$capture_rc" -ne 0 ]; then exit "$capture_rc"; fi; '
        f"{shlex.quote(python)} -c {shlex.quote(validate_program)} "
        f"{shlex.quote(runner_dir)} {shlex.quote(engine)}"
    )
    return _docker_shell(
        settings.container,
        env,
        shell,
    )


def _snapshot_mirror_command(
    settings: VolcAcquisitionSettings,
    snapshot: Mapping[str, Any],
    task_document: Mapping[str, Any],
) -> str:
    snapshot_id = str(snapshot["snapshotId"])
    snapshot_dir = f"{settings.host_data_root}/operator-cache/{snapshot_id}"
    input_spec = _mapping(task_document.get("input"), "task.input")
    field = str(input_spec.get("field", "text"))
    modality = str(input_spec.get("modality", ""))
    identity = {
        key: snapshot[key]
        for key in (
            "schemaVersion", "snapshotId", "sourceRevision", "producer",
            "parentFingerprint", "operatorSpecHash", "builderVersion",
        )
    }
    if snapshot.get("partitionPolicy") is not None:
        identity["partitionPolicy"] = snapshot["partitionPolicy"]
    program = r'''import hashlib,json,math,os,re,sys
from pathlib import Path
import lance
import pyarrow as pa
lance_path,jsonl_path,manifest_path,sidecar_path,complete_path,field,modality,identity_json,source_manifest_ref=sys.argv[1:10]
identity=json.loads(identity_json)
table=lance.dataset(lance_path).to_table()
source_column=field
projected=False
table_changed=False
if field not in table.column_names:
    candidates=[]
    for name in table.column_names:
        match=re.search(r"_(\d+)_out$",name)
        if match:
            candidates.append((int(match.group(1)),name))
    if not candidates:
        raise ValueError(
            f"snapshot has no logical field {field!r} and no operator output column: "
            f"{table.column_names}"
        )
    source_column=max(candidates)[1]
    table=table.append_column(field,table[source_column])
    projected=True
    table_changed=True
media_keys={"image":"images","audio":"audios","video":"videos"}
media_key=media_keys.get(modality)
compatibility_fields=[]
if media_key:
    logical_values=table[field].to_pylist()
    if media_key not in table.column_names:
        table=table.append_column(media_key,pa.array([[str(value)] if value is not None else [] for value in logical_values]))
        table_changed=True
    compatibility_fields.append(media_key)
    if "source_file" not in table.column_names:
        table=table.append_column("source_file",pa.array([[str(value)] if value is not None else [] for value in logical_values]))
        table_changed=True
    compatibility_fields.append("source_file")
    if "text" not in table.column_names:
        table=table.append_column("text",pa.array([""]*len(table)))
        table_changed=True
    compatibility_fields.append("text")
source_fragments=0
if source_manifest_ref:
    source_manifest_path=Path(source_manifest_ref)
    if not source_manifest_path.is_absolute():
        source_manifest_path=Path(os.environ.get("VOLC_DE_BENCH_ROOT",""))/source_manifest_path
    if source_manifest_path.is_file():
        try:
            source_manifest=json.loads(source_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # file_manifest inputs are JSONL row streams, not dataset-level
            # JSON authorities. They declare no Lance partition contract.
            source_manifest={}
        source_partition=source_manifest.get("partition_spec") or {}
        source_fragments=int(source_partition.get("fragments") or 0)
current_fragments=len(list(lance.dataset(lance_path).get_fragments()))
needs_repartition=source_fragments>0 and current_fragments!=source_fragments
if table_changed or needs_repartition:
    if source_fragments>0:
        max_rows_per_file=max(1,math.ceil(len(table)/source_fragments))
        lance.write_dataset(table,lance_path,mode="overwrite",max_rows_per_file=max_rows_per_file,max_rows_per_group=min(8192,max_rows_per_file))
    else:
        lance.write_dataset(table,lance_path,mode="overwrite")
    table=lance.dataset(lance_path).to_table()
output_fragments=len(list(lance.dataset(lance_path).get_fragments()))
if source_fragments>0 and output_fragments!=source_fragments:
    raise ValueError(f"snapshot partition inheritance failed: source={source_fragments} output={output_fragments}")
partition_spec={"status":"passed" if source_fragments>0 else "not_applicable","policy":"inherit_if_declared","sourceFragments":source_fragments or None,"fragments":output_fragments,"maxRowsPerFile":max(1,math.ceil(len(table)/source_fragments)) if source_fragments>0 else None}
rows=table.to_pylist()
def default(value):
    if isinstance(value,(bytes,bytearray,memoryview)):
        return bytes(value).hex()
    if hasattr(value,"tolist"):
        return value.tolist()
    return str(value)
tmp=Path(jsonl_path+".partial")
serialized=[]
with tmp.open("w",encoding="utf-8") as handle:
    for row in rows:
        line=json.dumps(row,ensure_ascii=False,sort_keys=True,default=default,separators=(",",":"))
        serialized.append(line)
        handle.write(line+"\n")
os.replace(tmp,jsonl_path)
ordered=hashlib.sha256(("\n".join(serialized)+"\n").encode("utf-8")).hexdigest()
content=hashlib.sha256("".join(sorted(hashlib.sha256(line.encode("utf-8")).hexdigest() for line in serialized)).encode("ascii")).hexdigest()
schema=hashlib.sha256(str(table.schema).encode("utf-8")).hexdigest()
lance_files=[path for path in Path(lance_path).rglob("*") if path.is_file()]
manifest={**identity,"status":"complete","kind":"lance","path":lance_path,"jsonl_path":jsonl_path,"field":field,"logicalField":{"status":"passed","field":field,"sourceColumn":source_column,"projected":projected},"mediaCompatibility":{"status":"passed" if media_key else "not_applicable","modality":modality or None,"requiredFields":compatibility_fields},"representations":{"lance":{"rows":len(rows),"files":len(lance_files),"bytes":sum(path.stat().st_size for path in lance_files),"schemaFingerprint":"sha256:"+schema},"jsonl":{"rows":len(rows),"files":1,"bytes":Path(jsonl_path).stat().st_size,"schemaFingerprint":"sha256:"+schema}},"parity":{"status":"passed","orderedFingerprint":"sha256:"+ordered,"contentFingerprint":"sha256:"+content,"partitionSpec":partition_spec}}
manifest_tmp=Path(manifest_path+".partial")
manifest_tmp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
os.replace(manifest_tmp,manifest_path)
sidecar={"schemaVersion":1,"dataset_fingerprint":"sha256:"+content,"checksum_rollup":content,"ordered_fingerprint":"sha256:"+ordered,"row_count":len(rows),"schema_fingerprint":"sha256:"+schema,"source_lance_path":lance_path,"jsonl_path":jsonl_path}
sidecar_tmp=Path(sidecar_path+".partial")
sidecar_tmp.write_text(json.dumps(sidecar,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
os.replace(sidecar_tmp,sidecar_path)
manifest_sha=hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
complete={"schemaVersion":1,"status":"complete","snapshotId":identity["snapshotId"],"manifestSha256":manifest_sha}
complete_tmp=Path(complete_path+".partial")
complete_tmp.write_text(json.dumps(complete,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
os.replace(complete_tmp,complete_path)
'''
    args = (
        f"{snapshot_dir}/snapshot.lance",
        f"{snapshot_dir}/snapshot.jsonl",
        f"{snapshot_dir}/manifest.json",
        f"{snapshot_dir}/snapshot.mirror.meta.json",
        f"{snapshot_dir}/COMPLETE.json",
        field,
        modality,
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
        str(input_spec.get("manifest_path") or ""),
    )
    shell = " ".join(
        [
            shlex.quote(settings.daft_python),
            "-c",
            shlex.quote(program),
            *(shlex.quote(value) for value in args),
        ]
    )
    return _docker_shell(
        settings.container,
        {
            "VOLC_DE_BENCH_ROOT": settings.host_data_root,
            "PYFRAMEWORK_SCOPE": "snapshot_build",
        },
        shell,
    )


def _snapshot_input_spec(
    host_data_root: str,
    snapshot_id: str,
    task_document: Mapping[str, Any],
) -> dict[str, Any]:
    root = f"{host_data_root}/operator-cache/{snapshot_id}"
    original = _mapping(task_document.get("input"), "task.input")
    spec: dict[str, Any] = {
        "kind": "lance",
        "path": f"{root}/snapshot.lance",
        "jsonl_mirror": f"{root}/snapshot.jsonl",
        "manifest_path": f"{root}/manifest.json",
        "mirror_meta": f"{root}/snapshot.mirror.meta.json",
        "input_fingerprint": f"sha256:{snapshot_id}",
        "field": str(original.get("field", "text")),
    }
    for key in ("modality", "fixture_id"):
        if original.get(key) is not None:
            spec[key] = original[key]
    return spec


def _perf_annotate_command(
    settings: VolcAcquisitionSettings,
    perf_root: str,
    perf_case: str,
    engine: str,
    *,
    symbolizer_path: str,
) -> str:
    path_filter = f"*/{engine}/{perf_case}/perf.data"
    cache = f"{perf_root}/_symbol-cache"
    perf = f"/usr/bin/perf --buildid-dir {shlex.quote(cache + '/buildid')}"
    python = shlex.quote(_python_for(settings, engine))
    helper = shlex.quote(symbolizer_path)
    script = (
        f"data=$(find {shlex.quote(perf_root)} -type f "
        f"-path {shlex.quote(path_filter)} -printf '%T@ %p\\n' "
        "| sort -nr | head -n 1 | cut -d' ' -f2-); "
        "test -n \"$data\" || { echo 'perf.data not found' >&2; exit 19; }; "
        "dir=$(dirname \"$data\"); "
        f"if {perf} buildid-list -i \"$data\" > \"$dir/.perf-buildid-list.txt.partial\" 2>&1; "
        "then mv \"$dir/.perf-buildid-list.txt.partial\" \"$dir/perf-buildid-list.txt\"; "
        "else rm -f \"$dir/.perf-buildid-list.txt.partial\"; fi; "
        f"if {perf} report --stdio --no-children --show-total-period "
        "--field-separator='|' --fields overhead,period,sample,comm,pid,dso,symbol,addr "
        "-i \"$data\" > \"$dir/.perf-report-period.txt.partial\" 2>&1; "
        "then mv \"$dir/.perf-report-period.txt.partial\" \"$dir/perf-report-period.txt\"; "
        "else rm -f \"$dir/.perf-report-period.txt.partial\"; fi; "
        f"if ! {python} {helper} resolve "
        "--source \"$dir/perf-report-period.txt\" "
        "--manifest \"$dir/perf-dso-manifest.json\" "
        "--output \"$dir/perf-report-period-resolved.txt\" "
        "--perf-data \"$data\" "
        "--real-perf /usr/bin/perf "
        f"--buildid-dir {shlex.quote(cache + '/buildid')} "
        "--require-complete; then exit 18; fi; "
        f"if {perf} annotate --stdio --no-source --percent-limit=0.5 -i \"$data\" "
        "> \"$dir/.perf-annotate.txt.partial\" 2>&1; "
        "then mv \"$dir/.perf-annotate.txt.partial\" \"$dir/perf-annotate.txt\"; "
        "else rm -f \"$dir/.perf-annotate.txt.partial\"; fi; "
        "objdump --version > \"$dir/objdump-version.txt\"; "
        "readelf --version > \"$dir/readelf-version.txt\""
    )
    return _docker_shell(
        settings.container,
        {"PYFRAMEWORK_SCOPE": "operator_case_perf"},
        script,
    )


def _run_checked(
    executor: Any,
    command: str,
    *,
    scope: str,
    timeout: int,
) -> None:
    logger.info("[5a] Volc scope=%s", scope)
    result = executor.run(command, timeout=timeout, stream=True)
    if result.returncode != 0:
        raise StepError(
            f"Volc {scope} failed (exit {result.returncode}): "
            f"stdout={result.stdout[-2000:]} stderr={result.stderr[-1000:]}"
        )


def _run_idempotent_checked(
    executor: Any,
    command: str,
    *,
    scope: str,
    timeout: int,
    attempts: int = 3,
) -> None:
    """Run one atomic/idempotent Host mutation with transport-only retries."""

    logger.info("[5a] Volc idempotent scope=%s", scope)
    result = None
    for attempt in range(1, attempts + 1):
        result = executor.run(command, timeout=timeout, stream=True)
        if result.returncode == 0:
            return
        if result.returncode not in {124, 255} or attempt == attempts:
            break
        logger.warning(
            "Transient SSH failure in idempotent %s command; retry %d/%d",
            scope,
            attempt + 1,
            attempts,
        )
        time.sleep(1)
    assert result is not None
    raise StepError(
        f"Volc {scope} failed (exit {result.returncode}): "
        f"stdout={result.stdout[-2000:]} stderr={result.stderr[-1000:]}"
    )


def _docker_shell(
    container: str, env: Mapping[str, Any], script: str
) -> str:
    env_args = " ".join(
        f"-e {shlex.quote(str(key) + '=' + str(value))}"
        for key, value in env.items()
    )
    return (
        f"docker exec {env_args} {shlex.quote(container)} bash -lc "
        f"{shlex.quote(script)}"
    )


def _python_for(settings: VolcAcquisitionSettings, engine: str) -> str:
    if engine == "datajuicer_native":
        return settings.datajuicer_python
    return settings.daft_python


def _normalize_pipeline_timing(
    local_raw: Path, timing_path: Path, platform: str
) -> None:
    cases: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted((local_raw / "pipeline_e2e").glob("**/summary.json")):
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
            elapsed_s = summary.get("elapsed_s")
            if elapsed_s is None or int(summary.get("returncode", 0)) != 0:
                continue
            case = str(summary.get("case") or path.parent.name)
            engine = str(summary.get("engine") or "unknown")
            case_id = f"{case}::{engine}"
            seen.add((case, engine))
            cases.append(
                {
                    "caseId": case_id,
                    "label": case_id,
                    "engineId": engine,
                    "measurementScope": "pipeline_e2e",
                    "metrics": {
                        "wallClockTime": {
                            "wall_clock_ns": int(float(elapsed_s) * 1_000_000_000)
                        }
                    },
                    "sourceArtifact": str(path.relative_to(local_raw)),
                }
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    # ``bench_capture.sh`` discovers result JSON beside its own output.  Full
    # derived tasks intentionally write runner results to a separate persistent
    # directory, so an otherwise successful run can leave the wrapper summary
    # with null status/elapsed.  The runner JSON is the primary target-owned
    # result and has already been validated by _capture_overlay_command.
    runner_root = local_raw / "pipeline_e2e" / "runner"
    for path in sorted(runner_root.glob("**/*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
            metrics = _mapping(result.get("metrics"), "runner.metrics")
            elapsed_s = metrics.get("elapsed_s")
            if result.get("status") != "ok" or elapsed_s is None:
                continue
            engine = str(result.get("engine_id") or "unknown")
            task_spec = _mapping(result.get("task_spec") or {}, "runner.task_spec")
            metadata = _mapping(task_spec.get("metadata") or {}, "runner.metadata")
            source_task = str(
                metadata.get("sourceTaskSpecId")
                or result.get("task_id")
                or path.parent.name
            )
            case = source_task.split("@", 1)[0].split("__", 1)[0]
            if (case, engine) in seen:
                continue
            seen.add((case, engine))
            case_id = f"{case}::{engine}"
            cases.append(
                {
                    "caseId": case_id,
                    "label": case_id,
                    "engineId": engine,
                    "measurementScope": "pipeline_e2e",
                    "metrics": {
                        "wallClockTime": {
                            "wall_clock_ns": int(float(elapsed_s) * 1_000_000_000)
                        }
                    },
                    "sourceArtifact": str(path.relative_to(local_raw)),
                }
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    if not cases:
        raise StepError(
            f"Volc pipeline_e2e produced no successful summary under {local_raw}"
        )
    _write_json(
        timing_path,
        {
            "schemaVersion": 1,
            "platform_id": platform,
            "benchmark": "volc-operator-sim",
            "cases": cases,
        },
    )


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StepError(f"{path} must be a mapping")
    return dict(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    return cleaned or "case"
