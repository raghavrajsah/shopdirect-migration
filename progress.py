"""Terminal progress display for the migration plan using rich."""

from __future__ import annotations

from rich.table import Table

# Status label mapping with emoji indicators.
_STATUS_LABELS: dict[str, str] = {
    "queued": "\u23f3 QUEUED",
    "running": "\U0001f504 RUNNING",
    "complete": "\u2705 COMPLETE",
    "blocked": "\u274c BLOCKED",
    "needs_input": "\u26a0\ufe0f NEEDS INPUT",
    "skipped": "\u23f8\ufe0f SKIPPED",
}

# Phase display names shown in the progress table.
_PHASE_LABELS: dict[str, str] = {
    "foundation": "Phase 0 \u2014 Foundation",
    "migration": "Phase 1\u20132 \u2014 Parallel Migration",
    "consolidation": "Phase 3 \u2014 Consolidation",
}


def format_elapsed(elapsed_seconds: float) -> str:
    """Format an elapsed duration as ``Mm Ss``.

    Args:
        elapsed_seconds: Total elapsed time in seconds.

    Returns:
        A human-readable string like ``1m 5s``.
    """
    total = max(0, int(elapsed_seconds))
    minutes = total // 60
    seconds = total % 60
    return f"{minutes}m {seconds}s"


def build_progress_table(
    plan: dict,
    elapsed_seconds: float,
    *,
    foundation_status: str = "queued",
    foundation_pr_url: str | None = None,
    consolidation_status: str = "queued",
    consolidation_pr_url: str | None = None,
) -> Table:
    """Build a rich :class:`Table` showing per-batch migration progress.

    The table includes rows for the foundation phase, each migration batch,
    and the consolidation phase so the user can see all three stages at once.

    Args:
        plan: A migration plan dict (as produced by :func:`scanner.scan_and_plan`).
            Each entry in ``plan["batches"]`` should contain ``name``, ``tier``,
            ``file_count``, ``status``, and optionally ``pr_url``.
        elapsed_seconds: Wall-clock seconds since the migration started.
        foundation_status: Current status of the foundation phase.
        foundation_pr_url: PR URL opened by the foundation phase, if any.
        consolidation_status: Current status of the consolidation phase.
        consolidation_pr_url: PR URL opened by the consolidation phase, if any.

    Returns:
        A :class:`rich.table.Table` ready to be printed with
        :func:`rich.print` or a :class:`rich.console.Console`.
    """
    batches: list[dict] = plan.get("batches", [])

    total_files = sum(b["file_count"] for b in batches)
    completed_files = sum(b["file_count"] for b in batches if b["status"] == "complete")
    completed_batches = sum(1 for b in batches if b["status"] == "complete")
    total_batches = len(batches)

    table = Table(title="ShopDirect TS Migration \u2014 Progress")

    table.add_column("Phase / Batch", style="cyan", no_wrap=True)
    table.add_column("Tier", justify="center")
    table.add_column("Files", justify="right")
    table.add_column("Status", no_wrap=True)
    table.add_column("PR")

    # --- Foundation row ---
    table.add_row(
        _PHASE_LABELS["foundation"],
        "\u2014",
        "\u2014",
        _STATUS_LABELS.get(foundation_status, foundation_status.upper()),
        foundation_pr_url or "\u2014",
    )

    table.add_section()

    # --- Migration batch rows ---
    for batch in batches:
        status_raw = batch["status"]
        status_label = _STATUS_LABELS.get(status_raw, status_raw.upper())
        pr_url = batch.get("pr_url", "\u2014")

        table.add_row(
            batch["name"],
            str(batch["tier"]),
            str(batch["file_count"]),
            status_label,
            pr_url,
        )

    table.add_section()

    # --- Consolidation row ---
    table.add_row(
        _PHASE_LABELS["consolidation"],
        "\u2014",
        "\u2014",
        _STATUS_LABELS.get(consolidation_status, consolidation_status.upper()),
        consolidation_pr_url or "\u2014",
    )

    elapsed_str = format_elapsed(elapsed_seconds)
    table.caption = (
        f"Files: {completed_files}/{total_files} | "
        f"Batches: {completed_batches}/{total_batches} | "
        f"Elapsed: {elapsed_str}"
    )

    return table
