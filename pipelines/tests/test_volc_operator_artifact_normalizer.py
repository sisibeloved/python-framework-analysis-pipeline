"""End-to-end normalization tests for collected Volc operator artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pyframework_pipeline.adapters.volcoperatorsim.artifact_normalizer import (
    _collect_perf_rows,
    _normalize_perf_scope_records,
    _runtime_input_fingerprint,
    normalize_operator_artifacts,
)
from pyframework_pipeline.adapters.volcoperatorsim.operator_report import (
    _format_top_symbols,
    render_operator_reports,
)
from pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle import (
    IDENTITY_POLICY,
)
from pyframework_pipeline.adapters.volcoperatorsim.acquisition_manifest import (
    build_acquisition_manifest,
)
from pyframework_pipeline.contracts.operator import (
    OperatorDataset,
    OperatorQuality,
    OperatorRecord,
    OperatorResources,
    OperatorTiming,
)


class VolcOperatorArtifactNormalizerTest(unittest.TestCase):
    def test_current_identity_policy_with_incomplete_resolution_blocks_conclusions(
        self,
    ) -> None:
        key = ("abc123def456", "daft_ray")

        records = _normalize_perf_scope_records(
            index={
                key: {
                    "taskSpecId": "pipeline@v0",
                    "operatorCaseId": "case",
                    "operatorId": "op",
                    "order": 0,
                    "pipelineId": "pipeline",
                    "engineId": "daft_ray",
                }
            },
            perf_by_key={
                key: {
                    "sampleCount": 10_000,
                    "totalPeriod": 10_000,
                    "rows": [],
                    "symbolResolution": {
                        "status": "incomplete",
                        "identityPolicy": IDENTITY_POLICY,
                    },
                }
            },
            isolated_by_key={},
            run_id="run",
            platform="arm",
            min_perf_samples=5_000,
            unblock_perf=False,
            representative_profile=False,
            top_symbol_limit=5,
        )

        self.assertEqual(len(records), 1)
        quality = records[0].quality
        self.assertEqual(quality.grade, "C")
        self.assertIn("symbol_resolution_incomplete", quality.flags)
        self.assertNotIn("symbol_identity_policy_legacy", quality.flags)
        self.assertFalse(quality.formal_conclusion_allowed)

    def test_single_pass_perf_inherits_context_input_and_perf_lock_evidence(
        self,
    ) -> None:
        key = ("abc123def456", "daft_ray")
        context = OperatorRecord(
            run_id="run",
            platform_id="arm",
            arch="aarch64",
            pipeline_id="pipeline",
            task_spec_id="pipeline@v0",
            engine_id="daft_ray",
            operator_case_id="case",
            operator_id="op",
            order=0,
            measurement_scope="pipeline_context",
            input_fingerprint="sha256:input",
            timing=OperatorTiming(timing_source="daft_collect_boundary"),
            resources=OperatorResources(
                window_cpu_time_ns_estimate=500_000_000,
                sample_count=10,
            ),
            quality=OperatorQuality(
                grade="A",
                flags=("perf_lock_warn",),
                formal_conclusion_allowed=True,
            ),
            metadata={
                "perfLockStatus": "warn",
                "perfLockWarningCodes": ["swap_present"],
                "perfLockViolationCodes": [],
            },
        )

        records = _normalize_perf_scope_records(
            index={
                key: {
                    "taskSpecId": "pipeline@v0",
                    "operatorCaseId": "case",
                    "operatorId": "op",
                    "order": 0,
                    "pipelineId": "pipeline",
                    "engineId": "daft_ray",
                }
            },
            perf_by_key={
                key: {
                    "sampleCount": 10_000,
                    "totalPeriod": 10_000,
                    "rows": [],
                    "symbolResolution": {
                        "status": "complete",
                        "identityPolicy": IDENTITY_POLICY,
                    },
                }
            },
            isolated_by_key={},
            context_by_key={key: context},
            run_id="run",
            platform="arm",
            min_perf_samples=5_000,
            unblock_perf=True,
            representative_profile=True,
            top_symbol_limit=5,
        )

        self.assertEqual(len(records), 1)
        perf = records[0]
        self.assertEqual(perf.arch, "aarch64")
        self.assertEqual(perf.input_fingerprint, "sha256:input")
        self.assertEqual(perf.quality.grade, "A")
        self.assertEqual(perf.quality.flags, ("perf_lock_warn",))
        self.assertTrue(perf.quality.formal_conclusion_allowed)
        self.assertEqual(perf.metadata["perfLockStatuses"], ["warn"])
        self.assertEqual(
            perf.metadata["perfLockWarningCodes"], ["swap_present"]
        )

    def test_perf_normalization_prefers_resolved_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            raw_root = platform_dir / "operators/raw"
            artifact = (
                raw_root
                / "operator_case_perf/ts/aarch64/daft_ray"
                / "operator_case_perf__abc123def456__perf_attempt_001"
            )
            artifact.mkdir(parents=True)
            original = artifact / "perf-report-period.txt"
            resolved = artifact / "perf-report-period-resolved.txt"
            original.write_text(
                "100.00|100|10|python|1:python|(deleted)|[.] 0x1234\n",
                encoding="utf-8",
            )
            resolved.write_text(
                "100.00|100|10|python|1:python|libfoo.so|[.] foo\n",
                encoding="utf-8",
            )
            resolution = artifact / "perf-symbol-resolution.json"
            resolution.write_text(
                json.dumps({"status": "complete", "identityPolicy": IDENTITY_POLICY}),
                encoding="utf-8",
            )
            index = {
                ("abc123def456", "daft_ray"): {
                    "taskSpecId": "pipeline@v0",
                    "operatorCaseId": "case",
                    "operatorId": "op",
                    "order": 0,
                    "pipelineId": "pipeline",
                }
            }

            rows, grouped = _collect_perf_rows(
                raw_root=raw_root,
                index=index,
                run_id="run",
                platform="arm",
                allowed_paths={
                    original.resolve(), resolved.resolve(), resolution.resolve()
                },
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shared_object"], "libfoo.so")
        self.assertEqual(rows[0]["symbol"], "[.] foo")
        self.assertTrue(rows[0]["source_report"].endswith("-resolved.txt"))
        self.assertEqual(grouped[("abc123def456", "daft_ray")]["sampleCount"], 10)

    def test_perf_normalization_rejects_legacy_resolved_identity_guesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            raw_root = platform_dir / "operators/raw"
            artifact = (
                raw_root
                / "operator_case_perf/ts/aarch64/daft_ray"
                / "operator_case_perf__abc123def456__perf_attempt_001"
            )
            artifact.mkdir(parents=True)
            original = artifact / "perf-report-period.txt"
            resolved = artifact / "perf-report-period-resolved.txt"
            resolution = artifact / "perf-symbol-resolution.json"
            original.write_text(
                "100.00|100|10|python|1:python|(deleted)|[.] hot_native_code\n",
                encoding="utf-8",
            )
            resolved.write_text(
                "100.00|100|10|python|1:python|python|[.] hot_native_code\n",
                encoding="utf-8",
            )
            resolution.write_text(
                json.dumps({"status": "complete"}), encoding="utf-8"
            )
            index = {
                ("abc123def456", "daft_ray"): {
                    "taskSpecId": "pipeline@v0",
                    "operatorCaseId": "case",
                    "operatorId": "op",
                    "order": 0,
                    "pipelineId": "pipeline",
                }
            }

            rows, grouped = _collect_perf_rows(
                raw_root=raw_root,
                index=index,
                run_id="run",
                platform="arm",
                allowed_paths={
                    original.resolve(), resolved.resolve(), resolution.resolve()
                },
            )

        self.assertEqual(rows[0]["shared_object"], "(deleted)")
        self.assertTrue(rows[0]["source_report"].endswith("perf-report-period.txt"))
        self.assertEqual(
            grouped[("abc123def456", "daft_ray")]["symbolResolution"]["status"],
            "complete",
        )

    def test_context_falls_back_to_separate_validated_runner_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            case_id = "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
            _write_plan(platform_dir, case_id)
            _write_context_artifact(platform_dir)
            capture = (
                platform_dir
                / "operators/raw/pipeline_context/ts/aarch64/daft_ray"
                / "pipeline_context__pipeline_text__daft_ray"
            )
            result = json.loads(
                (capture / "result.json").read_text(encoding="utf-8")
            )
            (capture / "result.json").unlink()
            summary = json.loads(
                (capture / "summary.json").read_text(encoding="utf-8")
            )
            summary["artifacts"]["result_json"] = None
            (capture / "summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )
            runner = (
                platform_dir
                / "operators/raw/pipeline_context/runner"
                / "pipeline_context__pipeline_text__daft_ray"
            )
            runner.mkdir(parents=True)
            result["task_id"] = "pipeline_text@v0__pipeline_context"
            result["task_spec"] = {
                "metadata": {"sourceTaskSpecId": "pipeline_text@v0"}
            }
            result["metrics"].pop("input_fingerprint")
            result["perf_lock"]["input_fingerprint"] = {
                "task_json_sha256": "runtime-task",
                "path": "/host/frozen/input.lance",
            }
            (runner / "result.json").write_text(
                json.dumps(result), encoding="utf-8"
            )
            for scope in (
                "pipeline_e2e", "snapshot_build", "operator_case_e2e",
                "operator_case_perf",
            ):
                marker = platform_dir / f"operators/raw/{scope}/evidence.json"
                marker.parent.mkdir(parents=True)
                marker.write_text("{}", encoding="utf-8")
            build_acquisition_manifest(platform_dir, platform="arm")

            records_path = normalize_operator_artifacts(
                platform_dir, platform="arm"
            )
            records = OperatorDataset.read_jsonl(records_path).records
            coverage = json.loads(
                (platform_dir / "operators/operator-coverage.json").read_text(
                    encoding="utf-8"
                )
            )

        context = [
            record
            for record in records
            if record.measurement_scope == "pipeline_context"
        ]
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0].timing.pipeline_context_ns, 200_000_000)
        self.assertEqual(
            context[0].input_fingerprint,
            '{"path":"/host/frozen/input.lance",'
            '"task_json_sha256":"runtime-task"}',
        )
        self.assertEqual(coverage["scopes"]["pipeline_context"]["actual"], 1)

    def test_runtime_fingerprint_falls_back_to_executed_task_input(self) -> None:
        target = {
            "task_spec": {
                "input": {"input_fingerprint": "sha256:canonical-input"}
            },
            "metrics": {"input_rows": 32},
        }

        self.assertEqual(
            _runtime_input_fingerprint(target, target["metrics"]),
            "sha256:canonical-input",
        )

    def test_runtime_fingerprint_uses_perf_lock_capture_evidence(self) -> None:
        target = {
            "perf_lock": {
                "input_fingerprint": {
                    "task_json_sha256": "runtime-task",
                    "path": "/host/frozen/input.lance",
                }
            },
            "task_spec": {"input": {"path": "fixtures/input.lance"}},
            "metrics": {"input_rows": 100_000},
        }

        self.assertEqual(
            _runtime_input_fingerprint(target, target["metrics"]),
            '{"path":"/host/frozen/input.lance",'
            '"task_json_sha256":"runtime-task"}',
        )

    def test_skipped_isolated_scope_is_terminal_for_operator_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            case_id = "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
            case_hash = hashlib.sha256(case_id.encode()).hexdigest()[:12]
            _write_plan(platform_dir, case_id)
            _write_context_artifact(platform_dir)
            _write_perf_artifact(platform_dir, case_hash)
            for scope in ("pipeline_e2e", "operator_case_e2e"):
                marker = platform_dir / f"operators/raw/{scope}/SKIPPED.json"
                marker.parent.mkdir(parents=True)
                marker.write_text(
                    json.dumps(
                        {
                            "status": "skipped",
                            "measurementPolicy": "single_pass_context_perf",
                        }
                    ),
                    encoding="utf-8",
                )
            snapshot = (
                platform_dir / "operators/raw/snapshot_build/evidence.json"
            )
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text("{}", encoding="utf-8")
            build_acquisition_manifest(platform_dir, platform="arm")

            normalize_operator_artifacts(platform_dir, platform="arm")
            coverage = json.loads(
                (platform_dir / "operators/operator-coverage.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(
            coverage["scopes"]["operator_case_e2e"],
            {"status": "skipped", "actual": 0, "missing": []},
        )
        self.assertEqual(
            coverage["scopes"]["pipeline_context"]["status"], "complete"
        )
        self.assertEqual(
            coverage["scopes"]["operator_case_perf"]["status"], "complete"
        )

    def test_readable_report_aggregates_duplicate_hot_symbols(self) -> None:
        rendered = _format_top_symbols(
            [
                {
                    "symbol": "_PyEval_EvalFrameDefault",
                    "sharedObject": "python3.10",
                    "periodShare": 0.3,
                },
                {
                    "symbol": "_PyEval_EvalFrameDefault",
                    "sharedObject": "python3.10",
                    "periodShare": 0.2,
                },
                {
                    "symbol": "gc_collect_main",
                    "sharedObject": "python3.10",
                    "periodShare": 0.1,
                },
            ]
        )

        self.assertEqual(
            rendered,
            "`_PyEval_EvalFrameDefault` 50.00%<br>"
            "`gc_collect_main` 10.00%",
        )

    def test_context_isolated_and_perf_scopes_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            operator_case_id = "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
            case_hash = hashlib.sha256(operator_case_id.encode()).hexdigest()[:12]
            _write_plan(platform_dir, operator_case_id)
            _write_context_artifact(platform_dir)
            _write_isolated_round(
                platform_dir,
                case_hash,
                round_number=1,
                runner_elapsed_s=0.8,
                operator_elapsed_s=0.5,
                outer_s=1.0,
                cpu_s=0.7,
                rss_mb=100.0,
            )
            _write_isolated_round(
                platform_dir,
                case_hash,
                round_number=2,
                runner_elapsed_s=0.9,
                operator_elapsed_s=0.6,
                outer_s=1.1,
                cpu_s=0.9,
                rss_mb=120.0,
                output_rows=0,
                perf_lock_status="warn",
            )
            _write_perf_artifact(platform_dir, case_hash)
            for scope in ("pipeline_e2e", "snapshot_build"):
                marker = platform_dir / f"operators/raw/{scope}/evidence.json"
                marker.parent.mkdir(parents=True)
                marker.write_text("{}", encoding="utf-8")
            build_acquisition_manifest(platform_dir, platform="arm")

            records_path = normalize_operator_artifacts(
                platform_dir,
                platform="arm",
                min_perf_samples=5000,
                unblock_perf=False,
                representative_profile=False,
                top_symbols=1,
            )
            self.assertFalse(
                (platform_dir / "operators/operator-report.html").exists()
            )
            report_index = render_operator_reports(platform_dir)
            report_index_exists = report_index.is_file()
            records = OperatorDataset.read_jsonl(records_path).records
            readable_report = (
                platform_dir / "operators/reports/pipeline_text.md"
            ).read_text(encoding="utf-8")
            readable_html = (
                platform_dir / "operators/reports/pipeline_text.html"
            ).read_text(encoding="utf-8")
            with (
                platform_dir / "operators" / "perf" / "operator-perf-records.csv"
            ).open(encoding="utf-8", newline="") as handle:
                perf_rows = list(csv.DictReader(handle))
            manifest = json.loads(
                (platform_dir / "operators" / "acquisition-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            coverage = json.loads(
                (platform_dir / "operators" / "operator-coverage.json").read_text(
                    encoding="utf-8"
                )
            )

        by_scope = {record.measurement_scope: record for record in records}
        self.assertEqual(set(by_scope), {"pipeline_context", "operator_case_e2e", "operator_case_perf"})
        self.assertEqual(
            by_scope["pipeline_context"].timing.pipeline_context_ns, 200_000_000
        )
        isolated = by_scope["operator_case_e2e"]
        self.assertEqual(isolated.timing.outer_process_wall_ns, 1_050_000_000)
        self.assertEqual(isolated.timing.runner_elapsed_ns, 850_000_000)
        self.assertEqual(isolated.timing.isolated_operator_ns, 550_000_000)
        self.assertEqual(isolated.timing.residual_ns, 300_000_000)
        self.assertEqual(isolated.timing.median_ns, 850_000_000)
        self.assertEqual(isolated.timing.stddev_ns, 50_000_000)
        self.assertEqual(isolated.timing.rounds, 2)
        self.assertEqual(isolated.input_fingerprint, "sha256:input")
        self.assertEqual(
            isolated.metadata["runtimeInputFingerprints"],
            [
                '{"manifest_path_sha256":"runtime-manifest","task_path":"/host/run/overlay.json"}'
            ],
        )
        self.assertTrue(isolated.metadata["emptyOutputObserved"])
        self.assertEqual(isolated.metadata["inputRows"], [2, 2])
        self.assertEqual(isolated.metadata["outputRows"], [0, 1])
        self.assertAlmostEqual(
            isolated.resources.mean_cores_busy or 0.0,
            (0.7 / 1.0 + 0.9 / 1.1) / 2,
        )
        self.assertEqual(isolated.perf_stat["ipc"], 2.0)
        self.assertEqual(isolated.perf_stat["l1dMissRate"], 0.1)
        self.assertEqual(isolated.perf_stat["events"]["cycles"]["coveragePct"], 50.0)
        self.assertIn("stalled-cycles-frontend", isolated.perf_stat["unsupportedEvents"])
        self.assertEqual(isolated.quality.grade, "A")
        self.assertIn("diagnostic_only", isolated.quality.flags)
        self.assertIn("perf_lock_warn", isolated.quality.flags)
        self.assertNotIn("perf_lock_failed", isolated.quality.flags)
        self.assertEqual(isolated.metadata["perfLockStatuses"], ["pass", "warn"])
        self.assertEqual(
            isolated.metadata["perfLockWarningCodes"], ["swap_present"]
        )

        self.assertEqual(len(perf_rows), 2)
        top = perf_rows[0]
        self.assertEqual(top["arch"], "aarch64")
        self.assertEqual(top["measurement_scope"], "operator_case_perf")
        self.assertEqual(top["period"], "6000")
        self.assertEqual(top["period_share"], "0.6")
        self.assertEqual(top["estimated_cpu_time_ns"], "480000000")
        self.assertEqual(top["category_top"], "Python/CPython runtime")
        self.assertEqual(by_scope["operator_case_perf"].resources.sample_count, 10_000)
        self.assertIn("perf_lock_warn", by_scope["operator_case_perf"].quality.flags)
        self.assertEqual(
            by_scope["operator_case_perf"].metadata["perfLockStatuses"],
            ["pass", "warn"],
        )
        self.assertTrue(
            any(
                artifact.endswith("perf-annotate.txt")
                for artifact in by_scope["operator_case_perf"].source_artifacts
            )
        )
        self.assertTrue(
            any(
                artifact.endswith("cpu.svg")
                for artifact in by_scope["operator_case_perf"].source_artifacts
            )
        )
        self.assertEqual(
            len(by_scope["operator_case_perf"].metadata["topSymbols"]), 1
        )
        self.assertEqual(
            by_scope["operator_case_perf"].metadata["topSymbols"][0]["symbol"],
            "PyEval_EvalFrameDefault",
        )
        self.assertIn(
            "symbol_identity_policy_legacy",
            by_scope["operator_case_perf"].quality.flags,
        )
        self.assertFalse(
            by_scope["operator_case_perf"].quality.formal_conclusion_allowed
        )
        self.assertEqual(
            by_scope["operator_case_perf"].metadata["buildIds"][0]["buildId"],
            "abc123",
        )
        self.assertTrue(
            by_scope["operator_case_perf"].metadata["asmArtifacts"][0].endswith(
                "PyEval_EvalFrameDefault.s"
            )
        )
        self.assertTrue(
            all(
                record.metadata["runFingerprintSha256"] == "f" * 64
                for record in records
            )
        )
        self.assertFalse(by_scope["operator_case_perf"].quality.formal_conclusion_allowed)
        self.assertEqual(manifest["sourceRevision"], "56d3b6856895427a0519cbaa437d55443fcb578b")
        self.assertGreater(manifest["artifactCount"], 0)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["artifacts"]))
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["expectedCaseCount"], 1)
        self.assertEqual(
            coverage["scopes"],
            {
                "operator_case_e2e": {
                    "status": "complete",
                    "actual": 1,
                    "missing": [],
                },
                "operator_case_perf": {
                    "status": "complete",
                    "actual": 1,
                    "missing": [],
                },
                "pipeline_context": {
                    "status": "complete",
                    "actual": 1,
                    "missing": [],
                },
            },
        )
        self.assertIn("# pipeline_text · ARM 逐算子报告", readable_report)
        self.assertIn("Pipeline E2E", readable_report)
        self.assertIn("E2E 占比", readable_report)
        self.assertIn("隔离算子中位数", readable_report)
        self.assertIn("Perf CPU 分布", readable_report)
        self.assertIn("clean_html_mapper", readable_report)
        self.assertIn("20.00%", readable_report)
        self.assertIn("PyEval_EvalFrameDefault", readable_report)
        self.assertEqual(
            report_index,
            platform_dir / "operators" / "operator-report.html",
        )
        self.assertTrue(report_index_exists)
        self.assertIn("逐算子运行时长与 E2E 占比", readable_html)
        self.assertIn("算子内 Perf CPU 耗时分布", readable_html)
        self.assertIn("20.00%", readable_html)
        self.assertIn("PyEval_EvalFrameDefault", readable_html)
        self.assertIn("语言 / 运行时分布（推断）", readable_html)
        self.assertIn("库 / 映射分布", readable_html)
        self.assertIn("CPython 3.10", readable_html)
        self.assertIn("Python / CPython（C 实现）", readable_html)
        self.assertIn("OpenCV", readable_html)
        self.assertIn("C++", readable_html)

    def test_missing_source_revision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            _write_plan(platform_dir, "pipeline_text@v0::000::clean_html_mapper::44136fa355b3")
            plan_path = platform_dir / "operators" / "operator-plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["sourceRevision"] = ""
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "sourceRevision"):
                normalize_operator_artifacts(platform_dir, platform="arm")

    def test_missing_or_tampered_acquisition_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            case_id = "pipeline_text@v0::000::clean_html_mapper::44136fa355b3"
            _write_plan(platform_dir, case_id)
            with self.assertRaisesRegex(ValueError, "acquisition manifest"):
                normalize_operator_artifacts(platform_dir, platform="arm")

            artifact = platform_dir / "operators/raw/pipeline_context/a.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("before", encoding="utf-8")
            for scope in (
                "pipeline_e2e", "snapshot_build", "operator_case_e2e",
                "operator_case_perf",
            ):
                marker = platform_dir / f"operators/raw/{scope}/evidence.json"
                marker.parent.mkdir(parents=True)
                marker.write_text("{}", encoding="utf-8")
            build_acquisition_manifest(platform_dir, platform="arm")
            artifact.write_text("beforz", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sha256_mismatch"):
                normalize_operator_artifacts(platform_dir, platform="arm")


def _write_plan(platform_dir: Path, operator_case_id: str) -> None:
    plan = {
        "schemaVersion": 1,
        "runId": "run-1",
        "platform": "arm",
        "group": "core_dual_engine",
        "sourceRevision": "56d3b6856895427a0519cbaa437d55443fcb578b",
        "environmentFingerprintSha256": "e" * 64,
        "runFingerprintSha256": "f" * 64,
        "tasks": [
            {
                "pipelineId": "pipeline_text",
                "taskSpecId": "pipeline_text@v0",
                "modality": "text",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorCaseId": operator_case_id,
                        "order": 0,
                        "operatorId": "clean_html_mapper",
                        "category": "mapper",
                        "params": {},
                        "input": {"fingerprint": "sha256:input"},
                    }
                ],
                "snapshots": [],
                "pseudoStages": [],
            }
        ],
    }
    path = platform_dir / "operators" / "operator-plan.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(plan), encoding="utf-8")


def _write_context_artifact(platform_dir: Path) -> None:
    directory = (
        platform_dir
        / "operators/raw/pipeline_context/ts/aarch64/daft_ray"
        / "pipeline_context__pipeline_text__daft_ray"
    )
    directory.mkdir(parents=True)
    result = {
        "engine_id": "daft_ray",
        "resources": {"arch": "aarch64"},
        "perf_lock": {"status": "pass"},
        "metrics": {
            "elapsed_s": 1.0,
            "input_fingerprint": {
                "task_path": "/host/run/overlay.json",
                "manifest_path_sha256": "runtime-manifest",
            },
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
    (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (directory / "summary.json").write_text(
        json.dumps(
            {
                "case": "pipeline_context__pipeline_text__daft_ray",
                "engine": "daft_ray",
                "returncode": 0,
                "artifacts": {"result_json": "result.json", "samples": "samples.jsonl"},
            }
        ),
        encoding="utf-8",
    )
    (directory / "sampler-summary.json").write_text(
        json.dumps({"interval_s": 0.2}), encoding="utf-8"
    )
    (directory / "samples.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"t": 10.2, "tree_cpu_pct": 100.0, "tree_rss_mb": 100.0}),
                json.dumps({"t": 10.4, "tree_cpu_pct": 200.0, "tree_rss_mb": 200.0}),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _write_isolated_round(
    platform_dir: Path,
    case_hash: str,
    *,
    round_number: int,
    runner_elapsed_s: float,
    operator_elapsed_s: float,
    outer_s: float,
    cpu_s: float,
    rss_mb: float,
    output_rows: int = 1,
    perf_lock_status: str = "pass",
) -> None:
    case = f"operator_case_e2e__{case_hash}__round_{round_number:03d}"
    # Explicit overlay runs write runner JSON beside the measured capture tree,
    # while bench_capture.sh writes its summary below a timestamped directory.
    runner = (
        platform_dir
        / "operators/raw/operator_case_e2e/measured/runner"
        / case
    )
    runner.mkdir(parents=True)
    result = {
        "engine_id": "daft_ray",
        "status": "ok",
        "resources": {"arch": "aarch64"},
        "perf_lock": {
            "status": perf_lock_status,
            "warnings": (
                [{"code": "swap_present", "message": "swap is configured"}]
                if perf_lock_status == "warn"
                else []
            ),
            "violations": [],
        },
        "metrics": {
            "elapsed_s": runner_elapsed_s,
            "input_rows": 2,
            "output_rows": output_rows,
            "input_fingerprint": {
                "task_path": "/host/run/overlay.json",
                "manifest_path_sha256": "runtime-manifest",
            },
            "operator_timings": [
                {
                    "dj_ops": "clean_html_mapper",
                    "order": 0,
                    "elapsed_s": operator_elapsed_s,
                    "start_offset_s": 0.1,
                    "end_offset_s": 0.1 + operator_elapsed_s,
                }
            ],
        },
    }
    (runner / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (runner / "zz-wrong-engine.json").write_text(
        json.dumps(
            {
                "engine_id": "datajuicer_native",
                "status": "ok",
                "metrics": {
                    "elapsed_s": 99.0,
                    "operator_timings": [{"elapsed_s": 98.0}],
                },
            }
        ),
        encoding="utf-8",
    )

    artifact = (
        platform_dir
        / f"operators/raw/operator_case_e2e/measured/ts{round_number}/aarch64/daft_ray/{case}"
    )
    artifact.mkdir(parents=True)
    (artifact / "summary.json").write_text(
        json.dumps({"case": case, "engine": "daft_ray", "returncode": 0}),
        encoding="utf-8",
    )
    (artifact / "sampler-summary.json").write_text(
        json.dumps(
            {
                "duration_s": outer_s,
                "returncode": 0,
                "peak_tree_rss_mb": rss_mb,
                "mean_tree_cpu_pct": cpu_s / outer_s * 100,
                "tree_cpu_time_s": {"total": cpu_s},
                "interval_s": 0.2,
                "n_samples": 10,
            }
        ),
        encoding="utf-8",
    )
    (artifact / "perf-stat.txt").write_text(
        "1,000 cycles (50.00%)\n"
        "2,000 instructions\n"
        "100 L1-dcache-loads\n"
        "10 L1-dcache-load-misses\n"
        "<not supported> stalled-cycles-frontend\n",
        encoding="utf-8",
    )


def _write_perf_artifact(platform_dir: Path, case_hash: str) -> None:
    case = f"operator_case_perf__{case_hash}__perf_attempt_002"
    artifact = (
        platform_dir
        / f"operators/raw/operator_case_perf/ts/aarch64/daft_ray/{case}"
    )
    artifact.mkdir(parents=True)
    first_attempt = (
        artifact.parent
        / f"operator_case_perf__{case_hash}__perf_attempt_001"
    )
    first_attempt.mkdir(parents=True)
    (first_attempt / "perf-report-period.txt").write_text(
        "# overhead|period|sample|comm|pid|dso|symbol\n"
        "100.00|900|900|python|123|/usr/lib/libpython3.10.so|staleFirstAttempt\n",
        encoding="utf-8",
    )
    (artifact / "perf-report-period.txt").write_text(
        "# overhead|period|sample|comm|pid|dso|symbol\n"
        "60.00|6000|6000|python|123|python3.10|PyEval_EvalFrameDefault\n"
        "40.00|4000|4000|python|123|/usr/lib/libopencv_core.so|cv::Laplacian\n",
        encoding="utf-8",
    )
    (artifact / "perf-annotate.txt").write_text("asm", encoding="utf-8")
    (artifact / "perf-buildid-list.txt").write_text(
        "abc123 /usr/lib/libpython3.10.so\n", encoding="utf-8"
    )
    (artifact / "PyEval_EvalFrameDefault.s").write_text("mov x0, x0", encoding="utf-8")
    flamegraph = artifact.parent / f"operator_case_perf__{case_hash}__flamegraph"
    flamegraph.mkdir(parents=True)
    (flamegraph / "cpu.svg").write_text("<svg/>", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
