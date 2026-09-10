#!/usr/bin/env python3
"""Build or read-only verify six prospectively fixed warning-retention runs."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import build_effective_config
from tools.build_disaster_matrix import build_config as base_config, canonical_bytes
from tools.build_refuge_layout_study import (
    AGENT_COUNT, AGENTS_PER_MODEL, COMMON_INITIAL_ELIGIBLE_RECTANGLES,
    MAX_CONCURRENCY, MAX_TOKENS, METRIC_SPEC_PATH, METRIC_SPEC_SHA256,
    METRIC_VERSION, MODEL_ORDER, OFFICIAL_RECIPIENT_IDS, PROMPT_CONTRACT_VERSION,
    REFUGES_BY_LAYOUT, RESPONSE_CONTRACT_VERSION, RESPONSE_FAILURE_POLICY,
    SERVER_LAYOUT, TRANSPORT_BEHAVIOR_VERSION, verify_metric_spec,
)
from tools.scan_publication import scan_text

OUTPUT_DIR = REPO_ROOT / "configs" / "warning_retention_study_v1"
BATCH_ID_TIMESTAMP = "20260910T080000Z"
BATCH_ID = f"warning-retention-study-v1-{BATCH_ID_TIMESTAMP}"
PROTOCOL_VERSION = "warning-retention-study-v1.0.0"
MANIFEST_SCHEMA_VERSION = "warning-retention-study-manifest-v1.0.0"
RETENTION_METRIC_VERSION = "warning-retention-study-metric-v1.0.0"
RETENTION_METRIC_SPEC_PATH = REPO_ROOT / "docs" / "WARNING_RETENTION_STUDY_METRIC_V1_SPEC.md"
RETENTION_METRIC_SPEC_SHA256 = "a8bca6c4b0f9563e3d2a7bfaeabd2b9530eec28abf86495360fafb60a8452e81"
INPUT_OBSERVABILITY_VERSION = "message-presentation-v1.0.0"
LOG_SCHEMA_VERSION = "2.0.0"
CONDITIONS = ("recent", "retained")
POLICIES = {"recent": "recent-v1.0.0", "retained": "retain-official-warning-v1.0.0"}
SEEDS = (7301, 7302, 7303)
DURATION = 60
LAYOUT = "inset"
MODELS = MODEL_ORDER
RUN_ORDER = (("recent", 7301), ("retained", 7301),
             ("retained", 7302), ("recent", 7302),
             ("recent", 7303), ("retained", 7303))
PLANNED_CALLS = len(RUN_ORDER) * DURATION * AGENT_COUNT * 2
SCHEMA_PROBE_REQUESTS = 9
TOTAL_HTTP_ATTEMPT_CAP = PLANNED_CALLS + SCHEMA_PROBE_REQUESTS
WALL_TIME_LIMIT_SECONDS = 10800
MAXIMUM_GPU_COUNT = 4
PLANNED_GPU_COUNT = 4


def verify_retention_metric_spec() -> None:
    verify_metric_spec()
    if hashlib.sha256(RETENTION_METRIC_SPEC_PATH.read_bytes()).hexdigest() != RETENTION_METRIC_SPEC_SHA256:
        raise ValueError("warning retention metric specification differs from frozen bytes")


def build_config(condition: str, seed: int) -> dict:
    if condition not in CONDITIONS or type(seed) is not int or seed not in SEEDS:
        raise ValueError("condition is outside the fixed warning-retention study design")
    config = base_config("mixed", "free_text", seed, "a",
                         response_contract_version=RESPONSE_CONTRACT_VERSION,
                         protocol_version=PROTOCOL_VERSION)
    run_id = f"{BATCH_ID}-{condition}-d{DURATION}-s{seed}"
    config["simulation"].update({
        "duration": DURATION, "run_id": run_id, "run_name": run_id,
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION, "metric_spec_sha256": METRIC_SPEC_SHA256,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RESPONSE_FAILURE_POLICY,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "input_observability_version": INPUT_OBSERVABILITY_VERSION,
        "research_eligible": False,
    })
    config["agents"]["message_selection_policy"] = POLICIES[condition]
    config["agents"]["message_context_size"] = 5
    config["llm_defaults"].update({"max_tokens": MAX_TOKENS, "max_concurrency": MAX_CONCURRENCY})
    config["scenario"]["refuges"] = copy.deepcopy(list(REFUGES_BY_LAYOUT[LAYOUT]))
    config["scenario"]["initial_eligible_rectangles"] = copy.deepcopy(list(COMMON_INITIAL_ELIGIBLE_RECTANGLES))
    config["scenario"]["official_warning"]["initial_recipient_ids"] = list(OFFICIAL_RECIPIENT_IDS)
    if tuple(bloc["name"] for bloc in config["blocs"]) != MODEL_ORDER:
        raise ValueError("mixed bloc ordering differs from the fixed model assignment")
    for bloc in config["blocs"]:
        model = bloc["name"]
        bloc.update({"num_agents": AGENTS_PER_MODEL,
                     "endpoint_id": f"refuge-{model}",
                     "device_slot": f"refuge-{model}-tp{2 if model == 'gemma' else 1}",
                     "tensor_parallel_size": 2 if model == "gemma" else 1,
                     "flashinfer_mode": "disabled",
                     "gpu_memory_utilization": 0.92 if model == "gemma" else 0.9})
    return build_effective_config(config)


def build_files() -> dict[str, bytes]:
    verify_retention_metric_spec()
    files = {}
    rows = []
    for ordinal, (condition, seed) in enumerate(RUN_ORDER, 1):
        config = build_config(condition, seed)
        filename = f"{config['simulation']['run_id']}.json"
        payload = canonical_bytes(config)
        files[filename] = payload
        rows.append({"ordinal": ordinal, "filename": filename,
                     "sha256": hashlib.sha256(payload).hexdigest(),
                     "run_id": config["simulation"]["run_id"],
                     "condition": condition, "message_selection_policy": POLICIES[condition],
                     "composition": "mixed", "layout": LAYOUT, "duration": DURATION,
                     "seed": seed, "agent_count": AGENT_COUNT, "model_names": list(MODEL_ORDER),
                     "communication_mode": "free_text",
                     "expected_logical_llm_calls": DURATION * AGENT_COUNT * 2,
                     "expected_http_attempts": DURATION * AGENT_COUNT * 2,
                     "research_eligible": False})
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION, "batch_id": BATCH_ID,
        "batch_id_timestamp": BATCH_ID_TIMESTAMP,
        "freeze_policy": "clean-source-commit-before-model-requests-v1.0.0",
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION, "metric_spec_sha256": METRIC_SPEC_SHA256,
        "retention_metric_version": RETENTION_METRIC_VERSION,
        "retention_metric_spec_sha256": RETENTION_METRIC_SPEC_SHA256,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RESPONSE_FAILURE_POLICY,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "input_observability_version": INPUT_OBSERVABILITY_VERSION,
        "research_eligible": False, "formal_eligible": False,
        "analysis_class": "prospective-exploratory-paired-input-selection-followup",
        "conditions": list(CONDITIONS), "message_selection_policies": POLICIES,
        "durations": [DURATION], "layouts": [LAYOUT], "compositions": ["mixed"],
        "agent_count": AGENT_COUNT, "agents_per_model": AGENTS_PER_MODEL,
        "official_recipient_ids": list(OFFICIAL_RECIPIENT_IDS),
        "common_initial_eligible_cell_count": 2121, "message_context_size": 5,
        "max_tokens": MAX_TOKENS, "max_concurrency": MAX_CONCURRENCY,
        "seeds": list(SEEDS), "models": list(MODEL_ORDER),
        "communication_modes": ["free_text"], "planned_runs": len(rows),
        "planned_logical_llm_calls": PLANNED_CALLS, "planned_http_attempts": PLANNED_CALLS,
        "schema_probe_http_attempts": SCHEMA_PROBE_REQUESTS,
        "total_http_attempt_cap": TOTAL_HTTP_ATTEMPT_CAP,
        "wall_time_limit_seconds": WALL_TIME_LIMIT_SECONDS,
        "maximum_gpu_count": MAXIMUM_GPU_COUNT, "planned_gpu_count": PLANNED_GPU_COUNT,
        "server_layout": list(SERVER_LAYOUT),
        "run_order_policy": "seed7301-A-B-seed7302-B-A-seed7303-A-B-v1.0.0",
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
    expected = build_files()  # Reject all invalid input before creating output.
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
        print("PASS: six paired mixed configs; 17280 run calls + 9 probe calls")
    else:
        write_files()
        print("wrote six paired warning-retention configs and manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
