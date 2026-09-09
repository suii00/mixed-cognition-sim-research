"""Shared read-only checks for immutable artifact ancestry."""

from __future__ import annotations

from pathlib import Path


SINGLE_FILE_COMPLETION_MARKERS = (
    "run_meta.json",
    "batch_manifest.json",
    "aggregate_manifest.json",
)
PROBE_COMPLETION_MARKERS = (
    "artifact_manifest.json",
    "probe_meta.json",
)


def is_within(path: Path, parent: Path) -> bool:
    """Return whether ``path`` equals or is nested under ``parent``."""
    return path == parent or parent in path.parents


def is_allowed_derived_output_root(path: Path, *, repo_root: Path) -> bool:
    """Allow repository-local derived output only below ``derived/``.

    External output roots remain valid.  Both arguments are resolved here so
    existing symlink components cannot bypass the repository-local boundary.
    """
    resolved_path = path.resolve(strict=False)
    resolved_repo = repo_root.resolve(strict=True)
    if not is_within(resolved_path, resolved_repo):
        return True
    return is_within(resolved_path, resolved_repo / "derived")


def find_immutable_artifact_ancestor(path: Path) -> Path | None:
    """Find a simulator run, probe, direction batch, or aggregate ancestor."""
    for candidate in (path, *path.parents):
        if candidate.name.startswith("output_"):
            return candidate
        if any(
            (candidate / marker).is_file()
            for marker in SINGLE_FILE_COMPLETION_MARKERS
        ):
            return candidate
        if all(
            (candidate / marker).is_file()
            for marker in PROBE_COMPLETION_MARKERS
        ):
            return candidate
    return None
