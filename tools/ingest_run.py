"""Byte-copy an already public-safe run into the tracked runs directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.provenance import normalize_run_id  # noqa: E402
from tools.scan_publication import scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


def _file_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            raise ValueError("embedded Git metadata is forbidden in a run")
        if path.is_symlink():
            raise ValueError(f"symbolic links are forbidden: {relative}")
        if path.is_file():
            hashes[relative.as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def ingest_run(source: Path, runs_root: Path) -> Path:
    source = source.resolve()
    runs_root = runs_root.resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source must be a real run directory")
    if runs_root == source or source in runs_root.parents:
        raise ValueError("runs root must not be inside the source run")
    meta = json.loads((source / "run_meta.json").read_text(encoding="utf-8"))
    run_id = normalize_run_id(meta.get("run_id"))
    expected_name = f"output_{run_id}"
    if source.name != expected_name:
        raise ValueError(f"source directory must be named {expected_name!r}")

    report = validate_run(source, strict=True)
    if not report.valid:
        raise ValueError("run integrity validation failed: " + "; ".join(report.errors))
    findings = scan_tree(source)
    if findings:
        first = findings[0]
        raise ValueError(
            f"publication scan failed at {first.path}:{first.line} "
            f"({first.pattern_id})"
        )

    source_hashes = _file_hashes(source)
    destination = runs_root / expected_name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"destination already exists: {destination}")
    runs_root.mkdir(parents=True, exist_ok=True)
    temporary = runs_root / f".ingest-{run_id}-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        if _file_hashes(temporary) != source_hashes:
            raise RuntimeError("copied bytes differ from source")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "runs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        destination = ingest_run(args.source, args.runs_root)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    print(f"PASS: copied immutable run bytes to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
