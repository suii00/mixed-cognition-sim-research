#!/usr/bin/env python3
"""Build or verify the fresh r003 three-model observability probe config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
R002_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r002.json"
)
R003_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r003.json"
)
R003_PROTOCOL_VERSION = "engineering-vllm-observability-probe-v2.0.2"
R003_RUN_ID = "engineering-vllm-observability-probe-3model-s2300-r003"


def build_r003_bytes() -> bytes:
    config = json.loads(R002_CONFIG.read_text(encoding="utf-8"))
    config["simulation"].update({
        "protocol_version": R003_PROTOCOL_VERSION,
        "run_id": R003_RUN_ID,
        "run_name": R003_RUN_ID,
    })
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_r003_bytes()
    if args.check:
        if not R003_CONFIG.is_file() or R003_CONFIG.read_bytes() != expected:
            raise SystemExit("r003 observability probe config differs from generator")
        action = "verified"
    else:
        if R003_CONFIG.exists():
            raise SystemExit(f"refusing to overwrite existing config: {R003_CONFIG}")
        R003_CONFIG.write_bytes(expected)
        action = "wrote"
    print(f"{action}: {R003_CONFIG.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
