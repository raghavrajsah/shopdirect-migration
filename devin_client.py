"""Thin Python wrapper around the Devin v3 API."""

from __future__ import annotations

import os
from typing import Any

import requests


class DevinClient:
    """Client for interacting with the Devin v3 API.

    Provides methods for managing playbooks and sessions via the
    Devin REST API using Bearer token authentication.
    """

    BASE = "https://api.devin.ai/v3/organizations"

    def __init__(self, api_key: str, org_id: str) -> None:
        """Initialise the client with explicit credentials.

        Args:
            api_key: Devin API key used for Bearer authentication.
            org_id: Organisation ID that scopes all API requests.
        """
        self._api_key = api_key
        self._org_id = org_id
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    @classmethod
    def from_env(cls) -> DevinClient:
        """Create a client from ``DEVIN_API_KEY`` and ``DEVIN_ORG_ID`` environment variables.

        Raises:
            RuntimeError: If either environment variable is missing.
        """
        api_key = os.environ.get("DEVIN_API_KEY")
        org_id = os.environ.get("DEVIN_ORG_ID")

        missing: list[str] = []
        if not api_key:
            missing.append("DEVIN_API_KEY")
        if not org_id:
            missing.append("DEVIN_ORG_ID")

        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them before creating the client."
            )

        return cls(api_key, org_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        """Build a fully-qualified API URL for the given *path*."""
        return f"{self.BASE}/{self._org_id}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Send an HTTP request, raise on error, and return parsed JSON.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path relative to the organisation root (e.g. ``/sessions``).
            **kwargs: Forwarded to :pymethod:`requests.Session.request`.

        Returns:
            Parsed JSON response body.
        """
        resp = self._session.request(method, self._url(path), **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Playbooks
    # ------------------------------------------------------------------

    def create_playbook(self, name: str, instructions: str) -> dict[str, Any]:
        """Create a new playbook.

        Args:
            name: Display name for the playbook.
            instructions: Markdown instructions that define the playbook behaviour.

        Returns:
            API response containing the created playbook details.
        """
        payload = {"name": name, "instructions": instructions}
        return self._request("POST", "/playbooks", json=payload)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def create_session(
        self,
        prompt: str,
        *,
        playbook_id: str | None = None,
        repos: list[str] | None = None,
        tags: list[str] | None = None,
        title: str | None = None,
        max_acu_limit: float | None = None,
    ) -> dict[str, Any]:
        """Create a new Devin session.

        Args:
            prompt: The task prompt for Devin.
            playbook_id: Optional playbook to attach to the session.
            repos: Optional list of repository identifiers.
            tags: Optional tags for categorising the session.
            title: Optional human-readable title.
            max_acu_limit: Optional ACU spending cap.

        Returns:
            API response containing the created session details.
        """
        payload: dict[str, Any] = {"prompt": prompt}
        if playbook_id is not None:
            payload["playbook_id"] = playbook_id
        if repos is not None:
            payload["repos"] = repos
        if tags is not None:
            payload["tags"] = tags
        if title is not None:
            payload["title"] = title
        if max_acu_limit is not None:
            payload["max_acu_limit"] = max_acu_limit

        return self._request("POST", "/sessions", json=payload)

    def get_session(self, session_id: str) -> dict[str, Any]:
        """Retrieve details for an existing session.

        Args:
            session_id: The unique session identifier.

        Returns:
            API response containing session details.
        """
        return self._request("GET", f"/sessions/{session_id}")

    def send_message(self, session_id: str, message: str) -> dict[str, Any]:
        """Send a follow-up message to a running session.

        Args:
            session_id: The target session identifier.
            message: The message text to send.

        Returns:
            API response acknowledging the message.
        """
        payload = {"message": message}
        return self._request("POST", f"/sessions/{session_id}/messages", json=payload)

    def list_sessions(self, *, tags: list[str] | None = None) -> dict[str, Any]:
        """List sessions, optionally filtered by tags.

        Args:
            tags: If provided, only sessions matching these tags are returned.

        Returns:
            API response containing a list of sessions.
        """
        params: dict[str, Any] = {}
        if tags is not None:
            params["tags"] = tags
        return self._request("GET", "/sessions", params=params)
