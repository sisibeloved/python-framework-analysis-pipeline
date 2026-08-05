"""Black-box acquisition tests for the Volc Operator Sim adapter."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pyframework_pipeline.adapters.volcoperatorsim.adapter import VolcOperatorSimAdapter
from pyframework_pipeline.adapters.volcoperatorsim.acquisition import (
    _apply_task_input_overrides,
    _capture_overlay_command,
    _context_perf_windows_complete,
    _load_settings,
    _normalize_real_task_contracts,
    _normalize_pipeline_timing,
    _profile_cpu_envelope,
    _prepare_stage_resume,
    _read_completed_capture_cases,
    _remote_thin_context_view_command,
    _run_context_fast_operator_profiles,
    _run_with_profile_cpu_envelope,
    _snapshot_mirror_command,
    _should_recover_remote,
    _stable_run_id,
    _write_context_perf_skip_markers,
)
from pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle import (
    IDENTITY_POLICY,
)
from pyframework_pipeline.contracts.step import StepError
from pipelines.tests.test_volcoperatorsim_support import _write_volc_project


REVISION = "56d3b6856895427a0519cbaa437d55443fcb578b"


def _target_inputs() -> dict:
    task = {
        "task_id": "pipeline_text@v0",
        "input": {
            "kind": "lance",
            "path": "fixtures/text.lance",
            "jsonl_mirror": "fixtures/text.jsonl",
            "manifest_path": "fixtures/text.manifest.json",
            "field": "text",
        },
        "pipeline": [
            {"dj_ops": "clean_html_mapper", "category": "mapper"},
            {
                "dj_ops": "text_length_filter",
                "category": "filter",
                "params": {"min_len": 5},
            },
        ],
    }
    return {
        "revision": REVISION,
        "formalConfig": {
            "groups": {"core_dual_engine": {"tasks": ["pipeline_text"]}},
            "pipelines": {
                "pipeline_text": {
                    "modality": "text",
                    "engines": ["daft_ray", "datajuicer_native"],
                }
            },
        },
        "taskDocuments": {"pipeline_text": task},
    }


class FakeExecutor:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        fail_once_on: str | None = None,
        remote_complete: bool = False,
        completed_cases: set[tuple[str, str]] | None = None,
        completed_case_samples: dict[tuple[str, str], int] | None = None,
        push_ok: bool = True,
        fail_once_returncode: int = 17,
        target_inputs: dict | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.fail_once_on = fail_once_on
        self.remote_complete = remote_complete
        self.completed_cases = completed_cases or set()
        self.completed_case_samples = completed_case_samples or {}
        self.push_ok = push_ok
        self.fail_once_returncode = fail_once_returncode
        self.target_inputs = target_inputs
        self.commands: list[str] = []
        self.pushed: list[tuple[Path, str]] = []
        self.fetches: list[tuple[str, Path, int | None]] = []

    def run(
        self, command: str, timeout: int = 300, stream: bool = False
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if "PYFRAMEWORK_REMOTE_SCOPE_COMPLETE=" in command:
            marker = (
                "PYFRAMEWORK_REMOTE_SCOPE_COMPLETE=1\n"
                if self.remote_complete
                else ""
            )
            return subprocess.CompletedProcess([], 0, marker, "")
        if "PYFRAMEWORK_CAPTURE_CASE_INVENTORY=" in command:
            payload = [
                {
                    "case": case,
                    "engine": engine,
                    "ok": True,
                    "perfData": True,
                    "cpuSvg": True,
                    "annotate": True,
                    "symbolized": True,
                    "sampleCount": self.completed_case_samples.get((case, engine), 0),
                }
                for case, engine in sorted(self.completed_cases)
            ]
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            return subprocess.CompletedProcess(
                [], 0, f"PYFRAMEWORK_CAPTURE_CASE_INVENTORY={encoded}\n", ""
            )
        if "PYFRAMEWORK_TARGET_INPUTS=" in command:
            payload = base64.b64encode(
                json.dumps(self.target_inputs or _target_inputs()).encode("utf-8")
            ).decode("ascii")
            return subprocess.CompletedProcess([], 0, f"PYFRAMEWORK_TARGET_INPUTS={payload}\n", "")
        if self.fail_on and self.fail_on in command:
            return subprocess.CompletedProcess([], 17, "partial-output", "target failed")
        if self.fail_once_on and self.fail_once_on in command:
            self.fail_once_on = None
            return subprocess.CompletedProcess(
                [], self.fail_once_returncode, "partial-output", "case failed"
            )
        return subprocess.CompletedProcess([], 0, "ok", "")

    def push_file(self, local_path: Path, remote_path: str) -> bool:
        self.pushed.append((local_path, remote_path))
        return self.push_ok

    def fetch_dir(
        self,
        remote_dir: str,
        local_dir: Path,
        *,
        timeout: int | None = None,
    ) -> bool:
        self.fetches.append((remote_dir, local_dir, timeout))
        scope = Path(remote_dir).name
        if scope == "manifests":
            (local_dir / "remote-COMPLETE.json").parent.mkdir(
                parents=True, exist_ok=True
            )
            (local_dir / "remote-COMPLETE.json").write_text(
                "{}", encoding="utf-8"
            )
            return True
        artifact = local_dir / "run" / "aarch64" / "daft_ray" / "pipeline_text"
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "summary.json").write_text(
            json.dumps(
                {
                    "case": "pipeline_text",
                    "engine": "daft_ray",
                    "arch": "aarch64",
                    "status": "ok",
                    "elapsed_s": 1.25,
                    "returncode": 0,
                }
            ),
            encoding="utf-8",
        )
        (local_dir / "fetch-complete.txt").write_text("kept", encoding="utf-8")
        marker = local_dir / "fetch-evidence.json"
        marker.write_text("{}", encoding="utf-8")
        return True


class VolcOperatorSimAcquisitionTest(unittest.TestCase):
    def test_context_perf_split_reuses_complete_symbolized_windows(self) -> None:
        task = {
            "operators": [
                {"operatorCaseId": "task::000::first"},
                {"operatorCaseId": "task::001::second"},
            ]
        }
        inventory = {}
        for case_id in ("task::000::first", "task::001::second"):
            digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
            inventory[
                (f"operator_case_perf__{digest}__context_window_001", "daft_ray")
            ] = {"symbolized": True}

        self.assertTrue(
            _context_perf_windows_complete(task, "daft_ray", inventory)
        )
        inventory.pop(next(iter(inventory)))
        self.assertFalse(
            _context_perf_windows_complete(task, "daft_ray", inventory)
        )

    def test_adapter_requires_explicit_project_opt_in_for_formal_operator_reports(
        self,
    ) -> None:
        project = Path("project.yaml")
        run_dir = Path("run")
        with (
            patch(
                "pyframework_pipeline.config.get_workload_config",
                return_value={
                    "profile": "smoke",
                    "operatorAnalysis": {
                        "unblockPerf": True,
                        "representativeProfile": True,
                    },
                },
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim."
                "artifact_normalizer.normalize_operator_artifacts",
                return_value=run_dir / "arm/operators/operator-records.jsonl",
            ) as normalize,
        ):
            VolcOperatorSimAdapter().normalize_operator_artifacts(
                project, run_dir, "arm"
            )

        self.assertTrue(normalize.call_args.kwargs["unblock_perf"])
        self.assertTrue(normalize.call_args.kwargs["representative_profile"])

    def test_profile_cpu_envelope_adds_one_disjoint_observer_cpu(self) -> None:
        self.assertEqual(_profile_cpu_envelope("4-7", "8"), "4-8")
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            _profile_cpu_envelope("4-7", "7")

    def test_settings_select_the_platform_observer_cpu(self) -> None:
        settings = _load_settings(
            {"operatorAnalysis": {}},
            {
                "software": {
                    "volcOperatorSimRevision": REVISION,
                    "volcCpuSets": {"arm": "4-7"},
                    "volcObserverCpuSets": {"arm": "8"},
                }
            },
            platform="arm",
        )

        self.assertEqual(settings.workload_cpu_set, "4-7")
        self.assertEqual(settings.observer_cpu_set, "8")

    def test_settings_enable_single_pass_context_perf_explicitly(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "contextPerf": {
                        "enabled": True,
                        "splitByOperatorBoundary": True,
                        "perfFrequency": 990,
                        "fastOperatorMinSamples": 5000,
                        "fastOperatorCalls": {
                            "text_chunk_mapper": 10000,
                            "bge_vectorize_mapper": 32,
                        },
                    }
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )

        self.assertTrue(settings.context_perf_enabled)
        self.assertTrue(settings.context_perf_split)
        self.assertEqual(settings.context_perf_frequency, 990)
        self.assertEqual(settings.fast_operator_min_samples, 5000)
        self.assertEqual(
            settings.context_fast_operator_calls,
            {"text_chunk_mapper": 10000, "bge_vectorize_mapper": 32},
        )

    def test_fast_operator_profiles_ignore_configured_operators_outside_task(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "audio_pipeline": {
                            "kind": "file_manifest",
                            "path": "fixtures/audio.jsonl",
                            "manifest_path": "fixtures/audio.jsonl",
                            "rows": 512,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {
                            "text_chunk_mapper": 10000,
                            "bge_vectorize_mapper": 32,
                        }
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "audio_pipeline",
                "engines": ["daft_ray", "datajuicer_native"],
                "operators": [
                    {
                        "operatorId": "audio_duration_filter",
                        "operatorCaseId": "audio_pipeline::000::audio_duration_filter",
                    }
                ],
            },
            support={"microprofile": "/bench/microprofile.py"},
        )

        self.assertEqual(executor.commands, [])

    def test_fast_operator_profiles_skip_datajuicer_diagnostic_engine(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "text_pipeline": {
                            "kind": "lance",
                            "field": "text",
                            "path": "fixtures/text.lance",
                            "jsonl_mirror": "fixtures/text.jsonl",
                            "rows": 50,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {"clean_html_mapper": 1000}
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "text_pipeline",
                "engines": ["daft_ray", "datajuicer_native"],
                "operators": [
                    {
                        "operatorId": "clean_html_mapper",
                        "operatorCaseId": "text_pipeline::000::clean_html_mapper",
                    }
                ],
            },
            support={
                "microprofile": "/bench/microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        joined = "\n".join(executor.commands)
        self.assertIn("ENGINE=daft_ray", joined)
        self.assertNotIn("ENGINE=datajuicer_native", joined)

    def test_fast_operator_profiles_reuse_completed_cases_above_sample_floor(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "text_pipeline": {
                            "kind": "lance",
                            "field": "text",
                            "path": "fixtures/text.lance",
                            "jsonl_mirror": "fixtures/text.jsonl",
                            "rows": 50,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorMinSamples": 5000,
                        "fastOperatorCalls": {"clean_html_mapper": 1000},
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        case = "operator_case_perf__f595a3bb3dc9__context_fast_001"
        executor = FakeExecutor(
            completed_cases={(case, "daft_ray")},
            completed_case_samples={(case, "daft_ray"): 6000},
        )

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "text_pipeline",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "clean_html_mapper",
                        "operatorCaseId": "pipeline_text_fineweb_full_min@v0::000::clean_html_mapper::44136fa355b3",
                    }
                ],
            },
            support={
                "microprofile": "/bench/microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        joined = "\n".join(executor.commands)
        self.assertNotIn("--operator clean_html_mapper", joined)

    def test_fast_operator_profiles_reuse_context_window_above_sample_floor(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "text_pipeline": {
                            "kind": "lance",
                            "field": "text",
                            "path": "fixtures/text.lance",
                            "jsonl_mirror": "fixtures/text.jsonl",
                            "rows": 50,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorMinSamples": 5000,
                        "fastOperatorCalls": {"clean_html_mapper": 1000},
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        case_id = (
            "pipeline_text_fineweb_full_min@v0::000::"
            "clean_html_mapper::44136fa355b3"
        )
        case_hash = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:12]
        window = f"operator_case_perf__{case_hash}__context_window_001"
        executor = FakeExecutor(
            completed_cases={(window, "daft_ray")},
            completed_case_samples={(window, "daft_ray"): 6000},
        )

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "text_pipeline",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "clean_html_mapper",
                        "operatorCaseId": case_id,
                    }
                ],
            },
            support={
                "microprofile": "/bench/microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        joined = "\n".join(executor.commands)
        self.assertNotIn("--operator clean_html_mapper", joined)

    def test_fast_operator_profiles_cover_duplicate_operator_ids(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "audio_pipeline": {
                            "kind": "lance",
                            "field": "file_path",
                            "path": "fixtures/audio.lance",
                            "jsonl_mirror": "fixtures/audio.jsonl",
                            "rows": 512,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {"audio_duration_filter": 1536}
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "audio_pipeline",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "audio_duration_filter",
                        "operatorCaseId": "audio_pipeline::001::audio_duration_filter",
                    },
                    {
                        "operatorId": "audio_duration_filter",
                        "operatorCaseId": "audio_pipeline::005::audio_duration_filter",
                    },
                ],
            },
            support={
                "microprofile": "/bench/microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        capture_commands = [
            command
            for command in executor.commands
            if "--operator audio_duration_filter" in command
        ]
        self.assertEqual(len(capture_commands), 2)

    def test_text_fast_profile_prepares_the_configured_text_field_directly(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "text_pipeline": {
                            "kind": "lance",
                            "field": "text",
                            "path": "fixtures/text.lance",
                            "jsonl_mirror": "fixtures/text.jsonl",
                            "rows": 50,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {"text_chunk_mapper": 100}
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "text_pipeline",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "text_chunk_mapper",
                        "operatorCaseId": "text_pipeline::000::text_chunk_mapper",
                    }
                ],
            },
            support={
                "microprofile": "/bench/microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        joined = "\n".join(executor.commands)
        self.assertIn("--operator identity", joined)
        self.assertIn("--field text", joined)
        self.assertNotIn("--operator pdf_table_extract_mapper", joined)
        self.assertIn("PYTHONPATH=/opt/volc_operator_sim", joined)
        self.assertIn("perf-report-period-resolved.txt", joined)

    def test_pdf_fast_profile_preparation_imports_project_operators(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "pdf_pipeline": {
                            "kind": "jsonl",
                            "path": "fixtures/pdf.jsonl",
                            "manifest_path": "fixtures/pdf.jsonl",
                            "rows": 4,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {"bge_vectorize_mapper": 32}
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "pdf_pipeline",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "bge_vectorize_mapper",
                        "operatorCaseId": "pdf_pipeline::004::bge_vectorize_mapper",
                    }
                ],
            },
            support={
                "microprofile": "/bench/frozen_microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        prepare_command = next(
            command for command in executor.commands if "--output-json" in command
        )
        self.assertIn("--operator pdf_table_extract_mapper", prepare_command)
        self.assertIn("PYTHONPATH=/opt/volc_operator_sim", prepare_command)

    def test_media_fast_profile_uses_frozen_paths_and_task_params(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "inputOverrides": {
                        "image_pipeline": {
                            "kind": "file_manifest",
                            "field": "file_path",
                            "path": "fixtures/image.jsonl",
                            "manifest_path": "fixtures/image.jsonl",
                            "rows": 5000,
                        }
                    },
                    "contextPerf": {
                        "fastOperatorCalls": {"image_size_filter": 100000}
                    },
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        executor = FakeExecutor()

        _run_context_fast_operator_profiles(
            executor=executor,
            settings=settings,
            remote_root="/bench/run/arm",
            plan={"runId": "run-1"},
            task={
                "pipelineId": "image_pipeline",
                "modality": "image",
                "engines": ["daft_ray"],
                "operators": [
                    {
                        "operatorId": "image_size_filter",
                        "operatorCaseId": "image_pipeline::003::image_size_filter",
                        "params": {"min_size": "1B", "max_size": "20MB"},
                    }
                ],
            },
            support={
                "microprofile": "/bench/frozen_microprofile.py",
                "symbol_env": "/bench/perf_symbol_env.sh",
                "symbolizer": "/bench/perf_symbol_bundle.py",
            },
        )

        joined = "\n".join(executor.commands)
        self.assertIn("--operator identity", joined)
        self.assertNotIn("--operator pdf_table_extract_mapper", joined)
        self.assertIn("--params-json", joined)
        self.assertIn("min_size", joined)
        self.assertIn("VOLC_MEDIA_DISABLE_CACHE=1", joined)
        self.assertIn("trap ", joined)
        self.assertIn('rm -rf -- "$VOLC_MEDIA_DERIVED_ROOT"', joined)

    def test_overlay_capture_isolates_and_cleans_datajuicer_intermediates(self) -> None:
        settings = _load_settings(
            {"operatorAnalysis": {}},
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )

        command = _capture_overlay_command(
            settings=settings,
            remote_out="/bench/run/arm/pipeline_e2e",
            task_path="/bench/run/overlays/audio.json",
            engine="datajuicer_native",
            profile="{}",
            case="audio_asr_prep_canonical",
            mode="timing",
            scope="pipeline_e2e",
        )

        self.assertIn("DJ_PRODUCED_DATA_DIR=", command)
        self.assertIn("VOLC_MEDIA_DERIVED_ROOT=", command)
        self.assertIn(
            "/bench/run/arm/pipeline_e2e/_intermediate/"
            "audio_asr_prep_canonical__datajuicer_native",
            command,
        )
        self.assertIn("trap", command)
        self.assertIn("rm -rf --", command)

    def test_overlay_capture_prefers_tools_from_the_selected_conda_environment(self) -> None:
        """External tools must match the libraries prepended by bench_capture."""

        settings = _load_settings(
            {"operatorAnalysis": {}},
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )

        command = _capture_overlay_command(
            settings=settings,
            remote_out="/bench/run/arm/pipeline_context",
            task_path="/bench/run/overlays/pdf.json",
            engine="daft_ray",
            profile="{}",
            case="pipeline_context__pipeline_pdf_full_min__daft_ray",
            mode="perfrecord",
            scope="pipeline_context",
        )

        self.assertIn(
            "export PATH=/opt/conda/envs/xarch/bin:/opt/conda/bin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin && cd /opt/volc_operator_sim",
            command,
        )
        self.assertIn("PERF_EXEC_PATH=/opt/conda/envs/xarch/bin", command)

    def test_single_pass_context_perf_marks_redundant_scopes_skipped(self) -> None:
        settings = _load_settings(
            {
                "operatorAnalysis": {
                    "contextPerf": {
                        "enabled": True,
                        "splitByOperatorBoundary": True,
                    }
                }
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
            platform="arm",
        )
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            _write_context_perf_skip_markers(platform_dir, settings)
            markers = {
                scope: json.loads(
                    (
                        platform_dir
                        / f"operators/raw/{scope}/SKIPPED.json"
                    ).read_text(encoding="utf-8")
                )
                for scope in ("pipeline_e2e", "snapshot_build", "operator_case_e2e")
            }

        self.assertTrue(all(value["status"] == "skipped" for value in markers.values()))
        self.assertTrue(
            all(
                value["measurementPolicy"] == "single_pass_context_perf"
                for value in markers.values()
            )
        )

    def test_disabled_isolated_timing_skips_without_remote_archive_or_fetch(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "    isolatedTiming: true\n",
                    "    contextPerf:\n"
                    "      enabled: true\n"
                    "      splitByOperatorBoundary: true\n"
                    "    isolatedTiming: false\n",
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"

            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm", force=True
                )

            skipped = json.loads(
                (
                    run_dir
                    / "arm/operators/raw/operator_case_e2e/SKIPPED.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(skipped["measurementPolicy"], "single_pass_context_perf")
        self.assertEqual(executor.fetches, [])
        self.assertNotIn("PYFRAMEWORK_SCOPE_ARCHIVE", "\n".join(executor.commands))

    def test_thin_context_transfer_keeps_host_perf_and_skips_bulk_outputs(self) -> None:
        command = _remote_thin_context_view_command(
            "/bench/run/arm/pipeline_context",
            "/bench/run/arm/transfer-views/pipeline_context-1",
        )

        self.assertIn('"perf.data"', command)
        self.assertIn('"perf-script.txt"', command)
        self.assertIn('"perf-report-period.txt"', command)
        self.assertIn('"perf-report-period-resolved-full.txt"', command)
        self.assertIn('"_symbol-cache"', command)
        self.assertIn('"outputs"', command)
        self.assertIn("os.link", command)
        self.assertNotIn("shutil.rmtree", command)

    def test_remote_run_identity_tracks_workload_and_observer_cpu_sets(self) -> None:
        workload = {"operatorAnalysis": {}}
        common = {
            "volcOperatorSimRevision": REVISION,
            "volcCpuSets": {"arm": "4-7"},
        }
        first = _load_settings(
            workload,
            {"software": {**common, "volcObserverCpuSets": {"arm": "8"}}},
            platform="arm",
        )
        second = _load_settings(
            workload,
            {"software": {**common, "volcObserverCpuSets": {"arm": "9"}}},
            platform="arm",
        )

        first_id = _stable_run_id(
            Path("run"),
            "arm",
            first,
            environment_fingerprint_sha256="e" * 64,
        )
        second_id = _stable_run_id(
            Path("run"),
            "arm",
            second,
            environment_fingerprint_sha256="e" * 64,
        )

        self.assertNotEqual(first_id, second_id)

    def test_remote_run_identity_ignores_nonsemantic_fast_sample_budget(self) -> None:
        common = {"software": {"volcOperatorSimRevision": REVISION}}
        first = _load_settings(
            {
                "operatorAnalysis": {
                    "contextPerf": {
                        "fastOperatorCalls": {"clean_html_mapper": 1000}
                    }
                }
            },
            common,
            platform="arm",
        )
        second = _load_settings(
            {
                "operatorAnalysis": {
                    "contextPerf": {
                        "fastOperatorCalls": {"clean_html_mapper": 2000}
                    }
                }
            },
            common,
            platform="arm",
        )

        first_id = _stable_run_id(
            Path("run"),
            "arm",
            first,
            environment_fingerprint_sha256="e" * 64,
        )
        second_id = _stable_run_id(
            Path("run"),
            "arm",
            second,
            environment_fingerprint_sha256="e" * 64,
        )

        self.assertEqual(first_id, second_id)

    def test_profile_cpu_envelope_is_restored_after_the_perf_command(self) -> None:
        settings = _load_settings(
            {"operatorAnalysis": {}},
            {
                "software": {
                    "volcOperatorSimRevision": REVISION,
                    "volcOperatorSimContainer": "bench",
                    "volcCpuSets": {"arm": "4-7"},
                    "volcObserverCpuSets": {"arm": "8"},
                }
            },
            platform="arm",
        )
        executor = FakeExecutor()

        def capture() -> bool:
            executor.commands.append("CAPTURE")
            return True

        result = _run_with_profile_cpu_envelope(
            executor=executor,
            settings=settings,
            action=capture,
        )

        self.assertTrue(result)
        update_commands = [
            command for command in executor.commands if "docker update" in command
        ]
        self.assertEqual(len(update_commands), 2)
        self.assertIn("--cpuset-cpus 4-8", update_commands[0])
        self.assertIn("--cpuset-cpus 4-7", update_commands[1])
        self.assertLess(
            executor.commands.index(update_commands[0]),
            executor.commands.index("CAPTURE"),
        )
        self.assertGreater(
            executor.commands.index(update_commands[1]),
            executor.commands.index("CAPTURE"),
        )

    def test_capture_inventory_uses_latest_same_named_case_artifacts(self) -> None:
        class LocalExecutor:
            def run(self, command: str, timeout: int = 300, stream: bool = False):
                return subprocess.run(
                    command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "20260101/aarch64/daft_ray/repeated_case"
            new = root / "20260102/aarch64/daft_ray/repeated_case"
            for directory in (old, new):
                directory.mkdir(parents=True)
                (directory / "summary.json").write_text(
                    json.dumps(
                        {
                            "case": "repeated_case",
                            "engine": "daft_ray",
                            "status": "ok",
                        }
                    ),
                    encoding="utf-8",
                )
                (directory / "perf.data").write_bytes(b"perf")
                (directory / "perf-annotate.txt").write_text("ok", encoding="utf-8")
            (old / "perf-symbol-resolution.json").write_text(
                json.dumps({"status": "complete"}), encoding="utf-8"
            )
            (old / "perf-report-period-resolved.txt").write_text(
                "resolved", encoding="utf-8"
            )
            for path in old.iterdir():
                os.utime(path, (10, 10))
            for path in new.iterdir():
                os.utime(path, (20, 20))

            inventory = _read_completed_capture_cases(LocalExecutor(), str(root))

        self.assertTrue(inventory[("repeated_case", "daft_ray")]["perfData"])
        self.assertFalse(inventory[("repeated_case", "daft_ray")]["symbolized"])

    def test_capture_inventory_rejects_legacy_symbol_resolution_policy(self) -> None:
        class LocalExecutor:
            def run(self, command: str, timeout: int = 300, stream: bool = False):
                return subprocess.run(
                    command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "ts/aarch64/daft_ray/repeated_case"
            artifact.mkdir(parents=True)
            (artifact / "summary.json").write_text(
                json.dumps(
                    {
                        "case": "repeated_case",
                        "engine": "daft_ray",
                        "status": "ok",
                    }
                ),
                encoding="utf-8",
            )
            (artifact / "perf.data").write_bytes(b"perf")
            (artifact / "perf-report-period-resolved.txt").write_text(
                "resolved", encoding="utf-8"
            )
            resolution = artifact / "perf-symbol-resolution.json"
            resolution.write_text(
                json.dumps({"status": "complete"}), encoding="utf-8"
            )

            legacy = _read_completed_capture_cases(LocalExecutor(), str(root))
            resolution.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "identityPolicy": IDENTITY_POLICY,
                    }
                ),
                encoding="utf-8",
            )
            strict = _read_completed_capture_cases(LocalExecutor(), str(root))

        self.assertFalse(legacy[("repeated_case", "daft_ray")]["symbolized"])
        self.assertTrue(strict[("repeated_case", "daft_ray")]["symbolized"])

    def test_pipeline_timing_falls_back_to_successful_runner_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local_raw = root / "raw"
            wrapper = local_raw / "pipeline_e2e/capture/case"
            wrapper.mkdir(parents=True)
            (wrapper / "summary.json").write_text(
                json.dumps(
                    {
                        "case": "pipeline_text",
                        "engine": "daft_ray",
                        "returncode": 0,
                        "status": None,
                        "elapsed_s": None,
                    }
                ),
                encoding="utf-8",
            )
            runner = local_raw / "pipeline_e2e/runner/pipeline_text"
            runner.mkdir(parents=True)
            (runner / "result.json").write_text(
                json.dumps(
                    {
                        "task_id": "pipeline_text@v0__pipeline_e2e",
                        "engine_id": "daft_ray",
                        "status": "ok",
                        "task_spec": {
                            "metadata": {
                                "sourceTaskSpecId": "pipeline_text@v0",
                                "measurementScope": "pipeline_e2e",
                            }
                        },
                        "metrics": {
                            "elapsed_s": 801.5,
                            "input_rows": 10000,
                            "output_rows": 9993,
                        },
                    }
                ),
                encoding="utf-8",
            )
            timing_path = root / "timing.json"

            _normalize_pipeline_timing(local_raw, timing_path, "arm")
            timing = json.loads(timing_path.read_text(encoding="utf-8"))

        self.assertEqual(len(timing["cases"]), 1)
        self.assertEqual(timing["cases"][0]["caseId"], "pipeline_text::daft_ray")
        self.assertEqual(
            timing["cases"][0]["metrics"]["wallClockTime"]["wall_clock_ns"],
            801_500_000_000,
        )
        self.assertIn("runner/pipeline_text/result.json", timing["cases"][0]["sourceArtifact"])

    def test_operator_analysis_parses_frozen_input_overrides(self) -> None:
        override = {
            "kind": "lance",
            "path": "fixtures/text/scale/fineweb_vectorize_10k_p4.lance",
            "jsonl_mirror": "fixtures/text/scale/fineweb_vectorize_10k.jsonl",
            "manifest_path": "fixtures/text/scale/fineweb_vectorize_10k.manifest.json",
            "field": "text",
            "rows": 10000,
            "input_fingerprint": "sha256:" + "a" * 64,
        }
        settings = _load_settings(
            {
                "group": "core_dual_engine",
                "operatorAnalysis": {
                    "tasks": ["pipeline_text"],
                    "inputOverrides": {"pipeline_text": override},
                },
            },
            {"software": {"volcOperatorSimRevision": REVISION}},
        )

        self.assertEqual(settings.task_input_overrides["pipeline_text"], override)
        documents = _apply_task_input_overrides(
            _target_inputs()["taskDocuments"], settings.task_input_overrides
        )
        self.assertEqual(documents["pipeline_text"]["input"], override)
        self.assertEqual(
            _target_inputs()["taskDocuments"]["pipeline_text"]["input"]["path"],
            "fixtures/text.lance",
        )

    def test_pdf_real_task_normalizes_legacy_bge_contract(self) -> None:
        source = {
            "pipeline_pdf_full_min": {
                "task_id": "pipeline_pdf_full_min@v0",
                "pipeline": [
                    {"dj_ops": "pdf_table_extract_mapper", "category": "mapper"},
                    {
                        "dj_ops": "bge_vectorize_mapper",
                        "category": "mapper",
                        "params": {"dim": 16},
                    },
                    {
                        "dj_ops": "write_lance",
                        "category": "sink",
                        "params": {"output_uri": "fixtures/pdf.lance", "mode": "overwrite"},
                    },
                ],
            }
        }

        normalized = _normalize_real_task_contracts(source)
        vector = normalized["pipeline_pdf_full_min"]["pipeline"][1]["params"]
        sink = normalized["pipeline_pdf_full_min"]["pipeline"][2]["params"]

        self.assertEqual(vector["dim"], 384)
        self.assertEqual(vector["model_name"], "all-MiniLM-L6-v2")
        self.assertTrue(vector["emit_vector"])
        self.assertEqual(sink["field"], "embedding")
        self.assertEqual(sink["vector_dim"], 384)
        self.assertEqual(
            normalized["pipeline_pdf_full_min"]["engine_overrides"]["into_partitions"],
            4,
        )
        self.assertEqual(source["pipeline_pdf_full_min"]["pipeline"][1]["params"], {"dim": 16})

    def test_scaled_input_routes_plan_p0_and_context_through_full_overlays(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "    topSymbols: 20\n",
                    "    topSymbols: 20\n"
                    "    tasks:\n"
                    "      - pipeline_text\n"
                    "    inputOverrides:\n"
                    "      pipeline_text:\n"
                    "        kind: lance\n"
                    "        path: fixtures/text/scale/fineweb_vectorize_10k_p4.lance\n"
                    "        jsonl_mirror: fixtures/text/scale/fineweb_vectorize_10k.jsonl\n"
                    "        manifest_path: fixtures/text/scale/fineweb_vectorize_10k.manifest.json\n"
                    "        field: text\n"
                    "        rows: 10000\n"
                    "        input_fingerprint: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().run_benchmark(project, run_dir, "arm")
                plan = json.loads(
                    (run_dir / "arm/operators/operator-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
                VolcOperatorSimAdapter().collect_context_timing(
                    project, run_dir, "arm"
                )
            pushed = {
                path.name: json.loads(path.read_text(encoding="utf-8"))
                for path in (run_dir / "arm/operators/overlays").glob(
                    "*__full_*.json"
                )
            }

        self.assertEqual(
            plan["tasks"][0]["operators"][0]["input"]["spec"]["rows"],
            10000,
        )
        commands = "\n".join(executor.commands)
        self.assertNotIn("run_all_pipelines.sh", commands)
        self.assertIn("run_perf_suite.py", commands)
        self.assertIn("__full_pipeline_e2e.json", commands)
        self.assertIn("__full_pipeline_context.json", commands)
        self.assertEqual(
            pushed["pipeline_text__full_pipeline_e2e.json"]["input"]["rows"],
            10000,
        )
        self.assertIn(
            "/pipeline_e2e/outputs/pipeline_text.lance",
            pushed["pipeline_text__full_pipeline_e2e.json"]["pipeline"][-1]["params"]["output_uri"],
        )

    def test_selected_tasks_route_p0_through_overlays_without_input_overrides(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            project.write_text(
                project.read_text(encoding="utf-8").replace(
                    "    topSymbols: 20\n",
                    "    topSymbols: 20\n"
                    "    tasks:\n"
                    "      - pipeline_text\n",
                ),
                encoding="utf-8",
            )
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().run_benchmark(project, run_dir, "arm")

            overlays = list(
                (run_dir / "arm/operators/overlays").glob(
                    "pipeline_text__full_pipeline_e2e.json"
                )
            )

        commands = "\n".join(executor.commands)
        self.assertNotIn("run_all_pipelines.sh", commands)
        self.assertIn("run_perf_suite.py", commands)
        self.assertIn("__full_pipeline_e2e.json", commands)
        self.assertEqual(len(overlays), 1)

    def test_operator_analysis_can_override_group_and_select_tasks(self) -> None:
        settings = _load_settings(
            {
                "group": "core_dual_engine",
                "profile": "smoke",
                "operatorAnalysis": {
                    "group": "dual_engine_candidate",
                    "tasks": ["text_corpus_minhash_dedup"],
                    "engines": ["daft_ray"],
                },
            },
            {
                "software": {
                    "volcOperatorSimRevision": REVISION,
                }
            },
        )

        self.assertEqual(settings.group, "core_dual_engine")
        self.assertEqual(settings.operator_group, "dual_engine_candidate")
        self.assertEqual(
            settings.operator_tasks,
            ("text_corpus_minhash_dedup",),
        )
        self.assertEqual(settings.operator_engines, ("daft_ray",))

    def test_case_inventory_uses_successful_runner_json_when_wrapper_status_is_null(self) -> None:
        class LocalExecutor:
            def run(
                self, command: str, timeout: int = 300, stream: bool = False
            ) -> subprocess.CompletedProcess[str]:
                del stream
                return subprocess.run(
                    command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )

        with tempfile.TemporaryDirectory() as tmp:
            scope = Path(tmp) / "operator_case_e2e"
            wrapper = (
                scope
                / "measured/20260716/x86_64/daft_ray/operator_case__001"
            )
            wrapper.mkdir(parents=True)
            (wrapper / "summary.json").write_text(
                json.dumps(
                    {
                        "case": "operator_case__001",
                        "engine": "daft_ray",
                        "status": None,
                        "returncode": 0,
                    }
                ),
                encoding="utf-8",
            )
            (wrapper / "perf.data").write_bytes(b"perf")
            runner = scope / "measured/runner/operator_case__001"
            runner.mkdir(parents=True)
            (runner / "result.json").write_text(
                json.dumps(
                    {
                        "engine_id": "daft_ray",
                        "status": "ok",
                        "metrics": {"elapsed_s": 1.0},
                    }
                ),
                encoding="utf-8",
            )

            inventory = _read_completed_capture_cases(
                LocalExecutor(), str(scope)
            )

        self.assertTrue(inventory[("operator_case__001", "daft_ray")]["ok"])
        self.assertTrue(
            inventory[("operator_case__001", "daft_ray")]["perfData"]
        )

    def test_case_inventory_recovers_native_flamegraph_without_summary(self) -> None:
        class LocalExecutor:
            def run(
                self, command: str, timeout: int = 300, stream: bool = False
            ) -> subprocess.CompletedProcess[str]:
                del stream
                return subprocess.run(
                    command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )

        case = "operator_case_perf__abc123__flamegraph_attempt_001__native_perf_fallback"
        with tempfile.TemporaryDirectory() as tmp:
            scope = Path(tmp) / "operator_case_perf"
            fallback = scope / "20260716/x86_64/daft_ray" / case
            fallback.mkdir(parents=True)
            (fallback / "cpu.svg").write_text("<svg/>", encoding="utf-8")
            (fallback / "flamegraph-metadata.json").write_text(
                json.dumps(
                    {
                        "engineId": "daft_ray",
                        "outputCase": case,
                        "sourceCase": "operator_case_perf__abc123__perf_attempt_002",
                    }
                ),
                encoding="utf-8",
            )

            inventory = _read_completed_capture_cases(
                LocalExecutor(), str(scope)
            )

        self.assertTrue(inventory[(case, "daft_ray")]["ok"])
        self.assertTrue(inventory[(case, "daft_ray")]["cpuSvg"])

    def test_transport_failed_case_is_classified_and_cleared_after_gap_resume(self) -> None:
        executor = FakeExecutor(
            fail_once_on="operator_case_e2e__",
            fail_once_returncode=255,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            failure_path = (
                run_dir
                / "arm/operators/raw/case-failures-operator_case_e2e.json"
            )
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                adapter = VolcOperatorSimAdapter()
                adapter.collect_operator_timing(project, run_dir, "arm")
                first_failure = json.loads(
                    failure_path.read_text(encoding="utf-8")
                )["cases"][0]
                adapter.collect_operator_timing(project, run_dir, "arm")
                failure_remains = failure_path.exists()

        self.assertTrue(first_failure["retryable"])
        self.assertEqual(first_failure["reason"], "ssh_transport")
        self.assertFalse(failure_remains)

    def test_transport_case_failure_invalidates_local_scope_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            manifests = platform_dir / "operators/manifests"
            raw = platform_dir / "operators/raw"
            manifests.mkdir(parents=True)
            raw.mkdir(parents=True)
            (manifests / "operator_case_e2e-COMPLETE.json").write_text(
                "{}", encoding="utf-8"
            )
            (raw / "case-failures-operator_case_e2e.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "cases": [
                            {
                                "status": "failed",
                                "error": "Volc operator_case_e2e failed (exit 255): timeout",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            skipped = _prepare_stage_resume(
                platform_dir, "operator_case_e2e", force=False
            )

        self.assertFalse(skipped)

    def test_transport_case_failure_disables_remote_complete_recovery(self) -> None:
        executor = FakeExecutor(remote_complete=True)
        with tempfile.TemporaryDirectory() as tmp:
            local_raw = Path(tmp)
            (local_raw / "case-failures-operator_case_e2e.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "cases": [
                            {
                                "status": "failed",
                                "retryable": True,
                                "reason": "ssh_transport",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            recover = _should_recover_remote(
                executor,
                "/host/run",
                local_raw,
                "operator_case_e2e",
                force=False,
            )

        self.assertFalse(recover)

    def test_overlay_upload_falls_back_to_atomic_ssh_write(self) -> None:
        executor = FakeExecutor(push_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, root / "run", "arm"
                )

        self.assertTrue(
            any(
                "PYFRAMEWORK_ATOMIC_OVERLAY_WRITE=ok" in command
                for command in executor.commands
            )
        )

    def test_snapshot_mirror_retries_transient_ssh_failure(self) -> None:
        executor = FakeExecutor()
        original_run = executor.run
        mirror_attempts = 0

        def transient_mirror_failure(
            command: str, timeout: int = 300, stream: bool = False
        ) -> subprocess.CompletedProcess[str]:
            nonlocal mirror_attempts
            if '"mediaCompatibility"' in command:
                mirror_attempts += 1
                if mirror_attempts == 1:
                    executor.commands.append(command)
                    return subprocess.CompletedProcess(
                        [], 255, "", "Timeout, server not responding"
                    )
            return original_run(command, timeout, stream)

        executor.run = transient_mirror_failure  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ), patch(
                "pyframework_pipeline.adapters.volcoperatorsim.acquisition.time.sleep"
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm"
                )
            failures = (
                run_dir
                / "arm/operators/raw/case-failures-operator_case_e2e.json"
            )
            failure_exists = failures.exists()

        self.assertEqual(mirror_attempts, 2)
        self.assertFalse(failure_exists)

    def test_snapshot_mirror_accepts_jsonl_source_manifest(self) -> None:
        settings = _load_settings(
            {"operatorAnalysis": {}},
            {
                "software": {
                    "volcOperatorSimRevision": REVISION,
                    "hostDataRoot": "/host/data",
                }
            },
        )
        command = _snapshot_mirror_command(
            settings,
            {
                "schemaVersion": 1,
                "snapshotId": "snapshot-1",
                "sourceRevision": REVISION,
                "producer": "daft_ray",
                "parentFingerprint": "sha256:input",
                "operatorSpecHash": "sha256:operator",
                "builderVersion": "4",
                "partitionPolicy": "inherit_if_declared",
            },
            {
                "input": {
                    "kind": "file_manifest",
                    "field": "file_path",
                    "manifest_path": "fixtures/ad/manifest.jsonl",
                }
            },
        )

        self.assertIn("except json.JSONDecodeError", command)

    def test_generated_files_prefer_atomic_ssh_write_over_scp(self) -> None:
        executor = FakeExecutor(push_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, root / "run", "arm"
                )

        self.assertEqual(executor.pushed, [])
        self.assertEqual(
            sum(
                "PYFRAMEWORK_ATOMIC_OVERLAY_WRITE=ok" in command
                for command in executor.commands
            ),
            1,
        )

    def test_snapshot_overlays_extend_previous_snapshot_without_replaying_prefix(self) -> None:
        target_inputs = _target_inputs()
        target_inputs["taskDocuments"]["pipeline_text"]["pipeline"].append(
            {"dj_ops": "tokenize_mapper", "category": "mapper"}
        )
        executor = FakeExecutor(target_inputs=target_inputs)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm"
                )

            operator_root = run_dir / "arm" / "operators"
            plan = json.loads(
                (operator_root / "operator-plan.json").read_text(encoding="utf-8")
            )
            snapshots = plan["tasks"][0]["snapshots"]
            overlay = json.loads(
                (
                    operator_root
                    / "overlays"
                    / "pipeline_text__snapshot_001.json"
                ).read_text(encoding="utf-8")
            )

        parent_id = snapshots[0]["snapshotId"]
        self.assertEqual(
            overlay["input"]["path"],
            f"/home/lxy/de_bench_full/operator-cache/{parent_id}/snapshot.lance",
        )
        self.assertEqual(
            [step["dj_ops"] for step in overlay["pipeline"]],
            ["text_length_filter", "write_lance"],
        )
        self.assertEqual(overlay["metadata"]["startOrder"], 1)
        self.assertEqual(overlay["metadata"]["throughOrder"], 1)

    def test_pinned_target_input_probe_retries_transient_ssh_failure(self) -> None:
        executor = FakeExecutor()
        original_run = executor.run
        attempts = 0

        def transient_probe_failure(
            command: str, timeout: int = 300, stream: bool = False
        ) -> subprocess.CompletedProcess[str]:
            nonlocal attempts
            if "PYFRAMEWORK_TARGET_INPUTS=" in command:
                attempts += 1
                if attempts == 1:
                    executor.commands.append(command)
                    return subprocess.CompletedProcess(
                        [], 255, "", "Timeout, server not responding"
                    )
            return original_run(command, timeout, stream)

        executor.run = transient_probe_failure  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ), patch(
                "pyframework_pipeline.adapters.volcoperatorsim.acquisition.time.sleep"
            ):
                plan = VolcOperatorSimAdapter().plan_operator_cases(
                    project, root / "run", "arm"
                )
                plan_exists = plan.is_file()

        self.assertTrue(plan_exists)
        self.assertEqual(attempts, 2)

    def test_context_resume_skips_each_completed_remote_case(self) -> None:
        completed = "pipeline_context__pipeline_text__daft_ray"
        executor = FakeExecutor(completed_cases={(completed, "daft_ray")})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_context_timing(
                    project, root / "run", "arm"
                )

        captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
        ]
        self.assertTrue(captures)
        self.assertFalse(any(completed in command for command in captures))
        self.assertTrue(
            any(
                "pipeline_context__pipeline_text__datajuicer_native" in command
                for command in captures
            )
        )

    def test_isolated_resume_skips_completed_warmup_and_measured_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=FakeExecutor(),
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            operator = plan["tasks"][0]["operators"][0]
            case_hash = hashlib.sha256(
                operator["operatorCaseId"].encode("utf-8")
            ).hexdigest()[:12]
            case_names = {
                f"operator_case_e2e__{case_hash}__warmup_001",
                *{
                    f"operator_case_e2e__{case_hash}__round_{number:03d}"
                    for number in range(1, 4)
                },
            }
            completed = {
                (case, engine)
                for case in case_names
                for engine in operator["engines"]
            }
            executor = FakeExecutor(completed_cases=completed)
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm"
                )

        captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
        ]
        self.assertTrue(captures)
        self.assertFalse(
            any(case in command for case in case_names for command in captures)
        )

    def test_profile_resume_skips_completed_perf_and_flamegraph_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=FakeExecutor(),
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            operator = plan["tasks"][0]["operators"][0]
            case_hash = hashlib.sha256(
                operator["operatorCaseId"].encode("utf-8")
            ).hexdigest()[:12]
            case_names = {
                f"operator_case_perf__{case_hash}__perf_attempt_001",
                f"operator_case_perf__{case_hash}__perf_attempt_002",
                f"operator_case_perf__{case_hash}__flamegraph_attempt_001",
            }
            completed = {
                (case, engine)
                for case in case_names
                for engine in operator["engines"]
            }
            executor = FakeExecutor(completed_cases=completed)
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

        captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
            and case_hash in command
        ]
        self.assertEqual(captures, [])
        self.assertFalse(
            any(
                "perf annotate --stdio" in command and case_hash in command
                for command in executor.commands
            )
        )

    def test_profile_resume_uses_completed_second_attempt_without_repeating_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=FakeExecutor(),
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            completed: set[tuple[str, str]] = set()
            first_attempts: set[str] = set()
            for operator in plan["tasks"][0]["operators"]:
                case_hash = hashlib.sha256(
                    operator["operatorCaseId"].encode("utf-8")
                ).hexdigest()[:12]
                first_attempts.add(
                    f"operator_case_perf__{case_hash}__perf_attempt_001"
                )
                for engine in operator["engines"]:
                    completed.add(
                        (
                            f"operator_case_perf__{case_hash}__perf_attempt_002",
                            engine,
                        )
                    )
                    completed.add(
                        (
                            f"operator_case_perf__{case_hash}__flamegraph_attempt_001",
                            engine,
                        )
                    )
            executor = FakeExecutor(completed_cases=completed)

            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

        captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
        ]
        self.assertFalse(
            any(case in command for case in first_attempts for command in captures)
        )

    def test_profile_circuit_breaks_pyspy_per_engine_after_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            executor = FakeExecutor(fail_on="-e MODE=profile")
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=executor,
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

        pyspy_captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
            and "-e MODE=profile" in command
        ]
        native_fallbacks = [
            command
            for command in executor.commands
            if "PYFRAMEWORK_NATIVE_FLAMEGRAPH=fallback" in command
        ]
        self.assertEqual(len(pyspy_captures), 2)
        self.assertTrue(any("ENGINE=daft_ray" in command for command in pyspy_captures))
        self.assertTrue(any("ENGINE=datajuicer_native" in command for command in pyspy_captures))
        self.assertEqual(len(native_fallbacks), 4)

    def test_profile_resume_keeps_pyspy_for_engine_with_later_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=FakeExecutor(),
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            first_operator = plan["tasks"][0]["operators"][0]
            first_hash = hashlib.sha256(
                first_operator["operatorCaseId"].encode("utf-8")
            ).hexdigest()[:12]
            flame_case = (
                f"operator_case_perf__{first_hash}__flamegraph_attempt_001"
            )
            completed = {
                (
                    f"{flame_case}__native_perf_fallback",
                    "daft_ray",
                ),
                (
                    f"{flame_case}__native_perf_fallback",
                    "datajuicer_native",
                ),
                (flame_case, "datajuicer_native"),
            }
            executor = FakeExecutor(completed_cases=completed)
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=executor,
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

        pyspy_captures = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
            and "-e MODE=profile" in command
        ]
        self.assertTrue(pyspy_captures)
        self.assertTrue(
            all("ENGINE=datajuicer_native" in command for command in pyspy_captures)
        )

    def test_profile_success_clears_earlier_native_circuit_breaker(self) -> None:
        target_inputs = _target_inputs()
        target_inputs["taskDocuments"]["pipeline_text"]["pipeline"].append(
            {
                "dj_ops": "text_length_filter",
                "category": "filter",
                "params": {"min_len": 1},
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=FakeExecutor(target_inputs=target_inputs),
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            operators = json.loads(
                plan_path.read_text(encoding="utf-8")
            )["tasks"][0]["operators"]
            first_hash, second_hash = [
                hashlib.sha256(operator["operatorCaseId"].encode("utf-8"))
                .hexdigest()[:12]
                for operator in operators[:2]
            ]
            completed = {
                (
                    f"operator_case_perf__{first_hash}__flamegraph_attempt_001"
                    "__native_perf_fallback",
                    "datajuicer_native",
                ),
                (
                    f"operator_case_perf__{second_hash}__flamegraph_attempt_001",
                    "datajuicer_native",
                ),
            }
            executor = FakeExecutor(
                completed_cases=completed,
                target_inputs=target_inputs,
            )
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=executor,
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

        datajuicer_pyspy = [
            command
            for command in executor.commands
            if "bash scripts/capture/bench_capture.sh" in command
            and "-e MODE=profile" in command
            and "ENGINE=datajuicer_native" in command
        ]
        self.assertEqual(len(datajuicer_pyspy), 1)

    def test_legacy_marker_failure_is_a_collection_success_receipt(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            local_raw = Path(tmp)
            scope = "pipeline_e2e"
            remote_root = "/host/run"
            (local_raw / f"acquisition-failure-{scope}.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "scope": scope,
                        "remoteRoot": remote_root,
                        "errorType": "StepError",
                        "error": (
                            "failed to write remote pipeline_e2e COMPLETE marker"
                        ),
                    }
                ),
                encoding="utf-8",
            )

            recover = _should_recover_remote(
                executor,
                remote_root,
                local_raw,
                scope,
                force=False,
            )

        self.assertTrue(recover)

    def test_complete_marker_retries_transient_ssh_failure(self) -> None:
        executor = FakeExecutor()
        original_run = executor.run
        marker_attempts = 0

        def transient_marker_failure(
            command: str, timeout: int = 300, stream: bool = False
        ) -> subprocess.CompletedProcess[str]:
            nonlocal marker_attempts
            if 'complete_payload={"schemaVersion"' in command:
                marker_attempts += 1
                if marker_attempts == 1:
                    executor.commands.append(command)
                    return subprocess.CompletedProcess(
                        [], 255, "", "Timeout, server not responding"
                    )
            return original_run(command, timeout, stream)

        executor.run = transient_marker_failure  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ), patch(
                "pyframework_pipeline.adapters.volcoperatorsim.acquisition.time.sleep"
            ):
                VolcOperatorSimAdapter().collect_context_timing(
                    project, run_dir, "arm"
                )

        self.assertEqual(marker_attempts, 2)

    def test_collection_receipt_recovers_after_marker_outage_without_rerun(self) -> None:
        executor = FakeExecutor()
        original_run = executor.run
        fail_markers = True

        def marker_outage(
            command: str, timeout: int = 300, stream: bool = False
        ) -> subprocess.CompletedProcess[str]:
            if fail_markers and 'complete_payload={"schemaVersion"' in command:
                executor.commands.append(command)
                return subprocess.CompletedProcess(
                    [], 255, "", "Timeout, server not responding"
                )
            return original_run(command, timeout, stream)

        executor.run = marker_outage  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ), patch(
                "pyframework_pipeline.adapters.volcoperatorsim.acquisition.time.sleep"
            ):
                with self.assertRaisesRegex(StepError, "COMPLETE marker"):
                    VolcOperatorSimAdapter().collect_context_timing(
                        project, run_dir, "arm"
                    )
                first_capture_count = sum(
                    "MODE=timing" in command for command in executor.commands
                )
                receipt = (
                    run_dir
                    / "arm/operators/raw/collection-succeeded-pipeline_context.json"
                )
                self.assertTrue(receipt.is_file())

                fail_markers = False
                VolcOperatorSimAdapter().collect_context_timing(
                    project, run_dir, "arm"
                )
                second_capture_count = sum(
                    "MODE=timing" in command for command in executor.commands
                )

        self.assertGreater(first_capture_count, 0)
        self.assertEqual(second_capture_count, first_capture_count)

    def test_runs_all_four_scopes_through_target_scripts_and_fetches_host_results(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                timing_path = VolcOperatorSimAdapter().run_benchmark(
                    project, run_dir, "arm"
                )

            commands = "\n".join(executor.commands)
            timing = json.loads(timing_path.read_text(encoding="utf-8"))
            plan = json.loads(
                (run_dir / "arm" / "operators" / "operator-plan.json").read_text(
                    encoding="utf-8"
                )
            )
            overlay_documents = [
                json.loads(local.read_text(encoding="utf-8"))
                for local, _remote in executor.pushed
                if local.suffix == ".json"
            ]

        self.assertIn("scripts/pipelines/run_all_pipelines.sh", commands)
        self.assertIn("GROUP=core_dual_engine", commands)
        self.assertIn("PROFILE_NAME=smoke", commands)
        self.assertIn("ROUNDS=1", commands)
        self.assertNotIn("pipeline_context", commands)
        self.assertNotIn("snapshot_build", commands)
        self.assertNotIn("operator_case_e2e", commands)
        self.assertNotIn("MODE=perfrecord", commands)
        self.assertNotIn("MODE=profile", commands)
        self.assertIn(
            "PERF_EVENTS=cycles,instructions,cache-references,cache-misses,"
            "L1-dcache-loads,L1-dcache-load-misses,LLC-loads,LLC-load-misses,"
            "branches,branch-misses,context-switches,cpu-migrations,page-faults",
            commands,
        )
        self.assertIn("/home/lxy/de_bench_full/bench-results/pyframework/", commands)
        self.assertTrue(executor.fetches)
        self.assertEqual(timing["cases"][0]["metrics"]["wallClockTime"]["wall_clock_ns"], 1_250_000_000)
        self.assertEqual(plan["sourceRevision"], REVISION)
        self.assertEqual(overlay_documents, [])

    def test_independent_operator_stages_do_not_reexecute_other_scopes(self) -> None:
        adapter = VolcOperatorSimAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")

            plan_executor = FakeExecutor()
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=plan_executor,
            ):
                plan_path = adapter.plan_operator_cases(project, root / "plan", "arm")
            self.assertTrue(plan_path.is_file())
            self.assertNotIn(
                "run_all_pipelines.sh", "\n".join(plan_executor.commands)
            )

            context_executor = FakeExecutor()
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=context_executor,
            ):
                adapter.collect_context_timing(project, root / "context", "arm")
            context_commands = "\n".join(context_executor.commands)
            self.assertIn("pipeline_context", context_commands)
            self.assertNotIn("run_all_pipelines.sh", context_commands)
            self.assertNotIn("MODE=perfrecord", context_commands)

            isolated_executor = FakeExecutor()
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=isolated_executor,
            ):
                adapter.collect_operator_timing(project, root / "isolated", "arm")
            isolated_commands = "\n".join(isolated_executor.commands)
            self.assertIn("snapshot_build", isolated_commands)
            self.assertIn("operator_case_e2e", isolated_commands)
            self.assertIn("PYFRAMEWORK_RUNNER_RESULT_STATUS=ok", isolated_commands)
            self.assertIn('payload.get("engine_id")', isolated_commands)
            self.assertNotIn("MODE=perfrecord", isolated_commands)
            self.assertNotIn("MODE=profile", isolated_commands)

            profile_executor = FakeExecutor()
            with patch(
                "pyframework_pipeline.remote.build_executor",
                return_value=profile_executor,
            ):
                adapter.collect_operator_profiles(project, root / "profile", "arm")
            profile_commands = "\n".join(profile_executor.commands)
            self.assertIn("MODE=perfrecord", profile_commands)
            self.assertIn("MODE=profile", profile_commands)
            self.assertIn(
                "timeout --signal=TERM --kill-after=10s 90s "
                "bash scripts/capture/bench_capture.sh",
                profile_commands,
            )
            self.assertNotIn("run_all_pipelines.sh", profile_commands)
            self.assertNotRegex(profile_commands, r"operator_case_e2e__.+__round_")
            self.assertEqual(
                [Path(remote).name for remote, _local, _timeout in profile_executor.fetches],
                ["snapshot_build", "operator_case_perf", "manifests"],
            )
            self.assertEqual(
                [timeout for _remote, _local, timeout in profile_executor.fetches],
                [14400, 14400, 14400],
            )

    def test_pyspy_failure_falls_back_to_native_perf_flamegraph_and_keeps_annotate(self) -> None:
        executor = FakeExecutor(fail_on="MODE=profile")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

            commands = "\n".join(executor.commands)
            failure_path = (
                run_dir
                / "arm/operators/raw/case-failures-operator_case_perf.json"
            )

        self.assertIn("PYFRAMEWORK_NATIVE_FLAMEGRAPH", commands)
        self.assertIn("native_flamegraph.py", commands)
        self.assertIn(" annotate --stdio", commands)
        self.assertIn("perf_symbol_bundle.py", commands)
        self.assertIn("PYFRAMEWORK_PERF_SYMBOL_CACHE", commands)
        self.assertIn("PYFRAMEWORK_PERF_MAP_POLL_INTERVAL=0.005", commands)
        self.assertIn("--buildid-mmap", commands)
        self.assertIn("--require-complete", commands)
        self.assertIn("perf-report-period-resolved.txt", commands)
        self.assertIn("--fields overhead,period,sample,comm,pid,dso,symbol,addr", commands)
        self.assertIn("sort -nr | head -n 1", commands)
        self.assertIn("--percent-limit=0.5", commands)
        self.assertIn(".perf-annotate.txt.partial", commands)
        self.assertIn('mv "$dir/.perf-annotate.txt.partial"', commands)
        self.assertFalse(failure_path.exists())

    def test_remote_complete_profile_recovers_by_fetching_without_rerunning(self) -> None:
        executor = FakeExecutor(remote_complete=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm"
                )

            commands = "\n".join(executor.commands)
            complete = (
                run_dir
                / "arm/operators/manifests/operator_case_perf-COMPLETE.json"
            )
            complete_exists = complete.is_file()

        self.assertNotIn("MODE=perfrecord", commands)
        self.assertNotIn("MODE=profile", commands)
        self.assertEqual(
            [Path(remote).name for remote, _local, _timeout in executor.fetches],
            ["snapshot_build", "operator_case_perf", "manifests"],
        )
        self.assertTrue(complete_exists)

    def test_failure_still_fetches_partial_host_evidence(self) -> None:
        executor = FakeExecutor(fail_on="pipeline_context")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                with self.assertRaisesRegex(StepError, "pipeline_context"):
                    VolcOperatorSimAdapter().collect_context_timing(
                        project, run_dir, "arm"
                    )

            evidence = (
                run_dir
                / "arm/operators/raw/pipeline_context/fetch-complete.txt"
            )
            self.assertEqual(evidence.read_text(encoding="utf-8"), "kept")
            self.assertTrue(executor.fetches)

    def test_revision_mismatch_fails_before_target_benchmark(self) -> None:
        executor = FakeExecutor()
        original_run = executor.run

        def mismatched(command: str, timeout: int = 300, stream: bool = False):
            result = original_run(command, timeout, stream)
            if "PYFRAMEWORK_TARGET_INPUTS=" in command:
                payload = _target_inputs()
                payload["revision"] = "0" * 40
                encoded = base64.b64encode(json.dumps(payload).encode()).decode()
                return subprocess.CompletedProcess([], 0, f"PYFRAMEWORK_TARGET_INPUTS={encoded}\n", "")
            return result

        executor.run = mismatched  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                with self.assertRaisesRegex(StepError, "revision mismatch"):
                    VolcOperatorSimAdapter().run_benchmark(project, root / "run", "arm")

        self.assertNotIn(
            "scripts/pipelines/run_all_pipelines.sh", "\n".join(executor.commands)
        )

    def test_generic_collect_substeps_use_adapter_owned_operator_artifacts(self) -> None:
        from pyframework_pipeline.orchestrator import _run_collect_substep

        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                adapter = VolcOperatorSimAdapter()
                adapter.run_benchmark(project, run_dir, "arm")
                adapter.collect_context_timing(project, run_dir, "arm")
                adapter.collect_operator_timing(project, run_dir, "arm")
                adapter.collect_operator_profiles(project, run_dir, "arm")
                from pyframework_pipeline.adapters.volcoperatorsim.acquisition_manifest import (
                    build_acquisition_manifest,
                )
                build_acquisition_manifest(run_dir / "arm", platform="arm")
                adapter.normalize_operator_artifacts(project, run_dir, "arm")

            command_count = len(executor.commands)
            with patch(
                "pyframework_pipeline.remote.build_executor",
                side_effect=AssertionError("generic remote collector must not run"),
            ):
                for substep in ("5b.1", "5b.2", "5b.2b", "5b.3"):
                    _run_collect_substep(project, run_dir, "arm", substep)

            self.assertEqual(len(executor.commands), command_count)

    def test_case_failure_is_recorded_and_later_operator_cases_continue(self) -> None:
        executor = FakeExecutor(fail_once_on="operator_case_e2e__")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm"
                )

            failures = json.loads(
                (
                    run_dir
                    / "arm/operators/raw/case-failures-operator_case_e2e.json"
                ).read_text(
                    encoding="utf-8"
                )
            )
            commands = "\n".join(executor.commands)

        self.assertEqual(len(failures["cases"]), 1)
        self.assertEqual(failures["cases"][0]["status"], "failed")
        self.assertGreater(commands.count("operator_case_e2e__"), 1)

    def test_snapshot_failure_blocks_only_dependent_operator(self) -> None:
        executor = FakeExecutor(fail_once_on="snapshot_build__pipeline_text__000")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_timing(
                    project, run_dir, "arm"
                )
            failures = json.loads(
                (
                    run_dir
                    / "arm/operators/raw/case-failures-operator_case_e2e.json"
                ).read_text(
                    encoding="utf-8"
                )
            )["cases"]
            commands = "\n".join(executor.commands)

        self.assertIn("failed", {item["status"] for item in failures})
        self.assertIn("blocked", {item["status"] for item in failures})
        self.assertIn("operator_case_e2e__102230ab2cf9", commands)
        self.assertNotIn("operator_case_e2e__4dffeb38e776", commands)

    def test_complete_marker_skips_stage_unless_force_is_requested(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            adapter = VolcOperatorSimAdapter()
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                adapter.collect_context_timing(project, run_dir, "arm")
                first_count = len(executor.commands)
                adapter.collect_context_timing(project, run_dir, "arm")
                skipped_count = len(executor.commands)
                adapter.collect_context_timing(
                    project, run_dir, "arm", force=True
                )

            complete = run_dir / "arm/operators/manifests/pipeline_context-COMPLETE.json"
            complete_exists = complete.is_file()

        self.assertTrue(complete_exists)
        self.assertEqual(first_count, skipped_count)
        self.assertGreater(len(executor.commands), skipped_count)

    def test_complete_marker_removes_stale_acquisition_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            manifests = platform_dir / "operators/manifests"
            local_raw = platform_dir / "operators/raw"
            manifests.mkdir(parents=True)
            local_raw.mkdir(parents=True)
            (manifests / "pipeline_context-COMPLETE.json").write_text(
                "{}\n", encoding="utf-8"
            )
            failure = local_raw / "acquisition-failure-pipeline_context.json"
            failure.write_text('{"error":"old transport failure"}\n', encoding="utf-8")

            skipped = _prepare_stage_resume(
                platform_dir, "pipeline_context", force=False
            )

            self.assertTrue(skipped)
            self.assertFalse(failure.exists())

    def test_complete_marker_keeps_failure_while_transport_cases_need_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            platform_dir = Path(tmp) / "arm"
            manifests = platform_dir / "operators/manifests"
            local_raw = platform_dir / "operators/raw"
            manifests.mkdir(parents=True)
            local_raw.mkdir(parents=True)
            (manifests / "pipeline_context-COMPLETE.json").write_text(
                "{}\n", encoding="utf-8"
            )
            failure = local_raw / "acquisition-failure-pipeline_context.json"
            failure.write_text('{"error":"old transport failure"}\n', encoding="utf-8")
            (local_raw / "case-failures-pipeline_context.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "reason": "ssh_transport",
                                "retryable": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            skipped = _prepare_stage_resume(
                platform_dir, "pipeline_context", force=False
            )

            self.assertFalse(skipped)
            self.assertTrue(failure.exists())

    def test_force_rerun_archives_prior_scope_before_collecting(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            adapter = VolcOperatorSimAdapter()
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                adapter.run_benchmark(project, run_dir, "arm")
                stale = (
                    run_dir
                    / "arm/operators/raw/pipeline_e2e/stale-attempt.json"
                )
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_text("old", encoding="utf-8")
                before = len(executor.commands)
                adapter.run_benchmark(project, run_dir, "arm", force=True)

            rerun_commands = executor.commands[before:]
            archive_index = next(
                index
                for index, command in enumerate(rerun_commands)
                if "PYFRAMEWORK_SCOPE_ARCHIVE=" in command
            )
            collect_index = next(
                index
                for index, command in enumerate(rerun_commands)
                if "run_all_pipelines.sh" in command
            )
            quarantined = list(
                (run_dir / "arm/operators/quarantine").glob(
                    "*/pipeline_e2e/stale-attempt.json"
                )
            )
            stale_exists = stale.exists()
            quarantined_contents = [
                item.read_text(encoding="utf-8") for item in quarantined
            ]

        self.assertLess(archive_index, collect_index)
        self.assertFalse(stale_exists)
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined_contents, ["old"])

    def test_force_profile_rerun_preserves_existing_e2e_evidence(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            raw_root = run_dir / "arm/operators/raw"
            e2e = raw_root / "operator_case_e2e/kept.json"
            perf = raw_root / "operator_case_perf/replaced.json"
            e2e.parent.mkdir(parents=True)
            perf.parent.mkdir(parents=True)
            e2e.write_text("e2e", encoding="utf-8")
            perf.write_text("old-perf", encoding="utf-8")

            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, run_dir, "arm", force=True
                )

            archived_perf = list(
                (run_dir / "arm/operators/quarantine").glob(
                    "*/operator_case_perf/replaced.json"
                )
            )

            self.assertEqual(e2e.read_text(encoding="utf-8"), "e2e")
            self.assertFalse(perf.exists())
            self.assertEqual(len(archived_perf), 1)
            self.assertEqual(
                archived_perf[0].read_text(encoding="utf-8"), "old-perf"
            )
            self.assertNotIn(
                "operator_case_e2e",
                "\n".join(
                    command
                    for command in executor.commands
                    if "PYFRAMEWORK_SCOPE_ARCHIVE=" in command
                ),
            )

    def test_plan_embeds_environment_and_run_fingerprints(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            record = run_dir / "arm/environment-record.json"
            record.parent.mkdir(parents=True)
            record.write_text(
                json.dumps({"imageId": "sha256:image", "containerId": "cid"}),
                encoding="utf-8",
            )
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                plan_path = VolcOperatorSimAdapter().plan_operator_cases(
                    project, run_dir, "arm"
                )
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            fingerprint = json.loads(
                (run_dir / "arm/operators/run-fingerprint.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertRegex(plan["environmentFingerprintSha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            plan["runFingerprintSha256"], fingerprint["runFingerprintSha256"]
        )

    def test_remote_run_root_ignores_volatile_environment_fields_but_tracks_image(self) -> None:
        executor = FakeExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            run_dir = root / "run"
            record = run_dir / "arm/environment-record.json"
            record.parent.mkdir(parents=True)
            adapter = VolcOperatorSimAdapter()
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                record.write_text(
                    json.dumps(
                        {
                            "environmentFingerprint": {
                                "imageId": "sha256:first",
                                "containerId": "one",
                                "capturedAtEpochNs": 1,
                            },
                            "startedAt": "first",
                        }
                    ),
                    encoding="utf-8",
                )
                adapter.run_benchmark(project, run_dir, "arm", force=True)
                first_remote_root = executor.fetches[-1][0]

                record.write_text(
                    json.dumps(
                        {
                            "environmentFingerprint": {
                                "imageId": "sha256:first",
                                "containerId": "two",
                                "capturedAtEpochNs": 2,
                            },
                            "startedAt": "second",
                        }
                    ),
                    encoding="utf-8",
                )
                adapter.run_benchmark(project, run_dir, "arm", force=True)
                same_environment_remote_root = executor.fetches[-1][0]

                record.write_text(
                    json.dumps(
                        {
                            "environmentFingerprint": {
                                "imageId": "sha256:second",
                                "containerId": "three",
                                "capturedAtEpochNs": 3,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                adapter.run_benchmark(project, run_dir, "arm", force=True)
                changed_image_remote_root = executor.fetches[-1][0]

        self.assertEqual(first_remote_root, same_environment_remote_root)
        self.assertNotEqual(first_remote_root, changed_image_remote_root)

    def test_insufficient_perf_samples_trigger_a_unique_retry_attempt(self) -> None:
        class SampleExecutor(FakeExecutor):
            def __init__(self):
                super().__init__()
                self.probes = 0

            def run(self, command, timeout=300, stream=False):
                if "PYFRAMEWORK_PERF_SAMPLE_COUNT" in command:
                    self.commands.append(command)
                    self.probes += 1
                    count = 100 if self.probes % 2 else 6000
                    return subprocess.CompletedProcess(
                        [], 0, f"PYFRAMEWORK_PERF_SAMPLE_COUNT={count}\n", ""
                    )
                return super().run(command, timeout, stream)

        executor = SampleExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, root / "run", "arm"
                )
            commands = "\n".join(executor.commands)

        self.assertIn("attempt_001", commands)
        self.assertIn("attempt_002", commands)
        self.assertIn("PERF_FREQ=990", commands)

    def test_unknown_perf_sample_count_also_triggers_the_bounded_retry(self) -> None:
        class UnknownSampleExecutor(FakeExecutor):
            def run(self, command, timeout=300, stream=False):
                if "PYFRAMEWORK_PERF_SAMPLE_COUNT" in command:
                    self.commands.append(command)
                    return subprocess.CompletedProcess([], 0, "unknown\n", "")
                return super().run(command, timeout, stream)

        executor = UnknownSampleExecutor()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = _write_volc_project(root / "project")
            with patch(
                "pyframework_pipeline.remote.build_executor", return_value=executor
            ):
                VolcOperatorSimAdapter().collect_operator_profiles(
                    project, root / "run", "arm"
                )
            commands = "\n".join(executor.commands)

        self.assertIn("attempt_002", commands)
        self.assertIn("PERF_FREQ=990", commands)


if __name__ == "__main__":
    unittest.main()
