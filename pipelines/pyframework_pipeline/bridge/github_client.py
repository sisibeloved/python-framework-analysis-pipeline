"""GitHub Issues API client using urllib.request."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_GITHUB_DEFAULT_BASE = "https://api.github.com"
_REQUEST_TIMEOUT = 30  # seconds


class GitHubClient:
    """REST v3 client for GitHub Issues API.

    Parameters
    ----------
    token:
        GitHub personal-access token (Bearer auth).
    base_url:
        Override default ``https://api.github.com``.
    """

    def __init__(self, token: str, base_url: str | None = None) -> None:
        self._token = token
        self._base = (base_url or _GITHUB_DEFAULT_BASE).rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create an issue and return ``{"number": ..., "html_url": ...}``."""
        url = f"{self._base}/repos/{owner}/{repo}/issues"
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = self._request("POST", url, payload)
        return {
            "number": data.get("number"),
            "html_url": data.get("html_url", ""),
        }

    def get_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, Any]]:
        """Fetch all comments for *issue_number*, handling Link-header pagination."""
        comments: list[dict[str, Any]] = []
        url: str | None = (
            f"{self._base}/repos/{owner}/{repo}/issues/{issue_number}/comments"
        )
        while url:
            page, next_url = self._get_page(url)
            for c in page:
                comments.append({"body": c.get("body", ""), "id": c.get("id")})
            url = next_url
        return comments

    def ensure_label(
        self,
        owner: str,
        repo: str,
        name: str,
        color: str,
        description: str = "",
    ) -> None:
        """Create a label; ignore 422 (already exists)."""
        url = f"{self._base}/repos/{owner}/{repo}/labels"
        payload = {"name": name, "color": color, "description": description}
        try:
            self._request("POST", url, payload)
            logger.info("Label %r created in %s/%s", name, owner, repo)
        except urllib.error.HTTPError as exc:
            if exc.code == 422:
                logger.debug("Label %r already exists in %s/%s", name, owner, repo)
            else:
                raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> urllib.request.Request:
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "pyframework-pipeline")
        if body is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        return req

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return parsed JSON.  Raises on HTTP errors."""
        req = self._build_request(method, url, payload)
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                self._check_rate_limit(resp)
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            logger.error(
                "GitHub API %s %s failed: %s %s",
                method,
                url,
                exc.code,
                exc.reason,
            )
            raise
        except urllib.error.URLError as exc:
            logger.error("GitHub API network error: %s", exc.reason)
            raise

    def _get_page(
        self,
        url: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """GET *url* and return ``(items, next_page_url_or_None)``.

        Captures the ``Link`` response header for pagination before the
        response body is consumed.
        """
        req = self._build_request("GET", url)
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                self._check_rate_limit(resp)
                link_header: str = resp.headers.get("Link", "")
                raw = resp.read()
                data = json.loads(raw.decode("utf-8")) if raw else []
                items = data if isinstance(data, list) else [data]
                return items, _parse_link_header(link_header)
        except urllib.error.HTTPError as exc:
            logger.error(
                "GitHub API GET %s failed: %s %s",
                url,
                exc.code,
                exc.reason,
            )
            return [], None
        except urllib.error.URLError as exc:
            logger.error("GitHub API network error: %s", exc.reason)
            return [], None

    @staticmethod
    def _check_rate_limit(resp: http.client.HTTPResponse) -> None:  # type: ignore[name-defined]  # noqa: F821
        """Log a warning when remaining requests are low."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and int(remaining) < 100:
            logger.warning(
                "GitHub rate limit low: %s requests remaining",
                remaining,
            )


def _parse_link_header(header: str) -> str | None:
    """Extract the ``next`` page URL from a GitHub ``Link`` header.

    Example header::

        Link: <https://api.github.com/repos/o/r/issues/1/comments?page=2>;
              rel="next", <...?page=5>; rel="last"
    """
    for part in header.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            start = section.find("<")
            end = section.find(">")
            if start != -1 and end != -1:
                return section[start + 1 : end]
    return None
