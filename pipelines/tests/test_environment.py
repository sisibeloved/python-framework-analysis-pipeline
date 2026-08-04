"""Tests for environment plan generation and record validation."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import atexit
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "pipelines"))
PROJECT_YAML = REPO_ROOT / "projects" / "pyflink-tpch-reference" / "project.yaml"


def _ensure_environment_yaml_fixture() -> None:
    env = PROJECT_YAML.parent / "environment.yaml"
    example = PROJECT_YAML.parent / "environment.yaml.example"
    if env.exists() or not example.exists():
        return
    env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    atexit.register(lambda: env.exists() and env.unlink())


_ensure_environment_yaml_fixture()


class CliInvoker:
    """Helper to run the pipeline CLI as a subprocess."""

    @staticmethod
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pyframework_pipeline", *args],
            cwd=REPO_ROOT,
            env={"PYTHONPATH": str(REPO_ROOT / "pipelines")},
            text=True,
            capture_output=True,
            check=False,
        )


class EnvironmentPlanTest(unittest.TestCase):
    """Test 'environment plan' subcommand."""

    def test_plan_generates_arm_plan(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)

        self.assertEqual(plan["projectId"], "pyflink-tpch-reference")
        self.assertEqual(plan["framework"], "pyflink")
        self.assertEqual(plan["platform"], "arm")
        self.assertEqual(plan["mode"], "plan-only")
        self.assertTrue(plan["planHash"].startswith("sha256:"))
        self.assertGreater(len(plan["steps"]), 0)

    def test_plan_generates_x86_plan(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "x86"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["platform"], "x86")

    def test_plan_rejects_unknown_platform(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "riscv"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_plan_steps_have_required_fields(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)

        required_fields = [
            "id", "kind", "hostRef", "command",
            "required", "mutatesHost", "requiresPrivilege",
            "requiresApproval", "rollbackHint",
        ]
        for step in plan["steps"]:
            for field in required_fields:
                self.assertIn(field, step, f"Step {step.get('id', '?')} missing {field}")

    def test_plan_contains_container_steps(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        step_ids = [s["id"] for s in plan["steps"]]

        # Build step (runs build script if image missing)
        self.assertIn("build-flink-image", step_ids)
        build_step = next(s for s in plan["steps"] if s["id"] == "build-flink-image")
        self.assertEqual(build_step["kind"], "build")
        self.assertEqual(build_step["scriptPath"], "adapters/pyflink/scripts/build-flink-image.sh")
        self.assertEqual(build_step["timeout"], 6000)
        self.assertIn("bash /tmp/build-flink-image.sh aarch64", build_step["command"])
        self.assertTrue(build_step["mutatesHost"])
        self.assertTrue(build_step["requiresApproval"])

        # Container deployment steps
        self.assertIn("start-jobmanager", step_ids)
        self.assertIn("start-taskmanager-1", step_ids)
        self.assertIn("start-taskmanager-2", step_ids)
        self.assertIn("readiness-cluster-health", step_ids)
        self.assertIn("readiness-taskmanager-count", step_ids)

    def test_plan_uses_platform_specific_pyflink_image(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        build_step = next(s for s in plan["steps"] if s["id"] == "build-flink-image")
        start_step = next(s for s in plan["steps"] if s["id"] == "start-jobmanager")

        self.assertIn("flink-pyflink:2.2.0-py314-arm-final", build_step["command"])
        self.assertIn("flink-pyflink:2.2.0-py314-arm-final", start_step["command"])

    def test_plan_build_step_skips_when_image_exists(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        build_step = next(s for s in plan["steps"] if s["id"] == "build-flink-image")

        self.assertIn("docker image inspect", build_step["command"])
        self.assertIn("bash /tmp/build-flink-image.sh", build_step["command"])
        self.assertIn("IMAGE_NAME=", build_step["command"])
        self.assertIn("BASE_IMAGE=", build_step["command"])
        self.assertIn("PYTHON_VERSION=", build_step["command"])

    def test_plan_recreates_existing_container_when_image_differs(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "x86"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        start_step = next(s for s in plan["steps"] if s["id"] == "start-jobmanager")

        self.assertIn("docker inspect -f '{{.Config.Image}}' flink-jm", start_step["command"])
        self.assertIn("docker rm -f flink-jm", start_step["command"])
        self.assertIn("flink-pyflink:2.2.0-py314-x86-final", start_step["command"])

    def test_plan_supports_host_cluster_network_mode(self) -> None:
        from pyframework_pipeline.adapters.pyflink.environment import PyFlinkEnvironmentAdapter

        adapter = PyFlinkEnvironmentAdapter()
        steps = adapter.get_plan_steps(
            platform="arm",
            platform_config={
                "arch": "aarch64",
                "hosts": [
                    {"role": "jobmanager", "hostRef": "test-host"},
                    {"role": "taskmanager", "hostRef": "test-host"},
                ],
            },
            software={
                "flinkPyflinkImages": {"arm": "my-flink:latest"},
                "containerNetwork": "test-net",
                "clusterNetworkMode": "host",
            },
            host_refs={"test-host": {"alias": "1.2.3.4"}},
        )
        start_jm = next(s for s in steps if s.id == "start-jobmanager")
        start_tm = next(s for s in steps if s.id == "start-taskmanager-1")

        self.assertIn("--network host", start_jm.command)
        self.assertIn("--network host", start_tm.command)
        self.assertIn("--add-host $(hostname):127.0.0.1", start_jm.command)
        self.assertIn("--add-host $(hostname):127.0.0.1", start_tm.command)
        self.assertIn("jobmanager.rpc.address: 127.0.0.1", start_jm.command)
        self.assertIn("jobmanager.rpc.address: 127.0.0.1", start_tm.command)
        self.assertIn("HostConfig.NetworkMode", start_jm.command)
        self.assertIn("HostConfig.ExtraHosts", start_jm.command)
        self.assertIn("Recreating flink-jm for network mode host", start_jm.command)
        self.assertIn("Recreating flink-jm with host hostname mapping", start_jm.command)

    def test_plan_contains_profiling_tool_steps(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        step_ids = [s["id"] for s in plan["steps"]]

        # Profiling tool verification steps (tools baked into image)
        self.assertIn("verify-profiling-tools-flink-jm", step_ids)
        self.assertIn("verify-profiling-tools-flink-tm1", step_ids)
        self.assertIn("verify-profiling-tools-flink-tm2", step_ids)
        self.assertIn("verify-profiling-tools", step_ids)
        self.assertIn("enable-perf-paranoid", step_ids)

        # Verify steps check dpkg (read-only), not install
        verify_jm_step = next(
            s for s in plan["steps"]
            if s["id"] == "verify-profiling-tools-flink-jm"
        )
        verify_all_step = next(
            s for s in plan["steps"]
            if s["id"] == "verify-profiling-tools"
        )
        self.assertIn("dpkg -s", verify_jm_step["command"])
        self.assertIn("flink-jm", verify_jm_step["command"])
        self.assertIn("perf --version", verify_all_step["command"])
        # Verification steps don't mutate the host
        self.assertFalse(verify_jm_step.get("mutatesHost", False))

    def test_plan_sets_perf_paranoid_to_zero(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        paranoid_step = next(
            s for s in plan["steps"] if s["id"] == "enable-perf-paranoid"
        )
        self.assertIn("kernel.perf_event_paranoid=0", paranoid_step["command"])

    def test_plan_tm_containers_have_pythonperf(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        tm_step = next(
            s for s in plan["steps"] if s["id"] == "start-taskmanager-1"
        )
        self.assertIn("PYTHONPERFSUPPORT=1", tm_step["command"])

    def test_plan_deduplicates_host_probes(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        step_ids = [s["id"] for s in plan["steps"]]

        # Single-machine mode: only one set of host probes
        self.assertIn("probe-os-arm-host", step_ids)
        self.assertNotIn("probe-os-jobmanager", step_ids)

    def test_plan_writes_to_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = CliInvoker.run(
                "environment", "plan", str(PROJECT_YAML),
                "--platform", "arm", "--output", tmp,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            plan_path = Path(tmp) / "environment-plan.json"
            self.assertTrue(plan_path.exists())
            plan = json.loads(plan_path.read_text())
            self.assertEqual(plan["platform"], "arm")


class EnvironmentValidateTest(unittest.TestCase):
    """Test 'environment validate' subcommand."""

    def _generate_plan(self, tmp_dir: Path) -> dict:
        """Helper: generate a plan into tmp_dir, return parsed plan."""
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML),
            "--platform", "arm", "--output", str(tmp_dir),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((tmp_dir / "environment-plan.json").read_text())

    def test_validate_rejects_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = CliInvoker.run("environment", "validate", tmp)

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "error")
        self.assertGreater(report["issueCount"], 0)

    def test_validate_accepts_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = self._generate_plan(tmp_dir)

            record = {
                "schemaVersion": 1,
                "projectId": plan["projectId"],
                "platform": plan["platform"],
                "planHash": plan["planHash"],
                "startedAt": "2026-04-15T10:00:00Z",
                "finishedAt": "2026-04-15T10:12:00Z",
                "mode": "manual-record",
                "provenance": {
                    "recordedBy": "manual",
                    "operatorRef": "test-operator",
                    "source": "test",
                },
                "facts": {"arch": "aarch64", "kernel": "6.6.0"},
                "steps": [
                    {"id": s["id"], "status": "passed"}
                    for s in plan["steps"]
                    if not s["mutatesHost"]
                ] + [
                    {"id": s["id"], "status": "passed", "logPath": f"logs/{s['id']}.log"}
                    for s in plan["steps"]
                    if s["mutatesHost"]
                ],
            }
            (tmp_dir / "environment-record.json").write_text(
                json.dumps(record, indent=2)
            )

            readiness = {
                "schemaVersion": 1,
                "projectId": plan["projectId"],
                "platform": plan["platform"],
                "status": "ready",
                "checks": [
                    {"id": "cluster-health", "status": "passed", "message": "OK"},
                    {"id": "tm-count", "status": "passed", "message": "OK"},
                ],
            }
            (tmp_dir / "readiness-report.json").write_text(
                json.dumps(readiness, indent=2)
            )

            result = CliInvoker.run("environment", "validate", str(tmp_dir))

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["issueCount"], 0)

    def test_validate_accepts_framework_fingerprint_plan_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = self._generate_plan(tmp_dir)
            plan["steps"].append(
                {
                    "id": "record-environment-fingerprint",
                    "kind": "framework-fingerprint",
                    "hostRef": "arm-host",
                    "command": "true",
                    "required": True,
                    "mutatesHost": True,
                    "requiresPrivilege": False,
                    "requiresApproval": False,
                    "rollbackHint": "No rollback required.",
                }
            )
            (tmp_dir / "environment-plan.json").write_text(
                json.dumps(plan, indent=2), encoding="utf-8"
            )
            record = {
                "schemaVersion": 1,
                "projectId": plan["projectId"],
                "platform": plan["platform"],
                "planHash": plan["planHash"],
                "startedAt": "2026-04-15T10:00:00Z",
                "finishedAt": "2026-04-15T10:12:00Z",
                "mode": "full-auto",
                "provenance": {"recordedBy": "auto"},
                "steps": [
                    {
                        "id": step["id"],
                        "status": "passed",
                        **({"note": "executed"} if step["mutatesHost"] else {}),
                    }
                    for step in plan["steps"]
                ],
            }
            (tmp_dir / "environment-record.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
            readiness = {
                "schemaVersion": 1,
                "projectId": plan["projectId"],
                "platform": plan["platform"],
                "status": "ready",
                "checks": [
                    {"id": "readiness", "status": "passed", "message": "OK"}
                ],
            }
            (tmp_dir / "readiness-report.json").write_text(
                json.dumps(readiness, indent=2), encoding="utf-8"
            )

            result = CliInvoker.run("environment", "validate", str(tmp_dir))

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_validate_detects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._generate_plan(tmp_dir)

            record = {
                "schemaVersion": 1,
                "projectId": "pyflink-tpch-reference",
                "platform": "arm",
                "planHash": "sha256:WRONG",
                "startedAt": "2026-04-15T10:00:00Z",
                "finishedAt": "2026-04-15T10:12:00Z",
                "mode": "manual-record",
                "provenance": {"recordedBy": "manual"},
                "steps": [],
            }
            (tmp_dir / "environment-record.json").write_text(
                json.dumps(record, indent=2)
            )

            result = CliInvoker.run("environment", "validate", str(tmp_dir))

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        messages = " ".join(i["message"] for i in report["issues"])
        self.assertIn("does not match", messages)

    def test_validate_detects_unknown_step_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = self._generate_plan(tmp_dir)

            record = {
                "schemaVersion": 1,
                "projectId": plan["projectId"],
                "platform": plan["platform"],
                "planHash": plan["planHash"],
                "startedAt": "2026-04-15T10:00:00Z",
                "finishedAt": "2026-04-15T10:12:00Z",
                "mode": "manual-record",
                "provenance": {"recordedBy": "manual"},
                "steps": [
                    {"id": "nonexistent-step", "status": "passed"},
                ],
            }
            (tmp_dir / "environment-record.json").write_text(
                json.dumps(record, indent=2)
            )

            result = CliInvoker.run("environment", "validate", str(tmp_dir))

        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        messages = " ".join(i["message"] for i in report["issues"])
        self.assertIn("not found in plan", messages)


class EnvironmentDeployRecordTest(unittest.TestCase):
    """Test deploy outputs validate-ready record files."""

    def test_deploy_retries_read_only_step_after_ssh_transport_failure(self) -> None:
        from pyframework_pipeline.environment.deploy import deploy_plan

        class FlakyExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(
                        returncode=255,
                        stdout="",
                        stderr="Timeout, server test-host not responding.\n",
                    )
                return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

            def push_file(self, *_args, **_kwargs):
                return True

        executor = FlakyExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = {
                "schemaVersion": 1,
                "projectId": "volc-test",
                "platform": "arm",
                "planHash": "sha256:test",
                "steps": [
                    {
                        "id": "probe-os",
                        "kind": "host-probe",
                        "hostRef": "arm-host",
                        "command": "uname -a",
                        "description": "Probe host OS",
                        "mutatesHost": False,
                    }
                ],
            }
            plan_path = tmp_dir / "environment-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=executor,
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path,
                    output_dir=tmp_dir,
                    yes=True,
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(executor.calls, 2)

    def test_deploy_does_not_retry_mutating_step_after_ssh_transport_failure(self) -> None:
        from pyframework_pipeline.environment.deploy import deploy_plan

        class FailingExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, *_args, **_kwargs):
                self.calls += 1
                return SimpleNamespace(
                    returncode=255,
                    stdout="",
                    stderr="Timeout, server test-host not responding.\n",
                )

            def push_file(self, *_args, **_kwargs):
                return True

        executor = FailingExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = {
                "schemaVersion": 1,
                "projectId": "volc-test",
                "platform": "arm",
                "planHash": "sha256:test",
                "steps": [
                    {
                        "id": "start-container",
                        "kind": "framework-start",
                        "hostRef": "arm-host",
                        "command": "docker run test-image",
                        "description": "Start container",
                        "mutatesHost": True,
                    }
                ],
            }
            plan_path = tmp_dir / "environment-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=executor,
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path,
                    output_dir=tmp_dir,
                    yes=True,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failedStep"]["exitCode"], 255)
        self.assertEqual(executor.calls, 1)

    def test_deploy_generates_plan_with_current_adapter_loader(self) -> None:
        from pyframework_pipeline.environment.deploy import deploy_plan

        class FakeExecutor:
            def run(self, *_args, **_kwargs):
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def push_file(self, *_args, **_kwargs):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=FakeExecutor(),
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path=None,
                    output_dir=tmp_dir,
                    yes=True,
            )

            self.assertEqual(result["status"], "completed")
            self.assertTrue((tmp_dir / "deploy-record.json").exists())

    def test_deploy_writes_environment_record_and_readiness_report(self) -> None:
        from pyframework_pipeline.environment.deploy import deploy_plan

        class FakeExecutor:
            def run(self, *_args, **_kwargs):
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def push_file(self, *_args, **_kwargs):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = {
                "schemaVersion": 1,
                "projectId": "pyflink-tpch-reference",
                "platform": "arm",
                "planHash": "sha256:test",
                "steps": [
                    {
                        "id": "start-jobmanager",
                        "kind": "framework-start",
                        "hostRef": "arm-host",
                        "command": "true",
                        "description": "Start JobManager",
                        "mutatesHost": True,
                    },
                    {
                        "id": "readiness-cluster-health",
                        "kind": "framework-readiness",
                        "hostRef": "arm-host",
                        "command": "true",
                        "description": "Cluster health",
                    },
                ],
            }
            plan_path = tmp_dir / "environment-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=FakeExecutor(),
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path,
                    output_dir=tmp_dir,
                    yes=True,
                )

            self.assertEqual(result["status"], "completed")
            self.assertTrue((tmp_dir / "deploy-record.json").exists())
            self.assertTrue((tmp_dir / "environment-record.json").exists())
            self.assertTrue((tmp_dir / "readiness-report.json").exists())
            record = json.loads((tmp_dir / "environment-record.json").read_text())
            readiness = json.loads((tmp_dir / "readiness-report.json").read_text())

        mutating = next(s for s in record["steps"] if s["id"] == "start-jobmanager")
        self.assertIn("note", mutating)
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["checks"][0]["id"], "readiness-cluster-health")

    def test_deploy_captures_structured_environment_fingerprint(self) -> None:
        import base64
        from pyframework_pipeline.environment.deploy import deploy_plan

        fingerprint = {
            "sourceRevision": "a" * 40,
            "imageId": "sha256:image",
            "containerId": "container-id",
            "condaFingerprints": {"xarch": "x", "xdj": "y"},
            "perf": {"cyclesAvailable": True, "callGraphAvailable": True},
        }
        marker = "PYFRAMEWORK_ENV_FINGERPRINT=" + base64.b64encode(
            json.dumps(fingerprint).encode("utf-8")
        ).decode("ascii")

        class FakeExecutor:
            def run(self, *_args, **_kwargs):
                return SimpleNamespace(returncode=0, stdout=marker + "\n", stderr="")

            def push_file(self, *_args, **_kwargs):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = {
                "schemaVersion": 1,
                "projectId": "volc-test",
                "platform": "arm",
                "planHash": "sha256:test",
                "steps": [
                    {
                        "id": "record-volc-environment",
                        "kind": "framework-fingerprint",
                        "hostRef": "arm-host",
                        "command": "true",
                        "captureOutput": True,
                    }
                ],
            }
            plan_path = tmp_dir / "environment-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=FakeExecutor(),
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path,
                    output_dir=tmp_dir,
                    yes=True,
                )
            record = json.loads(
                (tmp_dir / "environment-record.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(record["environmentFingerprint"], fingerprint)
        self.assertEqual(record["sourceRevision"], "a" * 40)
        self.assertEqual(record["imageId"], "sha256:image")
        self.assertIn("stdout", record["steps"][0])

    def test_deploy_rejects_missing_required_fingerprint_marker(self) -> None:
        from pyframework_pipeline.environment.deploy import deploy_plan

        class FakeExecutor:
            def run(self, *_args, **_kwargs):
                return SimpleNamespace(returncode=0, stdout="not-a-marker\n", stderr="")

            def push_file(self, *_args, **_kwargs):
                return True

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan = {
                "schemaVersion": 1,
                "projectId": "volc-test",
                "platform": "arm",
                "planHash": "sha256:test",
                "steps": [
                    {
                        "id": "record-volc-environment",
                        "kind": "framework-fingerprint",
                        "hostRef": "arm-host",
                        "command": "true",
                        "captureOutput": True,
                    }
                ],
            }
            plan_path = tmp_dir / "environment-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with patch(
                "pyframework_pipeline.environment.deploy.build_executor",
                return_value=FakeExecutor(),
            ):
                result = deploy_plan(
                    PROJECT_YAML,
                    "arm",
                    plan_path,
                    output_dir=tmp_dir,
                    yes=True,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failedStep"]["id"], "record-volc-environment")
        self.assertIn("fingerprint marker", result["failedStep"]["stderr"])


class EnvironmentPreflightTest(unittest.TestCase):
    """Test read-only remote environment preflight reports."""

    def test_preflight_records_commands_and_overall_status(self) -> None:
        from collections import namedtuple
        from pyframework_pipeline.environment.preflight import run_preflight

        Result = namedtuple("Result", "returncode stdout stderr")

        class FakeExecutor:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def run(self, command: str, timeout: int = 30):
                self.commands.append(command)
                if "docker info" in command:
                    return Result(1, "Docker version 18.09.0", "daemon unavailable")
                return Result(0, "ok", "")

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            report = run_preflight(
                PROJECT_YAML,
                "arm",
                output_dir=output,
                executor_factory=lambda _host_ref, _env: FakeExecutor(),
            )

            self.assertEqual(report["status"], "error")
            self.assertEqual(report["platform"], "arm")
            self.assertEqual(report["hostRef"], "arm-host")
            self.assertGreaterEqual(len(report["checks"]), 4)
            self.assertTrue(
                all(check["mutatesHost"] is False for check in report["checks"])
            )
            docker_check = next(c for c in report["checks"] if c["id"] == "docker")
            self.assertEqual(docker_check["exitCode"], 1)
            self.assertIn("daemon unavailable", docker_check["stderr"])

            saved = json.loads((output / "preflight-report.json").read_text())
            self.assertEqual(saved["status"], "error")

    def test_preflight_warns_when_perf_or_target_image_not_ready(self) -> None:
        from collections import namedtuple
        from pyframework_pipeline.environment.preflight import run_preflight

        Result = namedtuple("Result", "returncode stdout stderr")

        class FakeExecutor:
            def run(self, command: str, timeout: int = 30):
                if "perf_event_paranoid" in command:
                    return Result(0, "kernel.perf_event_paranoid = 2", "")
                if "docker images" in command:
                    return Result(0, "cinderx-pyperf-realenv:arm64 3.83GB", "")
                return Result(0, "ok", "")

        report = run_preflight(
            PROJECT_YAML,
            "arm",
            executor_factory=lambda _host_ref, _env: FakeExecutor(),
        )

        self.assertEqual(report["status"], "warning")
        messages = "\n".join(report["warnings"])
        self.assertIn("kernel.perf_event_paranoid", messages)
        self.assertIn("flink-pyflink:2.2.0-py314-arm-final", messages)

    def test_preflight_warns_when_resources_are_tight(self) -> None:
        from collections import namedtuple
        from pyframework_pipeline.environment.preflight import run_preflight

        Result = namedtuple("Result", "returncode stdout stderr")

        class FakeExecutor:
            def run(self, command: str, timeout: int = 30):
                if "df -Pk" in command:
                    return Result(
                        0,
                        "\n".join([
                            "Filesystem 1024-blocks Used Available Capacity Mounted on",
                            "/dev/vda1 10485760 8388608 2097152 80% /",
                            "Mem: 2048 1024 1024 0 0 1024",
                        ]),
                        "",
                    )
                if "docker images" in command:
                    return Result(0, "flink-pyflink:2.2.0-py314-arm-final 4GB", "")
                if "perf_event_paranoid" in command:
                    return Result(0, "kernel.perf_event_paranoid = 0", "")
                return Result(0, "ok", "")

        report = run_preflight(
            PROJECT_YAML,
            "arm",
            executor_factory=lambda _host_ref, _env: FakeExecutor(),
        )

        self.assertEqual(report["status"], "warning")
        messages = "\n".join(report["warnings"])
        self.assertIn("disk", messages.lower())
        self.assertIn("memory", messages.lower())

    def test_resource_parser_ignores_free_output_for_disk(self) -> None:
        from pyframework_pipeline.environment.preflight import (
            _parse_min_available_disk_kb,
        )

        stdout = "\n".join([
            "Filesystem     1024-blocks      Used  Available Capacity Mounted on",
            "/dev/nvme3n1p4  3885910168 107847072 3778063096       3% /",
            "               total        used        free      shared  buff/cache   available",
            "Mem:          515697        6397      480470         146       32237      509300",
            "Swap:          16383           0       16383",
        ])

        self.assertEqual(_parse_min_available_disk_kb(stdout), 3778063096)

    def test_preflight_stops_after_host_connectivity_failure(self) -> None:
        from collections import namedtuple
        from pyframework_pipeline.environment.preflight import run_preflight

        Result = namedtuple("Result", "returncode stdout stderr")

        class FakeExecutor:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, command: str, timeout: int = 30):
                self.calls += 1
                return Result(255, "", "Connection closed by UNKNOWN port 65535")

        executor = FakeExecutor()
        report = run_preflight(
            PROJECT_YAML,
            "x86",
            executor_factory=lambda _host_ref, _env: executor,
        )

        self.assertEqual(report["status"], "error")
        self.assertEqual(executor.calls, 1)
        self.assertEqual(report["checks"][0]["id"], "host")
        skipped = [c for c in report["checks"] if c.get("skipped")]
        self.assertGreaterEqual(len(skipped), 1)
        self.assertTrue(all(c["exitCode"] is None for c in skipped))

    def test_preflight_allows_check_timeout_override(self) -> None:
        from collections import namedtuple
        from pyframework_pipeline.environment.preflight import run_preflight

        Result = namedtuple("Result", "returncode stdout stderr")

        class FakeExecutor:
            def __init__(self) -> None:
                self.timeouts: list[int] = []

            def run(self, command: str, timeout: int = 30):
                self.timeouts.append(timeout)
                if "perf_event_paranoid" in command:
                    return Result(0, "kernel.perf_event_paranoid = 0", "")
                if "docker images" in command:
                    return Result(0, "flink-pyflink:2.2.0-py314-x86-final 4GB", "")
                return Result(0, "ok", "")

        executor = FakeExecutor()
        report = run_preflight(
            PROJECT_YAML,
            "x86",
            executor_factory=lambda _host_ref, _env: executor,
            check_timeout=120,
        )

        self.assertEqual(report["status"], "ok")
        self.assertTrue(executor.timeouts)
        self.assertTrue(all(timeout == 120 for timeout in executor.timeouts))
        self.assertTrue(all(c["timeout"] == 120 for c in report["checks"]))


class YamlParserTest(unittest.TestCase):
    """Test the environment YAML parser."""

    def test_parses_full_environment_yaml(self) -> None:
        from pyframework_pipeline.environment.parser import load_environment_yaml

        env = load_environment_yaml(
            REPO_ROOT / "projects" / "pyflink-tpch-reference" / "environment.yaml"
        )

        self.assertEqual(env["schemaVersion"], 1)
        self.assertEqual(env["framework"], "pyflink")
        self.assertEqual(env["mode"], "plan-only")
        self.assertEqual(len(env["platforms"]), 2)
        self.assertEqual(env["platforms"][0]["id"], "arm")
        self.assertEqual(env["platforms"][0]["arch"], "aarch64")
        self.assertEqual(len(env["platforms"][0]["hosts"]), 3)
        self.assertEqual(env["software"]["flinkImage"], "flink:2.2.0-java17")
        self.assertTrue(env["software"]["dockerRequired"])
        self.assertIn("arm-host", env["hostRefs"])

    def test_parses_capabilities(self) -> None:
        from pyframework_pipeline.environment.parser import load_environment_yaml

        env = load_environment_yaml(
            REPO_ROOT / "projects" / "pyflink-tpch-reference" / "environment.yaml"
        )

        caps = env["hostRefs"]["arm-host"]["capabilities"]
        self.assertTrue(caps["ssh"])
        self.assertTrue(caps["sudo"])
        self.assertTrue(caps["docker"])


class SshExecutorEnvTest(unittest.TestCase):
    """Test SshExecutor env var injection."""

    def test_env_injected_into_command(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(
            host="myhost",
            env={"http_proxy": "http://proxy:3128", "https_proxy": "http://proxy:3128"},
        )
        args = executor._build_ssh_args("docker pull busybox")

        remote_cmd = args[-1]  # last arg is the bash -lc ... part
        self.assertIn("export http_proxy=", remote_cmd)
        self.assertIn("export https_proxy=", remote_cmd)
        self.assertIn("docker pull busybox", remote_cmd)

    def test_no_env_means_no_export(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        args = executor._build_ssh_args("docker pull busybox")

        remote_cmd = args[-1]
        self.assertNotIn("export", remote_cmd)

    def test_remote_login_shell_preserves_inner_variable_expansion(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        remote_cmd = executor._build_ssh_args(
            'value=kept; [ "$value" = kept ]'
        )[-1]

        result = subprocess.run(
            ["bash", "-c", remote_cmd], text=True, capture_output=True, check=False
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_env_values_are_shell_escaped(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(
            host="myhost",
            env={"proxy": "http://user:pass@host:3128"},
        )
        args = executor._build_ssh_args("echo hi")

        remote_cmd = args[-1]
        # The @ and : in the value should be properly quoted
        self.assertIn("proxy=", remote_cmd)

    def test_uses_explicit_ssh_config_for_ssh_and_scp(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        ssh_config = Path("/tmp/test-ssh-config")
        executor = SshExecutor(host="myhost", ssh_config=ssh_config)

        ssh_args = executor._build_ssh_args("echo hi")
        scp_args = ["scp", *executor._scp_options()]

        self.assertIn("-F", ssh_args)
        self.assertIn(str(ssh_config), ssh_args)
        self.assertIn("-F", scp_args)
        self.assertIn(str(ssh_config), scp_args)
        for option in (
            "ConnectTimeout=15",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=4",
            "ControlMaster=auto",
            "ControlPersist=600",
        ):
            self.assertIn(option, ssh_args)
            self.assertIn(option, scp_args)
        self.assertTrue(any(option.startswith("ControlPath=/tmp/pyframework-ssh-") for option in ssh_args))
        self.assertTrue(any(option.startswith("ControlPath=/tmp/pyframework-ssh-") for option in scp_args))

    def test_fetch_file_prefers_sftp_scp_with_timeout(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "perf.data"

            def fake_run(args, **_kwargs):
                Path(args[-1]).write_text("payload", encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=["scp"],
                    returncode=0,
                    stdout="",
                    stderr="",
                )

            with patch(
                "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
                side_effect=fake_run,
            ) as run:
                ok = executor.fetch_file("/tmp/perf.data", local_path)

            self.assertEqual(local_path.read_text(encoding="utf-8"), "payload")
            self.assertEqual(list(Path(tmp).glob("*.tmp-*")), [])

        self.assertTrue(ok)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "scp")
        self.assertNotIn("-O", args)
        self.assertIn("myhost:/tmp/perf.data", args)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_fetch_file_falls_back_to_ssh_cat(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        failures = [
            subprocess.CompletedProcess(args=["scp"], returncode=1, stdout="", stderr="sftp failed"),
            subprocess.CompletedProcess(args=["scp"], returncode=1, stdout="", stderr="scp failed"),
            subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=b""),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "perf.data"
            with patch(
                "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
                side_effect=failures,
            ) as run:
                ok = executor.fetch_file("/tmp/perf.data", local_path)

            self.assertTrue(ok)
            self.assertTrue(local_path.exists())

        self.assertEqual(run.call_count, 3)
        scp_sftp_args = run.call_args_list[0].args[0]
        scp_legacy_args = run.call_args_list[1].args[0]
        ssh_args = run.call_args_list[2].args[0]
        self.assertNotIn("-O", scp_sftp_args)
        self.assertIn("-O", scp_legacy_args)
        self.assertEqual(ssh_args[0], "ssh")
        self.assertIn("cat /tmp/perf.data", ssh_args[-1])
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 300)
        self.assertEqual(run.call_args_list[2].kwargs["timeout"], 300)

    def test_fetch_file_failure_keeps_existing_local_file(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        failures = [
            subprocess.CompletedProcess(args=["scp"], returncode=1, stdout="", stderr="sftp failed"),
            subprocess.CompletedProcess(args=["scp"], returncode=1, stdout="", stderr="scp failed"),
            subprocess.CompletedProcess(args=["ssh"], returncode=1, stdout="", stderr=b"cat failed"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "perf.data"
            local_path.write_text("old-good-data", encoding="utf-8")
            with patch(
                "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
                side_effect=failures,
            ):
                ok = executor.fetch_file("/tmp/perf.data", local_path)

            self.assertFalse(ok)
            self.assertEqual(local_path.read_text(encoding="utf-8"), "old-good-data")
            self.assertEqual(list(Path(tmp).glob("*.tmp-*")), [])

    def test_push_file_prefers_legacy_scp_for_unstable_sftp_hosts(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        with tempfile.TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "script.py"
            local_path.write_text("print('ok')\n", encoding="utf-8")
            with patch(
                "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["scp"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ) as run:
                ok = executor.push_file(local_path, "/tmp/script.py")

        self.assertTrue(ok)
        args = run.call_args.args[0]
        self.assertEqual(args[0], "scp")
        self.assertIn("-O", args)
        self.assertIn(str(local_path), args)
        self.assertIn("myhost:/tmp/script.py", args)
        self.assertEqual(run.call_args.kwargs["timeout"], 300)

    def test_fetch_dir_prefers_ssh_tar_stream(self) -> None:
        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")
        calls: list[tuple[list[str], dict]] = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "ssh":
                kwargs["stdout"].write(b"tar-payload")
                return subprocess.CompletedProcess(args, 0, b"", b"")
            if args[0] == "tar":
                destination = Path(args[args.index("-C") + 1])
                (destination / "summary.json").write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, b"", b"")
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "results"
            with patch(
                "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
                side_effect=fake_run,
            ):
                ok = executor.fetch_dir(
                    "/remote/results", local_dir, timeout=901
                )

            self.assertTrue(ok)
            self.assertTrue((local_dir / "summary.json").is_file())
            self.assertEqual(list(Path(tmp).glob(".*.remote-*.tar")), [])

        self.assertEqual(calls[0][0][0], "ssh")
        self.assertIn("tar -C /remote/results -cf - .", calls[0][0][-1])
        self.assertEqual(calls[0][1]["timeout"], 901)
        self.assertEqual(calls[1][0][0], "tar")
        self.assertEqual(calls[1][1]["timeout"], 901)
        self.assertNotIn("scp", [call[0][0] for call in calls])

    def test_run_timeout_returns_completed_process(self) -> None:
        import subprocess
        from unittest.mock import patch

        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        executor = SshExecutor(host="myhost")

        def raise_timeout(*_args, **_kwargs):
            raise subprocess.TimeoutExpired(
                cmd=["ssh", "myhost"],
                timeout=7,
                output="partial stdout",
                stderr="partial stderr",
            )

        with patch(
            "pyframework_pipeline.acquisition.ssh_executor.subprocess.run",
            side_effect=raise_timeout,
        ):
            result = executor.run("echo hi", timeout=7)

        self.assertEqual(result.returncode, 124)
        self.assertIn("partial stdout", result.stdout)
        self.assertIn("partial stderr", result.stderr)
        self.assertIn("TIMEOUT after 7s", result.stderr)

    def test_streaming_uses_replacement_decoding(self) -> None:
        from unittest.mock import patch

        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = iter(["ok\n"])
                self.returncode = 0

            def wait(self, timeout: int) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        executor = SshExecutor(host="myhost")
        fake = FakeProcess()
        with patch(
            "pyframework_pipeline.acquisition.ssh_executor.subprocess.Popen",
            return_value=fake,
        ) as popen:
            result = executor.run("echo hi", stream=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(popen.call_args.kwargs["errors"], "replace")

    def test_streaming_timeout_interrupts_silent_stdout_reader(self) -> None:
        from unittest.mock import patch

        from pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        class SilentStdout:
            def __init__(self, killed: threading.Event) -> None:
                self.killed = killed

            def __iter__(self):
                return self

            def __next__(self) -> str:
                self.killed.wait(0.2)
                raise StopIteration

        class FakeProcess:
            def __init__(self) -> None:
                self.killed = threading.Event()
                self.stdout = SilentStdout(self.killed)
                self.returncode = None

            def wait(self, timeout: int | None = None) -> None:
                if not self.killed.is_set():
                    raise subprocess.TimeoutExpired(cmd=["ssh"], timeout=timeout)
                self.returncode = -9

            def kill(self) -> None:
                self.killed.set()
                self.returncode = -9

        executor = SshExecutor(host="myhost")
        fake = FakeProcess()
        started = time.monotonic()
        with patch(
            "pyframework_pipeline.acquisition.ssh_executor.subprocess.Popen",
            return_value=fake,
        ):
            result = executor.run("sleep forever", timeout=1, stream=True)

        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(result.returncode, 124)
        self.assertIn("TIMEOUT after 1s", result.stdout)
        self.assertTrue(fake.killed.is_set())


class DockerRegistryTest(unittest.TestCase):
    """Test dockerRegistry prefix in environment plans."""

    def test_registry_prefix_in_pull_and_run(self) -> None:
        from pyframework_pipeline.adapters.pyflink.environment import PyFlinkEnvironmentAdapter

        adapter = PyFlinkEnvironmentAdapter()
        steps = adapter.get_plan_steps(
            platform="arm",
            platform_config={
                "hosts": [
                    {"role": "client", "hostRef": "test-host"},
                    {"role": "jobmanager", "hostRef": "test-host"},
                    {"role": "taskmanager", "hostRef": "test-host"},
                ],
            },
            software={
                "flinkPyflinkImages": {"arm": "my-flink:latest"},
                "containerNetwork": "test-net",
                "dockerRegistry": "registry.internal",
            },
            host_refs={"test-host": {"alias": "1.2.3.4"}},
        )
        pull_step = next(s for s in steps if s.id == "build-flink-image")
        start_step = next(s for s in steps if s.id == "start-jobmanager")

        self.assertIn("registry.internal/my-flink:latest", pull_step.command)
        self.assertIn("registry.internal/my-flink:latest", start_step.command)

    def test_no_registry_means_no_prefix(self) -> None:
        from pyframework_pipeline.adapters.pyflink.environment import PyFlinkEnvironmentAdapter

        adapter = PyFlinkEnvironmentAdapter()
        steps = adapter.get_plan_steps(
            platform="arm",
            platform_config={
                "hosts": [
                    {"role": "jobmanager", "hostRef": "test-host"},
                ],
            },
            software={
                "flinkPyflinkImages": {"arm": "my-flink:latest"},
                "containerNetwork": "test-net",
            },
            host_refs={"test-host": {"alias": "1.2.3.4"}},
        )
        build_step = next(s for s in steps if s.id == "build-flink-image")

        self.assertIn("my-flink:latest", build_step.command)
        self.assertNotIn("registry.internal", build_step.command)

    def test_pip_mirror_config_in_build_command(self) -> None:
        from pyframework_pipeline.adapters.pyflink.environment import PyFlinkEnvironmentAdapter

        adapter = PyFlinkEnvironmentAdapter()
        steps = adapter.get_plan_steps(
            platform="arm",
            platform_config={
                "hosts": [
                    {"role": "jobmanager", "hostRef": "test-host"},
                ],
            },
            software={
                "flinkPyflinkImages": {"arm": "my-flink:latest"},
                "containerNetwork": "test-net",
                "pipIndexUrl": "https://pypi.tuna.tsinghua.edu.cn/simple",
                "pipTrustedHosts": ["pypi.tuna.tsinghua.edu.cn"],
                "pipTimeout": 180,
                "pipRetries": 12,
            },
            host_refs={"test-host": {"alias": "1.2.3.4"}},
        )
        build_step = next(s for s in steps if s.id == "build-flink-image")

        self.assertIn(
            "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple",
            build_step.command,
        )
        self.assertIn("PIP_TRUSTED_HOSTS=pypi.tuna.tsinghua.edu.cn", build_step.command)
        self.assertIn("PIP_TIMEOUT=180", build_step.command)
        self.assertIn("PIP_RETRIES=12", build_step.command)

    def test_build_script_supports_pip_mirror_env(self) -> None:
        script = Path(
            "pipelines/pyframework_pipeline/adapters/pyflink/scripts/build-flink-image.sh"
        ).read_text()

        self.assertIn("PIP_INDEX_URL=", script)
        self.assertIn("--index-url $PIP_INDEX_URL", script)
        self.assertIn("$PIP install $PIP_INSTALL_ARGS", script)
        self.assertIn("PIP_TRUSTED_HOSTS", script)

    def test_build_script_can_reuse_existing_pyenv(self) -> None:
        script = Path(
            "pipelines/pyframework_pipeline/adapters/pyflink/scripts/build-flink-image.sh"
        ).read_text()

        self.assertIn("pyenv already installed", script)
        self.assertIn("Python already installed", script)

    def test_build_script_prefers_pyarrow_wheel(self) -> None:
        script = Path(
            "pipelines/pyframework_pipeline/adapters/pyflink/scripts/build-flink-image.sh"
        ).read_text()

        self.assertIn("--only-binary=:all: pyarrow==23.0.1", script)
        self.assertIn("pyarrow wheel unavailable; falling back to source build", script)


if __name__ == "__main__":
    unittest.main()
