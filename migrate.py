"""Main orchestration script for the ShopDirect TypeScript Migration."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.live import Live

from devin_client import DevinClient
from github_client import GitHubClient, parse_pr_url
from progress import build_progress_table
from scanner import get_all_tiers, get_batches_by_tier, scan_and_plan

try:
    from requests.exceptions import HTTPError as _RequestsHTTPError
except ImportError:  # pragma: no cover
    _RequestsHTTPError = None  # type: ignore[assignment,misc]

_POLL_INTERVAL_SECONDS = 30

# Extra polls after a session reaches terminal status to discover the PR URL.
# The Devin API may not populate pull_requests immediately on session exit.
_PR_DISCOVERY_RETRIES = 6
_PR_DISCOVERY_INTERVAL_SECONDS = 10

# Number of consecutive transient (5xx) poll errors to tolerate before
# marking a session as blocked.  A single 502 Bad Gateway is common and
# should not kill the run.
_MAX_TRANSIENT_POLL_ERRORS = 3

console = Console()


# ------------------------------------------------------------------
# Shared phase state — keeps foundation / consolidation status in one
# place so the progress table can be refreshed from anywhere.
# ------------------------------------------------------------------


class PhaseState:
    """Mutable container for the foundation and consolidation phase status.

    Attributes:
        foundation_status: Current status string for the foundation phase.
        foundation_pr_url: PR URL produced by the foundation phase, if any.
        consolidation_status: Current status string for the consolidation phase.
        consolidation_pr_url: PR URL produced by the consolidation phase, if any.
    """

    def __init__(self) -> None:
        self.foundation_status: str = "queued"
        self.foundation_pr_url: str | None = None
        self.consolidation_status: str = "queued"
        self.consolidation_pr_url: str | None = None


# ------------------------------------------------------------------
# Session tracker — records every session launched during the run so
# that a Ctrl+C handler can offer to terminate them all.
# ------------------------------------------------------------------


class SessionTracker:
    """Tracks all Devin session IDs created during a migration run.

    Attributes:
        sessions: Mapping of session_id to a human-readable label.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}  # session_id -> label

    def register(self, session_id: str, label: str) -> None:
        """Record a newly created session.

        Args:
            session_id: The Devin session identifier.
            label: A short human-readable name (e.g. batch name or phase).
        """
        self.sessions[session_id] = label

    def terminate_all(self, client: DevinClient) -> None:
        """Attempt to terminate every tracked session.

        Args:
            client: Initialised Devin API client.
        """
        if not self.sessions:
            console.print("[dim]No sessions to stop.[/dim]")
            return

        for session_id, label in self.sessions.items():
            try:
                client.terminate_session(session_id)
                console.print(f"  [red]Stopped[/red] {label} ({session_id})")
            except Exception as exc:
                console.print(f"  [yellow]Could not stop {label} ({session_id}): {exc}[/yellow]")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def load_playbook() -> str:
    """Read ``playbook.md`` from the same directory as this script.

    Returns:
        The playbook contents as a string.

    Raises:
        FileNotFoundError: If ``playbook.md`` is not found.
    """
    path = Path(__file__).resolve().parent / "playbook.md"
    if not path.exists():
        raise FileNotFoundError(f"playbook.md not found at {path}")
    return path.read_text()


def build_batch_prompt(batch: dict) -> str:
    """Build the Devin session prompt for a single migration batch.

    The prompt instructs Devin to import shared types from the canonical
    ``src/types/`` module instead of redefining cross-file domain entities.

    Args:
        batch: A batch dict from the migration plan.

    Returns:
        A multi-line prompt string instructing Devin to migrate the batch.
    """
    file_list = "\n".join(f"  - {f}" for f in batch["files"])
    return (
        f"You are migrating the **{batch['name']}** batch of the ShopDirect "
        f"frontend repo from JavaScript to TypeScript.\n\n"
        f"## Files to migrate\n{file_list}\n\n"
        f"## Instructions\n"
        f"1. Rename `.js` files to `.ts` and `.jsx` files to `.tsx`.\n"
        f"2. Add TypeScript types (interfaces, type annotations) without "
        f"changing business logic.\n"
        f"3. **Import shared domain types** from `src/types/` (e.g. "
        f"`src/types/index.ts`) instead of redefining them locally. "
        f"Shared entities such as `Product`, `CartItem`, `Order`, `User`, "
        f"`Address`, and similar cross-file types should already be defined "
        f"there by the foundation phase.\n"
        f"4. **Do not redefine** `Product`, `CartItem`, `Order`, `User`, "
        f"`Address`, or other shared domain interfaces if they already "
        f"exist in `src/types/`. Import and reuse them.\n"
        f"5. Define new local types only when they are truly specific to "
        f"this batch and not shared across the repo.\n"
        f"6. Prefer canonical shared types over `unknown` and unnecessary "
        f"type assertions.\n"
        f"7. Update any directly necessary imports affected by the renames.\n"
        f"8. Run `npx tsc --noEmit` to verify the code compiles.\n"
        f"9. Run tests if a test runner is available.\n"
        f"10. If everything passes, open a PR with the changes.\n"
        f"11. Keep changes scoped to the listed files plus any directly "
        f"necessary import or compile-fix updates.\n"
        f"12. If you cannot complete cleanly, summarize the blockers in "
        f"your final message.\n"
        f"13. Once the PR is open and verification passes, finish the session. "
        f"Do NOT wait for manual testing or further instructions.\n"
    )


def is_terminal_status(session_data: dict) -> bool:
    """Return True if the session has reached a terminal state.

    Terminal states: ``exit``, ``error``, ``suspended``, or
    ``running`` with ``status_detail == "finished"``.

    Args:
        session_data: Session details from the Devin API.
    """
    status = (session_data.get("status") or "").lower()
    status_detail = (session_data.get("status_detail") or "").lower()
    if status in {"exit", "error", "suspended"}:
        return True
    if status == "running" and status_detail == "finished":
        return True
    if status == "running" and status_detail == "waiting_for_user":
        pull_requests = session_data.get("pull_requests") or []
        if pull_requests:
            return True
    return False


def map_session_to_batch_status(session_data: dict) -> str:
    """Map a Devin session status to a batch status string.

    Args:
        session_data: Session details from the Devin API.

    Returns:
        One of ``running``, ``complete``, ``blocked``, or ``needs_input``.
    """
    status = (session_data.get("status") or "").lower()
    status_detail = (session_data.get("status_detail") or "").lower()
    if status == "exit":
        return "complete"
    if status == "running" and status_detail == "finished":
        return "complete"
    if status in {"error", "suspended"}:
        return "blocked"
    if status == "running" and status_detail == "waiting_for_user":
        pull_requests = session_data.get("pull_requests") or []
        if pull_requests:
            return "complete"
        return "needs_input"
    return "running"


def extract_pr_url(session_data: dict) -> str | None:
    """Extract the first pull request URL from session data, if available.

    Checks ``pull_requests[].pr_url``, ``pull_requests[].html_url``,
    ``pull_requests[].url``, and ``structured_output.pr_url``.

    Args:
        session_data: Session details from the Devin API.

    Returns:
        A PR URL string, or ``None``.
    """
    pull_requests = session_data.get("pull_requests") or []
    if pull_requests:
        pr = pull_requests[0]
        url = pr.get("pr_url") or pr.get("html_url") or pr.get("url")
        if url:
            return url

    structured = session_data.get("structured_output") or {}
    pr_url = structured.get("pr_url")
    if pr_url:
        return pr_url

    return None


def _is_transient_error(exc: Exception) -> bool:
    """Return True if *exc* looks like a transient server-side error (5xx).

    These are safe to retry — a single 502 Bad Gateway from the Devin API
    should not kill the entire run.
    """
    if _RequestsHTTPError is not None and isinstance(exc, _RequestsHTTPError):
        resp = getattr(exc, "response", None)
        if resp is not None and 500 <= resp.status_code < 600:
            return True
    # Fall back to string matching for wrapped or non-requests errors.
    text = str(exc)
    for code in ("500", "502", "503", "504"):
        if code in text and ("Server Error" in text or "Bad Gateway" in text or "Service Unavailable" in text or "Gateway Timeout" in text):
            return True
    return False


def _discover_pr_url(
    client: DevinClient,
    session_id: str,
    label: str,
) -> str | None:
    """Poll a completed session a few extra times to discover its PR URL.

    The Devin API may not populate ``pull_requests`` on the exact poll
    where the session transitions to a terminal state.  This helper
    retries a handful of times with short intervals.

    Args:
        client: Initialised Devin API client.
        session_id: The session to query.
        label: Human-readable name for log messages.

    Returns:
        The PR URL if found, otherwise ``None``.
    """
    for attempt in range(1, _PR_DISCOVERY_RETRIES + 1):
        time.sleep(_PR_DISCOVERY_INTERVAL_SECONDS)
        try:
            data = client.get_session(session_id)
        except Exception:
            continue
        pr_url = extract_pr_url(data)
        if pr_url:
            console.print(
                f"[green]{label} PR discovered on retry {attempt}: {pr_url}[/green]"
            )
            return pr_url
    return None


def _make_refresh(
    plan: dict,
    start_time: float,
    phase_state: PhaseState,
    live: Live,
) -> Callable[[], None]:
    """Return a zero-arg callable that refreshes the Live progress table.

    Args:
        plan: The migration plan (read each refresh).
        start_time: Wall-clock start time.
        phase_state: Shared mutable phase state.
        live: The active ``rich.live.Live`` context.

    Returns:
        A callable suitable for ``_refresh()``.
    """

    def _refresh() -> None:
        live.update(
            build_progress_table(
                plan,
                time.time() - start_time,
                foundation_status=phase_state.foundation_status,
                foundation_pr_url=phase_state.foundation_pr_url,
                consolidation_status=phase_state.consolidation_status,
                consolidation_pr_url=phase_state.consolidation_pr_url,
            )
        )

    return _refresh


def _wait_for_merge_manual(pr_urls: list[str], message: str) -> None:
    """Pause execution and wait for the user to confirm PRs are merged.

    Used when ``--no-auto-merge`` is set.

    Args:
        pr_urls: List of PR URLs to display.
        message: The prompt message shown above the PR list.
    """
    console.print()
    console.print(f"[bold yellow]{message}[/bold yellow]")
    for url in pr_urls:
        console.print(f"  • {url}")
    console.print()
    input("Press Enter to continue once the PR(s) above are merged… ")
    console.print()


def _auto_merge_prs(gh: GitHubClient, pr_urls: list[str], phase_label: str) -> bool:
    """Auto-merge a list of PRs via the GitHub API.

    For each PR, polls until it is mergeable, then squash-merges it.
    Prints status for each PR.

    Args:
        gh: Initialised GitHub API client.
        pr_urls: GitHub PR URLs to merge.
        phase_label: Human-readable phase name for log output.

    Returns:
        ``True`` if **all** PRs were merged successfully, ``False`` otherwise.
    """
    all_ok = True
    # Use print() alongside console.print() to ensure visibility even if
    # Rich's Live context interferes with console output.
    print(f"\n>>> Auto-merging {phase_label} PR(s)…")
    console.print()
    console.print(f"[bold green]Auto-merging {phase_label} PR(s)…[/bold green]")
    for url in pr_urls:
        print(f"  Merging {url} …", end=" ", flush=True)
        console.print(f"  Merging {url} …", end=" ")
        try:
            owner, repo, pr_number = parse_pr_url(url)
            ok = gh.wait_and_merge(owner, repo, pr_number)
            if ok:
                print("merged")
                console.print("[green]merged[/green]")
            else:
                print("FAILED (conflict, CI, or timeout)")
                console.print(
                    "[red]failed (conflict, CI, or timeout) — merge manually[/red]"
                )
                all_ok = False
        except Exception as exc:
            print(f"ERROR: {exc}")
            console.print(f"[red]error: {exc}[/red]")
            all_ok = False
    console.print()
    return all_ok


# ------------------------------------------------------------------
# Phase 0 — Foundation
# ------------------------------------------------------------------

_FOUNDATION_PROMPT = """\
You are preparing the **ShopDirect** frontend repo for a large-scale \
JavaScript-to-TypeScript migration.

## Goal
Establish shared type contracts and migration prerequisites **only**. \
Do NOT migrate application files, rename JS/JSX files, or make unrelated \
refactors.

## Tasks
1. Analyse the entire frontend repo to identify shared cross-file domain \
entities (e.g. Product, CartItem, Order, User, Address, and similar \
repeated shapes).
2. Create a shared canonical types module under `src/types/` \
(for example `src/types/index.ts`).
3. Define canonical shared interfaces for every cross-file domain entity \
you identified.
4. Separate core shared domain types from UI-specific derived shapes when \
appropriate (e.g. `src/types/product.ts`, `src/types/cart.ts`, \
re-exported from `src/types/index.ts`).
5. Add `tsconfig.json` at the repo root if it does not already exist, \
configured for incremental migration (allow JS, strict where possible).
6. Add required TypeScript dev dependencies (`typescript`, \
`@types/react`, etc.) if they are not already present.
7. Open a PR with these foundation changes if possible.

## Constraints
- Do **not** rename any `.js` or `.jsx` files.
- Do **not** migrate application code.
- Do **not** perform unrelated refactors.
- Keep changes scoped to shared type definitions and TS configuration only.
- Once the PR is open and verification passes, finish the session. \
Do NOT wait for manual testing or further instructions.
"""


def run_foundation_phase(
    client: DevinClient,
    playbook_id: str,
    frontend_repo_name: str,
    plan: dict,
    start_time: float,
    phase_state: PhaseState,
    tracker: SessionTracker,
    live: Live,
) -> None:
    """Launch a single Devin session for the foundation phase and poll until done.

    The session analyses the repo, creates shared canonical types under
    ``src/types/``, adds ``tsconfig.json`` if missing, installs TS dev
    dependencies, and opens a PR.

    Args:
        client: Initialised Devin API client.
        playbook_id: ID of the created playbook.
        frontend_repo_name: Devin repo identifier for the frontend repo.
        plan: The full migration plan (used for progress display).
        start_time: Wall-clock start time.
        phase_state: Shared mutable phase state.
        tracker: Session tracker for Ctrl+C cleanup.
        live: Active ``rich.live.Live`` context for refreshing the table.
    """
    _refresh = _make_refresh(plan, start_time, phase_state, live)

    phase_state.foundation_status = "running"
    _refresh()

    try:
        resp = client.create_session(
            prompt=_FOUNDATION_PROMPT,
            playbook_id=playbook_id,
            repos=[frontend_repo_name],
            tags=["ts-migration", "foundation"],
            title="ShopDirect TS Migration: Foundation",
            max_acu_limit=10,
        )
    except Exception as exc:
        console.print(f"[red]Failed to create foundation session: {exc}[/red]")
        phase_state.foundation_status = "blocked"
        _refresh()
        return

    session_id = resp.get("session_id") or resp.get("id")
    if not session_id:
        console.print("[red]API returned no session ID for foundation — marking blocked[/red]")
        phase_state.foundation_status = "blocked"
        _refresh()
        return

    tracker.register(session_id, "Foundation")

    session_url = resp.get("url") or resp.get("session_url", "")
    if session_url:
        console.print(f"[bold]Foundation session:[/bold] {session_url}")

    notified_needs_input = False
    consecutive_errors = 0

    # Poll until the session reaches a terminal state.
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            data = client.get_session(session_id)
        except Exception as exc:
            if _is_transient_error(exc) and consecutive_errors < _MAX_TRANSIENT_POLL_ERRORS:
                consecutive_errors += 1
                console.print(
                    f"[yellow]Foundation poll error ({consecutive_errors}/"
                    f"{_MAX_TRANSIENT_POLL_ERRORS}): {exc} — will retry[/yellow]"
                )
                continue
            console.print(f"[red]Foundation poll error: {exc} — marking blocked[/red]")
            phase_state.foundation_status = "blocked"
            _refresh()
            return

        consecutive_errors = 0  # Reset on successful poll.

        phase_state.foundation_status = map_session_to_batch_status(data)

        if phase_state.foundation_status == "needs_input" and not notified_needs_input:
            notified_needs_input = True
            console.print(
                f"[yellow]\u26a0 Foundation session is waiting for user input \u2192 {session_url}[/yellow]"
            )

        pr_url = extract_pr_url(data)
        if pr_url:
            phase_state.foundation_pr_url = pr_url

        _refresh()

        if is_terminal_status(data):
            break

    # The API may not populate pull_requests on the exact poll where the
    # session reaches terminal status.  Retry a few times if needed.
    if not phase_state.foundation_pr_url and phase_state.foundation_status == "complete":
        console.print(
            "[yellow]Foundation completed but PR URL not found yet — "
            "retrying discovery…[/yellow]"
        )
        discovered = _discover_pr_url(client, session_id, "Foundation")
        if discovered:
            phase_state.foundation_pr_url = discovered
            _refresh()


# ------------------------------------------------------------------
# Phase 3 — Consolidation
# ------------------------------------------------------------------

_CONSOLIDATION_PROMPT = """\
You are performing **post-migration consolidation** on the ShopDirect \
frontend repo after parallel TypeScript migration batches have completed.

## Goal
Reconcile cross-batch type inconsistencies and ensure the repo compiles \
cleanly with a single coherent type system.

## Tasks
1. Run `npx tsc --noEmit` across the repo and collect all errors.
2. Resolve remaining cross-batch type mismatches.
3. Replace duplicated local shared-domain interfaces (e.g. `Product`, \
`CartItem`, `Order`, `User`, `Address`) with imports from the canonical \
shared types module at `src/types/`.
4. Remove remaining unnecessary `unknown` usages where shared types exist.
5. Remove unnecessary type assertions where proper typing is available.
6. Run tests if available (`npm test` or equivalent).
7. Open a cleanup PR with all consolidation fixes if possible.

## Constraints
- Do **not** change business logic.
- Do **not** perform unrelated refactors.
- Keep changes scoped to type-related fixes only.
- Once the PR is open and verification passes, finish the session. \
Do NOT wait for manual testing or further instructions.
"""


def run_consolidation_phase(
    client: DevinClient,
    playbook_id: str,
    frontend_repo_name: str,
    plan: dict,
    start_time: float,
    phase_state: PhaseState,
    tracker: SessionTracker,
    live: Live,
) -> None:
    """Launch a single Devin session for the consolidation phase and poll until done.

    The session runs ``tsc --noEmit``, resolves cross-batch type mismatches,
    deduplicates local domain interfaces in favour of shared types, and
    opens a cleanup PR.

    Args:
        client: Initialised Devin API client.
        playbook_id: ID of the created playbook.
        frontend_repo_name: Devin repo identifier for the frontend repo.
        plan: The full migration plan (used for progress display).
        start_time: Wall-clock start time.
        phase_state: Shared mutable phase state.
        tracker: Session tracker for Ctrl+C cleanup.
        live: Active ``rich.live.Live`` context for refreshing the table.
    """
    _refresh = _make_refresh(plan, start_time, phase_state, live)

    phase_state.consolidation_status = "running"
    _refresh()

    try:
        resp = client.create_session(
            prompt=_CONSOLIDATION_PROMPT,
            playbook_id=playbook_id,
            repos=[frontend_repo_name],
            tags=["ts-migration", "consolidation"],
            title="ShopDirect TS Migration: Consolidation",
            max_acu_limit=10,
        )
    except Exception as exc:
        console.print(f"[red]Failed to create consolidation session: {exc}[/red]")
        phase_state.consolidation_status = "blocked"
        _refresh()
        return

    session_id = resp.get("session_id") or resp.get("id")
    if not session_id:
        console.print("[red]API returned no session ID for consolidation — marking blocked[/red]")
        phase_state.consolidation_status = "blocked"
        _refresh()
        return

    tracker.register(session_id, "Consolidation")

    session_url = resp.get("url") or resp.get("session_url", "")
    if session_url:
        console.print(f"[bold]Consolidation session:[/bold] {session_url}")

    notified_needs_input = False
    consecutive_errors = 0

    # Poll until the session reaches a terminal state.
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            data = client.get_session(session_id)
        except Exception as exc:
            if _is_transient_error(exc) and consecutive_errors < _MAX_TRANSIENT_POLL_ERRORS:
                consecutive_errors += 1
                console.print(
                    f"[yellow]Consolidation poll error ({consecutive_errors}/"
                    f"{_MAX_TRANSIENT_POLL_ERRORS}): {exc} — will retry[/yellow]"
                )
                continue
            console.print(f"[red]Consolidation poll error: {exc} — marking blocked[/red]")
            phase_state.consolidation_status = "blocked"
            _refresh()
            return

        consecutive_errors = 0  # Reset on successful poll.

        phase_state.consolidation_status = map_session_to_batch_status(data)

        if phase_state.consolidation_status == "needs_input" and not notified_needs_input:
            notified_needs_input = True
            console.print(
                f"[yellow]\u26a0 Consolidation session is waiting for user input \u2192 {session_url}[/yellow]"
            )

        pr_url = extract_pr_url(data)
        if pr_url:
            phase_state.consolidation_pr_url = pr_url

        _refresh()

        if is_terminal_status(data):
            break

    # Retry PR URL discovery if not found on terminal poll.
    if not phase_state.consolidation_pr_url and phase_state.consolidation_status == "complete":
        console.print(
            "[yellow]Consolidation completed but PR URL not found yet — "
            "retrying discovery…[/yellow]"
        )
        discovered = _discover_pr_url(client, session_id, "Consolidation")
        if discovered:
            phase_state.consolidation_pr_url = discovered
            _refresh()


# ------------------------------------------------------------------
# Tier-by-tier parallel batch runner (existing logic, updated refresh)
# ------------------------------------------------------------------


def run_tier(
    client: DevinClient,
    plan: dict,
    tier: int,
    playbook_id: str,
    frontend_repo_name: str,
    max_parallel: int,
    start_time: float,
    phase_state: PhaseState,
    tracker: SessionTracker,
    live: Live,
) -> None:
    """Launch and monitor all batches in a single tier.

    Sessions are launched up to *max_parallel* at a time.  As sessions
    finish, new ones are started until every batch in the tier is done.

    Args:
        client: Initialised Devin API client.
        plan: The full migration plan (mutated in-place).
        tier: The tier number to run.
        playbook_id: ID of the created playbook.
        frontend_repo_name: Devin repo identifier for the frontend repo.
        max_parallel: Maximum concurrent sessions within the tier.
        start_time: Wall-clock start time (from ``time.time()``).
        phase_state: Shared mutable phase state.
        tracker: Session tracker for Ctrl+C cleanup.
        live: Active ``rich.live.Live`` context for refreshing the table.
    """
    tier_batches = get_batches_by_tier(plan, tier)
    if not tier_batches:
        return

    _refresh = _make_refresh(plan, start_time, phase_state, live)

    # Track which batches still need launching and which are in-flight.
    pending = list(tier_batches)
    active: dict[str, dict] = {}  # session_id -> batch
    notified_needs_input: set[str] = set()  # session_ids already warned about
    poll_error_counts: dict[str, int] = {}  # session_id -> consecutive error count

    def _launch(batch: dict) -> None:
        """Create a Devin session for *batch* and mark it running."""
        try:
            resp = client.create_session(
                prompt=build_batch_prompt(batch),
                playbook_id=playbook_id,
                repos=[frontend_repo_name],
                tags=["ts-migration", f"batch-{batch['name']}"],
                title=f"ShopDirect TS Migration: {batch['name']}",
                max_acu_limit=10,
            )
            session_id = resp.get("session_id") or resp.get("id")
            if not session_id:
                console.print(
                    f"[red]API returned no session ID for {batch['name']} — marking blocked[/red]"
                )
                batch["status"] = "blocked"
                return
            session_url = resp.get("url") or resp.get("session_url", "")
            batch["status"] = "running"
            batch["session_id"] = session_id
            if session_url:
                batch["session_url"] = session_url
            active[session_id] = batch
            tracker.register(session_id, f"Batch: {batch['name']}")
        except Exception as exc:
            console.print(f"[red]Failed to create session for {batch['name']}: {exc}[/red]")
            batch["status"] = "blocked"
        _refresh()

    # Seed initial sessions up to max_parallel.
    while pending and len(active) < max_parallel:
        _launch(pending.pop(0))

    # Poll until every session is done.
    while active:
        time.sleep(_POLL_INTERVAL_SECONDS)

        finished_ids: list[str] = []
        for session_id, batch in active.items():
            try:
                data = client.get_session(session_id)
            except Exception as exc:
                err_count = poll_error_counts.get(session_id, 0) + 1
                poll_error_counts[session_id] = err_count
                if _is_transient_error(exc) and err_count < _MAX_TRANSIENT_POLL_ERRORS:
                    console.print(
                        f"[yellow]Poll error for {batch['name']} ({err_count}/"
                        f"{_MAX_TRANSIENT_POLL_ERRORS}): {exc} — will retry[/yellow]"
                    )
                    continue
                console.print(f"[red]Poll error for {batch['name']}: {exc} — marking blocked[/red]")
                batch["status"] = "blocked"
                batch["error"] = str(exc)
                finished_ids.append(session_id)
                continue

            poll_error_counts[session_id] = 0  # Reset on successful poll.

            batch["status"] = map_session_to_batch_status(data)

            if batch["status"] == "needs_input" and session_id not in notified_needs_input:
                notified_needs_input.add(session_id)
                session_url = batch.get("session_url", "")
                console.print(
                    f"[yellow]\u26a0 {batch['name']} is waiting for user input \u2192 {session_url}[/yellow]"
                )

            pr_url = extract_pr_url(data)
            if pr_url:
                batch["pr_url"] = pr_url

            if is_terminal_status(data):
                finished_ids.append(session_id)

        for sid in finished_ids:
            batch = active.pop(sid)
            # Retry PR URL discovery for completed batches if not found.
            if batch["status"] == "complete" and not batch.get("pr_url"):
                discovered = _discover_pr_url(client, sid, batch["name"])
                if discovered:
                    batch["pr_url"] = discovered

        # Backfill new sessions.
        while pending and len(active) < max_parallel:
            _launch(pending.pop(0))

        _refresh()


# ------------------------------------------------------------------
# Ctrl+C cleanup
# ------------------------------------------------------------------


def _handle_interrupt(client: DevinClient, tracker: SessionTracker) -> None:
    """Prompt the user and optionally terminate all tracked sessions.

    Args:
        client: Initialised Devin API client.
        tracker: Contains all session IDs launched during this run.
    """
    console.print()
    console.print("[bold red]Cancelling...[/bold red]")

    if not tracker.sessions:
        console.print("[dim]No running sessions to clean up.[/dim]")
        raise SystemExit(1)

    console.print(f"[bold]Stop all {len(tracker.sessions)} running Devin session(s)? (y/n)[/bold]")
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"

    if answer == "y":
        tracker.terminate_all(client)
    else:
        console.print("[dim]Sessions left running:[/dim]")
        for session_id, label in tracker.sessions.items():
            console.print(f"  • {label} ({session_id})")

    raise SystemExit(1)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ShopDirect TypeScript Migration Orchestrator",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Local path to the frontend repo to scan.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show the migration plan and exit without running sessions.",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=3,
        help="Max concurrent Devin sessions within a tier (default: 3).",
    )
    parser.add_argument(
        "--frontend-repo-name",
        default=None,
        help="Devin repo identifier for the frontend repo (required unless --dry-run).",
    )
    parser.add_argument(
        "--no-auto-merge",
        action="store_true",
        default=False,
        help=(
            "Disable automatic PR merging between phases. "
            "When set, the orchestrator pauses and waits for the user "
            "to merge PRs manually before continuing."
        ),
    )
    return parser.parse_args()


def main() -> None:
    """Entry-point for the migration orchestrator.

    Executes three phases in order:

    1. **Foundation** — creates shared canonical types and TS config.
    2. **Parallel migration** — migrates batches tier-by-tier.
    3. **Consolidation** — reconciles cross-batch type inconsistencies.

    Merge gates between phases ensure PRs are merged before dependent
    phases begin.  By default PRs are auto-merged via the GitHub API;
    pass ``--no-auto-merge`` to pause for manual review instead.
    Ctrl+C triggers a cleanup prompt that can terminate all running
    Devin sessions.
    """
    args = _parse_args()

    # Enable logging so github_client debug output is visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1. Scan
    console.print(f"[bold]Scanning[/bold] {args.repo} …")
    plan = scan_and_plan(args.repo)

    if not plan["batches"]:
        console.print("[yellow]No JS/JSX files found — nothing to migrate.[/yellow]")
        return

    # 2. Show initial plan
    phase_state = PhaseState()
    start_time = time.time()
    console.print(
        build_progress_table(
            plan,
            0,
            foundation_status=phase_state.foundation_status,
            consolidation_status=phase_state.consolidation_status,
        )
    )

    # 3. Dry-run exit
    if args.dry_run:
        console.print("[dim]Dry-run mode — exiting.[/dim]")
        return

    # 4. Validate required args for live run
    if args.max_parallel < 1:
        console.print("[red]--max-parallel must be at least 1.[/red]")
        raise SystemExit(1)

    if not args.frontend_repo_name:
        console.print("[red]--frontend-repo-name is required for live runs.[/red]")
        raise SystemExit(1)

    # 5. Init client, playbook, tracker & optional GitHub client
    client = DevinClient.from_env()
    tracker = SessionTracker()

    gh: GitHubClient | None = None
    if not args.no_auto_merge:
        try:
            gh = GitHubClient.from_env()
            console.print("[green]GitHub client initialised — PRs will be auto-merged.[/green]")
        except RuntimeError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            console.print("[yellow]Falling back to manual merge gates.[/yellow]")
            args.no_auto_merge = True

    # Validate the token can access the target frontend repo before
    # spending time on foundation.  The repo name is "owner/repo".
    if gh is not None and args.frontend_repo_name:
        parts = args.frontend_repo_name.split("/", 1)
        if len(parts) == 2:
            try:
                gh.validate_repo_access(parts[0], parts[1])
                console.print(
                    f"[green]GitHub token verified — has access to "
                    f"{args.frontend_repo_name}.[/green]"
                )
            except RuntimeError as exc:
                console.print(f"[bold red]{exc}[/bold red]")
                console.print("[yellow]Falling back to manual merge gates.[/yellow]")
                gh = None
                args.no_auto_merge = True

    console.print("[bold]Loading playbook …[/bold]")
    playbook_text = load_playbook()
    playbook_resp = client.create_playbook(
        name="ShopDirect TS Migration",
        instructions=playbook_text,
    )
    playbook_id = playbook_resp.get("playbook_id", playbook_resp.get("id", ""))
    console.print(f"[green]Playbook created:[/green] {playbook_id}")

    # 6. Execute three-phase migration with Ctrl+C handling
    tiers = get_all_tiers(plan)
    try:
        with Live(
            build_progress_table(
                plan,
                0,
                foundation_status=phase_state.foundation_status,
                consolidation_status=phase_state.consolidation_status,
            ),
            console=console,
            refresh_per_second=1,
        ) as live:
            # ── Phase 0 — Foundation ──
            console.print("[bold blue]\u25b6 Phase 0: Foundation[/bold blue]")
            run_foundation_phase(
                client=client,
                playbook_id=playbook_id,
                frontend_repo_name=args.frontend_repo_name,
                plan=plan,
                start_time=start_time,
                phase_state=phase_state,
                tracker=tracker,
                live=live,
            )

            # Abort if foundation failed or was blocked.
            if phase_state.foundation_status in {"blocked", "needs_input"}:
                console.print(
                    "[bold red]Foundation phase did not complete successfully "
                    f"(status: {phase_state.foundation_status}). "
                    "Aborting migration — parallel batches depend on the "
                    "shared types created by foundation.[/bold red]"
                )
                sys.exit(1)

            # Merge gate — always pause after foundation completes.
            live.stop()
            print(
                f"\n>>> Foundation merge gate: "
                f"status={phase_state.foundation_status} "
                f"pr_url={phase_state.foundation_pr_url} "
                f"no_auto_merge={args.no_auto_merge}"
            )
            if phase_state.foundation_pr_url:
                if args.no_auto_merge:
                    _wait_for_merge_manual(
                        [phase_state.foundation_pr_url],
                        "Foundation PR is ready. Please review and merge it, "
                        "then press Enter to continue…",
                    )
                else:
                    assert gh is not None
                    ok = _auto_merge_prs(gh, [phase_state.foundation_pr_url], "foundation")
                    if not ok:
                        print(
                            "\n>>> Auto-merge FAILED for foundation PR. "
                            "Please merge it manually, then press Enter to continue…"
                        )
                        console.print(
                            "[bold red]Auto-merge failed for foundation PR. "
                            "Please merge it manually, then press Enter…[/bold red]"
                        )
                        input()
            else:
                print(
                    "\n>>> Foundation completed but no PR URL was detected. "
                    "Check the session for a PR, merge it, then press Enter…"
                )
                console.print(
                    "[bold yellow]Foundation completed but no PR URL was detected.[/bold yellow]"
                )
                console.print(
                    "[yellow]Batch sessions need the foundation changes merged into the "
                    "default branch. Check the foundation session for a PR, merge it, "
                    "then press Enter to continue…[/yellow]"
                )
                input()
            live.start()

            # ── Phase 1–2 — Parallel migration batches (tier by tier) ──
            console.print("[bold blue]\u25b6 Phase 1\u20132: Parallel Migration Batches[/bold blue]")
            for tier in tiers:
                run_tier(
                    client=client,
                    plan=plan,
                    tier=tier,
                    playbook_id=playbook_id,
                    frontend_repo_name=args.frontend_repo_name,
                    max_parallel=args.max_parallel,
                    start_time=start_time,
                    phase_state=phase_state,
                    tracker=tracker,
                    live=live,
                )

            # Merge gate — always pause after batches complete.
            # Collect PR URLs from ALL batches that have one, regardless of
            # status.  A batch marked "blocked" (e.g. by a transient API
            # error) may still have opened a perfectly valid PR.
            batch_pr_urls = [
                b["pr_url"] for b in plan["batches"]
                if b.get("pr_url")
            ]
            blocked_batches = [
                b for b in plan["batches"] if b["status"] == "blocked"
            ]
            completed_batches = [
                b for b in plan["batches"] if b["status"] == "complete"
            ]
            live.stop()
            print(
                f"\n>>> Batch merge gate: "
                f"{len(batch_pr_urls)} PR URL(s) found, "
                f"{len(completed_batches)} complete, "
                f"{len(blocked_batches)} blocked, "
                f"no_auto_merge={args.no_auto_merge}"
            )
            if blocked_batches:
                blocked_names = ", ".join(b["name"] for b in blocked_batches)
                blocked_with_prs = [b for b in blocked_batches if b.get("pr_url")]
                console.print(
                    f"[yellow]⚠ {len(blocked_batches)} batch(es) marked blocked: "
                    f"{blocked_names}[/yellow]"
                )
                if blocked_with_prs:
                    console.print(
                        f"[yellow]  {len(blocked_with_prs)} of these still have "
                        f"PR(s) that will be included in the merge.[/yellow]"
                    )
            if batch_pr_urls:
                if args.no_auto_merge:
                    _wait_for_merge_manual(
                        batch_pr_urls,
                        "All batch PRs are ready. Please review and merge them, "
                        "then press Enter to continue consolidation…",
                    )
                else:
                    assert gh is not None
                    ok = _auto_merge_prs(gh, batch_pr_urls, "batch")
                    if not ok:
                        print(
                            "\n>>> Auto-merge FAILED for one or more batch PRs. "
                            "Please merge them manually, then press Enter to continue…"
                        )
                        console.print(
                            "[bold red]Auto-merge failed for one or more batch PRs. "
                            "Please merge them manually, then press Enter…[/bold red]"
                        )
                        input()
            else:
                if completed_batches or blocked_batches:
                    console.print(
                        "[bold yellow]Batch sessions finished but no PR URLs "
                        "were detected.[/bold yellow]"
                    )
                    console.print(
                        "[yellow]Consolidation needs the batch changes merged. "
                        "Check batch sessions for PRs, merge them, then press "
                        "Enter to continue…[/yellow]"
                    )
                    input()
            live.start()

            # ── Phase 3 — Consolidation ──
            console.print("[bold blue]\u25b6 Phase 3: Consolidation[/bold blue]")
            run_consolidation_phase(
                client=client,
                playbook_id=playbook_id,
                frontend_repo_name=args.frontend_repo_name,
                plan=plan,
                start_time=start_time,
                phase_state=phase_state,
                tracker=tracker,
                live=live,
            )

    except KeyboardInterrupt:
        _handle_interrupt(client, tracker)

    # 7. Final summary
    elapsed = time.time() - start_time
    console.print()
    console.print(
        build_progress_table(
            plan,
            elapsed,
            foundation_status=phase_state.foundation_status,
            foundation_pr_url=phase_state.foundation_pr_url,
            consolidation_status=phase_state.consolidation_status,
            consolidation_pr_url=phase_state.consolidation_pr_url,
        )
    )
    console.print()

    # Foundation summary
    f_label = phase_state.foundation_status
    f_pr = phase_state.foundation_pr_url or "\u2014"
    console.print(f"[bold]Foundation:[/bold] {f_label} \u2192 {f_pr}")

    # Batch summary
    completed = [b for b in plan["batches"] if b["status"] == "complete"]
    blocked = [b for b in plan["batches"] if b["status"] == "blocked"]

    console.print(f"[bold green]Completed batches:[/bold green] {len(completed)}")
    for b in completed:
        pr = b.get("pr_url", "\u2014")
        console.print(f"  \u2022 {b['name']} \u2192 {pr}")

    if blocked:
        console.print(f"[bold red]Blocked batches:[/bold red] {len(blocked)}")
        for b in blocked:
            console.print(f"  \u2022 {b['name']}")

    # Consolidation summary
    c_label = phase_state.consolidation_status
    c_pr = phase_state.consolidation_pr_url or "\u2014"
    console.print(f"[bold]Consolidation:[/bold] {c_label} \u2192 {c_pr}")

    console.print(f"\n[dim]Total time: {int(elapsed)}s[/dim]")


if __name__ == "__main__":
    main()
