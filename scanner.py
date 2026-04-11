"""Scan a frontend codebase and group JS/JSX files into tiered migration batches."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

# Tier assignments for well-known top-level folders under src/.
_TIER_MAP: dict[str, int] = {
    "utils": 1,
    "constants": 1,
    "data": 1,
    "mocks": 1,
    "hooks": 2,
    "services": 2,
    "contexts": 2,
    "components": 3,
    "pages": 4,
}

_DEFAULT_TIER = 2
_JS_EXTENSIONS = {".js", ".jsx"}


def scan_and_plan(repo_path: str) -> dict:
    """Scan ``src/`` for JS/JSX files and produce a tiered migration plan.

    Args:
        repo_path: Absolute or relative path to the repository root.

    Returns:
        A dict containing ``repo_path``, ``src_path``, ``total_files``,
        and a sorted list of ``batches``.

    Raises:
        FileNotFoundError: If *repo_path* or its ``src/`` subdirectory
            does not exist.
    """
    root = Path(repo_path)
    if not root.exists():
        raise FileNotFoundError(f"Repository path does not exist: {repo_path}")

    src = root / "src"
    if not src.exists():
        raise FileNotFoundError(f"src/ directory does not exist: {src}")

    # Collect files grouped by their top-level folder under src/.
    groups: dict[str, list[str]] = defaultdict(list)

    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in _JS_EXTENSIONS:
            continue

        # Determine the top-level folder name relative to src/.
        rel = path.relative_to(src)
        batch_name = rel.parts[0] if len(rel.parts) > 1 else "_root"
        groups[batch_name].append(rel.as_posix())

    # Build sorted batch list.
    batches: list[dict] = []
    for name in sorted(groups):
        tier = _TIER_MAP.get(name, _DEFAULT_TIER)
        files = sorted(f"src/{f}" for f in groups[name])
        batches.append(
            {
                "name": name,
                "tier": tier,
                "files": files,
                "file_count": len(files),
                "status": "queued",
            }
        )

    # Sort batches by tier first, then by name.
    batches.sort(key=lambda b: (b["tier"], b["name"]))

    total = sum(b["file_count"] for b in batches)

    return {
        "repo_path": str(root),
        "src_path": str(src),
        "total_files": total,
        "batches": batches,
    }


def get_batches_by_tier(plan: dict, tier: int) -> list[dict]:
    """Return only the batches that belong to the given *tier*.

    Args:
        plan: A migration plan produced by :func:`scan_and_plan`.
        tier: The dependency tier to filter on (1–4).

    Returns:
        A list of batch dicts matching the requested tier.
    """
    return [b for b in plan["batches"] if b["tier"] == tier]


def get_all_tiers(plan: dict) -> list[int]:
    """Return a sorted list of unique tiers present in the plan.

    Args:
        plan: A migration plan produced by :func:`scan_and_plan`.

    Returns:
        Sorted list of tier numbers.
    """
    return sorted({b["tier"] for b in plan["batches"]})
