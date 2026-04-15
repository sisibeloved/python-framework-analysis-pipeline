import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPO_ROOT / "examples" / "four-layer" / "pyflink-reference"


class ValidateCliTest(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pyframework_pipeline", *args],
            cwd=REPO_ROOT,
            env={"PYTHONPATH": str(REPO_ROOT / "pipelines")},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_validate_accepts_current_four_layer_example(self) -> None:
        result = self.run_cli("validate", str(EXAMPLE_ROOT))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["projectId"], "tpch-pyflink-reference")
        self.assertEqual(payload["errorCount"], 0)

    def test_validate_reports_missing_artifact_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir) / "pyflink-reference"
            self.copy_tree(EXAMPLE_ROOT, temp_root)
            source_path = temp_root / "sources" / "pyflink-reference-source.source.json"
            source = json.loads(source_path.read_text())
            source["artifactIndex"] = [
                artifact for artifact in source["artifactIndex"] if artifact["id"] != "asm_arm_func_001"
            ]
            source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2))

            result = self.run_cli("validate", str(temp_root))

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertGreater(payload["errorCount"], 0)
        self.assertIn("asm_arm_func_001", "\n".join(error["message"] for error in payload["errors"]))

    def test_validate_accepts_project_yaml_config(self) -> None:
        project_config = REPO_ROOT / "projects" / "pyflink-tpch-reference" / "project.yaml"

        result = self.run_cli("validate", str(project_config))

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["projectId"], "tpch-pyflink-reference")

    def copy_tree(self, source: Path, destination: Path) -> None:
        for item in source.rglob("*"):
            target = destination / item.relative_to(source)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.read_bytes())


if __name__ == "__main__":
    unittest.main()
