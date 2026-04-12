"""Main orchestration script for the ShopDirect TypeScript Migration."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.live import Live

from devin_client import DevinClient
from progress import build_progress_table
from scanner import get_all_tiers, get_batches_by_tier, scan_and_plan

_POLL_INTERVAL_SECONDS = 30

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

    session_url = resp.get("url") or resp.get("session_url", "")
    if session_url:
        console.print(f"[bold]Foundation session:[/bold] {session_url}")

    notified_needs_input = False

    # Poll until the session reaches a terminal state.
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            data = client.get_session(session_id)
        except Exception as exc:
            console.print(f"[red]Foundation poll error: {exc} — marking blocked[/red]")
            phase_state.foundation_status = "blocked"
            _refresh()
            return

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

    session_url = resp.get("url") or resp.get("session_url", "")
    if session_url:
        console.print(f"[bold]Consolidation session:[/bold] {session_url}")

    notified_needs_input = False

    # Poll until the session reaches a terminal state.
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)

        try:
            data = client.get_session(session_id)
        except Exception as exc:
            console.print(f"[red]Consolidation poll error: {exc} — marking blocked[/red]")
            phase_state.consolidation_status = "blocked"
            _refresh()
            return

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
    """Entry-point for the migration orchestrator.

    Executes three phases in order:

    1. **Foundation** — creates shared canonical types and TS config.
    2. **Parallel migration** — migrates batches tier-by-tier.
    3. **Consolidation** — reconciles cross-batch type inconsistencies.
    """
    args = _parse_args()

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

    # 6. Execute three-phase migration
    tiers = get_all_tiers(plan)
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
        # Phase 0 — Foundation
        console.print("[bold blue]▶ Phase 0: Foundation[/bold blue]")
        run_foundation_phase(
            client=client,
            playbook_id=playbook_id,
            frontend_repo_name=args.frontend_repo_name,
            plan=plan,
            start_time=start_time,
            phase_state=phase_state,
            live=live,
        )

        # Phase 1–2 — Parallel migration batches (tier by tier)
        console.print("[bold blue]▶ Phase 1–2: Parallel Migration Batches[/bold blue]")
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
                live=live,
            )

        # Phase 3 — Consolidation
        console.print("[bold blue]▶ Phase 3: Consolidation[/bold blue]")
        run_consolidation_phase(
            client=client,
            playbook_id=playbook_id,
            frontend_repo_name=args.frontend_repo_name,
            plan=plan,
            start_time=start_time,
            phase_state=phase_state,
            live=live,
        )

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
    f_pr = phase_state.foundation_pr_url or "—"
    console.print(f"[bold]Foundation:[/bold] {f_label} → {f_pr}")

    # Batch summary
    completed = [b for b in plan["batches"] if b["status"] == "complete"]
    blocked = [b for b in plan["batches"] if b["status"] == "blocked"]

    console.print(f"[bold green]Completed batches:[/bold green] {len(completed)}")
    for b in completed:
        pr = b.get("pr_url", "—")
        console.print(f"  • {b['name']} → {pr}")

    if blocked:
        console.print(f"[bold red]Blocked batches:[/bold red] {len(blocked)}")
        for b in blocked:
            console.print(f"  • {b['name']}")

    # Consolidation summary
    c_label = phase_state.consolidation_status
    c_pr = phase_state.consolidation_pr_url or "—"
    console.print(f"[bold]Consolidation:[/bold] {c_label} → {c_pr}")

    console.print(f"\n[dim]Total time: {int(elapsed)}s[/dim]")


if __name__ == "__main__":
    main()
