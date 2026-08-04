"""UDF_Benchmarking environment adapter.

Creates a single Python 3.11 Docker container with UDF_Benchmarking checked out
under /opt/UDF_Benchmarking.  The workload deploy step overlays a small
project-local config into /workspace/benchmark before running upstream main.py.
"""

from __future__ import annotations

import shlex
from typing import Any

from pyframework_pipeline.environment.planning import PlanStep

DEFAULT_BASE_IMAGE = "python:3.11-slim"
DEFAULT_CONTAINER = "udf-benchmarking-bench"
DEFAULT_IMAGE = "udf-benchmarking-bench:py311"
DEFAULT_REPO = "https://gitcode.com/stone31415/UDF_Benchmarking.git"


class UdfBenchmarkingEnvironmentAdapter:
    """Generates UDF_Benchmarking-specific environment plan steps."""

    framework_id = "udfbenchmarking"

    def get_plan_steps(
        self,
        platform: str,
        platform_config: dict[str, Any],
        software: dict[str, Any],
        host_refs: dict[str, Any],
    ) -> list[PlanStep]:
        steps: list[PlanStep] = []

        hosts_by_role = {
            entry["role"]: entry["hostRef"]
            for entry in platform_config.get("hosts", [])
            if "role" in entry and "hostRef" in entry
        }
        host = hosts_by_role.get("client") or next(iter(hosts_by_role.values()), "")
        host_alias = host_refs.get(host, {}).get("alias", host)
        host_env = host_refs.get(host, {}).get("env", {})
        arch = platform_config.get("arch", "x86_64")

        image = software.get("udfBenchmarkingImages", {}).get(
            platform,
            software.get("udfBenchmarkingImage", DEFAULT_IMAGE),
        )
        registry = software.get("dockerRegistry", "")
        base_image = software.get("pythonImage", DEFAULT_BASE_IMAGE)
        if registry:
            base_image = f"{registry}/{base_image}"

        container = software.get("udfBenchmarkingContainer", DEFAULT_CONTAINER)
        repo = software.get("udfBenchmarkingRepo", DEFAULT_REPO)
        revision = software.get("udfBenchmarkingRevision", "")

        pip_trusted_hosts = software.get("pipTrustedHosts", "")
        if isinstance(pip_trusted_hosts, list):
            pip_trusted_hosts = " ".join(str(host) for host in pip_trusted_hosts)

        build_env = _env_assignments({
            "IMAGE_NAME": image,
            "BASE_IMAGE": base_image,
            "UDF_BENCHMARKING_REPO": repo,
            "UDF_BENCHMARKING_REVISION": revision,
            "NUMPY_VERSION": software.get("numpyVersion", "2.4.3"),
            "PY_SPY_VERSION": software.get("pySpyVersion", ""),
            "APT_MIRROR": software.get("aptMirror", ""),
            "APT_SECURITY_MIRROR": software.get("aptSecurityMirror", ""),
            "PIP_INDEX_URL": software.get("pipIndexUrl", ""),
            "PIP_EXTRA_INDEX_URL": software.get("pipExtraIndexUrl", ""),
            "PIP_TRUSTED_HOST": pip_trusted_hosts,
            "PIP_TIMEOUT": software.get("pipTimeout", ""),
            "PIP_RETRIES": software.get("pipRetries", ""),
            **_proxy_env(host_env),
        })
        steps.append(PlanStep(
            id="build-udfbenchmarking-image",
            kind="build",
            hostRef=host,
            command=(
                f"docker image inspect {image} >/dev/null 2>&1 "
                f"|| {build_env} bash /tmp/build-udfbenchmarking-image.sh {arch}"
            ),
            description=f"Build UDF_Benchmarking Python 3.11 image on {host_alias}",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint=f"docker rmi {image}",
            scriptPath="adapters/udfbenchmarking/scripts/build-udfbenchmarking-image.sh",
            timeout=3600,
        ))

        run_env = _docker_env_flags({
            **_proxy_env(host_env),
            "PYTHONUNBUFFERED": "1",
        })
        run_args = (
            f"docker run -d --name {container} --privileged "
            f"{run_env} {image} sleep infinity"
        )
        steps.append(PlanStep(
            id="start-udfbenchmarking",
            kind="framework-start",
            hostRef=host,
            command=_docker_reconcile_container(
                name=container,
                image=image,
                run_args=run_args,
                require_privileged=True,
            ),
            description=f"Start UDF_Benchmarking container on {host_alias}",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint=f"docker rm -f {container}",
        ))

        readiness_script = (
            "test -d /opt/UDF_Benchmarking && "
            "cd /opt/UDF_Benchmarking && "
            "python -c \"import daft, cv2, skimage, psutil, yaml; "
            "print('udfbenchmarking-ready')\""
        )
        steps.append(PlanStep(
            id="readiness-udfbenchmarking",
            kind="framework-readiness",
            hostRef=host,
            command=f"docker exec {container} bash -lc {shlex.quote(readiness_script)}",
            description=f"Verify UDF_Benchmarking imports on {host_alias}",
            timeout=120,
        ))

        profiling_tools = software.get("profilingTools", [])
        verify_tools = ["command -v perf", "command -v objdump"]
        if _python_flamegraph_enabled(software) or "py-spy" in profiling_tools:
            verify_tools.append("command -v py-spy")
        verify_tools.append("python3 --version")

        steps.append(PlanStep(
            id="verify-udfbenchmarking-perf-tools",
            kind="framework-readiness",
            hostRef=host,
            command=(
                f"docker exec {container} bash -lc "
                f"{shlex.quote(_join_shell_checks(verify_tools))}"
            ),
            description=f"Verify perf, objdump, and optional py-spy in {container} on {host_alias}",
        ))

        if "perf" in profiling_tools:
            steps.append(PlanStep(
                id="enable-perf-paranoid",
                kind="prepare",
                hostRef=host,
                command="sudo sysctl -w kernel.perf_event_paranoid=0",
                description=f"Set kernel.perf_event_paranoid=0 on {host_alias}",
                requiresPrivilege=True,
                requiresApproval=True,
                rollbackHint="sudo sysctl -w kernel.perf_event_paranoid=2",
            ))

        return steps


def _python_flamegraph_enabled(software: dict[str, Any]) -> bool:
    config = software.get("pythonFlamegraph", {})
    if isinstance(config, dict):
        return _bool_env(config.get("enabled", False)) == "true"
    return _bool_env(config) == "true"


def _join_shell_checks(checks: list[str]) -> str:
    return " && ".join(checks)


def _proxy_env(host_env: dict[str, Any]) -> dict[str, Any]:
    names = [
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    ]
    return {name: host_env.get(name, "") for name in names}


def _bool_env(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    raw = str(value).strip().lower()
    return "true" if raw in {"1", "true", "yes", "on"} else "false"


def _env_assignments(values: dict[str, Any]) -> str:
    return " ".join(
        f"{name}={shlex.quote(str(value))}"
        for name, value in values.items()
        if value not in ("", None)
    )


def _docker_env_flags(values: dict[str, Any]) -> str:
    return " ".join(
        f"-e {name}={shlex.quote(str(value))}"
        for name, value in values.items()
        if value not in ("", None)
    )


def _docker_reconcile_container(
    name: str,
    image: str,
    run_args: str,
    require_privileged: bool = False,
) -> str:
    checks = "recreate=0; "
    if require_privileged:
        checks += (
            f"priv=$(docker inspect -f '{{{{.HostConfig.Privileged}}}}' {name}); "
            f"if [ \"$priv\" != \"true\" ]; then "
            f"echo Recreating {name} with --privileged; "
            f"recreate=1; fi; "
        )
    checks += f"if [ \"$recreate\" = \"1\" ]; then docker rm -f {name}; fi; "
    return (
        f"if docker inspect {name} >/dev/null 2>&1; then "
        f"{checks}"
        f"fi; "
        f"if docker inspect {name} >/dev/null 2>&1; then "
        f"current=$(docker inspect -f '{{{{.Config.Image}}}}' {name}); "
        f"if [ \"$current\" = \"{image}\" ]; then "
        f"state=$(docker inspect -f '{{{{.State.Running}}}}' {name}); "
        f"if [ \"$state\" != \"true\" ]; then docker start {name}; fi; "
        f"else docker rm -f {name} && {run_args}; fi; "
        f"else {run_args}; fi"
    )
