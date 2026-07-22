"""Cross-platform comparison gates for Volc operator evidence."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pyframework_pipeline.adapters.volcoperatorsim.operator_compare import (
    compare_operator_platforms,
)
from pyframework_pipeline.contracts.operator import (
    OperatorDataset,
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
)


class VolcOperatorCompareTest(unittest.TestCase):
    def test_orchestrator_uses_volc_normalized_backfill_and_compare_paths(self) -> None:
        from pyframework_pipeline.orchestrator import _run_backfill, _run_compare

        project = (
            Path(__file__).resolve().parents[2]
            / "projects"
            / "volc-operator-sim-reference"
            / "project.yaml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            _write_platform(run_dir / "arm", "arm", 2_000_000_000, "sha256:same", False)
            _write_platform(run_dir / "x86", "x86", 1_000_000_000, "sha256:same", False)
            with patch(
                "pyframework_pipeline.backfill.pipeline.run_backfill",
                side_effect=AssertionError("generic backfill must not run"),
            ):
                _run_backfill(project, run_dir)
            with patch(
                "pyframework_pipeline.compare.pipeline.run_compare",
                side_effect=AssertionError("generic compare must not run"),
            ):
                _run_compare(project, run_dir)

            self.assertTrue((run_dir / "compare" / "operator-compare.md").is_file())

    def test_smoke_emits_diagnostic_ratio_but_not_formal_speedup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm = root / "arm"
            x86 = root / "x86"
            _write_platform(arm, "arm", 2_000_000_000, "sha256:same", False)
            _write_platform(x86, "x86", 1_000_000_000, "sha256:same", False)

            model = compare_operator_platforms(arm, x86, root / "compare")
            operator = model["operators"][0]
            report = (root / "compare" / "operator-compare.md").read_text(
                encoding="utf-8"
            )

        self.assertTrue(operator["inputParity"])
        self.assertEqual(operator["diagnosticX86OverArm"], 0.5)
        self.assertIsNone(operator["formalSpeedup"])
        self.assertIn("diagnostic_only", operator["comparisonFlags"])
        self.assertIn("Pipeline E2E", report)
        self.assertIn("Pipeline context operator timing", report)
        self.assertIn("Isolated operator E2E", report)
        self.assertIn("Perf CPU-time distribution", report)
        self.assertIn("Top perf symbols", report)
        self.assertEqual(
            operator["armPerfTopSymbols"][0]["symbol"],
            "PyEval_EvalFrameDefault",
        )
        self.assertIn("Perf / ASM / flamegraph evidence", report)
        self.assertIn("perf-annotate.txt", report)
        self.assertIn("cpu.svg", report)
        self.assertIn("No formal speedup", report)

    def test_formal_speedup_requires_input_parity_and_both_quality_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm = root / "arm"
            x86 = root / "x86"
            _write_platform(arm, "arm", 2_000_000_000, "sha256:arm", True)
            _write_platform(x86, "x86", 1_000_000_000, "sha256:x86", True)

            model = compare_operator_platforms(arm, x86, root / "compare")
            operator = model["operators"][0]

        self.assertFalse(operator["inputParity"])
        self.assertIsNone(operator["diagnosticX86OverArm"])
        self.assertIsNone(operator["formalSpeedup"])
        self.assertIn("input_fingerprint_mismatch", operator["comparisonFlags"])

    def test_reports_cross_engine_hardware_and_quality_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm = root / "arm"
            x86 = root / "x86"
            _write_platform(arm, "arm", 2_000_000_000, "sha256:same", False)
            _write_platform(x86, "x86", 1_000_000_000, "sha256:same", False)
            _append_engine(arm, "datajuicer_native", 3_000_000_000)
            _append_engine(x86, "datajuicer_native", 2_000_000_000)

            model = compare_operator_platforms(arm, x86, root / "compare")
            report = (root / "compare/operator-compare.md").read_text(
                encoding="utf-8"
            )

        by_platform = {
            row["platformId"]: row for row in model["engineComparisons"]
        }
        self.assertEqual(by_platform["arm"]["diagnosticDataJuicerOverDaft"], 1.5)
        self.assertEqual(by_platform["x86"]["diagnosticDataJuicerOverDaft"], 2.0)
        daft = next(row for row in model["operators"] if row["engineId"] == "daft_ray")
        self.assertEqual(daft["armPerfStat"]["ipc"], 2.0)
        self.assertEqual(daft["diagnosticX86OverArmPeakRss"], 1.0)
        self.assertEqual(model["qualitySummary"]["operatorRows"], 2)
        self.assertIn("Daft vs Data-Juicer", report)
        self.assertIn("Hardware counters and resources", report)
        self.assertIn("Quality gate summary", report)

    def test_plan_keeps_case_missing_on_both_platforms_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arm = root / "arm"
            x86 = root / "x86"
            _write_platform(arm, "arm", 2_000_000_000, "sha256:same", False)
            _write_platform(x86, "x86", 1_000_000_000, "sha256:same", False)
            _write_plan_with_missing_case(arm)
            _write_plan_with_missing_case(x86)

            model = compare_operator_platforms(arm, x86, root / "compare")
            missing = next(
                row
                for row in model["operators"]
                if row["operatorId"] == "missing_mapper"
            )

        self.assertIn("platform_record_missing", missing["comparisonFlags"])
        self.assertIsNone(missing["armQuality"])
        self.assertIsNone(missing["x86Quality"])
        self.assertEqual(model["qualitySummary"]["operatorRows"], 2)


def _write_platform(
    directory: Path,
    platform: str,
    median_ns: int,
    fingerprint: str,
    formal: bool,
) -> None:
    case_id = "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
    common = dict(
        run_id="run-1",
        platform_id=platform,
        arch="aarch64" if platform == "arm" else "x86_64",
        pipeline_id="pipeline_text",
        task_spec_id="pipeline_text@v0",
        engine_id="daft_ray",
        operator_case_id=case_id,
        operator_id="clean_html_mapper",
        order=0,
        input_fingerprint=fingerprint,
    )
    records = (
        OperatorRecord(
            **common,
            measurement_scope="pipeline_context",
            timing=OperatorTiming(
                pipeline_context_ns=median_ns // 2,
                timing_source="daft_collect_boundary",
                rounds=1,
            ),
            quality=OperatorQuality(
                grade="A",
                flags=() if formal else ("diagnostic_only",),
                formal_conclusion_allowed=formal,
            ),
        ),
        OperatorRecord(
            **common,
            measurement_scope="operator_case_e2e",
            timing=OperatorTiming(
                runner_elapsed_ns=median_ns,
                median_ns=median_ns,
                p95_ns=median_ns,
                rounds=3,
                timing_source="isolated_operator_timing",
            ),
            resources=OperatorResources(
                tree_cpu_time_ns=median_ns,
                peak_tree_rss_bytes=100 * 1024 * 1024,
                sample_count=6000,
            ),
            perf_stat={
                "cycles": 1000,
                "instructions": 2000,
                "ipc": 2.0,
                "events": {
                    "cycles": {"value": 1000, "status": "ok", "coveragePct": 100.0},
                    "instructions": {"value": 2000, "status": "ok", "coveragePct": 100.0},
                },
                "unsupportedEvents": [],
            },
            quality=OperatorQuality(
                grade="A",
                flags=() if formal else ("diagnostic_only",),
                formal_conclusion_allowed=formal,
            ),
        ),
        OperatorRecord(
            **common,
            measurement_scope="operator_case_perf",
            source_artifacts=(
                "operators/raw/operator_case_perf/ts/arch/engine/perf/perf-annotate.txt",
                "operators/raw/operator_case_perf/ts/arch/engine/flamegraph/cpu.svg",
            ),
            resources=OperatorResources(sample_count=6000),
            quality=OperatorQuality(
                grade="A",
                flags=() if formal else ("diagnostic_only",),
                formal_conclusion_allowed=formal,
            ),
            metadata={
                "event": "cycles",
                "periodSemantics": "sampled_cpu_time_distribution",
                "categoryPeriodShare": {
                    "Python/CPython runtime": 0.6,
                    "operator native libraries": 0.4,
                },
                "topSymbols": [
                    {
                        "symbol": "PyEval_EvalFrameDefault",
                        "sharedObject": "/usr/lib/libpython.so",
                        "period": 6000,
                        "periodShare": 0.6,
                    }
                ],
            },
        ),
    )
    OperatorDataset(records=records).write_jsonl(
        directory / "operators" / "operator-records.jsonl"
    )
    timing = {
        "schemaVersion": 1,
        "platform_id": platform,
        "benchmark": "volc-operator-sim",
        "cases": [
            {
                "caseId": "pipeline_text::daft_ray",
                "label": "pipeline_text::daft_ray",
                "metrics": {
                    "wallClockTime": {"wall_clock_ns": median_ns * 2}
                },
            }
        ],
    }
    timing_path = directory / "timing" / "timing-normalized.json"
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    timing_path.write_text(json.dumps(timing), encoding="utf-8")


def _append_engine(directory: Path, engine: str, median_ns: int) -> None:
    path = directory / "operators/operator-records.jsonl"
    dataset = OperatorDataset.read_jsonl(path)
    template = dataset.records
    appended = []
    for record in template:
        timing = record.timing
        if record.measurement_scope == "pipeline_context":
            timing = replace(timing, pipeline_context_ns=median_ns // 2)
        elif record.measurement_scope == "operator_case_e2e":
            timing = replace(
                timing,
                runner_elapsed_ns=median_ns,
                median_ns=median_ns,
                p95_ns=median_ns,
            )
        appended.append(replace(record, engine_id=engine, timing=timing))
    OperatorDataset(records=template + tuple(appended)).write_jsonl(path)


def _write_plan_with_missing_case(directory: Path) -> None:
    plan = {
        "tasks": [
            {
                "pipelineId": "pipeline_text",
                "taskSpecId": "pipeline_text@v0",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorCaseId": (
                            "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
                        ),
                        "operatorId": "clean_html_mapper",
                        "order": 0,
                        "isolationStatus": "supported",
                    },
                    {
                        "operatorCaseId": (
                            "pipeline_text@v0::001::missing_mapper::44136fa355b3"
                        ),
                        "operatorId": "missing_mapper",
                        "order": 1,
                        "isolationStatus": "supported",
                    },
                ],
            }
        ]
    }
    path = directory / "operators/operator-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
