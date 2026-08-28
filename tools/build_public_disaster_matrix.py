#!/usr/bin/env python3
"""Build or check the frozen public-by-construction 4 x 3 x 5 matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.response_contracts import (  # noqa: E402
    CANONICAL_RESPONSE_CONTRACT_VERSION,
)
from tools.build_disaster_matrix import (  # noqa: E402
    COMPOSITIONS,
    METRIC_VERSION,
    MODES,
    build_config as build_legacy_matrix_config,
    canonical_bytes,
)


OUTPUT_DIR = REPO_ROOT / "configs" / "public_formal_disaster_v3"
SEEDS = (3101, 3102, 3103, 3104, 3105)
PROTOCOL_VERSION = "formal-public-disaster-protocol-v3.0.0"
MATRIX_SCHEMA_VERSION = "formal-public-disaster-matrix-v1.0.0"
RESPONSE_CONTRACT_VERSION = CANONICAL_RESPONSE_CONTRACT_VERSION
LOG_SCHEMA_VERSION = "2.0.0"
DURATION = 60
AGENT_COUNT = 24

# The two logical worker lanes have independent Qwen and Llama replicas. Gemma
# is one tensor-parallel server shared by the two lanes because its 4096-token
# profile does not fit safely on one 24 GiB A5000. This is operational routing;
# bloc/model labels remain hidden from agent prompts.
SERVER_LAYOUT = (
    {
        "server_id": "qwen-worker-a",
        "model_name": "qwen",
        "logical_endpoint_ids": ["worker-a-qwen"],
        "gpu_ordinals": [0],
        "tensor_parallel_size": 1,
    },
    {
        "server_id": "qwen-worker-b",
        "model_name": "qwen",
        "logical_endpoint_ids": ["worker-b-qwen"],
        "gpu_ordinals": [1],
        "tensor_parallel_size": 1,
    },
    {
        "server_id": "llama-worker-a",
        "model_name": "llama",
        "logical_endpoint_ids": ["worker-a-llama"],
        "gpu_ordinals": [2],
        "tensor_parallel_size": 1,
    },
    {
        "server_id": "llama-worker-b",
        "model_name": "llama",
        "logical_endpoint_ids": ["worker-b-llama"],
        "gpu_ordinals": [3],
        "tensor_parallel_size": 1,
    },
    {
        "server_id": "gemma-shared",
        "model_name": "gemma",
        "logical_endpoint_ids": ["worker-a-gemma", "worker-b-gemma"],
        "gpu_ordinals": [4, 5],
        "tensor_parallel_size": 2,
    },
)


def expected_calls(mode: str) -> int:
    decisions_per_step = 1 if mode == "communication_none" else 2
    return AGENT_COUNT * DURATION * decisions_per_step


def build_config(composition: str, mode: str, seed: int, slot: str) -> dict:
    """Create one prospective formal config without changing prompt semantics."""
    config = build_legacy_matrix_config(
        composition,
        mode,
        seed,
        slot,
        response_contract_version=RESPONSE_CONTRACT_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )
    run_id = f"public-disaster-v3-{composition}-{mode}-s{seed}-w{slot}"
    config["simulation"].update({
        "duration": DURATION,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "research_eligible": True,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "run_id": run_id,
        "run_name": run_id,
    })
    for bloc in config["blocs"]:
        name = bloc["name"]
        bloc["flashinfer_mode"] = "disabled"
        bloc["gpu_memory_utilization"] = 0.92 if name == "gemma" else 0.9
        if name == "gemma":
            bloc["tensor_parallel_size"] = 2
            bloc["device_slot"] = "public-gemma-shared-tp2"
        else:
            bloc["tensor_parallel_size"] = 1
            bloc["device_slot"] = f"public-worker-{slot}-{name}"
    return config


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    rows = []
    ordinal = 0
    for seed_index, seed in enumerate(SEEDS):
        for composition_index, composition in enumerate(COMPOSITIONS):
            # Keep all three communication conditions for one
            # composition/seed on the same logical replica. Alternate that
            # paired block across seeds while leaving exactly two composition
            # blocks (six runs/14,400 calls) on each worker per seed.
            slot = (
                "a"
                if (seed_index + composition_index) % 2 == 0
                else "b"
            )
            for mode in MODES:
                config = build_config(composition, mode, seed, slot)
                filename = f"{config['simulation']['run_id']}.json"
                payload = canonical_bytes(config)
                files[filename] = payload
                calls = expected_calls(mode)
                rows.append({
                    "ordinal": ordinal + 1,
                    "filename": filename,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "run_id": config["simulation"]["run_id"],
                    "worker_slot": slot,
                    "seed": seed,
                    "composition": composition,
                    "communication_mode": mode,
                    "expected_logical_llm_calls": calls,
                    "expected_http_attempts": calls,
                    "research_eligible": True,
                })
                ordinal += 1
    planned_calls = sum(row["expected_logical_llm_calls"] for row in rows)
    manifest = {
        "schema_version": MATRIX_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "duration": DURATION,
        "agent_count": AGENT_COUNT,
        "research_eligible": True,
        "seeds": list(SEEDS),
        "compositions": list(COMPOSITIONS),
        "communication_modes": list(MODES),
        "planned_runs": len(rows),
        "planned_logical_llm_calls": planned_calls,
        "planned_http_attempts": planned_calls,
        "contingency_ceiling_logical_llm_calls": planned_calls,
        "contingency_ceiling_http_attempts": planned_calls,
        "maximum_gpu_count": 6,
        "worker_slots": ["a", "b"],
        "server_layout": list(SERVER_LAYOUT),
        "rows": rows,
    }
    files["manifest.json"] = canonical_bytes(manifest)
    return files


def load_verified_manifest() -> dict:
    expected = build_files()
    actual_names = {path.name for path in OUTPUT_DIR.glob("*.json")}
    if actual_names != set(expected):
        raise ValueError("public formal matrix file set differs from generator")
    for name, payload in expected.items():
        if (OUTPUT_DIR / name).read_bytes() != payload:
            raise ValueError(f"public formal matrix file differs: {name}")
    manifest = json.loads(expected["manifest.json"])
    if (
        manifest["planned_runs"] != 60
        or manifest["planned_logical_llm_calls"] != 144000
        or manifest["planned_http_attempts"] != 144000
        or manifest["maximum_gpu_count"] != 6
    ):
        raise ValueError("public formal matrix differs from the frozen envelope")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_files()
    if args.check:
        load_verified_manifest()
        print("PASS: public formal matrix is byte-identical (60 runs/144000 calls)")
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in expected.items():
        (OUTPUT_DIR / name).write_bytes(payload)
    print("wrote 60 public formal configs plus manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
