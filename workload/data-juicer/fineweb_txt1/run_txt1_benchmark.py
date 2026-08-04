#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


OP_BLOCKS = [
    (
        "clean_html_mapper",
        """\
  - clean_html_mapper: {}
""",
    ),
    (
        "clean_links_mapper",
        """\
  - clean_links_mapper: {}
""",
    ),
    (
        "whitespace_normalization_mapper",
        """\
  - whitespace_normalization_mapper: {}
""",
    ),
    (
        "clean_email_mapper",
        """\
  - clean_email_mapper: {}
""",
    ),
    (
        "language_id_score_filter",
        """\
  - language_id_score_filter:
      lang: en
      min_score: 0.80
""",
    ),
    (
        "text_length_filter",
        """\
  - text_length_filter:
      min_len: 200
      max_len: 200000
""",
    ),
    (
        "perplexity_filter",
        """\
  - perplexity_filter:
      lang: en
      min_ppl: 0
      max_ppl: 1500
""",
    ),
    (
        "document_deduplicator",
        """\
  - document_deduplicator:
      lowercase: false
      ignore_non_character: false
""",
    ),
    (
        "text_chunk_mapper",
        """\
  - text_chunk_mapper:
      max_len: 4096
      split_pattern: "\\\\n\\\\n"
      overlap_len: 0
""",
    ),
]

OP_BLOCK_BY_NAME = dict(OP_BLOCKS)
DEFAULT_OPS = [name for name, _ in OP_BLOCKS]


def parse_np_value(value):
    raw = str(value).strip().lower()
    if raw in {"none", "null"}:
        return None
    try:
        np_value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid --np value {value!r}") from exc
    if np_value <= 0:
        raise ValueError(f"--np must be positive or none/null, got {value!r}")
    return np_value


def np_label(value):
    return "none" if value is None else str(value)


def yaml_scalar(value):
    return "null" if value is None else str(value)


def parse_op_np_overrides(values):
    overrides = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"invalid --op-np value {value!r}, expected OP=N")
        name, raw_np = value.split("=", 1)
        name = name.strip()
        raw_np = raw_np.strip().lower()
        if name not in OP_BLOCK_BY_NAME:
            raise ValueError(f"unknown op in --op-np: {name}")
        if raw_np in {"none", "null"}:
            np_value = None
        else:
            try:
                np_value = int(raw_np)
            except ValueError as exc:
                raise ValueError(f"invalid num_proc in --op-np {value!r}") from exc
            if np_value <= 0:
                raise ValueError(f"num_proc must be positive in --op-np {value!r}")
        overrides[name] = np_value
    return overrides


def op_np_suffix(overrides):
    if not overrides:
        return ""
    parts = [
        f"{''.join(part[0] for part in name.split('_') if part)}{'none' if overrides[name] is None else overrides[name]}"
        for name in sorted(overrides)
    ]
    return "_opnp_" + "_".join(parts)


def apply_op_np_override(name, block, op_np_overrides):
    if name not in op_np_overrides:
        return block

    extra = f"""\
      num_proc: {yaml_scalar(op_np_overrides[name])}
      auto_op_parallelism: false
"""
    marker = f"  - {name}: {{}}\n"
    if block == marker:
        return f"  - {name}:\n{extra}"

    if not block.endswith("\n"):
        block += "\n"
    return block + extra


def render_process(op_names, op_np_overrides=None):
    op_np_overrides = op_np_overrides or {}
    unknown = [name for name in op_names if name not in OP_BLOCK_BY_NAME]
    if unknown:
        raise ValueError(f"unknown op(s): {', '.join(unknown)}")
    unused = sorted(set(op_np_overrides) - set(op_names))
    if unused:
        raise ValueError(f"--op-np specified op(s) not selected by --only-ops: {', '.join(unused)}")
    return "process:\n" + "".join(
        apply_op_np_override(name, OP_BLOCK_BY_NAME[name], op_np_overrides) for name in op_names
    )


def write_config(path, *, project_name, input_path, export_path, work_dir, tmp_dir, np_value, op_names, op_np_overrides):
    cfg = f"""\
project_name: {project_name}

dataset_path: {input_path}
export_path: {export_path}
export_type: parquet
work_dir: {work_dir}

text_keys: text
np: {yaml_scalar(np_value)}
auto_op_parallelism: false
executor_type: default
skip_op_error: false
use_cache: false
ds_cache_dir: /cache/hf-datasets
temp_dir: {tmp_dir}
keep_stats_in_res_ds: true
keep_hashes_in_res_ds: false
export_in_parallel: false
open_monitor: false
open_tracer: false
op_fusion: false

{render_process(op_names, op_np_overrides)}"""
    path.write_text(cfg, encoding="utf-8")


def parse_log(log_text):
    op_times = {
        name: float(seconds)
        for name, seconds in re.findall(r"OP \[(.*?)\] Done in ([0-9.]+)s", log_text)
    }
    total_match = re.search(r"All OPs are done in ([0-9.]+)s", log_text)
    return {
        "op_times": op_times,
        "all_ops_seconds": float(total_match.group(1)) if total_match else None,
    }


def run_one(args, np_value, repeat_idx):
    run_name = f"{args.tag}_np{np_label(np_value)}{op_np_suffix(args.op_np_overrides)}_r{repeat_idx}"
    run_dir = Path(args.output_root) / run_name
    if args.clean and run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    export_path = run_dir / "result.parquet"
    config_path = run_dir / "config.yaml"
    work_dir = run_dir / "work"
    tmp_dir = run_dir / "tmp"
    write_config(
        config_path,
        project_name=run_name,
        input_path=args.input,
        export_path=export_path,
        work_dir=work_dir,
        tmp_dir=tmp_dir,
        np_value=np_value,
        op_names=args.only_ops,
        op_np_overrides=args.op_np_overrides,
    )

    env = os.environ.copy()
    stub_path = args.stub_path or str(Path(args.source_root) / "benchmarks" / "fineweb_edu_txt1" / "stubs")
    pythonpath = os.pathsep.join([stub_path, args.source_root])
    env.update(
        {
            "PYTHONPATH": pythonpath,
            "HF_DATASETS_CACHE": "/cache/hf-datasets",
            "HF_HOME": "/cache/huggingface",
            "TOKENIZERS_PARALLELISM": "false",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )

    process_script = str(Path(args.source_root) / "tools" / "process_data.py")
    inner_cmd = [args.python, process_script, "--config", str(config_path)]
    if args.profile == "perf-stat":
        process_cmd = inner_cmd
        inner_cmd = [
            "perf",
            "stat",
            "-x,",
            "-o",
            str(run_dir / "perf_stat.csv"),
        ]
        if args.perf_events:
            inner_cmd.extend(["-e", args.perf_events])
        inner_cmd.extend(["--", *process_cmd])
    elif args.profile == "strace-summary":
        inner_cmd = [
            "strace",
            "-f",
            "-c",
            "-o",
            str(run_dir / "strace_summary.txt"),
            *inner_cmd,
        ]

    cmd = ["/usr/bin/time", "-v", "-o", str(run_dir / "time_v.txt"), *inner_cmd]
    started = time.time()
    proc = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    elapsed = time.time() - started
    (run_dir / "process.log").write_text(proc.stdout, encoding="utf-8")

    parsed = parse_log(proc.stdout)
    meta = {
        "run_name": run_name,
        "np": np_value,
        "repeat": repeat_idx,
        "profile": args.profile,
        "perf_events": args.perf_events,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "command": cmd,
        "python": args.python,
        "source_root": args.source_root,
        "input": args.input,
        "only_ops": args.only_ops,
        "op_np_overrides": args.op_np_overrides,
        **parsed,
    }
    (run_dir / "summary.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(meta, sort_keys=True))
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--python", default="/work/venvs/py311/bin/python")
    parser.add_argument("--source-root", default="/work/src/data-juicer-codex")
    parser.add_argument("--stub-path", default=None)
    parser.add_argument("--tag", default="fineweb_txt1_py311")
    parser.add_argument("--np", nargs="+", default=["1", "2", "4", "8", "16"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--profile", choices=["none", "perf-stat", "strace-summary"], default="none")
    parser.add_argument("--perf-events", default=None, help="Comma-separated perf events used with --profile perf-stat")
    parser.add_argument("--only-ops", nargs="+", choices=DEFAULT_OPS, default=DEFAULT_OPS)
    parser.add_argument("--op-np", nargs="*", default=[], help="Override a selected operator num_proc, e.g. text_chunk_mapper=4")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    args.np = [parse_np_value(value) for value in args.np]
    args.op_np_overrides = parse_op_np_overrides(args.op_np)

    for repeat_idx in range(1, args.repeat + 1):
        for np_value in args.np:
            run_one(args, np_value, repeat_idx)


if __name__ == "__main__":
    main()
