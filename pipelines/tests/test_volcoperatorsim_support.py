"""Framework, environment, and configuration tests for Volc Operator Sim."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "pipelines"))


class CliInvoker:
    @staticmethod
    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "pyframework_pipeline", *args],
            cwd=REPO_ROOT,
            env={"PYTHONPATH": str(REPO_ROOT / "pipelines")},
            text=True,
            capture_output=True,
            check=False,
        )


class VolcAdapterRegistrationTest(unittest.TestCase):
    def test_pipeline_run_cli_exposes_platform_filter(self) -> None:
        result = CliInvoker.run("run", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--platform", result.stdout)

    def test_builtin_adapter_exposes_operator_analysis_capabilities(self) -> None:
        from pyframework_pipeline.adapters.registry import adapter_names, get_adapter

        self.assertIn("volcoperatorsim", adapter_names())
        adapter = get_adapter("volcoperatorsim")
        capabilities = adapter.operator_capabilities()
        self.assertTrue(capabilities.context_timing)
        self.assertTrue(capabilities.isolated_timing)
        self.assertTrue(capabilities.operator_perf)
        self.assertTrue(capabilities.operator_asm)

        from pyframework_pipeline.contracts.adapter import OperatorAnalysisAdapter

        self.assertIsInstance(adapter, OperatorAnalysisAdapter)

    def test_environment_adapter_is_available_through_cli_loader(self) -> None:
        from pyframework_pipeline.cli._common import load_adapter

        adapter = load_adapter("volcoperatorsim")
        self.assertEqual(adapter.framework_id, "volcoperatorsim")

    def test_operator_cli_exposes_all_independent_stages(self) -> None:
        result = CliInvoker.run("operator", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("plan", "run", "normalize", "compare"):
            self.assertIn(command, result.stdout)

        run_help = CliInvoker.run("operator", "run", "--help")
        self.assertEqual(run_help.returncode, 0, run_help.stderr)
        for mode in ("context", "isolated", "profile", "all"):
            self.assertIn(mode, run_help.stdout)


class VolcEnvironmentPlanTest(unittest.TestCase):
    def test_perf_sysctl_step_is_declared_as_a_host_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_yaml = _write_volc_project(Path(tmp))
            result = CliInvoker.run(
                "environment", "plan", str(project_yaml), "--platform", "arm"
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        step = next(
            item for item in plan["steps"] if item["id"] == "enable-perf-paranoid"
        )
        self.assertTrue(step["mutatesHost"])
        self.assertTrue(step["requiresApproval"])
        self.assertTrue(step["requiresPrivilege"])
        self.assertIn("sysctl -w", step["command"])
        self.assertIn("sysctl -w", step["rollbackHint"])

    def test_environment_schema_declares_all_volc_software_fields(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "schemas" / "environment.schema.json").read_text(
                encoding="utf-8"
            )
        )
        properties = schema["properties"]["software"]["properties"]
        expected = {
            "volcOperatorSimRepo",
            "volcOperatorSimRevision",
            "volcOperatorSimImage",
            "volcOperatorSimImages",
            "volcOperatorSimContainer",
            "volcOperatorSimBaseImage",
            "volcDebianMirrorHost",
            "volcMiniforgeUrlTemplate",
            "volcMiniforgeSha256s",
            "volcPytorchCpuIndexUrl",
            "hostDataRoot",
            "dataSourceManifest",
            "daftCondaEnv",
            "dataJuicerCondaEnv",
            "shmSize",
            "volcCpuSets",
            "volcObserverCpuSets",
            "volcMemoryNodes",
            "volcVirtualization",
            "volcNofileSoft",
            "volcNofileHard",
            "perfFrequency",
            "perfEvents",
            "minHostFreeGiB",
            "volcPrivileged",
        }

        self.assertTrue(expected.issubset(properties))
        self.assertEqual(
            properties["volcOperatorSimRevision"]["pattern"], "^[0-9a-fA-F]{40}$"
        )
        self.assertEqual(properties["perfFrequency"]["minimum"], 1)
        self.assertEqual(properties["minHostFreeGiB"]["minimum"], 0)
        self.assertEqual(properties["volcPrivileged"]["type"], "boolean")
        self.assertEqual(
            properties["volcMiniforgeSha256s"]["additionalProperties"]["pattern"],
            "^[0-9a-fA-F]{64}$",
        )

    def test_reference_environment_includes_audio_row_isolation_fix(self) -> None:
        from pyframework_pipeline.environment.parser import load_environment_yaml

        environment = load_environment_yaml(
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "environment.yaml.example"
        )
        software = environment["software"]

        self.assertEqual(
            software["volcOperatorSimRevision"],
            "c0c52fd514510bc223d76767e55bcefbc190033c",
        )
        self.assertEqual(
            software["volcOperatorSimImages"],
            {
                "arm": "volc-operator-sim-bench:c0c52fd5-aarch64",
                "x86": "volc-operator-sim-bench:c0c52fd5-x86_64",
            },
        )

    def test_versioned_reference_project_is_valid(self) -> None:
        from pyframework_pipeline.config import validate_pipeline_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        report = validate_pipeline_config(project, require_bridge_token=False)

        self.assertEqual(report["status"], "ok", report["issues"])
        self.assertEqual(report["issueCount"], 0)

    def test_reference_project_defaults_to_six_modality_pipelines(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        workload = payload["workload"]
        analysis = workload["operatorAnalysis"]

        self.assertEqual(workload["testSet"], "default_6")
        self.assertEqual(workload["group"], "target_14")
        self.assertEqual(analysis["group"], "target_14")
        self.assertEqual(
            analysis["tasks"],
            [
                "pipeline_text_fineweb_full_min",
                "video_scene_split_etl",
                "pipeline_image_full_min",
                "audio_asr_prep_canonical",
                "pipeline_pdf_full_min",
                "pipeline_ad_nuscenes_min",
            ],
        )
        overrides = analysis["inputOverrides"]
        self.assertEqual(
            overrides["pipeline_text_fineweb_full_min"],
            {
                "kind": "lance",
                "field": "text",
                "path": "fixtures/text/scale/fineweb_edu_50k_p4/dataset_p4.lance",
                "jsonl_mirror": "fixtures/text/scale/fineweb_edu_50k_p4/dataset.jsonl",
                "manifest_path": "fixtures/text/scale/fineweb_edu_50k_p4/fixture-manifest.json",
                "fixture_id": "fineweb_edu_50k_p4",
                "rows": 50000,
                "input_fingerprint": "sha256:586d8cac2e8665beeae328b572b3e0c262fc007d6bf9685676d7a2d31c67a6ec",
            },
        )
        self.assertEqual(
            overrides["video_scene_split_etl"]["source_dataset"],
            "panda_70m",
        )
        self.assertEqual(
            overrides["video_scene_split_etl"]["path"],
            "fixtures/canonical/panda_70m_video.lance",
        )
        self.assertEqual(overrides["video_scene_split_etl"]["rows"], 8)
        self.assertEqual(
            overrides["video_scene_split_etl"]["input_fingerprint"],
            "sha256:cad57d309f3fcadfbc721887eac25766d4d5b63053d7bc89b2c25a4d7ae306e3",
        )
        self.assertEqual(
            overrides["audio_asr_prep_canonical"]["source_dataset"],
            "common_voice",
        )
        self.assertEqual(
            overrides["audio_asr_prep_canonical"]["path"],
            "fixtures/audio/scale/common_voice_cv26_en_test_512/common_voice_cv26_en_test_512_audio.lance",
        )
        self.assertEqual(overrides["audio_asr_prep_canonical"]["rows"], 512)
        self.assertEqual(
            overrides["audio_asr_prep_canonical"]["input_fingerprint"],
            "sha256:b6114e0eb8ca83265cb52732651396b8feb4ab86e0770369e34375b75bee5e4c",
        )
        self.assertNotIn(
            "manifest_path",
            overrides["audio_asr_prep_canonical"],
            "the row-oriented JSONL source is not a single JSON fixture manifest",
        )
        self.assertEqual(
            overrides["pipeline_pdf_full_min"],
            {
                "kind": "lance",
                "field": "file_path",
                "path": "fixtures/pdf/scale/pmc_pdf_hash4/pdfs_p4.lance",
                "jsonl_mirror": "fixtures/pdf/scale/pmc_pdf_hash4/manifest.jsonl",
                "manifest_path": "fixtures/pdf/scale/pmc_pdf_hash4/fixture-manifest.json",
                "fixture_id": "pmc_pdf_hash4",
                "rows": 4,
                "input_fingerprint": "sha256:2ae67663b803198f9e6e22671ae9067b227db4e3763783d3652d8f6be4a34fa4",
            },
        )
        self.assertEqual(
            overrides["pipeline_ad_nuscenes_min"],
            {
                "kind": "file_manifest",
                "field": "file_path",
                "path": "fixtures/ad/scale/nuscenes_cam_front_5m/manifest.jsonl",
                "manifest_path": "fixtures/ad/scale/nuscenes_cam_front_5m/manifest.jsonl",
                "fixture_id": "nuscenes_cam_front_5m",
                "rows": 5000000,
                "input_fingerprint": "sha256:891fba38c95045eb729daf183b6463192e23ec4937ea424ee9292806cbec2500",
            },
        )
        self.assertTrue(analysis["contextPerf"]["enabled"])
        self.assertTrue(analysis["contextPerf"]["splitByOperatorBoundary"])
        self.assertEqual(analysis["contextPerf"]["perfFrequency"], 249)
        self.assertEqual(
            analysis["contextPerf"]["fastOperatorCalls"]["text_chunk_mapper"],
            150000,
        )
        self.assertEqual(
            analysis["contextPerf"]["fastOperatorCalls"]["image_size_filter"],
            1800000,
        )
        self.assertEqual(
            analysis["contextPerf"]["fastOperatorCalls"]["video_resize_resolution_mapper"],
            640,
        )
        self.assertEqual(
            analysis["contextPerf"]["fastOperatorCalls"]["video_duration_filter"],
            400,
        )
        self.assertFalse(analysis["isolatedTiming"])
        self.assertEqual(analysis["warmup"], 0)
        self.assertEqual(analysis["rounds"], 1)
        self.assertTrue(analysis["unblockPerf"])
        self.assertTrue(analysis["representativeProfile"])

    def test_reference_text_fixture_is_bounded_for_thirty_minute_arm_run(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        text = payload["workload"]["operatorAnalysis"]["inputOverrides"][
            "pipeline_text_fineweb_full_min"
        ]

        self.assertEqual(text["fixture_id"], "fineweb_edu_50k_p4")
        self.assertEqual(text["rows"], 50000)
        self.assertIn("fineweb_edu_50k_p4", text["path"])

    def test_reference_text_fast_profile_meets_the_sample_floor(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        context_perf = payload["workload"]["operatorAnalysis"]["contextPerf"]

        fast_calls = context_perf["fastOperatorCalls"]

        self.assertEqual(fast_calls["clean_html_mapper"], 3500000)
        self.assertEqual(fast_calls["clean_links_mapper"], 125000)
        self.assertEqual(fast_calls["clean_copyright_mapper"], 100000)
        self.assertEqual(fast_calls["whitespace_normalization_mapper"], 70000)
        self.assertEqual(fast_calls["text_length_filter"], 20000000)
        self.assertEqual(fast_calls["document_deduplicator"], 12)
        self.assertEqual(fast_calls["text_chunk_mapper"], 150000)

    def test_reference_video_fast_profiles_have_blue98_sample_margin(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        fast_calls = payload["workload"]["operatorAnalysis"]["contextPerf"]["fastOperatorCalls"]

        self.assertEqual(fast_calls["video_resize_resolution_mapper"], 640)
        self.assertEqual(fast_calls["video_duration_filter"], 400)

    def test_reference_audio_fast_profile_has_blue98_sample_margin(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        fast_calls = payload["workload"]["operatorAnalysis"]["contextPerf"]["fastOperatorCalls"]

        self.assertEqual(fast_calls["audio_duration_filter"], 1536)

    def test_reference_image_fast_profiles_have_blue98_sample_margin(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)
        fast_calls = payload["workload"]["operatorAnalysis"]["contextPerf"]["fastOperatorCalls"]

        self.assertEqual(fast_calls["download_file_mapper"], 4000000)
        self.assertEqual(fast_calls["image_shape_filter"], 6000)
        self.assertEqual(fast_calls["image_aspect_ratio_filter"], 6000)
        self.assertEqual(fast_calls["image_size_filter"], 1800000)
        self.assertEqual(fast_calls["image_aesthetics_filter"], 3600000)
        self.assertEqual(fast_calls["image_text_similarity_filter"], 3600000)
        self.assertEqual(fast_calls["image_clip_vectorize_mapper"], 8000000)

    def test_reference_project_uses_bounded_context_perf_frequency(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = REPO_ROOT / "projects" / "volc-operator-sim-reference" / "project.yaml"
        payload = load_project_config(project)

        self.assertEqual(payload["workload"]["operatorAnalysis"]["contextPerf"]["perfFrequency"], 249)

    def test_extended_project_preserves_the_original_fourteen_pipelines(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.extended-14.yaml"
        )
        payload = load_project_config(project)
        workload = payload["workload"]
        analysis = workload["operatorAnalysis"]

        self.assertEqual(workload["testSet"], "extended_14")
        self.assertEqual(workload["group"], "target_14")
        self.assertEqual(analysis["group"], "target_14")
        self.assertEqual(
            analysis["tasks"],
            [
                "pipeline_text_fineweb_full_min",
                "text_corpus_minhash_dedup",
                "pipeline_text_vectorize_full_min",
                "audio_asr_prep_canonical",
                "video_clip_etl_canonical",
                "video_scene_split_etl",
                "video_vectorize_cpu",
                "pipeline_video_full_min",
                "image_laion_clean_canonical",
                "image_coco_audit",
                "pipeline_image_full_min",
                "text_anti_join_test",
                "pipeline_pdf_full_min",
                "pipeline_ad_nuscenes_min",
            ],
        )
        self.assertNotIn("inputOverrides", analysis)

    def test_text_vectorize_10k_project_pins_frozen_host_input(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.text-vectorize-10k.yaml"
        )
        payload = load_project_config(project)
        operator = payload["workload"]["operatorAnalysis"]
        frozen = operator["inputOverrides"][
            "pipeline_text_vectorize_full_min"
        ]

        self.assertEqual(frozen["rows"], 10000)
        self.assertEqual(
            frozen["path"],
            "fixtures/text/scale/fineweb_edu_vectorize_10k_p4.lance",
        )
        self.assertEqual(
            frozen["input_fingerprint"],
            "sha256:7475aaa2659f66ee4ca085c6178173db37f95c9f22d02e7b9c340bf71012d8ca",
        )


    def test_pdf_hash64_project_allows_real_ocr_stage_duration(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.pdf-full-min-hash64.yaml"
        )
        payload = load_project_config(project)
        frozen = payload["workload"]["operatorAnalysis"]["inputOverrides"][
            "pipeline_pdf_full_min"
        ]
        analysis = payload["workload"]["operatorAnalysis"]

        self.assertEqual(payload["workload"]["timeout"], 28800)
        self.assertEqual(frozen["rows"], 64)
        self.assertFalse(analysis["isolatedTiming"])
        self.assertFalse(analysis["profiling"])
        self.assertEqual(
            analysis["boundedFrozenProfile"]["ocrWindowSeconds"], 600
        )
        self.assertEqual(
            analysis["boundedFrozenProfile"]["sourceRows"], 16
        )

    def test_pdf_hash4_project_freezes_e2e_and_reuses_context_perf(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.pdf-full-min-hash4.yaml"
        )
        payload = load_project_config(project)
        analysis = payload["workload"]["operatorAnalysis"]
        frozen = analysis["inputOverrides"]["pipeline_pdf_full_min"]

        self.assertEqual(frozen["rows"], 4)
        self.assertEqual(frozen["fixture_id"], "pmc_pdf_hash4")
        self.assertEqual(
            frozen["input_fingerprint"],
            "sha256:2ae67663b803198f9e6e22671ae9067b227db4e3763783d3652d8f6be4a34fa4",
        )
        self.assertTrue(analysis["contextPerf"]["enabled"])
        self.assertTrue(analysis["contextPerf"]["splitByOperatorBoundary"])
        self.assertFalse(analysis["isolatedTiming"])

    def test_fineweb_200k_project_targets_bounded_single_pass_perf(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.text-fineweb-200k.yaml"
        )
        payload = load_project_config(project)
        analysis = payload["workload"]["operatorAnalysis"]
        frozen = analysis["inputOverrides"]["pipeline_text_fineweb_full_min"]

        self.assertEqual(payload["workload"]["group"], "core_dual_engine")
        self.assertEqual(analysis["group"], "core_dual_engine")
        self.assertEqual(analysis["engines"], ["daft_ray"])
        self.assertEqual(frozen["rows"], 200000)
        self.assertEqual(
            frozen["input_fingerprint"],
            "sha256:80e9d0ce05d073f6685059c5cf4861dbfbf81936a9f2c3592b48a15eeb07fdc2",
        )
        self.assertTrue(analysis["contextPerf"]["splitByOperatorBoundary"])
        self.assertEqual(analysis["contextPerf"]["perfFrequency"], 990)
        self.assertFalse(analysis["isolatedTiming"])

    def test_ad_500k_calibration_project_is_timing_only(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.ad-nuscenes-500k-calibration.yaml"
        )
        payload = load_project_config(project)
        analysis = payload["workload"]["operatorAnalysis"]
        frozen = analysis["inputOverrides"]["pipeline_ad_nuscenes_min"]

        self.assertEqual(frozen["rows"], 500000)
        self.assertEqual(
            frozen["input_fingerprint"],
            "sha256:c8262d7c96e0186fa6362255b36dd109d5c6103948cb24b2768aef7cfa331112",
        )
        self.assertTrue(analysis["contextTiming"])
        self.assertFalse(analysis["isolatedTiming"])
        self.assertFalse(analysis["profiling"])

    def test_ad_5m_project_targets_bounded_single_pass_perf(self) -> None:
        from pyframework_pipeline.config import load_project_config

        project = (
            REPO_ROOT
            / "projects"
            / "volc-operator-sim-reference"
            / "project.ad-nuscenes-5m.yaml"
        )
        payload = load_project_config(project)
        analysis = payload["workload"]["operatorAnalysis"]
        frozen = analysis["inputOverrides"]["pipeline_ad_nuscenes_min"]

        self.assertEqual(frozen["rows"], 5000000)
        self.assertEqual(
            frozen["input_fingerprint"],
            "sha256:891fba38c95045eb729daf183b6463192e23ec4937ea424ee9292806cbec2500",
        )
        self.assertTrue(analysis["contextPerf"]["splitByOperatorBoundary"])
        self.assertEqual(analysis["contextPerf"]["perfFrequency"], 990)
        self.assertFalse(analysis["isolatedTiming"])

    def test_reference_manifest_pins_required_fasttext_model(self) -> None:
        manifest = json.loads(
            (
                REPO_ROOT
                / "projects"
                / "volc-operator-sim-reference"
                / "data-sources.json"
            ).read_text(encoding="utf-8")
        )

        fasttext = next(
            entry
            for entry in manifest["entries"]
            if entry["sourceId"] == "fasttext-lid-176"
        )
        self.assertEqual(fasttext["path"], "models/lid.176.bin")
        self.assertEqual(fasttext["size"], 131266198)
        self.assertEqual(
            fasttext["sha256"],
            "7e69ec5451bc261cc7844e49e4792a85d7f09c06789ec800fc4a44aec362764e",
        )
        self.assertTrue(fasttext["required"])

        by_id = {entry["sourceId"]: entry for entry in manifest["entries"]}
        self.assertEqual(
            by_id["datajuicer-en-sentencepiece"]["sha256"],
            "cf8147a573770b4e6c0d4df1dcb75453baa88190706dab406be7711b84f059de",
        )
        self.assertEqual(by_id["datajuicer-en-sentencepiece"]["size"], 931348)
        self.assertEqual(
            by_id["datajuicer-en-kenlm"]["sha256"],
            "04923fccbb4e63005c40f01d66112659416de01accd80d16e366a592289ee07a",
        )
        self.assertEqual(by_id["datajuicer-en-kenlm"]["size"], 4444690658)
        self.assertTrue(by_id["datajuicer-en-sentencepiece"]["required"])
        self.assertTrue(by_id["datajuicer-en-kenlm"]["required"])

    def test_reference_manifest_pins_persistent_minilm_snapshot(self) -> None:
        manifest = json.loads(
            (
                REPO_ROOT
                / "projects"
                / "volc-operator-sim-reference"
                / "data-sources.json"
            ).read_text(encoding="utf-8")
        )
        by_id = {entry["sourceId"]: entry for entry in manifest["entries"]}
        prefix = "sentence-transformers-all-minilm-l6-v2-"
        model_entries = {
            source_id: entry
            for source_id, entry in by_id.items()
            if source_id.startswith(prefix)
        }

        self.assertEqual(len(model_entries), 11)
        weights = model_entries[prefix + "model-safetensors"]
        self.assertEqual(weights["path"], "models/all-MiniLM-L6-v2/model.safetensors")
        self.assertEqual(weights["size"], 90868376)
        self.assertEqual(
            weights["sha256"],
            "53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db",
        )
        for entry in model_entries.values():
            self.assertIn(
                "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
                entry["url"],
            )
            self.assertTrue(entry["required"])

    def test_host_prepare_precedes_container_and_mounts_persistent_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_yaml = _write_volc_project(Path(tmp))
            result = CliInvoker.run(
                "environment", "plan", str(project_yaml), "--platform", "arm"
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["framework"], "volcoperatorsim")
        step_ids = [step["id"] for step in plan["steps"]]
        prepare_index = step_ids.index("prepare-volc-host-data")
        build_index = step_ids.index("build-volc-image")
        start_index = step_ids.index("start-volc-container")
        self.assertLess(prepare_index, build_index)
        self.assertLess(build_index, start_index)

        prepare = next(s for s in plan["steps"] if s["id"] == "prepare-volc-host-data")
        build = next(s for s in plan["steps"] if s["id"] == "build-volc-image")
        start = next(s for s in plan["steps"] if s["id"] == "start-volc-container")
        readiness = next(s for s in plan["steps"] if s["id"] == "readiness-volc")
        fingerprint = next(
            s for s in plan["steps"] if s["id"] == "record-volc-environment"
        )

        self.assertEqual(
            prepare["scriptPath"],
            "adapters/volcoperatorsim/scripts/prepare-host-data.sh",
        )
        self.assertIn("DATA_MANIFEST_B64=", prepare["command"])
        self.assertIn("HOST_DATA_ROOT=/home/lxy/de_bench_full", prepare["command"])
        self.assertIn(
            "/home/lxy/de_bench_full/raw:/home/lxy/de_bench_full/raw-host:ro",
            start["command"],
        )
        self.assertIn(
            "/home/lxy/de_bench_full/fixtures/raw:"
            "/home/lxy/de_bench_full/raw/min_fixtures:rw",
            start["command"],
        )
        self.assertIn("docker exec volc-operator-sim-bench bash -lc", start["command"])
        self.assertIn("raw-host", start["command"])
        self.assertIn("base64 -d", start["command"])
        self.assertIn("/opt/conda/envs/xarch/bin/python", start["command"])
        self.assertNotIn("while IFS=", start["command"])
        self.assertIn("docker ps -aq --filter", start["command"])
        self.assertIn("label=pyframework.volc.config=", start["command"])
        self.assertIn("--label pyframework.volc.layout=3", start["command"])
        self.assertIn("label=pyframework.volc.layout=3", start["command"])
        self.assertNotIn("index .Config.Labels", start["command"])
        self.assertIn(
            "-e LD_PRELOAD=/opt/conda/envs/xarch/lib/libstdc++.so.6",
            start["command"],
        )
        self.assertIn(
            "-e PATH=/opt/conda/envs/xarch/bin:/opt/conda/bin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin",
            start["command"],
        )
        self.assertIn("-e CONDA_PREFIX=/opt/conda", start["command"])
        self.assertIn("--cpuset-cpus 4-7", start["command"])
        self.assertIn("--cpuset-mems 0", start["command"])
        self.assertIn(
            "docker update --cpuset-cpus 4-7 --cpuset-mems 0 "
            "volc-operator-sim-bench",
            start["command"],
        )
        self.assertIn("--ulimit nofile=65536:524288", start["command"])
        self.assertIn("-e PERF_LOCK_NUMA_POLICY=cpus=4-7,mems=0", start["command"])
        self.assertIn("-e PERF_LOCK_VIRTUALIZATION=bare-metal", start["command"])
        self.assertIn(
            "/home/lxy/de_bench_full/models:/home/lxy/de_bench_full/models:ro",
            start["command"],
        )
        self.assertIn(
            "/home/lxy/de_bench_full/operator-cache:/home/lxy/de_bench_full/operator-cache:rw",
            start["command"],
        )
        self.assertIn(
            "/home/lxy/de_bench_full/bench-results:/home/lxy/de_bench_full/bench-results:rw",
            start["command"],
        )
        self.assertIn("--shm-size 64g", start["command"])
        self.assertIn("sched_getaffinity", readiness["command"])
        self.assertIn("RLIMIT_NOFILE", readiness["command"])
        self.assertIn("/opt/conda/envs/xarch/bin/python", readiness["command"])
        self.assertIn("/opt/conda/envs/xdj/bin/python", readiness["command"])
        self.assertIn(
            "import data_juicer, psutil, selectolax, torchcodec",
            readiness["command"],
        )
        self.assertIn(
            "test -f /home/lxy/de_bench_full/models/lid.176.bin",
            readiness["command"],
        )
        self.assertIn(
            "test -f /home/lxy/de_bench_full/models/en.sp.model",
            readiness["command"],
        )
        self.assertIn(
            "test -f /home/lxy/de_bench_full/models/en.arpa.bin",
            readiness["command"],
        )
        self.assertIn("rev-parse HEAD", readiness["command"])
        self.assertTrue(fingerprint["captureOutput"])
        self.assertEqual(
            fingerprint["scriptPath"],
            "adapters/volcoperatorsim/scripts/collect-environment-fingerprint.sh",
        )
        self.assertLess(
            step_ids.index("enable-perf-paranoid"),
            step_ids.index("record-volc-environment"),
        )
        self.assertIn("--privileged", start["command"])
        self.assertIn("MIN_HOST_FREE_BYTES=21474836480", prepare["command"])
        self.assertIn(
            "VOLC_BASE_IMAGE=m.daocloud.io/docker.io/debian:bookworm-slim",
            build["command"],
        )
        self.assertIn(
            "VOLC_DEBIAN_MIRROR_HOST=mirrors.huaweicloud.com",
            build["command"],
        )
        self.assertIn("VOLC_MINIFORGE_URL_TEMPLATE=", build["command"])
        self.assertIn(
            "https://mirrors.ustc.edu.cn/github-release/conda-forge/"
            "miniforge/LatestRelease/Miniforge3-26.3.2-3-Linux-__ARCH__.sh",
            build["command"],
        )
        self.assertIn(
            "VOLC_MINIFORGE_SHA256="
            "2c113a69297e612b01ca0f320c22a3107a11f2ab9b573d79ac868a175945ce29",
            build["command"],
        )
        self.assertIn(
            "VOLC_PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu",
            build["command"],
        )
        self.assertIn("VOLC_BUILD_CONFIG_HASH=", build["command"])
        self.assertIn("label=pyframework.volc.build-config=", build["command"])
        self.assertIn("REQUIRE_PERF=true", fingerprint["command"])
        self.assertIn("EXPECTED_CPUSET_CPUS=4-7", fingerprint["command"])
        self.assertIn("EXPECTED_CPUSET_MEMS=0", fingerprint["command"])
        self.assertIn("EXPECTED_NOFILE_SOFT=65536", fingerprint["command"])
        self.assertIn("EXPECTED_NOFILE_HARD=524288", fingerprint["command"])
        self.assertIn("EXPECTED_VIRTUALIZATION=bare-metal", fingerprint["command"])

    def test_fixture_step_builds_and_verifies_all_canonical_media_with_xarch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_yaml = _write_volc_project(Path(tmp))
            result = CliInvoker.run(
                "environment", "plan", str(project_yaml), "--platform", "arm"
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        fixture = next(
            step for step in plan["steps"] if step["id"] == "build-volc-fixtures"
        )
        readiness = next(
            step for step in plan["steps"] if step["id"] == "readiness-volc"
        )
        command = fixture["command"]

        self.assertIn("/opt/conda/envs/xarch/bin/python", command)
        self.assertIn("export PYTHONPATH=/opt/volc_operator_sim", command)
        self.assertLess(
            command.index("export PYTHONPATH=/opt/volc_operator_sim"),
            command.index("build_min_fixtures.sh"),
        )
        self.assertIn("build_min_fixtures.sh", command)
        self.assertEqual(command.count("build_media_lance.py"), 3)
        self.assertIn("--source-dataset laion_subset", command)
        self.assertIn("--source-dataset librispeech", command)
        self.assertIn("--source-dataset msrvtt", command)
        self.assertEqual(command.count("--verify"), 3)
        self.assertIn("check_input_parity.py --require-canonical-media", command)
        self.assertIn("laion_subset_image.lance", readiness["command"])
        self.assertIn("librispeech_audio.mirror.jsonl", readiness["command"])
        self.assertIn("msrvtt_video.mirror.meta.json", readiness["command"])

    def test_revision_change_rebuilds_image_and_reconciles_container(self) -> None:
        first_revision = "56d3b6856895427a0519cbaa437d55443fcb578b"
        second_revision = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_project = _write_volc_project(root / "first", first_revision)
            second_project = _write_volc_project(root / "second", second_revision)
            first = CliInvoker.run(
                "environment", "plan", str(first_project), "--platform", "arm"
            )
            second = CliInvoker.run(
                "environment", "plan", str(second_project), "--platform", "arm"
            )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        first_steps = {step["id"]: step for step in json.loads(first.stdout)["steps"]}
        second_steps = {
            step["id"]: step for step in json.loads(second.stdout)["steps"]
        }
        build_command = first_steps["build-volc-image"]["command"]

        self.assertIn("docker images -q --filter", build_command)
        self.assertIn("pyframework.volc.revision", build_command)
        self.assertIn("pyframework.volc.build-config", build_command)
        self.assertIn(first_revision, build_command)
        self.assertNotIn("index .Config.Labels", build_command)
        self.assertNotEqual(
            first_steps["start-volc-container"]["command"],
            second_steps["start-volc-container"]["command"],
        )

    def test_raw_host_links_are_created_safely_and_idempotently(self) -> None:
        from pyframework_pipeline.adapters.volcoperatorsim.environment import (
            _raw_link_bootstrap,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "raw-host" / "coco2017"
            source.mkdir(parents=True)
            (source / "sample.txt").write_text("existing-host-data", encoding="utf-8")
            fixture_mount = root / "raw" / "min_fixtures"
            fixture_mount.mkdir(parents=True)
            command = _raw_link_bootstrap(str(root), python=sys.executable)

            first = subprocess.run(
                ["bash", "-lc", command], text=True, capture_output=True, check=False
            )
            second = subprocess.run(
                ["bash", "-lc", command], text=True, capture_output=True, check=False
            )

            target = root / "raw" / "coco2017"
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(), source.resolve())
            self.assertEqual(
                (target / "sample.txt").read_text(encoding="utf-8"),
                "existing-host-data",
            )
            self.assertTrue(fixture_mount.is_dir())

    def test_config_validate_accepts_complete_volc_project(self) -> None:
        from pyframework_pipeline.config import validate_pipeline_config

        with tempfile.TemporaryDirectory() as tmp:
            project_yaml = _write_volc_project(Path(tmp))
            report = validate_pipeline_config(
                project_yaml, require_bridge_token=False
            )

        self.assertEqual(report["status"], "ok", report["issues"])
        self.assertEqual(report["issueCount"], 0)

    def test_config_validate_rejects_missing_platform_image(self) -> None:
        from pyframework_pipeline.config import validate_pipeline_config

        with tempfile.TemporaryDirectory() as tmp:
            project_yaml = _write_volc_project(Path(tmp))
            environment = project_yaml.parent / "environment.yaml"
            environment.write_text(
                environment.read_text(encoding="utf-8").replace(
                    "    x86: volc-operator-sim-bench:test-x86_64\n", ""
                ),
                encoding="utf-8",
            )
            report = validate_pipeline_config(
                project_yaml, require_bridge_token=False
            )

        messages = " ".join(issue["message"] for issue in report["issues"])
        self.assertEqual(report["status"], "error")
        self.assertIn("x86", messages)

    def test_host_prepare_downloads_to_persistent_root_and_reuses_checksum(self) -> None:
        script = (
            REPO_ROOT
            / "pipelines"
            / "pyframework_pipeline"
            / "adapters"
            / "volcoperatorsim"
            / "scripts"
            / "prepare-host-data.sh"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(b"persistent-dataset")
            manifest = {
                "schemaVersion": 1,
                "entries": [
                    {
                        "sourceId": "dataset",
                        "url": source.as_uri(),
                        "path": "raw/dataset.bin",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "required": True,
                    }
                ],
            }
            data_root = root / "host-data"
            env = dict(os.environ)
            env.update(
                {
                    "HOST_DATA_ROOT": str(data_root),
                    "DATA_MANIFEST_B64": base64.b64encode(
                        json.dumps(manifest).encode("utf-8")
                    ).decode("ascii"),
                }
            )

            first = subprocess.run(
                ["bash", str(script)], text=True, capture_output=True, env=env
            )
            second = subprocess.run(
                ["bash", str(script)], text=True, capture_output=True, env=env
            )

            record = json.loads(
                (data_root / "manifests" / "host-data-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                (data_root / "raw" / "dataset.bin").read_bytes(),
                b"persistent-dataset",
            )
            self.assertTrue((data_root / "fixtures" / "raw").is_dir())
            self.assertEqual(record["entries"][0]["status"], "reused")
            self.assertTrue(
                (data_root / "manifests" / "host-data-COMPLETE.json").is_file()
            )

    def test_host_prepare_records_and_quarantines_required_size_failure(self) -> None:
        script = _host_prepare_script()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.bin"
            source.write_bytes(b"size-mismatch")
            manifest = {
                "schemaVersion": 1,
                "entries": [
                    {
                        "sourceId": "bad-size",
                        "url": source.as_uri(),
                        "path": "raw/bad-size.bin",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "size": source.stat().st_size + 1,
                        "required": True,
                    }
                ],
            }
            data_root = root / "host-data"
            result = _run_host_prepare(script, data_root, manifest)
            record = json.loads(
                (data_root / "manifests" / "host-data-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(record["entries"][0]["status"], "failed")
            self.assertIn("size mismatch", record["entries"][0]["error"])
            self.assertTrue(any((data_root / "quarantine").iterdir()))
            self.assertFalse(
                (data_root / "manifests" / "host-data-COMPLETE.json").exists()
            )

    def test_host_prepare_rejects_non_allowlisted_url_scheme_before_download(self) -> None:
        script = _host_prepare_script()
        with tempfile.TemporaryDirectory() as tmp:
            data_root = Path(tmp) / "host-data"
            manifest = {
                "schemaVersion": 1,
                "entries": [
                    {
                        "sourceId": "unsafe",
                        "url": "ftp://example.invalid/dataset.bin",
                        "path": "raw/dataset.bin",
                        "sha256": "a" * 64,
                        "required": True,
                    }
                ],
            }
            result = _run_host_prepare(script, data_root, manifest)
            record = json.loads(
                (data_root / "manifests" / "host-data-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("URL scheme", record["entries"][0]["error"])

    def test_build_script_creates_pinned_dual_conda_image(self) -> None:
        script_path = (
            REPO_ROOT
            / "pipelines"
            / "pyframework_pipeline"
            / "adapters"
            / "volcoperatorsim"
            / "scripts"
            / "build-volcoperatorsim-image.sh"
        )
        script = script_path.read_text(encoding="utf-8")

        self.assertNotIn("\r\n", script)
        self.assertIn("VOLC_OPERATOR_SIM_REVISION", script)
        self.assertIn("git checkout --detach", script)
        self.assertIn("rebuild_xarch.sh", script)
        self.assertIn("DAFT_CONDA_ENV", script)
        self.assertIn("setup_dj_av11_env.sh", script)
        self.assertIn("DATAJUICER_CONDA_ENV", script)
        self.assertIn("docker build", script)
        self.assertNotIn('  --platform "$DOCKER_PLATFORM"', script)
        self.assertIn('--build-arg "TARGETARCH=$DOCKER_TARGETARCH"', script)
        self.assertIn("requested architecture", script)
        self.assertIn("VOLC_DEBIAN_MIRROR_HOST", script)
        self.assertIn("Acquire::Retries=5", script)
        self.assertIn("VOLC_MINIFORGE_URL_TEMPLATE", script)
        self.assertIn("VOLC_MINIFORGE_SHA256", script)
        self.assertIn(
            'sed "s/__ARCH__/$miniforge_arch/g"',
            script,
        )
        self.assertIn('echo "${MINIFORGE_SHA256}  /tmp/miniforge.sh" | sha256sum -c -', script)
        self.assertIn("VOLC_PYTORCH_CPU_INDEX_URL", script)
        self.assertIn("VOLC_BUILD_CONFIG_HASH", script)
        self.assertIn("pyframework.volc.build-config", script)
        self.assertIn(
            '/opt/conda/envs/${DATAJUICER_CONDA_ENV}/bin/python -m pip install',
            script,
        )
        self.assertIn("selectolax==0.4.11", script)
        self.assertIn("torchcodec==0.15.0+cpu", script)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple/", script)
        self.assertIn("--trusted-host mirrors.aliyun.com", script)
        self.assertEqual(
            script.count('PIP_EXTRA_INDEX_URL="$PYTORCH_CPU_INDEX_URL"'),
            2,
            "xarch rebuild and xdj frozen-stack replay must both see the CPU index",
        )

        template = "https://example.invalid/Miniforge3-Linux-__ARCH__.sh"
        for arch in ("aarch64", "x86_64"):
            expanded = subprocess.run(
                [
                    "bash",
                    "-c",
                    'printf "%s\\n" "$URL" | sed "s/__ARCH__/$ARCH/g"',
                ],
                env={"URL": template, "ARCH": arch},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(expanded.returncode, 0, expanded.stderr)
            self.assertEqual(
                expanded.stdout.strip(),
                f"https://example.invalid/Miniforge3-Linux-{arch}.sh",
            )

    def test_environment_fingerprint_hashes_stable_source_manifest(self) -> None:
        script = (
            REPO_ROOT
            / "pipelines"
            / "pyframework_pipeline"
            / "adapters"
            / "volcoperatorsim"
            / "scripts"
            / "collect-environment-fingerprint.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('record.get("sourceManifest", record)', script)
        self.assertIn('sort_keys=True, separators=(",", ":")', script)
        self.assertNotIn('sha256sum "$data_manifest"', script)
        self.assertIn('"cpuSet"', script)
        self.assertIn('"nofile"', script)
        self.assertIn('"virtualization"', script)


def _write_volc_project(
    root: Path,
    revision: str = "56d3b6856895427a0519cbaa437d55443fcb578b",
) -> Path:
    workload = root / "workload"
    workload.mkdir(parents=True)
    (workload / "README.md").write_text("fixture", encoding="utf-8")
    (root / "data-sources.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "entries": [
                    {
                        "sourceId": "fixture",
                        "url": "https://example.invalid/fixture.tar.gz",
                        "path": "raw/fixture.tar.gz",
                        "sha256": "a" * 64,
                        "required": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    project = root / "project.yaml"
    project.write_text(
        """id: volc-test
name: Volc Test
fourLayerRoot: .
workload:
  localDir: workload
  benchmark: volc-operator-sim
  group: core_dual_engine
  profile: smoke
  rounds: 1
  operatorAnalysis:
    enabled: true
    contextTiming: true
    isolatedTiming: true
    profiling: true
    warmup: 1
    rounds: 3
    minPerfSamples: 5000
    topSymbols: 20
bridge:
  repo: XuanYuL5/volc_operator_sim
  platform: gitcode
run:
  platforms:
    - arm
    - x86
""",
        encoding="utf-8",
    )
    (root / "environment.yaml").write_text(
        f"""schemaVersion: 1
framework: volcoperatorsim
mode: plan-only
platforms:
  - id: arm
    arch: aarch64
    hosts:
      - role: client
        hostRef: arm-host
  - id: x86
    arch: x86_64
    hosts:
      - role: client
        hostRef: x86-host
software:
  volcOperatorSimRepo: https://gitcode.com/XuanYuL5/volc_operator_sim.git
  volcOperatorSimBaseImage: m.daocloud.io/docker.io/debian:bookworm-slim
  volcDebianMirrorHost: mirrors.huaweicloud.com
  volcMiniforgeUrlTemplate: https://mirrors.ustc.edu.cn/github-release/conda-forge/miniforge/LatestRelease/Miniforge3-26.3.2-3-Linux-__ARCH__.sh
  volcMiniforgeSha256s:
    arm: 2c113a69297e612b01ca0f320c22a3107a11f2ab9b573d79ac868a175945ce29
    x86: 848194851a98903134187fbb4ab50efe87b003e0c0f808f97644b7524a62bf2c
  volcPytorchCpuIndexUrl: https://download.pytorch.org/whl/cpu
  volcOperatorSimRevision: {revision}
  volcOperatorSimImages:
    arm: volc-operator-sim-bench:test-aarch64
    x86: volc-operator-sim-bench:test-x86_64
  volcOperatorSimContainer: volc-operator-sim-bench
  hostDataRoot: /home/lxy/de_bench_full
  dataSourceManifest: data-sources.json
  daftCondaEnv: xarch
  dataJuicerCondaEnv: xdj
  shmSize: 64g
  volcCpuSets:
    arm: 4-7
    x86: 0-3
  volcMemoryNodes:
    arm: '0'
    x86: '0'
  volcVirtualization:
    arm: bare-metal
    x86: kvm
  volcNofileSoft: 65536
  volcNofileHard: 524288
  perfFrequency: 99
  minHostFreeGiB: 20
  volcPrivileged: true
  profilingTools:
    - perf
    - objdump
    - readelf
    - py-spy
hostRefs:
  arm-host:
    connect: ssh
    alias: blue-98
    capabilities:
      ssh: true
      docker: true
      internet: true
      upload: true
      download: true
  x86-host:
    connect: ssh
    alias: 85.93.9.221
    user: root
    port: 22
    capabilities:
      ssh: true
      docker: true
      internet: true
      upload: true
      download: true
""",
        encoding="utf-8",
    )
    return project


def _host_prepare_script() -> Path:
    return (
        REPO_ROOT
        / "pipelines"
        / "pyframework_pipeline"
        / "adapters"
        / "volcoperatorsim"
        / "scripts"
        / "prepare-host-data.sh"
    )


def _run_host_prepare(
    script: Path, data_root: Path, manifest: dict
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        {
            "HOST_DATA_ROOT": str(data_root),
            "MIN_HOST_FREE_BYTES": "0",
            "DATA_MANIFEST_B64": base64.b64encode(
                json.dumps(manifest).encode("utf-8")
            ).decode("ascii"),
        }
    )
    return subprocess.run(
        ["bash", str(script)], text=True, capture_output=True, env=env
    )


if __name__ == "__main__":
    unittest.main()
