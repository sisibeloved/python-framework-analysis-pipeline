import argparse
import json
import sys
from pathlib import Path

from .validators.four_layer import validate_four_layer_project


def _schemas_dir() -> Path:
    """Resolve the schemas/ directory relative to this package."""
    return Path(__file__).resolve().parent.parent.parent / "schemas"


def _load_adapter(framework: str):
    """Load the environment adapter for a given framework."""
    if framework == "pyflink":
        from .adapters.pyflink.environment import PyFlinkEnvironmentAdapter
        return PyFlinkEnvironmentAdapter()
    raise ValueError(f"No environment adapter for framework: {framework}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyframework-pipeline",
        description="Python 框架自动化分析流程工具。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    validate_parser = subparsers.add_parser(
        "validate",
        help="校验四层输入目录或 project.yaml。",
    )
    validate_parser.add_argument("path", help="四层输入目录或 project.yaml 路径")

    # environment
    env_parser = subparsers.add_parser(
        "environment",
        help="环境搭建相关命令。",
    )
    env_sub = env_parser.add_subparsers(dest="env_command", required=True)

    # environment plan
    plan_parser = env_sub.add_parser(
        "plan",
        help="生成环境搭建计划（plan-only，不执行远程命令）。",
    )
    plan_parser.add_argument(
        "project",
        help="project.yaml 路径",
    )
    plan_parser.add_argument(
        "--platform",
        required=True,
        help="目标平台 ID，例如 arm、x86",
    )
    plan_parser.add_argument(
        "--output",
        help="输出目录（默认打印到 stdout）",
    )

    # environment validate
    env_validate_parser = env_sub.add_parser(
        "validate",
        help="校验环境记录和 readiness 报告。",
    )
    env_validate_parser.add_argument(
        "run_dir",
        help="包含 environment-plan.json / environment-record.json / readiness-report.json 的运行目录",
    )

    # acquire
    acquire_parser = subparsers.add_parser(
        "acquire",
        help="数据采集相关命令。",
    )
    acquire_sub = acquire_parser.add_subparsers(dest="acquire_command", required=True)

    # bridge
    bridge_parser = subparsers.add_parser(
        "bridge",
        help="差异分析 Issue 桥接相关命令。",
    )
    bridge_sub = bridge_parser.add_subparsers(
        dest="bridge_command", required=True,
    )

    # bridge publish
    br_pub = bridge_sub.add_parser(
        "publish",
        help="发布分析 Issue（每函数一个）。",
    )
    br_pub.add_argument("project", help="project.yaml 路径")
    br_pub.add_argument("--repo", required=True, help="owner/repo")
    br_pub.add_argument(
        "--platform", required=True, choices=["github", "gitcode"],
        help="目标平台",
    )
    br_pub.add_argument("--token", required=True, help="API token")
    br_pub.add_argument("--dry-run", action="store_true", help="只生成不发布")
    br_pub.add_argument("--max-lines", type=int, default=2000, help="最大汇编行数")
    br_pub.add_argument("--base-url", help="覆盖默认 API base URL")

    # bridge fetch
    br_fetch = bridge_sub.add_parser(
        "fetch",
        help="拉取分析评论并回填 Dataset。",
    )
    br_fetch.add_argument("project", help="project.yaml 路径")
    br_fetch.add_argument("--repo", required=True, help="owner/repo")
    br_fetch.add_argument(
        "--platform", required=True, choices=["github", "gitcode"],
        help="目标平台",
    )
    br_fetch.add_argument("--token", required=True, help="API token")
    br_fetch.add_argument("--base-url", help="覆盖默认 API base URL")

    # bridge status
    br_status = bridge_sub.add_parser(
        "status",
        help="查看桥接状态。",
    )
    br_status.add_argument("project", help="project.yaml 路径")

    # backfill
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="数据回填相关命令。",
    )
    backfill_sub = backfill_parser.add_subparsers(dest="backfill_command", required=True)

    # backfill run
    bf_run = backfill_sub.add_parser(
        "run",
        help="执行数据回填（timing + perf + asm → 四层模型）。",
    )
    bf_run.add_argument("project", help="project.yaml 路径")
    bf_run.add_argument("--arm-run-dir", required=True, help="ARM 平台运行目录")
    bf_run.add_argument("--x86-run-dir", required=True, help="x86 平台运行目录")
    bf_run.add_argument("--output", help="输出目录（默认就地更新）")

    # backfill status
    bf_status = backfill_sub.add_parser(
        "status",
        help="查看回填状态。",
    )
    bf_status.add_argument("project", help="project.yaml 路径")

    # acquire timing
    acq_timing = acquire_sub.add_parser(
        "timing",
        help="采集用例数据（框架开销计时）。",
    )
    acq_timing.add_argument("project", help="project.yaml 路径")
    acq_timing.add_argument("--platform", required=True, help="目标平台 ID")
    acq_timing.add_argument("--run-dir", required=True, help="运行输出目录")
    acq_timing.add_argument("--stdout-file", action="append", dest="stdout_files",
                            help="TM stdout 日志文件路径（可多次指定）")

    # acquire perf
    acq_perf = acquire_sub.add_parser(
        "perf",
        help="采集性能分析数据（perf profile）。",
    )
    acq_perf.add_argument("project", help="project.yaml 路径")
    acq_perf.add_argument("--platform", required=True, help="目标平台 ID")
    acq_perf.add_argument("--run-dir", required=True, help="运行输出目录")
    acq_perf.add_argument("--perf-data", help="perf.data 文件路径")
    acq_perf.add_argument("--kits-dir", help="python-performance-kits 目录路径")
    acq_perf.add_argument("--top-n", type=int, default=50, help="热点数量")

    # acquire asm
    acq_asm = acquire_sub.add_parser(
        "asm",
        help="采集机器码（反汇编）。",
    )
    acq_asm.add_argument("project", help="project.yaml 路径")
    acq_asm.add_argument("--platform", required=True, help="目标平台 ID")
    acq_asm.add_argument("--run-dir", required=True, help="运行输出目录")
    acq_asm.add_argument("--perf-data", help="perf.data 文件路径")
    acq_asm.add_argument("--kits-dir", help="python-performance-kits 目录路径")
    acq_asm.add_argument("--binary", action="append", dest="binaries",
                         help="要 objdump 的二进制文件路径（可多次指定）")
    acq_asm.add_argument("--top-n", type=int, default=20, help="热点数量")

    # acquire validate
    acq_validate = acquire_sub.add_parser(
        "validate",
        help="校验采集清单完整性。",
    )
    acq_validate.add_argument("run_dir", help="运行输出目录")

    # acquire all
    acq_all = acquire_sub.add_parser(
        "all",
        help="执行全部采集子步骤。",
    )
    acq_all.add_argument("project", help="project.yaml 路径")
    acq_all.add_argument("--platform", required=True, help="目标平台 ID")
    acq_all.add_argument("--run-dir", required=True, help="运行输出目录")
    acq_all.add_argument("--perf-data", help="perf.data 文件路径")
    acq_all.add_argument("--kits-dir", help="python-performance-kits 目录路径")
    acq_all.add_argument("--binary", action="append", dest="binaries",
                         help="要 objdump 的二进制文件路径（可多次指定）")
    acq_all.add_argument("--stdout-file", action="append", dest="stdout_files",
                         help="TM stdout 日志文件路径")
    acq_all.add_argument("--top-n", type=int, default=50, help="热点数量")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        report = validate_four_layer_project(Path(args.path))
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.status == "ok" else 1

    if args.command == "environment":
        return _handle_environment(args)

    if args.command == "acquire":
        return _handle_acquire(args)

    if args.command == "backfill":
        return _handle_backfill(args)

    if args.command == "bridge":
        return _handle_bridge(args)

    parser.print_help(sys.stderr)
    return 2


def _handle_environment(args) -> int:
    if args.env_command == "plan":
        return _cmd_env_plan(args)
    if args.env_command == "validate":
        return _cmd_env_validate(args)
    return 2


def _cmd_env_plan(args) -> int:
    from .environment.parser import load_environment_yaml
    from .environment.planning import generate_plan

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: {project_path} not found", file=sys.stderr)
        return 1

    # Detect framework from environment.yaml
    env_yaml_path = project_path.parent / "environment.yaml"
    if not env_yaml_path.exists():
        print(f"Error: environment.yaml not found at {env_yaml_path}", file=sys.stderr)
        return 1

    env_config = load_environment_yaml(env_yaml_path)
    framework = env_config.get("framework", "")

    try:
        adapter = _load_adapter(framework)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        plan = generate_plan(project_path, args.platform, adapter)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)

    if args.output:
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "environment-plan.json").write_text(plan_json, encoding="utf-8")
        print(f"Plan written to {output_dir / 'environment-plan.json'}")
    else:
        print(plan_json)

    return 0


def _cmd_env_validate(args) -> int:
    from .environment.records import validate_run

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print(f"Error: {run_dir} is not a directory", file=sys.stderr)
        return 1

    schemas = _schemas_dir()
    report = validate_run(run_dir, schemas)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.status == "ok" else 1


def _handle_acquire(args) -> int:
    if args.acquire_command == "timing":
        return _cmd_acquire_timing(args)
    if args.acquire_command == "perf":
        return _cmd_acquire_perf(args)
    if args.acquire_command == "asm":
        return _cmd_acquire_asm(args)
    if args.acquire_command == "validate":
        return _cmd_acquire_validate(args)
    if args.acquire_command == "all":
        return _cmd_acquire_all(args)
    return 2


def _resolve_run_dir(args) -> Path:
    """Resolve run-dir relative to the project directory."""
    project_dir = Path(args.project).resolve().parent
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = project_dir / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_manifest(run_dir: Path, manifest: "AcquisitionManifest") -> None:
    """Write acquisition manifest to run_dir."""
    from .acquisition.manifest import AcquisitionManifest
    manifest.write(run_dir / "acquisition-manifest.json")


def _cmd_acquire_timing(args) -> int:
    from .acquisition.timing import collect_timing
    from .acquisition.manifest import AcquisitionManifest, AcquisitionSection

    run_dir = _resolve_run_dir(args)
    stdout_files = [Path(f) for f in (args.stdout_files or [])]

    result = collect_timing(run_dir, args.platform, stdout_files or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # Update manifest
    manifest_path = run_dir / "acquisition-manifest.json"
    if manifest_path.exists():
        from .acquisition.manifest import load_manifest
        manifest = load_manifest(manifest_path)
    else:
        manifest = AcquisitionManifest(platform=args.platform, runDir=str(run_dir))
    manifest.timing = AcquisitionSection(
        status="collected" if result["cases"] else "skipped",
        files={"raw": result.get("raw_file", ""), "normalized": result.get("normalized_file", "")},
        extra={"cases": result.get("cases", [])},
    )
    _write_manifest(run_dir, manifest)
    return 0


def _cmd_acquire_perf(args) -> int:
    from .acquisition.perf_profile import collect_perf
    from .acquisition.manifest import AcquisitionManifest, AcquisitionSection

    run_dir = _resolve_run_dir(args)
    perf_data = Path(args.perf_data) if args.perf_data else None
    kits_dir = Path(args.kits_dir) if args.kits_dir else None

    result = collect_perf(run_dir, args.platform, perf_data, kits_dir, top_n=args.top_n)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    manifest_path = run_dir / "acquisition-manifest.json"
    if manifest_path.exists():
        from .acquisition.manifest import load_manifest
        manifest = load_manifest(manifest_path)
    else:
        manifest = AcquisitionManifest(platform=args.platform, runDir=str(run_dir))
    manifest.perf = AcquisitionSection(
        status=result.get("status", "pending"),
        files=result.get("files", {}),
    )
    _write_manifest(run_dir, manifest)
    return 0 if result.get("status") != "failed" else 1


def _cmd_acquire_asm(args) -> int:
    from .acquisition.machine_code import collect_asm
    from .acquisition.manifest import AcquisitionManifest, AcquisitionSection

    run_dir = _resolve_run_dir(args)
    perf_data = Path(args.perf_data) if args.perf_data else None
    kits_dir = Path(args.kits_dir) if args.kits_dir else None
    binaries = [Path(b) for b in (args.binaries or [])]

    result = collect_asm(run_dir, args.platform, perf_data, kits_dir, binaries, args.top_n)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    manifest_path = run_dir / "acquisition-manifest.json"
    if manifest_path.exists():
        from .acquisition.manifest import load_manifest
        manifest = load_manifest(manifest_path)
    else:
        manifest = AcquisitionManifest(platform=args.platform, runDir=str(run_dir))
    manifest.asm = AcquisitionSection(
        status=result.get("status", "pending"),
        extra={
            "hotspotCount": result.get("hotspotCount", 0),
            "objdumpFiles": result.get("objdumpFiles", []),
        },
    )
    _write_manifest(run_dir, manifest)
    return 0 if result.get("status") != "failed" else 1


def _cmd_acquire_validate(args) -> int:
    import jsonschema as _js

    run_dir = Path(args.run_dir)
    manifest_path = run_dir / "acquisition-manifest.json"

    if not manifest_path.exists():
        print(json.dumps({
            "status": "error",
            "errors": [f"acquisition-manifest.json not found in {run_dir}"],
        }, ensure_ascii=False, indent=2))
        return 1

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema_path = _schemas_dir() / "acquisition-manifest.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    try:
        _js.validate(instance=manifest_data, schema=schema)
    except _js.ValidationError as e:
        print(json.dumps({
            "status": "error",
            "errors": [str(e.message)],
        }, ensure_ascii=False, indent=2))
        return 1

    # Check that collected sections have their files
    errors = []
    for section in ("timing", "perf", "asm"):
        sec = manifest_data.get(section, {})
        if sec.get("status") == "collected":
            for fname in sec.get("files", {}).values():
                fpath = run_dir / fname
                if not fpath.exists():
                    errors.append(f"{section}: missing file {fname}")

    if errors:
        print(json.dumps({"status": "error", "errors": errors}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({
        "status": "ok",
        "sections": {
            s: manifest_data.get(s, {}).get("status", "pending")
            for s in ("timing", "perf", "asm")
        },
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_acquire_all(args) -> int:
    """Run all three acquisition sub-steps in sequence."""
    rc = 0
    rc |= _cmd_acquire_timing(args)
    rc |= _cmd_acquire_perf(args)
    rc |= _cmd_acquire_asm(args)
    return rc


def _handle_backfill(args) -> int:
    if args.backfill_command == "run":
        return _cmd_backfill_run(args)
    if args.backfill_command == "status":
        return _cmd_backfill_status(args)
    return 2


def _cmd_backfill_run(args) -> int:
    from .backfill.pipeline import run_backfill

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: {project_path} not found", file=sys.stderr)
        return 1

    arm_dir = Path(args.arm_run_dir)
    x86_dir = Path(args.x86_run_dir)
    output = Path(args.output) if args.output else None

    return run_backfill(project_path, arm_dir, x86_dir, output)


def _cmd_backfill_status(args) -> int:
    from .config import resolve_four_layer_root

    project_path = Path(args.project)
    root = resolve_four_layer_root(project_path)

    info: dict[str, Any] = {"project": str(project_path), "fourLayerRoot": str(root)}

    # Check dataset.
    ds_dir = root / "datasets"
    ds_files = list(ds_dir.glob("*.dataset.json")) if ds_dir.is_dir() else []
    info["dataset"] = {"exists": bool(ds_files), "file": ds_files[0].name if ds_files else None}

    # Check source.
    src_dir = root / "sources"
    src_files = list(src_dir.glob("*.source.json")) if src_dir.is_dir() else []
    info["source"] = {"exists": bool(src_files), "file": src_files[0].name if src_files else None}

    # Check project.
    proj_dir = root / "projects"
    proj_files = list(proj_dir.glob("*.project.json")) if proj_dir.is_dir() else []
    info["project"] = {"exists": bool(proj_files), "file": proj_files[0].name if proj_files else None}

    if ds_files:
        ds = json.loads(ds_files[0].read_text(encoding="utf-8"))
        info["dataset"]["cases"] = len(ds.get("cases", []))
        info["dataset"]["functions"] = len(ds.get("functions", []))
        info["dataset"]["hasStackOverview"] = bool(ds.get("stackOverview"))

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _handle_bridge(args) -> int:
    if args.bridge_command == "publish":
        return _cmd_bridge_publish(args)
    if args.bridge_command == "fetch":
        return _cmd_bridge_fetch(args)
    if args.bridge_command == "status":
        return _cmd_bridge_status(args)
    return 2


def _cmd_bridge_publish(args) -> int:
    from .bridge.analysis import publish

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: {project_path} not found", file=sys.stderr)
        return 1

    try:
        result = publish(
            project_path,
            repo=args.repo,
            platform=args.platform,
            token=args.token,
            dry_run=args.dry_run,
            max_lines=args.max_lines,
            base_url=args.base_url,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_bridge_fetch(args) -> int:
    from .bridge.analysis import fetch

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: {project_path} not found", file=sys.stderr)
        return 1

    try:
        result = fetch(
            project_path,
            repo=args.repo,
            platform=args.platform,
            token=args.token,
            base_url=args.base_url,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("failed", 0) == 0 else 1


def _cmd_bridge_status(args) -> int:
    from .bridge.analysis import status

    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Error: {project_path} not found", file=sys.stderr)
        return 1

    result = status(project_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
