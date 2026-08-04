"""Runtime patches for CPU-only Data-Juicer TXT-1 runs on Python 3.14."""

from __future__ import annotations

import sys


def _package_base(spec: object) -> str:
    name = str(spec).strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    if "[" in name:
        name = name.split("[", 1)[0]
    for marker in ("==", ">=", "<=", ">", "<"):
        if marker in name:
            name = name.split(marker, 1)[0]
            break
    return name.strip().lower().replace("_", "-")


try:
    from data_juicer.utils.lazy_loader import LazyLoader
except Exception as exc:  # pragma: no cover - diagnostic only
    sys.stderr.write(f"[pyframework] LazyLoader patch skipped: {exc!r}\n")
else:
    _original_check_packages = LazyLoader.check_packages.__func__

    @classmethod
    def _cpu_only_check_packages(cls, package_specs, pip_args=None):
        specs = [package_specs] if isinstance(package_specs, str) else list(package_specs)
        filtered = [
            spec
            for spec in specs
            if _package_base(spec) not in {"ray", "torch"}
        ]
        skipped = [spec for spec in specs if spec not in filtered]
        if skipped:
            sys.stderr.write(
                "[pyframework] skipping Data-Juicer lazy install for "
                + ", ".join(map(str, skipped))
                + "\n"
            )
        if filtered:
            return _original_check_packages(cls, filtered, pip_args)
        return None

    LazyLoader.check_packages = _cpu_only_check_packages
