"""Main orchestration script for the ShopDirect TypeScript Migration."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.live import Live

from devin_client import DevinClient
from progress import build_progress_table
from scanner import get_all_tiers, get_batches_by_tier, scan_and_plan

_POLL_INTERVAL_SECONDS = 30

console = Console()


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
        f"3. Update any directly necessary imports affected by the renames.\n"
        f"4. Run `npx tsc --noEmit` to verify the code compiles.\n"
        f"5. Run tests if a test runner is available.\n"
        f"6. If everything passes, open a PR with the changes.\n"
        f"7. Keep changes scoped to the listed files plus any directly "
        f"necessary import updates.\n"
        f"8. If you cannot complete cleanly, summarize the blockers in "
        f"your final message.\n"
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
        return "needs_input"
    return "running"


def extract_pr_url(session_data: dict) -> str | None:
    """Extract the first pull request URL from session data, if available.

    Args:
        session_data: Session details from the Devin API.

    Returns:
        A PR URL string, or ``None``.
    """
    pull_requests = session_data.get("pull_requests") or []
    if pull_requests:
        url = pull_requests[0].get("pr_url") or pull_requests[0].get("html_url")
        if url:
            return url

    structured = session_data.get("structured_output") or {}
    pr_url = structured.get("pr_url")
    if pr_url:
        return pr_url

    return None


def run_tier(
    client: DevinClient,
    plan: dict,
    tier: int,
    playbook_id: str,
    frontend_repo_name: str,
    max_parallel: int,
    start_time: float,
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
        live: Active ``rich.live.Live`` context for refreshing the table.
    """
    tier_batches = get_batches_by_tier(plan, tier)
    if not tier_batches:
        return

    # Track which batches still need launching and which are in-flight.
    pending = list(tier_batches)
    active: dict[str, dict] = {}  # session_id -> batch

    def _refresh() -> None:
        live.update(build_progress_table(plan, time.time() - start_time))

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
                console.print(f"[red]Poll error for {batch['name']}: {exc} — marking blocked[/red]")
                batch["status"] = "blocked"
                batch["error"] = str(exc)
                finished_ids.append(session_id)
                continue

            batch["status"] = map_session_to_batch_status(data)

            if batch["status"] == "needs_input":
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
            del active[sid]

        # Backfill new sessions.
        while pending and len(active) < max_parallel:
            _launch(pending.pop(0))

        _refresh()


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
    return parser.parse_args()


def main() -> None:
    """Entry-point for the migration orchestrator."""
    args = _parse_args()

    # 1. Scan
    console.print(f"[bold]Scanning[/bold] {args.repo} …")
    plan = scan_and_plan(args.repo)

    if not plan["batches"]:
        console.print("[yellow]No JS/JSX files found — nothing to migrate.[/yellow]")
        return

    # 2. Show initial plan
    start_time = time.time()
    console.print(build_progress_table(plan, 0))

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

    # 5. Init client & playbook
    client = DevinClient.from_env()
    console.print("[bold]Loading playbook …[/bold]")
    playbook_text = load_playbook()
    playbook_resp = client.create_playbook(
        name="ShopDirect TS Migration",
        instructions=playbook_text,
    )
    playbook_id = playbook_resp.get("playbook_id", playbook_resp.get("id", ""))
    console.print(f"[green]Playbook created:[/green] {playbook_id}")

    # 6. Execute tier by tier
    tiers = get_all_tiers(plan)
    with Live(build_progress_table(plan, 0), console=console, refresh_per_second=1) as live:
        for tier in tiers:
            run_tier(
                client=client,
                plan=plan,
                tier=tier,
                playbook_id=playbook_id,
                frontend_repo_name=args.frontend_repo_name,
                max_parallel=args.max_parallel,
                start_time=start_time,
                live=live,
            )

    # 7. Final summary
    elapsed = time.time() - start_time
    console.print()
    console.print(build_progress_table(plan, elapsed))
    console.print()

    completed = [b for b in plan["batches"] if b["status"] == "complete"]
    blocked = [b for b in plan["batches"] if b["status"] == "blocked"]

    console.print(f"[bold green]Completed:[/bold green] {len(completed)} batch(es)")
    for b in completed:
        pr = b.get("pr_url", "—")
        console.print(f"  • {b['name']} → {pr}")

    if blocked:
        console.print(f"[bold red]Blocked:[/bold red] {len(blocked)} batch(es)")
        for b in blocked:
            console.print(f"  • {b['name']}")

    console.print(f"\n[dim]Total time: {int(elapsed)}s[/dim]")


if __name__ == "__main__":
    main()
