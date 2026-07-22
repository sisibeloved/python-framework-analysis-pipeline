"""Sub-step 5c: Machine code / assembly collection.

Uses perf annotate for instruction-level profiling and objdump for
full binary disassembly.  Discovers shared libraries from perf_records.csv
so that symbols from ALL libraries (not just libpython) are collected.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from .manifest import AcquisitionManifest, AcquisitionSection

# The annotate entry module lives in the analyze subpackage; invoked as a
# module so it resolves regardless of where the caller runs from.
_ANNOTATE_MODULE = "pyframework_pipeline.analyze.annotate_perf_hotspots"

# Directories to search for shared libraries on the local system.
_LIB_SEARCH_DIRS = [
    Path("/usr/lib"),
    Path("/usr/local/lib"),
    Path("/opt"),
]


def _parse_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_asm_candidate(so: str, sym: str) -> bool:
    so = so.strip()
    sym = sym.strip()
    if not so or not sym:
        return False
    if so == "(deleted)" or (so.startswith("[") and so.endswith("]")):
        return False
    if sym.startswith("0x"):
        return False
    if len(sym) >= 8 and all(c in "0123456789abcdef" for c in sym.lower()):
        return False
    return True


def _discover_from_symbol_hotspots(
    hotspots_csv: Path,
    *,
    max_symbols_per_so: int,
) -> dict[str, list[str]]:
    if not hotspots_csv.exists():
        return {}
    so_to_syms: dict[str, list[str]] = {}
    try:
        with open(hotspots_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                so = (row.get("shared_object") or "").strip()
                if not _is_asm_candidate(so, sym):
                    continue
                if _parse_float(row.get("self_share")) <= 0:
                    continue
                syms = so_to_syms.setdefault(so, [])
                if sym not in syms and len(syms) < max_symbols_per_so:
                    syms.append(sym)
    except Exception:
        return {}
    return so_to_syms


def _discover_libs_from_perf(records_csv: Path, max_symbols_per_so: int = 30) -> dict[str, list[str]]:
    """Return {shared_object: [top_symbols]} for real objdump candidates.

    Prefer aggregated symbol_hotspots.csv when available.  Raw perf rows are
    often split by thread/command, which can hide real symbols behind many
    low-self records and overrepresent pseudo DSOs such as ``(deleted)``.
    """
    hotspots_csv = records_csv.parent.parent / "tables" / "symbol_hotspots.csv"
    hotspots = _discover_from_symbol_hotspots(
        hotspots_csv,
        max_symbols_per_so=max_symbols_per_so,
    )
    if hotspots:
        return hotspots

    if not records_csv.exists():
        return {}
    so_to_syms: dict[str, list[str]] = {}
    try:
        with open(records_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip()
                so = (row.get("shared_object") or "").strip()
                if not _is_asm_candidate(so, sym):
                    continue
                so_to_syms.setdefault(so, []).append(sym)
    except Exception:
        return {}

    result: dict[str, list[str]] = {}
    for so, syms in so_to_syms.items():
        counts = Counter(syms)
        result[so] = [s for s, _ in counts.most_common(max_symbols_per_so)]
    return result


def _find_local_lib(so_name: str) -> Path | None:
    """Find a shared library on the local system by name."""
    for search_dir in _LIB_SEARCH_DIRS:
        try:
            matches = sorted(search_dir.rglob(so_name))
            if matches:
                return matches[0]
        except (OSError, PermissionError):
            continue
    return None


def _extract_symbol(objdump_output: str, symbol: str, max_lines: int = 500) -> str:
    """Extract one symbol's disassembly from full objdump output."""
    pattern = re.compile(rf"^[0-9a-f]+ <{re.escape(symbol)}>")
    lines: list[str] = []
    capturing = False
    for line in objdump_output.splitlines():
        if not capturing and pattern.match(line):
            capturing = True
        if capturing:
            if line.strip() == "" and lines:
                break
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def collect_asm(
    run_dir: Path,
    platform: str,
    perf_data: Path | None = None,
    kits_dir: Path | None = None,  # deprecated; analyze subpackage is the source now
    binaries: list[Path] | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """Collect machine code annotations and binary dumps.

    Parameters
    ----------
    run_dir : Path
        The run output directory.
    platform : str
        Platform identifier.
    perf_data : Path | None
        Path to perf.data file.
    kits_dir : Path | None
        Deprecated. Kept for backwards-compatibility; the analyze subpackage
        (``pyframework_pipeline.analyze``) is the single source of the annotate
        script and is invoked as a module.
    binaries : list[Path] | None
        Binary files to objdump (e.g. libpython3.14.so).
    top_n : int
        Number of top hotspots to annotate.

    Returns
    -------
    dict with asm file paths and metadata.
    """
    if perf_data is None:
        perf_data = run_dir / "perf.data"

    asm_dir = run_dir / "asm" / platform
    objdump_dir = run_dir / "asm" / "objdump"
    asm_dir.mkdir(parents=True, exist_ok=True)
    objdump_dir.mkdir(parents=True, exist_ok=True)

    hotspot_files = []
    objdump_files = []

    # Use the analyze annotate script (invoked as a module) for hotspots
    if perf_data.exists():
        # Read hotspot symbols from perf output if available
        records_csv = run_dir / "perf" / "data" / "perf_records.csv"
        if records_csv.exists():
            import sys as _sys
            cmd = [
                _sys.executable,
                "-m",
                _ANNOTATE_MODULE,
                str(records_csv),
                "--perf-data", str(perf_data),
                "--output", str(asm_dir.parent),
                "--top-n", str(top_n),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=False)

            # Collect generated .s files
            for f in sorted(asm_dir.glob("*.s")):
                hotspot_files.append(str(f.relative_to(run_dir)))
            for f in sorted((asm_dir.parent / "tables").glob("instruction_hotspots.csv")):
                hotspot_files.append(str(f.relative_to(run_dir)))

    # objdump for specified binaries
    if binaries:
        for binary in binaries:
            if binary.exists():
                out_name = binary.name + ".dump"
                out_path = objdump_dir / out_name
                result = subprocess.run(
                    ["objdump", "-d", "-C", str(binary)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0:
                    out_path.write_text(result.stdout, encoding="utf-8")
                    objdump_files.append(str(out_path.relative_to(run_dir)))

    # Discover hotspot symbols from perf CSV and collect objdump from all shared libs.
    records_csv = run_dir / "perf" / "data" / "perf_records.csv"
    lib_syms = _discover_libs_from_perf(records_csv)
    collected_syms = 0

    for so_name, syms in lib_syms.items():
        lib_path = _find_local_lib(so_name)
        if not lib_path:
            continue
        try:
            result = subprocess.run(
                ["objdump", "-d", "-C", str(lib_path)],
                capture_output=True, text=True, check=False, timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0 or not result.stdout:
            continue

        # Load existing symbol map for hash→symbol resolution.
        sym_map_path = asm_dir / "symbol_map.json"
        try:
            symbol_map = json.loads(sym_map_path.read_text(encoding="utf-8")) if sym_map_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            symbol_map = {}

        for sym in syms:
            sym_hash = hashlib.md5(sym.encode()).hexdigest()[:8]
            out_file = asm_dir / f"{sym_hash}.s"
            if out_file.exists() and out_file.stat().st_size > 0:
                continue
            content = _extract_symbol(result.stdout, sym)
            if content.strip():
                out_file.write_text(content, encoding="utf-8")
                symbol_map[sym_hash] = sym
                hotspot_files.append(str(out_file.relative_to(run_dir)))
                collected_syms += 1

        # Persist updated symbol map.
        if symbol_map:
            sym_map_path.write_text(
                json.dumps(symbol_map, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    return {
        "status": "collected" if (hotspot_files or objdump_files) else "skipped",
        "hotspotCount": len(hotspot_files),
        "hotspotFiles": hotspot_files,
        "objdumpFiles": objdump_files,
        "symbolsCollectedFromAllLibs": collected_syms,
    }
