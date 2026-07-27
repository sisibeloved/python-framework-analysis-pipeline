"""Tests for portable perf symbol bundles used by the Volc adapter."""

from __future__ import annotations

import json
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle import (
    DeletedMappingCollector,
    IDENTITY_POLICY,
    ProcMap,
    UnresolvedDeletedMappings,
    _archive_stable_perf_object,
    _batch_symbol_names,
    _batch_symbolize,
    _defined_symbols,
    _isolate_observer_affinity,
    _populate_perf_buildid_cache,
    _select_workload_affinity,
    _symbol_lookup_keys,
    index_raw_perf_lines,
    build_raw_sample_index,
    parse_proc_maps,
    resolve_period_report,
    record_with_symbol_bundle,
    write_resolved_period_report,
)


class VolcPerfSymbolBundleTest(unittest.TestCase):
    def test_raw_sample_builder_filters_non_event_dump_lines_before_python(self) -> None:
        class Process:
            def __init__(self, output: str, returncode: int = 0) -> None:
                self.stdout = io.StringIO(output)
                self.returncode = returncode
                self.terminated = False

            def wait(self) -> int:
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True

        perf_process = Process("unfiltered raw dump\n")
        filter_process = Process(
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 <abc123>]: r-xp /tmp/lib.so\n"
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 100 addr: 0\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc123": {
                    "buildId": "abc123",
                    "cachePath": "objects/abc123.elf",
                    "originalPath": "/tmp/lib.so",
                }
            },
            "mappings": [],
        }

        with patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "subprocess.Popen",
            side_effect=[perf_process, filter_process],
        ) as popen:
            index = build_raw_sample_index(
                perf_data=Path("/capture/perf.data"),
                manifest=manifest,
            )

        self.assertEqual(
            index[(222, 0x123)],
            {"buildid:abc123": {"period": 100, "sampleCount": 1}},
        )
        filter_call = popen.call_args_list[1]
        self.assertEqual(filter_call.args[0][0:2], ("grep", "-E"))
        self.assertIn("PERF_RECORD_", filter_call.args[0][2])
        self.assertEqual(filter_call.kwargs["env"]["LC_ALL"], "C")

    def test_perf_lock_policy_reserves_the_observer_cpu_from_the_workload(self) -> None:
        target = _select_workload_affinity(
            {4, 5, 6, 7, 8}, "cpus=4-7,mems=0"
        )

        self.assertEqual(target, {4, 5, 6, 7})

    def test_observer_moves_off_the_inherited_workload_cpus(self) -> None:
        with (
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.cpu_count",
                return_value=8,
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.sched_setaffinity"
            ) as set_affinity,
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.sched_getaffinity",
                return_value={4},
            ),
        ):
            evidence = _isolate_observer_affinity({0, 1, 2, 3})

        self.assertEqual(evidence["status"], "isolated")
        self.assertEqual(evidence["targetCpus"], [0, 1, 2, 3])
        self.assertEqual(evidence["observerCpus"], [4])
        set_affinity.assert_called_once_with(0, {4})

    def test_record_wrapper_fails_closed_without_a_spare_observer_cpu(self) -> None:
        class Process:
            pid = 123
            returncode = 0

            @staticmethod
            def poll() -> int:
                return 0

        class Collector:
            objects: dict[str, dict] = {}

            def __init__(self, **kwargs):
                del kwargs

            def scan(self) -> None:
                return None

            def finalize(self) -> None:
                return None

            def manifest(self, *, perf_data: Path) -> dict:
                return {"status": "complete", "perfData": str(perf_data)}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "subprocess.Popen",
                return_value=Process(),
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "DeletedMappingCollector",
                Collector,
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "_supported_record_options",
                return_value=(),
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "_populate_perf_buildid_cache",
                return_value=[],
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.sched_getaffinity",
                return_value={0},
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.cpu_count",
                return_value=1,
            ),
        ):
            output = Path(tmp) / "perf.data"
            returncode = record_with_symbol_bundle(
                real_perf="/usr/bin/perf",
                cache_root=Path(tmp) / "cache",
                arguments=("-o", str(output), "--", "true"),
            )
            manifest = json.loads(
                (Path(tmp) / "perf-dso-manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(returncode, 20)
        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["observerAffinity"]["status"], "unavailable")

    def test_record_wrapper_scans_the_container_pid_namespace_for_reparented_workers(
        self,
    ) -> None:
        constructor_args: dict[str, object] = {}

        class Process:
            pid = 123
            returncode = 0

            @staticmethod
            def poll() -> int:
                return 0

        class Collector:
            objects: dict[str, dict] = {}

            def __init__(self, **kwargs):
                constructor_args.update(kwargs)

            def scan(self) -> None:
                return None

            def finalize(self) -> None:
                return None

            def manifest(self, *, perf_data: Path) -> dict:
                return {"status": "complete", "perfData": str(perf_data)}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "subprocess.Popen",
                return_value=Process(),
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "DeletedMappingCollector",
                Collector,
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "_supported_record_options",
                return_value=(),
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "_populate_perf_buildid_cache",
                return_value=[],
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "os.sched_getaffinity",
                return_value={0},
            ),
            patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "_isolate_observer_affinity",
                return_value={
                    "status": "isolated",
                    "targetCpus": [0],
                    "observerCpus": [1],
                },
            ),
        ):
            output = Path(tmp) / "perf.data"
            returncode = record_with_symbol_bundle(
                real_perf="/usr/bin/perf",
                cache_root=Path(tmp) / "cache",
                arguments=("-o", str(output), "--", "true"),
            )

        self.assertEqual(returncode, 0)
        self.assertNotIn("root_pid", constructor_args)

    def test_required_resolution_rejects_incomplete_mapping_manifest_without_deleted_rows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "perf-report-period.txt"
            manifest = root / "perf-dso-manifest.json"
            output = root / "perf-report-period-resolved.txt"
            source.write_text(
                "100.00|100|1|python|1:python|python3.10|[.] known\n",
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "incomplete",
                        "identityPolicy": IDENTITY_POLICY,
                        "objects": {},
                        "mappings": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(UnresolvedDeletedMappings):
                write_resolved_period_report(
                    source=source,
                    manifest_path=manifest,
                    output=output,
                    require_complete=True,
                )

            summary = json.loads(
                (root / "perf-symbol-resolution.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["unresolvedDeletedRows"], 0)
        self.assertEqual(summary["mappingManifestStatus"], "incomplete")
        self.assertFalse(summary["mappingManifestComplete"])

    def test_raw_sample_index_cache_is_reused_for_the_same_perf_data(self) -> None:
        report = (
            " 100.00%|100|1|python|  88:python|(deleted)|[.] 0x1234\n"
        )
        raw_index = {
            (88, 0x1234): {
                "buildid:abc": {"period": 100, "sampleCount": 1}
            }
        }
        resolution = {
            "status": "complete",
            "unresolvedDeletedRows": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "perf-report-period.txt"
            manifest = root / "perf-dso-manifest.json"
            output = root / "perf-report-period-resolved.txt"
            perf_data = root / "perf.data"
            cache = root / "raw-sample-index.json"
            source.write_text(report, encoding="utf-8")
            perf_data.write_bytes(b"perf")
            manifest.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "status": "complete",
                        "identityPolicy": IDENTITY_POLICY,
                        "objects": {},
                        "mappings": [],
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim."
                    "perf_symbol_bundle.build_raw_sample_index",
                    return_value=raw_index,
                ) as build,
                patch(
                    "pyframework_pipeline.adapters.volcoperatorsim."
                    "perf_symbol_bundle.resolve_period_report",
                    return_value=("resolved\n", dict(resolution)),
                ) as resolve,
            ):
                for _ in range(2):
                    write_resolved_period_report(
                        source=source,
                        manifest_path=manifest,
                        output=output,
                        perf_data=perf_data,
                        raw_index_cache=cache,
                    )

        build.assert_called_once()
        self.assertEqual(
            resolve.call_args_list[1].kwargs["raw_sample_index"],
            raw_index,
        )

    def test_perf_buildid_cache_timeout_is_a_bounded_manifest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = root / "objects/library.elf"
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b"elf")

            with patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "subprocess.run",
                side_effect=TimeoutExpired(["perf", "buildid-cache"], 30),
            ) as run:
                failures = _populate_perf_buildid_cache(
                    real_perf="/usr/bin/perf",
                    cache_root=root,
                    objects={"sha256:abc": {"cachePath": "objects/library.elf"}},
                )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["objectId"], "sha256:abc")
        self.assertEqual(failures[0]["failureType"], "timeout")
        self.assertIn("timed out", failures[0]["error"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)

    def test_symbol_lookup_keys_remove_return_type_and_argument_signature(self) -> None:
        demangled = (
            "bool std::__detail::__regex_algo_impl<"
            "char, (std::__detail::_RegexExecutorPolicy)0, true>(char const*)"
        )
        perf_name = (
            "std::__detail::__regex_algo_impl<"
            "char, (std::__detail::_RegexExecutorPolicy)0, true>"
        )

        self.assertIn(perf_name, _symbol_lookup_keys(demangled))
        self.assertIn(perf_name, _symbol_lookup_keys(perf_name))

    def test_batch_symbol_names_preserves_aliases_at_the_same_address(self) -> None:
        static = CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        dynamic = CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "00000100 00000020 T __libc_free\n"
                "00000100 00000020 W cfree\n"
                "00000200 00000010 T next_symbol\n"
            ),
            stderr="",
        )
        plt = CompletedProcess(
            args=[],
            returncode=0,
            stdout="00000300 <plt_only@plt>:\n 300: nop\n",
            stderr="",
        )
        with patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "subprocess.run",
            side_effect=[static, dynamic, plt],
        ):
            symbols = _batch_symbol_names(
                Path("/bundle/libc.so"), [0x108, 0x220, 0x308]
            )

        self.assertEqual(symbols[0x108], frozenset({"__libc_free", "cfree"}))
        self.assertEqual(symbols[0x220], frozenset())
        self.assertEqual(symbols[0x308], frozenset({"plt_only"}))

    def test_batch_symbolize_uses_one_addr2line_process_for_all_addresses(self) -> None:
        completed = CompletedProcess(
            args=[],
            returncode=0,
            stdout="function_a\na.cc:10\nfunction_b\nb.cc:20\n",
            stderr="",
        )
        with patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "subprocess.run",
            return_value=completed,
        ) as run:
            symbols = _batch_symbolize(Path("/bundle/liba.so"), [0x10, 0x20])

        self.assertEqual(
            symbols,
            {
                0x10: ("function_a", "a.cc:10"),
                0x20: ("function_b", "b.cc:20"),
            },
        )
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["input"], "0x10\n0x20\n")

    def test_defined_symbols_reads_full_symtab_not_only_dynamic_exports(self) -> None:
        static = CompletedProcess(
            args=[],
            returncode=0,
            stdout="00000010 t local_cpp_symbol\n",
            stderr="",
        )
        dynamic = CompletedProcess(
            args=[],
            returncode=0,
            stdout="00000020 T exported_symbol@@V1\n",
            stderr="",
        )
        with patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "subprocess.run",
            side_effect=[static, dynamic],
        ) as run:
            symbols = _defined_symbols(Path("/bundle/gcs_server"))

        self.assertEqual(symbols, frozenset({"local_cpp_symbol", "exported_symbol"}))
        arguments = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all("--defined-only" in item for item in arguments))
        self.assertTrue(any("-D" not in item for item in arguments))
        self.assertTrue(any("-D" in item for item in arguments))

    def test_raw_perf_mmap2_recovers_a_short_lived_process_by_build_id(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc123": {
                    "buildId": "abc123",
                    "cachePath": "objects/abc123.elf",
                    "soname": "libshortlived.so",
                }
            },
            "mappings": [],
        }
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0x2000 <abc123>]: r-xp / (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(lines, manifest)

        self.assertEqual(
            index[(222, 0x2123)],
            {"buildid:abc123": {"period": 100, "sampleCount": 1}},
        )
        self.assertEqual(
            index[("absolute", 222, 0x7123)],
            {
                "buildid:abc123": {
                    "period": 100,
                    "sampleCount": 1,
                    "relativeIp": 0x2123,
                }
            },
        )
        self.assertEqual(
            manifest["objects"]["buildid:abc123"]["identityEvidence"],
            "perf-build-id",
        )

    def test_raw_perf_retains_mmap_identity_when_deleted_elf_is_gone(self) -> None:
        manifest = {"schemaVersion": 1, "objects": {}, "mappings": []}
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 <abc123>]: r-xp /tmp/libgone.so (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(
            lines,
            manifest,
            object_loader=lambda _path, _build_id, _comm: "",
        )

        self.assertEqual(
            index[("absolute", 222, 0x7123)],
            {
                "buildid:abc123": {
                    "period": 100,
                    "sampleCount": 1,
                    "relativeIp": 0x123,
                }
            },
        )
        self.assertEqual(
            manifest["objects"]["buildid:abc123"],
            {
                "buildId": "abc123",
                "capturedFrom": "perf-mmap2",
                "identityEvidence": "perf-build-id",
                "identityPolicy": "exact-buildid-or-live-procfs-v2",
                "metadataOnly": True,
                "originalPath": "/tmp/libgone.so",
            },
        )

    def test_raw_perf_static_mapping_fallback_uses_indexed_smallest_interval(
        self,
    ) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:wide": {},
                "buildid:narrow": {},
                "buildid:irrelevant": {},
            },
            "mappings": [
                *[
                    {
                        "pid": 1000 + index,
                        "tids": [1000 + index],
                        "start": "0x1000",
                        "end": "0x9000",
                        "offset": "0x0",
                        "objectId": "buildid:irrelevant",
                    }
                    for index in range(1000)
                ],
                {
                    "pid": 111,
                    "tids": [111, 222],
                    "start": "0x7000",
                    "end": "0x9000",
                    "offset": "0x0",
                    "objectId": "buildid:wide",
                },
                {
                    "pid": 111,
                    "tids": [111, 222],
                    "start": "0x7100",
                    "end": "0x7200",
                    "offset": "0x10",
                    "objectId": "buildid:narrow",
                },
            ],
        }
        lines = [
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 100 addr: 0\n",
            "1 0x89 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 50 addr: 0\n",
        ]

        index = index_raw_perf_lines(lines, manifest)

        self.assertEqual(
            index[(222, 0x33)],
            {"buildid:narrow": {"period": 150, "sampleCount": 2}},
        )

    def test_raw_perf_mmap2_lazily_archives_a_stable_short_lived_binary(self) -> None:
        manifest = {"schemaVersion": 1, "objects": {}, "mappings": []}
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 103:0d 123 456]: r-xp /usr/bin/ps\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/111: 0x7123 period: 100 addr: 0\n",
        ]
        requested: list[tuple[str, str, str]] = []

        def archive(path: str, build_id: str, comm: str) -> str:
            requested.append((path, build_id, comm))
            manifest["objects"]["buildid:ps"] = {
                "cachePath": "objects/ps.elf",
                "originalPath": path,
            }
            return "buildid:ps"

        index = index_raw_perf_lines(lines, manifest, object_loader=archive)

        self.assertEqual(requested, [("/usr/bin/ps", "", "")])
        self.assertEqual(
            index[(111, 0x123)]["buildid:ps"],
            {"period": 100, "sampleCount": 1},
        )

    def test_raw_perf_deleted_mmap_passes_exec_comm_to_object_loader(self) -> None:
        manifest = {"schemaVersion": 1, "objects": {}, "mappings": []}
        lines = [
            "1 0x10 [0x28]: PERF_RECORD_COMM exec: grep:111/111\n",
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 <abc123>]: r-xp / (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/111: 0x7123 period: 100 addr: 0\n",
        ]
        requested: list[tuple[str, str, str]] = []

        def archive(path: str, build_id: str, comm: str) -> str:
            requested.append((path, build_id, comm))
            manifest["objects"]["buildid:abc123"] = {
                "cachePath": "objects/grep.elf",
                "originalPath": "/usr/bin/grep",
            }
            return "buildid:abc123"

        index = index_raw_perf_lines(lines, manifest, object_loader=archive)

        self.assertEqual(requested, [("/", "abc123", "grep")])
        self.assertIn("buildid:abc123", index[(111, 0x123)])

    def test_deleted_mmap_without_build_id_never_guesses_from_process_name(self) -> None:
        manifest = {"schemaVersion": 1, "objects": {}, "mappings": []}
        lines = [
            "1 0x10 [0x28]: PERF_RECORD_COMM exec: python:111/111\n",
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 00:00 0 0]: r-xp / (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/111: 0x7123 period: 100 addr: 0\n",
        ]
        requested: list[tuple[str, str, str]] = []

        def unsafe_guess(path: str, build_id: str, comm: str) -> str:
            requested.append((path, build_id, comm))
            return "sha256:wrong-python"

        index = index_raw_perf_lines(
            lines, manifest, object_loader=unsafe_guess
        )

        self.assertEqual(requested, [])
        candidates = index[(111, 0x123)]
        self.assertNotIn("sha256:wrong-python", candidates)
        object_id = next(iter(candidates))
        self.assertEqual(
            manifest["objects"][object_id]["identityEvidence"],
            "ambiguous-perf-mmap",
        )

    def test_exact_live_procfs_mapping_wins_for_anonymous_deleted_mmap(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "sha256:real": {
                    "cachePath": "objects/real.elf",
                    "identityEvidence": "procfs-map-files",
                    "originalPath": "/",
                },
                "sha256:wrong": {
                    "cachePath": "objects/wrong.elf",
                    "identityEvidence": "current-absolute-path",
                    "originalPath": "/",
                },
            },
            "mappings": [
                {
                    "pid": 111,
                    "tids": [111, 222],
                    "start": "0x7000",
                    "end": "0x8000",
                    "offset": "0x0",
                    "device": "00:00",
                    "inode": 0,
                    "objectId": "sha256:real",
                    "deleted": True,
                }
            ],
        }
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 00:00 0 0]: r-xp / (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/222: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(
            lines,
            manifest,
            object_loader=lambda *_args: self.fail(
                "exact procfs identity must not use a heuristic loader"
            ),
        )

        self.assertEqual(
            index[(222, 0x123)],
            {"sha256:real": {"period": 100, "sampleCount": 1}},
        )

    def test_raw_build_id_conflict_does_not_reuse_path_cached_object(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:old": {
                    "buildId": "old",
                    "identityEvidence": "perf-build-id",
                    "originalPath": "/tmp/libworker.so",
                }
            },
            "mappings": [],
        }
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 <aabbcc>]: r-xp "
            "/tmp/libworker.so (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/111: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(lines, manifest)

        self.assertNotIn("buildid:old", index[(111, 0x123)])
        self.assertIn("buildid:aabbcc", index[(111, 0x123)])
        self.assertEqual(
            manifest["objects"]["buildid:aabbcc"]["identityEvidence"],
            "perf-build-id",
        )

    def test_raw_mmap_inode_conflict_rejects_stale_procfs_mapping(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "sha256:old": {
                    "identityEvidence": "procfs-map-files",
                    "originalPath": "/tmp/libworker.so",
                }
            },
            "mappings": [
                {
                    "pid": 111,
                    "tids": [111],
                    "start": "0x7000",
                    "end": "0x8000",
                    "offset": "0x0",
                    "device": "08:01",
                    "inode": 123,
                    "objectId": "sha256:old",
                    "deleted": True,
                }
            ],
        }
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 08:01 999 1]: r-xp "
            "/tmp/libworker.so (deleted)\n",
            "1 0x88 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "111/111: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(lines, manifest)

        self.assertNotIn("sha256:old", index[(111, 0x123)])
        object_id = next(iter(index[(111, 0x123)]))
        self.assertTrue(object_id.startswith("perf-mmap:"))
        self.assertEqual(
            manifest["objects"][object_id]["identityEvidence"],
            "ambiguous-perf-mmap",
        )

    def test_raw_perf_fork_inherits_parent_runtime_mappings(self) -> None:
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc123": {
                    "buildId": "abc123",
                    "cachePath": "objects/libc.elf",
                    "soname": "libc.so.6",
                }
            },
            "mappings": [],
        }
        lines = [
            "1 0x20 [0x68]: PERF_RECORD_MMAP2 111/111: "
            "[0x7000(0x1000) @ 0 <abc123>]: r-xp /lib/libc.so.6\n",
            "2 0x30 [0x30]: PERF_RECORD_FORK(222:222):(111:111)\n",
            "3 0x40 [0x40]: PERF_RECORD_SAMPLE(IP, 0x1): "
            "222/222: 0x7123 period: 100 addr: 0\n",
        ]

        index = index_raw_perf_lines(lines, manifest)

        self.assertEqual(
            index[("absolute", 222, 0x7123)]["buildid:abc123"],
            {"period": 100, "sampleCount": 1, "relativeIp": 0x123},
        )

    def test_archive_stable_perf_object_copies_elf_into_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {"schemaVersion": 1, "objects": {}}

            object_id = _archive_stable_perf_object(
                sys.executable,
                expected_build_id="",
                bundle_root=root,
                manifest=manifest,
            )

            item = manifest["objects"][object_id]
            captured = root / item["cachePath"]
            self.assertEqual(captured.read_bytes()[:4], b"\x7fELF")
            self.assertEqual(item["originalPath"], sys.executable)

    def test_archive_stable_perf_object_upgrades_metadata_only_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "schemaVersion": 1,
                "objects": {
                    "perf-mmap:old": {
                        "metadataOnly": True,
                        "originalPath": sys.executable,
                    }
                },
            }

            object_id = _archive_stable_perf_object(
                sys.executable,
                expected_build_id="",
                bundle_root=root,
                manifest=manifest,
            )

            item = manifest["objects"][object_id]
            self.assertFalse(item["metadataOnly"])
            self.assertTrue((root / item["cachePath"]).is_file())

    def test_archive_anonymous_mapping_without_build_id_does_not_use_comm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "shutil.which",
            return_value=sys.executable,
        ) as which:
            object_id = _archive_stable_perf_object(
                "/",
                expected_build_id="",
                bundle_root=Path(tmp),
                manifest={"schemaVersion": 1, "objects": {}},
                comm="python",
            )

        self.assertEqual(object_id, "")
        which.assert_not_called()

    def test_collector_copies_deleted_elf_while_map_files_is_reachable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            process = proc_root / "77"
            (process / "task/77").mkdir(parents=True)
            (process / "map_files").mkdir()
            (process / "maps").write_text(
                "00400000-00452000 r-xp 00000000 08:01 123 / (deleted)\n",
                encoding="utf-8",
            )
            os.symlink(
                sys.executable,
                process / "map_files/400000-452000",
            )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root
            )

            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(manifest["mappings"]), 1)
            object_id = manifest["mappings"][0]["objectId"]
            captured = root / "bundle" / manifest["objects"][object_id]["cachePath"]
            self.assertTrue(captured.is_file())
            self.assertEqual(captured.read_bytes()[:4], b"\x7fELF")

    def test_collector_finalizes_stable_mappings_for_perf_namespace_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            process = proc_root / "88"
            (process / "task/88").mkdir(parents=True)
            (process / "maps").write_text(
                f"00500000-00552000 r-xp 00000000 08:01 456 {sys.executable}\n",
                encoding="utf-8",
            )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root
            )

            collector.scan()
            collector.finalize()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(manifest["status"], "complete")
        self.assertTrue(manifest["mappings"][0]["objectId"])

    def test_collector_retries_when_a_stable_mapping_later_becomes_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            process = proc_root / "77"
            (process / "task/77").mkdir(parents=True)
            (process / "map_files").mkdir()
            maps = process / "maps"
            maps.write_text(
                "00400000-00452000 r-xp 00000000 08:01 123 /tmp/runtime.so\n",
                encoding="utf-8",
            )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root
            )

            collector.scan()
            (process / "task/99").mkdir()
            maps.write_text(
                "00400000-00452000 r-xp 00000000 08:01 123 / (deleted)\n",
                encoding="utf-8",
            )
            os.symlink(
                sys.executable,
                process / "map_files/400000-452000",
            )
            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["captureMode"], "live-procfs-poll")
        self.assertEqual(manifest["deletedMappingsObserved"], 1)
        self.assertEqual(manifest["capturedDeletedMappings"], 1)
        self.assertTrue(manifest["mappings"][0]["deleted"])
        self.assertEqual(manifest["mappings"][0]["tids"], [77, 99])
        self.assertTrue(manifest["mappings"][0]["objectId"])

    def test_collector_empty_scan_is_not_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            proc_root.mkdir()
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root
            )

            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(manifest["mappingsObserved"], 0)

    def test_collector_scans_only_the_recorded_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            for pid, children, address in (
                (10, "11", "00400000"),
                (11, "12", "00500000"),
                (12, "", "00600000"),
                (99, "", "00900000"),
            ):
                process = proc_root / str(pid)
                (process / f"task/{pid}").mkdir(parents=True)
                (process / f"task/{pid}/children").write_text(
                    children, encoding="utf-8"
                )
                start = int(address, 16)
                (process / "maps").write_text(
                    f"{start:08x}-{start + 0x1000:08x} r-xp 00000000 "
                    f"08:01 {pid} {sys.executable}\n",
                    encoding="utf-8",
                )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle",
                proc_root=proc_root,
                root_pid=10,
            )

            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(manifest["processesObserved"], 3)
        self.assertEqual(
            {item["pid"] for item in manifest["mappings"]}, {10, 11, 12}
        )

    def test_collector_discovers_children_created_by_nonleader_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            leader = proc_root / "10"
            (leader / "task/10").mkdir(parents=True)
            (leader / "task/20").mkdir()
            (leader / "task/10/children").write_text("", encoding="utf-8")
            (leader / "task/20/children").write_text("11", encoding="utf-8")
            (leader / "maps").write_text(
                f"00400000-00401000 r-xp 00000000 08:01 10 {sys.executable}\n",
                encoding="utf-8",
            )
            child = proc_root / "11"
            (child / "task/11").mkdir(parents=True)
            (child / "task/11/children").write_text("", encoding="utf-8")
            (child / "maps").write_text(
                f"00500000-00501000 r-xp 00000000 08:01 11 {sys.executable}\n",
                encoding="utf-8",
            )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root, root_pid=10
            )

            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(
            {item["pid"] for item in manifest["mappings"]}, {10, 11}
        )

    def test_collector_content_verifies_reused_inode_before_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.elf"
            second = root / "second.elf"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            collector = DeletedMappingCollector(cache_root=root / "bundle")
            common = {
                "pid": 77,
                "tids": (77,),
                "end": 0x2000,
                "permissions": "r-xp",
                "offset": 0,
                "device": "08:01",
                "inode": 123,
                "deleted": True,
            }

            with patch(
                "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
                "inspect_elf",
                side_effect=[
                    {"objectId": "sha256:first", "sha256": "first"},
                    {"objectId": "sha256:second", "sha256": "second"},
                ],
            ) as inspect:
                first_id = collector._capture_source(
                    first,
                    ProcMap(start=0x1000, path="/tmp/first.so", **common),
                )
                second_id = collector._capture_source(
                    second,
                    ProcMap(start=0x3000, path="/tmp/second.so", **common),
                )

        self.assertEqual(first_id, "sha256:first")
        self.assertEqual(second_id, "sha256:second")
        self.assertEqual(inspect.call_count, 2)

    def test_collector_retries_deleted_capture_without_duplicate_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc_root = root / "proc"
            process = proc_root / "77"
            (process / "task/77").mkdir(parents=True)
            (process / "maps").write_text(
                "00400000-00452000 r-xp 00000000 08:01 123 / (deleted)\n",
                encoding="utf-8",
            )
            collector = DeletedMappingCollector(
                cache_root=root / "bundle", proc_root=proc_root
            )

            collector.scan()
            collector.scan()
            manifest = collector.manifest(perf_data=root / "case/perf.data")

        self.assertEqual(manifest["status"], "incomplete")
        self.assertEqual(len(manifest["captureErrors"]), 1)

    def test_parse_proc_maps_preserves_deleted_executable_identity(self) -> None:
        records = parse_proc_maps(
            "00400000-00452000 r-xp 00002000 08:01 123 /tmp/libfoo.so (deleted)\n"
            "00652000-00653000 r--p 00052000 08:01 123 /tmp/libfoo.so (deleted)\n"
            "7fff0000-7fff1000 r-xp 00000000 00:00 0 [vdso]\n",
            pid=77,
            tids=(77, 79),
        )

        self.assertEqual(
            records,
            (
                ProcMap(
                    pid=77,
                    tids=(77, 79),
                    start=0x00400000,
                    end=0x00452000,
                    permissions="r-xp",
                    offset=0x2000,
                    device="08:01",
                    inode=123,
                    path="/tmp/libfoo.so",
                    deleted=True,
                ),
            ),
        )

    def test_resolve_period_report_uses_tid_mapping_and_elf_soname(self) -> None:
        report = (
            "# Overhead|Period|Samples|Command|Pid:Command|Shared Object|Symbol\n"
            " 80.00%|800|8|DAFTCPU-0|  79:DAFTCPU-0|(deleted)|[.] 0x0000000000003234\n"
            "            |\n"
            "            ---0x0000007f00001234\n"
            " 20.00%|200|2|python|  77:python|python3.10|[.] PyEval_EvalFrameDefault\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc": {
                    "cachePath": "_symbol-cache/objects/abc.elf",
                    "soname": "libarm_compute.so",
                    "buildId": "abc",
                    "identityEvidence": "procfs-map-files",
                }
            },
            "mappings": [
                {
                    "pid": 77,
                    "tids": [77, 79],
                    "start": "0x7f00000000",
                    "end": "0x7f00100000",
                    "offset": "0x2000",
                    "objectId": "buildid:abc",
                    "deleted": True,
                }
            ],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            symbolizer=lambda path, address: (
                "arm_gemm::a64_ffhybrid_fp32_mla_6x16",
                f"{path.name}:0x{address:x}",
            ),
        )

        self.assertIn("libarm_compute.so", resolved)
        self.assertIn("arm_gemm::a64_ffhybrid_fp32_mla_6x16", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["deletedRowsBefore"], 1)
        self.assertEqual(summary["resolvedDeletedRows"], 1)
        self.assertEqual(summary["unresolvedDeletedRows"], 0)
        self.assertEqual(summary["status"], "complete")

    def test_legacy_buildid_label_without_identity_evidence_is_not_trusted(self) -> None:
        report = (
            "100.00|800|8|worker|79:worker|(deleted)|[.] 0x0000000000003234\n"
            "      |\n"
            "      ---0x0000007f00001234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc": {
                    "cachePath": "objects/abc.elf",
                    "soname": "libguessed.so",
                    "buildId": "abc",
                }
            },
            "mappings": [
                {
                    "pid": 79,
                    "tids": [79],
                    "start": "0x7f00000000",
                    "end": "0x7f00100000",
                    "offset": "0x2000",
                    "objectId": "buildid:abc",
                    "deleted": True,
                }
            ],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            symbolizer=lambda _path, _address: ("guessed", ""),
        )

        self.assertIn("(deleted)", resolved)
        self.assertNotIn("libguessed.so", resolved)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["ambiguousDeletedRows"], 1)

    def test_resolve_period_report_correlates_relative_ip_with_raw_sample(self) -> None:
        report = (
            " 100.00%|800|8|DAFTCPU-0|  79:DAFTCPU-0|(deleted)|[.] 0x3234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:abc": {
                    "cachePath": "objects/abc.elf",
                    "soname": "libarm_compute.so",
                    "identityEvidence": "perf-build-id",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                (79, 0x3234): {
                    "buildid:abc": {"period": 800, "sampleCount": 8}
                }
            },
            symbolizer=lambda _path, _address: ("arm_gemm::kernel", ""),
        )

        self.assertIn("libarm_compute.so", resolved)
        self.assertIn("arm_gemm::kernel", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_resolve_named_row_uses_absolute_addr_and_mmap_metadata(self) -> None:
        report = (
            " 100.00%|800|8|DAFTCPU-0|  79:DAFTCPU-0|(deleted)|"
            "[.] torch::FunctionSignature::parse|0x7f00101234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:torch": {
                    "buildId": "torch",
                    "capturedFrom": "perf-mmap2",
                    "identityEvidence": "perf-build-id",
                    "metadataOnly": True,
                    "originalPath": "/tmp/libtorch_python.so",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                ("absolute", 79, 0x7F00101234): {
                    "buildid:torch": {
                        "period": 800,
                        "sampleCount": 8,
                        "relativeIp": 0x101234,
                    }
                }
            },
        )

        self.assertIn("libtorch_python.so", resolved)
        self.assertIn("torch::FunctionSignature::parse", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(
            summary["resolutions"][0]["mappingSource"], "perf-mmap2-metadata"
        )

    def test_resolve_named_row_accepts_short_absolute_addr(self) -> None:
        report = (
            " 100.00%|800|8|DAFTCPU-0|  79:DAFTCPU-0|(deleted)|"
            "[.] ucs2lib_utf8_encoder|0x54cd44\n"
            "            0xffffb021fbb4\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:python": {
                    "buildId": "python",
                    "capturedFrom": "perf-mmap2",
                    "identityEvidence": "perf-build-id",
                    "metadataOnly": True,
                    "originalPath": "/opt/python314/bin/python3.14",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                ("absolute", 79, 0x54CD44): {
                    "buildid:python": {
                        "period": 1600,
                        "sampleCount": 16,
                        "relativeIp": 0x14CD44,
                    }
                }
            },
        )

        self.assertIn("python3.14", resolved)
        self.assertIn("ucs2lib_utf8_encoder", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_raw_absolute_mapping_overrides_conflicting_procfs_snapshot(
        self,
    ) -> None:
        report = (
            " 100.00%|800|8|DAFTCPU-0|  79:DAFTCPU-0|(deleted)|"
            "[.] ucs2lib_utf8_encoder|0x54cd44\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:libc": {
                    "cachePath": "objects/libc.elf",
                    "soname": "libc.so.6",
                    "identityEvidence": "procfs-map-files",
                },
                "buildid:python": {
                    "cachePath": "objects/python.elf",
                    "soname": "python3.14",
                    "identityEvidence": "perf-build-id",
                },
            },
            "mappings": [
                {
                    "pid": 79,
                    "tids": [79],
                    "start": "0x500000",
                    "end": "0x600000",
                    "offset": "0x0",
                    "objectId": "buildid:libc",
                }
            ],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                ("absolute", 79, 0x54CD44): {
                    "buildid:python": {
                        "period": 1600,
                        "sampleCount": 16,
                        "relativeIp": 0x14CD44,
                    }
                }
            },
            symbolizer=lambda _path, _address: ("wrong_snapshot_symbol", ""),
        )

        self.assertIn("python3.14", resolved)
        self.assertNotIn("libc.so.6", resolved)
        self.assertNotIn("wrong_snapshot_symbol", resolved)
        self.assertEqual(
            summary["resolutions"][0]["mappingSource"], "perf-mmap2"
        )

    def test_resolve_named_row_keeps_ambiguous_deleted_identity_unresolved(self) -> None:
        report = (
            " 100.00%|800|8|python|  79:python|(deleted)|"
            "[.] hot_native_code|0x7f00101234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "perf-mmap:anonymous": {
                    "capturedFrom": "perf-mmap2",
                    "identityEvidence": "ambiguous-perf-mmap",
                    "metadataOnly": True,
                    "originalPath": "/",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                ("absolute", 79, 0x7F00101234): {
                    "perf-mmap:anonymous": {
                        "period": 800,
                        "sampleCount": 8,
                        "relativeIp": 0x101234,
                    }
                }
            },
        )

        self.assertIn("(deleted)", resolved)
        self.assertNotIn("|python                             ", resolved)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["ambiguousDeletedRows"], 1)
        self.assertEqual(
            summary["resolutions"][0]["reason"],
            "ambiguous_deleted_mapping_identity",
        )

    def test_resolve_numeric_row_rejects_metadata_without_elf_symbols(self) -> None:
        report = (
            " 100.00%|800|8|worker|  79:worker|(deleted)|"
            "[.] 0x0000000000001234|0x7f00101234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:gone": {
                    "buildId": "gone",
                    "capturedFrom": "perf-mmap2",
                    "identityEvidence": "perf-build-id",
                    "metadataOnly": True,
                    "originalPath": "/tmp/libgone.so",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                ("absolute", 79, 0x7F00101234): {
                    "buildid:gone": {
                        "period": 800,
                        "sampleCount": 8,
                        "relativeIp": 0x101234,
                    }
                }
            },
        )

        self.assertIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(
            summary["resolutions"][0]["reason"], "captured_elf_not_available"
        )

    def test_resolve_batches_absolute_numeric_addresses_once_per_elf(self) -> None:
        report = (
            " 60.00%|600|6|worker|  79:worker|(deleted)|[.] 0x110|0x7110\n"
            " 40.00%|400|4|worker|  79:worker|(deleted)|[.] 0x120|0x7120\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:worker": {
                    "cachePath": "objects/worker.elf",
                    "soname": "libworker.so",
                    "identityEvidence": "perf-build-id",
                }
            },
            "mappings": [],
        }
        raw_index = {
            (79, 0x110): {
                "buildid:worker": {"period": 600, "sampleCount": 6}
            },
            (79, 0x120): {
                "buildid:worker": {"period": 400, "sampleCount": 4}
            },
            ("absolute", 79, 0x7110): {
                "buildid:worker": {
                    "period": 600,
                    "sampleCount": 6,
                    "relativeIp": 0x110,
                }
            },
            ("absolute", 79, 0x7120): {
                "buildid:worker": {
                    "period": 400,
                    "sampleCount": 4,
                    "relativeIp": 0x120,
                }
            },
        }

        with patch(
            "pyframework_pipeline.adapters.volcoperatorsim.perf_symbol_bundle."
            "_batch_symbolize",
            return_value={
                0x110: ("worker_a", "a.cc:1"),
                0x120: ("worker_b", "b.cc:2"),
            },
        ) as batch:
            resolved, summary = resolve_period_report(
                report,
                manifest,
                bundle_root=Path("/bundle"),
                raw_sample_index=raw_index,
            )

        batch.assert_called_once_with(
            Path("/bundle/objects/worker.elf"), {0x110, 0x120}
        )
        self.assertIn("worker_a", resolved)
        self.assertIn("worker_b", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_resolve_period_report_rejects_ambiguous_relative_ip(self) -> None:
        report = (
            " 100.00%|800|8|worker|  79:worker|(deleted)|[.] 0x3234\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:a": {
                    "cachePath": "objects/a.elf",
                    "soname": "liba.so",
                },
                "buildid:b": {
                    "cachePath": "objects/b.elf",
                    "soname": "libb.so",
                },
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                (79, 0x3234): {
                    "buildid:a": {"period": 800, "sampleCount": 8},
                    "buildid:b": {"period": 800, "sampleCount": 8},
                }
            },
            symbolizer=lambda _path, _address: ("same_symbol", ""),
        )

        self.assertIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["unresolvedDeletedRows"], 1)

    def test_resolve_symbol_only_deleted_row_by_exact_raw_sample_totals(self) -> None:
        report = (
            " 100.00%|800|8|python|  79:python|(deleted)|[.] pthread_cond_signal\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:libc": {
                    "cachePath": "objects/libc.elf",
                    "soname": "libc.so.6",
                    "identityEvidence": "perf-build-id",
                }
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                (79, 0x86AA8): {
                    "buildid:libc": {"period": 800, "sampleCount": 8}
                },
            },
            symbolizer=lambda _path, _address: ("unknown_symbol", ""),
        )

        self.assertIn("libc.so.6", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_equal_period_group_excludes_non_deleted_reported_dso(self) -> None:
        report = (
            " 50.00%|800|1|python|  79:python|python3.10|[.] PyEval_EvalFrame\n"
            " 50.00%|800|1|worker|  79:worker|(deleted)|[.] shared_helper\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:python": {
                    "cachePath": "objects/python.elf",
                    "originalPath": "/opt/conda/bin/python3.10",
                    "identityEvidence": "perf-build-id",
                },
                "buildid:worker": {
                    "cachePath": "objects/worker.elf",
                    "soname": "libworker.so",
                    "identityEvidence": "perf-build-id",
                },
            },
            "mappings": [],
        }

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                (79, 0x100): {
                    "buildid:python": {"period": 800, "sampleCount": 1}
                },
                (79, 0x200): {
                    "buildid:worker": {"period": 800, "sampleCount": 1}
                },
            },
            symbolizer=lambda _path, _address: ("ambiguous_helper", ""),
        )

        self.assertIn("libworker.so", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_resolve_known_symbol_when_one_sampled_elf_defines_it(self) -> None:
        report = (
            " 100.00%|999|9|gcs_server|  79:gcs_server|(deleted)|"
            "[.] local_cpp_symbol\n"
        )
        manifest = {
            "schemaVersion": 1,
            "objects": {
                "buildid:gcs": {
                    "cachePath": "objects/gcs.elf",
                    "soname": "gcs_server",
                    "identityEvidence": "perf-build-id",
                },
                "buildid:libc": {
                    "cachePath": "objects/libc.elf",
                    "soname": "libc.so.6",
                    "identityEvidence": "perf-build-id",
                },
            },
            "mappings": [],
        }

        def symbolize(path: Path, _address: int) -> tuple[str, str]:
            return (
                ("bool local_cpp_symbol(int, std::string const&)", "")
                if path.name == "gcs.elf"
                else ("malloc", "")
            )

        resolved, summary = resolve_period_report(
            report,
            manifest,
            bundle_root=Path("/bundle"),
            raw_sample_index={
                (79, 0x100): {
                    "buildid:gcs": {"period": 500, "sampleCount": 5}
                },
                (79, 0x200): {
                    "buildid:libc": {"period": 200, "sampleCount": 2}
                },
            },
            symbolizer=symbolize,
        )

        self.assertIn("gcs_server", resolved)
        self.assertNotIn("(deleted)", resolved)
        self.assertEqual(summary["status"], "complete")

    def test_required_resolution_rejects_a_remaining_deleted_mapping(self) -> None:
        report = (
            " 100.00%|100|1|python|  88:python|(deleted)|[.] 0x1234\n"
            "            |\n"
            "            ---0x0000007f00001234\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "perf-report-period.txt"
            manifest = root / "perf-dso-manifest.json"
            output = root / "perf-report-period-resolved.txt"
            source.write_text(report, encoding="utf-8")
            manifest.write_text(
                json.dumps({"schemaVersion": 1, "objects": {}, "mappings": []}),
                encoding="utf-8",
            )

            with self.assertRaises(UnresolvedDeletedMappings):
                write_resolved_period_report(
                    source=source,
                    manifest_path=manifest,
                    output=output,
                    require_complete=True,
                )

            summary = json.loads(
                (root / "perf-symbol-resolution.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["unresolvedDeletedRows"], 1)


if __name__ == "__main__":
    unittest.main()
