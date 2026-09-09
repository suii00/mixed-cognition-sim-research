#!/usr/bin/env python3
"""Build or read-only verify four prospectively fixed mixed-population runs."""

from __future__ import annotations

import argparse
import copy
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

OUTPUT_DIR = REPO_ROOT / "configs" / "refuge_layout_study_v1"
BATCH_ID_TIMESTAMP = "20260909T162300Z"
BATCH_ID = f"refuge-layout-study-v1-{BATCH_ID_TIMESTAMP}"
PROTOCOL_VERSION = "refuge-layout-study-v1.0.0"
MANIFEST_SCHEMA_VERSION = "refuge-layout-study-manifest-v1.0.0"
METRIC_VERSION = "disaster-metric-v2.0.0"
METRIC_SPEC_PATH = REPO_ROOT / "docs" / "DISASTER_METRIC_V2_SPEC.md"
METRIC_SPEC_SHA256 = "cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711"
PROMPT_CONTRACT_VERSION = BOUNDED_PROMPT_CONTRACT_VERSION
RESPONSE_CONTRACT_VERSION = BOUNDED_RESPONSE_CONTRACT_VERSION
TRANSPORT_BEHAVIOR_VERSION = NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION
RESPONSE_FAILURE_POLICY = ABORT_RUN_RESPONSE_FAILURE_POLICY
LOG_SCHEMA_VERSION = "2.0.0"
MODEL_ORDER = ("qwen", "llama", "gemma")
MODELS = MODEL_ORDER
COMPOSITIONS = ("mixed",)
LAYOUTS = ("edge", "inset")
SEEDS = (6301,)
DURATIONS = (60, 120)
AGENT_COUNT = 24
AGENTS_PER_MODEL = 8
OFFICIAL_RECIPIENT_IDS = (1, 5, 9, 13, 17, 21)
MAX_TOKENS = 1024
MAX_CONCURRENCY = 24
SCHEMA_PROBE_REQUESTS = 9
RUN_ORDER = (
    ("mixed", "edge", 60, 6301),
    ("mixed", "inset", 60, 6301),
    ("mixed", "inset", 120, 6301),
    ("mixed", "edge", 120, 6301),
)
PLANNED_CALLS = sum(duration * AGENT_COUNT * 2 for _, _, duration, _ in RUN_ORDER)
TOTAL_HTTP_ATTEMPT_CAP = PLANNED_CALLS + SCHEMA_PROBE_REQUESTS
WALL_TIME_LIMIT_SECONDS = 10800
MAXIMUM_GPU_COUNT = 4
PLANNED_GPU_COUNT = 4
SERVER_LAYOUT = (
    {"server_id": "refuge-qwen", "model_name": "qwen",
     "logical_endpoint_ids": ["refuge-qwen"], "gpu_ordinals": [0],
     "tensor_parallel_size": 1},
    {"server_id": "refuge-llama", "model_name": "llama",
     "logical_endpoint_ids": ["refuge-llama"], "gpu_ordinals": [1],
     "tensor_parallel_size": 1},
    {"server_id": "refuge-gemma", "model_name": "gemma",
     "logical_endpoint_ids": ["refuge-gemma"], "gpu_ordinals": [2, 3],
     "tensor_parallel_size": 2},
)

# Both layouts use this same 2,121-cell set. The engine also excludes active
# refuge cells, so excluding the union here prevents layout-dependent starts.
COMMON_INITIAL_ELIGIBLE_RECTANGLES = (
    {"x_min": -25, "x_max": 25, "y_min": -25, "y_max": 5},
    {"x_min": -25, "x_max": 25, "y_min": 12, "y_max": 17},
    {"x_min": -25, "x_max": -11, "y_min": 6, "y_max": 11},
    {"x_min": -4, "x_max": 4, "y_min": 6, "y_max": 11},
    {"x_min": 11, "x_max": 25, "y_min": 6, "y_max": 11},
)
REFUGES_BY_LAYOUT = {
    "edge": (
        {"refuge_id": "refuge-west", "rectangle":
         {"x_min": -23, "x_max": -18, "y_min": 18, "y_max": 23}},
        {"refuge_id": "refuge-east", "rectangle":
         {"x_min": 18, "x_max": 23, "y_min": 18, "y_max": 23}},
    ),
    "inset": (
        {"refuge_id": "refuge-west", "rectangle":
         {"x_min": -10, "x_max": -5, "y_min": 6, "y_max": 11}},
        {"refuge_id": "refuge-east", "rectangle":
         {"x_min": 5, "x_max": 10, "y_min": 6, "y_max": 11}},
    ),
}


def verify_metric_spec() -> None:
    if hashlib.sha256(METRIC_SPEC_PATH.read_bytes()).hexdigest() != METRIC_SPEC_SHA256:
        raise ValueError("disaster metric v2 specification differs from frozen bytes")


def build_config(composition: str, layout: str, duration: int, seed: int) -> dict:
    if (composition not in COMPOSITIONS or layout not in LAYOUTS
            or duration not in DURATIONS or seed not in SEEDS):
        raise ValueError("condition is outside the fixed refuge-layout study design")
    config = base_config(
        composition, "free_text", seed, "a",
        response_contract_version=RESPONSE_CONTRACT_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )
    run_id = f"{BATCH_ID}-{layout}-d{duration}-s{seed}"
    config["simulation"].update({
        "duration": duration,
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
    config["scenario"]["refuges"] = copy.deepcopy(list(REFUGES_BY_LAYOUT[layout]))
    config["scenario"]["initial_eligible_rectangles"] = copy.deepcopy(
        list(COMMON_INITIAL_ELIGIBLE_RECTANGLES)
    )
    config["scenario"]["official_warning"]["initial_recipient_ids"] = list(
        OFFICIAL_RECIPIENT_IDS
    )
    if tuple(bloc["name"] for bloc in config["blocs"]) != MODEL_ORDER:
        raise ValueError("mixed bloc ordering differs from the fixed model assignment")
    for bloc in config["blocs"]:
        model = bloc["name"]
        bloc.update({
            "num_agents": AGENTS_PER_MODEL,
            "endpoint_id": f"refuge-{model}",
            "device_slot": f"refuge-{model}-tp{2 if model == 'gemma' else 1}",
            "tensor_parallel_size": 2 if model == "gemma" else 1,
            "flashinfer_mode": "disabled",
            "gpu_memory_utilization": 0.92 if model == "gemma" else 0.9,
        })
    return build_effective_config(config)


def build_files() -> dict[str, bytes]:
    verify_metric_spec()
    files: dict[str, bytes] = {}
    rows = []
    for ordinal, (composition, layout, duration, seed) in enumerate(RUN_ORDER, 1):
        config = build_config(composition, layout, duration, seed)
        filename = f"{config['simulation']['run_id']}.json"
        payload = canonical_bytes(config)
        files[filename] = payload
        calls = duration * AGENT_COUNT * 2
        rows.append({
            "ordinal": ordinal,
            "filename": filename,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "run_id": config["simulation"]["run_id"],
            "composition": composition,
            "layout": layout,
            "duration": duration,
            "seed": seed,
            "agent_count": AGENT_COUNT,
            "model_names": list(MODEL_ORDER),
            "communication_mode": "free_text",
            "expected_logical_llm_calls": calls,
            "expected_http_attempts": calls,
            "research_eligible": False,
        })
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_id": BATCH_ID,
        "batch_id_timestamp": BATCH_ID_TIMESTAMP,
        "freeze_policy": "clean-source-commit-before-model-requests-v1.0.0",
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
        "analysis_class": "prospective-descriptive-layout-followup",
        "durations": list(DURATIONS),
        "agent_count": AGENT_COUNT,
        "agents_per_model": AGENTS_PER_MODEL,
        "official_recipient_ids": list(OFFICIAL_RECIPIENT_IDS),
        "common_initial_eligible_cell_count": 2121,
        "max_tokens": MAX_TOKENS,
        "max_concurrency": MAX_CONCURRENCY,
        "seeds": list(SEEDS),
        "models": list(MODEL_ORDER),
        "compositions": list(COMPOSITIONS),
        "layouts": list(LAYOUTS),
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
        "run_order_policy": "duration60-edge-inset-duration120-inset-edge-v1.0.0",
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
        raise ValueError("study config directory must be a regular directory")
    entries = list(directory.iterdir())
    if {path.name for path in entries} != set(expected):
        raise ValueError("study config file set differs from fixed generator")
    for path in entries:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected[path.name]:
            raise ValueError(f"study config differs from fixed generator: {path.name}")
    return json.loads(expected["manifest.json"])


def write_files(output_dir: Path | None = None, runs_root: Path | None = None) -> None:
    directory = OUTPUT_DIR if output_dir is None else output_dir
    run_directory = REPO_ROOT / "runs" if runs_root is None else runs_root
    expected = build_files()  # Validate every payload before output creation.
    manifest = json.loads(expected["manifest.json"])
    if directory.exists() or directory.is_symlink():
        raise FileExistsError("study config directory already exists; use --check")
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
        print("PASS: four fixed mixed configs; 17280 run calls + 9 probe calls")
    else:
        write_files()
        print("wrote four mixed refuge-layout configs and manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
