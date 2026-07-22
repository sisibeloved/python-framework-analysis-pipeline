"""Readable Volc per-operator report rendering tests."""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from pyframework_pipeline.adapters.volcoperatorsim.operator_report import (
    build_operator_report_html,
    build_operator_report_markdown,
    write_operator_reports,
)
from pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle import (
    IDENTITY_POLICY,
)
from pyframework_pipeline.contracts.operator import (
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
)


class VolcOperatorReportTest(unittest.TestCase):
    def test_missing_perf_scope_cannot_be_labeled_formal(self) -> None:
        records = _operator_records(context_ns=200_000_000)[:-1]

        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results={},
        )
        markdown = build_operator_report_markdown(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results={},
        )

        self.assertIn("诊断分析", html)
        self.assertNotIn('class="badge formal">正式结论', html)
        self.assertIn("数据用途：**诊断分析**", markdown)

    def test_partial_pipeline_coverage_cannot_be_labeled_formal(self) -> None:
        with TemporaryDirectory() as tmp:
            platform_dir = Path(tmp)
            coverage = platform_dir / "operators/operator-coverage.json"
            coverage.parent.mkdir(parents=True)
            coverage.write_text(
                '{"status":"partial","scopes":{"operator_case_perf":'
                '{"missing":[{"pipelineId":"pipeline_text"}]}}}',
                encoding="utf-8",
            )

            write_operator_reports(
                platform_dir,
                records=_operator_records(context_ns=200_000_000),
                allowed_paths=set(),
            )

            html = (
                platform_dir / "operators/reports/pipeline_text.html"
            ).read_text(encoding="utf-8")

        self.assertIn("诊断分析", html)
        self.assertNotIn('class="badge formal">正式结论', html)

    def test_report_aggregates_exact_top_five_symbols_from_full_perf_csv(self) -> None:
        with TemporaryDirectory() as tmp:
            platform_dir = Path(tmp)
            perf_dir = platform_dir / "operators" / "perf"
            perf_dir.mkdir(parents=True)
            (perf_dir / "operator-perf-records.csv").write_text(
                "benchmark,engine_id,operator_case_id,symbol,shared_object,period\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,aggregate_hot,python3.10,40\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,second_hot,python3.10,70\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,aggregate_hot,python3.10,35\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,third_hot,python3.10,60\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,fourth_hot,python3.10,50\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,fifth_hot,python3.10,40\n"
                "pipeline_text,daft_ray,pipeline_text@v0::000::clean_html_mapper::44136fa355b3,sixth_hot,python3.10,30\n",
                encoding="utf-8",
            )

            write_operator_reports(
                platform_dir,
                records=_operator_records(context_ns=200_000_000),
                allowed_paths=set(),
            )

            report = (
                platform_dir / "operators" / "reports" / "pipeline_text.html"
            ).read_text(encoding="utf-8")
            for symbol in (
                "aggregate_hot",
                "second_hot",
                "third_hot",
                "fourth_hot",
                "fifth_hot",
            ):
                self.assertIn(symbol, report)
            self.assertNotIn("sixth_hot", report)
            self.assertIn("23.08%", report)

    def test_html_connects_operator_wall_time_e2e_share_and_perf_distribution(self) -> None:
        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=_operator_records(context_ns=200_000_000),
            context_results={
                "daft_ray": {
                    "metrics": {
                        "elapsed_s": 1.0,
                        "input_rows": 100,
                        "output_rows": 90,
                        "timing_breakdown": {
                            "buckets": {
                                "ray_init_s": 0.1,
                                "operator_chain_s": 0.2,
                                "finalize_s": 0.7,
                            }
                        },
                    }
                }
            },
        )

        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("pipeline_text", html)
        self.assertIn("200.000 ms", html)
        self.assertIn("20.00%", html)
        self.assertIn("Python/CPython runtime", html)
        self.assertIn("60.00%", html)
        self.assertIn("600.000 ms", html)
        self.assertIn("PyEval_EvalFrameDefault", html)
        self.assertIn("逐算子运行时长与 E2E 占比", html)
        self.assertIn("算子内 Perf CPU 耗时分布", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_html_marks_missing_pipeline_context_as_unattributable(self) -> None:
        records = _operator_records(context_ns=None)
        context_results = {
            "daft_ray": {
                "metrics": {
                    "elapsed_s": 1.0,
                    "input_rows": 100,
                    "output_rows": 90,
                }
            }
        }
        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results=context_results,
        )

        self.assertIn("不可归因", html)
        self.assertIn("需要 per_op 算子边界", html)
        self.assertNotIn("clean_html_mapper</code></td><td>0.000", html)

        markdown = build_operator_report_markdown(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results=context_results,
        )
        self.assertIn("算子链合计：**不可归因**", markdown)
        self.assertIn("非算子/未归因", markdown)

    def test_library_distribution_keeps_long_names_inside_narrow_cards(self) -> None:
        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=_operator_records(context_ns=200_000_000),
            context_results={},
        )

        self.assertIn('class="library-name"', html)
        self.assertIn('class="library-share"', html)
        self.assertIn('class="library-bar bar"', html)
        self.assertIn('class="library-bar bar"><span style="width:40.0000%"', html)
        self.assertIn(
            'grid-template-areas:"name value" "language value" "bar bar"', html
        )
        self.assertIn("overflow-wrap:anywhere", html)
        self.assertIn(
            "fasttext_pybind.cpython-310-aarch64-linux-gnu.so", html
        )

    def test_legacy_complete_resolution_is_never_shown_as_trusted(self) -> None:
        records = list(_operator_records(context_ns=200_000_000))
        records[-1] = replace(
            records[-1],
            metadata={
                **records[-1].metadata,
                "symbolResolution": {
                    "status": "complete",
                    "deletedRowsBefore": 42,
                    "unresolvedDeletedRows": 0,
                },
            },
        )

        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results={},
        )
        markdown = build_operator_report_markdown(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results={},
        )

        self.assertIn("旧版映射结果不可信", html)
        self.assertNotIn("映射解析完成 · deleted 42→0", html)
        self.assertIn("旧版结果：不可信", markdown)

    def test_strict_complete_resolution_is_shown_as_trusted(self) -> None:
        records = list(_operator_records(context_ns=200_000_000))
        records[-1] = replace(
            records[-1],
            metadata={
                **records[-1].metadata,
                "symbolResolution": {
                    "status": "complete",
                    "identityPolicy": IDENTITY_POLICY,
                    "deletedRowsBefore": 2,
                    "unresolvedDeletedRows": 0,
                },
            },
        )

        html = build_operator_report_html(
            pipeline_id="pipeline_text",
            platform_id="arm",
            records=records,
            context_results={},
        )

        self.assertIn("映射解析完成 · deleted 2→0", html)


def _operator_records(*, context_ns: int | None) -> tuple[OperatorRecord, ...]:
    common = {
        "run_id": "run-1",
        "platform_id": "arm",
        "arch": "aarch64",
        "pipeline_id": "pipeline_text",
        "task_spec_id": "pipeline_text@v0",
        "engine_id": "daft_ray",
        "operator_case_id": (
            "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
        ),
        "operator_id": "clean_html_mapper",
        "order": 0,
        "input_fingerprint": "sha256:input",
        "quality": OperatorQuality(
            grade="A", flags=(), formal_conclusion_allowed=True
        ),
    }
    return (
        OperatorRecord(
            **common,
            measurement_scope="pipeline_context",
            timing=OperatorTiming(
                pipeline_context_ns=context_ns,
                timing_source=(
                    "daft_collect_boundary" if context_ns is not None else "unknown"
                ),
                rounds=1,
            ),
        ),
        OperatorRecord(
            **common,
            measurement_scope="operator_case_e2e",
            timing=OperatorTiming(
                runner_elapsed_ns=800_000_000,
                isolated_operator_ns=500_000_000,
                p95_ns=900_000_000,
                rounds=3,
            ),
            resources=OperatorResources(tree_cpu_time_ns=1_000_000_000),
        ),
        OperatorRecord(
            **common,
            measurement_scope="operator_case_perf",
            resources=OperatorResources(sample_count=10_000),
            metadata={
                "categoryPeriodShare": {
                    "Python/CPython runtime": 0.6,
                    "operator native libraries": 0.4,
                },
                "topSymbols": [
                    {
                        "symbol": "PyEval_EvalFrameDefault",
                        "sharedObject": "python3.10",
                        "periodShare": 0.5,
                    }
                ],
                "topLibraries": [
                    {
                        "library": (
                            "fasttext_pybind.cpython-310-aarch64-linux-gnu.so"
                        ),
                        "language": "C++",
                        "periodShare": 0.4,
                    }
                ],
            },
        ),
    )


if __name__ == "__main__":
    unittest.main()
