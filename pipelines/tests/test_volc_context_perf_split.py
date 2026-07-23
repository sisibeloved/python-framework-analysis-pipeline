from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.adapters.volcoperatorsim.context_perf_split import (
    build_operator_windows,
    clock_offset_seconds,
    compact_period_report,
)


class VolcContextPerfSplitTest(unittest.TestCase):
    def test_compact_period_report_sums_same_symbol_without_losing_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "full.txt"
            output = root / "compact.txt"
            source.write_text(
                "# header\n"
                " 10.00%|100|2|worker|1|libx.so|[.] hot\n"
                " 20.00%|300|3|worker|1|libx.so|[.] hot\n"
                " 70.00%|600|6|worker|1|liby.so|[.] other\n",
                encoding="utf-8",
            )

            summary = compact_period_report(source, output)

            rows = [line.split("|") for line in output.read_text().splitlines()
                    if line and not line.startswith("#")]
            self.assertEqual(summary, {"rowsBefore": 3, "rowsAfter": 2,
                                       "totalPeriod": 1000, "totalSamples": 11})
            hot = next(row for row in rows if row[-1] == "[.] hot")
            self.assertEqual(int(hot[1]), 400)
            self.assertEqual(int(hot[2]), 5)
            self.assertEqual(hot[0], "40.000000%")

    def test_clock_offset_maps_epoch_boundaries_to_perf_monotonic_time(self) -> None:
        self.assertEqual(
            clock_offset_seconds({"epochSeconds": 1_000.25, "monotonicSeconds": 40.0}),
            960.25,
        )

    def test_build_operator_windows_uses_plan_identity_and_runner_boundaries(self) -> None:
        operator_case_id = "pipeline_pdf_full_min@v0/operator/001/pdf_ocr_mapper"
        plan = {
            "tasks": [
                {
                    "pipelineId": "pipeline_pdf_full_min",
                    "operators": [
                        {
                            "order": 1,
                            "operatorId": "pdf_ocr_mapper",
                            "operatorCaseId": operator_case_id,
                            "engines": ["daft_ray"],
                        }
                    ],
                }
            ]
        }
        result = {
            "metrics": {
                "timeline_t0_epoch": 1_000.0,
                "op_boundaries": [
                    {
                        "order": 1,
                        "dj_ops": "pdf_ocr_mapper",
                        "start_offset_s": 2.5,
                        "end_offset_s": 12.75,
                    }
                ],
            }
        }

        windows = build_operator_windows(
            plan=plan,
            result=result,
            clock_sync={"epochSeconds": 990.0, "monotonicSeconds": 90.0},
            pipeline_id="pipeline_pdf_full_min",
            engine="daft_ray",
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].start_seconds, 102.5)
        self.assertEqual(windows[0].end_seconds, 112.75)
        self.assertEqual(windows[0].operator_id, "pdf_ocr_mapper")
        self.assertEqual(
            windows[0].case_hash,
            hashlib.sha256(operator_case_id.encode("utf-8")).hexdigest()[:12],
        )

    def test_build_operator_windows_rejects_missing_exact_boundary(self) -> None:
        plan = {
            "tasks": [
                {
                    "pipelineId": "p",
                    "operators": [
                        {
                            "order": 0,
                            "operatorId": "op",
                            "operatorCaseId": "case",
                            "engines": ["daft_ray"],
                        }
                    ],
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "missing boundary"):
            build_operator_windows(
                plan=plan,
                result={"metrics": {"timeline_t0_epoch": 100.0, "op_boundaries": []}},
                clock_sync={"epochSeconds": 100.0, "monotonicSeconds": 10.0},
                pipeline_id="p",
                engine="daft_ray",
            )


if __name__ == "__main__":
    unittest.main()
