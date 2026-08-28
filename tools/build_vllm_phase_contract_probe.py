#!/usr/bin/env python3
"""Build the public-native three-model Phase 3 contract probe config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r003.json"
)
OUTPUT_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_phase_contract_probe_3model_20260825_r002.json"
)
PROTOCOL_VERSION = "engineering-vllm-phase-contract-probe-v2.0.0"
RUN_ID = "engineering-vllm-phase-contract-probe-3model-20260825-r002"


def build_bytes() -> bytes:
    config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    for bloc in config["blocs"]:
        bloc.pop("llm_overrides", None)
        bloc["endpoint_id"] = f"phase-contract-{bloc['name']}"
    config["simulation"].update({
        "log_schema_version": "2.0.0",
        "metric_version": "engineering-phase-contract-probe-v2.0.0",
        "protocol_version": PROTOCOL_VERSION,
        "response_contract_version": "phase-response-v2.0.0",
        "run_id": RUN_ID,
        "run_name": RUN_ID,
        "seed": 2301,
    })
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_bytes()
    if args.check:
        if not OUTPUT_CONFIG.is_file() or OUTPUT_CONFIG.read_bytes() != expected:
            raise SystemExit("Phase contract probe config differs from generator")
        action = "verified"
    else:
        if OUTPUT_CONFIG.exists():
            raise SystemExit(f"refusing to overwrite existing config: {OUTPUT_CONFIG}")
        OUTPUT_CONFIG.write_bytes(expected)
        action = "wrote"
    print(f"{action}: {OUTPUT_CONFIG.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
