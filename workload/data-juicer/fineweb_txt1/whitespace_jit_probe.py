#!/usr/bin/env python3
"""Focused CinderX JIT probe for Data-Juicer whitespace normalization."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from data_juicer.ops.common.special_characters import VARIOUS_WHITESPACES
from data_juicer.ops.mapper.whitespace_normalization_mapper import WhitespaceNormalizationMapper


def normalize_original_shape(samples, text_key="text"):
    for idx, text in enumerate(samples[text_key]):
        text = text.strip()
        samples[text_key][idx] = "".join([char if char not in VARIOUS_WHITESPACES else " " for char in text])
    return samples


def normalize_text_only(text):
    text = text.strip()
    return "".join([char if char not in VARIOUS_WHITESPACES else " " for char in text])


def normalize_generator(text):
    text = text.strip()
    return "".join(char if char not in VARIOUS_WHITESPACES else " " for char in text)


def normalize_translate(text, table):
    return text.strip().translate(table)


def make_translate_table():
    return str.maketrans({char: " " for char in VARIOUS_WHITESPACES})


def load_texts(args):
    if args.input_parquet:
        import pyarrow.parquet as pq

        table = pq.read_table(args.input_parquet, columns=["text"])
        texts = table.column("text").to_pylist()[: args.batch_size]
        return [text or "" for text in texts]

    base = (
        "  FineWeb sample\tline with unicode\u2003space and\n"
        "links already cleaned; many ordinary ASCII words.  "
    )
    return [(base * args.text_repeat) for _ in range(args.batch_size)]


def time_call(label, func, make_arg, loops):
    timings = []
    checksum = 0
    for _ in range(loops):
        arg = make_arg()
        start = time.perf_counter()
        result = func(arg)
        elapsed = time.perf_counter() - start
        timings.append(elapsed)
        if isinstance(result, dict):
            checksum += sum(len(text) for text in result["text"][:8])
        else:
            checksum += len(result)
    return {
        "label": label,
        "loops": loops,
        "min_s": min(timings),
        "median_s": statistics.median(timings),
        "total_s": sum(timings),
        "checksum": checksum,
    }


def jit_info(functions):
    try:
        import cinderx
        import cinderx.jit as jit
    except Exception as exc:
        return {
            "available": False,
            "error": repr(exc),
        }

    info = {
        "available": True,
        "initialized": cinderx.is_initialized(),
        "import_error": cinderx.get_import_error(),
        "enabled": jit.is_enabled(),
        "compile_after": jit.get_compile_after_n_calls(),
        "functions": {},
    }
    for name, func in functions.items():
        try:
            info["functions"][name] = jit.is_jit_compiled(func)
        except Exception as exc:
            info["functions"][name] = f"ERROR {exc!r}"
    try:
        compiled = [str(item) for item in jit.get_compiled_functions()]
    except Exception as exc:
        info["compiled_error"] = repr(exc)
    else:
        needles = (
            "WhitespaceNormalizationMapper",
            "normalize_original_shape",
            "normalize_text_only",
            "normalize_generator",
            "<listcomp>",
            "<genexpr>",
        )
        info["compiled_matches"] = [item for item in compiled if any(needle in item for needle in needles)]
        info["compiled_count"] = len(compiled)
    return info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-parquet", default=None)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--text-repeat", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--loops", type=int, default=40)
    parser.add_argument("--json-output", default=None)
    args = parser.parse_args()

    texts = load_texts(args)
    table = make_translate_table()
    op = WhitespaceNormalizationMapper()

    functions = {
        "class_process_batched": WhitespaceNormalizationMapper.process_batched,
        "normalize_original_shape": normalize_original_shape,
        "normalize_text_only": normalize_text_only,
        "normalize_generator": normalize_generator,
    }
    print("jit_info_before", json.dumps(jit_info(functions), sort_keys=True, ensure_ascii=False))

    for _ in range(args.warmup):
        op.process_batched({"text": list(texts)})
        normalize_original_shape({"text": list(texts)})
        for text in texts[:64]:
            normalize_text_only(text)
            normalize_generator(text)
            normalize_translate(text, table)

    print("jit_info_after_warmup", json.dumps(jit_info(functions), sort_keys=True, ensure_ascii=False))

    results = [
        time_call("class_process_batched", lambda sample: op.process_batched(sample), lambda: {"text": list(texts)}, args.loops),
        time_call("standalone_original_shape", normalize_original_shape, lambda: {"text": list(texts)}, args.loops),
        time_call("text_only_listcomp", lambda text: normalize_text_only(text), lambda: texts[0], args.loops * args.batch_size),
        time_call("text_only_generator", lambda text: normalize_generator(text), lambda: texts[0], args.loops * args.batch_size),
        time_call("text_only_translate", lambda text: normalize_translate(text, table), lambda: texts[0], args.loops * args.batch_size),
    ]
    payload = {
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
        "batch_size": len(texts),
        "first_text_len": len(texts[0]) if texts else 0,
        "results": results,
        "jit_info_final": jit_info(functions),
    }
    print("probe_summary", json.dumps(payload, sort_keys=True, ensure_ascii=False))
    if args.json_output:
        Path(args.json_output).write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
