"""Integration tests for the local pipeline orchestration path.

The fake remote steps write the same artifact shapes that the SSH/Docker steps
produce on real ARM/x86 hosts.  This keeps the local test deterministic while
still exercising run state, dual-platform flow, acquire summary, and backfill.
"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pyframework_pipeline.orchestrator import run_pipeline


class PipelineIntegrationTest(unittest.TestCase):
    def test_selective_resume_preserves_other_platform_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "platform-resume"

            with mock.patch(
                "pyframework_pipeline.orchestrator._execute_step"
            ):
                first_rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    platforms=["arm"],
                    resume_from="5a",
                    stop_before="5b.1",
                    yes=True,
                )
                second_rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    platforms=["x86"],
                    resume_from="5a",
                    stop_before="5b.1",
                    yes=True,
                )

            state = json.loads((run_dir / "pipeline-run.json").read_text())
            completed = {
                (item["step"], item.get("platform"))
                for item in state["steps"]
                if item["status"] == "completed"
            }

        self.assertEqual(first_rc, 0)
        self.assertEqual(second_rc, 0)
        self.assertEqual(state["platforms"], ["arm", "x86"])
        for step in ("5a", "5a.1", "5a.2"):
            self.assertIn((step, "arm"), completed)
            self.assertIn((step, "x86"), completed)

    def test_resume_retries_stage_without_forcing_adapter_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "resume"

            with mock.patch(
                "pyframework_pipeline.orchestrator._execute_step"
            ) as execute:
                rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    platforms=["arm"],
                    resume_from="5a",
                    stop_before="5b.1",
                    yes=True,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(execute.call_args_list), 3)
            self.assertTrue(
                all(not call.kwargs["force"] for call in execute.call_args_list)
            )

    def test_explicit_force_is_forwarded_to_adapter_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "force"

            with mock.patch(
                "pyframework_pipeline.orchestrator._execute_step"
            ) as execute:
                rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    platforms=["arm"],
                    stop_before="4",
                    force=True,
                    yes=True,
                )

            self.assertEqual(rc, 0)
            self.assertEqual(len(execute.call_args_list), 1)
            self.assertTrue(execute.call_args.kwargs["force"])

    def test_run_pipeline_filters_to_one_selected_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "arm-only"

            with mock.patch(
                "pyframework_pipeline.orchestrator._execute_step"
            ) as execute:
                rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    platforms=["arm"],
                    stop_before="5d",
                    force=True,
                    yes=True,
                )

            self.assertEqual(rc, 0)
            state = json.loads((run_dir / "pipeline-run.json").read_text())
            self.assertEqual(state["platforms"], ["arm"])
            per_platform_calls = [
                call.args[3]
                for call in execute.call_args_list
                if call.args[3] is not None
            ]
            self.assertTrue(per_platform_calls)
            self.assertEqual(set(per_platform_calls), {"arm"})
            global_calls = [
                call for call in execute.call_args_list if call.args[3] is None
            ]
            self.assertEqual(
                {call.args[0] for call in global_calls}, {"5c", "5c.1"}
            )
            self.assertTrue(
                all(call.kwargs["platforms"] == ["arm"] for call in global_calls)
            )

    def test_run_pipeline_rejects_unconfigured_selected_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "bad-platform"

            rc = run_pipeline(
                project_yaml,
                run_dir,
                platforms=["riscv"],
                stop_before="5d",
                force=True,
                yes=True,
            )

            self.assertEqual(rc, 1)

    def test_run_pipeline_backfills_dual_platform_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            project_dir.mkdir()
            project_yaml = _write_project(project_dir)
            run_dir = project_dir / "runs" / "integration"

            with (
                mock.patch(
                    "pyframework_pipeline.adapters.pyflink.adapter.PyFlinkAdapter.deploy_workload",
                    side_effect=_fake_workload_deploy,
                ),
                mock.patch(
                    "pyframework_pipeline.adapters.pyflink.adapter.PyFlinkAdapter.run_benchmark",
                    side_effect=_fake_benchmark,
                ),
                mock.patch(
                    "pyframework_pipeline.orchestrator._run_collect_substep",
                    side_effect=_fake_collect_substep,
                ),
                mock.patch(
                    "pyframework_pipeline.environment.deploy.deploy_plan",
                    side_effect=_fake_deploy_plan,
                ),
            ):
                rc = run_pipeline(
                    project_yaml,
                    run_dir,
                    stop_before="6b",
                    force=True,
                    yes=True,
                )

            self.assertEqual(rc, 0)

            state = json.loads((run_dir / "pipeline-run.json").read_text())
            completed = {(s["step"], s.get("platform")) for s in state["steps"]}
            for platform in ("arm", "x86"):
                self.assertIn(("3", platform), completed)
                self.assertIn(("4", platform), completed)
                self.assertIn(("5a", platform), completed)
                self.assertIn(("5b.1", platform), completed)
                self.assertIn(("5b.2", platform), completed)
                self.assertIn(("5b.2b", platform), completed)
                self.assertIn(("5b.3", platform), completed)
            self.assertIn(("5c", None), completed)
            self.assertIn(("6", None), completed)
            self.assertNotIn(("6b", None), completed)

            dataset = json.loads(
                (project_dir / "datasets" / "integration.dataset.json").read_text()
            )
            source = json.loads(
                (project_dir / "sources" / "integration.source.json").read_text()
            )
            project = json.loads(
                (project_dir / "projects" / "integration.project.json").read_text()
            )

            q01 = next(c for c in dataset["cases"] if c["legacyCaseId"] == "q01")
            self.assertIn("8.0", q01["metrics"]["framework"]["arm"])
            self.assertTrue(q01["metrics"]["framework"]["arm"].endswith("s"))
            self.assertIn("6.0", q01["metrics"]["framework"]["x86"])
            self.assertTrue(q01["metrics"]["framework"]["x86"].endswith("s"))
            self.assertEqual(q01["metrics"]["demo"]["arm"], "2.00 s")
            self.assertEqual(q01["metrics"]["demo"]["x86"], "1.50 s")

            symbols = {f["symbol"] for f in dataset["functions"]}
            self.assertIn("_PyEval_EvalFrameDefault", symbols)
            self.assertGreater(len(source["artifactIndex"]), 0)
            self.assertGreater(len(project["functionBindings"]), 0)


def _write_project(project_dir: Path) -> Path:
    project_yaml = project_dir / "project.yaml"
    project_yaml.write_text(
        "\n".join(
            [
                "id: integration-project",
                "name: Integration Project",
                "fourLayerRoot: .",
                "workload:",
                "  localDir: workload",
                "  rows: 10",
                "  queries:",
                "    - q01",
                "bridge:",
                "  repo: owner/repo",
                "  platform: github",
                "run:",
                "  platforms:",
                "    - arm",
                "    - x86",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    (project_dir / "datasets").mkdir()
    (project_dir / "sources").mkdir()
    (project_dir / "projects").mkdir()
    (project_dir / "datasets" / "integration.dataset.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "integration-dataset",
                "cases": [
                    {
                        "id": "tpch-q01-pyflink",
                        "legacyCaseId": "q01",
                        "name": "TPC-H Q01",
                    }
                ],
                "functions": [],
                "patterns": [],
                "rootCauses": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (project_dir / "sources" / "integration.source.json").write_text(
        json.dumps({"schemaVersion": 1, "id": "integration-source", "artifactIndex": []}),
        encoding="utf-8",
    )
    (project_dir / "projects" / "integration.project.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "id": "integration-project",
                "caseBindings": [],
                "functionBindings": [],
            }
        ),
        encoding="utf-8",
    )
    return project_yaml


def _fake_workload_deploy(project_path: Path, run_dir: Path, platform: str, *, yes: bool = False) -> None:
    marker = run_dir / platform / "workload-deployed.txt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")


def _fake_deploy_plan(
    project_path: Path,
    platform_id: str,
    plan_path: Path | None = None,
    *,
    output_dir: Path | None = None,
    yes: bool = False,
) -> dict:
    return {
        "status": "completed",
        "platform": platform_id,
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "record": {
            "schemaVersion": 1,
            "projectId": "integration-project",
            "platform": platform_id,
            "planHash": "sha256:test",
            "steps": [{"id": "fake-deploy", "status": "passed"}],
        },
    }


def _fake_benchmark(
    project_path: Path,
    run_dir: Path,
    platform: str,
    *,
    force: bool = False,
) -> None:
    cases = {
        "arm": {
            "wall_clock_ns": 2_000_000_000,
            "framework_ns": 8_000,
            "operator_ns": 2_000,
        },
        "x86": {
            "wall_clock_ns": 1_500_000_000,
            "framework_ns": 6_000,
            "operator_ns": 1_500,
        },
    }
    values = cases[platform]
    timing_dir = run_dir / platform / "timing"
    timing_dir.mkdir(parents=True, exist_ok=True)
    (timing_dir / "timing-normalized.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "platform": platform,
                "cases": [
                    {
                        "caseId": "q01",
                        "metrics": {
                            "wallClockTime": {"wall_clock_ns": values["wall_clock_ns"]},
                            "tmE2eTime": {"wall_clock_ns": values["wall_clock_ns"]},
                            "frameworkCallTime": {
                                "per_invocation_ns": values["framework_ns"]
                            },
                            "businessOperatorTime": {
                                "per_invocation_ns": values["operator_ns"]
                            },
                        },
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _fake_collect_substep(
    project_path: Path,
    run_dir: Path,
    platform: str,
    substep: str,
    *,
    force: bool = False,
) -> None:
    platform_dir = run_dir / platform
    perf_dir = platform_dir / "perf" / "data"
    perf_dir.mkdir(parents=True, exist_ok=True)

    if substep == "5b.1":
        (perf_dir / f"perf-{platform}.data").write_bytes(b"perf-data")
    elif substep == "5b.2":
        _write_perf_csv(perf_dir / "perf_records.csv", platform)
    elif substep == "5b.2b":
        (perf_dir / "symbol_source_map.json").write_text(
            json.dumps(
                {
                    "_PyEval_EvalFrameDefault": {
                        "sourceFile": "Python/ceval.c",
                        "snippet": "PyObject *_PyEval_EvalFrameDefault(void) { return NULL; }",
                    }
                }
            ),
            encoding="utf-8",
        )
    elif substep == "5b.3":
        arch = "arm64" if platform == "arm" else "x86_64"
        asm_dir = platform_dir / "asm" / arch
        asm_dir.mkdir(parents=True, exist_ok=True)
        symbol = "_PyEval_EvalFrameDefault"
        stem = "a8fe4a73"
        (asm_dir / "symbol_map.json").write_text(
            json.dumps({stem: symbol}), encoding="utf-8"
        )
        (asm_dir / f"{stem}.s").write_text(
            f"<{symbol}>:\n\tmov x0, x0\n\tret\n",
            encoding="utf-8",
        )


def _write_perf_csv(path: Path, platform: str) -> None:
    rows = {
        "arm": {"self": "60.0", "children": "70.0", "period": "1200", "samples": "600"},
        "x86": {"self": "50.0", "children": "55.0", "period": "900", "samples": "450"},
    }
    row = rows[platform]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "symbol",
                "self",
                "children",
                "period",
                "sample_count",
                "category_top",
                "category_sub",
                "shared_object",
                "python_version",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "symbol": "_PyEval_EvalFrameDefault",
                "self": row["self"],
                "children": row["children"],
                "period": row["period"],
                "sample_count": row["samples"],
                "category_top": "CPython.Interpreter",
                "category_sub": "",
                "shared_object": "libpython3.14.so.1.0",
                "python_version": "3.14.3",
            }
        )


if __name__ == "__main__":
    unittest.main()
