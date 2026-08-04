#!/opt/python314/bin/python3.14
"""Small GNU time subset for containers that lack /usr/bin/time."""

from __future__ import annotations

import resource
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    output: str | None = None
    command: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg in {"-v", "--verbose"}:
            idx += 1
        elif arg == "-o":
            output = argv[idx + 1]
            idx += 2
        elif arg == "--":
            command = argv[idx + 1 :]
            break
        elif arg.startswith("-"):
            idx += 1
        else:
            command = argv[idx:]
            break
    if not command:
        print("time_wrapper: missing command", file=sys.stderr)
        return 125

    started = time.perf_counter()
    proc = subprocess.run(command)
    elapsed = time.perf_counter() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    text = "\n".join(
        [
            f"Command being timed: {' '.join(command)}",
            f"User time (seconds): {usage.ru_utime:.6f}",
            f"System time (seconds): {usage.ru_stime:.6f}",
            f"Elapsed (wall clock) time (h:mm:ss or m:ss): {elapsed:.6f}s",
            f"Maximum resident set size (kbytes): {usage.ru_maxrss}",
            f"Exit status: {proc.returncode}",
            "",
        ]
    )
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stderr.write(text)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
