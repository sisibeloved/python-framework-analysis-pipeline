"""Execute environment plan steps on remote hosts via SSH.

Reads an environment-plan.json, connects to the target hosts via SSH,
executes each step, and writes an environment-record.json.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..acquisition.ssh_executor import SshExecutor
from ..remote import build_executor, get_platform_host_ref

logger = logging.getLogger(__name__)
_ENV_FINGERPRINT_MARKER = "PYFRAMEWORK_ENV_FINGERPRINT="


def deploy_plan(
    project_path: Path,
    platform_id: str,
    plan_path: Path | None = None,
    *,
    output_dir: Path | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    """Execute an environment plan on remote hosts.

    Parameters
    ----------
    project_path : Path
        Path to project.yaml.
    platform_id : str
        Platform ID (e.g. "arm", "x86").
    plan_path : Path or None
        Path to environment-plan.json. If None, generates a fresh plan.
    output_dir : Path or None
        Directory for deploy-record.json. If None, falls back to
        project_path.parent / "output" / platform_id.
    yes : bool
        Skip approval prompts for mutating steps.

    Returns
    -------
    dict with deployment summary.
    """

    _record_dir = output_dir or (project_path.parent / "output" / platform_id)

    def _save_record(record: dict) -> None:
        _record_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record, indent=2, ensure_ascii=False)
        for name in ("deploy-record.json", "environment-record.json"):
            (_record_dir / name).write_text(payload, encoding="utf-8")

    def _save_readiness_report(record: dict, project_id: str) -> None:
        plan_steps_by_id = {s.get("id", ""): s for s in steps}
        checks = []
        for step in record.get("steps", []):
            plan_step = plan_steps_by_id.get(step.get("id", ""))
            if not plan_step:
                continue
            if plan_step.get("kind") not in ("framework-readiness", "framework-smoke-test"):
                continue
            status = step.get("status", "unknown")
            checks.append({
                "id": step.get("id", ""),
                "status": status if status in ("passed", "failed") else "unknown",
                "message": plan_step.get("description", ""),
            })
        report = {
            "schemaVersion": 1,
            "projectId": project_id,
            "platform": platform_id,
            "status": "ready" if checks and all(c["status"] == "passed" for c in checks) else "not-ready",
            "checks": checks,
        }
        _record_dir.mkdir(parents=True, exist_ok=True)
        (_record_dir / "readiness-report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _fail(step_id: str, description: str, command: str,
              exit_code: int, stderr: str) -> dict[str, Any]:
        """Build failure result and save record for resume."""
        record_steps.append({
            "id": step_id,
            "status": "failed",
            "exitCode": exit_code,
            "command": command,
            "description": description,
            "note": stderr[:500],
        })
        failed_now = failed + 1
        record = {
            "schemaVersion": 1,
            "platform": platform_id,
            "startedAt": started_at,
            "finishedAt": _now_iso(),
            "mode": "full-auto",
            "steps": record_steps,
        }
        _save_record(record)
        return {
            "status": "failed",
            "platform": platform_id,
            "passed": passed,
            "failed": failed_now,
            "skipped": skipped,
            "failedStep": {
                "id": step_id,
                "description": description,
                "command": command,
                "exitCode": exit_code,
                "stderr": stderr[:500],
            },
            "record": record,
        }
    from ..config import load_environment_config
    from .planning import generate_plan

    env_config = load_environment_config(project_path)

    # Load or generate plan.
    if plan_path and plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        framework = env_config.get("framework", "")
        from ..cli._common import load_adapter
        adapter = load_adapter(framework)
        plan = generate_plan(project_path, platform_id, adapter)
        plan = json.loads(json.dumps(plan, ensure_ascii=False))

    plan_hash = plan.get("planHash", "")
    steps = plan.get("steps", [])

    if not steps:
        return {"status": "skipped", "reason": "no steps in plan"}

    # Build SSH executor for the target host.
    host_ref = get_platform_host_ref(env_config, platform_id)
    executor = build_executor(host_ref, env_config)

    # Resume: load previous record and skip already-passed steps.
    passed_ids: set[str] = set()
    prev_record_path = _record_dir / "deploy-record.json"
    if prev_record_path.exists():
        try:
            prev = json.loads(prev_record_path.read_text(encoding="utf-8"))
            for s in prev.get("steps", []):
                if s.get("status") == "passed":
                    passed_ids.add(s["id"])
            if passed_ids:
                logger.info("Resuming: skipping %d already-passed steps: %s",
                            len(passed_ids), ", ".join(sorted(passed_ids)))
        except (json.JSONDecodeError, KeyError):
            logger.warning("Could not parse previous deploy record, starting fresh")

    # Execute steps.
    record_steps: list[dict[str, Any]] = []
    environment_fingerprint: dict[str, Any] | None = None
    passed = 0
    failed = 0
    skipped = 0
    started_at = _now_iso()

    for step in steps:
        step_id = step.get("id", "")
        command = step.get("command", "")
        description = step.get("description", "")
        requires_approval = step.get("requiresApproval", False)
        rollback = step.get("rollbackHint", "")
        script_path = step.get("scriptPath", "")
        step_timeout = step.get("timeout", 0) or 120
        capture_output = bool(step.get("captureOutput", False))

        logger.info("[Step %s] %s", step_id, description)

        # Skip already-passed steps from previous run, except container
        # start/readiness steps which must always re-run (containers may have
        # stopped due to host reboot, docker restart policy, etc.).
        kind = step.get("kind", "")
        always_run = kind in ("framework-start", "framework-readiness")
        if step_id in passed_ids and not always_run:
            logger.info("[Step %s] Already passed, skipping", step_id)
            record_steps.append({"id": step_id, "status": "passed", "note": "resumed"})
            passed += 1
            continue

        # Upload local script if specified.
        if script_path:
            import pyframework_pipeline
            pkg_dir = Path(pyframework_pipeline.__file__).parent
            local_script = pkg_dir / script_path
            if not local_script.exists():
                return _fail(step_id, description, command, -1,
                             f"Local script not found: {local_script}")
            remote_name = local_script.name
            logger.info("[Step %s] Uploading %s", step_id, script_path)
            uploaded = executor.push_file(local_script, f"/tmp/{remote_name}")
            if not uploaded:
                return _fail(step_id, description, command, -1,
                             f"Failed to upload script {script_path}")

        # Approval check.
        if requires_approval and not yes:
            print(f"  [{step_id}] {description}")
            print(f"    Command: {command}")
            if rollback:
                print(f"    Rollback: {rollback}")
            try:
                answer = input("    Execute? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                record_steps.append({"id": step_id, "status": "skipped", "note": "user declined"})
                skipped += 1
                continue
            if answer in ("n", "no"):
                record_steps.append({"id": step_id, "status": "skipped", "note": "user declined"})
                skipped += 1
                continue

        # Execute.  Exit 255 is SSH's transport-failure status, not the
        # remote command's exit status.  Read-only probes are safe to retry;
        # mutating steps are deliberately attempted once because the client
        # cannot know whether the remote side started them before disconnect.
        max_attempts = 1 if step.get("mutatesHost") else 3
        for attempt in range(1, max_attempts + 1):
            try:
                result = executor.run(command, timeout=step_timeout, stream=True)
            except Exception as exc:
                logger.error("[Step %s] SSH error: %s", step_id, exc)
                return _fail(step_id, description, command, -1, str(exc))
            if result.returncode != 255 or attempt == max_attempts:
                break
            logger.warning(
                "[Step %s] SSH transport failed (attempt %d/%d); retrying read-only step",
                step_id,
                attempt,
                max_attempts,
            )

        if result.returncode != 0:
            stderr_snippet = result.stderr[:500] if result.stderr else ""
            if result.returncode == 127 and not stderr_snippet:
                stderr_snippet = "command not found in PATH (exit 127)"
            logger.error("[Step %s] Failed (exit %d)", step_id, result.returncode)
            return _fail(step_id, description, command, result.returncode, stderr_snippet)
        else:
            logger.info("[Step %s] Passed", step_id)
            step_record = {"id": step_id, "status": "passed", "exitCode": 0}
            if capture_output:
                step_record["stdout"] = result.stdout
                step_record["stderr"] = result.stderr
                captured = _parse_environment_fingerprint(result.stdout)
                if kind == "framework-fingerprint" and captured is None:
                    return _fail(
                        step_id,
                        description,
                        command,
                        -1,
                        "required environment fingerprint marker was not emitted",
                    )
                if captured is not None:
                    environment_fingerprint = captured
            if step.get("mutatesHost"):
                step_record["note"] = "executed by environment deploy"
            record_steps.append(step_record)
            passed += 1

    finished_at = _now_iso()

    # Build record.
    project_id = plan.get("projectId", "")
    record = {
        "schemaVersion": 1,
        "projectId": project_id,
        "platform": platform_id,
        "planHash": plan_hash,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "mode": "full-auto",
        "provenance": {
            "recordedBy": "auto",
            "source": "pyframework-pipeline environment deploy",
        },
        "steps": record_steps,
    }
    if environment_fingerprint is not None:
        record["environmentFingerprint"] = environment_fingerprint
        for key in (
            "sourceRevision",
            "imageId",
            "containerId",
            "dataManifestSha256",
            "condaFingerprints",
            "perf",
        ):
            if key in environment_fingerprint:
                record[key] = environment_fingerprint[key]

    _save_record(record)
    _save_readiness_report(record, project_id)

    return {
        "status": "failed" if failed > 0 else "completed",
        "platform": platform_id,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "record": record,
    }


def teardown(
    project_path: Path,
    platform_id: str,
    *,
    yes: bool = False,
) -> dict[str, Any]:
    """Tear down the remote environment (stop containers, remove network).

    Parameters
    ----------
    project_path : Path
        Path to project.yaml.
    platform_id : str
        Platform ID.
    yes : bool
        Skip approval prompt.

    Returns
    -------
    dict with teardown summary.
    """
    from ..config import load_environment_config

    env_config = load_environment_config(project_path)
    host_ref = get_platform_host_ref(env_config, platform_id)
    executor = build_executor(host_ref, env_config)

    software = env_config.get("software", {})
    network = software.get("containerNetwork", "flink-network")
    topology = software.get("clusterTopology", "1jm-2tm")

    tm_count = 2
    parts = topology.split("-")
    for p in parts:
        if p.endswith("tm"):
            try:
                tm_count = int(p[:-2])
            except ValueError:
                pass

    if not yes:
        containers = " ".join(
            ["flink-jm"] + [f"flink-tm{i}" for i in range(1, tm_count + 1)]
        )
        print(f"Will remove containers: {containers}")
        print(f"Will remove network: {network}")
        try:
            answer = input("Proceed? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return {"status": "cancelled"}
        if answer in ("n", "no"):
            return {"status": "cancelled"}

    removed = []
    errors = []

    # Stop containers in reverse order.
    for name in [f"flink-tm{i}" for i in range(tm_count, 0, -1)] + ["flink-jm"]:
        result = executor.run(f"docker rm -f {name}", timeout=30)
        if result.returncode == 0:
            removed.append(name)
        else:
            errors.append(f"{name}: {result.stderr.strip()}")

    # Remove network.
    result = executor.run(f"docker network rm {network}", timeout=30)
    network_removed = result.returncode == 0

    return {
        "status": "completed" if not errors else "partial",
        "removed": removed,
        "networkRemoved": network_removed,
        "errors": errors,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_environment_fingerprint(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(_ENV_FINGERPRINT_MARKER):
            continue
        payload = line[len(_ENV_FINGERPRINT_MARKER) :]
        try:
            value = json.loads(base64.b64decode(payload).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None
