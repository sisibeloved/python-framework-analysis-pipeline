#!/usr/bin/env python3
"""Print a small, non-secret CPython/CinderX state fingerprint."""

from __future__ import annotations

import os
import sys
import sysconfig


def main() -> int:
    print("executable", sys.executable)
    print("version", sys.version.replace("\n", " "))
    print("soabi", sysconfig.get_config_var("SOABI"))
    print("include", sysconfig.get_config_var("INCLUDEPY"))
    for name in (
        "PYTHONJITAUTO",
        "PYTHONJIT",
        "PYTHONJITHUGEPAGES",
        "PYTHONJITLISTFILE",
        "PYTHONPATH",
        "CINDERX_JIT",
        "CINDERX",
    ):
        print(f"{name}={os.environ.get(name, '<unset>')}")

    try:
        import cinderx
    except Exception as exc:
        print("cinderx_error", repr(exc))
        return 0

    print("cinderx_file", getattr(cinderx, "__file__", None))
    print("cinderx_initialized", cinderx.is_initialized())
    print("cinderx_import_error", cinderx.get_import_error())
    print(
        "cinderx_jit_attrs",
        ",".join(
            name
            for name in dir(cinderx)
            if "jit" in name.lower() or "init" in name.lower()
        ),
    )
    try:
        import cinderx.jit as jit
    except Exception as exc:
        print("cinderx_jit_error", repr(exc))
        return 0

    print("cinderx_jit_file", getattr(jit, "__file__", None))
    print(
        "cinderx_jit_public_attrs",
        ",".join(name for name in dir(jit) if not name.startswith("__")),
    )
    for name in ("is_enabled", "is_jit_enabled", "auto_jit_threshold"):
        if hasattr(jit, name):
            value = getattr(jit, name)
            try:
                value = value() if callable(value) else value
            except Exception as exc:
                value = repr(exc)
            print(f"cinderx_jit_{name}", value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
