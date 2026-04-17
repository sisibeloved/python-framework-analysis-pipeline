"""IssueClient protocol and factory for GitHub/GitCode."""
from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class IssueClient(Protocol):
    """Protocol for platform-specific issue API clients."""

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue.  Returns dict with ``number`` and ``html_url``."""
        ...

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, Any]]:
        """Fetch all comments.  Returns list with ``body`` and ``id`` fields."""
        ...

    def ensure_label(
        self,
        owner: str,
        repo: str,
        name: str,
        color: str,
        description: str = "",
    ) -> None:
        """Create label if it doesn't exist."""
        ...


def create_client(
    platform: str,
    token: str,
    base_url: str | None = None,
) -> IssueClient:
    """Factory for platform clients.

    Parameters
    ----------
    platform:
        ``"github"`` or ``"gitcode"``.
    token:
        API personal-access token.
    base_url:
        Override the default API base URL for the chosen platform.
    """
    # Late import so the protocol module has zero hard deps on implementations.
    if platform == "github":
        from .github_client import GitHubClient

        return GitHubClient(token=token, base_url=base_url)

    if platform == "gitcode":
        from .gitcode_client import GitCodeClient

        return GitCodeClient(token=token, base_url=base_url)

    raise ValueError(
        f"Unsupported platform {platform!r}; choose 'github' or 'gitcode'"
    )
