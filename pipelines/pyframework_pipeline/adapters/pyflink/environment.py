"""PyFlink environment adapter.

Declares the framework-specific steps needed to set up a PyFlink analysis
environment in Docker containers (1 JM + N TM), with readiness verification
via the Flink REST API.
"""

from __future__ import annotations

from typing import Any

from pyframework_pipeline.environment.planning import PlanStep

DEFAULT_IMAGE = "flink:1.20.1-java17"
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

        image = software.get("flinkImage", DEFAULT_IMAGE)
        network = software.get("containerNetwork", DEFAULT_NETWORK)
        tm_count = DEFAULT_TM_COUNT
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

        # Step 1: Create Docker network
        steps.append(PlanStep(
            id="create-network",
            kind="prepare",
            hostRef=host,
            command=f"docker network create {network} 2>/dev/null || true",
            description=f"Create Docker network '{network}' on {host_alias}",
        ))

        # Step 2: Pull Flink image
        steps.append(PlanStep(
            id="pull-flink-image",
            kind="prepare",
            hostRef=host,
            command=f"docker pull {image}",
            description=f"Pull Flink image {image} on {host_alias}",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint=f"docker rmi {image}",
        ))

        # Step 3: Start JobManager
        steps.append(PlanStep(
            id="start-jobmanager",
            kind="framework-start",
            hostRef=host,
            command=(
                f"docker run -d --name flink-jm --network {network} "
                f"-e FLINK_PROPERTIES='jobmanager.rpc.address: flink-jm' "
                f"-p 8081:8081 {image} jobmanager"
            ),
            description=f"Start JobManager container on {host_alias}",
            mutatesHost=True,
            requiresApproval=True,
            rollbackHint="docker rm -f flink-jm",
        ))

        # Step 4: Start TaskManagers
        for i in range(1, tm_count + 1):
            steps.append(PlanStep(
                id=f"start-taskmanager-{i}",
                kind="framework-start",
                hostRef=host,
                command=(
                    f"docker run -d --name flink-tm{i} --network {network} "
                    f"-e FLINK_PROPERTIES='jobmanager.rpc.address: flink-jm' "
                    f"{image} taskmanager"
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

        return steps
