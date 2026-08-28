"""Verify the exact lifecycle contract for the public 15-step Ollama smoke."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from validate_run import validate_run


def resolve_run(path: Path) -> Path:
    if (path / "run_meta.json").is_file():
        return path
    candidates = sorted(child for child in path.glob("output_*") if child.is_dir())
    if len(candidates) != 1:
        raise ValueError(f"expected exactly one output_* directory below {path}; found {len(candidates)}")
    return candidates[0]


def verify_smoke(path: Path, expected_steps: int, expected_agents: int) -> list[str]:
    run = resolve_run(path)
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    expected = {
        "status": "completed",
        "aborted": False,
        "expected_steps": expected_steps,
        "completed_steps": expected_steps,
        "expected_agents": expected_agents,
        "observed_agents": expected_agents,
        "raw_manifest_status": "available",
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            errors.append(f"{key}: expected {value!r}, got {meta.get(key)!r}")
    report = validate_run(run, strict=True)
    if not report.valid:
        errors.extend(f"strict validator: {message}" for message in report.errors)
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="run directory or its unique output root")
    parser.add_argument("--steps", type=int, default=15)
    parser.add_argument("--agents", type=int, default=6)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        errors = verify_smoke(args.path.resolve(), args.steps, args.agents)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {type(error).__name__}: {error}")
        return 2
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print(
        f"PASS: completed, non-aborted, {args.steps}/{args.steps} steps, "
        f"{args.agents} agents, raw manifest available, strict validation passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
