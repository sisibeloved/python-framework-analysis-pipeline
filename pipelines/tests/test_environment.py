"""Tests for environment plan generation and record validation."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_YAML = REPO_ROOT / "projects" / "pyflink-tpch-reference" / "project.yaml"


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

    def test_plan_contains_profiling_tool_steps(self) -> None:
        result = CliInvoker.run(
            "environment", "plan", str(PROJECT_YAML), "--platform", "arm"
        )
        plan = json.loads(result.stdout)
        step_ids = [s["id"] for s in plan["steps"]]

        # Profiling tool installation steps
        self.assertIn("install-profiling-tools-flink-jm", step_ids)
        self.assertIn("install-profiling-tools-flink-tm1", step_ids)
        self.assertIn("install-profiling-tools-flink-tm2", step_ids)
        self.assertIn("verify-profiling-tools", step_ids)
        self.assertIn("enable-perf-paranoid", step_ids)

        # Verify the install step mentions profiling packages
        install_step = next(
            s for s in plan["steps"]
            if s["id"] == "install-profiling-tools-flink-jm"
        )
        self.assertIn("docker exec -u root flink-jm", install_step["command"])
        self.assertIn("linux-tools-.*-generic", install_step["command"])
        self.assertIn("/usr/local/bin/perf", install_step["command"])
        self.assertIn("strace", install_step["command"])
        self.assertIn("binutils", install_step["command"])
        self.assertTrue(install_step["mutatesHost"])
        self.assertTrue(install_step["requiresApproval"])

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


if __name__ == "__main__":
    unittest.main()
