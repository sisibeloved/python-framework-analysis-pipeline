"""Host-persistent, dual-Conda environment plan for Volc Operator Sim."""

from __future__ import annotations

import base64
import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from ...environment.planning import PlanStep


DEFAULT_REPO = "https://gitcode.com/XuanYuL5/volc_operator_sim.git"
DEFAULT_CONTAINER = "volc-operator-sim-bench"
DEFAULT_DATA_ROOT = "/home/lxy/de_bench_full"
CONTAINER_LAYOUT_VERSION = "3"


class VolcOperatorSimEnvironmentAdapter:
    framework_id = "volcoperatorsim"

    def get_plan_steps(
        self,
        platform: str,
        platform_config: dict[str, Any],
        software: dict[str, Any],
        host_refs: dict[str, Any],
        project_dir: Path | None = None,
    ) -> list[PlanStep]:
        host_ref = _client_host(platform_config)
        host_alias = host_refs.get(host_ref, {}).get("alias", host_ref)
        host_env = host_refs.get(host_ref, {}).get("env", {})
        arch = str(platform_config.get("arch") or "x86_64")
        images = software.get("volcOperatorSimImages") or {}
        image = str(images.get(platform) or software.get("volcOperatorSimImage") or "")
        if not image:
            raise ValueError(f"missing Volc Operator Sim image for platform {platform}")

        container = str(
            software.get("volcOperatorSimContainer", DEFAULT_CONTAINER)
        )
        data_root = str(software.get("hostDataRoot", DEFAULT_DATA_ROOT))
        repo = str(software.get("volcOperatorSimRepo", DEFAULT_REPO))
        base_image = str(
            software.get(
                "volcOperatorSimBaseImage",
                "m.daocloud.io/docker.io/debian:bookworm-slim",
            )
        )
        debian_mirror = str(software.get("volcDebianMirrorHost") or "")
        miniforge_url_template = str(
            software.get("volcMiniforgeUrlTemplate")
            or (
                "https://github.com/conda-forge/miniforge/releases/download/"
                "24.11.3-2/Miniforge3-24.11.3-2-Linux-__ARCH__.sh"
            )
        )
        miniforge_sha256s = software.get("volcMiniforgeSha256s") or {}
        miniforge_sha256 = str(miniforge_sha256s.get(platform) or "")
        pytorch_cpu_index = str(
            software.get("volcPytorchCpuIndexUrl")
            or "https://download.pytorch.org/whl/cpu"
        )
        revision = str(software.get("volcOperatorSimRevision") or "")
        xarch = str(software.get("daftCondaEnv", "xarch"))
        xdj = str(software.get("dataJuicerCondaEnv", "xdj"))
        shm_size = str(software.get("shmSize", "64g"))
        cpu_set = str((software.get("volcCpuSets") or {}).get(platform) or "")
        memory_nodes = str(
            (software.get("volcMemoryNodes") or {}).get(platform) or ""
        )
        virtualization = str(
            (software.get("volcVirtualization") or {}).get(platform) or ""
        )
        nofile_soft = int(software.get("volcNofileSoft", 65536))
        nofile_hard = int(software.get("volcNofileHard", 524288))
        if nofile_soft < 4096:
            raise ValueError("software.volcNofileSoft must be at least 4096")
        if nofile_hard < nofile_soft:
            raise ValueError(
                "software.volcNofileHard must be greater than or equal to "
                "volcNofileSoft"
            )
        if bool(cpu_set) != bool(memory_nodes):
            raise ValueError(
                "software.volcCpuSets and volcMemoryNodes must both define "
                f"platform {platform}"
            )
        min_host_free_gib = max(0, int(software.get("minHostFreeGiB", 20)))
        privileged = bool(software.get("volcPrivileged", False))
        manifest_b64 = _load_manifest_b64(
            project_dir, str(software.get("dataSourceManifest") or "")
        )
        build_script = Path(__file__).parent / "scripts" / "build-volcoperatorsim-image.sh"
        build_script_sha256 = hashlib.sha256(build_script.read_bytes()).hexdigest()
        build_config_hash = hashlib.sha256(
            json.dumps(
                {
                    "repo": repo,
                    "baseImage": base_image,
                    "debianMirrorHost": debian_mirror,
                    "miniforgeUrlTemplate": miniforge_url_template,
                    "miniforgeSha256": miniforge_sha256,
                    "pytorchCpuIndexUrl": pytorch_cpu_index,
                    "revision": revision,
                    "xarch": xarch,
                    "xdj": xdj,
                    "buildScriptSha256": build_script_sha256,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]

        prepare_env = _env_assignments(
            {
                "HOST_DATA_ROOT": data_root,
                "DATA_MANIFEST_B64": manifest_b64,
                "MIN_HOST_FREE_BYTES": min_host_free_gib * 1024**3,
                **_proxy_env(host_env),
            }
        )
        build_env = _env_assignments(
            {
                "IMAGE_NAME": image,
                "VOLC_OPERATOR_SIM_REPO": repo,
                "VOLC_BASE_IMAGE": base_image,
                "VOLC_DEBIAN_MIRROR_HOST": debian_mirror,
                "VOLC_MINIFORGE_URL_TEMPLATE": miniforge_url_template,
                "VOLC_MINIFORGE_SHA256": miniforge_sha256,
                "VOLC_PYTORCH_CPU_INDEX_URL": pytorch_cpu_index,
                "VOLC_OPERATOR_SIM_REVISION": revision,
                "VOLC_BUILD_CONFIG_HASH": build_config_hash,
                "DAFT_CONDA_ENV": xarch,
                "DATAJUICER_CONDA_ENV": xdj,
                **_proxy_env(host_env),
            }
        )
        ld_preload = f"/opt/conda/envs/{xarch}/lib/libstdc++.so.6"
        config_hash = hashlib.sha256(
            json.dumps(
                {
                    "image": image,
                    "repo": repo,
                    "baseImage": base_image,
                    "debianMirrorHost": debian_mirror,
                    "miniforgeUrlTemplate": miniforge_url_template,
                    "miniforgeSha256": miniforge_sha256,
                    "pytorchCpuIndexUrl": pytorch_cpu_index,
                    "revision": revision,
                    "buildConfigHash": build_config_hash,
                    "dataRoot": data_root,
                    "xarch": xarch,
                    "xdj": xdj,
                    "ldPreload": ld_preload,
                    "condaPrefix": "/opt/conda",
                    "shm": shm_size,
                    "privileged": privileged,
                    "cpuSet": cpu_set,
                    "memoryNodes": memory_nodes,
                    "virtualization": virtualization,
                    "nofileSoft": nofile_soft,
                    "nofileHard": nofile_hard,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        run_args = _docker_run_args(
            container=container,
            image=image,
            data_root=data_root,
            shm_size=shm_size,
            config_hash=config_hash,
            ld_preload=ld_preload,
            privileged=privileged,
            cpu_set=cpu_set,
            memory_nodes=memory_nodes,
            virtualization=virtualization,
            nofile_soft=nofile_soft,
            nofile_hard=nofile_hard,
        )

        resource_readiness = _resource_readiness_script(
            cpu_set=cpu_set,
            memory_nodes=memory_nodes,
            nofile_soft=nofile_soft,
            nofile_hard=nofile_hard,
        )

        readiness_script = " && ".join(
            [
                "test -d /opt/volc_operator_sim/.git",
                (
                    "test \"$(git -C /opt/volc_operator_sim rev-parse HEAD)\" = "
                    f"{shlex.quote(revision)}"
                ),
                f"/opt/conda/envs/{shlex.quote(xarch)}/bin/python -c "
                + shlex.quote("import daft, lance, psutil; print('xarch-ready')"),
                f"/opt/conda/envs/{shlex.quote(xdj)}/bin/python -c "
                + shlex.quote(
                    "import data_juicer, psutil, selectolax, torchcodec; "
                    "print('xdj-ready')"
                ),
                f"test -f {shlex.quote(data_root)}/models/lid.176.bin",
                f"test -f {shlex.quote(data_root)}/models/en.sp.model",
                f"test -f {shlex.quote(data_root)}/models/en.arpa.bin",
                f"test -d {shlex.quote(data_root)}/fixtures",
                (
                    f"test -d {shlex.quote(data_root)}/fixtures/canonical/"
                    "laion_subset_image.lance"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "laion_subset_image.mirror.jsonl"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "laion_subset_image.mirror.meta.json"
                ),
                (
                    f"test -d {shlex.quote(data_root)}/fixtures/canonical/"
                    "librispeech_audio.lance"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "librispeech_audio.mirror.jsonl"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "librispeech_audio.mirror.meta.json"
                ),
                (
                    f"test -d {shlex.quote(data_root)}/fixtures/canonical/"
                    "msrvtt_video.lance"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "msrvtt_video.mirror.jsonl"
                ),
                (
                    f"test -f {shlex.quote(data_root)}/fixtures/canonical/"
                    "msrvtt_video.mirror.meta.json"
                ),
                f"test -d {shlex.quote(data_root)}/bench-results",
                f"/opt/conda/envs/{shlex.quote(xarch)}/bin/python -c "
                + shlex.quote(resource_readiness),
            ]
        )

        steps = [
            PlanStep(
                id="prepare-volc-host-data",
                kind="prepare",
                hostRef=host_ref,
                command=(
                    f"{prepare_env} bash /tmp/prepare-host-data.sh"
                ),
                description=f"Pre-download persistent Volc datasets/models on {host_alias}",
                mutatesHost=True,
                requiresApproval=True,
                rollbackHint=f"Preserve {data_root}; remove only manifest-owned files manually.",
                scriptPath="adapters/volcoperatorsim/scripts/prepare-host-data.sh",
                timeout=14400,
            ),
            PlanStep(
                id="build-volc-image",
                kind="build",
                hostRef=host_ref,
                command=_build_image_if_revision_changed(
                    image=image,
                    revision=revision,
                    build_config_hash=build_config_hash,
                    build_command=(
                        f"{build_env} bash /tmp/build-volcoperatorsim-image.sh "
                        f"{shlex.quote(arch)}"
                    ),
                ),
                description=f"Build Volc dual-Conda image on {host_alias}",
                mutatesHost=True,
                requiresApproval=True,
                rollbackHint=f"docker rmi {image}",
                scriptPath="adapters/volcoperatorsim/scripts/build-volcoperatorsim-image.sh",
                timeout=10800,
            ),
            PlanStep(
                id="start-volc-container",
                kind="framework-start",
                hostRef=host_ref,
                command=_reconcile_container(
                    container=container,
                    image=image,
                    config_hash=config_hash,
                    run_args=run_args,
                    privileged=privileged,
                    bootstrap_command=_raw_link_bootstrap(data_root),
                ),
                description=f"Start Volc benchmark container on {host_alias}",
                mutatesHost=True,
                requiresApproval=True,
                rollbackHint=f"docker rm -f {container}",
            ),
            PlanStep(
                id="build-volc-fixtures",
                kind="prepare",
                hostRef=host_ref,
                command=(
                    f"docker exec -e VOLC_DE_BENCH_ROOT={shlex.quote(data_root)} "
                    f"{shlex.quote(container)} bash -lc "
                    + shlex.quote(_fixture_build_script(xarch=xarch))
                ),
                description=f"Build canonical fixtures into Host mount on {host_alias}",
                mutatesHost=True,
                requiresApproval=True,
                rollbackHint=f"Preserve {data_root}/fixtures for reuse.",
                timeout=3600,
            ),
            PlanStep(
                id="readiness-volc",
                kind="framework-readiness",
                hostRef=host_ref,
                command=(
                    f"docker exec {shlex.quote(container)} bash -lc "
                    f"{shlex.quote(readiness_script)}"
                ),
                description=f"Verify revision and dual Conda environments on {host_alias}",
                timeout=300,
            ),
            PlanStep(
                id="verify-volc-perf-tools",
                kind="framework-readiness",
                hostRef=host_ref,
                command=(
                    f"docker exec {shlex.quote(container)} bash -lc "
                    + shlex.quote(
                        "command -v perf && command -v objdump && "
                        "command -v readelf && command -v py-spy"
                    )
                ),
                description=f"Verify Volc profiling tools on {host_alias}",
            ),
        ]
        if "perf" in software.get("profilingTools", []):
            steps.append(
                PlanStep(
                    id="enable-perf-paranoid",
                    kind="prepare",
                    hostRef=host_ref,
                    command="sudo sysctl -w kernel.perf_event_paranoid=0",
                    description=f"Enable perf collection on {host_alias}",
                    mutatesHost=True,
                    requiresPrivilege=True,
                    requiresApproval=True,
                    rollbackHint="sudo sysctl -w kernel.perf_event_paranoid=2",
                )
            )
        fingerprint_env = _env_assignments(
            {
                "CONTAINER_NAME": container,
                "HOST_DATA_ROOT": data_root,
                "EXPECTED_REVISION": revision,
                "EXPECTED_ARCH": arch,
                "EXPECTED_PRIVILEGED": str(privileged).lower(),
                "EXPECTED_CPUSET_CPUS": cpu_set,
                "EXPECTED_CPUSET_MEMS": memory_nodes,
                "EXPECTED_NOFILE_SOFT": nofile_soft,
                "EXPECTED_NOFILE_HARD": nofile_hard,
                "EXPECTED_VIRTUALIZATION": virtualization,
                "REQUIRE_PERF": str(
                    "perf" in software.get("profilingTools", [])
                ).lower(),
            }
        )
        steps.append(
            PlanStep(
                id="record-volc-environment",
                kind="framework-fingerprint",
                hostRef=host_ref,
                command=(
                    f"{fingerprint_env} "
                    "bash /tmp/collect-environment-fingerprint.sh"
                ),
                description=f"Record immutable Volc environment facts on {host_alias}",
                mutatesHost=True,
                scriptPath=(
                    "adapters/volcoperatorsim/scripts/"
                    "collect-environment-fingerprint.sh"
                ),
                captureOutput=True,
                timeout=300,
            )
        )
        return steps


def _load_manifest_b64(project_dir: Path | None, manifest_value: str) -> str:
    if not manifest_value:
        raise ValueError("software.dataSourceManifest is required")
    path = Path(manifest_value)
    if not path.is_absolute():
        if project_dir is None:
            raise ValueError("project_dir is required for relative dataSourceManifest")
        path = project_dir / path
    if not path.is_file():
        raise ValueError(f"data source manifest not found: {path}")
    payload = path.read_bytes()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid data source manifest {path}: {exc}") from exc
    if parsed.get("schemaVersion") != 1 or not isinstance(parsed.get("entries"), list):
        raise ValueError(f"invalid data source manifest contract: {path}")
    return base64.b64encode(payload).decode("ascii")


def _client_host(platform_config: dict[str, Any]) -> str:
    hosts = platform_config.get("hosts", [])
    by_role = {
        str(entry.get("role")): str(entry.get("hostRef"))
        for entry in hosts
        if entry.get("hostRef")
    }
    host = by_role.get("client") or next(iter(by_role.values()), "")
    if not host:
        raise ValueError("Volc platform requires a client hostRef")
    return host


def _docker_run_args(
    *,
    container: str,
    image: str,
    data_root: str,
    shm_size: str,
    config_hash: str,
    ld_preload: str,
    privileged: bool,
    cpu_set: str,
    memory_nodes: str,
    virtualization: str,
    nofile_soft: int,
    nofile_hard: int,
) -> str:
    mounts = [
        ("raw", "raw-host", "ro"),
        ("models", "models", "ro"),
        ("fixtures", "fixtures", "rw"),
        ("fixtures/raw", "raw/min_fixtures", "rw"),
        ("operator-cache", "operator-cache", "rw"),
        ("bench-results", "bench-results", "rw"),
        ("manifests", "manifests", "rw"),
    ]
    mount_flags = " ".join(
        f"-v {shlex.quote(data_root + '/' + source)}:"
        f"{shlex.quote(data_root + '/' + destination)}:{mode}"
        for source, destination, mode in mounts
    )
    security = (
        "--privileged"
        if privileged
        else "--cap-add PERFMON --cap-add SYS_PTRACE --security-opt seccomp=unconfined"
    )
    resource_flags = f"--ulimit nofile={nofile_soft}:{nofile_hard}"
    perf_lock_env: list[str] = []
    if cpu_set and memory_nodes:
        resource_flags += (
            f" --cpuset-cpus {shlex.quote(cpu_set)}"
            f" --cpuset-mems {shlex.quote(memory_nodes)}"
        )
        perf_lock_env.append(
            "-e PERF_LOCK_NUMA_POLICY="
            + shlex.quote(f"cpus={cpu_set},mems={memory_nodes}")
        )
    if virtualization:
        perf_lock_env.append(
            "-e PERF_LOCK_VIRTUALIZATION=" + shlex.quote(virtualization)
        )
    perf_lock_flags = " ".join(perf_lock_env)
    return (
        f"docker run -d --name {shlex.quote(container)} {security} "
        f"--shm-size {shlex.quote(shm_size)} "
        f"{resource_flags} "
        f"--label pyframework.volc.config={config_hash} "
        f"--label pyframework.volc.layout={CONTAINER_LAYOUT_VERSION} "
        f"-e VOLC_DE_BENCH_ROOT={shlex.quote(data_root)} "
        f"-e LD_PRELOAD={shlex.quote(ld_preload)} "
        "-e CONDA_PREFIX=/opt/conda "
        f"{perf_lock_flags} "
        f"{mount_flags} {shlex.quote(image)} sleep infinity"
    )


def _resource_readiness_script(
    *,
    cpu_set: str,
    memory_nodes: str,
    nofile_soft: int,
    nofile_hard: int,
) -> str:
    expected_cpus = sorted(_expand_id_set(cpu_set))
    _ = _expand_id_set(memory_nodes)
    return "\n".join(
        [
            "import os",
            "import resource",
            f"expected_cpus = set({expected_cpus!r})",
            "if expected_cpus:",
            "    assert os.sched_getaffinity(0) == expected_cpus, "
            "(os.sched_getaffinity(0), expected_cpus)",
            "status = {}",
            "for line in open('/proc/self/status', encoding='utf-8'):",
            "    if ':' in line:",
            "        key, value = line.split(':', 1)",
            "        status[key] = value.strip()",
            f"expected_mems = {memory_nodes!r}",
            "if expected_mems:",
            "    assert status.get('Mems_allowed_list') == expected_mems, "
            "status.get('Mems_allowed_list')",
            "soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)",
            f"assert soft == {nofile_soft}, (soft, {nofile_soft})",
            f"assert hard == {nofile_hard}, (hard, {nofile_hard})",
        ]
    )


def _expand_id_set(value: str) -> set[int]:
    result: set[int] = set()
    if not value:
        return result
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise ValueError(f"invalid empty id in set: {value!r}")
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid descending id range: {item!r}")
            result.update(range(start, end + 1))
        else:
            result.add(int(item))
    return result


def _raw_link_bootstrap(
    data_root: str, *, python: str = "/opt/conda/envs/xarch/bin/python"
) -> str:
    """Return a quote-stable bootstrap for exposing read-only Host datasets.

    The payload is encoded so that older SSH/shell combinations cannot expand
    loop variables while the command crosses multiple shell boundaries.  It
    only creates or repairs symlinks; a real entry at the destination is never
    replaced.
    """

    raw = json.dumps(f"{data_root}/raw")
    raw_host = json.dumps(f"{data_root}/raw-host")
    source = "\n".join(
        [
            "from pathlib import Path",
            f"raw = Path({raw})",
            f"raw_host = Path({raw_host})",
            "raw.mkdir(parents=True, exist_ok=True)",
            "for source in sorted(raw_host.iterdir()):",
            '    if source.name == "min_fixtures":',
            "        continue",
            "    target = raw / source.name",
            "    if target.is_symlink():",
            "        if target.resolve(strict=False) == source.resolve(strict=False):",
            "            continue",
            "        target.unlink()",
            "    if target.exists():",
            '        raise RuntimeError(f"refusing to replace raw entry: {target}")',
            "    target.symlink_to(source, target_is_directory=source.is_dir())",
        ]
    )
    payload = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return (
        f"printf %s {shlex.quote(payload)} | base64 -d | "
        f"{shlex.quote(python)}"
    )


def _build_image_if_revision_changed(
    *, image: str, revision: str, build_config_hash: str, build_command: str
) -> str:
    quoted_image = shlex.quote(image)
    quoted_revision = shlex.quote(revision)
    return (
        "image_match=$(docker images -q --filter "
        f"label=pyframework.volc.revision={quoted_revision} "
        "--filter "
        f"label=pyframework.volc.build-config={shlex.quote(build_config_hash)} "
        f"{quoted_image} 2>/dev/null || true); "
        "if [ -z \"$image_match\" ]; then "
        f"{build_command}; fi"
    )


def _fixture_build_script(*, xarch: str) -> str:
    python = f"/opt/conda/envs/{xarch}/bin/python"
    root = "$VOLC_DE_BENCH_ROOT"
    commands = [
        "cd /opt/volc_operator_sim",
        f"export PYTHON={shlex.quote(python)}",
        "export PYTHONPATH=/opt/volc_operator_sim",
        "bash scripts/data/build_min_fixtures.sh",
    ]
    media = (
        ("image", "image_manifest.jsonl", "laion_subset"),
        ("audio", "audio_manifest.jsonl", "librispeech"),
        ("video", "video_manifest.jsonl", "msrvtt"),
    )
    for kind, manifest, source_dataset in media:
        commands.append(
            f'"$PYTHON" scripts/data/build_media_lance.py '
            f"--kind {kind} "
            f'--from-manifest "{root}/fixtures/media/{manifest}" '
            f'--fixture-root "{root}" '
            f"--source-dataset {source_dataset} "
            f'--out-dir "{root}/fixtures/canonical" --verify'
        )
    commands.append(
        '"$PYTHON" scripts/validation/check_input_parity.py '
        "--require-canonical-media"
    )
    return " && ".join(commands)


def _reconcile_container(
    *,
    container: str,
    image: str,
    config_hash: str,
    run_args: str,
    privileged: bool,
    bootstrap_command: str,
) -> str:
    name = shlex.quote(container)
    expected_privileged = "true" if privileged else "false"
    return (
        f"if docker inspect {name} >/dev/null 2>&1; then "
        f"current=$(docker inspect -f '{{{{.Config.Image}}}}' {name}); "
        "cfg_match=$(docker ps -aq "
        f"--filter name=^/{container}$ "
        f"--filter label=pyframework.volc.config={config_hash} "
        f"--filter label=pyframework.volc.layout={CONTAINER_LAYOUT_VERSION}); "
        f"priv=$(docker inspect -f '{{{{.HostConfig.Privileged}}}}' {name}); "
        f"if [ \"$current\" != {shlex.quote(image)} ] || "
        "[ -z \"$cfg_match\" ] || "
        f"[ \"$priv\" != {expected_privileged} ]; then "
        f"docker rm -f {name}; fi; fi; "
        f"if docker inspect {name} >/dev/null 2>&1; then "
        f"running=$(docker inspect -f '{{{{.State.Running}}}}' {name}); "
        f"if [ \"$running\" != true ]; then docker start {name}; fi; "
        f"else {run_args}; fi; "
        f"docker exec {name} bash -lc {shlex.quote(bootstrap_command)}"
    )


def _env_assignments(values: dict[str, Any]) -> str:
    return " ".join(
        f"{name}={shlex.quote(str(value))}"
        for name, value in values.items()
        if value not in ("", None)
    )


def _proxy_env(host_env: dict[str, Any]) -> dict[str, Any]:
    names = (
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    return {name: host_env.get(name, "") for name in names}
