#!/usr/bin/env python3
"""Fail-closed runner for one worker slice of a frozen 120-step matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import (  # noqa: E402
    load_config,
    load_runtime_bindings,
    required_endpoint_ids,
)
from engine.provenance import atomic_write_json, utc_now_iso  # noqa: E402
from engine.sim import Simulation  # noqa: E402
from tools.build_disaster_120_matrix import (  # noqa: E402
    FORMAL_OUTPUT_DIR,
    FORMAL_SEEDS,
    PILOT_OUTPUT_DIR,
    PILOT_SEEDS,
    build_formal_files,
    build_pilot_files,
)
from tools.validate_run import validate_run  # noqa: E402


TIER_SPEC = {
    "pilot": {
        "output_dir": PILOT_OUTPUT_DIR,
        "seeds": PILOT_SEEDS,
        "build": build_pilot_files,
        "planned_runs": 12,
        "planned_calls": 57600,
        "research_eligible": False,
    },
    "formal": {
        "output_dir": FORMAL_OUTPUT_DIR,
        "seeds": FORMAL_SEEDS,
        "build": build_formal_files,
        "planned_runs": 60,
        "planned_calls": 288000,
        "research_eligible": True,
    },
}


def load_verified_manifest(tier: str) -> dict:
    spec = TIER_SPEC[tier]
    expected = spec["build"]()
    output_dir = spec["output_dir"]
    actual = {path.name for path in output_dir.glob("*.json")}
    if actual != set(expected):
        raise ValueError(f"{tier} matrix file set differs from prospective generator")
    for filename, payload in expected.items():
        if (output_dir / filename).read_bytes() != payload:
            raise ValueError(f"{tier} matrix content differs: {filename}")
    manifest = json.loads(expected["manifest.json"])
    if (
        manifest["planned_runs"] != spec["planned_runs"]
        or manifest["planned_logical_llm_calls"] != spec["planned_calls"]
        or manifest["research_eligible"] is not spec["research_eligible"]
    ):
        raise ValueError(f"{tier} matrix totals differ from the frozen envelope")
    return manifest


def git_preflight(source_sha: str) -> str:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    if head != source_sha:
        raise ValueError(f"source Git SHA mismatch: expected {source_sha}, got {head}")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout
    if dirty:
        raise ValueError("120-step execution requires a clean Git worktree")
    return head


def select_worker_rows(
    manifest: dict,
    *,
    slot: str,
    selected_seeds: tuple[int, ...],
) -> list[dict]:
    rows = [
        row for row in manifest["rows"]
        if row["worker_slot"] == slot and row["seed"] in selected_seeds
    ]
    expected_runs = 6 * len(selected_seeds)
    expected_calls = 28800 * len(selected_seeds)
    actual_calls = sum(row["expected_logical_llm_calls"] for row in rows)
    if len(rows) != expected_runs or actual_calls != expected_calls:
        raise ValueError(
            "worker slice differs from the frozen 120-step envelope: "
            f"expected {expected_runs} runs/{expected_calls} calls, "
            f"got {len(rows)} runs/{actual_calls} calls"
        )
    return rows


def run_worker(args: argparse.Namespace) -> int:
    spec = TIER_SPEC[args.tier]
    manifest = load_verified_manifest(args.tier)
    source_sha = git_preflight(args.source_git_sha)
    selected_seeds = tuple(int(value) for value in args.seeds.split(","))
    if not selected_seeds or len(set(selected_seeds)) != len(selected_seeds):
        raise ValueError("--seeds must contain unique predeclared seeds")
    if any(seed not in spec["seeds"] for seed in selected_seeds):
        raise ValueError(f"--seeds must be drawn from {spec['seeds']!r}")
    rows = select_worker_rows(
        manifest,
        slot=args.slot,
        selected_seeds=selected_seeds,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch = root / f"{args.tier}-disaster-120-{timestamp}-worker-{args.slot}"
    batch.mkdir(parents=False, exist_ok=False)
    runs = batch / "runs"
    runs.mkdir()
    state = {
        "schema_version": "disaster-120-worker-batch-v2.0.0",
        "status": "running",
        "tier": args.tier,
        "research_eligible": spec["research_eligible"],
        "worker_slot": args.slot,
        "seeds": list(selected_seeds),
        "approval_reference": args.approval_reference,
        "source_git_sha": source_sha,
        "start_time_utc": utc_now_iso(),
        "end_time_utc": None,
        "planned_runs": len(rows),
        "planned_logical_llm_calls": sum(
            row["expected_logical_llm_calls"] for row in rows
        ),
        "completed_runs": 0,
        "runs": [],
    }
    atomic_write_json(batch / "batch_meta.json", state)
    try:
        for row in rows:
            path = spec["output_dir"] / row["filename"]
            if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError(f"config hash mismatch: {row['filename']}")
            config = load_config(str(path))
            bindings = load_runtime_bindings(
                args.runtime_bindings, required_endpoint_ids(config)
            )
            simulation = Simulation(
                config,
                output_root=runs,
                repo_root=REPO_ROOT,
                runtime_bindings=bindings,
            )
            simulation.run()
            validation = validate_run(Path(simulation.output_dir), strict=True)
            state["runs"].append({
                "run_id": row["run_id"],
                "config_sha256": row["sha256"],
                "expected_logical_llm_calls": row["expected_logical_llm_calls"],
                "strict_valid": validation.valid,
                "strict_errors": validation.errors,
                "strict_unverifiable": validation.unverifiable,
            })
            if not validation.valid:
                raise RuntimeError(f"strict validation failed: {row['run_id']}")
            state["completed_runs"] += 1
            atomic_write_json(batch / "batch_meta.json", state)
    except BaseException as error:
        state["status"] = "failed"
        state["failure_type"] = type(error).__name__
        state["end_time_utc"] = utc_now_iso()
        atomic_write_json(batch / "batch_meta.json", state)
        raise
    state["status"] = "completed"
    state["end_time_utc"] = utc_now_iso()
    atomic_write_json(batch / "batch_meta.json", state)
    print(batch)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--tier", choices=tuple(TIER_SPEC), required=True)
    run = sub.add_parser("run-worker")
    run.add_argument("--tier", choices=tuple(TIER_SPEC), required=True)
    run.add_argument("--slot", choices=("a", "b"), required=True)
    run.add_argument("--seeds", required=True, help="comma-separated predeclared seeds")
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--source-git-sha", required=True)
    run.add_argument("--approval-reference", required=True)
    run.add_argument("--runtime-bindings", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "verify":
        manifest = load_verified_manifest(args.tier)
        print(
            f"PASS: {args.tier} {manifest['planned_runs']} runs, "
            f"{manifest['planned_logical_llm_calls']} logical calls"
        )
        return 0
    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
