"""Registered runtime steps for the current pipeline stages."""

from __future__ import annotations

from ..contracts.step import RunContext, StepError
from ..registry import register_step


def _platform(ctx: RunContext) -> str:
    if ctx.platform is None:
        raise StepError("step requires a platform")
    return ctx.platform


def _yes(ctx: RunContext) -> bool:
    return bool(ctx.config.get("yes", False))


def _force(ctx: RunContext) -> bool:
    return bool(ctx.config.get("force", False))


@register_step
class EnvironmentDeployStep:
    name = "3"
    requires = ()
    produces = ("environment",)

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        orchestrator._run_environment_deploy(
            ctx.project_path,
            ctx.run_dir,
            _platform(ctx),
            yes=_yes(ctx),
            force=_force(ctx),
        )


def _adapter(ctx: RunContext):
    """Resolve the framework adapter, from ctx or the registry (single source).

    The adapter is the single source of framework-specific acquisition
    behaviour; steps never fall back to orchestrator._run_* branches.
    """
    if ctx.adapter is not None:
        return ctx.adapter
    framework_id = str(ctx.config.get("framework_id") or "")
    if not framework_id:
        raise StepError("step requires a framework adapter; none resolved")
    from ..adapters.registry import get_adapter

    return get_adapter(framework_id)


@register_step
class WorkloadDeployStep:
    name = "4"
    requires = ("environment",)
    produces = ("workload",)

    def run(self, ctx: RunContext) -> None:
        _adapter(ctx).deploy_workload(
            ctx.project_path, ctx.run_dir, _platform(ctx), yes=_yes(ctx),
        )


@register_step
class BenchmarkRunStep:
    name = "5a"
    requires = ("workload",)
    produces = ("timing",)

    def run(self, ctx: RunContext) -> None:
        _adapter(ctx).run_benchmark(
            ctx.project_path, ctx.run_dir, _platform(ctx), force=_force(ctx),
        )


class _CollectSubstep:
    requires: tuple[str, ...] = ("timing",)
    produces: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        args = (
            ctx.project_path,
            ctx.run_dir,
            _platform(ctx),
            self.name,
        )
        if _force(ctx):
            orchestrator._run_collect_substep(*args, force=True)
        else:
            # Preserve the long-standing four-argument hook ABI for generic
            # adapters and test/integration replacements.
            orchestrator._run_collect_substep(*args)


@register_step
class CollectPerfDataStep(_CollectSubstep):
    name = "5b.1"
    requires = ("timing",)
    produces = ("perf-data",)


@register_step
class RunPerfKitsStep(_CollectSubstep):
    name = "5b.2"
    requires = ("perf-data",)
    produces = ("perf-records",)


@register_step
class ExtractCpythonSourceStep(_CollectSubstep):
    name = "5b.2b"
    requires = ("perf-records",)
    produces = ("cpython-source",)


@register_step
class CollectObjdumpAsmStep(_CollectSubstep):
    name = "5b.3"
    requires = ("perf-records",)
    produces = ("asm",)


@register_step
class AcquireAllStep:
    name = "5c"
    requires = ("timing", "perf-records", "asm")
    produces = ("acquired")

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        orchestrator._run_acquire_all(
            ctx.project_path,
            ctx.run_dir,
            force=_force(ctx),
            platforms=ctx.config.get("platforms"),
        )


@register_step
class BackfillRunStep:
    name = "6"
    requires = ("acquired",)
    produces = ("backfilled")

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        orchestrator._run_backfill(
            ctx.project_path,
            ctx.run_dir,
            force=_force(ctx),
        )


@register_step
class PlatformCompareStep:
    name = "6b"
    requires = ("backfilled",)
    produces = ("comparison")

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        orchestrator._run_compare(ctx.project_path, ctx.run_dir)


@register_step
class BridgePublishStep:
    name = "7"
    requires = ("backfilled",)
    produces = ("published-report")

    def run(self, ctx: RunContext) -> None:
        from .. import orchestrator

        orchestrator._run_bridge_publish(ctx.project_path)


__all__ = [
    "AcquireAllStep",
    "BackfillRunStep",
    "BenchmarkRunStep",
    "BridgePublishStep",
    "CollectObjdumpAsmStep",
    "CollectPerfDataStep",
    "EnvironmentDeployStep",
    "ExtractCpythonSourceStep",
    "PlatformCompareStep",
    "RunPerfKitsStep",
    "WorkloadDeployStep",
]
