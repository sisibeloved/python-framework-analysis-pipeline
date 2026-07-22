"""Tests for data acquisition module."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_YAML = REPO_ROOT / "projects" / "pyflink-tpch-reference" / "project.yaml"


class CliInvoker:
    """Helper to run the pipeline CLI as a subprocess."""

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


class TimingCollectorTest(unittest.TestCase):
    """Test timing data collection from TM stdout logs."""

    SAMPLE_STDOUT = """\
[PreUDF Start]: Start Time(ns)=1000000
[Benchmark Counter]: Count=1000000, Avg Overhead=95000.0 ns, Last Overhead=94000 ns
[PostUDF End]: End Time(ns)=5000000000
[BENCHMARK_SUMMARY] {"recordCount": 1000000, "avgFrameworkOverheadNs": 95811.0, "avgPyDurationNs": 324.5, "totalFrameworkOverheadNs": 95811980, "totalPyDurationNs": 324500}
"""

    def test_parse_benchmark_summary(self) -> None:
        from pipelines.pyframework_pipeline.acquisition.timing import _parse_summaries

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "tm-stdout.log"
            log_file.write_text(self.SAMPLE_STDOUT, encoding="utf-8")

            summaries = _parse_summaries(log_file)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]["recordCount"], 1000000)
            self.assertAlmostEqual(summaries[0]["avgFrameworkOverheadNs"], 95811.0)

    def test_parse_multiple_summaries(self) -> None:
        from pipelines.pyframework_pipeline.acquisition.timing import _parse_summaries

        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "tm-stdout.log"
            log_file.write_text(self.SAMPLE_STDOUT + self.SAMPLE_STDOUT, encoding="utf-8")

            summaries = _parse_summaries(log_file)
            self.assertEqual(len(summaries), 2)

    def test_collect_timing_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "2026-04-16-arm"
            run_dir.mkdir()

            # Create a sample stdout file
            stdout_file = run_dir / "tm-stdout.log"
            stdout_file.write_text(self.SAMPLE_STDOUT, encoding="utf-8")

            result = CliInvoker.run(
                "acquire", "timing", str(PROJECT_YAML),
                "--platform", "arm",
                "--run-dir", str(run_dir),
                "--stdout-file", str(stdout_file),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Check timing output
            normalized = json.loads((run_dir / "timing" / "timing-normalized.json").read_text())
            self.assertIn("cases", normalized)
            self.assertEqual(len(normalized["cases"]), 1)
            self.assertEqual(normalized["cases"][0]["recordCount"], 1000000)
            self.assertIn("frameworkCallTime", normalized["cases"][0]["metrics"])
            self.assertIn("businessOperatorTime", normalized["cases"][0]["metrics"])

            # Check manifest
            manifest = json.loads((run_dir / "acquisition-manifest.json").read_text())
            self.assertEqual(manifest["timing"]["status"], "collected")


class PerfCollectorTest(unittest.TestCase):
    """Test perf profile collection (skip when no perf.data)."""

    def test_collect_perf_skips_missing_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = CliInvoker.run(
                "acquire", "perf", str(PROJECT_YAML),
                "--platform", "arm",
                "--run-dir", str(run_dir),
            )
            # Should succeed (skip, not fail)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "skipped")


class AsmCollectorTest(unittest.TestCase):
    """Test machine code collection."""

    def test_collect_asm_skips_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            result = CliInvoker.run(
                "acquire", "asm", str(PROJECT_YAML),
                "--platform", "arm",
                "--run-dir", str(run_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "skipped")

    def test_discovers_real_asm_candidates_from_symbol_hotspots(self) -> None:
        from pipelines.pyframework_pipeline.acquisition.machine_code import (
            _discover_libs_from_perf,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "perf" / "data" / "perf_records.csv"
            hotspots = root / "perf" / "tables" / "symbol_hotspots.csv"
            records.parent.mkdir(parents=True)
            hotspots.parent.mkdir(parents=True)
            records.write_text(
                "shared_object,symbol,self\n"
                "[vdso],__kernel_clock_gettime,1.0\n"
                "(deleted),jit_deleted_symbol,2.0\n",
                encoding="utf-8",
            )
            hotspots.write_text(
                "shared_object,symbol,self_share\n"
                "[vdso],__kernel_clock_gettime,1.0\n"
                "(deleted),jit_deleted_symbol,2.0\n"
                "libscipy_openblas64_-7220728c.so,blas_thread_server,0.15\n",
                encoding="utf-8",
            )

            result = _discover_libs_from_perf(records)

        self.assertEqual(result, {"libscipy_openblas64_-7220728c.so": ["blas_thread_server"]})


class AcquisitionValidateTest(unittest.TestCase):
    """Test acquire validate command."""

    def test_validate_rejects_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = CliInvoker.run("acquire", "validate", tmp)
            self.assertEqual(result.returncode, 1)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "error")

    def test_validate_accepts_valid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = {
                "schemaVersion": 1,
                "projectId": "test",
                "platform": "arm",
                "runDir": str(run_dir),
                "timing": {"status": "pending"},
                "perf": {"status": "pending"},
                "asm": {"status": "pending"},
            }
            (run_dir / "acquisition-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            result = CliInvoker.run("acquire", "validate", str(run_dir))
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["status"], "ok")

    def test_validate_rejects_invalid_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = {
                "schemaVersion": 2,
                "projectId": "test",
                "platform": "arm",
                "runDir": str(run_dir),
                "timing": {"status": "pending"},
                "perf": {"status": "pending"},
                "asm": {"status": "pending"},
            }
            (run_dir / "acquisition-manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            result = CliInvoker.run("acquire", "validate", str(run_dir))

        self.assertEqual(result.returncode, 1)
        output = json.loads(result.stdout)
        self.assertEqual(output["status"], "error")
        self.assertIn("schemaVersion", "\n".join(output["errors"]))


class ManifestModelTest(unittest.TestCase):
    """Test AcquisitionManifest data model."""

    def test_roundtrip(self) -> None:
        from pipelines.pyframework_pipeline.acquisition.manifest import (
            AcquisitionManifest,
            AcquisitionSection,
            load_manifest,
        )

        with tempfile.TemporaryDirectory() as tmp:
            m = AcquisitionManifest(
                projectId="test-project",
                platform="arm",
                runDir="runs/test",
                timing=AcquisitionSection(
                    status="collected",
                    files={"raw": "timing/timing-raw.json"},
                ),
            )
            path = Path(tmp) / "acquisition-manifest.json"
            m.write(path)

            loaded = load_manifest(path)
            self.assertEqual(loaded.projectId, "test-project")
            self.assertEqual(loaded.timing.status, "collected")
            self.assertEqual(loaded.perf.status, "pending")
            self.assertEqual(loaded.timing.files["raw"], "timing/timing-raw.json")


class SshExecutorTest(unittest.TestCase):
    """Test SSH executor construction."""

    def test_from_string(self) -> None:
        from pipelines.pyframework_pipeline.acquisition.ssh_executor import SshExecutor

        ex = SshExecutor.from_string("root@192.168.1.1")
        self.assertEqual(ex.user, "root")
        self.assertEqual(ex.host, "192.168.1.1")

        ex2 = SshExecutor.from_string("myhost")
        self.assertEqual(ex2.user, "")
        self.assertEqual(ex2.host, "myhost")


class _FakeExecutor:
    """Minimal executor stub for unit-testing orchestrator helpers."""
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.pushed_manifests: list[dict] = []
    def run(self, cmd: str, **kw):
        self.calls.append(cmd)
        from collections import namedtuple
        R = namedtuple("R", "returncode stdout stderr")
        return R(0, "", "")
    def push_file(self, local, remote):
        if str(remote).endswith("_manifest.json"):
            self.pushed_manifests.append(json.loads(Path(local).read_text(encoding="utf-8")))
        return True
    def fetch_file(self, remote, local):
        return True
    def fetch_dir(self, remote, local):
        return True


class SymbolMapLogicTest(unittest.TestCase):
    """Test symbol_map key/value semantics used by _collect_asm_from_all_libs."""

    def test_collected_hashes_uses_keys_not_values(self):
        """existing_map is {hash: symbol}. collected_hashes must contain hashes."""
        existing_map = {"a1b2c3d4": "_Py_dict_lookup", "e5f6a7b8": "tuple_dealloc"}
        # The script does: collected_hashes = set(existing_map.keys())
        collected = set(existing_map.keys())
        # A new hash must NOT be in collected.
        import hashlib
        new_hash = hashlib.md5(b"new_symbol").hexdigest()[:8]
        self.assertNotIn(new_hash, collected)
        # Existing hashes MUST be in collected.
        self.assertIn("a1b2c3d4", collected)
        self.assertIn("e5f6a7b8", collected)

    def test_values_are_symbol_names_not_hashes(self):
        """Ensure .values() gives symbol names, NOT usable as hash set."""
        existing_map = {"a1b2c3d4": "_Py_dict_lookup"}
        values = set(existing_map.values())
        # values contains symbol names, not hashes — must NOT match an MD5 hash.
        import hashlib
        h = hashlib.md5(b"_Py_dict_lookup").hexdigest()[:8]
        self.assertNotIn(h, values)  # hash of symbol ≠ symbol name


class LibSearchLogicTest(unittest.TestCase):
    """Test the library-finding logic from the in-container ASM script."""

    def test_asm_manifest_uses_real_symbol_hotspots(self):
        from pipelines.pyframework_pipeline.orchestrator import _collect_asm_from_all_libs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            perf_dir = root / "perf" / "data"
            asm_dir = root / "asm" / "arm64"
            perf_dir.mkdir(parents=True)
            (root / "perf" / "tables").mkdir(parents=True)
            asm_dir.mkdir(parents=True)
            (perf_dir / "perf_records.csv").write_text(
                "shared_object,symbol,self\n"
                "[vdso],__kernel_clock_gettime,1.0\n"
                "(deleted),jit_deleted_symbol,2.0\n",
                encoding="utf-8",
            )
            (root / "perf" / "tables" / "symbol_hotspots.csv").write_text(
                "shared_object,symbol,self_share\n"
                "[vdso],__kernel_clock_gettime,1.0\n"
                "(deleted),jit_deleted_symbol,2.0\n"
                "libscipy_openblas64_-7220728c.so,blas_thread_server,0.15\n",
                encoding="utf-8",
            )
            executor = _FakeExecutor()

            _collect_asm_from_all_libs(
                executor, perf_dir, asm_dir, "arm", "udf-benchmarking-bench"
            )

        self.assertEqual(len(executor.pushed_manifests), 1)
        self.assertEqual(
            executor.pushed_manifests[0]["so_to_syms"],
            {"libscipy_openblas64_-7220728c.so": ["blas_thread_server"]},
        )

    def test_asm_collection_repairs_local_symbol_map_from_fetched_files(self):
        from hashlib import md5

        from pipelines.pyframework_pipeline.orchestrator import _collect_asm_from_all_libs

        class FetchingFakeExecutor(_FakeExecutor):
            def fetch_dir(self, remote, local):
                symbol_hash = md5(b"blas_thread_server").hexdigest()[:8]
                Path(local, f"{symbol_hash}.s").write_text("disassembly", encoding="utf-8")
                return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            perf_dir = root / "perf" / "data"
            asm_dir = root / "asm" / "arm64"
            perf_dir.mkdir(parents=True)
            (root / "perf" / "tables").mkdir(parents=True)
            asm_dir.mkdir(parents=True)
            (perf_dir / "perf_records.csv").write_text(
                "shared_object,symbol,self\n",
                encoding="utf-8",
            )
            (root / "perf" / "tables" / "symbol_hotspots.csv").write_text(
                "shared_object,symbol,self_share\n"
                "libscipy_openblas64_-7220728c.so,blas_thread_server,0.15\n",
                encoding="utf-8",
            )

            _collect_asm_from_all_libs(
                FetchingFakeExecutor(), perf_dir, asm_dir, "arm", "udf-benchmarking-bench"
            )
            symbol_map = json.loads((asm_dir / "symbol_map.json").read_text(encoding="utf-8"))

        self.assertEqual(symbol_map, {"b2d087c9": "blas_thread_server"})

    def _run_find(self, so_name, fake_fs: dict[str, list[str]]) -> str | None:
        """Simulate the library search from the in-container script.

        fake_fs maps directory -> list of filenames found by os.walk.
        Returns matched path or None.
        """
        import os
        base = os.path.basename(so_name)
        stem = base.split(".so")[0]
        search_dirs = list(fake_fs.keys())
        for d in search_dirs:
            for fn in fake_fs[d]:
                if fn == base:
                    return os.path.join(d, fn)
                if ".so" in fn and stem in fn:
                    return os.path.join(d, fn)
        return None

    def test_exact_match_in_usr_lib(self):
        fake_fs = {
            "/usr/lib": ["libc.so.6", "libpython3.14.so.1.0"],
            "/root/.pyenv": [],
        }
        result = self._run_find("libpython3.14.so.1.0", fake_fs)
        self.assertEqual(result, "/usr/lib/libpython3.14.so.1.0")

    def test_substring_match_stem(self):
        """perf reports libpython3.14.so, container has libpython3.14.so.1.0."""
        fake_fs = {
            "/usr/lib": ["libpython3.14.so.1.0"],
            "/root/.pyenv": [],
        }
        result = self._run_find("libpython3.14.so", fake_fs)
        self.assertIsNotNone(result)
        self.assertIn("libpython3.14.so", result)

    def test_pyenv_lib_dir_searched_for_so(self):
        """libpython in /root/.pyenv/versions/.../lib/ must be found."""
        pyenv_dir = "/root/.pyenv/versions/3.14.3/lib"
        fake_fs = {
            "/usr/lib": [],
            pyenv_dir: ["libpython3.14.so", "libpython3.14.so.1.0"],
        }
        result = self._run_find("libpython3.14.so.1.0", fake_fs)
        self.assertIsNotNone(result)
        self.assertIn(pyenv_dir, result)

    def test_python_binary_in_pyenv(self):
        """Non-.so binary like python3.14 must be found in pyenv."""
        pyenv_bin = "/root/.pyenv/versions/3.14.3/bin"
        fake_fs = {
            "/usr/lib": [],
            "/usr/local/bin": [],
            pyenv_bin: ["python3.14", "python3"],
        }
        # python3.14 is not a .so, stem match still works
        result = self._run_find("python3.14", fake_fs)
        self.assertIsNotNone(result)

    def test_full_path_so_name(self):
        """perf may report full path like /usr/lib/.../libpython3.14.so.1.0."""
        fake_fs = {
            "/usr/lib/aarch64-linux-gnu": ["libpython3.14.so.1.0"],
        }
        result = self._run_find(
            "/usr/lib/aarch64-linux-gnu/libpython3.14.so.1.0", fake_fs
        )
        self.assertIsNotNone(result)

    def test_no_match_returns_none(self):
        fake_fs = {"/usr/lib": ["libc.so.6"]}
        result = self._run_find("libnonexistent.so", fake_fs)
        self.assertIsNone(result)

    def test_version_specific_match_ignores_wrong_version(self):
        """libpython3.14 must NOT match libpython3.12."""
        fake_fs = {
            "/usr/lib/aarch64-linux-gnu": ["libpython3.12.so.1.0"],
            "/root/.pyenv/versions/3.14.3/lib": ["libpython3.14.so.1.0"],
        }
        result = self._run_find("libpython3.14.so.1.0", fake_fs)
        self.assertIsNotNone(result)
        self.assertIn("3.14", result)
        self.assertNotIn("3.12", result)


class AwkPatternTest(unittest.TestCase):
    """Test awk pattern generation for objdump extraction."""

    def test_awk_pattern_exact_match(self):
        import re
        sym = "deduce_unreachable"
        pat = re.compile(r"<" + sym + r".*>:")
        self.assertTrue(pat.search("<deduce_unreachable>:"))
        self.assertTrue(pat.search("<deduce_unreachable.isra.0>:"))

    def test_awk_pattern_c_identifier(self):
        """C identifiers have no awk-special chars."""
        import re
        for sym in ["_Py_dict_lookup", "builtin_exec", "r_object", "deduce_unreachable"]:
            self.assertFalse(re.search(r'[\\.^$*+?{}()|]', sym))


class FilterPythonRowsTest(unittest.TestCase):
    """Test _filter_python_rows keeps both CPython and third-party library symbols."""

    def _make_row(self, symbol="func", shared_object="libpython3.14.so",
                  pid_command=None, category_top="") -> dict:
        row: dict = {
            "symbol": symbol,
            "shared_object": shared_object,
            "category_top": category_top,
        }
        if pid_command is not None:
            row["pid_command"] = pid_command
        return row

    def test_cpython_symbols_kept(self):
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [self._make_row("_Py_dict_lookup", "libpython3.14.so.1.0")]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 1)

    def test_third_party_so_kept(self):
        """libarrow, libopenblas etc. must NOT be filtered out."""
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [
            self._make_row("arrow_func", "libarrow.so.1300"),
            self._make_row("openblas_func", "libopenblas.so.0"),
            self._make_row("glibc_func", "libc.so.6"),
        ]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 3, "Third-party SO symbols must be kept")

    def test_kernel_symbols_dropped(self):
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [
            self._make_row("copy_page_range", "[kernel.kallsyms]"),
        ]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 0)

    def test_hex_addresses_dropped(self):
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [
            self._make_row("0x7f1234567890", "libpython3.14.so"),
            self._make_row("a1b2c3d4e5f6a7b8", "libpython3.14.so"),
            self._make_row("[unknown]", "libpython3.14.so"),
        ]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 0)

    def test_kernel_idle_dropped(self):
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [self._make_row("default_idle_call", "[kernel.kallsyms]")]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 0)

    def test_pid_filter_keeps_python(self):
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [
            self._make_row("func_a", "libpython3.14.so", pid_command="python3.14"),
            self._make_row("func_b", "libpython3.14.so", pid_command="java"),
            self._make_row("func_c", "libpython3.14.so", pid_command="pyflink-udf-runner"),
        ]
        result = _filter_python_rows(rows)
        symbols = [r["symbol"] for r in result]
        self.assertIn("func_a", symbols)
        self.assertNotIn("func_b", symbols)  # java process filtered
        self.assertIn("func_c", symbols)

    def test_mixed_cpython_and_third_party(self):
        """Both CPython and third-party must survive filtering."""
        from pipelines.pyframework_pipeline.backfill.perf_backfill import _filter_python_rows
        rows = [
            self._make_row("_Py_dict_lookup", "libpython3.14.so.1.0"),
            self._make_row("arrow_batch", "libarrow.so.1300"),
            self._make_row("_PyEval_EvalFrameDefault", "libpython3.14.so.1.0"),
            self._make_row("cblas_dgemm", "libopenblas.so.0"),
        ]
        result = _filter_python_rows(rows)
        self.assertEqual(len(result), 4)
        symbols = {r["symbol"] for r in result}
        self.assertEqual(symbols, {"_Py_dict_lookup", "arrow_batch",
                                    "_PyEval_EvalFrameDefault", "cblas_dgemm"})
