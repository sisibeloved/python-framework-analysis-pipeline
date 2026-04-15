import argparse
import json
import sys
from pathlib import Path

from .validators.four_layer import validate_four_layer_project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyframework-pipeline",
        description="Python 框架自动化分析流程工具。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="校验四层输入目录或 project.yaml。",
    )
    validate_parser.add_argument("path", help="四层输入目录或 project.yaml 路径")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "validate":
        report = validate_four_layer_project(Path(args.path))
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.status == "ok" else 1

    parser.print_help(sys.stderr)
    return 2
