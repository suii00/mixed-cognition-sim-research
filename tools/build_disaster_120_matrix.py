#!/usr/bin/env python3
"""Build or check the prospective 120-step disaster pilot and formal matrices."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.build_disaster_matrix import (  # noqa: E402
    COMPOSITIONS,
    METRIC_VERSION,
    MODES,
    build_config as build_60_config,
    canonical_bytes,
)


DURATION = 120
PILOT_SEEDS = (2299,)
FORMAL_SEEDS = (2201, 2202, 2203, 2204, 2205)
PILOT_PROTOCOL_VERSION = "engineering-disaster-120-pilot-v2.0.0"
FORMAL_PROTOCOL_VERSION = "formal-disaster-120-protocol-v2.0.0"
PILOT_OUTPUT_DIR = REPO_ROOT / "configs" / "engineering_disaster_120_v1"
FORMAL_OUTPUT_DIR = REPO_ROOT / "configs" / "formal_disaster_120_v1"


def expected_calls(mode: str) -> int:
    decisions_per_step = 1 if mode == "communication_none" else 2
    return 24 * DURATION * decisions_per_step


def build_config(
    composition: str,
    mode: str,
    seed: int,
    slot: str,
    *,
    tier: str,
) -> dict:
    if tier not in {"pilot", "formal"}:
        raise ValueError("tier must be 'pilot' or 'formal'")
    config = build_60_config(composition, mode, seed, slot)
    prefix = "engineering-disaster-120-pilot" if tier == "pilot" else "disaster-120-v1"
    run_id = f"{prefix}-{composition}-{mode}-s{seed}-w{slot}"
    config["simulation"].update({
        "duration": DURATION,
        "protocol_version": (
            PILOT_PROTOCOL_VERSION if tier == "pilot" else FORMAL_PROTOCOL_VERSION
        ),
        "run_id": run_id,
        "run_name": run_id,
    })
    if tier == "pilot":
        config["simulation"]["research_eligible"] = False
    return config


def _build_matrix(*, tier: str, seeds: tuple[int, ...]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    rows = []
    ordinal = 0
    for seed in seeds:
        for composition in COMPOSITIONS:
            for mode in MODES:
                slot = "a" if ordinal % 2 == 0 else "b"
                config = build_config(composition, mode, seed, slot, tier=tier)
                filename = f"{config['simulation']['run_id']}.json"
                payload = canonical_bytes(config)
                files[filename] = payload
                rows.append({
                    "ordinal": ordinal + 1,
                    "filename": filename,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "run_id": config["simulation"]["run_id"],
                    "worker_slot": slot,
                    "seed": seed,
                    "composition": composition,
                    "communication_mode": mode,
                    "expected_logical_llm_calls": expected_calls(mode),
                })
                ordinal += 1

    is_pilot = tier == "pilot"
    manifest = {
        "schema_version": f"{tier}-disaster-120-matrix-v1.0.0",
        "protocol_version": (
            PILOT_PROTOCOL_VERSION if is_pilot else FORMAL_PROTOCOL_VERSION
        ),
        "metric_version": METRIC_VERSION,
        "duration": DURATION,
        "research_eligible": not is_pilot,
        "seeds": list(seeds),
        "planned_runs": len(rows),
        "planned_logical_llm_calls": sum(
            row["expected_logical_llm_calls"] for row in rows
        ),
        "rows": rows,
    }
    files["manifest.json"] = canonical_bytes(manifest)
    return files


def build_pilot_files() -> dict[str, bytes]:
    return _build_matrix(tier="pilot", seeds=PILOT_SEEDS)


def build_formal_files() -> dict[str, bytes]:
    return _build_matrix(tier="formal", seeds=FORMAL_SEEDS)


def _write_or_check(
    *, output_dir: Path, expected: dict[str, bytes], check: bool, label: str
) -> None:
    if check:
        actual_names = {path.name for path in output_dir.glob("*.json")}
        if actual_names != set(expected):
            raise SystemExit(f"{label} matrix file set differs from generator")
        for name, payload in expected.items():
            if (output_dir / name).read_bytes() != payload:
                raise SystemExit(f"{label} matrix file differs: {name}")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in expected.items():
        (output_dir / name).write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--tier", choices=("pilot", "formal", "all"), default="all")
    args = parser.parse_args()

    if args.tier in {"pilot", "all"}:
        _write_or_check(
            output_dir=PILOT_OUTPUT_DIR,
            expected=build_pilot_files(),
            check=args.check,
            label="pilot",
        )
    if args.tier in {"formal", "all"}:
        _write_or_check(
            output_dir=FORMAL_OUTPUT_DIR,
            expected=build_formal_files(),
            check=args.check,
            label="formal",
        )
    action = "verified" if args.check else "wrote"
    print(f"{action}: 120-step {args.tier} matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
