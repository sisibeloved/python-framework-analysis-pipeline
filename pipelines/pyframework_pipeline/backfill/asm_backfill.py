"""Backfill Source.artifactIndex and Dataset.functions[].diffView from asm files.

Globs .s files from arm/x86 run directories, generates artifact entries,
and builds initial diffView skeletons for each hotspot function.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Symbol -> {sourceFile, snippet} mapping for known CPython hotspot functions.
_SYMBOL_MAP_PATH = Path(__file__).parent / "data" / "symbol_source_map.json"
_symbol_source_map: dict[str, dict] | None = None


def _load_symbol_source_map() -> dict[str, dict]:
    """Load the CPython symbol -> source mapping (lazy, cached)."""
    global _symbol_source_map
    if _symbol_source_map is None:
        if _SYMBOL_MAP_PATH.exists():
            with open(_SYMBOL_MAP_PATH, encoding="utf-8") as f:
                _symbol_source_map = json.load(f)
        else:
            _symbol_source_map = {}
    return _symbol_source_map


# ---------------------------------------------------------------------------
# Platform directory name normalisation
# ---------------------------------------------------------------------------

# Known sub-directory names for each logical platform.  The acquisition step
# creates ``<run_dir>/asm/<platform>/`` where *platform* comes from the
# acquisition config, but we also tolerate the shorter "arm" / "x86" variants.
_ARM_DIRS = ("arm64", "arm")
_X86_DIRS = ("x86_64", "x86")

_PLATFORM_MAP: dict[str, str] = {}
for _d in _ARM_DIRS:
    _PLATFORM_MAP[_d] = "arm64"
for _d in _X86_DIRS:
    _PLATFORM_MAP[_d] = "x86_64"

# Canonical platform labels used in artifact IDs and JSON fields.
_CANONICAL_ARM = "arm64"
_CANONICAL_X86 = "x86_64"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _symbol_to_hash(symbol: str) -> str:
    """Return first 8 hex chars of MD5(symbol)."""
    return hashlib.md5(symbol.encode()).hexdigest()[:8]


def _artifact_id(platform: str, symbol: str) -> str:
    """Generate ``asm_<platform>_<hash>`` style artifact ID."""
    return f"asm_{platform}_{_symbol_to_hash(symbol)}"


def _artifact_path(platform: str, symbol: str) -> str:
    """Relative artifact path used in the artifactIndex entry."""
    return f"artifacts/asm/{_platform_dir(platform)}/{_symbol_to_hash(symbol)}.s"


def _platform_dir(platform: str) -> str:
    """Canonical sub-directory name for *platform*."""
    if platform == _CANONICAL_ARM:
        return "arm"
    return "x86"


def _empty_diff_view() -> dict:
    """Return an initial diffView skeleton (to be populated by Step 7)."""
    return {
        "sourceAnchors": [],
        "analysisBlocks": [],
        "armRegions": [],
        "x86Regions": [],
        "mappings": [],
        "diffSignals": [],
        "alignmentNote": "",
        "performanceNote": "",
    }


def _populate_diff_view(
    func: dict,
    symbol: str,
    arm_content: str | None,
    x86_content: str | None,
) -> None:
    """Populate diffView with raw ARM/x86 ASM content for side-by-side display.

    Creates a single analysisBlock with armRegions and x86Regions populated
    from the raw objdump output.  Source anchors are left empty (no C source
    available); the mapping links the ARM and x86 regions directly.
    """
    if not arm_content and not x86_content:
        return

    func_id = func.get("id", _symbol_to_hash(symbol))

    # Extract key opcodes for highlights (top 5 most frequent mnemonics)
    def _extract_highlights(asm_text: str, max_items: int = 5) -> list[str]:
        from collections import Counter
        mnemonics: Counter[str] = Counter()
        for line in asm_text.splitlines():
            # objdump format: "  addr:  bytes  mnemonic  operands"
            parts = line.split("\t")
            if len(parts) >= 3:
                mnemonic = parts[2].strip().split()[0] if parts[2].strip() else ""
                if mnemonic and not mnemonic.startswith("<") and not mnemonic.startswith("//"):
                    mnemonics[mnemonic] += 1
        return [m for m, _ in mnemonics.most_common(max_items)]

    # Build regions
    arm_region_id = f"arm_{func_id}"
    x86_region_id = f"x86_{func_id}"
    anchor_id = f"src_{func_id}"

    arm_region = None
    x86_region = None

    if arm_content:
        arm_region = {
            "id": arm_region_id,
            "label": f"Arm64 汇编 ({symbol})",
            "location": f"<arm64> {symbol}",
            "role": "assembly_arm64",
            "snippet": arm_content,
            "highlights": _extract_highlights(arm_content),
            "defaultExpanded": True,
        }

    if x86_content:
        x86_region = {
            "id": x86_region_id,
            "label": f"x86_64 汇编 ({symbol})",
            "location": f"<x86_64> {symbol}",
            "role": "assembly_x86_64",
            "snippet": x86_content,
            "highlights": _extract_highlights(x86_content),
            "defaultExpanded": True,
        }

    # Build mapping
    mapping = {
        "id": f"map_{func_id}",
        "label": "全函数对照",
        "sourceAnchorIds": [anchor_id],
        "armRegionIds": [arm_region_id] if arm_region else [],
        "x86RegionIds": [x86_region_id] if x86_region else [],
        "note": f"{symbol} 的 ARM/x86 反汇编对照（objdump -d 输出）",
    }

    # Build source anchor with real C source code if available
    source_map = _load_symbol_source_map()
    known_source = source_map.get(symbol, {})
    source_snippet = known_source.get("snippet", "")
    source_file = func.get("sourceFile", "") or known_source.get("sourceFile", "")

    source_anchor = {
        "id": anchor_id,
        "label": symbol,
        "location": source_file or f"<函数> {symbol}",
        "snippet": source_snippet,
        "defaultExpanded": True,
    }

    # Build analysis block
    has_real_source = bool(source_snippet)
    block = {
        "id": f"block_{func_id}",
        "label": f"{symbol} 全函数反汇编",
        "summary": f"{symbol} 在 ARM64 和 x86_64 上的 objdump 反汇编输出对照。",
        "mappingType": "full_function",
        "sourceAnchors": [source_anchor],
        "armRegions": [arm_region] if arm_region else [],
        "x86Regions": [x86_region] if x86_region else [],
        "mappings": [mapping],
        "diffSignals": [],
        "alignmentNote": "" if has_real_source else "无 C 源码对照，仅展示反汇编差异。",
        "performanceNote": "",
        "defaultExpanded": True,
    }

    diff_guide = ""
    if has_real_source:
        diff_guide = (f"下方展示 {source_file} 中 {symbol} 函数的 C 源码实现，"
                      "以及该函数在 ARM64 和 x86_64 架构上的反汇编输出对照。")
    else:
        diff_guide = ("下方展示该函数在 ARM64 和 x86_64 架构上的完整 objdump 反汇编输出。"
                      "由于是编译器生成的内置函数，暂无对应 C 源码锚点。")

    func["diffView"] = {
        "functionId": func_id,
        "sourceFile": source_file or func.get("sourceFile", "") or f"<{func.get('origin', 'unknown')}> {symbol}",
        "sourceLocation": source_file or f"<{func.get('origin', 'unknown')}> {symbol}",
        "diffGuide": diff_guide,
        "analysisBlocks": [block],
    }


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def _discover_asm_files(run_dir: Path, platform_dirs: tuple[str, ...]) -> dict[str, Path]:
    """Find .s files under ``<run_dir>/asm/<sub>/``.

    Returns ``{symbol_name: absolute_path}`` where *symbol_name* is the
    filename stem (without the ``.s`` extension).
    """
    result: dict[str, Path] = {}
    asm_root = run_dir / "asm"
    if not asm_root.is_dir():
        return result

    for sub in platform_dirs:
        sub_dir = asm_root / sub
        if not sub_dir.is_dir():
            continue
        for s_file in sorted(sub_dir.glob("*.s")):
            symbol = s_file.stem
            # First discovery wins — avoids duplicates when both arm64/ and
            # arm/ happen to exist with overlapping content.
            if symbol not in result:
                result[symbol] = s_file
    return result


# ---------------------------------------------------------------------------
# Artifact index helpers
# ---------------------------------------------------------------------------

def _existing_artifact_ids(source: dict) -> set[str]:
    """Return the set of IDs already present in ``source["artifactIndex"]``."""
    return {entry["id"] for entry in source.get("artifactIndex", [])}


def _build_artifact_entry(
    symbol: str,
    platform: str,
    rel_path: str,
    content: str = "",
) -> dict:
    """Build one artifactIndex entry for an assembly file."""
    platform_label = "Arm" if platform == _CANONICAL_ARM else "x86"
    entry: dict[str, Any] = {
        "id": _artifact_id(platform, symbol),
        "title": f"{symbol} 的 {platform_label} 汇编",
        "type": "assembly",
        "description": f"objdump -S -d 反汇编输出",
        "platform": platform,
        "contentType": "text/plain",
    }
    if content:
        entry["content"] = content
    else:
        entry["filePath"] = rel_path
    return entry


# ---------------------------------------------------------------------------
# Dataset.functions helpers
# ---------------------------------------------------------------------------

def _functions_by_symbol(dataset: dict) -> dict[str, dict]:
    """Index existing functions by symbol name."""
    result: dict[str, dict] = {}
    for func in dataset.get("functions", []):
        sym = func.get("symbol", "")
        if sym:
            result[sym] = func
    return result


def _ensure_diff_view(func: dict) -> None:
    """Add a diffView skeleton to *func* if it does not already have one."""
    if "diffView" not in func or not func["diffView"]:
        func["diffView"] = _empty_diff_view()


def _add_new_function(
    dataset: dict,
    symbol: str,
    arm_only: bool = False,
    x86_only: bool = False,
) -> dict:
    """Add a minimal function entry and return it.

    The entry contains only ``symbol`` and a ``diffView`` skeleton.  Metadata
    like ``component``, ``categoryL1``, ``caseIds`` will be filled by
    perf_backfill or later enrichment steps.
    """
    func: dict = {
        "symbol": symbol,
        "diffView": _empty_diff_view(),
    }
    if arm_only:
        func["platforms"] = ["arm64"]
    elif x86_only:
        func["platforms"] = ["x86_64"]
    dataset.setdefault("functions", []).append(func)
    return func


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def backfill_asm(
    arm_run_dir: Path,
    x86_run_dir: Path,
    source: dict,
    dataset: dict,
) -> dict:
    """Backfill Source.artifactIndex and initial Dataset.functions[].diffView.

    Parameters
    ----------
    arm_run_dir : Path
        Run output directory for the ARM platform.
    x86_run_dir : Path
        Run output directory for the x86 platform.
    source : dict
        The Source layer JSON (mutated in place).
    dataset : dict
        The Dataset layer JSON (mutated in place).

    Returns
    -------
    dict
        Summary with artifact and function counts.
    """
    # ------------------------------------------------------------------
    # 1. Discover .s files from both platforms
    # ------------------------------------------------------------------
    arm_files = _discover_asm_files(arm_run_dir, _ARM_DIRS)
    x86_files = _discover_asm_files(x86_run_dir, _X86_DIRS)

    # Union of all symbols across both platforms.
    all_symbols = sorted(set(arm_files.keys()) | set(x86_files.keys()))

    # ------------------------------------------------------------------
    # 2. Build artifact entries (dedup by ID)
    # ------------------------------------------------------------------
    existing_ids = _existing_artifact_ids(source)
    new_artifact_ids: list[str] = []
    artifacts_by_id = {a.get("id"): a for a in source.get("artifactIndex", [])}

    for symbol in all_symbols:
        has_arm = symbol in arm_files
        has_x86 = symbol in x86_files

        if has_arm:
            aid = _artifact_id(_CANONICAL_ARM, symbol)
            arm_content = arm_files[symbol].read_text(encoding="utf-8", errors="replace")
            if aid in existing_ids:
                # Update existing entry with content.
                art = artifacts_by_id.get(aid)
                if art is not None:
                    art["content"] = arm_content
                    art.pop("path", None)
            else:
                source.setdefault("artifactIndex", []).append(
                    _build_artifact_entry(symbol, _CANONICAL_ARM, "", content=arm_content)
                )
                existing_ids.add(aid)
                new_artifact_ids.append(aid)

        if has_x86:
            aid = _artifact_id(_CANONICAL_X86, symbol)
            x86_content = x86_files[symbol].read_text(encoding="utf-8", errors="replace")
            if aid in existing_ids:
                art = artifacts_by_id.get(aid)
                if art is not None:
                    art["content"] = x86_content
                    art.pop("path", None)
            else:
                source.setdefault("artifactIndex", []).append(
                    _build_artifact_entry(symbol, _CANONICAL_X86, "", content=x86_content)
                )
                existing_ids.add(aid)
                new_artifact_ids.append(aid)

    # ------------------------------------------------------------------
    # 3. Build / update Dataset.functions[] diffView skeletons
    # ------------------------------------------------------------------
    funcs_by_symbol = _functions_by_symbol(dataset)

    arm_only_count = 0
    x86_only_count = 0
    both_count = 0
    new_func_count = 0

    for symbol in all_symbols:
        has_arm = symbol in arm_files
        has_x86 = symbol in x86_files

        if has_arm and has_x86:
            both_count += 1
        elif has_arm:
            arm_only_count += 1
        else:
            x86_only_count += 1

        arm_content = arm_files[symbol].read_text(encoding="utf-8", errors="replace") if has_arm else None
        x86_content = x86_files[symbol].read_text(encoding="utf-8", errors="replace") if has_x86 else None

        if symbol in funcs_by_symbol:
            func = funcs_by_symbol[symbol]
            _ensure_diff_view(func)
            # Populate diffView with raw ASM for side-by-side display.
            if arm_content or x86_content:
                _populate_diff_view(func, symbol, arm_content, x86_content)
            # Update artifactIds to reference new artifacts with content.
            new_ids: list[str] = []
            if has_arm:
                new_ids.append(_artifact_id(_CANONICAL_ARM, symbol))
            if has_x86:
                new_ids.append(_artifact_id(_CANONICAL_X86, symbol))
            if new_ids:
                existing_ids_list = func.get("artifactIds", [])
                merged = list(dict.fromkeys(new_ids + existing_ids_list))
                func["artifactIds"] = merged
        else:
            # New function — add with minimal metadata.
            _add_new_function(
                dataset,
                symbol,
                arm_only=has_arm and not has_x86,
                x86_only=has_x86 and not has_arm,
            )
            new_func_count += 1

    # ------------------------------------------------------------------
    # 4. Annotate functions without usable ASM
    # ------------------------------------------------------------------
    no_asm_count = 0
    source_map = _load_symbol_source_map()
    for func in dataset.get("functions", []):
        dv = func.get("diffView")
        has_blocks = dv and dv.get("analysisBlocks")
        if has_blocks:
            continue
        origin = func.get("origin", func.get("sourceFile", ""))
        sym = func.get("symbol", "")
        source_file = func.get("sourceFile", "")
        known = source_map.get(sym, {})
        snippet = known.get("snippet", "")
        real_source = known.get("sourceFile", "")

        if origin == "kernel":
            note = "内核符号，无用户态反汇编可用。"
        elif origin and origin not in ("CPython", ""):
            note = f"来自 {origin} 的第三方库函数，当前未采集该库的反汇编。"
        else:
            note = "该符号为 static 内联函数或被编译器优化，未在共享库中导出。"

        sf = real_source or source_file
        loc = real_source or source_file or f"<{origin}> {sym}"

        if not dv:
            func["diffView"] = {
                "functionId": func.get("id", ""),
                "sourceFile": sf,
                "sourceLocation": loc,
                "diffGuide": note,
                "analysisBlocks": [],
            }
            dv = func["diffView"]
        else:
            dv["sourceFile"] = sf
            dv["sourceLocation"] = loc
            dv["diffGuide"] = note

        # If we have C source code but no ASM, add a source-only anchor block
        if snippet and not dv.get("analysisBlocks"):
            anchor_id = f"src_{func.get('id', '')}"
            dv["analysisBlocks"] = [{
                "id": f"block_{func.get('id', '')}",
                "label": f"{sym} C 源码",
                "summary": f"{real_source} 中 {sym} 函数的 C 实现（无反汇编对照）。",
                "mappingType": "source_only",
                "sourceAnchors": [{
                    "id": anchor_id,
                    "label": sym,
                    "location": real_source,
                    "snippet": snippet,
                    "defaultExpanded": True,
                }],
                "armRegions": [],
                "x86Regions": [],
                "mappings": [],
                "diffSignals": [],
                "alignmentNote": "",
                "performanceNote": "",
                "defaultExpanded": True,
            }]
            dv["diffGuide"] = f"下方展示 {real_source} 中 {sym} 函数的 C 源码实现。该符号为 static 内联函数或被编译器优化，暂无反汇编对照。"

        no_asm_count += 1

    # ------------------------------------------------------------------
    # 5. Return summary
    # ------------------------------------------------------------------
    total_arm = len(arm_files)
    total_x86 = len(x86_files)

    return {
        "status": "backfilled" if all_symbols else "skipped",
        "armFiles": total_arm,
        "x86Files": total_x86,
        "uniqueSymbols": len(all_symbols),
        "newArtifacts": len(new_artifact_ids),
        "newFunctions": new_func_count,
        "bothPlatforms": both_count,
        "armOnly": arm_only_count,
        "x86Only": x86_only_count,
    }
