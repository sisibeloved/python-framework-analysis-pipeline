"""Run real Volc operators on deterministic frozen inputs for bounded perf capture."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


def read_jsonl_field(path: Path, field: str, *, limit: int) -> list[str]:
    values: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            value = payload.get(field)
            if value is not None:
                values.append(str(value))
            if len(values) >= limit:
                break
    return values


def cycle_items(items: Sequence[str], *, total: int) -> list[str]:
    if total <= 0:
        return []
    if not items:
        raise ValueError("empty frozen source")
    return [items[index % len(items)] for index in range(total)]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve(
    operator: str, params: dict[str, Any] | None = None
) -> Callable[[str], Any]:
    if operator == "identity":
        return lambda value: value
    if operator == "pdf_parse_mapper":
        from ops.pdf_ops import dj_pdf_parse_real

        return dj_pdf_parse_real
    if operator == "pdf_table_extract_mapper":
        from ops.pdf_ops import dj_pdf_table_real

        return dj_pdf_table_real
    if operator == "text_chunk_mapper":
        from ops.datajuicer_cpu_text_ops import dj_sentence_split

        return dj_sentence_split
    if operator == "bge_vectorize_mapper":
        from ops.vectorize_ops import dj_bge_vectorize_vec

        return lambda text: dj_bge_vectorize_vec(
            text, dim=384, model_name="all-MiniLM-L6-v2"
        )
    from registry import OPERATOR_CATALOG, resolve_filter_fn, resolve_mapper_fn

    metadata = OPERATOR_CATALOG.get(operator)
    if metadata is None:
        raise ValueError(f"unsupported frozen microprofile operator: {operator}")
    if metadata.category == "mapper":
        return resolve_mapper_fn(operator, params or {})
    if metadata.category == "filter":
        return resolve_filter_fn(operator, params or {})
    raise ValueError(
        "unsupported frozen microprofile operator category: "
        f"{operator} ({metadata.category})"
    )


def _load_values(args: argparse.Namespace) -> list[str]:
    if args.input_json:
        payload = json.loads(args.input_json.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("--input-json must contain a JSON list")
        values = [str(value) for value in payload]
    else:
        values = read_jsonl_field(args.manifest, args.field, limit=args.limit)
    if not values:
        raise ValueError("empty frozen source")
    return values


def _run_document_deduplicator(
    source: list[str], *, repetitions: int
) -> int:
    """Replay Volc's real Daft exact-dedup barrier on the frozen text rows."""
    import daft
    from runner.pipeline_builder import _apply_deduplicator

    checksum = 0
    for _ in range(repetitions):
        frame = daft.from_pydict({"text": source})
        deduplicated, _ = _apply_deduplicator(
            frame,
            "text",
            {"dj_ops": "document_deduplicator"},
        )
        checksum += int(deduplicated.count_rows())
    return checksum


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = _load_values(args)
    params = json.loads(args.params_json or "{}")
    if not isinstance(params, dict):
        raise ValueError("--params-json must contain a JSON object")
    outputs: list[Any] = []
    checksum = 0
    started = time.perf_counter()
    if args.operator == "document_deduplicator":
        checksum = _run_document_deduplicator(
            source,
            repetitions=args.total_calls,
        )
        capture_mode = "frozen_daft_global_deduplicator"
    else:
        function = _resolve(args.operator, params)
        for index in range(args.total_calls):
            value = source[index % len(source)]
            output = function(value)
            checksum += len(output) if hasattr(output, "__len__") else 1
            if args.output_json and len(outputs) < len(source):
                outputs.append(output)
        capture_mode = "frozen_operator_microprofile"
    elapsed = time.perf_counter() - started
    if args.output_json:
        _atomic_json(args.output_json, outputs)
    result = {
        "operator": args.operator,
        "sourceRows": len(source),
        "totalCalls": args.total_calls,
        "elapsedSeconds": elapsed,
        "checksum": checksum,
        "captureMode": capture_mode,
    }
    if args.summary_json:
        _atomic_json(args.summary_json, result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--input-json", type=Path)
    parser.add_argument("--field", default="file_path")
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--total-calls", type=int, required=True)
    parser.add_argument("--params-json", default="{}")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--summary-json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run(args)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
