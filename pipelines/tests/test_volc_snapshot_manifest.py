"""Stage snapshot parity and cache-contract tests."""

from __future__ import annotations

import base64
import json
import subprocess
import unittest

from pyframework_pipeline.adapters.volcoperatorsim.snapshot import (
    expected_snapshot_identity,
    validate_snapshot_manifest,
)


class SnapshotManifestTest(unittest.TestCase):
    def test_complete_manifest_with_matching_identity_and_parity_is_cache_hit(self) -> None:
        expected = expected_snapshot_identity(
            snapshot_id="abc123",
            source_revision="5" * 40,
            producer="daft_ray",
            parent_fingerprint="sha256:parent",
            operator_spec={"dj_ops": "clean_html_mapper", "params": {}},
            builder_version="1",
        )
        manifest = {
            **expected,
            "status": "complete",
            "representations": {
                "lance": {"rows": 4, "files": 2, "bytes": 100, "schemaFingerprint": "s"},
                "jsonl": {"rows": 4, "files": 1, "bytes": 80, "schemaFingerprint": "s"},
            },
            "parity": {
                "status": "passed",
                "orderedFingerprint": "sha256:ordered",
                "contentFingerprint": "sha256:content",
                "partitionSpec": [],
            },
        }

        result = validate_snapshot_manifest(manifest, expected)

        self.assertTrue(result.cache_hit)
        self.assertEqual(result.reasons, ())

    def test_cache_identity_drift_or_incomplete_parity_is_rejected(self) -> None:
        expected = expected_snapshot_identity(
            snapshot_id="abc123",
            source_revision="5" * 40,
            producer="daft_ray",
            parent_fingerprint="sha256:parent",
            operator_spec={"dj_ops": "clean_html_mapper"},
            builder_version="1",
        )
        manifest = {
            **expected,
            "sourceRevision": "6" * 40,
            "status": "complete",
            "representations": {},
            "parity": {"status": "failed"},
        }

        result = validate_snapshot_manifest(manifest, expected)

        self.assertFalse(result.cache_hit)
        self.assertIn("source_revision_mismatch", result.reasons)
        self.assertIn("parity_failed", result.reasons)
        self.assertIn("representation_metadata_missing", result.reasons)

    def test_v2_snapshot_without_logical_field_contract_is_rejected(self) -> None:
        expected = {
            **expected_snapshot_identity(
                snapshot_id="abc123",
                source_revision="5" * 40,
                producer="daft_ray",
                parent_fingerprint="sha256:parent",
                operator_spec={"dj_ops": "clean_html_mapper"},
                builder_version="2",
            ),
            "logicalField": "text",
        }
        manifest = {
            **expected,
            "logicalField": None,
            "status": "complete",
            "representations": {
                "lance": {"rows": 1, "files": 1, "bytes": 10, "schemaFingerprint": "s"},
                "jsonl": {"rows": 1, "files": 1, "bytes": 8, "schemaFingerprint": "s"},
            },
            "parity": {
                "status": "passed",
                "orderedFingerprint": "sha256:o",
                "contentFingerprint": "sha256:c",
                "partitionSpec": [],
            },
        }

        result = validate_snapshot_manifest(manifest, expected)

        self.assertFalse(result.cache_hit)
        self.assertIn("logical_field_missing", result.reasons)

    def test_v3_media_snapshot_without_datajuicer_fields_is_rejected(self) -> None:
        expected = {
            **expected_snapshot_identity(
                snapshot_id="abc123",
                source_revision="5" * 40,
                producer="daft_ray",
                parent_fingerprint="sha256:parent",
                operator_spec={"dj_ops": "audio_duration_filter"},
                builder_version="3",
            ),
            "logicalField": "file_path",
            "mediaCompatibility": {"modality": "audio"},
        }
        manifest = {
            **expected,
            "logicalField": {"status": "passed", "field": "file_path"},
            "mediaCompatibility": None,
            "status": "complete",
            "representations": {
                "lance": {"rows": 1, "files": 1, "bytes": 10, "schemaFingerprint": "s"},
                "jsonl": {"rows": 1, "files": 1, "bytes": 8, "schemaFingerprint": "s"},
            },
            "parity": {
                "status": "passed",
                "orderedFingerprint": "sha256:o",
                "contentFingerprint": "sha256:c",
                "partitionSpec": [],
            },
        }

        result = validate_snapshot_manifest(manifest, expected)

        self.assertFalse(result.cache_hit)
        self.assertIn("media_compatibility_missing", result.reasons)

    def test_snapshot_mirror_projects_internal_output_to_logical_field(self) -> None:
        from pyframework_pipeline.adapters.volcoperatorsim.acquisition import (
            VolcAcquisitionSettings,
            _snapshot_mirror_command,
        )

        settings = VolcAcquisitionSettings(
            container="bench", host_data_root="/data", revision="5" * 40,
            daft_python="/xarch/python", datajuicer_python="/xdj/python",
            group="g", profile="smoke", rounds=1, timeout=60,
            operator_enabled=True, context_timing=True, isolated_timing=True,
            profiling=True, operator_warmup=0, operator_rounds=1,
            perf_frequency=99, perf_events="cycles",
        )
        snapshot = {
            **expected_snapshot_identity(
                snapshot_id="abc123",
                source_revision="5" * 40,
                producer="daft_ray",
                parent_fingerprint="sha256:parent",
                operator_spec={"dj_ops": "clean_html_mapper"},
                builder_version="2",
            ),
            "logicalField": "text",
        }

        command = _snapshot_mirror_command(
            settings,
            snapshot,
            {
                "input": {
                    "kind": "lance",
                    "field": "text",
                    "manifest_path": "fixtures/text/scale/input.manifest.json",
                }
            },
        )

        self.assertIn("table.append_column(field,table[source_column])", command)
        self.assertIn('source_partition.get("fragments")', command)
        self.assertIn("max_rows_per_file=max_rows_per_file", command)
        self.assertIn('"policy":"inherit_if_declared"', command)
        self.assertIn("fixtures/text/scale/input.manifest.json", command)
        self.assertIn('"logicalField"', command)

    def test_snapshot_cache_rejects_failed_partition_inheritance(self) -> None:
        expected = {
            **expected_snapshot_identity(
                snapshot_id="abc123",
                source_revision="5" * 40,
                producer="daft_ray",
                parent_fingerprint="sha256:parent",
                operator_spec={"dj_ops": "clean_html_mapper"},
                builder_version="4",
            ),
            "logicalField": "text",
            "partitionPolicy": "inherit_if_declared",
        }
        manifest = {
            **expected,
            "status": "complete",
            "logicalField": {"status": "passed", "field": "text"},
            "representations": {
                "lance": {"rows": 10, "files": 3, "bytes": 10, "schemaFingerprint": "s"},
                "jsonl": {"rows": 10, "files": 1, "bytes": 8, "schemaFingerprint": "s"},
            },
            "parity": {
                "status": "passed",
                "orderedFingerprint": "sha256:o",
                "contentFingerprint": "sha256:c",
                "partitionSpec": {
                    "status": "failed",
                    "policy": "inherit_if_declared",
                    "sourceFragments": 4,
                    "fragments": 1,
                },
            },
        }

        result = validate_snapshot_manifest(manifest, expected)

        self.assertFalse(result.cache_hit)
        self.assertIn("partition_inheritance_failed", result.reasons)

    def test_snapshot_mirror_preserves_datajuicer_media_input_contract(self) -> None:
        from pyframework_pipeline.adapters.volcoperatorsim.acquisition import (
            VolcAcquisitionSettings,
            _snapshot_mirror_command,
        )

        settings = VolcAcquisitionSettings(
            container="bench", host_data_root="/data", revision="5" * 40,
            daft_python="/xarch/python", datajuicer_python="/xdj/python",
            group="g", profile="smoke", rounds=1, timeout=60,
            operator_enabled=True, context_timing=True, isolated_timing=True,
            profiling=True, operator_warmup=0, operator_rounds=1,
            perf_frequency=99, perf_events="cycles",
        )
        snapshot = {
            **expected_snapshot_identity(
                snapshot_id="abc123",
                source_revision="5" * 40,
                producer="daft_ray",
                parent_fingerprint="sha256:parent",
                operator_spec={"dj_ops": "audio_duration_filter"},
                builder_version="3",
            ),
            "logicalField": "file_path",
        }

        command = _snapshot_mirror_command(
            settings,
            snapshot,
            {
                "input": {
                    "kind": "lance",
                    "field": "file_path",
                    "modality": "audio",
                }
            },
        )

        self.assertIn('media_keys={"image":"images","audio":"audios","video":"videos"}', command)
        self.assertIn('table.append_column(media_key', command)
        self.assertIn('table.append_column("source_file"', command)
        self.assertIn('table.append_column("text"', command)
        self.assertIn('"mediaCompatibility"', command)
        self.assertIn("audio", command)

    def test_snapshot_builder_reuses_only_a_valid_complete_cache_entry(self) -> None:
        from pyframework_pipeline.adapters.volcoperatorsim.acquisition import (
            VolcAcquisitionSettings,
            _build_snapshots,
        )

        expected = expected_snapshot_identity(
            snapshot_id="abc123",
            source_revision="5" * 40,
            producer="daft_ray",
            parent_fingerprint="sha256:parent",
            operator_spec={"dj_ops": "clean_html_mapper"},
            builder_version="1",
        )
        manifest = {
            **expected,
            "status": "complete",
            "representations": {
                "lance": {"rows": 1, "files": 1, "bytes": 10, "schemaFingerprint": "s"},
                "jsonl": {"rows": 1, "files": 1, "bytes": 8, "schemaFingerprint": "s"},
            },
            "parity": {
                "status": "passed",
                "orderedFingerprint": "sha256:o",
                "contentFingerprint": "sha256:c",
                "partitionSpec": [],
            },
        }

        class Executor:
            def __init__(self):
                self.commands = []

            def run(self, command, timeout=300, stream=False):
                self.commands.append(command)
                payload = base64.b64encode(json.dumps(manifest).encode()).decode()
                return subprocess.CompletedProcess(
                    [], 0, f"PYFRAMEWORK_SNAPSHOT_MANIFEST={payload}\n", ""
                )

        settings = VolcAcquisitionSettings(
            container="bench", host_data_root="/data", revision="5" * 40,
            daft_python="/xarch/python", datajuicer_python="/xdj/python",
            group="g", profile="smoke", rounds=1, timeout=60,
            operator_enabled=True, context_timing=True, isolated_timing=True,
            profiling=True, operator_warmup=0, operator_rounds=1,
            perf_frequency=99, perf_events="cycles",
        )
        plan = {
            "tasks": [{
                "pipelineId": "p",
                "snapshots": [{**expected, "afterOrder": 0}],
            }]
        }
        executor = Executor()

        result = _build_snapshots(
            executor=executor,
            plan=plan,
            overlay_paths={("p", "snapshot", 0): "/overlay.json"},
            task_documents={"p": {"input": {"kind": "lance", "field": "text"}}},
            settings=settings,
            remote_root="/results",
        )

        self.assertEqual(result[("p", 0)]["manifest_path"], "/data/operator-cache/abc123/manifest.json")
        commands = "\n".join(executor.commands)
        self.assertIn("PYFRAMEWORK_SNAPSHOT_MANIFEST", commands)
        self.assertNotIn("MODE=timing", commands)


if __name__ == "__main__":
    unittest.main()
