"""Contract and pure-normalization tests for operator-level analysis."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.contracts.operator import (
    OperatorDataset,
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
    build_operator_case_id,
)
from pyframework_pipeline.adapters.volcoperatorsim.normalize import (
    attribute_resource_window,
    evaluate_operator_quality,
)


class OperatorContractTest(unittest.TestCase):
    def test_case_id_is_stable_for_canonical_parameter_order(self) -> None:
        left = build_operator_case_id(
            "pipeline_text@v0",
            3,
            "text_length_filter",
            {"max_len": 100, "min_len": 5},
        )
        right = build_operator_case_id(
            "pipeline_text@v0",
            3,
            "text_length_filter",
            {"min_len": 5, "max_len": 100},
        )

        self.assertEqual(left, right)
        self.assertRegex(
            left,
            r"^pipeline_text@v0::003::text_length_filter::[0-9a-f]{12}$",
        )

    def test_case_id_rejects_invalid_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_spec_id"):
            build_operator_case_id("", 0, "op", {})
        with self.assertRaisesRegex(ValueError, "order"):
            build_operator_case_id("task@v0", -1, "op", {})
        with self.assertRaisesRegex(ValueError, "operator_id"):
            build_operator_case_id("task@v0", 0, "", {})

    def test_operator_dataset_roundtrip_preserves_measurement_scope(self) -> None:
        record = OperatorRecord(
            run_id="run-1",
            platform_id="arm",
            arch="aarch64",
            pipeline_id="pipeline_text",
            task_spec_id="pipeline_text@v0",
            engine_id="daft_ray",
            operator_case_id="pipeline_text@v0::000::clean_html_mapper::abc123abc123",
            operator_id="clean_html_mapper",
            order=0,
            measurement_scope="operator_case_e2e",
            input_fingerprint="sha256:input",
            timing=OperatorTiming(
                outer_process_wall_ns=1_200_000_000,
                runner_elapsed_ns=1_000_000_000,
                isolated_operator_ns=700_000_000,
                residual_ns=300_000_000,
                rounds=3,
            ),
            resources=OperatorResources(
                tree_cpu_time_ns=900_000_000,
                peak_tree_rss_bytes=512 * 1024 * 1024,
                mean_cores_busy=0.9,
            ),
            quality=OperatorQuality(
                grade="A",
                flags=(),
                formal_conclusion_allowed=False,
            ),
            source_artifacts=("raw/result.json", "raw/perf-stat.txt"),
        )
        dataset = OperatorDataset(records=(record,))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operator-records.jsonl"
            dataset.write_jsonl(path)
            actual = OperatorDataset.read_jsonl(path)

        self.assertEqual(actual, dataset)


class ResourceWindowAttributionTest(unittest.TestCase):
    def test_samples_are_weighted_by_interval_overlap(self) -> None:
        estimate = attribute_resource_window(
            samples=(
                {"t": 10.2, "tree_cpu_pct": 100.0, "tree_rss_mb": 100.0},
                {"t": 10.4, "tree_cpu_pct": 200.0, "tree_rss_mb": 200.0},
            ),
            timeline_t0_epoch=10.0,
            start_offset_s=0.1,
            end_offset_s=0.3,
            sample_interval_s=0.2,
        )

        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertEqual(estimate.window_cpu_time_ns_estimate, 300_000_000)
        self.assertAlmostEqual(estimate.window_mean_cores_busy_estimate, 1.5)
        self.assertEqual(estimate.window_peak_tree_rss_bytes_estimate, 200 * 1024 * 1024)
        self.assertEqual(estimate.sample_count, 2)

    def test_invalid_or_empty_window_has_no_estimate(self) -> None:
        self.assertIsNone(
            attribute_resource_window(
                samples=(),
                timeline_t0_epoch=10.0,
                start_offset_s=0.3,
                end_offset_s=0.1,
                sample_interval_s=0.2,
            )
        )


class OperatorQualityTest(unittest.TestCase):
    def test_missing_input_parity_evidence_is_not_reported_as_a_failed_check(self) -> None:
        quality = evaluate_operator_quality(
            timing_source="isolated_operator_timing",
            input_parity=None,
            perf_lock_passed=True,
            sample_count=10000,
            min_perf_samples=5000,
            unblock_perf=True,
            representative_profile=True,
        )

        self.assertEqual(quality.grade, "B")
        self.assertIn("input_parity_unverified", quality.flags)
        self.assertNotIn("input_parity_failed", quality.flags)
        self.assertFalse(quality.formal_conclusion_allowed)

    def test_real_boundary_with_parity_and_samples_is_grade_a_diagnostic(self) -> None:
        quality = evaluate_operator_quality(
            timing_source="daft_collect_boundary",
            input_parity=True,
            perf_lock_passed=True,
            sample_count=5000,
            min_perf_samples=5000,
            unblock_perf=False,
            representative_profile=False,
        )

        self.assertEqual(quality.grade, "A")
        self.assertEqual(quality.flags, ("diagnostic_only",))
        self.assertFalse(quality.formal_conclusion_allowed)

    def test_estimated_even_split_is_grade_c_and_never_formal(self) -> None:
        quality = evaluate_operator_quality(
            timing_source="estimated_even_split",
            input_parity=True,
            perf_lock_passed=True,
            sample_count=10000,
            min_perf_samples=5000,
            unblock_perf=True,
            representative_profile=True,
        )

        self.assertEqual(quality.grade, "C")
        self.assertIn("estimated_timing", quality.flags)
        self.assertFalse(quality.formal_conclusion_allowed)

    def test_log_timing_is_grade_b_and_records_missing_samples(self) -> None:
        quality = evaluate_operator_quality(
            timing_source="data_juicer_log",
            input_parity=True,
            perf_lock_passed=True,
            sample_count=20,
            min_perf_samples=5000,
            unblock_perf=True,
            representative_profile=True,
        )

        self.assertEqual(quality.grade, "B")
        self.assertIn("log_timing", quality.flags)
        self.assertIn("insufficient_samples", quality.flags)
        self.assertFalse(quality.formal_conclusion_allowed)


if __name__ == "__main__":
    unittest.main()
