#!/usr/bin/env python3
"""Validate imports needed by the FineWeb-Edu TXT-1 benchmark."""

from __future__ import annotations

import importlib
import sys
import sysconfig


MODULES = [
    "data_juicer",
    "datasets",
    "pyarrow",
    "dill",
    "data_juicer.ops.mapper.clean_html_mapper",
    "data_juicer.ops.mapper.clean_links_mapper",
    "data_juicer.ops.mapper.whitespace_normalization_mapper",
    "data_juicer.ops.mapper.clean_email_mapper",
    "data_juicer.ops.filter.language_id_score_filter",
    "data_juicer.ops.filter.text_length_filter",
    "data_juicer.ops.filter.perplexity_filter",
    "data_juicer.ops.deduplicator.document_deduplicator",
    "data_juicer.ops.mapper.text_chunk_mapper",
]


def main() -> int:
    print("executable", sys.executable)
    print("version", sys.version.replace("\n", " "))
    print("soabi", sysconfig.get_config_var("SOABI"))
    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            print(f"{name}=ERROR {exc!r}")
            return 1
        print(f"{name}=OK {getattr(module, '__file__', '')}")
    try:
        import cinderx
    except Exception as exc:
        print("cinderx=ERROR", repr(exc))
    else:
        print("cinderx=OK", cinderx.is_initialized(), cinderx.get_import_error())
        try:
            import cinderx.jit as jit
        except Exception as exc:
            print("cinderx.jit=ERROR", repr(exc))
        else:
            print("cinderx.jit=OK", jit.is_enabled())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
