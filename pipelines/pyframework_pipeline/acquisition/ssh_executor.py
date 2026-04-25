"""Optional SSH executor for remote data acquisition.

Provides SSH-based remote command execution for collecting data from
remote clusters when data files are not already present locally.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SshExecutor:
    """Execute commands on a remote host via SSH."""

    def __init__(
        self,
        host: str,
        user: str = "",
        key: Path | None = None,
        port: int = 22,
    ) -> None:
        self.host = host
        self.user = user
        self.key = key
        self.port = port

    def _build_ssh_args(self, command: str) -> list[str]:
        args = ["ssh"]
        if self.port != 22:
            args.extend(["-p", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend(["-o", "StrictHostKeyChecking=no"])
        target = f"{self.user}@{self.host}" if self.user else self.host
        args.append(target)
        # Use login shell so PATH includes /usr/bin, /usr/local/bin, etc.
        args.append(f"bash -lc {subprocess.list2cmdline([command])}")
        return args

    def run(self, command: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        """Execute a command on the remote host."""
        args = self._build_ssh_args(command)
        log.info("SSH: %s", command)
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def fetch_file(self, remote_path: str, local_path: Path) -> bool:
        """Download a file from the remote host via scp."""
        target = f"{self.user}@{self.host}" if self.user else self.host
        args = ["scp"]
        if self.port != 22:
            args.extend(["-P", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend(["-o", "StrictHostKeyChecking=no"])
        args.append(f"{target}:{remote_path}")
        args.append(str(local_path))
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        return result.returncode == 0

    def push_file(self, local_path: Path, remote_path: str) -> bool:
        """Upload a file to the remote host via scp."""
        target = f"{self.user}@{self.host}" if self.user else self.host
        args = ["scp"]
        if self.port != 22:
            args.extend(["-P", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend(["-o", "StrictHostKeyChecking=no"])
        args.append(str(local_path))
        args.append(f"{target}:{remote_path}")
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        return result.returncode == 0

    def push_dir(self, local_dir: Path, remote_dir: str) -> bool:
        """Upload a directory to the remote host via scp -r."""
        target = f"{self.user}@{self.host}" if self.user else self.host
        args = ["scp", "-r"]
        if self.port != 22:
            args.extend(["-P", str(self.port)])
        if self.key:
            args.extend(["-i", str(self.key)])
        args.extend(["-o", "StrictHostKeyChecking=no"])
        args.append(str(local_dir))
        args.append(f"{target}:{remote_dir}")
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        return result.returncode == 0

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
