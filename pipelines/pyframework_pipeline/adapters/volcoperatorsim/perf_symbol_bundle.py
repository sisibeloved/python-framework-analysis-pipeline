"""Capture and resolve perf mappings that may disappear with a container.

Linux appends ``(deleted)`` to an MMAP name when the backing inode has been
unlinked.  Saving perf.data after that point is insufficient: once the process
exits there is no pathname from which perf can recover the ELF or its build-id.
This module is used as a small ``perf record`` wrapper.  It snapshots deleted
executable mappings through ``/proc/<pid>/map_files`` while the process is
alive, stores the ELF objects in a content-addressed bundle, and later resolves
any rows that the native perf symbolizer still reports as ``(deleted)``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
IDENTITY_POLICY = "exact-buildid-or-live-procfs-v2"
_MAP_LINE = re.compile(
    r"^(?P<start>[0-9a-f]+)-(?P<end>[0-9a-f]+)\s+"
    r"(?P<permissions>\S+)\s+(?P<offset>[0-9a-f]+)\s+"
    r"(?P<device>\S+)\s+(?P<inode>\d+)\s*(?P<path>.*)$"
)
_REPORT_ROW = re.compile(r"^\s*[0-9]+(?:\.[0-9]+)?%?\s*\|")
_ABSOLUTE_IP = re.compile(r"(?:---)?0x([0-9a-fA-F]{1,16})\b")
_BUILD_ID = re.compile(r"Build ID:\s*([0-9a-fA-F]+)")
_SONAME = re.compile(r"\(SONAME\).*?\[([^]]+)]")
_RAW_SAMPLE = re.compile(
    r"PERF_RECORD_SAMPLE\([^)]*\):\s+"
    r"(?P<pid>\d+)/(?P<tid>\d+):\s+"
    r"(?P<ip>0x[0-9a-fA-F]+)\s+period:\s+(?P<period>\d+)"
)
_RAW_MMAP2 = re.compile(
    r"PERF_RECORD_MMAP2\s+(?P<pid>-?\d+)/(?P<tid>\d+):\s+"
    r"\[(?P<start>0x[0-9a-fA-F]+)\((?P<length>0x[0-9a-fA-F]+)\)\s+"
    r"@\s+(?P<offset>0x[0-9a-fA-F]+|\d+)(?P<identity>[^]]*)\]:\s+"
    r"(?P<permissions>\S+)\s+(?P<path>.*)$"
)
_RAW_MMAP_FILE_ID = re.compile(
    r"(?:^|\s)(?P<device>[0-9a-fA-F]+:[0-9a-fA-F]+)\s+"
    r"(?P<inode>\d+)(?:\s+\d+)?(?:\s|$)"
)
_RAW_COMM_EXEC = re.compile(
    r"PERF_RECORD_COMM\s+exec:\s+(?P<comm>.*):(?P<pid>\d+)/(?P<tid>\d+)"
)
_RAW_FORK = re.compile(
    r"PERF_RECORD_FORK\((?P<pid>\d+):(?P<tid>\d+)\):"
    r"\((?P<ppid>\d+):(?P<ptid>\d+)\)"
)
_RAW_EXIT = re.compile(
    r"PERF_RECORD_EXIT\((?P<pid>\d+):(?P<tid>\d+)\):"
    r"\((?P<ppid>\d+):(?P<ptid>\d+)\)"
)
_RAW_EVENT_FILTER = (
    r"PERF_RECORD_(SAMPLE|MMAP2|FORK|EXIT|COMM)"
)
_RAW_SAMPLE_INDEX_CACHE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ProcMap:
    """One executable mapping observed for a process and all of its threads."""

    pid: int
    tids: tuple[int, ...]
    start: int
    end: int
    permissions: str
    offset: int
    device: str
    inode: int
    path: str
    deleted: bool


class UnresolvedDeletedMappings(RuntimeError):
    """Raised when a formal report would still contain deleted mappings."""


def parse_proc_maps(
    text: str, *, pid: int, tids: Iterable[int] = ()
) -> tuple[ProcMap, ...]:
    """Parse executable, file-backed mappings from one ``/proc/PID/maps``."""

    thread_ids = tuple(sorted(set(int(value) for value in tids))) or (pid,)
    result: list[ProcMap] = []
    for line in text.splitlines():
        match = _MAP_LINE.match(line)
        if not match or "x" not in match.group("permissions"):
            continue
        raw_path = match.group("path").strip()
        if not raw_path or raw_path.startswith("["):
            continue
        deleted = raw_path.endswith(" (deleted)")
        path = raw_path[: -len(" (deleted)")] if deleted else raw_path
        result.append(
            ProcMap(
                pid=pid,
                tids=thread_ids,
                start=int(match.group("start"), 16),
                end=int(match.group("end"), 16),
                permissions=match.group("permissions"),
                offset=int(match.group("offset"), 16),
                device=match.group("device"),
                inode=int(match.group("inode")),
                path=path,
                deleted=deleted,
            )
        )
    return tuple(result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_output(arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            list(arguments),
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def inspect_elf(path: Path) -> dict[str, Any]:
    """Return durable identity fields for a captured ELF object."""

    with path.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            raise ValueError(f"not an ELF object: {path}")
    notes = _tool_output(("readelf", "-nW", str(path)))
    dynamic = _tool_output(("readelf", "-dW", str(path)))
    build_match = _BUILD_ID.search(notes)
    soname_match = _SONAME.search(dynamic)
    digest = _sha256(path)
    build_id = build_match.group(1).lower() if build_match else ""
    return {
        "sha256": digest,
        "buildId": build_id,
        "soname": soname_match.group(1) if soname_match else "",
        "size": path.stat().st_size,
        "objectId": f"buildid:{build_id}" if build_id else f"sha256:{digest}",
    }


def _archive_stable_perf_object(
    path: str,
    *,
    expected_build_id: str,
    bundle_root: Path,
    manifest: dict[str, Any],
    comm: str = "",
) -> str:
    """Archive an MMAP2 object without guessing identity from a process name.

    A current absolute pathname is useful evidence for a non-deleted MMAP.  A
    process name (and its ``ldd`` dependencies) is only a search hint when perf
    supplied a build-id that can prove the candidate is the sampled object.
    In particular, ``/ (deleted)`` plus ``comm=python`` must never turn into the
    resolver's own Python executable.
    """

    objects = manifest.setdefault("objects", {})
    expected = expected_build_id.lower()
    existing_object_id = f"buildid:{expected}" if expected else ""
    existing_object = objects.get(existing_object_id)
    if isinstance(existing_object, Mapping) and existing_object.get("cachePath"):
        return existing_object_id
    for object_id, item in objects.items():
        if not isinstance(item, Mapping) or item.get("originalPath") != path:
            continue
        if not expected or str(item.get("buildId") or "").lower() == expected:
            if item.get("cachePath"):
                return str(object_id)
            if not existing_object_id:
                existing_object_id = str(object_id)
    candidates: list[Path] = []
    direct = Path(path)
    if direct.is_absolute() and direct.is_file():
        candidates.append(direct)
    executable = shutil.which(comm) if comm and expected else None
    if executable:
        executable_path = Path(executable)
        if executable_path not in candidates:
            candidates.append(executable_path)
        try:
            ldd = subprocess.run(
                ["ldd", str(executable_path)],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            ldd = None
        if ldd and ldd.returncode == 0:
            for match in re.finditer(r"(?:=>\s*)?(/[^\s(]+)", ldd.stdout):
                dependency = Path(match.group(1))
                if dependency.is_file() and dependency not in candidates:
                    candidates.append(dependency)
    object_root = bundle_root / "objects"
    object_root.mkdir(parents=True, exist_ok=True)
    for source in candidates:
        temporary = object_root / (
            ".perf-mmap-"
            + hashlib.sha256(str(source).encode("utf-8")).hexdigest()
            + ".partial"
        )
        try:
            identity = inspect_elf(source)
        except (OSError, ValueError):
            continue
        actual_build_id = str(identity.get("buildId") or "").lower()
        if expected and actual_build_id != expected:
            continue
        object_id = str(identity.pop("objectId"))
        target = object_root / f"{object_id.replace(':', '-')}.elf"
        try:
            if not target.exists():
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            objects[object_id] = {
                **dict(objects.get(object_id) or {}),
                **identity,
                "cachePath": f"objects/{target.name}",
                "capturedFrom": f"perf-mmap:{path}",
                "identityEvidence": (
                    "perf-build-id" if expected else "current-absolute-path"
                ),
                "identityPolicy": IDENTITY_POLICY,
                "metadataOnly": False,
                "originalPath": str(source),
            }
            captured = manifest.setdefault("lateCapturedObjects", [])
            if object_id not in captured:
                captured.append(object_id)
            return object_id
        except OSError:
            temporary.unlink(missing_ok=True)
    return existing_object_id if existing_object_id in objects else ""


def _process_ids(proc_root: Path) -> Iterable[int]:
    for entry in proc_root.iterdir():
        if entry.name.isdigit() and entry.is_dir():
            yield int(entry.name)


def _descendant_process_ids(
    proc_root: Path,
    root_pid: int,
    *,
    known_processes: Iterable[int] = (),
) -> Iterable[int]:
    """Yield one live process tree using Linux's per-task children index."""

    pending = [root_pid, *known_processes]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen or not (proc_root / str(pid)).is_dir():
            continue
        seen.add(pid)
        yield pid
        try:
            task_entries = tuple((proc_root / str(pid) / "task").iterdir())
        except OSError:
            continue
        for task in task_entries:
            if not task.name.isdigit():
                continue
            try:
                children = (task / "children").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            pending.extend(
                int(value) for value in children.split() if value.isdigit()
            )


def _thread_ids(proc_root: Path, pid: int) -> tuple[int, ...]:
    try:
        return tuple(
            sorted(
                int(entry.name)
                for entry in (proc_root / str(pid) / "task").iterdir()
                if entry.name.isdigit()
            )
        )
    except OSError:
        return (pid,)


class DeletedMappingCollector:
    """Poll procfs and archive every deleted executable ELF before it vanishes."""

    def __init__(
        self,
        *,
        cache_root: Path,
        proc_root: Path = Path("/proc"),
        root_pid: int | None = None,
    ) -> None:
        self.cache_root = cache_root
        self.proc_root = proc_root
        self.root_pid = root_pid
        self.object_root = cache_root / "objects"
        self.object_root.mkdir(parents=True, exist_ok=True)
        self.objects: dict[str, dict[str, Any]] = {}
        self.mappings: dict[tuple[int, int, int, int, int], dict[str, Any]] = {}
        self.errors: list[dict[str, Any]] = []
        self._error_keys: set[tuple[int, int, int, str]] = set()
        self.scan_count = 0
        self.processes_observed: set[int] = set()

    def scan(self) -> None:
        self.scan_count += 1
        process_ids = (
            _descendant_process_ids(
                self.proc_root,
                self.root_pid,
                known_processes=self.processes_observed,
            )
            if self.root_pid is not None
            else _process_ids(self.proc_root)
        )
        for pid in process_ids:
            process_root = self.proc_root / str(pid)
            try:
                text = (process_root / "maps").read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            self.processes_observed.add(pid)
            tids = _thread_ids(self.proc_root, pid)
            for mapping in parse_proc_maps(text, pid=pid, tids=tids):
                key = (
                    mapping.pid,
                    mapping.start,
                    mapping.end,
                    mapping.offset,
                    mapping.inode,
                )
                existing = self.mappings.get(key)
                if existing is not None:
                    existing["tids"] = sorted(
                        {
                            *(int(value) for value in existing.get("tids") or ()),
                            *mapping.tids,
                        }
                    )
                    if mapping.deleted:
                        existing["deleted"] = True
                        existing["path"] = mapping.path
                        if not existing.get("objectId"):
                            existing["objectId"] = self._capture_mapping(
                                process_root, mapping
                            )
                    continue
                object_id = ""
                if mapping.deleted:
                    object_id = self._capture_mapping(process_root, mapping)
                payload = _mapping_payload(mapping)
                payload["objectId"] = object_id
                self.mappings[key] = payload

    def _capture_mapping(self, process_root: Path, mapping: ProcMap) -> str:
        source = process_root / "map_files" / f"{mapping.start:x}-{mapping.end:x}"
        return self._capture_source(source, mapping)

    def _capture_source(self, source: Path, mapping: ProcMap) -> str:
        temporary = self.object_root / (
            f".pid-{mapping.pid}-{mapping.start:x}-{mapping.end:x}.partial"
        )
        try:
            with source.open("rb") as reader, temporary.open("wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            identity = inspect_elf(temporary)
            object_id = str(identity.pop("objectId"))
            safe_id = object_id.replace(":", "-")
            target = self.object_root / f"{safe_id}.elf"
            if target.exists():
                temporary.unlink()
            else:
                os.replace(temporary, target)
            record = {
                **identity,
                "cachePath": f"objects/{target.name}",
                "capturedFrom": str(source),
                "identityEvidence": (
                    "procfs-map-files"
                    if "map_files" in source.parts
                    else "current-absolute-path"
                ),
                "identityPolicy": IDENTITY_POLICY,
                "originalPath": mapping.path,
            }
            self.objects.setdefault(object_id, record)
            return object_id
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            error = f"{type(exc).__name__}: {exc}"
            error_key = (mapping.pid, mapping.start, mapping.end, error)
            if error_key not in self._error_keys:
                self._error_keys.add(error_key)
                self.errors.append(
                    {
                        "pid": mapping.pid,
                        "start": f"0x{mapping.start:x}",
                        "end": f"0x{mapping.end:x}",
                        "path": mapping.path,
                        "error": error,
                    }
                )
            return ""

    def finalize(self) -> None:
        """Archive stable-path mappings after the sampled processes exit."""

        for key, payload in self.mappings.items():
            if payload.get("objectId"):
                continue
            mapping = ProcMap(
                pid=int(payload["pid"]),
                tids=tuple(int(value) for value in payload.get("tids") or ()),
                start=_integer(payload["start"]),
                end=_integer(payload["end"]),
                permissions=str(payload["permissions"]),
                offset=_integer(payload["offset"]),
                device=str(payload["device"]),
                inode=int(payload["inode"]),
                path=str(payload["path"]),
                deleted=bool(payload["deleted"]),
            )
            if mapping.deleted:
                continue
            source = Path(mapping.path)
            if not source.is_file():
                continue
            object_id = self._capture_source(source, mapping)
            if object_id:
                self.mappings[key]["objectId"] = object_id

    def manifest(self, *, perf_data: Path) -> dict[str, Any]:
        mappings = list(self.mappings.values())
        deleted = [item for item in mappings if item.get("deleted")]
        captured_deleted = [item for item in deleted if item.get("objectId")]
        complete = bool(mappings) and len(captured_deleted) == len(deleted)
        return {
            "schemaVersion": SCHEMA_VERSION,
            "identityPolicy": IDENTITY_POLICY,
            "captureMode": "live-procfs-poll",
            "status": "complete" if complete else "incomplete",
            "perfData": perf_data.name,
            "bundleRoot": os.path.relpath(self.cache_root, perf_data.parent),
            "scanCount": self.scan_count,
            "processesObserved": len(self.processes_observed),
            "rootPid": self.root_pid,
            "mappingsObserved": len(mappings),
            "deletedMappingsObserved": len(deleted),
            "capturedDeletedMappings": len(captured_deleted),
            "objects": self.objects,
            "mappings": sorted(
                self.mappings.values(),
                key=lambda item: (
                    int(item["pid"]),
                    int(str(item["start"]), 16),
                ),
            ),
            "captureErrors": self.errors,
        }


def _mapping_payload(mapping: ProcMap) -> dict[str, Any]:
    payload = asdict(mapping)
    payload["tids"] = list(mapping.tids)
    for key in ("start", "end", "offset"):
        payload[key] = f"0x{int(payload[key]):x}"
    return payload


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _perf_output_path(arguments: Sequence[str]) -> Path:
    for index, value in enumerate(arguments):
        if value in {"-o", "--output"} and index + 1 < len(arguments):
            return Path(arguments[index + 1])
        if value.startswith("--output="):
            return Path(value.split("=", 1)[1])
    return Path("perf.data")


def _supported_record_options(real_perf: str) -> tuple[str, ...]:
    help_text = _tool_output((real_perf, "record", "-h"))
    requested = os.environ.get(
        "PYFRAMEWORK_PERF_RECORD_EXTRA", "--buildid-all,--buildid-mmap"
    )
    result = []
    for option in (item.strip() for item in requested.split(",")):
        if option and option in help_text:
            result.append(option)
    return tuple(result)


def _isolate_observer_affinity(
    target_cpus: Iterable[int],
    *,
    available_cpus: Iterable[int] | None = None,
) -> dict[str, Any]:
    """Move the live-mapping observer off the workload's inherited CPUs.

    ``perf`` is launched before this function is called, so the perf process
    and its descendants retain ``target_cpus``.  Only this Python observer is
    moved to a spare CPU.  Formal capture fails closed when the container has
    no usable spare CPU, because polling procfs on a workload CPU measurably
    perturbs a small pinned benchmark.
    """

    target = tuple(sorted(set(int(value) for value in target_cpus)))
    evidence: dict[str, Any] = {
        "status": "unavailable",
        "targetCpus": list(target),
        "observerCpus": [],
    }
    if not target:
        evidence["reason"] = "target-affinity-unavailable"
        return evidence
    if not hasattr(os, "sched_setaffinity") or not hasattr(
        os, "sched_getaffinity"
    ):
        evidence["reason"] = "sched-affinity-unsupported"
        return evidence

    available = (
        set(int(value) for value in available_cpus)
        if available_cpus is not None
        else set(range(int(os.cpu_count() or 0)))
    )
    evidence["availableCpus"] = sorted(available)
    candidates = sorted(available.difference(target))
    if not candidates:
        evidence["reason"] = "no-spare-cpu"
        return evidence

    failures: list[str] = []
    for cpu in candidates:
        try:
            os.sched_setaffinity(0, {cpu})
            actual = tuple(sorted(os.sched_getaffinity(0)))
        except OSError as exc:
            failures.append(f"cpu={cpu}:{type(exc).__name__}:{exc}")
            continue
        if actual == (cpu,):
            evidence["status"] = "isolated"
            evidence["observerCpus"] = [cpu]
            return evidence
        failures.append(f"cpu={cpu}:actual={','.join(map(str, actual))}")

    try:
        os.sched_setaffinity(0, set(target))
    except OSError as exc:
        failures.append(f"restore:{type(exc).__name__}:{exc}")
    evidence["reason"] = "spare-cpu-affinity-rejected"
    evidence["failures"] = failures
    return evidence


def _select_workload_affinity(
    available_cpus: Iterable[int], policy: str
) -> set[int]:
    """Return the workload CPUs declared by ``PERF_LOCK_NUMA_POLICY``."""

    available = set(int(value) for value in available_cpus)
    match = re.search(r"(?:^|,)\s*cpus=([^,]+)", policy)
    if not match:
        return available
    selected: set[int] = set()
    for item in match.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start, end = int(start_text), int(end_text)
            if end < start:
                raise ValueError(f"invalid workload CPU range: {item}")
            selected.update(range(start, end + 1))
        else:
            selected.add(int(item))
    if not selected:
        raise ValueError("PERF_LOCK_NUMA_POLICY has an empty CPU set")
    if not selected.issubset(available):
        raise ValueError(
            "workload CPU set is outside the container affinity: "
            f"selected={sorted(selected)} available={sorted(available)}"
        )
    return selected


def _populate_perf_buildid_cache(
    *, real_perf: str, cache_root: Path, objects: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    buildid_root = cache_root / "buildid"
    buildid_root.mkdir(parents=True, exist_ok=True)
    try:
        timeout_s = max(
            0.1,
            float(os.environ.get("PYFRAMEWORK_PERF_BUILDID_CACHE_TIMEOUT_S", "30")),
        )
    except ValueError:
        timeout_s = 30.0
    for object_id, item in objects.items():
        path = cache_root / str(item["cachePath"])
        try:
            result = subprocess.run(
                [
                    real_perf,
                    "--buildid-dir",
                    str(buildid_root),
                    "buildid-cache",
                    "--add",
                    str(path),
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                {
                    "objectId": object_id,
                    "failureType": "timeout",
                    "timeoutSeconds": timeout_s,
                    "error": f"perf buildid-cache timed out after {timeout_s:g}s",
                }
            )
            continue
        except OSError as exc:
            failures.append(
                {
                    "objectId": object_id,
                    "failureType": "os_error",
                    "error": str(exc),
                }
            )
            continue
        if result.returncode != 0:
            failures.append(
                {
                    "objectId": object_id,
                    "failureType": "returncode",
                    "returncode": result.returncode,
                    "error": (result.stderr or result.stdout).strip(),
                }
            )
    return failures


def record_with_symbol_bundle(
    *, real_perf: str, cache_root: Path, arguments: Sequence[str]
) -> int:
    """Run perf record while preserving deleted executable mappings."""

    record_arguments = list(arguments)
    if record_arguments and record_arguments[0] == "record":
        record_arguments.pop(0)
    output = _perf_output_path(record_arguments).resolve()
    record_options = _supported_record_options(real_perf)
    command = [
        real_perf,
        "--buildid-dir",
        str(cache_root / "buildid"),
        "record",
        *record_options,
        *record_arguments,
    ]
    try:
        available_affinity = set(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available_affinity = set()
    policy = os.environ.get("PERF_LOCK_NUMA_POLICY", "")
    workload_affinity: dict[str, Any] = {
        "status": "inherited",
        "policy": policy,
        "availableCpus": sorted(available_affinity),
        "targetCpus": sorted(available_affinity),
    }
    try:
        launch_affinity = _select_workload_affinity(
            available_affinity, policy
        )
        workload_affinity["targetCpus"] = sorted(launch_affinity)
        if launch_affinity != available_affinity:
            os.sched_setaffinity(0, launch_affinity)
            workload_affinity["status"] = "pinned"
    except (AttributeError, OSError, ValueError) as exc:
        launch_affinity = available_affinity
        workload_affinity["status"] = "invalid"
        workload_affinity["error"] = f"{type(exc).__name__}: {exc}"
    process = subprocess.Popen(command)
    observer_affinity = _isolate_observer_affinity(
        launch_affinity,
        available_cpus=available_affinity,
    )
    # Ray workers may be reparented after the driver launches the local
    # cluster.  perf continues following their fork records, so restricting
    # procfs polling to the driver's current parent/child tree can miss the
    # exact mappings perf later reports.  This helper runs inside the
    # dedicated benchmark container PID namespace; scanning that namespace
    # preserves parity with perf without touching unrelated host processes.
    collector = DeletedMappingCollector(cache_root=cache_root)
    interval = max(
        0.005, float(os.environ.get("PYFRAMEWORK_PERF_MAP_POLL_INTERVAL", "0.02"))
    )
    collector.scan()
    while process.poll() is None:
        time.sleep(interval)
        collector.scan()
    collector.scan()
    collector.finalize()
    manifest = collector.manifest(perf_data=output)
    manifest["recordCommand"] = command
    manifest["recordRootPid"] = process.pid
    manifest["scanScope"] = "container-pid-namespace"
    manifest["recordReturncode"] = int(process.returncode or 0)
    manifest["recordOptions"] = list(record_options)
    manifest["workloadAffinity"] = workload_affinity
    manifest["observerAffinity"] = observer_affinity
    manifest["buildIdCacheFailures"] = _populate_perf_buildid_cache(
        real_perf=real_perf,
        cache_root=cache_root,
        objects=collector.objects,
    )
    if manifest["buildIdCacheFailures"]:
        manifest["status"] = "incomplete"
    if observer_affinity.get("status") != "isolated":
        manifest["status"] = "incomplete"
    if workload_affinity.get("status") == "invalid":
        manifest["status"] = "incomplete"
    _atomic_json(output.with_name("perf-dso-manifest.json"), manifest)
    returncode = int(process.returncode or 0)
    if returncode == 0 and manifest["status"] != "complete":
        return 20
    return returncode


def _integer(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(str(value), 16 if str(value).lower().startswith("0x") else 10)


def _report_pid(parts: Sequence[str]) -> int | None:
    if len(parts) < 5:
        return None
    match = re.search(r"\b(\d+)\s*:", parts[4])
    if not match:
        match = re.search(r"\b(\d+)\b", parts[4])
    return int(match.group(1)) if match else None


def _mapping_for_ip(
    manifest: Mapping[str, Any], *, tid: int, ip: int
) -> Mapping[str, Any] | None:
    candidates = []
    for item in manifest.get("mappings") or []:
        if not isinstance(item, Mapping) or not item.get("objectId"):
            continue
        tids = {_integer(value) for value in item.get("tids") or ()}
        pid = _integer(item.get("pid", -1))
        if tid != pid and tid not in tids:
            continue
        start = _integer(item["start"])
        end = _integer(item["end"])
        if start <= ip < end:
            candidates.append(item)
    if not candidates:
        return None
    return min(candidates, key=lambda item: _integer(item["end"]) - _integer(item["start"]))


def _manifest_mapping_index(
    manifest: Mapping[str, Any],
) -> dict[
    int,
    tuple[
        tuple[Mapping[str, Any], ...],
        tuple[int, ...],
        tuple[int, ...],
    ],
]:
    """Index captured mappings by task and start address for point lookups.

    A context profile can contain hundreds of thousands of samples.  Falling
    back to ``_mapping_for_ip`` for each sample previously rescanned every
    mapping and rebuilt each mapping's TID set, which made symbol resolution
    O(samples * mappings).  Keep the same "smallest containing mapping"
    selection policy, but pre-group candidates and stop scanning once the
    prefix maximum end proves no earlier interval can contain the IP.
    """

    grouped: dict[int, list[Mapping[str, Any]]] = {}
    for item in manifest.get("mappings") or ():
        if not isinstance(item, Mapping) or not item.get("objectId"):
            continue
        task_ids = {_integer(item.get("pid", -1))}
        task_ids.update(_integer(value) for value in item.get("tids") or ())
        for task_id in task_ids:
            if task_id >= 0:
                grouped.setdefault(task_id, []).append(item)

    result = {}
    for task_id, candidates in grouped.items():
        ordered = tuple(
            sorted(candidates, key=lambda item: _integer(item["start"]))
        )
        starts = tuple(_integer(item["start"]) for item in ordered)
        maximum = -1
        prefix_max_end = []
        for item in ordered:
            maximum = max(maximum, _integer(item["end"]))
            prefix_max_end.append(maximum)
        result[task_id] = (ordered, starts, tuple(prefix_max_end))
    return result


def _mapping_for_ip_from_index(
    index: Mapping[
        int,
        tuple[
            tuple[Mapping[str, Any], ...],
            tuple[int, ...],
            tuple[int, ...],
        ],
    ],
    *,
    tid: int,
    ip: int,
) -> Mapping[str, Any] | None:
    entry = index.get(tid)
    if entry is None:
        return None
    mappings, starts, prefix_max_end = entry
    position = bisect_right(starts, ip) - 1
    candidates = []
    while position >= 0 and prefix_max_end[position] > ip:
        item = mappings[position]
        if ip < _integer(item["end"]):
            candidates.append(item)
        position -= 1
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda item: _integer(item["end"]) - _integer(item["start"]),
    )


def _mapping_object_for_raw_mmap(
    manifest: Mapping[str, Any],
    *,
    pid: int,
    tid: int,
    start: int,
    end: int,
    offset: int,
    device: str = "",
    inode: int | None = None,
    build_id: str = "",
) -> str:
    """Return an ELF captured from the exact live procfs mapping, if any."""

    def device_key(value: Any) -> tuple[int, int] | None:
        parts = str(value or "").split(":", 1)
        try:
            return (int(parts[0], 16), int(parts[1], 16))
        except (IndexError, ValueError):
            return None

    def object_build_id(object_id: str) -> str:
        item = (manifest.get("objects") or {}).get(object_id) or {}
        value = str(item.get("buildId") or "").lower()
        if not value and object_id.startswith("buildid:"):
            value = object_id.split(":", 1)[1].lower()
        return value

    candidates: list[Mapping[str, Any]] = []
    for item in manifest.get("mappings") or ():
        if not isinstance(item, Mapping) or not item.get("objectId"):
            continue
        mapping_pid = _integer(item.get("pid", -1))
        mapping_tids = {_integer(value) for value in item.get("tids") or ()}
        if pid != mapping_pid and tid != mapping_pid and tid not in mapping_tids:
            continue
        if (
            _integer(item.get("start", -1)) == start
            and _integer(item.get("end", -1)) == end
            and _integer(item.get("offset", 0)) == offset
        ):
            if device and inode is not None:
                if (
                    device_key(item.get("device")) != device_key(device)
                    or _integer(item.get("inode", -1)) != inode
                ):
                    continue
            elif not build_id:
                continue
            object_id = str(item["objectId"])
            if build_id and object_build_id(object_id) != build_id.lower():
                continue
            candidates.append(item)
    object_ids = {str(item["objectId"]) for item in candidates}
    return next(iter(object_ids)) if len(object_ids) == 1 else ""


def _active_mapping_for_ip(
    mappings: Sequence[Mapping[str, Any]], ip: int
) -> Mapping[str, Any] | None:
    """Find one address in sorted, non-overlapping runtime mappings."""

    position = bisect_right(
        mappings,
        ip,
        key=lambda item: _integer(item["start"]),
    ) - 1
    if position < 0:
        return None
    mapping = mappings[position]
    return mapping if ip < _integer(mapping["end"]) else None


def index_raw_perf_lines(
    lines: Iterable[str],
    manifest: Mapping[str, Any],
    *,
    object_loader: Callable[[str, str, str], str] | None = None,
) -> dict[tuple[Any, ...], dict[str, dict[str, int]]]:
    """Index samples using MMAP2 events first and procfs snapshots second."""

    if isinstance(manifest, dict):
        objects = manifest.setdefault("objects", {})
    else:
        objects = manifest.get("objects") or {}
    path_objects: dict[str, list[str]] = {}
    for object_id, item in objects.items():
        if isinstance(item, Mapping) and item.get("originalPath"):
            path_objects.setdefault(str(item["originalPath"]), []).append(
                str(object_id)
            )
    runtime_mappings: dict[int, list[dict[str, Any]]] = {}
    process_comms: dict[int, str] = {}
    index: dict[tuple[Any, ...], dict[str, dict[str, int]]] = {}
    captured_mapping_index = _manifest_mapping_index(manifest)
    captured_mapping_cache: dict[
        tuple[int, int], Mapping[str, Any] | None
    ] = {}

    def captured_mapping(tid: int, ip: int) -> Mapping[str, Any] | None:
        key = (tid, ip)
        if key not in captured_mapping_cache:
            captured_mapping_cache[key] = _mapping_for_ip_from_index(
                captured_mapping_index,
                tid=tid,
                ip=ip,
            )
        return captured_mapping_cache[key]

    for line in lines:
        fork_match = (
            _RAW_FORK.search(line)
            if "PERF_RECORD_FORK" in line
            else None
        )
        if fork_match:
            child_pid = int(fork_match.group("pid"))
            parent_pid = int(fork_match.group("ppid"))
            if parent_pid in runtime_mappings:
                runtime_mappings[child_pid] = [
                    dict(item) for item in runtime_mappings[parent_pid]
                ]
            if parent_pid in process_comms:
                process_comms[child_pid] = process_comms[parent_pid]
            continue
        exit_match = (
            _RAW_EXIT.search(line)
            if "PERF_RECORD_EXIT" in line
            else None
        )
        if exit_match:
            pid = int(exit_match.group("pid"))
            tid = int(exit_match.group("tid"))
            if pid == tid:
                runtime_mappings.pop(pid, None)
                process_comms.pop(pid, None)
            continue
        comm_match = (
            _RAW_COMM_EXEC.search(line)
            if "PERF_RECORD_COMM" in line
            else None
        )
        if comm_match:
            process_comms[int(comm_match.group("pid"))] = comm_match.group(
                "comm"
            ).strip()
            continue
        mmap_match = (
            _RAW_MMAP2.search(line)
            if "PERF_RECORD_MMAP2" in line
            else None
        )
        if mmap_match:
            pid = int(mmap_match.group("pid"))
            tid = int(mmap_match.group("tid"))
            path = mmap_match.group("path").strip()
            if pid < 0 or path.startswith("["):
                continue
            start = int(mmap_match.group("start"), 16)
            end = start + int(mmap_match.group("length"), 16)
            offset = int(mmap_match.group("offset"), 0)
            identity = mmap_match.group("identity")
            build_id_match = re.search(r"<([0-9a-fA-F]+)>", identity)
            raw_file_id = _RAW_MMAP_FILE_ID.search(identity)
            raw_device = raw_file_id.group("device") if raw_file_id else ""
            raw_inode = (
                int(raw_file_id.group("inode")) if raw_file_id else None
            )
            raw_build_id = (
                build_id_match.group(1).lower() if build_id_match else ""
            )
            deleted = path.endswith(" (deleted)")
            normalized_path = re.sub(r"\s+\(deleted\)$", "", path)
            object_id = _mapping_object_for_raw_mmap(
                manifest,
                pid=pid,
                tid=tid,
                start=start,
                end=end,
                offset=offset,
                device=raw_device,
                inode=raw_inode,
                build_id=raw_build_id,
            )
            if build_id_match:
                candidate = f"buildid:{raw_build_id}"
                if candidate in objects:
                    object_id = candidate
                    candidate_item = objects[candidate]
                    if (
                        isinstance(candidate_item, dict)
                        and candidate_item.get("identityEvidence")
                        != "procfs-map-files"
                    ):
                        candidate_item["identityEvidence"] = "perf-build-id"
                        candidate_item["identityPolicy"] = IDENTITY_POLICY
                        candidate_item["perfMmapBuildId"] = raw_build_id
            if not object_id and (not deleted or build_id_match):
                candidates = path_objects.get(normalized_path, [])
                if raw_build_id:
                    candidates = [
                        candidate
                        for candidate in candidates
                        if str(
                            (objects.get(candidate) or {}).get("buildId") or (
                                candidate.split(":", 1)[1]
                                if candidate.startswith("buildid:")
                                else ""
                            )
                        ).lower()
                        == raw_build_id
                    ]
                if len(candidates) == 1:
                    object_id = candidates[0]
            if (
                not object_id
                and object_loader is not None
                and (not deleted or build_id_match is not None)
            ):
                object_id = object_loader(
                    normalized_path,
                    raw_build_id,
                    process_comms.get(pid, ""),
                )
                if object_id:
                    path_objects.setdefault(normalized_path, []).append(object_id)
            if not object_id:
                build_id = raw_build_id
                identity_key = build_id or hashlib.sha256(
                    f"{normalized_path}\0{identity}".encode("utf-8")
                ).hexdigest()
                object_id = (
                    f"buildid:{identity_key}"
                    if build_id
                    else f"perf-mmap:{identity_key}"
                )
                if isinstance(objects, dict):
                    objects.setdefault(
                        object_id,
                        {
                            "buildId": build_id,
                            "capturedFrom": "perf-mmap2",
                            "identityEvidence": (
                                "perf-build-id"
                                if build_id
                                else "ambiguous-perf-mmap"
                            ),
                            "identityPolicy": IDENTITY_POLICY,
                            "metadataOnly": True,
                            "originalPath": normalized_path,
                        },
                    )
                path_objects.setdefault(normalized_path, []).append(object_id)
            if not object_id:
                continue
            mapping = {
                "start": start,
                "end": end,
                "offset": offset,
                "objectId": object_id,
            }
            active = runtime_mappings.setdefault(pid, [])
            active[:] = [
                current
                for current in active
                if current["end"] <= start or current["start"] >= end
            ]
            active.append(mapping)
            active.sort(key=lambda item: item["start"])
            continue
        sample_match = (
            _RAW_SAMPLE.search(line)
            if "PERF_RECORD_SAMPLE" in line
            else None
        )
        if not sample_match:
            continue
        pid = int(sample_match.group("pid"))
        tid = int(sample_match.group("tid"))
        ip = int(sample_match.group("ip"), 16)
        mapping: Mapping[str, Any] | None = _active_mapping_for_ip(
            runtime_mappings.get(pid, ()), ip
        )
        if mapping is None:
            mapping = captured_mapping(tid, ip)
        if mapping is None:
            mapping = captured_mapping(pid, ip)
        if mapping is None:
            continue
        relative_ip = ip - _integer(mapping["start"]) + _integer(
            mapping.get("offset", 0)
        )
        object_id = str(mapping["objectId"])
        totals = index.setdefault((tid, relative_ip), {}).setdefault(
            object_id, {"period": 0, "sampleCount": 0}
        )
        totals["period"] += int(sample_match.group("period"))
        totals["sampleCount"] += 1
        absolute_totals = index.setdefault(
            ("absolute", tid, ip), {}
        ).setdefault(
            object_id,
            {"period": 0, "sampleCount": 0, "relativeIp": relative_ip},
        )
        absolute_totals["period"] += int(sample_match.group("period"))
        absolute_totals["sampleCount"] += 1
    return index


def build_raw_sample_index(
    *,
    perf_data: Path,
    manifest: Mapping[str, Any],
    real_perf: str = "/usr/bin/perf",
    buildid_dir: Path | None = None,
    object_loader: Callable[[str, str, str], str] | None = None,
) -> dict[tuple[Any, ...], dict[str, dict[str, int]]]:
    """Map ``(tid, DSO-relative IP)`` to exact captured ELF sample totals.

    ``perf report`` prints only a DSO-relative address for unresolved rows.
    Raw PERF_RECORD_SAMPLE entries retain the absolute IP, so correlating them
    with the procfs mapping manifest removes ambiguity between libraries whose
    relative virtual-address ranges overlap.
    """

    command = [real_perf]
    if buildid_dir is not None:
        command.extend(("--buildid-dir", str(buildid_dir)))
    command.extend(("script", "-D", "--max-stack", "0", "-i", str(perf_data)))
    perf_process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert perf_process.stdout is not None
    filter_process = subprocess.Popen(
        ("grep", "-E", _RAW_EVENT_FILTER),
        stdin=perf_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "LC_ALL": "C"},
    )
    perf_process.stdout.close()
    assert filter_process.stdout is not None
    try:
        index = index_raw_perf_lines(
            filter_process.stdout,
            manifest,
            object_loader=object_loader,
        )
    except BaseException:
        filter_process.terminate()
        perf_process.terminate()
        filter_process.wait()
        perf_process.wait()
        raise
    finally:
        filter_process.stdout.close()
    filter_returncode = filter_process.wait()
    perf_returncode = perf_process.wait()
    if perf_returncode != 0 or filter_returncode not in {0, 1}:
        raise RuntimeError(
            "perf script raw sample extraction failed: "
            f"perf={perf_returncode} filter={filter_returncode}"
        )
    return index


def _raw_sample_index_cache_identity(perf_data: Path) -> dict[str, Any]:
    stat = perf_data.stat()
    return {
        "path": str(perf_data.resolve()),
        "size": stat.st_size,
        "mtimeNs": stat.st_mtime_ns,
    }


def _load_raw_sample_index_cache(
    path: Path,
    *,
    perf_data: Path,
) -> dict[tuple[Any, ...], dict[str, dict[str, int]]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        payload.get("schemaVersion")
        != _RAW_SAMPLE_INDEX_CACHE_SCHEMA_VERSION
        or payload.get("perfData") != _raw_sample_index_cache_identity(perf_data)
        or not isinstance(payload.get("entries"), list)
    ):
        return None
    result: dict[tuple[Any, ...], dict[str, dict[str, int]]] = {}
    try:
        for item in payload["entries"]:
            key = tuple(item["key"])
            candidates = {
                str(object_id): {
                    str(name): int(value)
                    for name, value in totals.items()
                }
                for object_id, totals in item["candidates"].items()
            }
            result[key] = candidates
    except (KeyError, TypeError, ValueError):
        return None
    return result


def _write_raw_sample_index_cache(
    path: Path,
    *,
    perf_data: Path,
    index: Mapping[tuple[Any, ...], Mapping[str, Mapping[str, int]]],
) -> None:
    payload = {
        "schemaVersion": _RAW_SAMPLE_INDEX_CACHE_SCHEMA_VERSION,
        "perfData": _raw_sample_index_cache_identity(perf_data),
        "entries": [
            {"key": list(key), "candidates": candidates}
            for key, candidates in index.items()
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".partial")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _default_symbolizer(path: Path, address: int) -> tuple[str, str]:
    result = subprocess.run(
        ["addr2line", "-f", "-C", "-e", str(path), f"0x{address:x}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    lines = [line.strip() for line in result.stdout.splitlines()]
    symbol = lines[0] if result.returncode == 0 and lines else ""
    source = lines[1] if len(lines) > 1 else ""
    if not symbol or symbol == "??":
        symbol = f"0x{address:x}"
    return symbol, source


def _batch_symbolize(
    path: Path, addresses: Iterable[int]
) -> dict[int, tuple[str, str]]:
    """Resolve every address for one ELF through a single addr2line process."""

    ordered = list(dict.fromkeys(int(address) for address in addresses))
    if not ordered:
        return {}
    try:
        result = subprocess.run(
            ["addr2line", "-f", "-C", "-e", str(path)],
            input="".join(f"0x{address:x}\n" for address in ordered),
            text=True,
            capture_output=True,
            check=False,
            timeout=max(60, min(600, len(ordered) // 100)),
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    lines = result.stdout.splitlines() if result and result.returncode == 0 else []
    resolved: dict[int, tuple[str, str]] = {}
    for index, address in enumerate(ordered):
        symbol_index = index * 2
        symbol = lines[symbol_index].strip() if symbol_index < len(lines) else ""
        source_index = symbol_index + 1
        source = lines[source_index].strip() if source_index < len(lines) else ""
        if not symbol or symbol == "??":
            symbol = f"0x{address:x}"
        resolved[address] = (symbol, source)
    return resolved


def _batch_symbol_names(
    path: Path, addresses: Iterable[int]
) -> dict[int, frozenset[str]]:
    """Return every ELF symbol alias whose address range contains a sample."""

    ordered = sorted(set(int(address) for address in addresses))
    if not ordered:
        return {}
    results = []
    for dynamic in (False, True):
        arguments = ["nm"]
        if dynamic:
            arguments.append("-D")
        arguments.extend(("-n", "-S", "--defined-only", "-C", str(path)))
        try:
            results.append(
                subprocess.run(
                    arguments,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=180,
                )
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
    groups: dict[int, dict[str, Any]] = {}
    for result in results:
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            fields = line.split(None, 3)
            if len(fields) != 4:
                continue
            try:
                start = int(fields[0], 16)
                size = int(fields[1], 16)
            except ValueError:
                continue
            group = groups.setdefault(start, {"size": 0, "names": set()})
            group["size"] = max(int(group["size"]), size)
            group["names"].add(fields[3].split("@", 1)[0])
    try:
        plt_result = subprocess.run(
            [
                "objdump",
                "-d",
                "-C",
                "-j",
                ".plt",
                "-j",
                ".plt.got",
                "-j",
                ".plt.sec",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        plt_result = None
    if plt_result is not None:
        for line in plt_result.stdout.splitlines():
            match = re.match(r"^\s*([0-9a-fA-F]+)\s+<(.+)>:$", line)
            if not match:
                continue
            start = int(match.group(1), 16)
            group = groups.setdefault(start, {"size": 0, "names": set()})
            group["size"] = max(int(group["size"]), 16)
            group["names"].add(match.group(2).split("@", 1)[0])
    starts = sorted(groups)
    resolved: dict[int, frozenset[str]] = {}
    for address in ordered:
        position = bisect_right(starts, address) - 1
        if position < 0:
            resolved[address] = frozenset()
            continue
        start = starts[position]
        group = groups[start]
        size = int(group["size"])
        next_start = starts[position + 1] if position + 1 < len(starts) else None
        in_range = address < start + size if size else (
            next_start is None or address < next_start
        )
        resolved[address] = (
            frozenset(group["names"]) if in_range else frozenset()
        )
    return resolved


def _defined_symbols(path: Path) -> frozenset[str]:
    """Read the full ELF symbol table once for known-symbol DSO recovery."""

    names: set[str] = set()
    for dynamic in (False, True):
        arguments = ["nm"]
        if dynamic:
            arguments.append("-D")
        arguments.extend(("--defined-only", "-C", str(path)))
        try:
            result = subprocess.run(
                arguments,
                text=True,
                capture_output=True,
                check=False,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3:
                names.add(fields[2].split("@", 1)[0])
    return frozenset(names)


def _symbol_lookup_keys(name: str) -> frozenset[str]:
    """Canonical keys shared by perf, nm, and addr2line demangling styles."""

    value = name.strip().split("@", 1)[0]
    angle = brace = 0
    index = 0
    while index < len(value):
        character = value[index]
        if character == "<":
            angle += 1
        elif character == ">" and angle:
            angle -= 1
        elif character == "{":
            brace += 1
        elif character == "}" and brace:
            brace -= 1
        elif character == "(" and angle == 0 and brace == 0:
            depth = 1
            closing = index + 1
            while closing < len(value) and depth:
                if value[closing] == "(":
                    depth += 1
                elif value[closing] == ")":
                    depth -= 1
                closing += 1
            before = value[:index].rstrip()
            after = value[closing:].lstrip()
            if after.startswith("::") or before.endswith("operator"):
                index = closing
                continue
            value = before
            break
        index += 1
    keys = {value}
    angle = paren = brace = 0
    for index, character in enumerate(value):
        if character == "<":
            angle += 1
        elif character == ">" and angle:
            angle -= 1
        elif character == "(":
            paren += 1
        elif character == ")" and paren:
            paren -= 1
        elif character == "{":
            brace += 1
        elif character == "}" and brace:
            brace -= 1
        elif character.isspace() and angle == paren == brace == 0:
            suffix = value[index + 1 :].strip()
            if suffix and (
                "::" in suffix or " " not in suffix or suffix.startswith("operator ")
            ):
                keys.add(suffix)
    return frozenset(key for key in keys if key)


def _symbol_name_matches(left: str, right: str) -> bool:
    """Match perf's signature-free names with nm/addr2line demangled names."""

    return bool(_symbol_lookup_keys(left) & _symbol_lookup_keys(right))


def resolve_period_report(
    text: str,
    manifest: Mapping[str, Any],
    *,
    bundle_root: Path,
    symbolizer: Callable[[Path, int], tuple[str, str]] = _default_symbolizer,
    raw_sample_index: Mapping[
        tuple[Any, ...], Mapping[str, Mapping[str, int]]
    ] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve deleted rows in a perf period report using captured mappings."""

    lines = text.splitlines(keepends=True)
    deleted_before = 0
    resolved_count = 0
    unresolved_count = 0
    ambiguous_count = 0
    details: list[dict[str, Any]] = []
    objects = manifest.get("objects") or {}
    object_ids_by_display: dict[str, set[str]] = {}
    for object_id, item in objects.items():
        if not isinstance(item, Mapping):
            continue
        original = str(item.get("originalPath") or "")
        for display in (
            str(item.get("soname") or ""),
            Path(original).name if original not in {"", "/"} else "",
        ):
            if display:
                object_ids_by_display.setdefault(display, set()).add(str(object_id))
    reported_objects_by_totals: dict[tuple[int, int, int], set[str]] = {}
    for report_line in lines:
        if not _REPORT_ROW.match(report_line):
            continue
        report_parts = report_line.rstrip("\n").split("|")
        if len(report_parts) < 7 or report_parts[5].strip() == "(deleted)":
            continue
        report_tid = _report_pid(report_parts)
        if report_tid is None:
            continue
        try:
            report_period = int(float(report_parts[1].replace(",", "")))
            report_samples = int(float(report_parts[2].replace(",", "")))
        except ValueError:
            continue
        display = Path(report_parts[5].strip()).name
        reported_objects_by_totals.setdefault(
            (report_tid, report_period, report_samples), set()
        ).update(object_ids_by_display.get(display, set()))
    batched_symbol_cache: dict[tuple[Path, int], tuple[str, str]] = {}
    batch_loaded_paths: set[Path] = set()
    batched_name_cache: dict[tuple[Path, int], frozenset[str]] = {}
    name_loaded_paths: set[Path] = set()
    samples_by_tid: dict[
        int, list[tuple[int, str, Mapping[str, int]]]
    ] = {}
    exact_totals_by_tid: dict[
        int, dict[tuple[int, int], list[tuple[int, str]]]
    ] = {}
    addresses_by_object: dict[str, set[int]] = {}
    if raw_sample_index is not None:
        for sample_key, candidates in raw_sample_index.items():
            if len(sample_key) != 2:
                continue
            sample_tid, sample_ip = sample_key
            for candidate, totals in candidates.items():
                samples_by_tid.setdefault(sample_tid, []).append(
                    (sample_ip, candidate, totals)
                )
                exact_totals_by_tid.setdefault(sample_tid, {}).setdefault(
                    (int(totals["period"]), int(totals["sampleCount"])), []
                ).append((sample_ip, candidate))
                addresses_by_object.setdefault(candidate, set()).add(sample_ip)
    symbol_evidence_by_tid: dict[
        int, dict[str, dict[str, dict[str, int]]]
    ] = {}

    def cached_symbolize(
        candidate: str, candidate_path: Path, address: int
    ) -> tuple[str, str]:
        if symbolizer is not _default_symbolizer:
            return symbolizer(candidate_path, address)
        if candidate_path not in batch_loaded_paths:
            candidate_addresses = set(addresses_by_object.get(candidate, set()))
            candidate_addresses.add(address)
            for item_address, resolved in _batch_symbolize(
                candidate_path, candidate_addresses
            ).items():
                batched_symbol_cache[(candidate_path, item_address)] = resolved
            batch_loaded_paths.add(candidate_path)
        return batched_symbol_cache.get(
            (candidate_path, address), (f"0x{address:x}", "")
        )

    def symbol_evidence(tid: int) -> dict[str, dict[str, dict[str, int]]]:
        cached = symbol_evidence_by_tid.get(tid)
        if cached is not None:
            return cached
        evidence: dict[str, dict[str, dict[str, int]]] = {}
        for sample_ip, candidate, totals in samples_by_tid.get(tid, ()):
            candidate_item = objects.get(candidate)
            if not isinstance(candidate_item, Mapping):
                continue
            candidate_path = bundle_root / str(candidate_item["cachePath"])
            candidate_addresses = addresses_by_object.get(candidate, set())
            names: set[str] = set()
            if symbolizer is _default_symbolizer:
                if candidate_path not in name_loaded_paths:
                    for address, aliases in _batch_symbol_names(
                        candidate_path, candidate_addresses
                    ).items():
                        batched_name_cache[(candidate_path, address)] = aliases
                    name_loaded_paths.add(candidate_path)
                names.update(
                    batched_name_cache.get(
                        (candidate_path, sample_ip), frozenset()
                    )
                )
                if not names:
                    name = cached_symbolize(
                        candidate, candidate_path, sample_ip
                    )[0]
                    names.add(name)
            else:
                names.add(symbolizer(candidate_path, sample_ip)[0])
            sample_keys = {
                key for name in names for key in _symbol_lookup_keys(name)
            }
            for key in sample_keys:
                total = evidence.setdefault(key, {}).setdefault(
                    candidate,
                    {
                        "period": 0,
                        "sampleCount": 0,
                        "relativeIp": sample_ip,
                    },
                )
                total["period"] += int(totals["period"])
                total["sampleCount"] += int(totals["sampleCount"])
        symbol_evidence_by_tid[tid] = evidence
        return evidence

    index = 0
    while index < len(lines):
        line = lines[index]
        if not _REPORT_ROW.match(line):
            index += 1
            continue
        parts = line.rstrip("\n").split("|")
        if len(parts) < 7 or parts[5].strip() != "(deleted)":
            index += 1
            continue
        deleted_before += 1
        next_index = index + 1
        absolute_ip = None
        if len(parts) > 7:
            explicit_address = _ABSOLUTE_IP.search(parts[7])
            if explicit_address:
                absolute_ip = int(explicit_address.group(1), 16)
        if absolute_ip is None:
            while (
                next_index < len(lines)
                and not _REPORT_ROW.match(lines[next_index])
            ):
                match = _ABSOLUTE_IP.search(lines[next_index])
                if match:
                    absolute_ip = int(match.group(1), 16)
                    break
                next_index += 1
        tid = _report_pid(parts)
        mapping = (
            _mapping_for_ip(manifest, tid=tid, ip=absolute_ip)
            if tid is not None and absolute_ip is not None
            else None
        )
        relative_ip: int | None = None
        resolved_symbol_override = ""
        mapping_source = "procfs-captured" if mapping else ""
        object_id = str(mapping.get("objectId")) if mapping else ""
        if mapping is not None and absolute_ip is not None:
            relative_ip = (
                int(absolute_ip)
                - _integer(mapping["start"])
                + _integer(mapping.get("offset", 0))
            )
        if (
            tid is not None
            and absolute_ip is not None
            and raw_sample_index is not None
        ):
            absolute_candidates = raw_sample_index.get(
                ("absolute", tid, absolute_ip), {}
            )
            try:
                row_period = int(float(parts[1].replace(",", "")))
                row_samples = int(float(parts[2].replace(",", "")))
            except ValueError:
                row_period = row_samples = -1
            exact_absolute = [
                (candidate, totals)
                for candidate, totals in absolute_candidates.items()
                if int(totals["period"]) == row_period
                and int(totals["sampleCount"]) == row_samples
            ]
            selected_absolute = (
                exact_absolute[0]
                if len(exact_absolute) == 1
                else next(iter(absolute_candidates.items()))
                if len(absolute_candidates) == 1
                else None
            )
            if selected_absolute is not None:
                # PERF_RECORD_MMAP2 is ordered with the sample stream and is
                # therefore stronger evidence than a procfs snapshot that may
                # contain a historical mapping at the same virtual address.
                object_id = selected_absolute[0]
                relative_ip = int(selected_absolute[1]["relativeIp"])
                target_symbol = re.sub(r"^\[[^]]+]\s*", "", parts[6].strip())
                if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", target_symbol):
                    resolved_symbol_override = target_symbol
                mapping_source = "perf-mmap2"
        if not object_id and tid is not None and raw_sample_index is not None:
            relative_match = re.search(r"0x([0-9a-fA-F]+)", parts[6])
            if relative_match:
                relative_ip = int(relative_match.group(1), 16)
                candidates = raw_sample_index.get((tid, relative_ip), {})
                if candidates:
                    try:
                        row_period = int(float(parts[1].replace(",", "")))
                        row_samples = int(float(parts[2].replace(",", "")))
                    except ValueError:
                        row_period = row_samples = 0
                    exact_candidates = [
                        candidate
                        for candidate, totals in candidates.items()
                        if int(totals["period"]) == row_period
                        and int(totals["sampleCount"]) == row_samples
                    ]
                    if len(exact_candidates) == 1:
                        object_id = exact_candidates[0]
                        mapping_source = "perf-raw-relative"
                    elif len(candidates) == 1:
                        object_id = next(iter(candidates))
                        mapping_source = "perf-raw-relative"
            if not object_id:
                try:
                    row_period = int(float(parts[1].replace(",", "")))
                    row_samples = int(float(parts[2].replace(",", "")))
                except ValueError:
                    row_period = row_samples = -1
                target_symbol = re.sub(
                    r"^\[[^]]+]\s*", "", parts[6].strip()
                )
                exact_samples = exact_totals_by_tid.get(tid, {}).get(
                    (row_period, row_samples), []
                )
                occupied = reported_objects_by_totals.get(
                    (tid, row_period, row_samples), set()
                )
                available_samples = [
                    sample
                    for sample in exact_samples
                    if sample[1] not in occupied
                ]
                if len(available_samples) == 1:
                    relative_ip, object_id = available_samples[0]
                    resolved_symbol_override = target_symbol
                    mapping_source = "perf-raw-totals"
                candidates: Mapping[str, dict[str, int]] = {}
                if not object_id:
                    evidence = symbol_evidence(tid)
                    for key in sorted(
                        _symbol_lookup_keys(target_symbol), key=len, reverse=True
                    ):
                        if evidence.get(key):
                            candidates = evidence[key]
                            break
                    exact = [
                        (candidate, totals)
                        for candidate, totals in candidates.items()
                        if totals["period"] == row_period
                        and totals["sampleCount"] == row_samples
                    ]
                    selected: tuple[str, Mapping[str, int]] | None = None
                    if len(exact) == 1:
                        selected = exact[0]
                    elif len(candidates) == 1:
                        selected = next(iter(candidates.items()))
                    if selected is not None:
                        object_id = selected[0]
                        relative_ip = int(selected[1]["relativeIp"])
                        resolved_symbol_override = target_symbol
                        mapping_source = "perf-symbol-evidence"
        item = objects.get(object_id) if object_id else None
        if not isinstance(item, Mapping):
            unresolved_count += 1
            details.append(
                {
                    "status": "unresolved",
                    "tid": tid,
                    "ip": f"0x{absolute_ip:x}" if absolute_ip is not None else "",
                    "reason": "captured_mapping_not_found",
                }
            )
            index += 1
            continue
        identity_evidence = str(item.get("identityEvidence") or "")
        trusted_identity = identity_evidence in {
            "perf-build-id",
            "procfs-map-files",
        }
        if not trusted_identity:
            unresolved_count += 1
            ambiguous_count += 1
            details.append(
                {
                    "status": "unresolved",
                    "tid": tid,
                    "ip": f"0x{absolute_ip:x}" if absolute_ip is not None else "",
                    "objectId": object_id,
                    "identityEvidence": identity_evidence or "missing",
                    "reason": "ambiguous_deleted_mapping_identity",
                }
            )
            index += 1
            continue
        assert relative_ip is not None
        metadata_only = bool(item.get("metadataOnly")) or not item.get("cachePath")
        if metadata_only and not resolved_symbol_override:
            unresolved_count += 1
            details.append(
                {
                    "status": "unresolved",
                    "tid": tid,
                    "ip": f"0x{absolute_ip:x}" if absolute_ip is not None else "",
                    "objectId": object_id,
                    "reason": "captured_elf_not_available",
                }
            )
            index += 1
            continue
        object_path = (
            bundle_root / str(item["cachePath"])
            if item.get("cachePath")
            else bundle_root
        )
        if resolved_symbol_override:
            symbol, source = resolved_symbol_override, ""
        else:
            symbol, source = cached_symbolize(
                object_id, object_path, relative_ip
            )
        display_name = str(item.get("soname") or "")
        if not display_name:
            original = str(item.get("originalPath") or "")
            display_name = Path(original).name if original not in {"", "/"} else ""
        if not display_name:
            identity = str(item.get("buildId") or item.get("sha256") or "unknown")
            display_name = f"captured-elf-{identity[:12]}"
        if metadata_only:
            mapping_source = "perf-mmap2-metadata"
        parts[5] = f" {display_name:<48}"
        marker = "[.] " if "[.]" in parts[6] else ""
        parts[6] = f"{marker}{symbol}"
        lines[index] = "|".join(parts) + ("\n" if line.endswith("\n") else "")
        resolved_count += 1
        details.append(
            {
                "status": "resolved",
                "tid": tid,
                "ip": f"0x{absolute_ip:x}" if absolute_ip is not None else "",
                "relativeIp": f"0x{relative_ip:x}",
                "objectId": object_id,
                "sharedObject": display_name,
                "symbol": symbol,
                "source": source,
                "mappingSource": mapping_source,
                "identityEvidence": (
                    identity_evidence
                    or ("perf-build-id" if object_id.startswith("buildid:") else "")
                ),
            }
        )
        index += 1
    summary = {
        "schemaVersion": SCHEMA_VERSION,
        "identityPolicy": IDENTITY_POLICY,
        "status": "complete" if unresolved_count == 0 else "incomplete",
        "deletedRowsBefore": deleted_before,
        "resolvedDeletedRows": resolved_count,
        "unresolvedDeletedRows": unresolved_count,
        "ambiguousDeletedRows": ambiguous_count,
        "resolutions": details,
    }
    return "".join(lines), summary


def write_resolved_period_report(
    *,
    source: Path,
    manifest_path: Path,
    output: Path,
    require_complete: bool = False,
    perf_data: Path | None = None,
    real_perf: str = "/usr/bin/perf",
    buildid_dir: Path | None = None,
    raw_index_cache: Path | None = None,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bundle_root = manifest_path.parent / str(manifest.get("bundleRoot") or ".")
    source_text = source.read_text(encoding="utf-8", errors="replace")
    raw_sample_index = None
    if "(deleted)" in source_text and perf_data is not None:
        prior_object_ids = set(manifest.get("objects") or {})
        prior_objects_json = json.dumps(
            manifest.get("objects") or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        if raw_index_cache is not None:
            raw_sample_index = _load_raw_sample_index_cache(
                raw_index_cache,
                perf_data=perf_data,
            )
        if raw_sample_index is None:
            raw_sample_index = build_raw_sample_index(
                perf_data=perf_data,
                manifest=manifest,
                real_perf=real_perf,
                buildid_dir=buildid_dir,
                object_loader=lambda path, build_id, comm: _archive_stable_perf_object(
                    path,
                    expected_build_id=build_id,
                    bundle_root=bundle_root,
                    manifest=manifest,
                    comm=comm,
                ),
            )
            if raw_index_cache is not None:
                _write_raw_sample_index_cache(
                    raw_index_cache,
                    perf_data=perf_data,
                    index=raw_sample_index,
                )
        new_objects = {
            object_id: item
            for object_id, item in (manifest.get("objects") or {}).items()
            if object_id not in prior_object_ids and item.get("cachePath")
        }
        manifest_changed = (
            json.dumps(
                manifest.get("objects") or {},
                sort_keys=True,
                separators=(",", ":"),
            )
            != prior_objects_json
        )
        if new_objects:
            failures = _populate_perf_buildid_cache(
                real_perf=real_perf,
                cache_root=bundle_root,
                objects=new_objects,
            )
            manifest.setdefault("buildIdCacheFailures", []).extend(failures)
            if failures:
                manifest["status"] = "incomplete"
        if manifest_changed:
            manifest["identityPolicy"] = IDENTITY_POLICY
            _atomic_json(manifest_path, manifest)
    resolved, summary = resolve_period_report(
        source_text,
        manifest,
        bundle_root=bundle_root,
        raw_sample_index=raw_sample_index,
    )
    manifest_status = str(manifest.get("status") or "unknown")
    manifest_policy = str(manifest.get("identityPolicy") or "")
    manifest_complete = (
        manifest_status == "complete" and manifest_policy == IDENTITY_POLICY
    )
    if not manifest_complete:
        summary["status"] = "incomplete"
    summary.update(
        {
            "mappingManifestStatus": manifest_status,
            "mappingManifestIdentityPolicy": manifest_policy,
            "mappingManifestComplete": manifest_complete,
        }
    )
    temporary = output.with_name("." + output.name + ".partial")
    temporary.write_text(resolved, encoding="utf-8")
    os.replace(temporary, output)
    summary.update(
        {
            "sourceReport": source.name,
            "resolvedReport": output.name,
            "mappingManifest": manifest_path.name,
        }
    )
    _atomic_json(source.parent / "perf-symbol-resolution.json", summary)
    if require_complete and summary["status"] != "complete":
        raise UnresolvedDeletedMappings(
            "perf symbol resolution is incomplete: "
            f"unresolved_deleted_rows={summary['unresolvedDeletedRows']}, "
            f"mapping_manifest_status={manifest_status}, "
            f"mapping_manifest_policy={manifest_policy or 'missing'}"
        )
    return summary


def _record_cli(args: argparse.Namespace) -> int:
    return record_with_symbol_bundle(
        real_perf=args.real_perf,
        cache_root=args.cache_root,
        arguments=args.arguments,
    )


def _resolve_cli(args: argparse.Namespace) -> int:
    try:
        write_resolved_period_report(
            source=args.source,
            manifest_path=args.manifest,
            output=args.output,
            require_complete=args.require_complete,
            perf_data=args.perf_data,
            real_perf=args.real_perf,
            buildid_dir=args.buildid_dir,
            raw_index_cache=args.raw_index_cache,
        )
    except UnresolvedDeletedMappings as exc:
        print(f"PYFRAMEWORK_PERF_SYMBOL_RESOLUTION=incomplete error={exc}", file=sys.stderr)
        return 18
    print(f"PYFRAMEWORK_PERF_SYMBOL_RESOLUTION=complete output={args.output}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--real-perf", default="/usr/bin/perf")
    record.add_argument("--cache-root", type=Path, required=True)
    record.add_argument("arguments", nargs=argparse.REMAINDER)
    record.set_defaults(handler=_record_cli)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--source", type=Path, required=True)
    resolve.add_argument("--manifest", type=Path, required=True)
    resolve.add_argument("--output", type=Path, required=True)
    resolve.add_argument("--require-complete", action="store_true")
    resolve.add_argument("--perf-data", type=Path)
    resolve.add_argument("--real-perf", default="/usr/bin/perf")
    resolve.add_argument("--buildid-dir", type=Path)
    resolve.add_argument("--raw-index-cache", type=Path)
    resolve.set_defaults(handler=_resolve_cli)
    args = parser.parse_args(argv)
    if args.command == "record" and args.arguments[:1] == ["--"]:
        args.arguments = args.arguments[1:]
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
