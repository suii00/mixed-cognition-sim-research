#!/usr/bin/env python3
"""Build or verify the fresh r002 three-model observability probe config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
R001_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r001.json"
)
R002_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r002.json"
)
R002_PROTOCOL_VERSION = "engineering-vllm-observability-probe-v2.0.1"
R002_RUN_ID = "engineering-vllm-observability-probe-3model-s2300-r002"


def build_r002_bytes() -> bytes:
    config = json.loads(R001_CONFIG.read_text(encoding="utf-8"))
    config["simulation"].update({
        "protocol_version": R002_PROTOCOL_VERSION,
        "run_id": R002_RUN_ID,
        "run_name": R002_RUN_ID,
    })
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_r002_bytes()
    if args.check:
        if not R002_CONFIG.is_file() or R002_CONFIG.read_bytes() != expected:
            raise SystemExit("r002 observability probe config differs from generator")
        action = "verified"
    else:
        if R002_CONFIG.exists():
            raise SystemExit(f"refusing to overwrite existing config: {R002_CONFIG}")
        R002_CONFIG.write_bytes(expected)
        action = "wrote"
    print(f"{action}: {R002_CONFIG.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
