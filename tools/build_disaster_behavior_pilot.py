#!/usr/bin/env python3
"""Build or read-only verify the frozen six-run descriptive disaster follow-up."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import build_effective_config  # noqa: E402
from engine.execution_contracts import (  # noqa: E402
    ABORT_RUN_RESPONSE_FAILURE_POLICY,
    BOUNDED_PROMPT_CONTRACT_VERSION,
    NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION,
)
from engine.response_contracts import BOUNDED_RESPONSE_CONTRACT_VERSION  # noqa: E402
from tools.build_disaster_matrix import build_config as base_config, canonical_bytes  # noqa: E402
from tools.scan_publication import scan_text  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "configs" / "disaster_behavior_pilot_v1"
FROZEN_AT_UTC = "20260909T120500Z"
BATCH_ID = f"disaster-llm-behavior-pilot-v1-{FROZEN_AT_UTC}"
PROTOCOL_VERSION = "disaster-llm-behavior-pilot-v1.0.0"
MANIFEST_SCHEMA_VERSION = "disaster-llm-behavior-pilot-manifest-v1.0.0"
METRIC_VERSION = "disaster-metric-v2.0.0"
METRIC_SPEC_PATH = REPO_ROOT / "docs" / "DISASTER_METRIC_V2_SPEC.md"
METRIC_SPEC_SHA256 = "cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711"
PROMPT_CONTRACT_VERSION = BOUNDED_PROMPT_CONTRACT_VERSION
RESPONSE_CONTRACT_VERSION = BOUNDED_RESPONSE_CONTRACT_VERSION
TRANSPORT_BEHAVIOR_VERSION = NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION
RESPONSE_FAILURE_POLICY = ABORT_RUN_RESPONSE_FAILURE_POLICY
LOG_SCHEMA_VERSION = "2.0.0"
MODELS = ("qwen", "llama", "gemma")
SEEDS = (6201, 6202)
DURATION = 60
AGENT_COUNT = 4
MAX_TOKENS = 1024
MAX_CONCURRENCY = 4
SCHEMA_PROBE_REQUESTS = 9
CALLS_PER_RUN = DURATION * AGENT_COUNT * 2
PLANNED_CALLS = CALLS_PER_RUN * len(MODELS) * len(SEEDS)
TOTAL_HTTP_ATTEMPT_CAP = SCHEMA_PROBE_REQUESTS + PLANNED_CALLS
WALL_TIME_LIMIT_SECONDS = 3600
MAXIMUM_GPU_COUNT = 4
PLANNED_GPU_COUNT = 4
RUN_ORDER = tuple((6201, model) for model in MODELS) + tuple(
    (6202, model) for model in reversed(MODELS)
)
SERVER_LAYOUT = (
    {"server_id": "followup-qwen", "model_name": "qwen",
     "logical_endpoint_ids": ["followup-qwen"], "gpu_ordinals": [0],
     "tensor_parallel_size": 1},
    {"server_id": "followup-llama", "model_name": "llama",
     "logical_endpoint_ids": ["followup-llama"], "gpu_ordinals": [1],
     "tensor_parallel_size": 1},
    {"server_id": "followup-gemma", "model_name": "gemma",
     "logical_endpoint_ids": ["followup-gemma"], "gpu_ordinals": [2, 3],
     "tensor_parallel_size": 2},
)


def verify_metric_spec() -> None:
    if hashlib.sha256(METRIC_SPEC_PATH.read_bytes()).hexdigest() != METRIC_SPEC_SHA256:
        raise ValueError("disaster metric v2 specification differs from frozen bytes")


def build_config(model: str, seed: int) -> dict:
    if model not in MODELS or seed not in SEEDS:
        raise ValueError("model/seed is outside the frozen follow-up design")
    config = base_config(
        f"{model}_only", "free_text", seed, "a",
        response_contract_version=RESPONSE_CONTRACT_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )
    run_id = f"{BATCH_ID}-{model}-s{seed}"
    config["simulation"].update({
        "duration": DURATION,
        "run_id": run_id,
        "run_name": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION,
        "metric_spec_sha256": METRIC_SPEC_SHA256,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RESPONSE_FAILURE_POLICY,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "research_eligible": False,
    })
    config["llm_defaults"].update({
        "max_tokens": MAX_TOKENS,
        "max_concurrency": MAX_CONCURRENCY,
    })
    config["scenario"]["official_warning"]["initial_recipient_ids"] = [1]
    bloc = config["blocs"][0]
    bloc.update({
        "num_agents": AGENT_COUNT,
        "endpoint_id": f"followup-{model}",
        "device_slot": f"followup-{model}-tp{2 if model == 'gemma' else 1}",
        "tensor_parallel_size": 2 if model == "gemma" else 1,
        "flashinfer_mode": "disabled",
        "gpu_memory_utilization": 0.92 if model == "gemma" else 0.9,
    })
    return build_effective_config(config)


def build_files() -> dict[str, bytes]:
    verify_metric_spec()
    files: dict[str, bytes] = {}
    rows = []
    for ordinal, (seed, model) in enumerate(RUN_ORDER, 1):
        config = build_config(model, seed)
        filename = f"{config['simulation']['run_id']}.json"
        payload = canonical_bytes(config)
        files[filename] = payload
        rows.append({
            "ordinal": ordinal,
            "filename": filename,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "run_id": config["simulation"]["run_id"],
            "model_name": model,
            "composition": f"{model}_only",
            "seed": seed,
            "communication_mode": "free_text",
            "expected_logical_llm_calls": CALLS_PER_RUN,
            "expected_http_attempts": CALLS_PER_RUN,
            "research_eligible": False,
        })
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "frozen_at_utc": FROZEN_AT_UTC,
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION,
        "metric_spec_sha256": METRIC_SPEC_SHA256,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RESPONSE_FAILURE_POLICY,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "research_eligible": False,
        "formal_eligible": False,
        "analysis_class": "prospective-descriptive-followup",
        "duration": DURATION,
        "agent_count": AGENT_COUNT,
        "max_tokens": MAX_TOKENS,
        "max_concurrency": MAX_CONCURRENCY,
        "seeds": list(SEEDS),
        "models": list(MODELS),
        "communication_modes": ["free_text"],
        "planned_runs": len(rows),
        "planned_logical_llm_calls": PLANNED_CALLS,
        "planned_http_attempts": PLANNED_CALLS,
        "schema_probe_http_attempts": SCHEMA_PROBE_REQUESTS,
        "total_http_attempt_cap": TOTAL_HTTP_ATTEMPT_CAP,
        "wall_time_limit_seconds": WALL_TIME_LIMIT_SECONDS,
        "maximum_gpu_count": MAXIMUM_GPU_COUNT,
        "planned_gpu_count": PLANNED_GPU_COUNT,
        "server_layout": list(SERVER_LAYOUT),
        "run_order_policy": "seed6201-forward-seed6202-reverse-v1.0.0",
        "attempt_set_policy": "retain-all-started-attempts-and-not-started-statuses-v1.0.0",
        "rows": rows,
    }
    files["manifest.json"] = canonical_bytes(manifest)
    for name, payload in files.items():
        if scan_text(name, payload.decode("utf-8")):
            raise ValueError(f"publication boundary finding in generated file: {name}")
    return files


def load_verified_manifest(output_dir: Path | None = None) -> dict:
    directory = OUTPUT_DIR if output_dir is None else output_dir
    expected = build_files()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("follow-up config directory must be a regular directory")
    entries = list(directory.iterdir())
    if {path.name for path in entries} != set(expected):
        raise ValueError("follow-up config file set differs from frozen generator")
    for path in entries:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected[path.name]:
            raise ValueError(f"follow-up config differs from frozen generator: {path.name}")
    return json.loads(expected["manifest.json"])


def write_files(output_dir: Path | None = None, runs_root: Path | None = None) -> None:
    directory = OUTPUT_DIR if output_dir is None else output_dir
    run_directory = REPO_ROOT / "runs" if runs_root is None else runs_root
    expected = build_files()  # Validate all bytes before any output creation.
    manifest = json.loads(expected["manifest.json"])
    if directory.exists() or directory.is_symlink():
        raise FileExistsError("frozen config directory already exists; use --check")
    for row in manifest["rows"]:
        target = run_directory / f"output_{row['run_id']}"
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"immutable run-ID collision: {row['run_id']}")
    directory.mkdir(parents=True, exist_ok=False)
    for name, payload in expected.items():
        with (directory / name).open("xb") as handle:
            handle.write(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        load_verified_manifest()
        print("PASS: six frozen follow-up configs; 2880 run calls + 9 probe calls")
    else:
        write_files()
        print("wrote six frozen follow-up configs and manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
