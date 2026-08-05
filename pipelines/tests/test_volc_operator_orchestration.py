"""Step-registry and dispatch tests for resumable Volc operator stages."""

from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pipelines.tests.test_volcoperatorsim_support import _write_volc_project


class VolcOperatorOrchestrationTest(unittest.TestCase):
    def test_operator_steps_are_ordered_before_backfill(self) -> None:
        from pyframework_pipeline.orchestrator import (
            GLOBAL_STEPS,
            PER_PLATFORM_STEPS,
            STEP_DEFS,
        )

        steps = [item["step"] for item in STEP_DEFS]
        self.assertLess(steps.index("5a"), steps.index("5a.1"))
        self.assertLess(steps.index("5a.1"), steps.index("5a.2"))
        self.assertLess(steps.index("5c"), steps.index("5c.1"))
        self.assertLess(steps.index("5c.1"), steps.index("5d"))
        self.assertLess(steps.index("5d"), steps.index("6"))
        self.assertTrue({"5a.1", "5a.2"}.issubset(PER_PLATFORM_STEPS))
        self.assertTrue({"5c.1", "5d"}.issubset(GLOBAL_STEPS))

    def test_operator_substeps_dispatch_to_adapter_capabilities(self) -> None:
        from pyframework_pipeline.orchestrator import _execute_step

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.collect_context_timing"
                ) as context,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.collect_operator_timing"
                ) as isolated,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.collect_operator_profiles"
                ) as profile,
            ):
                _execute_step("5a.1", project, run_dir, "arm")
                _execute_step("5a.2", project, run_dir, "arm")
                _execute_step("5b.2", project, run_dir, "arm")

        context.assert_called_once_with(
            project, run_dir, "arm", force=False
        )
        isolated.assert_called_once_with(
            project, run_dir, "arm", force=False
        )
        profile.assert_called_once_with(
            project, run_dir, "arm", force=False
        )

    def test_5c_builds_manifests_and_5d_normalizes_before_compare(self) -> None:
        from pyframework_pipeline.orchestrator import _execute_step

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            for platform in ("arm", "x86"):
                plan = run_dir / platform / "operators" / "operator-plan.json"
                plan.parent.mkdir(parents=True)
                plan.write_text("{}", encoding="utf-8")
            with patch(
                "pyframework_pipeline.adapters.volcoperatorsim.acquisition_manifest."
                "build_acquisition_manifest"
            ) as build_manifest:
                _execute_step("5c", project, run_dir, None)

            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.normalize_operator_artifacts"
                ) as normalize,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.operator_compare."
                    "compare_operator_platforms"
                ) as compare,
            ):
                _execute_step(
                    "5d", project, run_dir, None, platforms=["arm", "x86"]
                )

        self.assertEqual(build_manifest.call_count, 2)
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(compare.call_count, 1)

    def test_cross_platform_steps_skip_cleanly_for_arm_only_run(self) -> None:
        from pyframework_pipeline.orchestrator import _execute_step

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            plan = run_dir / "arm" / "operators" / "operator-plan.json"
            plan.parent.mkdir(parents=True)
            plan.write_text("{}", encoding="utf-8")

            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.normalize_operator_artifacts"
                ) as normalize,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.operator_compare."
                    "compare_operator_platforms"
                ) as compare,
            ):
                for step in ("5d", "6", "6b"):
                    _execute_step(
                        step,
                        project,
                        run_dir,
                        None,
                        platforms=["arm"],
                    )

            skipped = json.loads(
                (run_dir / "compare" / "operators" / "SKIPPED.json").read_text(
                    encoding="utf-8"
                )
            )

        normalize.assert_not_called()
        compare.assert_not_called()
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["availablePlatforms"], ["arm"])
        self.assertEqual(skipped["requiredPlatforms"], ["arm", "x86"])

    def test_5c_1_normalizes_then_writes_readable_report_for_selected_platform(self) -> None:
        from pyframework_pipeline.orchestrator import _execute_step

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.normalize_operator_artifacts"
                ) as normalize,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.operator_report."
                    "render_operator_reports"
                ) as render,
            ):
                _execute_step(
                    "5c.1", project, run_dir, None, platforms=["arm"]
                )

        normalize.assert_called_once_with(
            project, run_dir, "arm", force=False
        )
        render.assert_called_once_with(run_dir / "arm")

    def test_operator_report_cli_normalizes_then_returns_html_index(self) -> None:
        from pyframework_pipeline.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            report_path = run_dir / "arm/operators/operator-report.html"
            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.normalize_operator_artifacts"
                ) as normalize,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.operator_report."
                    "render_operator_reports",
                    return_value=report_path,
                ) as render,
                patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                exit_code = main(
                    [
                        "operator",
                        "report",
                        str(project),
                        "--platform",
                        "arm",
                        "--run-dir",
                        str(run_dir),
                    ]
                )

        self.assertEqual(exit_code, 0)
        normalize.assert_called_once_with(
            project, run_dir, "arm", force=False
        )
        render.assert_called_once_with(run_dir / "arm")
        self.assertIn(str(report_path), stdout.getvalue())

    def test_operator_run_cli_refreshes_acquisition_manifest(self) -> None:
        from pyframework_pipeline.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim.adapter."
                    "VolcOperatorSimAdapter.collect_context_timing",
                    return_value=run_dir / "arm/operators/raw/pipeline_context",
                ) as context,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim."
                    "acquisition_manifest.build_acquisition_manifest",
                    return_value=run_dir
                    / "arm/operators/acquisition-manifest.json",
                ) as build_manifest,
                patch("sys.stdout", new_callable=StringIO),
            ):
                exit_code = main(
                    [
                        "operator",
                        "run",
                        str(project),
                        "--platform",
                        "arm",
                        "--run-dir",
                        str(run_dir),
                        "--mode",
                        "context",
                    ]
                )

        self.assertEqual(exit_code, 0)
        context.assert_called_once_with(
            project, run_dir, "arm", force=False
        )
        build_manifest.assert_called_once_with(
            run_dir / "arm",
            platform="arm",
        )

    def test_volc_collect_checkpoints_require_stage_complete_not_future_normalized_files(self) -> None:
        from pyframework_pipeline.orchestrator import _run_collect_substep

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            manifests = run_dir / "arm/operators/manifests"
            manifests.mkdir(parents=True)
            (manifests / "pipeline_e2e-COMPLETE.json").write_text(
                "{}", encoding="utf-8"
            )
            (manifests / "operator_case_perf-COMPLETE.json").write_text(
                "{}", encoding="utf-8"
            )

            _run_collect_substep(project, run_dir, "arm", "5b.1")
            _run_collect_substep(project, run_dir, "arm", "5b.2b")
            _run_collect_substep(project, run_dir, "arm", "5b.3")

            self.assertFalse(
                (run_dir / "arm/operators/operator-records.jsonl").exists()
            )


if __name__ == "__main__":
    unittest.main()
