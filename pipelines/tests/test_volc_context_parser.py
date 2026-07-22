"""Context timing normalization tests against target result shapes."""

from __future__ import annotations

import unittest

from pyframework_pipeline.adapters.volcoperatorsim.normalize import parse_context_records


class ContextParserTest(unittest.TestCase):
    def test_daft_boundary_produces_context_timing_and_sampled_resource_window(self) -> None:
        result = {
            "engine_id": "daft_ray",
            "resources": {"arch": "aarch64"},
            "perf_lock": {"status": "pass"},
            "metrics": {
                "input_fingerprint": "sha256:input",
                "timeline_t0_epoch": 10.0,
                "operator_timings": [
                    {
                        "dj_ops": "clean_html_mapper",
                        "category": "mapper",
                        "order": 0,
                        "elapsed_s": 0.2,
                        "start_offset_s": 0.1,
                        "end_offset_s": 0.3,
                    }
                ],
                "op_boundaries": [
                    {
                        "dj_ops": "clean_html_mapper",
                        "order": 0,
                        "start_offset_s": 0.1,
                        "end_offset_s": 0.3,
                    }
                ],
            },
        }
        task = {
            "task_id": "pipeline_text@v0",
            "pipeline": [{"dj_ops": "clean_html_mapper", "category": "mapper"}],
        }
        samples = (
            {"t": 10.2, "tree_cpu_pct": 100.0, "tree_rss_mb": 100.0},
            {"t": 10.4, "tree_cpu_pct": 200.0, "tree_rss_mb": 200.0},
        )

        records = parse_context_records(
            result=result,
            task_document=task,
            run_id="run-1",
            platform_id="arm",
            pipeline_id="pipeline_text",
            samples=samples,
            sample_interval_s=0.2,
            unblock_perf=False,
            representative_profile=False,
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.measurement_scope, "pipeline_context")
        self.assertEqual(record.timing.pipeline_context_ns, 200_000_000)
        self.assertEqual(record.timing.timing_source, "daft_collect_boundary")
        self.assertEqual(record.timing.start_offset_ns, 100_000_000)
        self.assertEqual(record.timing.end_offset_ns, 300_000_000)
        self.assertEqual(record.resources.window_cpu_time_ns_estimate, 300_000_000)
        self.assertEqual(record.quality.grade, "A")
        self.assertIn("diagnostic_only", record.quality.flags)

    def test_datajuicer_log_timing_is_grade_b_without_resource_window(self) -> None:
        result = {
            "engine_id": "datajuicer_native",
            "resources": {"arch": "x86_64"},
            "perf_lock": {"status": "pass"},
            "metrics": {
                "operator_timings": [
                    {
                        "dj_ops": "text_length_filter",
                        "category": "filter",
                        "order": 0,
                        "elapsed_s": 0.25,
                        "source": "data_juicer_log",
                        "timing_method": "data_juicer_log",
                    }
                ]
            },
        }
        task = {
            "task_id": "pipeline_text@v0",
            "pipeline": [
                {
                    "dj_ops": "text_length_filter",
                    "category": "filter",
                    "params": {"min_len": 5},
                }
            ],
        }

        record = parse_context_records(
            result=result,
            task_document=task,
            run_id="run-1",
            platform_id="x86",
            pipeline_id="pipeline_text",
            samples=(),
            sample_interval_s=0.2,
            unblock_perf=False,
            representative_profile=False,
        )[0]

        self.assertEqual(record.timing.pipeline_context_ns, 250_000_000)
        self.assertEqual(record.timing.timing_source, "data_juicer_log")
        self.assertIsNone(record.resources.window_cpu_time_ns_estimate)
        self.assertEqual(record.quality.grade, "B")

    def test_estimated_split_is_stored_only_in_estimated_field(self) -> None:
        result = {
            "engine_id": "datajuicer_native",
            "resources": {"arch": "x86_64"},
            "perf_lock": {"status": "pass"},
            "metrics": {
                "operator_timings": [
                    {
                        "dj_ops": "clean_html_mapper",
                        "category": "mapper",
                        "order": 0,
                        "elapsed_s": 0.5,
                        "source": "estimated_even_split",
                    }
                ]
            },
        }
        task = {
            "task_id": "pipeline_text@v0",
            "pipeline": [{"dj_ops": "clean_html_mapper"}],
        }

        record = parse_context_records(
            result=result,
            task_document=task,
            run_id="run-1",
            platform_id="x86",
            pipeline_id="pipeline_text",
            samples=(),
            sample_interval_s=0.2,
            unblock_perf=True,
            representative_profile=True,
        )[0]

        self.assertIsNone(record.timing.pipeline_context_ns)
        self.assertEqual(record.timing.estimated_elapsed_ns, 500_000_000)
        self.assertEqual(record.quality.grade, "C")
        self.assertFalse(record.quality.formal_conclusion_allowed)

    def test_perf_lock_warn_preserves_warnings_without_failing_quality(self) -> None:
        result = {
            "engine_id": "datajuicer_native",
            "resources": {"arch": "aarch64"},
            "perf_lock": {
                "status": "warn",
                "warnings": [
                    {"code": "swap_present", "message": "swap_total_mb=16384"},
                    {"code": "turbo_unknown", "message": "turbo is unreadable"},
                ],
                "violations": [],
            },
            "metrics": {
                "operator_timings": [
                    {
                        "dj_ops": "text_length_filter",
                        "category": "filter",
                        "order": 0,
                        "elapsed_s": 0.25,
                        "source": "data_juicer_log",
                    }
                ]
            },
        }
        task = {
            "task_id": "pipeline_text@v0",
            "pipeline": [{"dj_ops": "text_length_filter"}],
        }

        record = parse_context_records(
            result=result,
            task_document=task,
            run_id="run-1",
            platform_id="arm",
            pipeline_id="pipeline_text",
            samples=(),
            sample_interval_s=0.2,
            unblock_perf=False,
            representative_profile=False,
        )[0]

        self.assertEqual(record.quality.grade, "B")
        self.assertNotIn("perf_lock_failed", record.quality.flags)
        self.assertIn("perf_lock_warn", record.quality.flags)
        self.assertEqual(record.metadata["perfLockStatus"], "warn")
        self.assertEqual(
            record.metadata["perfLockWarningCodes"],
            ["swap_present", "turbo_unknown"],
        )


if __name__ == "__main__":
    unittest.main()
