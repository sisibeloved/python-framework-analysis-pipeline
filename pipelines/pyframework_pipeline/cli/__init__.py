"""CLI entry point for the pyframework-pipeline tool.

This module is a thin shell: ``build_parser()`` declares the subcommand
structure and ``main()`` dispatches to the per-group handler modules under
``cli/`` (config / environment / run / workload / benchmark / collect /
acquire / backfill / bridge / compare). Spec §6: each subcommand group lives
in its own module; this file owns only the parser + dispatcher.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Re-export the shared helpers under their original (underscore-prefixed) names
# so external importers (e.g. tests) keep working after the subcommand split.
from ._common import run_requires_bridge_token as _run_requires_bridge_token  # noqa: F401


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyframework-pipeline",
        description="Python 框架自动化分析流程工具。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # config
    config_parser = subparsers.add_parser(
        "config",
        help="[Step 1] 配置获取和完整性校验。",
    )
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_validate = config_sub.add_parser(
        "validate",
        help="[S1] 校验 project.yaml / environment.yaml / 运行配置。",
    )
    config_validate.add_argument("project", help="project.yaml 路径")
    config_validate.add_argument(
        "--skip-bridge-token",
        action="store_true",
        help="只做桥接前流程时跳过 token 校验。",
    )

    # validate
    subparsers.add_parser(
        "validate",
        help="校验四层输入目录或 project.yaml。",
    ).add_argument("path", help="四层输入目录或 project.yaml 路径")

    # run (one-click)
    run_p = subparsers.add_parser(
        "run",
        help="[S3→S7] 一键全流程（可断点续跑）。",
    )
    run_p.add_argument("project", help="project.yaml 路径")
    run_p.add_argument("--run-dir", help="运行输出目录")
    run_p.add_argument("--resume-from", help="从指定步骤恢复（如 5b.2b）")
    run_p.add_argument("--stop-before", help="在指定步骤前停止（如 5b.3）")
    run_p.add_argument(
        "--platform",
        action="append",
        help="仅运行指定平台；可重复传入，且必须已在 run.platforms 中配置。",
    )
    run_p.add_argument("--force", action="store_true", help="清空状态重新跑")
    run_p.add_argument("--yes", action="store_true", help="跳过确认提示")

    # environment [Step 3]
    env_parser = subparsers.add_parser(
        "environment",
        help="[Step 3] 环境搭建。",
    )
    env_sub = env_parser.add_subparsers(dest="env_command", required=True)

    # environment plan
    plan_parser = env_sub.add_parser(
        "plan",
        help="[S3] 生成环境搭建计划（plan-only，不执行）。",
    )
    plan_parser.add_argument("project", help="project.yaml 路径")
    plan_parser.add_argument("--platform", required=True, help="目标平台 ID")
    plan_parser.add_argument("--output", help="输出目录")

    # environment deploy
    deploy_parser = env_sub.add_parser(
        "deploy",
        help="[S3] SSH 执行环境部署计划。",
    )
    deploy_parser.add_argument("project", help="project.yaml 路径")
    deploy_parser.add_argument("--platform", required=True, help="目标平台 ID")
    deploy_parser.add_argument("--plan", help="已有的 environment-plan.json 路径")
    deploy_parser.add_argument("--yes", action="store_true", help="跳过确认提示")
    deploy_parser.add_argument("--output", help="输出目录（与 plan 共用）")

    # environment teardown
    teardown_parser = env_sub.add_parser(
        "teardown",
        help="[S3] 销毁远程集群。",
    )
    teardown_parser.add_argument("project", help="project.yaml 路径")
    teardown_parser.add_argument("--platform", required=True, help="目标平台 ID")
    teardown_parser.add_argument("--yes", action="store_true", help="跳过确认提示")

    # environment validate
    env_validate_parser = env_sub.add_parser(
        "validate",
        help="[S3] 校验环境记录和 readiness 报告。",
    )
    env_validate_parser.add_argument("run_dir", help="运行目录")

    # environment preflight
    env_preflight_parser = env_sub.add_parser(
        "preflight",
        help="[S3] 只读远端环境预检（SSH/Docker/perf 状态）。",
    )
    env_preflight_parser.add_argument("project", help="project.yaml 路径")
    env_preflight_parser.add_argument("--platform", required=True, help="目标平台 ID")
    env_preflight_parser.add_argument("--output", help="输出目录")
    env_preflight_parser.add_argument(
        "--timeout",
        type=int,
        help="覆盖每条远端预检命令的超时时间（秒）",
    )

    # workload [Step 4]
    workload_parser = subparsers.add_parser(
        "workload",
        help="[Step 4] Workload 部署。",
    )
    workload_sub = workload_parser.add_subparsers(dest="workload_command", required=True)

    workload_deploy = workload_sub.add_parser(
        "deploy",
        help="[S4] 上传 workload 并分发到容器。",
    )
    workload_deploy.add_argument("project", help="project.yaml 路径")
    workload_deploy.add_argument("--platform", required=True, help="目标平台 ID")

    # benchmark [Step 5a]
    bench_parser = subparsers.add_parser(
        "benchmark",
        help="[Step 5a] 基准测试执行。",
    )
    bench_sub = bench_parser.add_subparsers(dest="bench_command", required=True)

    bench_run = bench_sub.add_parser(
        "run",
        help="[S5a] SSH 执行 benchmark + 容器内 perf 采集。",
    )
    bench_run.add_argument("project", help="project.yaml 路径")
    bench_run.add_argument("--platform", required=True, help="目标平台 ID")
    bench_run.add_argument("--run-dir", help="运行输出目录")

    # collect [Step 5b]
    collect_parser = subparsers.add_parser(
        "collect",
        help="[Step 5b] 远程数据采集。",
    )
    collect_sub = collect_parser.add_subparsers(dest="collect_command", required=True)

    collect_run = collect_sub.add_parser(
        "run",
        help="[S5b] 从远程容器拉取 perf/asm/logs。",
    )
    collect_run.add_argument("project", help="project.yaml 路径")
    collect_run.add_argument("--platform", required=True, help="目标平台 ID")
    collect_run.add_argument("--run-dir", required=True, help="本地运行输出目录")

    # acquire [Step 5c]
    acquire_parser = subparsers.add_parser(
        "acquire",
        help="[Step 5c] 数据解析（本地）。",
    )
    acquire_sub = acquire_parser.add_subparsers(dest="acquire_command", required=True)

    acq_timing = acquire_sub.add_parser(
        "timing",
        help="[S5c] 采集用例数据（框架开销计时）。",
    )
    acq_timing.add_argument("project", help="project.yaml 路径")
    acq_timing.add_argument("--platform", required=True)
    acq_timing.add_argument("--run-dir", required=True)
    acq_timing.add_argument("--stdout-file", action="append", dest="stdout_files")

    acq_perf = acquire_sub.add_parser(
        "perf",
        help="[S5c] 采集性能分析数据（perf profile）。",
    )
    acq_perf.add_argument("project", help="project.yaml 路径")
    acq_perf.add_argument("--platform", required=True)
    acq_perf.add_argument("--run-dir", required=True)
    acq_perf.add_argument("--perf-data")
    acq_perf.add_argument("--kits-dir")
    acq_perf.add_argument("--top-n", type=int, default=50)

    acq_asm = acquire_sub.add_parser(
        "asm",
        help="[S5c] 采集机器码（反汇编）。",
    )
    acq_asm.add_argument("project", help="project.yaml 路径")
    acq_asm.add_argument("--platform", required=True)
    acq_asm.add_argument("--run-dir", required=True)
    acq_asm.add_argument("--perf-data")
    acq_asm.add_argument("--kits-dir")
    acq_asm.add_argument("--binary", action="append", dest="binaries")
    acq_asm.add_argument("--top-n", type=int, default=20)

    acquire_sub.add_parser(
        "validate",
        help="[S5c] 校验采集清单完整性。",
    ).add_argument("run_dir")

    acq_all = acquire_sub.add_parser(
        "all",
        help="[S5c] 执行全部采集子步骤。",
    )
    acq_all.add_argument("project", help="project.yaml 路径")
    acq_all.add_argument("--platform", required=True)
    acq_all.add_argument("--run-dir", required=True)
    acq_all.add_argument("--perf-data")
    acq_all.add_argument("--kits-dir")
    acq_all.add_argument("--binary", action="append", dest="binaries")
    acq_all.add_argument("--stdout-file", action="append", dest="stdout_files")
    acq_all.add_argument("--top-n", type=int, default=50)

    # backfill [Step 6]
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="[Step 6] 数据回填。",
    )
    backfill_sub = backfill_parser.add_subparsers(dest="backfill_command", required=True)

    bf_run = backfill_sub.add_parser(
        "run",
        help="[S6] 执行数据回填。",
    )
    bf_run.add_argument("project", help="project.yaml 路径")
    bf_run.add_argument("--arm-run-dir", required=True)
    bf_run.add_argument("--x86-run-dir", required=True)
    bf_run.add_argument("--output")

    backfill_sub.add_parser(
        "status",
        help="[S6] 查看回填状态。",
    ).add_argument("project")

    # bridge [Step 7]
    bridge_parser = subparsers.add_parser(
        "bridge",
        help="[Step 7] 差异分析 Issue 桥接。",
    )
    bridge_sub = bridge_parser.add_subparsers(dest="bridge_command", required=True)

    br_pub = bridge_sub.add_parser(
        "publish",
        help="[S7] 发布分析 Issue。",
    )
    br_pub.add_argument("project", help="project.yaml 路径")
    br_pub.add_argument("--repo", help="覆盖 project.yaml 中的 bridge.repo")
    br_pub.add_argument("--platform", choices=["github", "gitcode"], help="覆盖 bridge.platform")
    br_pub.add_argument("--token", help="覆盖环境变量中的 token")
    br_pub.add_argument("--dry-run", action="store_true")
    br_pub.add_argument("--max-lines", type=int, default=2000)
    br_pub.add_argument("--symbol", action="append", dest="symbols",
                        help="仅发布 symbol 匹配的函数（可多次指定，支持 fnmatch 通配符）")
    br_pub.add_argument("--base-url")

    br_fetch = bridge_sub.add_parser(
        "fetch",
        help="[S7] 拉取分析评论并回填 Dataset。",
    )
    br_fetch.add_argument("project", help="project.yaml 路径")
    br_fetch.add_argument("--repo", help="覆盖 project.yaml 中的 bridge.repo")
    br_fetch.add_argument("--platform", choices=["github", "gitcode"], help="覆盖 bridge.platform")
    br_fetch.add_argument("--token", help="覆盖环境变量中的 token")
    br_fetch.add_argument("--base-url")

    bridge_sub.add_parser(
        "status",
        help="[S7] 查看桥接状态。",
    ).add_argument("project")

    # operator (independently resumable operator-level analysis)
    operator_parser = subparsers.add_parser(
        "operator",
        help="逐算子计划、采集、归一化与双平台对比。",
    )
    operator_sub = operator_parser.add_subparsers(
        dest="operator_command", required=True
    )
    operator_plan = operator_sub.add_parser("plan", help="生成逐算子执行计划。")
    operator_plan.add_argument("project", help="project.yaml 路径")
    operator_plan.add_argument("--platform", required=True)
    operator_plan.add_argument("--run-dir", required=True)

    operator_run = operator_sub.add_parser("run", help="执行独立逐算子采集阶段。")
    operator_run.add_argument("project", help="project.yaml 路径")
    operator_run.add_argument("--platform", required=True)
    operator_run.add_argument("--run-dir", required=True)
    operator_run.add_argument(
        "--mode",
        required=True,
        choices=("context", "isolated", "profile", "all"),
    )
    operator_run.add_argument("--force", action="store_true")

    operator_normalize = operator_sub.add_parser(
        "normalize", help="归一化逐算子原始证据。"
    )
    operator_normalize.add_argument("project", help="project.yaml 路径")
    operator_normalize.add_argument("--platform", required=True)
    operator_normalize.add_argument("--run-dir", required=True)
    operator_normalize.add_argument("--force", action="store_true")

    operator_report = operator_sub.add_parser(
        "report", help="生成高可读性逐算子 HTML/Markdown 报告。"
    )
    operator_report.add_argument("project", help="project.yaml 路径")
    operator_report.add_argument("--platform", required=True)
    operator_report.add_argument("--run-dir", required=True)
    operator_report.add_argument("--force", action="store_true")

    operator_compare = operator_sub.add_parser(
        "compare", help="对比 ARM 与 x86 逐算子结果。"
    )
    operator_compare.add_argument("project", help="project.yaml 路径")
    operator_compare.add_argument("--arm-run-dir", required=True, type=Path)
    operator_compare.add_argument("--x86-run-dir", required=True, type=Path)
    operator_compare.add_argument("--output-dir", required=True, type=Path)

    # --- compare (Step 6b) ---
    compare_parser = subparsers.add_parser(
        "compare",
        help="[Step 6b] 双平台性能对比。",
    )
    compare_parser.add_argument("project", help="project.yaml 路径")
    compare_parser.add_argument("--arm-run-dir", type=Path, help="ARM run 目录")
    compare_parser.add_argument("--x86-run-dir", type=Path, help="x86 run 目录")
    compare_parser.add_argument("--output-dir", type=Path, help="输出目录")
    compare_parser.add_argument("--top-n", type=int, default=20, help="报告展示前 N 条")

    return parser


def main(argv: list[str] | None = None) -> int:
    # Import the subcommand modules lazily so importing cli/__init__ stays cheap
    # and so a syntax error in one group does not break the others' --help.
    from . import (
        acquire as _acquire,
        backfill as _backfill,
        benchmark as _benchmark,
        bridge as _bridge,
        collect as _collect,
        compare as _compare,
        config as _config,
        environment as _environment,
        operator as _operator,
        run as _run,
        validate as _validate,
        workload as _workload,
    )

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        return _validate.cmd_validate(args)
    if args.command == "config":
        return _config.handle(args)
    if args.command == "run":
        return _run.cmd_run(args)
    if args.command == "environment":
        return _environment.handle(args)
    if args.command == "workload":
        return _workload.handle(args)
    if args.command == "benchmark":
        return _benchmark.handle(args)
    if args.command == "collect":
        return _collect.handle(args)
    if args.command == "acquire":
        return _acquire.handle(args)
    if args.command == "backfill":
        return _backfill.handle(args)
    if args.command == "bridge":
        return _bridge.handle(args)
    if args.command == "compare":
        return _compare.cmd_compare(args)
    if args.command == "operator":
        return _operator.handle(args)

    parser.print_help(sys.stderr)
    return 2
