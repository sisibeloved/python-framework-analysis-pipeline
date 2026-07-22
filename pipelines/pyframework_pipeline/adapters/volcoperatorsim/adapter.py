"""Volc Operator Sim framework adapter.

The target repository remains the execution authority.  This adapter owns
orchestration, overlay tasks, artifact discovery, and normalization only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts.adapter import DisassemblySpec, PerfAttachSpec, WorkloadHandle
from ...contracts.operator import OperatorCapabilities
from ..registry import register_adapter


@register_adapter
class VolcOperatorSimAdapter:
    framework_id = "volcoperatorsim"

    def describe(self) -> str:
        return "Volc Operator Sim black-box benchmark adapter"

    def operator_capabilities(self) -> OperatorCapabilities:
        return OperatorCapabilities(
            context_timing=True,
            isolated_timing=True,
            operator_perf=True,
            operator_flamegraph=True,
            operator_asm=True,
            stage_snapshot=True,
        )

    def deploy_workload(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        yes: bool = False,
    ) -> WorkloadHandle:
        from ...config import load_environment_config

        env_config = load_environment_config(project_path)
        software = env_config.get("software", {})
        return WorkloadHandle(
            container=str(
                software.get("volcOperatorSimContainer", "volc-operator-sim-bench")
            ),
            env_dir=run_dir / platform,
            metadata={
                "sourceRevision": str(
                    software.get("volcOperatorSimRevision", "")
                ),
                "hostDataRoot": str(
                    software.get("hostDataRoot", "/home/lxy/de_bench_full")
                ),
            },
        )

    def run_benchmark(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        """Run target-owned pipeline and per-operator collection paths."""

        from .acquisition import run_volc_acquisition

        return run_volc_acquisition(
            project_path, run_dir, platform, force=force
        )

    def plan_operator_cases(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        from .acquisition import plan_volc_operator_cases

        return plan_volc_operator_cases(
            project_path, run_dir, platform, force=force
        )

    def collect_context_timing(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        from .acquisition import collect_volc_context_timing

        return collect_volc_context_timing(
            project_path, run_dir, platform, force=force
        )

    def collect_operator_timing(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        from .acquisition import collect_volc_operator_timing

        return collect_volc_operator_timing(
            project_path, run_dir, platform, force=force
        )

    def collect_operator_profiles(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        from .acquisition import collect_volc_operator_profiles

        return collect_volc_operator_profiles(
            project_path, run_dir, platform, force=force
        )

    def normalize_operator_artifacts(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        force: bool = False,
    ) -> Path:
        del force
        from ...config import get_workload_config
        from .artifact_normalizer import normalize_operator_artifacts

        workload = get_workload_config(project_path)
        operator = workload.get("operatorAnalysis") or {}
        if not isinstance(operator, dict):
            operator = {}
        return normalize_operator_artifacts(
            run_dir / platform,
            platform=platform,
            min_perf_samples=max(0, int(operator.get("minPerfSamples", 5000))),
            unblock_perf=bool(operator.get("unblockPerf", False)),
            representative_profile=bool(
                operator.get(
                    "representativeProfile",
                    str(workload.get("profile", "smoke")) != "smoke",
                )
            ),
            top_symbols=max(0, int(operator.get("topSymbols", 20))),
        )

    def perf_attach_strategy(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
    ) -> PerfAttachSpec:
        return PerfAttachSpec(
            command="perf",
            output_path=run_dir / platform / "perf" / "data" / f"perf-{platform}.data",
            metadata={"mode": "target-bench-capture"},
        )

    def normalize_timing(
        self,
        timing_path: Path,
        *,
        platform: str,
    ) -> dict[str, Any]:
        if not timing_path.exists():
            return {}
        return json.loads(timing_path.read_text(encoding="utf-8"))

    def collect_flamegraph(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
        *,
        enabled: bool = False,
    ) -> Path | None:
        if not enabled:
            return None
        path = run_dir / platform / "operators" / "profiles"
        return path if path.exists() else None

    def disassembly_source(
        self,
        project_path: Path,
        run_dir: Path,
        platform: str,
    ) -> DisassemblySpec:
        return DisassemblySpec(
            source_path=run_dir / platform / "operators" / "profiles",
            output_dir=run_dir / platform / "operators" / "asm",
            metadata={"scope": "operator_case_perf"},
        )
