"""Thin Python wrapper around the GitHub REST API for PR operations."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# Polling interval when waiting for a PR to become mergeable.
_MERGE_POLL_SECONDS = 10
# Maximum time (seconds) to wait for CI / mergeability before giving up.
_MERGE_TIMEOUT_SECONDS = 600


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Extract owner, repo, and PR number from a GitHub PR URL.

    Args:
        pr_url: A URL like ``https://github.com/owner/repo/pull/123``.

    Returns:
        A tuple of ``(owner, repo, pr_number)``.

    Raises:
        ValueError: If *pr_url* does not match the expected pattern.
    """
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    if not match:
        raise ValueError(f"Cannot parse GitHub PR URL: {pr_url}")
    return match.group(1), match.group(2), int(match.group(3))


class GitHubClient:
    """Client for GitHub REST API operations needed by the orchestrator.

    Currently limited to reading and merging pull requests.
    """

    BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        """Initialise the client.

        Args:
            token: A GitHub personal access token with ``repo`` scope.
        """
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    @classmethod
    def from_env(cls) -> GitHubClient:
        """Create a client from the ``GITHUB_TOKEN`` environment variable.

        Raises:
            RuntimeError: If the variable is not set.
        """
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError(
                "Missing GITHUB_TOKEN environment variable. "
                "Set it or use --no-auto-merge to skip automatic merging."
            )
        return cls(token)

    def validate_repo_access(self, owner: str, repo: str) -> None:
        """Verify the token can read *and merge PRs* in the given repository.

        Makes a ``GET /repos/{owner}/{repo}`` call to check basic access,
        then inspects the ``permissions`` object in the response to verify
        the token has push (write) access — which is required for merging
        pull requests.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.

        Raises:
            RuntimeError: If the token cannot access the repository or
                lacks write permissions.
        """
        url = f"{self.BASE}/repos/{owner}/{repo}"
        resp = self._session.get(url)
        if resp.status_code == 401:
            raise RuntimeError(
                "GITHUB_TOKEN is invalid or expired (401 Unauthorized). "
                "Generate a new token at https://github.com/settings/tokens"
            )
        if resp.status_code == 403:
            raise RuntimeError(
                f"GITHUB_TOKEN does not have permission to access {owner}/{repo} "
                f"(403 Forbidden). Ensure the token has the 'repo' scope "
                f"(classic PAT) or 'Contents: Read and write' + "
                f"'Pull requests: Read and write' (fine-grained PAT)."
            )
        if resp.status_code == 404:
            raise RuntimeError(
                f"GITHUB_TOKEN cannot access {owner}/{repo} (404 Not Found). "
                f"This usually means:\n"
                f"  \u2022 The repo is private and the token lacks 'repo' scope\n"
                f"  \u2022 The token is a fine-grained PAT not scoped to this repo\n"
                f"Generate a token with 'repo' scope at "
                f"https://github.com/settings/tokens"
            )
        resp.raise_for_status()

        # Check write permission — required for merging PRs.
        data = resp.json()
        permissions = data.get("permissions", {})
        has_push = permissions.get("push", False)
        log.info(
            "[validate] Token permissions for %s/%s: %s",
            owner, repo, permissions,
        )
        if not has_push:
            raise RuntimeError(
                f"GITHUB_TOKEN can read {owner}/{repo} but lacks write "
                f"access (push=false). Merging PRs requires write permission.\n"
                f"For fine-grained PATs, add 'Contents: Read and write' "
                f"permission.\n"
                f"For classic PATs, ensure the 'repo' scope is enabled.\n"
                f"Update your token at https://github.com/settings/tokens"
            )
        log.info(
            "[validate] Token has read+write access to %s/%s",
            owner, repo,
        )

    # ------------------------------------------------------------------
    # Pull request helpers
    # ------------------------------------------------------------------

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        """Fetch a single pull request.

        Args:
            owner: Repository owner (user or org).
            repo: Repository name.
            pr_number: Pull request number.

        Returns:
            Parsed JSON response from the GitHub API.
        """
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
        resp = self._session.get(url)
        resp.raise_for_status()
        return resp.json()

    def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        merge_method: str = "squash",
    ) -> dict[str, Any]:
        """Merge a pull request.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            merge_method: One of ``merge``, ``squash``, or ``rebase``.

        Returns:
            Parsed JSON response from the GitHub API.

        Raises:
            requests.HTTPError: On API errors (e.g. merge conflict, CI failing).
        """
        url = f"{self.BASE}/repos/{owner}/{repo}/pulls/{pr_number}/merge"
        payload = {"merge_method": merge_method}
        resp = self._session.put(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    def wait_and_merge(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        merge_method: str = "squash",
        timeout: int = _MERGE_TIMEOUT_SECONDS,
    ) -> bool:
        """Poll until a PR is mergeable, then merge it.

        Returns ``True`` if the merge succeeded, ``False`` if it timed out
        or encountered an unrecoverable error.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            merge_method: Merge strategy (``merge``, ``squash``, ``rebase``).
            timeout: Maximum seconds to wait for mergeability.

        Returns:
            ``True`` on successful merge, ``False`` otherwise.
        """
        deadline = time.time() + timeout
        poll_count = 0

        while time.time() < deadline:
            poll_count += 1
            pr = self.get_pull_request(owner, repo, pr_number)

            state = pr.get("state")
            merged = pr.get("merged")
            mergeable = pr.get("mergeable")
            mergeable_state = pr.get("mergeable_state", "")
            log.info(
                "[auto-merge] poll %d for %s/%s#%d: "
                "state=%s merged=%s mergeable=%s mergeable_state=%s",
                poll_count, owner, repo, pr_number,
                state, merged, mergeable, mergeable_state,
            )

            # Already merged by someone else.
            if merged:
                log.info("[auto-merge] PR already merged.")
                return True

            # Closed without merging.
            if state == "closed":
                log.info("[auto-merge] PR is closed — cannot merge.")
                return False

            # GitHub sometimes returns null while computing mergeability.
            if mergeable is None:
                log.info("[auto-merge] mergeable=null — waiting for GitHub to compute…")
                time.sleep(_MERGE_POLL_SECONDS)
                continue

            if not mergeable:
                # Merge conflict — cannot auto-merge.
                log.info("[auto-merge] PR has merge conflicts — cannot auto-merge.")
                return False

            # Attempt the merge if CI is clean or unstable (some repos
            # don't require CI).
            if mergeable_state in {"clean", "unstable", "has_hooks"}:
                try:
                    self.merge_pull_request(
                        owner, repo, pr_number, merge_method=merge_method,
                    )
                    log.info("[auto-merge] Merge succeeded!")
                    return True
                except requests.HTTPError as exc:
                    status_code = (
                        exc.response.status_code
                        if exc.response is not None
                        else None
                    )
                    body = (
                        exc.response.text[:200]
                        if exc.response is not None
                        else str(exc)
                    )
                    log.warning(
                        "[auto-merge] Merge API returned %s: %s",
                        status_code, body,
                    )
                    # 401/403 are permission errors — retrying won't help.
                    if status_code in {401, 403}:
                        log.error(
                            "[auto-merge] Permission denied (HTTP %d). "
                            "The token likely lacks 'Contents: Read and "
                            "write' permission (fine-grained PAT) or "
                            "'repo' scope (classic PAT). Giving up.",
                            status_code,
                        )
                        return False
                    # Other errors (e.g. 405, 409) may be transient.
            else:
                log.info(
                    "[auto-merge] mergeable_state=%r not in allowed set — waiting…",
                    mergeable_state,
                )

            time.sleep(_MERGE_POLL_SECONDS)

        log.warning("[auto-merge] Timed out after %ds.", timeout)
        return False
