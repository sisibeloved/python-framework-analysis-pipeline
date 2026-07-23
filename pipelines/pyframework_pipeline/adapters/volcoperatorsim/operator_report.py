"""Human-readable per-pipeline operator reports for Volc evidence."""

from __future__ import annotations

import csv
import html
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...contracts.operator import OperatorDataset, OperatorRecord
from .perf_symbol_bundle import IDENTITY_POLICY


_ENGINE_ORDER = {"daft_ray": 0, "datajuicer_native": 1}
_FORMAL_SCOPES = frozenset(
    {"pipeline_context", "operator_case_e2e", "operator_case_perf"}
)


def _records_have_complete_scopes(
    records: Iterable[OperatorRecord],
    required_scopes: frozenset[str] = _FORMAL_SCOPES,
) -> bool:
    scopes_by_case: dict[tuple[str, str], set[str]] = {}
    for record in records:
        scopes_by_case.setdefault(
            (record.engine_id, record.operator_case_id), set()
        ).add(record.measurement_scope)
    return bool(scopes_by_case) and all(
        required_scopes.issubset(scopes)
        for scopes in scopes_by_case.values()
    )


def _skipped_coverage_scopes(platform_dir: Path) -> frozenset[str]:
    path = platform_dir / "operators" / "operator-coverage.json"
    try:
        coverage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return frozenset()
    return frozenset(
        scope
        for scope, state in (coverage.get("scopes") or {}).items()
        if isinstance(state, Mapping) and state.get("status") == "skipped"
    )


def _pipeline_coverage_complete(platform_dir: Path, pipeline_id: str) -> bool:
    path = platform_dir / "operators" / "operator-coverage.json"
    try:
        coverage = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if coverage.get("status") == "complete":
        return True
    if coverage.get("status") != "partial":
        return False
    missing = [
        item
        for scope in (coverage.get("scopes") or {}).values()
        if isinstance(scope, Mapping)
        for item in scope.get("missing") or ()
        if isinstance(item, Mapping)
        and str(item.get("pipelineId") or "") == pipeline_id
    ]
    return not missing


def write_operator_reports(
    platform_dir: Path,
    *,
    records: Iterable[OperatorRecord],
    allowed_paths: set[Path],
) -> tuple[Path, ...]:
    """Write self-contained HTML and Markdown reports plus both indexes."""

    record_rows = _records_with_exact_top_symbols(
        platform_dir,
        tuple(records),
        limit=5,
    )
    records_by_pipeline: dict[str, list[OperatorRecord]] = {}
    for record in record_rows:
        records_by_pipeline.setdefault(record.pipeline_id, []).append(record)
    pipelines = sorted(records_by_pipeline)
    context_results = _load_context_results(
        platform_dir=platform_dir,
        records=record_rows,
        allowed_paths=allowed_paths,
    )
    required_scopes = _FORMAL_SCOPES - _skipped_coverage_scopes(platform_dir)
    measurement_note = _measurement_policy_note(platform_dir)
    reports_dir = platform_dir / "operators" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []
    context_by_pipeline: dict[str, dict[str, Mapping[str, Any]]] = {}
    for (pipeline_id, engine), result in context_results.items():
        context_by_pipeline.setdefault(pipeline_id, {})[engine] = result
    for pipeline_id in pipelines:
        pipeline_records = tuple(records_by_pipeline[pipeline_id])
        pipeline_context = context_by_pipeline.get(pipeline_id, {})
        coverage_complete = _pipeline_coverage_complete(
            platform_dir, pipeline_id
        )
        path = reports_dir / f"{pipeline_id}.md"
        path.write_text(
            build_operator_report_markdown(
                pipeline_id=pipeline_id,
                platform_id=(pipeline_records[0].platform_id if pipeline_records else ""),
                records=pipeline_records,
                context_results=pipeline_context,
                coverage_complete=coverage_complete,
                required_scopes=required_scopes,
            ),
            encoding="utf-8",
        )
        output_paths.append(path)
        html_path = reports_dir / f"{pipeline_id}.html"
        html_path.write_text(
            build_operator_report_html(
                pipeline_id=pipeline_id,
                platform_id=(pipeline_records[0].platform_id if pipeline_records else ""),
                records=pipeline_records,
                context_results=pipeline_context,
                coverage_complete=coverage_complete,
                required_scopes=required_scopes,
                measurement_note=measurement_note,
            ),
            encoding="utf-8",
        )
        output_paths.append(html_path)

    index_path = platform_dir / "operators" / "operator-report.md"
    platform_id = record_rows[0].platform_id if record_rows else ""
    index_lines = [
        f"# {platform_id.upper()} 逐算子报告索引",
        "",
        "以下报告同时展示 pipeline 内算子耗时、E2E 占比、隔离运行耗时和 perf CPU 分布。",
        "",
    ]
    index_lines.extend(
        f"- [{pipeline_id}](reports/{pipeline_id}.md)"
        for pipeline_id in pipelines
    )
    index_path.write_text(
        "\n".join(index_lines) + "\n",
        encoding="utf-8",
    )
    index_html_path = platform_dir / "operators" / "operator-report.html"
    index_html_path.write_text(
        _build_operator_index_html(
            platform_id=platform_id,
            pipelines=pipelines,
        ),
        encoding="utf-8",
    )
    return (index_html_path, index_path, *output_paths)


def _measurement_policy_note(platform_dir: Path) -> str:
    marker = (
        platform_dir
        / "operators/raw/operator_case_e2e/SKIPPED.json"
    )
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    policy = payload.get("measurementPolicy")
    if policy == "single_pass_context_perf":
        return (
            "E2E 与重算子 Perf 来自同一次冻结 E2E；Perf 按 runner 的精确算子边界"
            "切分。仅对样本不足的快速算子使用同一冻结输入派生文本做短补采，"
            "未重复执行 OCR，也未执行 isolated wall-time 轮次。"
        )
    if policy == "bounded_representative_profile":
        return (
            "E2E 与算子 wall time 使用完整冻结 pipeline 的 per_op 边界；"
            "Perf 使用冻结代表性输入或同一冻结任务的稳态采样窗口。"
            "未执行重复的 isolated wall-time 轮次。"
        )
    return ""


def render_operator_reports(platform_dir: Path) -> Path:
    """Regenerate readable reports from normalized records and raw context.

    This is intentionally independent from normalization so operators can
    iterate on presentation without rerunning remote acquisition or perf.
    """

    records_path = platform_dir / "operators" / "operator-records.jsonl"
    if not records_path.is_file():
        raise ValueError(
            f"normalized operator records are required before reporting: {records_path}"
        )
    manifest_path = platform_dir / "operators" / "acquisition-manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"acquisition manifest is required: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid acquisition manifest: {manifest_path}") from exc
    if manifest.get("status") != "complete":
        raise ValueError("acquisition manifest is not complete")
    allowed_paths = {
        (platform_dir / str(item.get("path") or "")).resolve()
        for item in manifest.get("artifacts") or []
        if item.get("path")
    }
    records = OperatorDataset.read_jsonl(records_path).records
    if not records:
        raise ValueError(f"normalized operator records are empty: {records_path}")
    write_operator_reports(
        platform_dir,
        records=records,
        allowed_paths=allowed_paths,
    )
    return platform_dir / "operators" / "operator-report.html"


def _build_operator_index_html(
    *, platform_id: str, pipelines: Iterable[str]
) -> str:
    cards = "".join(
        f'<a class="pipeline" href="reports/{_html(pipeline_id)}.html">'
        f'<strong>{_html(pipeline_id)}</strong><span>打开逐算子报告 →</span></a>'
        for pipeline_id in pipelines
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html(platform_id.upper())} 逐算子报告索引</title>
<style>
  body {{ margin:0; background:#f4f7fb; color:#172033;
    font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ width:min(980px,calc(100% - 28px)); margin:36px auto; }}
  h1 {{ margin-bottom:6px; }} p {{ color:#637083; }}
  .reports {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; margin-top:24px; }}
  .pipeline {{ display:flex; min-height:112px; flex-direction:column; justify-content:space-between;
    padding:18px; color:#172033; text-decoration:none; background:#fff; border:1px solid #dfe5ec;
    border-radius:13px; box-shadow:0 10px 28px rgba(28,45,74,.08); }}
  .pipeline:hover {{ border-color:#1769e0; transform:translateY(-1px); }}
  .pipeline span {{ color:#1769e0; }}
</style></head><body><main>
<h1>{_html(platform_id.upper())} 逐算子报告</h1>
<p>每份报告展示算子 wall time、E2E 占比、隔离运行统计和算子内 perf CPU 分布。</p>
<div class="reports">{cards or '<p>没有可展示的 pipeline。</p>'}</div>
</main></body></html>
"""


def build_operator_report_markdown(
    *,
    pipeline_id: str,
    platform_id: str,
    records: Iterable[OperatorRecord],
    context_results: Mapping[str, Mapping[str, Any]],
    coverage_complete: bool = True,
    required_scopes: frozenset[str] = _FORMAL_SCOPES,
) -> str:
    """Render a readable timing and sampled-CPU report for one pipeline."""

    rows = tuple(records)
    engines = sorted(
        {record.engine_id for record in rows},
        key=lambda value: (_ENGINE_ORDER.get(value, 99), value),
    )
    formal_allowed = (
        coverage_complete
        and _records_have_complete_scopes(rows, required_scopes)
        and all(record.quality.formal_conclusion_allowed for record in rows)
    )
    lines = [
        f"# {_escape(pipeline_id)} · {platform_id.upper()} 逐算子报告",
        "",
        f"> 数据用途：**{'正式结论' if formal_allowed else '诊断分析'}**。",
        "> Pipeline 内耗时来自完整 pipeline 的算子边界；隔离耗时来自单算子多轮运行；",
        "> Perf 分布是采样 CPU period 占比，括号内 CPU 时间由隔离运行 CPU 总时间按该占比估算，并非额外的 wall time。",
        "",
        "## 口径",
        "",
        "- **Pipeline E2E**：目标 runner 的完整 `metrics.elapsed_s`。",
        "- **E2E 占比**：pipeline 内该算子耗时 / Pipeline E2E。各算子之和以外的是初始化、输入读取和收尾等开销。",
        "- **隔离 Runner 中位数**：单算子进程从 runner 入口到结束的中位数，包含初始化与输入读取。",
        "- **隔离算子中位数**：runner 内部归因给算子执行段的中位数。",
        "- **Perf CPU 分布**：算子隔离 perf 采样的分类与热点符号，不应与 E2E wall time直接相加。",
        "",
    ]

    for engine in engines:
        result = context_results.get(engine) or {}
        metrics = result.get("metrics") or {}
        e2e_s = _optional_float(metrics.get("elapsed_s"))
        engine_rows = tuple(record for record in rows if record.engine_id == engine)
        context_by_order = {
            record.order: record
            for record in engine_rows
            if record.measurement_scope == "pipeline_context"
        }
        isolated_by_order = {
            record.order: record
            for record in engine_rows
            if record.measurement_scope == "operator_case_e2e"
        }
        perf_by_order = {
            record.order: record
            for record in engine_rows
            if record.measurement_scope == "operator_case_perf"
        }
        orders = sorted(
            set(context_by_order) | set(isolated_by_order) | set(perf_by_order)
        )
        attributed_values = [
            record.timing.pipeline_context_ns
            for record in context_by_order.values()
            if record.timing.pipeline_context_ns is not None
        ]
        context_total_ns = sum(attributed_values)
        context_total = (
            _format_ns(context_total_ns) if attributed_values else "不可归因"
        )
        context_share = (
            _format_share_ns(context_total_ns, e2e_s)
            if attributed_values
            else "—"
        )

        lines.extend(
            [
                f"## {_escape(engine)}",
                "",
                "### Pipeline E2E 构成",
                "",
                f"- 输入/输出行数：`{_display_int(metrics.get('input_rows'))}` → "
                f"`{_display_int(metrics.get('output_rows'))}`",
                f"- Pipeline E2E：**{_format_seconds(e2e_s)}**",
                f"- 算子链合计：**{context_total}** （{context_share}）",
                f"- 非算子/未归因：**{_format_residual(e2e_s, context_total_ns)}** "
                f"（{_format_residual_share(e2e_s, context_total_ns)}）",
                "",
            ]
        )
        breakdown = (metrics.get("timing_breakdown") or {}).get("buckets") or {}
        included_buckets = [
            (str(name), _optional_float(value))
            for name, value in breakdown.items()
            if not str(name).endswith("_measured_s")
        ]
        if included_buckets:
            lines.extend(
                [
                    "| E2E 阶段 | 时长 | E2E 占比 |",
                    "|---|---:|---:|",
                ]
            )
            for name, seconds in included_buckets:
                lines.append(
                    f"| `{_escape(name)}` | {_format_seconds(seconds)} | "
                    f"{_format_share_seconds(seconds, e2e_s)} |"
                )
            lines.append("")

        lines.extend(
            [
                "### 逐算子运行时长",
                "",
                "| # | 算子 | Pipeline 内耗时 | E2E 占比 | 隔离 Runner 中位数 | 隔离算子中位数 | Runner P95 | 轮数 |",
                "|---:|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for order in orders:
            context = context_by_order.get(order)
            isolated = isolated_by_order.get(order)
            reference = context or isolated or perf_by_order[order]
            context_ns = context.timing.pipeline_context_ns if context else None
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(order),
                        f"`{_escape(reference.operator_id)}`",
                        _format_ns(context_ns),
                        _format_share_ns(context_ns, e2e_s),
                        _format_ns(
                            isolated.timing.runner_elapsed_ns if isolated else None
                        ),
                        _format_ns(
                            isolated.timing.isolated_operator_ns
                            if isolated
                            else None
                        ),
                        _format_ns(isolated.timing.p95_ns if isolated else None),
                        str(isolated.timing.rounds if isolated else 0),
                    ]
                )
                + " |"
            )
        lines.append("")

        lines.extend(
            [
                "### Perf CPU 分布",
                "",
                "| # | 算子 | CPU 总时间 | 样本数 | 映射解析 | 分类分布（period 占比 / 估算 CPU 时间） | Top 热点符号 |",
                "|---:|---|---:|---:|---|---|---|",
            ]
        )
        for order in orders:
            perf = perf_by_order.get(order)
            isolated = isolated_by_order.get(order)
            reference = perf or isolated or context_by_order[order]
            cpu_ns = (
                isolated.resources.tree_cpu_time_ns
                if isolated is not None
                else None
            )
            categories = (
                perf.metadata.get("categoryPeriodShare") if perf is not None else {}
            ) or {}
            top_symbols = (
                perf.metadata.get("topSymbols") if perf is not None else []
            ) or []
            resolution = (
                perf.metadata.get("symbolResolution") if perf is not None else {}
            ) or {}
            if _is_legacy_symbol_resolution(resolution):
                categories = {}
                top_symbols = []
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(order),
                        f"`{_escape(reference.operator_id)}`",
                        _format_ns(cpu_ns),
                        str(perf.resources.sample_count if perf else 0),
                        _format_symbol_resolution(
                            resolution
                        ),
                        _format_categories(categories, cpu_ns),
                        _format_top_symbols(top_symbols),
                    ]
                )
                + " |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_operator_report_html(
    *,
    pipeline_id: str,
    platform_id: str,
    records: Iterable[OperatorRecord],
    context_results: Mapping[str, Mapping[str, Any]],
    coverage_complete: bool = True,
    required_scopes: frozenset[str] = _FORMAL_SCOPES,
    measurement_note: str = "",
) -> str:
    """Render a self-contained visual report for one pipeline.

    Wall-clock percentages and perf period percentages deliberately live in
    separate sections: the former describe contribution to pipeline E2E,
    while the latter describe sampled CPU-time distribution inside an
    isolated operator run.
    """

    rows = tuple(records)
    engines = sorted(
        {record.engine_id for record in rows},
        key=lambda value: (_ENGINE_ORDER.get(value, 99), value),
    )
    formal_allowed = (
        coverage_complete
        and _records_have_complete_scopes(rows, required_scopes)
        and all(record.quality.formal_conclusion_allowed for record in rows)
    )
    engine_sections = "".join(
        _build_engine_html(
            engine=engine,
            records=tuple(record for record in rows if record.engine_id == engine),
            result=context_results.get(engine) or {},
        )
        for engine in engines
    )
    purpose = "正式结论" if formal_allowed else "诊断分析"
    purpose_class = "formal" if formal_allowed else "diagnostic"
    perf_scope_label = "算子内" if measurement_note else "隔离算子"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(pipeline_id)} · {_html(platform_id.upper())} 逐算子报告</title>
  <style>
    :root {{ color-scheme: light; --ink:#172033; --muted:#637083; --line:#dfe5ec;
      --panel:#fff; --canvas:#f4f7fb; --accent:#1769e0; --accent2:#00a37a;
      --warn:#b66a00; --shadow:0 10px 28px rgba(28,45,74,.08); }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--canvas); color:var(--ink);
      font:14px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(1480px,calc(100% - 32px)); margin:28px auto 64px; }}
    h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:-.02em; }}
    h2 {{ margin:34px 0 14px; font-size:23px; }}
    h3 {{ margin:24px 0 12px; font-size:17px; }}
    h4 {{ margin:16px 0 7px; font-size:13px; color:#455166; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:.92em; }}
    .muted {{ color:var(--muted); }}
    .badge {{ display:inline-block; padding:3px 9px; border-radius:999px; font-weight:700; }}
    .badge.formal {{ background:#dff7ee; color:#08765a; }}
    .badge.diagnostic {{ background:#fff0d7; color:#945600; }}
    .notice {{ margin:18px 0; padding:13px 16px; border-left:4px solid var(--accent);
      background:#edf4ff; border-radius:8px; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
      padding:16px; box-shadow:var(--shadow); }}
    .metric {{ font-size:24px; font-weight:750; margin-top:4px; }}
    .timeline {{ display:flex; min-height:34px; overflow:hidden; border-radius:8px;
      background:#e6ebf2; margin:14px 0 8px; }}
    .segment {{ min-width:2px; display:flex; align-items:center; justify-content:center;
      color:#fff; font-size:11px; font-weight:700; overflow:hidden; white-space:nowrap; }}
    .segment.residual {{ background:#8290a3; }}
    .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:12px;
      background:var(--panel); box-shadow:var(--shadow); }}
    table {{ width:100%; border-collapse:collapse; min-width:920px; }}
    th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); text-align:right;
      vertical-align:top; }}
    th {{ position:sticky; top:0; background:#f7f9fc; color:#455166; font-size:12px; }}
    th:nth-child(2),td:nth-child(2) {{ text-align:left; }}
    tr:last-child td {{ border-bottom:0; }}
    .share {{ font-weight:750; color:var(--accent); }}
    .unattributed {{ color:var(--warn); font-weight:700; }}
    .perf-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:14px; }}
    .perf-card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
      padding:15px; box-shadow:var(--shadow); min-width:0; }}
    .perf-title {{ display:flex; justify-content:space-between; gap:12px; margin-bottom:12px; }}
    .dist-row {{ display:grid; grid-template-columns:minmax(130px,1fr) 2fr auto;
      align-items:center; gap:9px; margin:8px 0; }}
    .bar {{ height:9px; background:#e6ebf2; border-radius:999px; overflow:hidden; }}
    .bar > span {{ display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); }}
    .symbols {{ margin:12px 0 0; padding-left:18px; color:#455166; }}
    .inference-note {{ margin:0 0 8px; color:var(--muted); font-size:12px; }}
    .libraries {{ margin:0; padding:0; list-style:none; color:#455166; }}
    .libraries li {{ display:grid; grid-template-columns:minmax(0,1fr) auto;
      grid-template-areas:"name value" "language value" "bar bar"; column-gap:10px; row-gap:2px;
      padding:8px 0; border-bottom:1px dashed var(--line); align-items:start; }}
    .libraries li:last-child {{ border-bottom:0; }}
    .library-name {{ grid-area:name; min-width:0; line-height:1.35; overflow-wrap:anywhere; }}
    .library-language {{ grid-area:language; min-width:0; color:var(--muted); font-size:12px;
      overflow-wrap:anywhere; }}
    .library-bar {{ grid-area:bar; width:100%; margin-top:4px; }}
    .library-share {{ grid-area:value; white-space:nowrap; text-align:right; font-weight:700; }}
    .stage-table {{ min-width:560px; }}
    @media (max-width:700px) {{ main {{ width:min(100% - 18px,1480px); margin-top:16px; }}
      h1 {{ font-size:23px; }} .perf-grid {{ grid-template-columns:1fr; }}
      .libraries li {{ grid-template-columns:1fr; grid-template-areas:"name" "language" "value" "bar";
        gap:2px; }}
      .library-share {{ text-align:left; }} }}
  </style>
</head>
<body><main>
  <header>
    <h1>{_html(pipeline_id)} · {_html(platform_id.upper())} 逐算子报告</h1>
    <span class="badge {purpose_class}">{purpose}</span>
    <p class="muted">直接回答三个问题：每个算子运行多久、占端到端多少、算子内部 CPU 时间花在哪里。</p>
  </header>
  <div class="notice"><strong>口径：</strong>运行时长和 E2E 占比来自完整 pipeline 的
    <code>per_op</code> 边界；Perf 是{perf_scope_label} CPU period 分布，估算 CPU 时间不能与 wall time 相加。</div>
  {f'<div class="notice"><strong>有界冻结采集：</strong>{_html(measurement_note)}</div>' if measurement_note else ''}
  {engine_sections or '<p class="unattributed">没有可展示的逐算子记录。</p>'}
</main></body></html>
"""


def _build_engine_html(
    *,
    engine: str,
    records: tuple[OperatorRecord, ...],
    result: Mapping[str, Any],
) -> str:
    metrics = result.get("metrics") or {}
    e2e_s = _optional_float(metrics.get("elapsed_s"))
    context_by_order = {
        record.order: record
        for record in records
        if record.measurement_scope == "pipeline_context"
    }
    isolated_by_order = {
        record.order: record
        for record in records
        if record.measurement_scope == "operator_case_e2e"
    }
    perf_by_order = {
        record.order: record
        for record in records
        if record.measurement_scope == "operator_case_perf"
    }
    orders = sorted(set(context_by_order) | set(isolated_by_order) | set(perf_by_order))
    attributed = [
        record.timing.pipeline_context_ns
        for record in context_by_order.values()
        if record.timing.pipeline_context_ns is not None
    ]
    attributed_ns = sum(attributed)
    residual_s = (
        max(e2e_s - attributed_ns / 1_000_000_000, 0.0)
        if e2e_s is not None
        else None
    )
    attribution_share = _share(attributed_ns / 1_000_000_000, e2e_s)
    residual_share = _share(residual_s, e2e_s)
    missing_attribution = any(
        context_by_order.get(order) is None
        or context_by_order[order].timing.pipeline_context_ns is None
        for order in orders
    )
    warning = (
        '<p class="unattributed">部分算子不可归因：需要 per_op 算子边界后才能计算其 E2E 占比。</p>'
        if missing_attribution
        else ""
    )
    runtime_rows = "".join(
        _runtime_row_html(
            order=order,
            context=context_by_order.get(order),
            isolated=isolated_by_order.get(order),
            perf=perf_by_order.get(order),
            e2e_s=e2e_s,
        )
        for order in orders
    )
    perf_cards = "".join(
        _perf_card_html(
            order=order,
            context=context_by_order.get(order),
            isolated=isolated_by_order.get(order),
            perf=perf_by_order.get(order),
        )
        for order in orders
    )
    timeline = _timeline_html(context_by_order, e2e_s)
    breakdown = (metrics.get("timing_breakdown") or {}).get("buckets") or {}
    stage_rows = "".join(
        f"<tr><td><code>{_html(name)}</code></td><td>{_html(_format_seconds(_optional_float(value)))}</td>"
        f"<td>{_html(_format_share_seconds(_optional_float(value), e2e_s))}</td></tr>"
        for name, value in breakdown.items()
        if not str(name).endswith("_measured_s")
    )
    stage_table = (
        '<h3>E2E 阶段</h3><div class="table-wrap"><table class="stage-table">'
        '<thead><tr><th>阶段</th><th>时长</th><th>E2E 占比</th></tr></thead>'
        f'<tbody>{stage_rows}</tbody></table></div>'
        if stage_rows
        else ""
    )
    return f"""
  <section>
    <h2>{_html(engine)}</h2>
    <div class="grid">
      {_metric_card("Pipeline E2E", _format_seconds(e2e_s))}
      {_metric_card("输入 → 输出", f"{_display_int(metrics.get('input_rows'))} → {_display_int(metrics.get('output_rows'))}")}
      {_metric_card("算子链合计", _format_ns(attributed_ns) if attributed else "不可归因", _format_pct(attribution_share))}
      {_metric_card("非算子/未归因", _format_seconds(residual_s), _format_pct(residual_share))}
    </div>
    <h3>逐算子运行时长与 E2E 占比</h3>
    {timeline}
    {warning}
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>算子</th><th>Pipeline 内耗时</th><th>E2E 占比</th>
        <th>隔离 Runner 中位数</th><th>隔离算子中位数</th><th>Runner P95</th><th>轮数</th></tr></thead>
      <tbody>{runtime_rows}</tbody>
    </table></div>
    {stage_table}
    <h3>算子内 Perf CPU 耗时分布</h3>
    <p class="muted">百分比为该算子 perf period 占比；时间为隔离进程 CPU 总时间 × period 占比。</p>
    <div class="perf-grid">{perf_cards}</div>
  </section>
"""


def _runtime_row_html(
    *,
    order: int,
    context: OperatorRecord | None,
    isolated: OperatorRecord | None,
    perf: OperatorRecord | None,
    e2e_s: float | None,
) -> str:
    reference = context or isolated or perf
    if reference is None:
        return ""
    context_ns = context.timing.pipeline_context_ns if context else None
    runtime = _format_ns(context_ns) if context_ns is not None else "不可归因"
    share = (
        _format_share_ns(context_ns, e2e_s)
        if context_ns is not None
        else "不可归因"
    )
    runtime_class = "" if context_ns is not None else ' class="unattributed"'
    share_class = ' class="share"' if context_ns is not None else ' class="share unattributed"'
    return (
        f"<tr><td>{order}</td><td><code>{_html(reference.operator_id)}</code></td>"
        f"<td{runtime_class}>{_html(runtime)}</td><td{share_class}>{_html(share)}</td>"
        f"<td>{_html(_format_ns(isolated.timing.runner_elapsed_ns if isolated else None))}</td>"
        f"<td>{_html(_format_ns(isolated.timing.isolated_operator_ns if isolated else None))}</td>"
        f"<td>{_html(_format_ns(isolated.timing.p95_ns if isolated else None))}</td>"
        f"<td>{isolated.timing.rounds if isolated else 0}</td></tr>"
    )


def _perf_card_html(
    *,
    order: int,
    context: OperatorRecord | None,
    isolated: OperatorRecord | None,
    perf: OperatorRecord | None,
) -> str:
    reference = perf or isolated or context
    if reference is None:
        return ""
    cpu_ns = isolated.resources.tree_cpu_time_ns if isolated else None
    resolution = (perf.metadata.get("symbolResolution") if perf else {}) or {}
    resolution_status = str(resolution.get("status") or "")
    legacy_resolution = _is_legacy_symbol_resolution(resolution)
    categories = (perf.metadata.get("categoryPeriodShare") if perf else {}) or {}
    languages = (perf.metadata.get("languagePeriodShare") if perf else {}) or {}
    if legacy_resolution:
        categories = {}
        languages = {}
    category_rows = _distribution_rows_html(categories, cpu_ns)
    language_rows = _distribution_rows_html(languages, cpu_ns)
    libraries = (perf.metadata.get("topLibraries") if perf else []) or []
    if legacy_resolution:
        libraries = []
    library_rows = "".join(
        _library_row_html(item, cpu_ns) for item in libraries
    )
    symbols = _top_symbols(perf.metadata.get("topSymbols") if perf else [])
    if legacy_resolution:
        symbols = []
    symbol_rows = "".join(
        f"<li><code>{_html(symbol)}</code>{f' · {share * 100:.2f}%' if share is not None else ''}</li>"
        for symbol, share in symbols
    )
    resolution_html = ""
    if resolution_status:
        before = int(resolution.get("deletedRowsBefore") or 0)
        unresolved = int(resolution.get("unresolvedDeletedRows") or 0)
        badge_class = (
            "formal"
            if resolution_status == "complete" and not legacy_resolution
            else "diagnostic"
        )
        if legacy_resolution:
            label = "旧版映射结果不可信 · 已隐藏库/语言归因"
        elif resolution_status == "complete":
            label = f"映射解析完成 · deleted {before}→{unresolved}"
        else:
            label = f"映射解析不完整 · 残留 {unresolved}"
        resolution_html = (
            f'<p><span class="badge {badge_class}">{_html(label)}</span></p>'
        )
    return (
        '<article class="perf-card">'
        f'<div class="perf-title"><strong>#{order} <code>{_html(reference.operator_id)}</code></strong>'
        f'<span class="muted">CPU {_html(_format_ns(cpu_ns))} · samples {perf.resources.sample_count if perf else 0}</span></div>'
        f"{resolution_html}"
        '<h4>框架 / 类型分类</h4>'
        f'{"".join(category_rows) or "<p class=\"unattributed\">无可用 perf 分类样本。</p>"}'
        '<h4>语言 / 运行时分布（推断）</h4>'
        '<p class="inference-note">根据共享对象和符号保守推断；未知映射不会强行归类。</p>'
        f'{"".join(language_rows) or "<p class=\"unattributed\">无可用语言归因。</p>"}'
        '<h4>库 / 映射分布</h4>'
        f'{f"<ol class=\"libraries\">{library_rows}</ol>" if library_rows else "<p class=\"unattributed\">无可用库归因。</p>"}'
        '<h4>Top 热点符号</h4>'
        f'{f"<ol class=\"symbols\">{symbol_rows}</ol>" if symbol_rows else ""}'
        '</article>'
    )


def _distribution_rows_html(
    distribution: Mapping[str, Any], cpu_ns: int | None
) -> list[str]:
    rows: list[str] = []
    for name, raw_share in sorted(
        distribution.items(),
        key=lambda item: _optional_float(item[1]) or 0.0,
        reverse=True,
    ):
        share = _optional_float(raw_share)
        if share is None:
            continue
        width = min(max(share * 100.0, 0.0), 100.0)
        estimate = _format_ns(round(cpu_ns * share)) if cpu_ns is not None else "—"
        rows.append(
            f'<div class="dist-row"><span>{_html(name)}</span>'
            f'<span class="bar"><span style="width:{width:.4f}%"></span></span>'
            f'<strong>{share * 100:.2f}% · {_html(estimate)}</strong></div>'
        )
    return rows


def _library_row_html(item: Mapping[str, Any], cpu_ns: int | None) -> str:
    share = _optional_float(item.get("periodShare"))
    width = min(max((share or 0.0) * 100.0, 0.0), 100.0)
    estimate = (
        _format_ns(round(cpu_ns * share))
        if cpu_ns is not None and share is not None
        else "—"
    )
    suffix = f"{share * 100:.2f}% · {estimate}" if share is not None else estimate
    return (
        f'<li><strong class="library-name">{_html(item.get("library") or "未知库")}</strong>'
        f'<span class="library-language">{_html(item.get("language") or "语言未解析")}</span>'
        f'<span class="library-bar bar"><span style="width:{width:.4f}%"></span></span>'
        f'<span class="library-share">{_html(suffix)}</span></li>'
    )


def _timeline_html(
    context_by_order: Mapping[int, OperatorRecord], total_s: float | None
) -> str:
    if total_s is None or total_s <= 0:
        return '<p class="unattributed">缺少 Pipeline E2E，无法绘制占比。</p>'
    palette = ("#1769e0", "#00a37a", "#7a55c7", "#dc6b37", "#b44a7d", "#397d9a")
    segments = []
    attributed_s = 0.0
    for index, order in enumerate(sorted(context_by_order)):
        record = context_by_order[order]
        value = record.timing.pipeline_context_ns
        if value is None:
            continue
        seconds = max(value / 1_000_000_000, 0.0)
        attributed_s += seconds
        width = min(seconds / total_s * 100.0, 100.0)
        segments.append(
            f'<span class="segment" style="width:{width:.4f}%;background:{palette[index % len(palette)]}" '
            f'title="#{order} {_html(record.operator_id)} · {width:.2f}%">{width:.1f}%</span>'
        )
    residual = max(total_s - attributed_s, 0.0)
    if residual:
        width = residual / total_s * 100.0
        segments.append(
            f'<span class="segment residual" style="width:{width:.4f}%" title="非算子/未归因 · {width:.2f}%">{width:.1f}%</span>'
        )
    return f'<div class="timeline" aria-label="Pipeline E2E 占比">{"".join(segments)}</div>'


def _metric_card(label: str, value: str, detail: str = "") -> str:
    suffix = f'<div class="muted">{_html(detail)}</div>' if detail else ""
    return f'<div class="card"><div class="muted">{_html(label)}</div><div class="metric">{_html(value)}</div>{suffix}</div>'


def _top_symbols(items: Iterable[Mapping[str, Any]]) -> list[tuple[str, float | None]]:
    aggregated: dict[str, float | None] = {}
    for item in items:
        symbol = str(item.get("symbol") or "unknown")
        share = _optional_float(item.get("periodShare"))
        previous = aggregated.get(symbol)
        if share is not None:
            aggregated[symbol] = (previous or 0.0) + share
        elif symbol not in aggregated:
            aggregated[symbol] = None
    return sorted(
        aggregated.items(),
        key=lambda item: (item[1] is not None, item[1] or 0.0),
        reverse=True,
    )[:5]


def _records_with_exact_top_symbols(
    platform_dir: Path,
    records: tuple[OperatorRecord, ...],
    *,
    limit: int,
) -> tuple[OperatorRecord, ...]:
    """Replace report previews with exact symbol aggregates from the full CSV.

    ``OperatorRecord.metadata.topSymbols`` is intentionally a compact transport
    preview.  The full normalized CSV retains every PID/symbol row, so reports
    must aggregate that table before ranking; otherwise a hot symbol split over
    workers can be understated or omitted by the preview cutoff.
    """

    perf_path = (
        platform_dir / "operators" / "perf" / "operator-perf-records.csv"
    )
    if limit <= 0 or not perf_path.is_file():
        return records

    periods_by_key: dict[tuple[str, str, str], dict[str, int]] = {}
    objects_by_key: dict[
        tuple[str, str, str], dict[str, dict[str, int]]
    ] = {}
    totals_by_key: dict[tuple[str, str, str], int] = {}
    libraries_by_key: dict[tuple[str, str, str], dict[str, int]] = {}
    library_languages_by_key: dict[
        tuple[str, str, str], dict[str, dict[str, int]]
    ] = {}
    languages_by_key: dict[tuple[str, str, str], dict[str, int]] = {}
    try:
        with perf_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                key = (
                    str(row.get("benchmark") or ""),
                    str(row.get("engine_id") or ""),
                    str(row.get("operator_case_id") or ""),
                )
                if not all(key):
                    continue
                period = int(row.get("period") or 0)
                if period <= 0:
                    continue
                symbol = str(row.get("symbol") or "unknown")
                shared_object = str(row.get("shared_object") or "")
                category = str(row.get("category_top") or "")
                language = _infer_execution_language(
                    shared_object=shared_object,
                    symbol=symbol,
                    category=category,
                )
                symbol_periods = periods_by_key.setdefault(key, {})
                symbol_periods[symbol] = symbol_periods.get(symbol, 0) + period
                symbol_objects = objects_by_key.setdefault(key, {}).setdefault(
                    symbol, {}
                )
                symbol_objects[shared_object] = (
                    symbol_objects.get(shared_object, 0) + period
                )
                totals_by_key[key] = totals_by_key.get(key, 0) + period
                library_periods = libraries_by_key.setdefault(key, {})
                library_periods[shared_object] = (
                    library_periods.get(shared_object, 0) + period
                )
                library_languages = library_languages_by_key.setdefault(
                    key, {}
                ).setdefault(shared_object, {})
                library_languages[language] = (
                    library_languages.get(language, 0) + period
                )
                language_periods = languages_by_key.setdefault(key, {})
                language_periods[language] = (
                    language_periods.get(language, 0) + period
                )
    except (OSError, csv.Error, ValueError) as exc:
        raise ValueError(f"invalid full perf symbol table: {perf_path}") from exc

    exact_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    libraries_exact_by_key: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = {}
    languages_exact_by_key: dict[tuple[str, str, str], dict[str, float]] = {}
    for key, symbol_periods in periods_by_key.items():
        total = totals_by_key[key]
        ranked = sorted(
            symbol_periods.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
        exact_by_key[key] = [
            {
                "symbol": symbol,
                "sharedObject": max(
                    objects_by_key[key][symbol].items(),
                    key=lambda item: (item[1], item[0]),
                )[0],
                "period": period,
                "periodShare": period / total,
            }
            for symbol, period in ranked
        ]
        ranked_libraries = sorted(
            libraries_by_key.get(key, {}).items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
        libraries_exact_by_key[key] = [
            {
                "library": _display_library(shared_object),
                "sharedObject": shared_object,
                "language": max(
                    library_languages_by_key[key][shared_object].items(),
                    key=lambda item: (item[1], item[0]),
                )[0],
                "period": period,
                "periodShare": period / total,
            }
            for shared_object, period in ranked_libraries
        ]
        languages_exact_by_key[key] = {
            language: period / total
            for language, period in sorted(
                languages_by_key.get(key, {}).items(),
                key=lambda item: (-item[1], item[0]),
            )
        }

    enriched: list[OperatorRecord] = []
    for record in records:
        key = (record.pipeline_id, record.engine_id, record.operator_case_id)
        exact = exact_by_key.get(key)
        if record.measurement_scope != "operator_case_perf" or exact is None:
            enriched.append(record)
            continue
        enriched.append(
            replace(
                record,
                metadata={
                    **record.metadata,
                    "topSymbols": exact,
                    "topSymbolsSource": "operator-perf-records.csv",
                    "topSymbolsAggregation": "sum_period_by_symbol",
                    "topLibraries": libraries_exact_by_key.get(key, []),
                    "languagePeriodShare": languages_exact_by_key.get(key, {}),
                    "languageAttribution": "heuristic_from_shared_object_symbol_and_category",
                },
            )
        )
    return tuple(enriched)


def _infer_execution_language(
    *, shared_object: str, symbol: str, category: str
) -> str:
    """Conservatively infer the executing language/runtime for one perf row."""

    obj = shared_object.lower()
    sym = symbol.lower()
    cat = category.lower()
    if not obj or obj in {"(deleted)", "unknown", "[unknown]"}:
        return "原生代码（语言未解析）"
    if obj.startswith("[jit]"):
        return "JIT 原生代码（语言未解析）"
    if "kernel.kallsyms" in obj or obj in {"[kernel]", "[vdso]"}:
        return "Linux 内核 / 系统代码"
    if "python" in obj or "cpython runtime" in cat:
        return "Python / CPython（C 实现）"
    if any(name in obj for name in ("tokenizers", "daft.abi3", "rpds.")):
        return "Rust"
    if any(
        name in obj
        for name in (
            "opencv", "libstdc++", "libc++", "onnxruntime", "libtorch",
            "torch_cpu", "protobuf",
        )
    ) or sym.startswith("cv::"):
        return "C++"
    if any(name in obj for name in ("multiarray_umath", "numpy")):
        return "C / NumPy 原生代码"
    if any(
        name in obj
        for name in (
            "libc.so", "libm.so", "libssl", "libcrypto", "libgomp",
            "libpthread", "ld-linux",
        )
    ):
        return "C / 系统运行库"
    if ".cpython-" in obj or obj.endswith(".abi3.so"):
        return "Python 原生扩展（语言未解析）"
    if "daft/ray" in cat:
        return "框架原生代码（语言未解析）"
    if obj.endswith(".so") or ".so." in obj:
        return "原生代码（语言未解析）"
    return "语言 / 运行时未解析"


def _display_library(shared_object: str) -> str:
    """Return a stable, human-readable library or mapping label."""

    raw = shared_object.strip()
    lower = raw.lower()
    basename = Path(raw).name or raw
    if not raw or lower in {"unknown", "[unknown]"}:
        return "未知映射"
    if lower == "(deleted)":
        return "已删除映射（库名不可恢复）"
    if lower.startswith("[jit]"):
        return "JIT 生成代码"
    if "kernel.kallsyms" in lower or lower == "[kernel]":
        return "Linux kernel"
    if lower == "[vdso]":
        return "Linux vDSO"
    if "python3.10" in lower or "libpython3.10" in lower:
        return "CPython 3.10"
    if "opencv" in lower:
        return f"OpenCV ({basename})"
    if "tokenizers" in lower:
        return f"Hugging Face tokenizers ({basename})"
    if "daft.abi3" in lower:
        return f"Daft ({basename})"
    if "multiarray_umath" in lower:
        return f"NumPy ({basename})"
    return basename


def _share(value: float | None, total: float | None) -> float | None:
    if value is None or total is None or total <= 0:
        return None
    return value / total


def _format_pct(value: float | None) -> str:
    return f"{value * 100:.2f}%" if value is not None else "—"


def _html(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _load_context_results(
    *,
    platform_dir: Path,
    records: tuple[OperatorRecord, ...],
    allowed_paths: set[Path],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    task_to_pipeline = {
        record.task_spec_id: record.pipeline_id for record in records
    }
    root = (platform_dir / "operators" / "raw" / "pipeline_context").resolve()
    selected: dict[tuple[str, str], tuple[str, Mapping[str, Any]]] = {}
    for path in sorted(allowed_paths):
        if path.suffix != ".json":
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_id = str(payload.get("task_id") or "")
        engine = str(payload.get("engine_id") or "")
        metrics = payload.get("metrics") or {}
        pipeline_id = task_to_pipeline.get(task_id)
        if pipeline_id is None and engine:
            # Early captures did not always persist task_id.  The capture case
            # directory is still unambiguous because it includes the complete
            # pipeline id and engine id.
            path_parts = set(path.parts)
            candidates = {
                record.pipeline_id
                for record in records
                if record.engine_id == engine
                and f"pipeline_context__{record.pipeline_id}__{engine}" in path_parts
            }
            if len(candidates) == 1:
                pipeline_id = candidates.pop()
        if (
            pipeline_id is None
            or not engine
            or not isinstance(metrics.get("operator_timings"), list)
        ):
            continue
        key = (pipeline_id, engine)
        candidate = (str(path), payload)
        if key not in selected or candidate[0] > selected[key][0]:
            selected[key] = candidate
    return {key: value[1] for key, value in selected.items()}


def _format_categories(categories: Mapping[str, Any], cpu_ns: int | None) -> str:
    values: list[tuple[str, float]] = []
    for name, raw_share in categories.items():
        share = _optional_float(raw_share)
        if share is not None:
            values.append((str(name), share))
    values.sort(key=lambda item: item[1], reverse=True)
    if not values:
        return "—"
    rendered = []
    for name, share in values:
        estimated = _format_ns(round(cpu_ns * share)) if cpu_ns is not None else "—"
        rendered.append(
            f"{_escape(name)} {share * 100:.2f}% / {estimated}"
        )
    return "<br>".join(rendered)


def _format_top_symbols(items: Iterable[Mapping[str, Any]]) -> str:
    rendered: list[str] = []
    for symbol, share in _top_symbols(items):
        suffix = f" {share * 100:.2f}%" if share is not None else ""
        rendered.append(f"`{_escape(symbol)}`{suffix}")
    return "<br>".join(rendered) if rendered else "—"


def _format_symbol_resolution(value: Any) -> str:
    if not isinstance(value, Mapping) or not value.get("status"):
        return "旧数据：未验证"
    if _is_legacy_symbol_resolution(value):
        return "旧版结果：不可信"
    before = int(value.get("deletedRowsBefore") or 0)
    unresolved = int(value.get("unresolvedDeletedRows") or 0)
    if value.get("status") == "complete":
        return f"完成（deleted {before}→{unresolved}）"
    return f"不完整（残留 {unresolved}）"


def _is_legacy_symbol_resolution(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value.get("status"))
        and value.get("identityPolicy") != IDENTITY_POLICY
    )


def _format_ns(value: int | None) -> str:
    if value is None:
        return "—"
    seconds = value / 1_000_000_000
    if seconds >= 1:
        return f"{seconds:.3f} s"
    milliseconds = value / 1_000_000
    if milliseconds >= 1:
        return f"{milliseconds:.3f} ms"
    microseconds = value / 1_000
    return f"{microseconds:.3f} µs"


def _format_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return _format_ns(round(value * 1_000_000_000))


def _format_share_ns(value: int | None, total_s: float | None) -> str:
    if value is None or total_s is None or total_s <= 0:
        return "—"
    return f"{value / 1_000_000_000 / total_s * 100:.2f}%"


def _format_share_seconds(value: float | None, total_s: float | None) -> str:
    if value is None or total_s is None or total_s <= 0:
        return "—"
    return f"{value / total_s * 100:.2f}%"


def _format_residual(total_s: float | None, attributed_ns: int) -> str:
    if total_s is None:
        return "—"
    return _format_seconds(max(total_s - attributed_ns / 1_000_000_000, 0.0))


def _format_residual_share(total_s: float | None, attributed_ns: int) -> str:
    if total_s is None or total_s <= 0:
        return "—"
    residual = max(total_s - attributed_ns / 1_000_000_000, 0.0)
    return f"{residual / total_s * 100:.2f}%"


def _display_int(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "unknown"


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
