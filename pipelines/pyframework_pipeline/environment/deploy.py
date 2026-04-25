"""Execute environment plan steps on remote hosts via SSH.

Reads an environment-plan.json, connects to the target hosts via SSH,
executes each step, and writes an environment-record.json.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..acquisition.ssh_executor import SshExecutor
from ..remote import build_executor, get_platform_host_ref

logger = logging.getLogger(__name__)


def deploy_plan(
    project_path: Path,
    platform_id: str,
    plan_path: Path | None = None,
    *,
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
    yes : bool
        Skip approval prompts for mutating steps.

    Returns
    -------
    dict with deployment summary.
    """
    from ..config import load_environment_config
    from .planning import generate_plan

    env_config = load_environment_config(project_path)

    # Load or generate plan.
    if plan_path and plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    else:
        framework = env_config.get("framework", "")
        from ..cli import _load_adapter
        adapter = _load_adapter(framework)
        plan = generate_plan(project_path, platform_id, adapter)
        plan = json.loads(json.dumps(plan, ensure_ascii=False))

    plan_hash = plan.get("planHash", "")
    steps = plan.get("steps", [])

    if not steps:
        return {"status": "skipped", "reason": "no steps in plan"}

    # Build SSH executor for the target host.
    host_ref = get_platform_host_ref(env_config, platform_id)
    executor = build_executor(host_ref, env_config)

    # Execute steps.
    record_steps: list[dict[str, Any]] = []
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

        logger.info("[Step %s] %s", step_id, description)

        # Upload local script if specified.
        if script_path:
            import pyframework_pipeline
            pkg_dir = Path(pyframework_pipeline.__file__).parent
            local_script = pkg_dir / script_path
            if not local_script.exists():
                return {
                    "status": "failed",
                    "platform": platform_id,
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                    "failedStep": {
                        "id": step_id,
                        "description": description,
                        "command": command,
                        "exitCode": -1,
                        "stderr": f"Local script not found: {local_script}",
                    },
                    "record": {
                        "schemaVersion": 1,
                        "platform": platform_id,
                        "startedAt": started_at,
                        "finishedAt": _now_iso(),
                        "mode": "full-auto",
                        "steps": record_steps,
                    },
                }
            remote_name = local_script.name
            logger.info("[Step %s] Uploading %s", step_id, script_path)
            uploaded = executor.push_file(local_script, f"/tmp/{remote_name}")
            if not uploaded:
                return {
                    "status": "failed",
                    "platform": platform_id,
                    "passed": passed,
                    "failed": failed,
                    "skipped": skipped,
                    "failedStep": {
                        "id": step_id,
                        "description": description,
                        "command": command,
                        "exitCode": -1,
                        "stderr": f"Failed to upload script {script_path}",
                    },
                    "record": {
                        "schemaVersion": 1,
                        "platform": platform_id,
                        "startedAt": started_at,
                        "finishedAt": _now_iso(),
                        "mode": "full-auto",
                        "steps": record_steps,
                    },
                }

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

        # Execute.
        try:
            result = executor.run(command, timeout=step_timeout)
        except Exception as exc:
            logger.error("[Step %s] SSH error: %s", step_id, exc)
            record_steps.append({
                "id": step_id,
                "status": "failed",
                "exitCode": -1,
                "command": command,
                "description": description,
                "note": str(exc),
            })
            failed += 1
            return {
                "status": "failed",
                "platform": platform_id,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "failedStep": {
                    "id": step_id,
                    "description": description,
                    "command": command,
                    "exitCode": -1,
                    "stderr": str(exc),
                },
                "record": {
                    "schemaVersion": 1,
                    "platform": platform_id,
                    "startedAt": started_at,
                    "finishedAt": _now_iso(),
                    "mode": "full-auto",
                    "steps": record_steps,
                },
            }

        if result.returncode != 0:
            stderr_snippet = result.stderr[:500] if result.stderr else ""
            if result.returncode == 127 and not stderr_snippet:
                stderr_snippet = "command not found in PATH (exit 127)"
            logger.error("[Step %s] Failed (exit %d)", step_id, result.returncode)
            record_steps.append({
                "id": step_id,
                "status": "failed",
                "exitCode": result.returncode,
                "command": command,
                "description": description,
                "note": stderr_snippet,
            })
            failed += 1
            # Abort: attach details so caller can surface them.
            return {
                "status": "failed",
                "platform": platform_id,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "failedStep": {
                    "id": step_id,
                    "description": description,
                    "command": command,
                    "exitCode": result.returncode,
                    "stderr": stderr_snippet,
                },
                "record": {
                    "schemaVersion": 1,
                    "platform": platform_id,
                    "startedAt": started_at,
                    "finishedAt": _now_iso(),
                    "mode": "full-auto",
                    "steps": record_steps,
                },
            }
        else:
            logger.info("[Step %s] Passed", step_id)
            record_steps.append({"id": step_id, "status": "passed", "exitCode": 0})
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
