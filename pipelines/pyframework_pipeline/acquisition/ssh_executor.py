"""Optional SSH executor for remote data acquisition.

Provides SSH-based remote command execution for collecting data from
remote clusters when data files are not already present locally.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCP_TIMEOUT_SECONDS = 300
_SFTP_TIMEOUT_SECONDS = 30
_SERVER_ALIVE_COUNT_MAX = 4
_CONTROL_PATH = f"/tmp/pyframework-ssh-{getattr(os, 'getuid', lambda: 'user')()}-%C"


class SshExecutor:
    """Execute commands on a remote host via SSH."""

    def __init__(
        self,
        host: str,
        user: str = "",
        key: Path | None = None,
        port: int = 22,
        env: dict[str, str] | None = None,
        ssh_config: Path | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.key = key
        self.port = port
        self.env: dict[str, str] = env or {}
        self.ssh_config = ssh_config or _default_ssh_config()

    def _ssh_options(self) -> list[str]:
        args: list[str] = []
        if self.ssh_config:
            args.extend(["-F", str(self.ssh_config)])
        if self.port != 22:
            args.extend(["-p", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend([
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15",
            "-o", f"ServerAliveCountMax={_SERVER_ALIVE_COUNT_MAX}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=600",
            "-o", f"ControlPath={_CONTROL_PATH}",
        ])
        return args

    def _scp_options(self) -> list[str]:
        args: list[str] = []
        if self.ssh_config:
            args.extend(["-F", str(self.ssh_config)])
        if self.port != 22:
            args.extend(["-P", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend([
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=15",
            "-o", f"ServerAliveCountMax={_SERVER_ALIVE_COUNT_MAX}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=600",
            "-o", f"ControlPath={_CONTROL_PATH}",
        ])
        return args

    def _build_ssh_args(self, command: str) -> list[str]:
        args = ["ssh", *self._ssh_options()]
        target = f"{self.user}@{self.host}" if self.user else self.host
        args.append(target)
        # Inject environment variables from config (e.g. proxy settings).
        if self.env:
            exports = " && ".join(
                f"export {k}={shlex.quote(v)}"
                for k, v in self.env.items()
            )
            command = f"{exports} && {command}"
        # Use login shell so PATH includes /usr/bin, /usr/local/bin, etc.
        # OpenSSH passes this string through the remote user's POSIX shell.
        # Quote for that shell so variables and substitutions are evaluated
        # exactly once by the requested login shell, not by its parent.
        args.append(f"bash -lc {shlex.quote(command)}")
        return args

    def run(self, command: str, timeout: int = 300, stream: bool = False) -> subprocess.CompletedProcess[str]:
        """Execute a command on the remote host.

        Parameters
        ----------
        stream : bool
            If True, stream stdout to the local terminal in real-time
            instead of capturing it. Useful for long-running build steps.
        """
        args = self._build_ssh_args(command)
        log.info("SSH: %s", command)
        if stream:
            return self._run_streaming(args, timeout)
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = _coerce_timeout_text(exc.stderr)
            timeout_line = f"[TIMEOUT after {timeout}s]"
            if stderr:
                stderr = f"{stderr}\n{timeout_line}"
            else:
                stderr = timeout_line
            stdout = _coerce_timeout_text(
                exc.stdout if exc.stdout is not None else exc.output
            )
            return subprocess.CompletedProcess(
                args=args,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            )

    def _run_streaming(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        """Execute command with real-time output streamed to the terminal."""
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout_lines: list[str] = []
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                print(f"  {line}", flush=True)
                stdout_lines.append(line)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_lines.append(f"[TIMEOUT after {timeout}s]")
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout="\n".join(stdout_lines),
            stderr="",
        )

    def _scp_args(self, *, recursive: bool = False, legacy_protocol: bool = True) -> list[str]:
        args = ["scp"]
        if legacy_protocol:
            # OpenSSH defaults scp to SFTP.  Some lab hosts have unstable SFTP
            # subsystems, while the legacy scp protocol still works reliably.
            args.append("-O")
        if recursive:
            args.append("-r")
        args.extend(self._scp_options())
        return args

    def _run_scp(self, args: list[str], *, timeout: int = _SCP_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = _coerce_timeout_text(exc.stderr)
            timeout_line = f"[TIMEOUT after {timeout}s]"
            if stderr:
                stderr = f"{stderr}\n{timeout_line}"
            else:
                stderr = timeout_line
            stdout = _coerce_timeout_text(
                exc.stdout if exc.stdout is not None else exc.output
            )
            return subprocess.CompletedProcess(
                args=args,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            )

    def _scp_transfer(
        self,
        source: str,
        destination: str,
        *,
        recursive: bool = False,
        timeout: int = _SCP_TIMEOUT_SECONDS,
        legacy_first: bool = True,
    ) -> bool:
        protocol_order = (True, False) if legacy_first else (False, True)
        for legacy_protocol in protocol_order:
            args = self._scp_args(
                recursive=recursive,
                legacy_protocol=legacy_protocol,
            )
            args.extend([source, destination])
            protocol_timeout = timeout if legacy_protocol else min(timeout, _SFTP_TIMEOUT_SECONDS)
            result = self._run_scp(args, timeout=protocol_timeout)
            if result.returncode == 0:
                return True
            mode = "legacy scp" if legacy_protocol else "sftp scp"
            log.warning(
                "%s transfer failed with exit %s: %s",
                mode,
                result.returncode,
                (result.stderr or result.stdout)[:500],
            )
        return False

    def fetch_file(self, remote_path: str, local_path: Path) -> bool:
        """Download a file from the remote host via scp."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = local_path.with_name(f".{local_path.name}.tmp-{os.getpid()}")
        temp_path.unlink(missing_ok=True)
        target = f"{self.user}@{self.host}" if self.user else self.host
        ok = False
        try:
            ok = self._scp_transfer(
                f"{target}:{remote_path}",
                str(temp_path),
                legacy_first=False,
            )
            if not ok:
                ok = self._fetch_file_via_ssh_cat(remote_path, temp_path)
            if ok:
                temp_path.replace(local_path)
                return True
            return False
        finally:
            temp_path.unlink(missing_ok=True)

    def _fetch_file_via_ssh_cat(self, remote_path: str, local_path: Path) -> bool:
        args = self._build_ssh_args(f"cat {shlex.quote(remote_path)}")
        log.info("SSH fetch fallback: %s", remote_path)
        try:
            with local_path.open("wb") as output:
                result = subprocess.run(
                    args,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=_SCP_TIMEOUT_SECONDS,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            local_path.unlink(missing_ok=True)
            log.warning("SSH fetch fallback timed out for %s", remote_path)
            return False
        if result.returncode != 0:
            local_path.unlink(missing_ok=True)
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            log.warning(
                "SSH fetch fallback failed with exit %s: %s",
                result.returncode,
                stderr[:500],
            )
            return False
        return True

    def push_file(self, local_path: Path, remote_path: str) -> bool:
        """Upload a file to the remote host via scp."""
        target = f"{self.user}@{self.host}" if self.user else self.host
        return self._scp_transfer(
            str(local_path),
            f"{target}:{remote_path}",
            legacy_first=True,
        )

    def push_dir(self, local_dir: Path, remote_dir: str) -> bool:
        """Upload a directory to the remote host via scp -r."""
        target = f"{self.user}@{self.host}" if self.user else self.host
        return self._scp_transfer(
            str(local_dir),
            f"{target}:{remote_dir}",
            recursive=True,
        )

    def fetch_dir(
        self,
        remote_dir: str,
        local_dir: Path,
        *,
        timeout: int | None = None,
    ) -> bool:
        """Download a directory from the remote host via scp -r."""
        transfer_timeout = timeout or _SCP_TIMEOUT_SECONDS
        local_dir.mkdir(parents=True, exist_ok=True)
        if self._fetch_dir_via_ssh_tar(
            remote_dir, local_dir, timeout=transfer_timeout
        ):
            return True
        target = f"{self.user}@{self.host}" if self.user else self.host
        return self._scp_transfer(
            f"{target}:{remote_dir}/.",
            str(local_dir),
            recursive=True,
            timeout=transfer_timeout,
        )

    def _fetch_dir_via_ssh_tar(
        self,
        remote_dir: str,
        local_dir: Path,
        *,
        timeout: int = _SCP_TIMEOUT_SECONDS,
    ) -> bool:
        """Fetch directory contents without relying on SFTP or legacy scp syntax."""

        archive = local_dir.parent / f".{local_dir.name}.remote-{os.getpid()}.tar"
        archive.unlink(missing_ok=True)
        command = f"tar -C {shlex.quote(remote_dir)} -cf - ."
        try:
            with archive.open("wb") as output:
                result = subprocess.run(
                    self._build_ssh_args(command),
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            if result.returncode != 0:
                stderr = _coerce_timeout_text(result.stderr)
                log.warning(
                    "SSH tar fetch failed with exit %s: %s",
                    result.returncode,
                    stderr[:500],
                )
                return False
            extracted = subprocess.run(
                [
                    "tar",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "-xf",
                    str(archive),
                    "-C",
                    str(local_dir),
                ],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            if extracted.returncode != 0:
                stderr = _coerce_timeout_text(extracted.stderr)
                log.warning(
                    "local tar extract failed with exit %s: %s",
                    extracted.returncode,
                    stderr[:500],
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            log.warning("SSH tar fetch timed out for %s", remote_dir)
            return False
        finally:
            archive.unlink(missing_ok=True)

    def docker_exec(self, container: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        """Execute a command inside a Docker container on the remote host."""
        return self.run(f"docker exec {container} {command}", timeout=timeout)

    def docker_logs(self, container: str, *, tail: int | None = None) -> str:
        """Fetch Docker container logs from the remote host."""
        cmd = f"docker logs {container}"
        if tail is not None:
            cmd += f" --tail {tail}"
        result = self.run(cmd)
        return result.stdout

    @staticmethod
    def from_string(spec: str) -> "SshExecutor":
        """Parse 'user@host' or 'host' into an SshExecutor."""
        if "@" in spec:
            user, host = spec.split("@", 1)
        else:
            user, host = "", spec
        return SshExecutor(host=host, user=user)


def _default_ssh_config() -> Path | None:
    config = Path.home() / ".ssh" / "config"
    return config if config.exists() else None


def _coerce_timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
