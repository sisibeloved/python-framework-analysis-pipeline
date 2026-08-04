"""Independent operator-analysis CLI handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def handle(args) -> int:
    try:
        if args.operator_command == "compare":
            from ..adapters.volcoperatorsim.operator_compare import (
                compare_operator_platforms,
            )

            result = compare_operator_platforms(
                args.arm_run_dir, args.x86_run_dir, args.output_dir
            )
            output = result if isinstance(result, Path) else args.output_dir
        else:
            from ..adapters.registry import get_adapter
            from ..contracts.adapter import OperatorAnalysisAdapter
            from ..orchestrator import _framework_id_from_project

            project = Path(args.project)
            run_dir = Path(args.run_dir)
            adapter = get_adapter(_framework_id_from_project(project))
            if not isinstance(adapter, OperatorAnalysisAdapter):
                raise ValueError("configured framework has no operator-analysis adapter")
            if args.operator_command == "plan":
                output = adapter.plan_operator_cases(
                    project, run_dir, args.platform
                )
            elif args.operator_command == "normalize":
                output = adapter.normalize_operator_artifacts(
                    project,
                    run_dir,
                    args.platform,
                    force=args.force,
                )
            elif args.operator_command == "report":
                from ..adapters.volcoperatorsim.operator_report import (
                    render_operator_reports,
                )

                adapter.normalize_operator_artifacts(
                    project,
                    run_dir,
                    args.platform,
                    force=args.force,
                )
                output = render_operator_reports(run_dir / args.platform)
            elif args.operator_command == "run":
                output = _run_modes(adapter, project, run_dir, args)
                if _framework_id_from_project(project) == "volcoperatorsim":
                    from ..adapters.volcoperatorsim.acquisition_manifest import (
                        build_acquisition_manifest,
                    )

                    build_acquisition_manifest(
                        run_dir / args.platform,
                        platform=args.platform,
                    )
            else:
                return 2
        print(
            json.dumps(
                {"status": "completed", "output": str(output)},
                ensure_ascii=False,
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


def _run_modes(adapter, project: Path, run_dir: Path, args):
    modes = (
        ("context", adapter.collect_context_timing),
        ("isolated", adapter.collect_operator_timing),
        ("profile", adapter.collect_operator_profiles),
    )
    output = run_dir / args.platform / "operators"
    for name, method in modes:
        if args.mode in (name, "all"):
            output = method(
                project,
                run_dir,
                args.platform,
                force=args.force,
            )
    return output
