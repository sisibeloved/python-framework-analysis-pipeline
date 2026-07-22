"""Render a dependency-free native flamegraph from ``perf script`` stacks."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


RENDERER_VERSION = 1
_FRAME = re.compile(
    r"^\s*[0-9a-fA-F]+\s+(.+?)(?:\s+\(([^()]*)\))?\s*$"
)


def parse_perf_script_stacks(text: str) -> tuple[tuple[str, ...], ...]:
    """Parse leaf-first ``perf script`` callchains into root-first stacks."""

    stacks: list[tuple[str, ...]] = []
    for block in re.split(r"\n\s*\n", text):
        frames: list[str] = []
        for line in block.splitlines()[1:]:
            match = _FRAME.match(line)
            if not match:
                continue
            symbol = match.group(1).strip()
            dso = (match.group(2) or "").strip()
            if symbol:
                frames.append(f"{symbol} [{Path(dso).name}]" if dso else symbol)
        if frames:
            stacks.append(tuple(reversed(frames)))
    return tuple(stacks)


def render_perf_script_svg(text: str) -> tuple[str, dict[str, Any]]:
    """Return a deterministic SVG flamegraph and its audit metadata."""

    stacks = parse_perf_script_stacks(text)
    if not stacks:
        raise ValueError("perf-script.txt contains no parseable callchains")

    root: dict[str, Any] = {"count": 0, "children": {}}
    max_depth = 0
    for stack in stacks:
        root["count"] += 1
        node = root
        max_depth = max(max_depth, len(stack))
        for frame in stack:
            children = node["children"]
            node = children.setdefault(frame, {"count": 0, "children": {}})
            node["count"] += 1

    width = 1200
    frame_height = 18
    top = 42
    bottom = 24
    graph_height = max_depth * frame_height
    height = top + graph_height + bottom
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:monospace;font-size:11px;fill:#111}"
        ".title{font-size:16px;font-weight:bold}</style>",
        '<rect width="100%" height="100%" fill="#fff"/>',
        '<text class="title" x="10" y="22">Native perf sampled flamegraph</text>',
        f'<text x="10" y="37">samples={len(stacks)}; source=perf-script.txt; '
        "width represents sampled CPU-time share</text>",
    ]

    def draw(children: Mapping[str, Mapping[str, Any]], x: float, span: float, depth: int) -> None:
        total = sum(int(item["count"]) for item in children.values())
        if total <= 0:
            return
        cursor = x
        for label, node in sorted(
            children.items(), key=lambda item: (-int(item[1]["count"]), item[0])
        ):
            node_width = span * int(node["count"]) / total
            y = top + graph_height - (depth + 1) * frame_height
            digest = hashlib.sha256(label.encode("utf-8")).digest()
            color = f"rgb({190 + digest[0] % 56},{80 + digest[1] % 111},{40 + digest[2] % 81})"
            safe = html.escape(label, quote=True)
            parts.append(
                f'<g><title>{safe} — samples={int(node["count"])}</title>'
                f'<rect x="{cursor:.3f}" y="{y}" width="{max(node_width - 0.5, 0):.3f}" '
                f'height="{frame_height - 1}" fill="{color}" stroke="#fff" stroke-width="0.5"/>'
            )
            max_chars = max(int(node_width / 7) - 1, 0)
            if max_chars >= 3:
                visible = label if len(label) <= max_chars else label[: max_chars - 1] + "…"
                parts.append(
                    f'<text x="{cursor + 3:.3f}" y="{y + 13}">{html.escape(visible)}</text>'
                )
            parts.append("</g>")
            draw(node["children"], cursor, node_width, depth + 1)
            cursor += node_width

    draw(root["children"], 0.0, float(width), 0)
    parts.append("</svg>\n")
    metadata = {
        "schemaVersion": 1,
        "renderer": "pyframework-native-perf-flamegraph",
        "rendererVersion": RENDERER_VERSION,
        "source": "perf-script.txt",
        "sampleCount": len(stacks),
        "maxDepth": max_depth,
    }
    return "".join(parts), metadata


def render_from_capture(
    *,
    perf_root: Path,
    engine: str,
    source_case: str,
    output_case: str,
) -> Path:
    """Locate one capture, render its native SVG, and write audit metadata."""

    matches = sorted(
        path
        for path in perf_root.rglob("perf-script.txt")
        if f"/{engine}/{source_case}/" in path.as_posix()
    )
    if not matches:
        data_matches = sorted(
            path
            for path in perf_root.rglob("perf.data")
            if f"/{engine}/{source_case}/" in path.as_posix()
        )
        if not data_matches:
            raise FileNotFoundError(
                f"no perf capture for engine={engine} case={source_case}"
            )
        source = data_matches[-1].with_name("perf-script.txt")
        result = subprocess.run(
            ["perf", "script", "-i", str(data_matches[-1])],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "perf script failed")
        source.write_text(result.stdout, encoding="utf-8")
    else:
        source = matches[-1]

    svg, metadata = render_perf_script_svg(
        source.read_text(encoding="utf-8", errors="replace")
    )
    output_dir = source.parent.parent / output_case
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / "cpu.svg"
    tmp_svg = svg_path.with_suffix(".svg.partial")
    tmp_svg.write_text(svg, encoding="utf-8")
    os.replace(tmp_svg, svg_path)
    metadata.update(
        {
            "engineId": engine,
            "sourceCase": source_case,
            "outputCase": output_case,
            "sourcePath": str(source),
            "fallbackReason": "py_spy_failed",
        }
    )
    metadata_path = output_dir / "flamegraph-metadata.json"
    tmp_metadata = metadata_path.with_suffix(".json.partial")
    tmp_metadata.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_metadata, metadata_path)
    return svg_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--perf-root", type=Path, required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--source-case", required=True)
    parser.add_argument("--output-case", required=True)
    args = parser.parse_args(argv)
    output = render_from_capture(
        perf_root=args.perf_root,
        engine=args.engine,
        source_case=args.source_case,
        output_case=args.output_case,
    )
    print(f"PYFRAMEWORK_NATIVE_FLAMEGRAPH={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
