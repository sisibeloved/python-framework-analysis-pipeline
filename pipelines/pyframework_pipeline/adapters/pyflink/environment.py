"""PyFlink environment adapter.

Declares the framework-specific steps needed to set up a PyFlink analysis
environment in Docker containers (1 JM + N TM), with readiness verification
via the Flink REST API.
"""

from __future__ import annotations

from typing import Any

from pyframework_pipeline.environment.planning import PlanStep

DEFAULT_IMAGE = "flink:2.2.0-java17"
DEFAULT_NETWORK = "flink-network"
DEFAULT_TM_COUNT = 2


class PyFlinkEnvironmentAdapter:
    """Generates PyFlink-specific environment plan steps.

    Assumes a containerised deployment: Flink runs in Docker containers,
    the host only needs Docker. No Java/Python/pip on the host.
    """

    framework_id = "pyflink"

    def get_plan_steps(
        self,
        platform: str,
        platform_config: dict[str, Any],
        software: dict[str, Any],
        host_refs: dict[str, Any],
    ) -> list[PlanStep]:
        """Return framework-specific plan steps for PyFlink."""
        steps: list[PlanStep] = []

        image = software.get("flinkPyflinkImages", {}).get(
            platform,
            software.get("flinkImage", DEFAULT_IMAGE),
        )
        registry = software.get("dockerRegistry", "")
        if registry:
            image = f"{registry}/{image}"
        network = software.get("containerNetwork", DEFAULT_NETWORK)
        tm_count = DEFAULT_TM_COUNT
        use_tmpfs = software.get("taskmanagerTmpfs", False)
        topology = software.get("clusterTopology", "")
        if topology:
            parts = topology.split("-")
            for p in parts:
                if p.endswith("tm"):
                    try:
                        tm_count = int(p[:-2])
                    except ValueError:
                        pass

        # Determine the host (all roles on same machine in single-node mode)
        hosts_by_role = {}
        for host_entry in platform_config.get("hosts", []):
            hosts_by_role[host_entry["role"]] = host_entry["hostRef"]

        host = hosts_by_role.get("jobmanager", hosts_by_role.get("client", ""))
        host_alias = host_refs.get(host, {}).get("alias", host)
        arch = platform_config.get("arch", "x86_64")
        python_version = software.get("pythonVersion", "3.14.3")
        use_tmpfs = software.get("taskmanagerTmpfs", False)

        # Step 0: Build Flink+PyFlink image (skip if already exists)
        build_script = "adapters/pyflink/scripts/build-flink-image.sh"
        base_image = software.get("flinkImage", DEFAULT_IMAGE)
        build_env = (
            f"IMAGE_NAME={image} BASE_IMAGE={base_image} "
            f"NETWORK={network} PYTHON_VERSION={python_version} "
            f"TM_COUNT={tm_count} USE_TMPFS={'true' if use_tmpfs else 'false'}"
        )
        steps.append(PlanStep(
            id="build-flink-image",
            kind="build",
            hostRef=host,
            command=(
                f"docker image inspect {image} >/dev/null 2>&1 "
                f"|| {build_env} bash /tmp/build-flink-image.sh {arch}"
            ),
            description=f"Build Flink+PyFlink image on {host_alias} (~80 min first run)",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint=f"docker rmi {image}",
            scriptPath=build_script,
            timeout=6000,  # ~100 min
        ))

        # Step 1: Create Docker network
        steps.append(PlanStep(
            id="create-network",
            kind="prepare",
            hostRef=host,
            command=f"docker network create {network} 2>/dev/null || true",
            description=f"Create Docker network '{network}' on {host_alias}",
        ))

        # Step 2: Start JobManager
        steps.append(PlanStep(
            id="start-jobmanager",
            kind="framework-start",
            hostRef=host,
            command=_docker_reconcile_container(
                name="flink-jm",
                image=image,
                run_args=(
                f"docker run -d --name flink-jm --network {network} "
                f"-e FLINK_PROPERTIES='jobmanager.rpc.address: flink-jm' "
                f"-p 8081:8081 {image} jobmanager"
                ),
            ),
            description=f"Start JobManager container on {host_alias}",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint="docker rm -f flink-jm",
        ))

        # Step 4: Start TaskManagers
        for i in range(1, tm_count + 1):
            tmpfs_flag = " --tmpfs /tmp:rw,exec" if use_tmpfs else ""
            privileged_flag = " --privileged"
            steps.append(PlanStep(
                id=f"start-taskmanager-{i}",
                kind="framework-start",
                hostRef=host,
                command=_docker_reconcile_container(
                    name=f"flink-tm{i}",
                    image=image,
                    run_args=(
                    f"docker run -d --name flink-tm{i} --network {network} "
                    f"-e FLINK_PROPERTIES='jobmanager.rpc.address: flink-jm' "
                    f"-e PYTHONPERFSUPPORT=1 "
                    f"{tmpfs_flag}{privileged_flag} {image} taskmanager"
                    ),
                ),
                description=f"Start TaskManager {i} container on {host_alias}",
                mutatesHost=True,
                requiresApproval=True,
                rollbackHint=f"docker rm -f flink-tm{i}",
            ))

        # Step 5: Readiness — check cluster overview via REST API
        steps.append(PlanStep(
            id="readiness-cluster-health",
            kind="framework-readiness",
            hostRef=host,
            command=(
                "docker exec flink-jm curl -sf "
                "http://localhost:8081/overview"
            ),
            description=f"Check Flink cluster health on {host_alias}",
        ))

        # Step 6: Readiness — verify TM count
        steps.append(PlanStep(
            id="readiness-taskmanager-count",
            kind="framework-smoke-test",
            hostRef=host,
            command=(
                f"docker exec flink-jm curl -sf "
                f"http://localhost:8081/taskmanagers | "
                f"python3 -c \"import sys,json; d=json.load(sys.stdin); "
                f"assert len(d.get('taskmanagers',[])) >= {tm_count}, "
                f"f'TM count {{len(d.get(\\\"taskmanagers\\\",[]))}} < {tm_count}'\""
            ),
            description=f"Verify {tm_count} TaskManagers registered on {host_alias}",
        ))

        # Step 7: Install profiling tools inside containers
        profiling_tools = software.get("profilingTools", [])
        if profiling_tools:
            # Map tool names to package names
            tool_packages = {
                "perf": "linux-tools-common",
                "strace": "strace",
                "objdump": "binutils",
                "gdb": "gdb",
                "readelf": "binutils",
            }
            packages = sorted({tool_packages.get(t, t) for t in profiling_tools})
            pkg_str = " ".join(packages)

            for name in ["flink-jm"] + [f"flink-tm{i}" for i in range(1, tm_count + 1)]:
                steps.append(PlanStep(
                    id=f"install-profiling-tools-{name}",
                    kind="prepare",
                    hostRef=host,
                    command=(
                        f"docker exec -u root {name} bash -c "
                        f"'apt-get update -qq && apt-get install -y -qq {pkg_str} && "
                        f"perf_pkg=$(apt-cache search --names-only "
                        f"\"^linux-tools-.*-generic$\" | "
                        f"awk \"{{print \\$1}}\" | sort -V | tail -1); "
                        f"if [ -n \"$perf_pkg\" ]; then "
                        f"apt-get install -y -qq \"$perf_pkg\"; fi; "
                        f"perf_bin=$(find /usr/lib -path \"*linux-tools*\" "
                        f"-name perf -type f | "
                        f"sort -V | tail -1); "
                        f"if [ -n \"$perf_bin\" ]; then "
                        f"install -D -m 0755 \"$perf_bin\" /usr/local/bin/perf; fi'"
                    ),
                    description=f"Install profiling tools ({', '.join(packages)}) in {name} on {host_alias}",
                    mutatesHost=True,
                    requiresApproval=True,
                    rollbackHint=f"docker exec -u root {name} apt-get remove -y {pkg_str}",
                ))

            # Step 8: Verify profiling tools
            verify_cmds = {
                "perf": "/usr/local/bin/perf --version",
                "strace": "strace --version",
                "objdump": "objdump --version",
                "gdb": "gdb --version",
                "readelf": "readelf --version",
            }
            verifications = " && ".join(
                verify_cmds[t] for t in profiling_tools if t in verify_cmds
            )
            steps.append(PlanStep(
                id="verify-profiling-tools",
                kind="framework-readiness",
                hostRef=host,
                command=f"docker exec flink-jm bash -c '{verifications}'",
                description=f"Verify profiling tools available on {host_alias}",
            ))

            # Step 9: Enable perf_event_paranoid on host
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


def _docker_reconcile_container(name: str, image: str, run_args: str) -> str:
    return (
        f"if docker inspect {name} >/dev/null 2>&1; then "
        f"current=$(docker inspect -f '{{{{.Config.Image}}}}' {name}); "
        f"if [ \"$current\" = \"{image}\" ]; then "
        f"state=$(docker inspect -f '{{{{.State.Running}}}}' {name}); "
        f"if [ \"$state\" = \"true\" ]; then "
        f"echo {name} already running with {image}; "
        f"else docker start {name}; fi; "
        f"else docker rm -f {name} && {run_args}; fi; "
        f"else {run_args}; fi"
    )
