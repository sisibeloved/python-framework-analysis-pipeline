"""Step 7: cross-platform assembly diff analysis + optimization opportunities.

Merged from original steps 7 and 8. Each hotspot function gets one issue on
GitCode/GitHub. The external LLM produces a full line-by-line comparison,
root cause summary, and optimization strategy as a structured Markdown comment.

Delegates to ``bridge.analysis`` for publish/fetch orchestration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..bridge.analysis import fetch as bridge_fetch
from ..bridge.analysis import publish as bridge_publish


def run_publish(
    project_path: Path,
    repo: str,
    platform: str,
    token: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Publish analysis issues for all hotspot functions."""
    return bridge_publish(
        project_path, repo, platform, token, **kwargs,
    )


def run_fetch(
    project_path: Path,
    repo: str,
    platform: str,
    token: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fetch LLM comments and backfill analysis results."""
    return bridge_fetch(
        project_path, repo, platform, token, **kwargs,
    )
