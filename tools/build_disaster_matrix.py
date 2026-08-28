#!/usr/bin/env python3
"""Build or check the prospective 4 x 3 x 5 disaster config matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.response_contracts import (  # noqa: E402
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    LEGACY_RESPONSE_CONTRACT_VERSION,
    validate_response_contract_version,
)
from engine.provenance import OBSERVABILITY_LOG_SCHEMA_VERSION  # noqa: E402
OUTPUT_DIR = REPO_ROOT / "configs" / "formal_disaster_v1"
SEEDS = (2101, 2102, 2103, 2104, 2105)
COMPOSITIONS = ("qwen_only", "llama_only", "gemma_only", "mixed")
MODES = ("free_text", "structured_warning", "communication_none")
PROTOCOL_VERSION = "formal-disaster-protocol-v2.0.0"
METRIC_VERSION = "disaster-metric-v1.0.0"

MODEL = {
    "qwen": {
        "model": "qwen2.5-7b-instruct",
        "model_source": "Qwen/Qwen2.5-7B-Instruct",
        "model_digest": "a09a35458c702b33eeacc393d103063234e8bc28",
        "chat_template": "tokenizer_config.json:chat_template:sha256:cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f",
    },
    "llama": {
        "model": "llama-3.1-8b-instruct",
        "model_source": "meta-llama/Llama-3.1-8B-Instruct",
        "model_digest": "0e9e39f249a16976918f6564b8830bc894c89659",
        "chat_template": "tokenizer_config.json:chat_template:sha256:e10ca381b1ccc5cf9db52e371f3b6651576caee0a630b452e2816b2d404d4b65",
    },
    "gemma": {
        "model": "gemma-2-9b-it",
        "model_source": "google/gemma-2-9b-it",
        "model_digest": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
        "chat_template": "tokenizer_config.json:chat_template:sha256:ecd6ae513fe103f0eb62e8ab5bfa8d0fe45c1074fa398b089c93a7e70c15cfd6",
    },
}
ENDPOINTS = {
    slot: {
        name: {
            "endpoint_id": f"worker-{slot}-{name}",
            "device_slot": f"worker-{slot}-device-{index}",
        }
        for index, name in enumerate(("qwen", "llama", "gemma"))
    }
    for slot in ("a", "b")
}


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def model_bloc(
    name: str,
    count: int,
    slot: str,
    *,
    response_contract_version: str = LEGACY_RESPONSE_CONTRACT_VERSION,
) -> dict:
    contract_version = validate_response_contract_version(
        response_contract_version
    )
    row = {
        "name": name,
        **MODEL[name],
        **ENDPOINTS[slot][name],
        "num_agents": count,
        "provider": "vllm",
        "backend_version": "0.27.1",
        "dtype": "bfloat16",
        "quantization": "none",
        "tensor_parallel_size": 1,
        "data_parallel_size": 1,
        "max_model_len": 4096,
        "generation_config": "vllm",
        "tokenizer_revision": MODEL[name]["model_digest"],
    }
    if contract_version == LEGACY_RESPONSE_CONTRACT_VERSION:
        row["llm_overrides"] = {
            "response_format": {"type": "json_object"}
        }
    if name == "gemma":
        row.update({"gpu_memory_utilization": 0.92, "use_flashinfer_sampler": False})
    return row


def build_config(
    composition: str,
    mode: str,
    seed: int,
    slot: str,
    *,
    response_contract_version: str = LEGACY_RESPONSE_CONTRACT_VERSION,
    protocol_version: Optional[str] = None,
) -> dict:
    contract_version = validate_response_contract_version(
        response_contract_version
    )
    if protocol_version is not None and (
        not isinstance(protocol_version, str) or not protocol_version
    ):
        raise ValueError("protocol_version must be a non-empty string")
    if (
        contract_version == CANONICAL_RESPONSE_CONTRACT_VERSION
        and protocol_version is None
    ):
        raise ValueError(
            "phase-response-v2.0.0 requires an explicit prospective protocol_version"
        )
    counts = (
        {"qwen": 8, "llama": 8, "gemma": 8}
        if composition == "mixed"
        else {composition.removesuffix("_only"): 24}
    )
    run_id = f"disaster-v1-{composition}-{mode}-s{seed}-w{slot}"
    config = {
        "agents": {
            "communication_radius": 12,
            "edge_policy": "full",
            "memory_limit": 20,
            "memory_size": 5,
            "message_context_size": 5,
            "message_history_limit": 20,
        },
        "blocs": [
            model_bloc(
                name,
                count,
                slot,
                response_contract_version=contract_version,
            )
            for name, count in counts.items()
        ],
        "llm_defaults": {
            "max_concurrency": 24,
            "max_tokens": 256,
            "temperature": 0.0,
            "timeout_s": 120,
        },
        "places": [],
        "scenario": {
            "schema_version": "disaster-scenario-v1.0.0",
            "type": "disaster_v1",
            "communication_mode": mode,
            "hazard": {
                "hazard_id": "inundation-zone-1",
                "stages": [
                    {"start_step": 10, "rectangles": [
                        {"x_min": -25, "x_max": 25, "y_min": -25, "y_max": -8}
                    ]},
                    {"start_step": 30, "rectangles": [
                        {"x_min": -25, "x_max": 25, "y_min": -25, "y_max": 0}
                    ]},
                ],
            },
            "refuges": [
                {"refuge_id": "refuge-west", "rectangle":
                    {"x_min": -23, "x_max": -18, "y_min": 18, "y_max": 23}},
                {"refuge_id": "refuge-east", "rectangle":
                    {"x_min": 18, "x_max": 23, "y_min": 18, "y_max": 23}},
            ],
            "official_warning": {
                "warning_id": "warning-inundation-1",
                "issue_step": 10,
                "initial_recipient_ids": [1, 5, 9, 13, 17, 21],
            },
            "initial_eligible_rectangles": [
                {"x_min": -25, "x_max": 25, "y_min": -25, "y_max": 17}
            ],
        },
        "simulation": {
            "duration": 60,
            "execution_mode": "vllm_openai_compatible",
            "failure_thresholds": {
                "schema_validation_failures": 0,
                "syntax_parse_failures": 0,
                "transport_failures": 0,
            },
            "half_space_size": 25,
            "metric_version": METRIC_VERSION,
            "protocol_version": protocol_version or PROTOCOL_VERSION,
            "run_id": run_id,
            "run_name": run_id,
            "seed": seed,
        },
    }
    if contract_version == CANONICAL_RESPONSE_CONTRACT_VERSION:
        config["simulation"]["response_contract_version"] = contract_version
        config["simulation"]["log_schema_version"] = (
            OBSERVABILITY_LOG_SCHEMA_VERSION
        )
    return config


def build_files() -> dict[str, bytes]:
    files = {}
    rows = []
    ordinal = 0
    for seed in SEEDS:
        for composition in COMPOSITIONS:
            for mode in MODES:
                slot = "a" if ordinal % 2 == 0 else "b"
                config = build_config(composition, mode, seed, slot)
                filename = f"{config['simulation']['run_id']}.json"
                payload = canonical_bytes(config)
                files[filename] = payload
                calls = 24 * 60 * (1 if mode == "communication_none" else 2)
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
                })
                ordinal += 1
    manifest = {
        "schema_version": "formal-disaster-matrix-v1.0.0",
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION,
        "seeds": list(SEEDS),
        "planned_runs": len(rows),
        "planned_logical_llm_calls": sum(row["expected_logical_llm_calls"] for row in rows),
        "contingency_ceiling_logical_llm_calls": 165000,
        "rows": rows,
    }
    files["manifest.json"] = canonical_bytes(manifest)
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = build_files()
    if args.check:
        actual_names = {path.name for path in OUTPUT_DIR.glob("*.json")}
        if actual_names != set(expected):
            raise SystemExit("matrix file set differs from generator")
        for name, payload in expected.items():
            if (OUTPUT_DIR / name).read_bytes() != payload:
                raise SystemExit(f"matrix file differs: {name}")
        print("PASS: 60 configs and manifest are byte-identical")
        return 0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in expected.items():
        (OUTPUT_DIR / name).write_bytes(payload)
    print(f"wrote {len(expected) - 1} configs plus manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
