"""Build deterministic Host-persistent scale fixtures for Volc pipelines."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator, Sequence


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _jsonl_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row must be an object: {path}")
            records.append(value)
    return records


def cycle_jsonl_rows(
    *,
    source: Path,
    output: Path,
    rows: int,
    fixture_id: str,
) -> dict[str, Any]:
    """Cycle an immutable manifest without copying its referenced media."""

    if rows <= 0:
        raise ValueError("rows must be positive")
    records = _jsonl_records(source)
    if not records:
        raise ValueError(f"empty JSONL source: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + ".partial")
    digest = hashlib.sha256()
    with temporary.open("wb") as handle:
        for index in range(rows):
            source_record = records[index % len(records)]
            record = dict(source_record)
            original_id = str(record.get("sample_id") or index % len(records))
            record["source_sample_id"] = original_id
            record["sample_id"] = f"{fixture_id}_{index:08d}"
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            handle.write(line)
            digest.update(line)
    os.replace(temporary, output)
    return {
        "schemaVersion": 1,
        "fixture_id": fixture_id,
        "kind": "file_manifest",
        "field": "file_path",
        "path": str(output),
        "manifest_path": str(output),
        "rows": rows,
        "sourceRows": len(records),
        "sourceManifest": str(source),
        "sourceManifestSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "selection_policy": (
            f"ordered cycle of immutable {source.name}; source index = row % "
            f"{len(records)}; referenced media is not copied"
        ),
        "input_fingerprint": "sha256:" + digest.hexdigest(),
        "representations": {
            "jsonl": {"files": 1, "bytes": output.stat().st_size}
        },
        "builder": "pyframework scale_freeze.py@1",
    }


def _parquet_texts(
    source: Path, *, rows: int, field: str = "text"
) -> tuple[list[str], list[Path]]:
    import pyarrow.parquet as parquet

    files = sorted(path for path in source.rglob("*.parquet") if path.is_file())
    if not files:
        raise ValueError(f"no parquet files under {source}")
    values: list[str] = []
    for path in files:
        parquet_file = parquet.ParquetFile(path)
        if field not in parquet_file.schema_arrow.names:
            raise ValueError(f"field {field!r} is missing from {path}")
        for batch in parquet_file.iter_batches(columns=[field], batch_size=4096):
            for value in batch.column(0).to_pylist():
                if value is None:
                    continue
                text = str(value)
                if not text:
                    continue
                values.append(text)
                if len(values) == rows:
                    return values, files
    if len(values) < rows:
        raise ValueError(f"requested {rows} text rows, found {len(values)}")
    return values, files


def freeze_parquet_text(
    *,
    source: Path,
    output_dir: Path,
    rows: int,
    fixture_id: str,
    fragments: int = 4,
    field: str = "text",
) -> dict[str, Any]:
    """Freeze the first N non-empty texts and create matching JSONL/Lance."""

    if rows <= 0 or fragments <= 0:
        raise ValueError("rows and fragments must be positive")
    import lance
    import pyarrow as arrow

    texts, source_files = _parquet_texts(source, rows=rows, field=field)
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "dataset.jsonl"
    temporary = jsonl_path.with_name("." + jsonl_path.name + ".partial")
    digest = hashlib.sha256()
    with temporary.open("wb") as handle:
        for text in texts:
            line = (
                json.dumps(
                    {field: text},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            handle.write(line)
            digest.update(line)
    os.replace(temporary, jsonl_path)
    lance_path = output_dir / "dataset_p4.lance"
    max_rows = max(1, math.ceil(rows / fragments))
    table = arrow.table({field: texts})
    lance.write_dataset(
        table,
        str(lance_path),
        mode="overwrite",
        max_rows_per_file=max_rows,
        max_rows_per_group=min(8192, max_rows),
    )
    actual_fragments = len(list(lance.dataset(str(lance_path)).get_fragments()))
    if actual_fragments != fragments:
        raise ValueError(
            f"Lance partition mismatch: expected={fragments} actual={actual_fragments}"
        )
    lance_files = [path for path in lance_path.rglob("*") if path.is_file()]
    return {
        "schemaVersion": 1,
        "fixture_id": fixture_id,
        "kind": "lance",
        "field": field,
        "path": str(lance_path),
        "jsonl_mirror": str(jsonl_path),
        "manifest_path": str(output_dir / "fixture-manifest.json"),
        "rows": rows,
        "source": str(source),
        "source_policy": (
            f"lexicographically sorted parquet files; first {rows} non-empty "
            f"{field} values"
        ),
        "source_files": [
            {"path": str(path), "bytes": path.stat().st_size}
            for path in source_files
        ],
        "input_fingerprint": "sha256:" + digest.hexdigest(),
        "partition_spec": {
            "fragments": actual_fragments,
            "max_rows_per_file": max_rows,
            "max_rows_per_group": min(8192, max_rows),
        },
        "representations": {
            "jsonl": {"files": 1, "bytes": jsonl_path.stat().st_size},
            "lance": {
                "files": len(lance_files),
                "bytes": sum(path.stat().st_size for path in lance_files),
            },
        },
        "builder": "pyframework scale_freeze.py@1",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    cycle = subparsers.add_parser("cycle-jsonl")
    cycle.add_argument("--source", type=Path, required=True)
    cycle.add_argument("--output-dir", type=Path, required=True)
    cycle.add_argument("--rows", type=int, required=True)
    cycle.add_argument("--fixture-id", required=True)
    text = subparsers.add_parser("parquet-text")
    text.add_argument("--source", type=Path, required=True)
    text.add_argument("--output-dir", type=Path, required=True)
    text.add_argument("--rows", type=int, required=True)
    text.add_argument("--fixture-id", required=True)
    text.add_argument("--fragments", type=int, default=4)
    text.add_argument("--field", default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cycle-jsonl":
        output = args.output_dir / "manifest.jsonl"
        payload = cycle_jsonl_rows(
            source=args.source,
            output=output,
            rows=args.rows,
            fixture_id=args.fixture_id,
        )
    else:
        payload = freeze_parquet_text(
            source=args.source,
            output_dir=args.output_dir,
            rows=args.rows,
            fixture_id=args.fixture_id,
            fragments=args.fragments,
            field=args.field,
        )
    manifest = args.output_dir / "fixture-manifest.json"
    payload["manifest_path"] = str(manifest)
    _atomic_json(manifest, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
