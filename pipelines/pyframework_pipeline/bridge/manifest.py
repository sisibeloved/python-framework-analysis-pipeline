"""BridgeManifest: track issue creation and resolution status.

Follows the AcquisitionManifest pattern: dataclass-based, JSON-serialisable,
written to ``bridge-manifest.json`` in the run directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class BridgeIssueEntry:
    """One issue tracked by the bridge."""

    issue_type: str  # "asm-diff" or "asm-diff-chunk"
    function_id: str
    platform: str  # "gitcode" or "github"
    repo: str  # "owner/repo"
    issue_number: int
    issue_url: str
    status: str  # "created" | "analysed" | "parsed" | "failed"
    created_at: str
    parsed_at: str = ""
    parent_issue_number: int | None = None  # for chunks, points to main issue
    extra: dict[str, Any] = field(default_factory=dict)

    # -- serialisation --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "issue_type": self.issue_type,
            "function_id": self.function_id,
            "platform": self.platform,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "issue_url": self.issue_url,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.parsed_at:
            d["parsed_at"] = self.parsed_at
        if self.parent_issue_number is not None:
            d["parent_issue_number"] = self.parent_issue_number
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BridgeIssueEntry:
        known_keys = {
            "issue_type", "function_id", "platform", "repo",
            "issue_number", "issue_url", "status", "created_at",
            "parsed_at", "parent_issue_number",
        }
        return cls(
            issue_type=data.get("issue_type", ""),
            function_id=data.get("function_id", ""),
            platform=data.get("platform", ""),
            repo=data.get("repo", ""),
            issue_number=int(data.get("issue_number", 0)),
            issue_url=data.get("issue_url", ""),
            status=data.get("status", "created"),
            created_at=data.get("created_at", ""),
            parsed_at=data.get("parsed_at", ""),
            parent_issue_number=data.get("parent_issue_number"),
            extra={k: v for k, v in data.items() if k not in known_keys},
        )


@dataclass
class BridgeManifest:
    """Master manifest for bridge issue tracking."""

    schema_version: int = SCHEMA_VERSION
    project_id: str = ""
    issues: list[BridgeIssueEntry] = field(default_factory=list)

    # -- serialisation --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "issues": [e.to_dict() for e in self.issues],
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # -- queries --------------------------------------------------------------

    def find_by_function(self, function_id: str) -> list[BridgeIssueEntry]:
        """Return all entries matching *function_id*."""
        return [e for e in self.issues if e.function_id == function_id]

    def find_main_issues(self) -> list[BridgeIssueEntry]:
        """Return entries that are NOT chunks (no parent)."""
        return [e for e in self.issues if e.parent_issue_number is None]


# ---------------------------------------------------------------------------
# Load helper
# ---------------------------------------------------------------------------

def load_bridge_manifest(path: Path) -> BridgeManifest:
    """Load a BridgeManifest from a JSON file.

    Returns an empty manifest if the file does not exist.
    """

    if not path.exists():
        return BridgeManifest()

    data = json.loads(path.read_text(encoding="utf-8"))
    issues = [
        BridgeIssueEntry.from_dict(e) for e in data.get("issues", [])
    ]
    return BridgeManifest(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        project_id=data.get("project_id", ""),
        issues=issues,
    )
