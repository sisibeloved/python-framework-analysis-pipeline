"""Generate environment setup plans from project config + framework adapter.

The planning layer combines:
- ``environment.yaml`` (project instance layer)
- framework adapter (framework-specific steps)
- generic probes (OS, CPU, Python, Docker)

to produce a ``environment-plan.json`` that can be executed manually or
automatically.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .parser import load_environment_yaml

SCHEMA_VERSION = 1


@dataclass
class PlanStep:
    id: str
    kind: str
    hostRef: str
    command: str
    required: bool = True
    mutatesHost: bool = False
    requiresPrivilege: bool = False
    requiresApproval: bool = False
    rollbackHint: str = "No rollback required."
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "hostRef": self.hostRef,
            "command": self.command,
            "required": self.required,
            "mutatesHost": self.mutatesHost,
            "requiresPrivilege": self.requiresPrivilege,
            "requiresApproval": self.requiresApproval,
            "rollbackHint": self.rollbackHint,
        }
        if self.description:
            d["description"] = self.description
        return d


@dataclass
class EnvironmentPlan:
    projectId: str
    framework: str
    platform: str
    mode: str
    steps: list[PlanStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        steps = [s.to_dict() for s in self.steps]
        plan = {
            "schemaVersion": SCHEMA_VERSION,
            "projectId": self.projectId,
            "framework": self.framework,
            "platform": self.platform,
            "mode": self.mode,
            "planHash": "",
            "steps": steps,
        }
        plan["planHash"] = _compute_hash(plan)
        return plan


def generate_plan(
    project_yaml_path: Path,
    platform: str,
    adapter: Any,
) -> dict[str, Any]:
    """Generate an environment plan for a specific platform.

    Parameters
    ----------
    project_yaml_path : Path
        Path to ``project.yaml``.
    platform : str
        Platform ID (must exist in environment.yaml platforms).
    adapter
        Framework adapter instance with ``get_plan_steps()`` method.

    Returns
    -------
    dict
        The environment plan as a JSON-serialisable dict.
    """
    env_yaml_path = project_yaml_path.parent / "environment.yaml"
    if not env_yaml_path.exists():
        raise FileNotFoundError(f"environment.yaml not found at {env_yaml_path}")

    env_config = load_environment_yaml(env_yaml_path)

    # Resolve project ID from project.yaml
    project_id = _read_project_id(project_yaml_path)

    # Find the target platform config
    platform_config = _find_platform(env_config, platform)
    if platform_config is None:
        available = [p.get("id", "?") for p in env_config.get("platforms", [])]
        raise ValueError(f"Platform '{platform}' not found. Available: {available}")

    host_refs = env_config.get("hostRefs", {})
    software = env_config.get("software", {})

    # Build plan
    plan = EnvironmentPlan(
        projectId=project_id,
        framework=env_config["framework"],
        platform=platform,
        mode=env_config.get("mode", "plan-only"),
    )

    # Generic probe steps — deduplicate by host (single machine may have multiple roles)
    seen_hosts: set[str] = set()
    for host_entry in platform_config.get("hosts", []):
        host_ref = host_entry["hostRef"]
        if host_ref in seen_hosts:
            continue
        seen_hosts.add(host_ref)
        _add_generic_probes(plan, host_ref, host_refs.get(host_ref, {}))

    # Framework-specific steps from adapter
    if hasattr(adapter, "get_plan_steps"):
        fw_steps = adapter.get_plan_steps(
            platform=platform,
            platform_config=platform_config,
            software=software,
            host_refs=host_refs,
        )
        plan.steps.extend(fw_steps)

    return plan.to_dict()


def _add_generic_probes(
    plan: EnvironmentPlan,
    host_ref: str,
    host_config: dict[str, Any],
) -> None:
    """Add generic host-level probe steps.

    These probes only check host-level facts (OS, CPU, disk, Docker).
    No Java or Python probes — those are container-internal concerns.
    """
    capabilities = host_config.get("capabilities", {})
    host_alias = host_config.get("alias", host_ref)

    plan.steps.append(PlanStep(
        id=f"probe-os-{host_ref}",
        kind="probe",
        hostRef=host_ref,
        command="uname -a && cat /etc/os-release",
        description=f"Probe OS info on {host_alias}",
    ))

    plan.steps.append(PlanStep(
        id=f"probe-cpu-{host_ref}",
        kind="probe",
        hostRef=host_ref,
        command="lscpu | grep -E 'Architecture|CPU\\(s\\)|Model name|Thread|Core|Socket'",
        description=f"Probe CPU info on {host_alias}",
    ))

    plan.steps.append(PlanStep(
        id=f"probe-disk-{host_ref}",
        kind="probe",
        hostRef=host_ref,
        command="df -h / && free -h",
        description=f"Check disk space and memory on {host_alias}",
    ))

    if capabilities.get("docker"):
        plan.steps.append(PlanStep(
            id=f"check-docker-{host_ref}",
            kind="check",
            hostRef=host_ref,
            command="docker --version && docker info --format '{{.ServerVersion}}'",
            description=f"Check Docker on {host_alias}",
        ))


def _find_platform(env_config: dict[str, Any], platform_id: str) -> dict[str, Any] | None:
    for p in env_config.get("platforms", []):
        if p.get("id") == platform_id:
            return p
    return None


def _read_project_id(project_yaml_path: Path) -> str:
    """Read the project ID from a minimal project.yaml."""
    for line in project_yaml_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("id:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    raise ValueError(f"Cannot find 'id' in {project_yaml_path}")


def _compute_hash(plan_dict: dict[str, Any]) -> str:
    """Compute SHA-256 hash of the plan (excluding the hash field itself)."""
    copy = {k: v for k, v in plan_dict.items() if k != "planHash"}
    raw = json.dumps(copy, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
