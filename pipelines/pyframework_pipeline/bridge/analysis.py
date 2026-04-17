"""Step 7 publish/fetch orchestration.

publish: create analysis issues for all hotspot functions.
fetch: pull LLM comments, parse structured results, backfill into Dataset.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .comment_parser import ParsedAnalysis, find_analysis_comment
from .issue_client import IssueClient, create_client
from .issue_template import build_asm_diff_issue, check_chunking
from .manifest import BridgeIssueEntry, BridgeManifest, load_bridge_manifest

logger = logging.getLogger(__name__)

_LABEL_ASM_DIFF = "asm-diff"
_LABEL_COLOR = "1d76db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _find_dataset(root: Path) -> Path | None:
    ds_dir = root / "datasets"
    if ds_dir.is_dir():
        files = list(ds_dir.glob("*.dataset.json"))
        if files:
            return files[0]
    return None


def _find_source(root: Path) -> Path | None:
    src_dir = root / "sources"
    if src_dir.is_dir():
        files = list(src_dir.glob("*.source.json"))
        if files:
            return files[0]
    return None


def _read_asm_content(source_data: dict[str, Any], artifact_id: str) -> str | None:
    """Read assembly text from a source artifact's file path or inline content."""
    for art in source_data.get("artifactIndex", []):
        if art.get("id") == artifact_id:
            # Prefer file path, fall back to inline content.
            file_path = art.get("filePath")
            if file_path:
                p = Path(file_path)
                if p.exists():
                    return p.read_text(encoding="utf-8", errors="replace")
            return art.get("content")
    return None


def _read_source_snippet(source_data: dict[str, Any], function: dict) -> str | None:
    """Read source code snippet for a function from sourceAnchors."""
    for anchor in source_data.get("sourceAnchors", []):
        # Match by function symbol reference or source location overlap.
        if anchor.get("functionId") == function.get("id"):
            return anchor.get("snippet", "")
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def publish(
    project_path: Path,
    repo: str,
    platform: str,
    token: str,
    *,
    dry_run: bool = False,
    max_lines: int = 2000,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Create analysis issues for all hotspot functions.

    Parameters
    ----------
    project_path:
        Path to ``project.yaml``.
    repo:
        ``"owner/repo"`` on the target platform.
    platform:
        ``"github"`` or ``"gitcode"``.
    token:
        API personal-access token.
    dry_run:
        If *True*, build issue bodies but do not create issues.
    max_lines:
        Maximum assembly lines per issue before truncation.
    base_url:
        Override default API base URL.

    Returns
    -------
    dict with summary stats.
    """
    from ..config import resolve_four_layer_root

    root = resolve_four_layer_root(project_path)
    dataset_path = _find_dataset(root)
    source_path = _find_source(root)

    if dataset_path is None:
        raise FileNotFoundError(f"No dataset JSON found under {root}")

    dataset = _load_json(dataset_path)
    source_data = _load_json(source_path) if source_path else {}

    owner, repo_name = repo.split("/", 1)

    # Load or create manifest.
    manifest_path = root / "bridge-manifest.json"
    manifest = load_bridge_manifest(manifest_path)
    manifest.project_id = dataset.get("id", "")

    # Resolve framework name for prompt.
    framework_id = dataset.get("frameworkId", "")
    framework_name = _resolve_framework_display(framework_id)

    # Ensure label exists (skip in dry-run).
    client: IssueClient | None = None
    if not dry_run:
        client = create_client(platform, token, base_url=base_url)
        try:
            client.ensure_label(owner, repo_name, _LABEL_ASM_DIFF, _LABEL_COLOR)
        except Exception:
            logger.warning("Failed to create label (may already exist)")

    # Track functions already published.
    existing = {
        e.function_id for e in manifest.issues if e.status != "failed"
    }

    functions = dataset.get("functions", [])
    published: list[dict[str, Any]] = []
    skipped = 0
    errors = 0

    for func in functions:
        func_id = func.get("id", "")
        symbol = func.get("symbol", "<unknown>")

        if func_id in existing:
            logger.info("Skipping %s (already published)", symbol)
            skipped += 1
            continue

        # Collect ARM and x86 assembly.
        arm_asm = None
        x86_asm = None
        for aid in func.get("artifactIds", []):
            content = _read_asm_content(source_data, aid)
            if content is None:
                continue
            if "_arm_" in aid:
                arm_asm = content
            elif "_x86_" in aid:
                x86_asm = content

        if arm_asm is None and x86_asm is None:
            logger.warning("No asm for %s, skipping", symbol)
            skipped += 1
            continue

        source_code = _read_source_snippet(source_data, func)

        try:
            issue = build_asm_diff_issue(
                function=func,
                arm_asm=arm_asm,
                x86_asm=x86_asm,
                source_code=source_code,
                framework_name=framework_name,
                max_lines=max_lines,
            )
        except ValueError as exc:
            logger.error("Template error for %s: %s", symbol, exc)
            errors += 1
            continue

        if dry_run:
            chunk_info = check_chunking(issue["body"])
            published.append({
                "symbol": symbol,
                "title": issue["title"],
                "body_length": len(issue["body"]),
                "needs_chunking": chunk_info.get("needs_chunking", False),
                "dry_run": True,
            })
            continue

        assert client is not None
        try:
            result = client.create_issue(
                owner=owner,
                repo=repo_name,
                title=issue["title"],
                body=issue["body"],
                labels=[_LABEL_ASM_DIFF],
            )
        except Exception as exc:
            logger.error("Failed to create issue for %s: %s", symbol, exc)
            errors += 1
            continue

        entry = BridgeIssueEntry(
            issue_type="asm-diff",
            function_id=func_id,
            platform=platform,
            repo=repo,
            issue_number=result.get("number", 0),
            issue_url=result.get("html_url", ""),
            status="created",
            created_at=_now_iso(),
        )
        manifest.issues.append(entry)
        published.append({
            "symbol": symbol,
            "issue_number": entry.issue_number,
            "issue_url": entry.issue_url,
        })
        logger.info("Published %s → #%s", symbol, entry.issue_number)

    # Save manifest.
    manifest.write(manifest_path)

    return {
        "total_functions": len(functions),
        "published": len(published),
        "skipped": skipped,
        "errors": errors,
        "issues": published,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(
    project_path: Path,
    repo: str,
    platform: str,
    token: str,
    *,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Fetch LLM analysis comments and backfill into Dataset.

    Parameters
    ----------
    project_path:
        Path to ``project.yaml``.
    repo:
        ``"owner/repo"`` on the target platform.
    platform:
        ``"github"`` or ``"gitcode"``.
    token:
        API personal-access token.
    base_url:
        Override default API base URL.

    Returns
    -------
    dict with summary stats.
    """
    from ..config import resolve_four_layer_root

    root = resolve_four_layer_root(project_path)
    dataset_path = _find_dataset(root)

    if dataset_path is None:
        raise FileNotFoundError(f"No dataset JSON found under {root}")

    manifest_path = root / "bridge-manifest.json"
    manifest = load_bridge_manifest(manifest_path)

    if not manifest.issues:
        return {"status": "no_issues", "fetched": 0, "parsed": 0, "failed": 0}

    owner, repo_name = repo.split("/", 1)
    client = create_client(platform, token, base_url=base_url)

    dataset = _load_json(dataset_path)
    func_map = {f["id"]: f for f in dataset.get("functions", []) if "id" in f}

    fetched = 0
    parsed = 0
    failed = 0
    patterns_new: list[dict[str, Any]] = []
    root_causes_new: list[dict[str, Any]] = []

    for entry in manifest.issues:
        if entry.status not in ("created", "analysed"):
            continue

        try:
            comments = client.get_issue_comments(
                owner, repo_name, entry.issue_number,
            )
        except Exception as exc:
            logger.error(
                "Failed to fetch comments for #%s: %s",
                entry.issue_number, exc,
            )
            continue

        fetched += 1
        entry.status = "analysed"

        parsed_result = find_analysis_comment(comments)
        if parsed_result is None:
            logger.info("No analysis comment on #%s yet", entry.issue_number)
            continue

        func = func_map.get(entry.function_id)
        if func is None:
            logger.warning(
                "Function %s not found in dataset", entry.function_id,
            )
            entry.status = "failed"
            failed += 1
            continue

        # Backfill diffView from parsed sections.
        _backfill_diff_view(func, parsed_result)

        # Extract root causes.
        for idx, rc in enumerate(parsed_result.root_causes):
            rc_id = f"rc_{entry.function_id}_{idx}"
            rc_entry = {
                "id": rc_id,
                "title": rc.get("劣势来源", rc.get("根因", "")),
                "category": rc.get("根因类别", ""),
                "location": rc.get("出现位置", ""),
                "impact": rc.get("热路径影响", ""),
                "functionId": entry.function_id,
            }
            root_causes_new.append(rc_entry)

        # Extract optimization opportunities from root causes table.
        for idx, opt in enumerate(parsed_result.optimizations):
            opt_entry = {
                "id": f"opt_{entry.function_id}_{idx}",
                "title": opt.get("优化点", ""),
                "strategy": opt.get("策略", ""),
                "beneficiary": opt.get("受益方", ""),
                "implementer": opt.get("实施方", ""),
                "functionId": entry.function_id,
            }
            patterns_new.append(opt_entry)

        entry.status = "parsed"
        entry.parsed_at = _now_iso()
        parsed += 1

        if parsed_result.warnings:
            logger.warning(
                "Parse warnings for #%s (%s): %s",
                entry.issue_number,
                parsed_result.symbol,
                "; ".join(parsed_result.warnings),
            )

    # Merge new patterns and root causes into dataset.
    _merge_list(dataset, "patterns", patterns_new, "id")
    _merge_list(dataset, "rootCauses", root_causes_new, "id")

    # Write back.
    _write_json(dataset_path, dataset)
    manifest.write(manifest_path)

    return {
        "fetched": fetched,
        "parsed": parsed,
        "failed": failed,
        "patterns_added": len(patterns_new),
        "root_causes_added": len(root_causes_new),
    }


# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------

def _backfill_diff_view(func: dict, parsed: ParsedAnalysis) -> None:
    """Populate func["diffView"] from a parsed analysis comment."""
    blocks: list[dict[str, Any]] = []
    for idx, sec in enumerate(parsed.sections):
        block: dict[str, Any] = {
            "id": f"blk_{idx:03d}",
            "label": sec.get("title", f"Section {idx + 1}"),
            "summary": "",
            "sourceAnchors": [],
            "armRegions": [],
            "x86Regions": [],
            "mappings": [],
            "diffSignals": [],
            "defaultExpanded": idx < 3,
        }

        # If there's a comparison table, extract diff signals.
        table = sec.get("table", [])
        if table:
            signals: list[str] = []
            for row in table:
                diff = row.get("差异", row.get("ARM劣势", ""))
                if diff and diff != "无差异":
                    signals.append(diff[:80])
            block["diffSignals"] = signals[:5]
            if signals:
                block["summary"] = signals[0][:120]

        blocks.append(block)

    func["diffView"] = {
        "functionId": func.get("id", ""),
        "analysisBlocks": blocks,
        "parsedFrom": "bridge-comment",
        "parsedAt": _now_iso(),
    }


def _merge_list(
    dataset: dict[str, Any],
    key: str,
    new_items: list[dict[str, Any]],
    id_key: str,
) -> None:
    """Merge *new_items* into ``dataset[key]`` by *id_key*, avoiding duplicates."""
    existing = dataset.setdefault(key, [])
    existing_ids = {item.get(id_key, "") for item in existing}
    for item in new_items:
        if item.get(id_key, "") not in existing_ids:
            existing.append(item)
            existing_ids.add(item[id_key])


def _resolve_framework_display(framework_id: str) -> str:
    """Map internal framework ID to display name for issue prompt."""
    _map: dict[str, str] = {
        "pyflink": "PyFlink",
        "pyspark": "PySpark",
        "cpython": "CPython 3.14",
    }
    return _map.get(framework_id, framework_id or "Python Framework")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status(project_path: Path) -> dict[str, Any]:
    """Report current bridge status for a project."""
    from ..config import resolve_four_layer_root

    root = resolve_four_layer_root(project_path)
    manifest_path = root / "bridge-manifest.json"
    manifest = load_bridge_manifest(manifest_path)

    counts: dict[str, int] = {}
    for entry in manifest.issues:
        counts[entry.status] = counts.get(entry.status, 0) + 1

    return {
        "project_id": manifest.project_id,
        "total_issues": len(manifest.issues),
        "by_status": counts,
        "manifest_path": str(manifest_path),
    }
