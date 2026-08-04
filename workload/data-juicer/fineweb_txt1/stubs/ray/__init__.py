"""Tiny Ray stub for Data-Juicer default-executor imports."""

from __future__ import annotations

from . import data


def is_initialized() -> bool:
    return False


def init(*_args, **_kwargs):
    return None


def shutdown() -> None:
    return None


def cluster_resources() -> dict[str, float]:
    return {}


def available_resources() -> dict[str, float]:
    return {}


def nodes() -> list[dict]:
    return []


def get(value):
    return value


def remote(*decorator_args, **_decorator_kwargs):
    def decorate(obj):
        return obj

    if len(decorator_args) == 1 and callable(decorator_args[0]):
        return decorator_args[0]
    return decorate


class _RuntimeContext:
    def get_job_id(self) -> str:
        return "pyframework-default"


def get_runtime_context() -> _RuntimeContext:
    return _RuntimeContext()
