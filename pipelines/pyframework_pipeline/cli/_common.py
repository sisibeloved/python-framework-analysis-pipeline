"""Shared helpers used by the cli/<group> subcommand modules.

Kept here so each subcommand module can import the small helpers (schema dir,
adapter loader, date/run-dir resolution) without reaching back into the cli
package __init__ (which is the thin main()/build_parser() shell).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def schemas_dir() -> Path:
    # cli/_common.py -> cli -> pyframework_pipeline -> pipelines -> repo-root
    return Path(__file__).resolve().parent.parent.parent.parent / "schemas"


def load_adapter(framework: str):
    if framework == "pyflink":
        from ..adapters.pyflink.environment import PyFlinkEnvironmentAdapter
        return PyFlinkEnvironmentAdapter()
    if framework == "datajuicer":
        from ..adapters.datajuicer.environment import DataJuicerEnvironmentAdapter
        return DataJuicerEnvironmentAdapter()
    if framework == "udfbenchmarking":
        from ..adapters.udfbenchmarking.environment import UdfBenchmarkingEnvironmentAdapter
        return UdfBenchmarkingEnvironmentAdapter()
    if framework == "volcoperatorsim":
        from ..adapters.volcoperatorsim.environment import VolcOperatorSimEnvironmentAdapter
        return VolcOperatorSimEnvironmentAdapter()
    raise ValueError(f"No environment adapter for framework: {framework}")


def now_date_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run_requires_bridge_token(stop_before: str | None) -> bool:
    if stop_before is None:
        return True
    step_ids = [
        "3", "4", "5a", "5a.1", "5a.2", "5b", "5b.1", "5b.2",
        "5b.2b", "5b.3", "5c", "5d", "6", "6b", "7",
    ]
    if stop_before not in step_ids:
        return True
    return step_ids.index(stop_before) > step_ids.index("7")


def resolve_run_dir(args) -> Path:
    project_dir = Path(args.project).resolve().parent
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = project_dir / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_manifest(run_dir: Path, manifest: Any) -> None:
    manifest.write(run_dir / "acquisition-manifest.json")


def resolve_bridge_config(args) -> dict[str, Any] | None:
    """Get bridge config from project.yaml, with CLI overrides."""
    from ..config import load_project_config

    project_path = Path(args.project)
    try:
        full_config = load_project_config(project_path)
    except (FileNotFoundError, ValueError):
        full_config = {}

    config: dict[str, Any] = full_config.get("bridge", {})

    env_var = config.get("tokenEnvVar", "PYFRAMEWORK_BRIDGE_TOKEN")
    token = os.environ.get(env_var, "")
    if token:
        config["token"] = token

    if hasattr(args, "repo") and args.repo:
        config["repo"] = args.repo
    if hasattr(args, "platform") and args.platform:
        config["platform"] = args.platform
    if hasattr(args, "token") and args.token:
        config["token"] = args.token

    is_dry_run = getattr(args, "dry_run", False)

    missing = []
    if not config.get("repo"):
        missing.append("bridge.repo (project.yaml or --repo)")
    if not config.get("platform"):
        missing.append("bridge.platform (project.yaml or --platform)")
    if not config.get("token") and not is_dry_run:
        missing.append("token (env var PYFRAMEWORK_BRIDGE_TOKEN or --token)")

    if missing:
        print(f"Error: missing {', '.join(missing)}", file=sys.stderr)  # noqa: T201
        return None

    return config


__all__ = [
    "schemas_dir",
    "load_adapter",
    "now_date_str",
    "run_requires_bridge_token",
    "resolve_run_dir",
    "write_manifest",
    "resolve_bridge_config",
]
