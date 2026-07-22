"""Framework adapter registry."""

from __future__ import annotations

from typing import cast

from ..contracts.adapter import FrameworkAdapter


_ADAPTERS: dict[str, FrameworkAdapter] = {}
_BUILTINS_LOADED = False


def register_adapter(adapter_cls: type[FrameworkAdapter]) -> type[FrameworkAdapter]:
    """Class decorator for registering a framework adapter."""

    adapter = adapter_cls()
    framework_id = getattr(adapter, "framework_id", "")
    if not framework_id:
        raise ValueError("adapter class must define framework_id")
    _ADAPTERS[str(framework_id)] = adapter
    return adapter_cls


def get_adapter(framework_id: str) -> FrameworkAdapter:
    """Return the adapter registered for the requested framework."""

    _load_builtin_adapters()
    try:
        return _ADAPTERS[framework_id]
    except KeyError as exc:
        known = ", ".join(sorted(_ADAPTERS)) or "<none>"
        raise KeyError(f"unknown framework adapter: {framework_id}; known: {known}") from exc


def adapter_names() -> set[str]:
    """Return registered adapter ids after loading built-ins."""

    _load_builtin_adapters()
    return set(_ADAPTERS)


def _load_builtin_adapters() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    from .datajuicer.adapter import DataJuicerAdapter as _DataJuicerAdapter
    from .pyflink.adapter import PyFlinkAdapter as _PyFlinkAdapter
    from .udfbenchmarking.adapter import UdfBenchmarkingAdapter as _UdfBenchmarkingAdapter
    from .volcoperatorsim.adapter import VolcOperatorSimAdapter as _VolcOperatorSimAdapter

    cast(object, _DataJuicerAdapter)
    cast(object, _PyFlinkAdapter)
    cast(object, _UdfBenchmarkingAdapter)
    cast(object, _VolcOperatorSimAdapter)
    _BUILTINS_LOADED = True
