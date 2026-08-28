#!/usr/bin/env python3
"""Build/check three non-research disaster-v1 GPU smoke configs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.build_disaster_matrix import build_config, canonical_bytes  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "configs" / "engineering_disaster_smoke_v1"
CASES = (
    ("free_text", "a"),
    ("structured_warning", "b"),
    ("communication_none", "a"),
)


def build_files() -> dict[str, bytes]:
    files = {}
    rows = []
    for ordinal, (mode, slot) in enumerate(CASES, start=1):
        config = build_config("mixed", mode, 42, slot)
        run_id = f"engineering-disaster-{mode}-24x60-s42-20260823-r001"
        config["simulation"].update({
            "run_id": run_id,
            "run_name": run_id,
            "protocol_version": "engineering-disaster-smoke-v1.0.0",
            "research_eligible": False,
        })
        filename = f"{run_id}.json"
        payload = canonical_bytes(config)
        files[filename] = payload
        calls = 1440 if mode == "communication_none" else 2880
        rows.append({
            "ordinal": ordinal,
            "filename": filename,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "run_id": run_id,
            "worker_slot": slot,
            "communication_mode": mode,
            "expected_logical_llm_calls": calls,
        })
    manifest = {
        "schema_version": "engineering-disaster-smoke-matrix-v1.0.0",
        "research_eligible": False,
        "seed": 42,
        "planned_runs": 3,
        "planned_logical_llm_calls": 7200,
        "rows": rows,
    }
    files["manifest.json"] = canonical_bytes(manifest)
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = build_files()
    if args.check:
        if {path.name for path in OUTPUT_DIR.glob("*.json")} != set(files):
            raise SystemExit("smoke config file set differs")
        for filename, payload in files.items():
            if (OUTPUT_DIR / filename).read_bytes() != payload:
                raise SystemExit(f"smoke config differs: {filename}")
        print("PASS: three smoke configs are byte-identical")
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, payload in files.items():
        (OUTPUT_DIR / filename).write_bytes(payload)
    print("wrote three smoke configs plus manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
