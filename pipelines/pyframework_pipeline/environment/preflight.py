"""Read-only remote environment preflight checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from ..config import load_environment_config
from ..remote import build_executor, get_platform_host_ref


ExecutorFactory = Callable[[str, dict[str, Any]], Any]


_CHECKS: list[dict[str, Any]] = [
    {
        "id": "host",
        "command": "printf '[host] '; hostname; printf '[arch] '; uname -m",
        "timeout": 30,
    },
    {
        "id": "docker",
        "command": "docker --version && docker info --format 'DockerServer={{.ServerVersion}}'",
        "timeout": 30,
    },
    {
        "id": "containers",
        "command": "docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | sed -n '1,20p'",
        "timeout": 30,
    },
    {
        "id": "resources",
        "command": "df -Pk / /var/lib/docker 2>/dev/null; free -m",
        "timeout": 30,
    },
    {
        "id": "perf-paranoid",
        "command": "sysctl kernel.perf_event_paranoid",
        "timeout": 30,
    },
    {
        "id": "images",
        "command": "docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep -E 'flink-pyflink|data-juicer|udf-benchmarking|volc-operator-sim|python' || true",
        "timeout": 30,
    },
]


def run_preflight(
    project_path: Path,
    platform: str,
    *,
    output_dir: Path | None = None,
    executor_factory: ExecutorFactory = build_executor,
    check_timeout: int | None = None,
) -> dict[str, Any]:
    """Run read-only remote checks for one platform.

    The checks intentionally avoid Docker/container/sysctl mutations.  They
    record command, stdout, stderr, exit code, and timeout so failed real-host
    runs can be diagnosed without relying on terminal scrollback.
    """
    env_config = load_environment_config(project_path)
    host_ref = get_platform_host_ref(env_config, platform)
    executor = executor_factory(host_ref, env_config)

    checks: list[dict[str, Any]] = []
    for index, check in enumerate(_CHECKS):
        timeout = check_timeout if check_timeout is not None else check["timeout"]
        result = executor.run(check["command"], timeout=timeout)
        checks.append({
            "id": check["id"],
            "command": check["command"],
            "timeout": timeout,
            "mutatesHost": False,
            "exitCode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        })
        if index == 0 and result.returncode != 0:
            checks.extend(_skipped_checks(_CHECKS[index + 1:], check_timeout))
            break

    warnings = _build_warnings(env_config, platform, checks)
    has_error = any(c["exitCode"] != 0 for c in checks)
    report = {
        "status": "error" if has_error else ("warning" if warnings else "ok"),
        "project": str(project_path),
        "platform": platform,
        "hostRef": host_ref,
        "warnings": warnings,
        "checks": checks,
    }

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "preflight-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return report


def _skipped_checks(
    checks: list[dict[str, Any]],
    check_timeout: int | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "id": check["id"],
            "command": check["command"],
            "timeout": check_timeout if check_timeout is not None else check["timeout"],
            "mutatesHost": False,
            "skipped": True,
            "exitCode": None,
            "stdout": "",
            "stderr": "skipped because host connectivity check failed",
        }
        for check in checks
    ]


def _build_warnings(
    env_config: dict[str, Any],
    platform: str,
    checks: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    by_id = {check["id"]: check for check in checks}

    perf = by_id.get("perf-paranoid", {})
    if perf.get("exitCode") == 0:
        value = _parse_perf_paranoid(str(perf.get("stdout", "")))
        if value is not None and value > 0:
            warnings.append(
                f"kernel.perf_event_paranoid is {value}; perf collection "
                "usually requires 0 and deploy will need to change it."
            )

    image = _target_image(env_config, platform)
    images = by_id.get("images", {})
    if image and images.get("exitCode") == 0 and image not in str(images.get("stdout", "")):
        warnings.append(
            f"Target image {image} is not present; deploy will need to build it."
        )

    resources = by_id.get("resources", {})
    if resources.get("exitCode") == 0:
        available_kb = _parse_min_available_disk_kb(str(resources.get("stdout", "")))
        if available_kb is not None and available_kb < 20 * 1024 * 1024:
            warnings.append(
                f"Available disk is {available_kb // 1024} MB; image build "
                "should have at least 20480 MB free."
            )

        memory_mb = _parse_memory_total_mb(str(resources.get("stdout", "")))
        if memory_mb is not None and memory_mb < 4096:
            warnings.append(
                f"Total memory is {memory_mb} MB; benchmark/deploy should "
                "have at least 4096 MB."
            )

    return warnings


def _target_image(env_config: dict[str, Any], platform: str) -> str:
    software = env_config.get("software", {})
    framework = str(env_config.get("framework", ""))
    if framework == "datajuicer":
        return str(software.get("dataJuicerImages", {}).get(platform, ""))
    if framework == "udfbenchmarking":
        return str(software.get("udfBenchmarkingImages", {}).get(platform, ""))
    if framework == "volcoperatorsim":
        return str(software.get("volcOperatorSimImages", {}).get(platform, ""))
    return str(software.get("flinkPyflinkImages", {}).get(platform, ""))


def _parse_perf_paranoid(stdout: str) -> int | None:
    for token in reversed(stdout.replace("=", " ").split()):
        try:
            return int(token)
        except ValueError:
            continue
    return None


def _parse_min_available_disk_kb(stdout: str) -> int | None:
    values: list[int] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] == "Filesystem" or not parts[4].endswith("%"):
            continue
        try:
            values.append(int(parts[3]))
        except ValueError:
            continue
    return min(values) if values else None


def _parse_memory_total_mb(stdout: str) -> int | None:
    for line in stdout.splitlines():
        parts = line.split()
        if parts and parts[0].rstrip(":") == "Mem" and len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None
