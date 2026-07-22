"""`run` subcommand handler (one-click full pipeline, resumable)."""
from __future__ import annotations

import sys
from pathlib import Path

from ._common import now_date_str, run_requires_bridge_token


def cmd_run(args) -> int:
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    from ..config import get_run_config, load_project_config, validate_pipeline_config

    project_path = Path(args.project)
    config_report = validate_pipeline_config(
        project_path,
        require_bridge_token=run_requires_bridge_token(args.stop_before),
    )
    if config_report["status"] != "ok":
        print("Error: config validation failed", file=sys.stderr)  # noqa: T201
        for issue in config_report["issues"]:
            print(  # noqa: T201
                f"  - {issue['path']}: {issue['message']}",
                file=sys.stderr,
            )
        return 1

    from ..orchestrator import run_pipeline

    config = load_project_config(project_path)
    run_config = get_run_config(project_path)

    # Resolve run directory.
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs_base = project_path.parent / "runs"
        run_dir = runs_base / now_date_str()
    run_dir.mkdir(parents=True, exist_ok=True)

    return run_pipeline(
        project_path,
        run_dir,
        resume_from=args.resume_from,
        stop_before=args.stop_before,
        platforms=args.platform,
        force=args.force,
        yes=args.yes,
    )
