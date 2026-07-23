"""Manifest-first Volc acquisition tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.adapters.volcoperatorsim.acquisition_manifest import (
    build_acquisition_manifest,
    validate_acquisition_manifest,
)


class VolcAcquisitionManifestTest(unittest.TestCase):
    def test_manifest_lists_only_current_supported_scopes_and_unsupported_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            plan_path = platform_dir / "operators/operator-plan.json"
            plan_path.parent.mkdir(parents=True)
            plan_path.write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "runId": "run-1",
                    "platform": "arm",
                    "sourceRevision": "5" * 40,
                    "tasks": [{
                        "pipelineId": "p",
                        "operators": [{
                            "operatorCaseId": "p@v0::000::op::abc",
                            "operatorId": "op",
                            "order": 0,
                            "isolationStatus": "unsupported",
                            "isolationReason": "external state",
                            "engines": [],
                        }],
                    }],
                }),
                encoding="utf-8",
            )
            artifact = platform_dir / "operators/raw/pipeline_context/case/summary.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"returncode":0}', encoding="utf-8")
            stale = platform_dir / "operators/raw/historical/old.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("old", encoding="utf-8")

            manifest_path = build_acquisition_manifest(platform_dir, platform="arm")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["runId"], "run-1")
        self.assertEqual(manifest["sourceRevision"], "5" * 40)
        self.assertEqual(manifest["artifacts"], [{
            "path": "operators/raw/pipeline_context/case/summary.json",
            "scope": "pipeline_context",
            "required": True,
            "size": len('{"returncode":0}'),
            "sha256": hashlib.sha256(b'{"returncode":0}').hexdigest(),
        }])
        self.assertEqual(manifest["unsupportedCases"][0]["reason"], "external state")
        self.assertEqual(manifest["scopes"]["pipeline_context"]["status"], "complete")
        self.assertEqual(manifest["scopes"]["operator_case_perf"]["status"], "missing")

    def test_validation_detects_tampered_or_missing_listed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            plan = platform_dir / "operators/operator-plan.json"
            plan.parent.mkdir(parents=True)
            plan.write_text(json.dumps({
                "runId": "r", "sourceRevision": "5" * 40, "tasks": []
            }), encoding="utf-8")
            artifact = platform_dir / "operators/raw/pipeline_e2e/a.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("before", encoding="utf-8")
            manifest_path = build_acquisition_manifest(platform_dir, platform="arm")
            artifact.write_text("beforz", encoding="utf-8")

            report = validate_acquisition_manifest(platform_dir, manifest_path)

        self.assertFalse(report.valid)
        self.assertIn("sha256_mismatch", report.errors[0])

    def test_complete_manifest_writes_hash_bound_complete_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            plan = platform_dir / "operators/operator-plan.json"
            plan.parent.mkdir(parents=True)
            plan.write_text(json.dumps({
                "runId": "r", "sourceRevision": "5" * 40, "tasks": []
            }), encoding="utf-8")
            for scope in (
                "pipeline_e2e", "pipeline_context", "snapshot_build",
                "operator_case_e2e", "operator_case_perf",
            ):
                artifact = platform_dir / f"operators/raw/{scope}/evidence.json"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("{}", encoding="utf-8")

            manifest_path = build_acquisition_manifest(platform_dir, platform="arm")
            complete_path = platform_dir / "operators/COMPLETE.json"
            complete = json.loads(complete_path.read_text(encoding="utf-8"))
            manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            report = validate_acquisition_manifest(platform_dir, manifest_path)

        self.assertEqual(complete["status"], "complete")
        self.assertEqual(
            complete["manifestSha256"],
            manifest_sha,
        )
        self.assertTrue(report.valid)

    def test_intentionally_skipped_scope_is_a_terminal_manifest_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            plan = platform_dir / "operators/operator-plan.json"
            plan.parent.mkdir(parents=True)
            plan.write_text(json.dumps({
                "runId": "r", "sourceRevision": "5" * 40, "tasks": []
            }), encoding="utf-8")
            for scope in (
                "pipeline_context", "snapshot_build", "operator_case_perf",
            ):
                artifact = platform_dir / f"operators/raw/{scope}/evidence.json"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("{}", encoding="utf-8")
            for scope in ("pipeline_e2e", "operator_case_e2e"):
                marker = platform_dir / f"operators/raw/{scope}/SKIPPED.json"
                marker.parent.mkdir(parents=True)
                marker.write_text(json.dumps({
                    "status": "skipped",
                    "reason": "bounded representative profile",
                }), encoding="utf-8")

            manifest_path = build_acquisition_manifest(platform_dir, platform="arm")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["scopes"]["pipeline_e2e"]["status"], "skipped")
        self.assertEqual(
            manifest["scopes"]["operator_case_e2e"]["status"], "skipped"
        )


if __name__ == "__main__":
    unittest.main()
