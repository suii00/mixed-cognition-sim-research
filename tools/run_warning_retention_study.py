#!/usr/bin/env python3
"""Run six frozen warning-retention runs behind a nine-request engineering gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import load_config
from tools.build_warning_retention_study import (
    AGENT_COUNT, BATCH_ID, INPUT_OBSERVABILITY_VERSION, OUTPUT_DIR, PLANNED_CALLS,
    POLICIES, PROTOCOL_VERSION, RETENTION_METRIC_VERSION, RETENTION_METRIC_SPEC_SHA256,
    WALL_TIME_LIMIT_SECONDS as MAX_WALL_S, load_verified_manifest,
)
from tools.disaster_behavior_schema_probe import CASES, run_one_request, utc_now_iso, _write_attempt
from tools.run_public_vllm import (
    DEFAULT_LOCK, PublicVllmError, _load_json_object, _tree_digest,
    attach_snapshots, build_endpoint_specs, check_installed_runtime,
    create_gpu_guard, parse_gpu_indices, ports_are_free,
    runtime_binding_values_absent, start_servers_sequentially, start_simulator,
    stop_process_groups, validate_runtime_lock, validate_vllm_config,
    verify_completed_run, wait_for_gpu_release, wait_for_simulator,
    write_flashinfer_shadow, write_runtime_inputs, query_gpu_rows,
)
from tools.run_refuge_layout_study import (
    FAILURE_COUNTERS, MODEL_ORDER, RUNTIME_PREFIX, check_runtime_ipc_path,
    checked_output_paths, predicted_runtime_ipc_path_bytes, promote_batch,
    public_tree_safe, safe_json, source_gate, specs_for_config,
)
from tools.scan_publication import scan_text, scan_tree

MANIFEST = OUTPUT_DIR / "manifest.json"
RUN_COUNT = 6


def expected_calls(config):
    """Derive the two decision-phase request count from the complete mixed population."""
    duration = config["simulation"]["duration"]
    blocs = config["blocs"]
    if (type(duration) is not int or duration != 60
            or len(blocs) != len(MODEL_ORDER)
            or tuple(bloc["name"] for bloc in blocs) != MODEL_ORDER
            or any(type(bloc["num_agents"]) is not int or bloc["num_agents"] != 8 for bloc in blocs)
            or sum(bloc["num_agents"] for bloc in blocs) != AGENT_COUNT):
        raise PublicVllmError("expected the frozen 24-agent mixed population and 60 steps")
    return duration * AGENT_COUNT * 2


def load_inputs():
    # The generator verifies the whole frozen file set before any output creation.
    manifest = load_verified_manifest(MANIFEST.parent)
    lock = _load_json_object(DEFAULT_LOCK)
    validate_runtime_lock(lock)
    configs = {}
    models = {}
    for row in manifest["rows"]:
        config = load_config(str(MANIFEST.parent / row["filename"]))
        validate_vllm_config(config, lock)
        calls = expected_calls(config)
        if (row["run_id"] in configs or row["run_id"] != config["simulation"]["run_id"]
                or row["duration"] != config["simulation"]["duration"]
                or row["composition"] != "mixed"
                or row["seed"] != config["simulation"]["seed"]
                or row["message_selection_policy"] != config["agents"]["message_selection_policy"]
                or row["message_selection_policy"] != POLICIES[row["condition"]]
                or row["expected_logical_llm_calls"] != calls
                or row["expected_http_attempts"] != calls
                or config["llm_defaults"]["max_concurrency"] != AGENT_COUNT):
            raise PublicVllmError("manifest row differs from mixed population or request envelope")
        if scan_text(row["filename"], json.dumps(config, ensure_ascii=False)):
            raise PublicVllmError("public config failed publication boundary")
        configs[row["run_id"]] = config
        for bloc in config["blocs"]:
            name = bloc["name"]
            if name in models and models[name] != bloc:
                raise PublicVllmError("model or endpoint conditions differ between study runs")
            models[name] = bloc
    if (len(configs) != RUN_COUNT or set(models) != set(MODEL_ORDER)
            or sum(expected_calls(config) for config in configs.values()) != PLANNED_CALLS
            or manifest["planned_logical_llm_calls"] != PLANNED_CALLS
            or manifest["planned_http_attempts"] != PLANNED_CALLS):
        raise PublicVllmError("expected six frozen paired mixed runs and 17280 experiment requests")
    union_config = dict(next(iter(configs.values())))
    union_config["blocs"] = [models[name] for name in MODEL_ORDER]
    return manifest, configs, models, union_config, lock


def check_run(run_dir, config, specs, source_sha):
    calls = expected_calls(config)
    strict_ok, unverifiable = verify_completed_run(run_dir, config, specs)
    meta = _load_json_object(run_dir / "run_meta.json")
    counts_ok = (
        meta.get("logical_llm_calls") == calls and meta.get("http_attempts") == calls
        and all(meta.get(key) == 0 for key in FAILURE_COUNTERS)
    )
    identity_ok = (meta.get("git_sha") == source_sha and meta.get("git_dirty") is False
                   and meta.get("config") == config)
    findings = scan_tree(run_dir)
    boundary_ok = public_tree_safe(run_dir, specs)
    row = {
        "run_id": config["simulation"]["run_id"],
        "status": meta.get("status"), "completed_steps": meta.get("completed_steps"),
        "expected_steps": config["simulation"]["duration"], "expected_agents": AGENT_COUNT,
        "expected_logical_llm_calls": calls, "expected_http_attempts": calls,
        "logical_llm_calls": meta.get("logical_llm_calls"),
        "http_attempts": meta.get("http_attempts"),
        "failures": {key: meta.get(key) for key in FAILURE_COUNTERS},
        "strict_validation_passed": strict_ok,
        "strict_unverifiable_count": unverifiable,
        "source_config_identity_passed": identity_ok,
        "publication_scan_finding_count": len(findings),
        "decoded_publication_boundary_passed": boundary_ok,
        "run_tree_sha256": _tree_digest(run_dir),
    }
    row["completion_gate_passed"] = bool(strict_ok and counts_ok and identity_ok and not findings and boundary_ok)
    return row


def run(args):
    manifest, configs, models, union_config, lock = load_inputs()
    indices = parse_gpu_indices(args.gpu_indices, 4, 4)
    specs = build_endpoint_specs(union_config, indices, args.base_port)
    if args.contract_only:
        print("PASS: six paired mixed configs, 17280 experiment requests, nine gate requests, four GPUs")
        return 0
    if os.name != "posix":
        raise PublicVllmError("GPU execution requires a POSIX host")
    predicted_ipc_bytes = predicted_runtime_ipc_path_bytes()
    source_gate(args.source_git_sha)
    runtime = check_installed_runtime(lock)
    specs = attach_snapshots(specs)
    guard = create_gpu_guard(indices, 4, 256)
    if not ports_are_free(specs):
        raise PublicVllmError("requested loopback ports are occupied")
    batch = BATCH_ID
    stage, final_evidence = checked_output_paths(batch, configs, repo_root=REPO_ROOT)
    if args.preflight_only:
        print("PASS: clean source, exact runtime and snapshots, four free GPUs, free ports, bounded IPC path, no collisions")
        return 0
    started = time.monotonic()
    deadline = started + MAX_WALL_S
    stage.mkdir(parents=True, exist_ok=False)
    stage_runs = stage / "runs"
    stage_runs.mkdir()
    probe_dir = stage / "probe"
    probe_dir.mkdir()
    servers = []
    simulations = []
    rows = [{**{key: row[key] for key in (
        "run_id", "condition", "seed", "composition", "layout", "duration",
        "expected_logical_llm_calls", "expected_http_attempts",
    )}, "status": "not_started"} for row in manifest["rows"]]
    attempts = []
    error_code = None
    process_cleanup = False
    gpu_release = False
    result = {
        "schema_version": "warning-retention-study-verification-v1.0.0", "batch_id": batch,
        "protocol_version": PROTOCOL_VERSION,
        "retention_metric_version": RETENTION_METRIC_VERSION,
        "retention_metric_spec_sha256": RETENTION_METRIC_SPEC_SHA256,
        "input_observability_version": INPUT_OBSERVABILITY_VERSION,
        "planned_runs": RUN_COUNT, "agent_count": AGENT_COUNT,
        "planned_logical_llm_calls": PLANNED_CALLS,
        "planned_http_attempts": PLANNED_CALLS,
        "total_http_attempt_cap": PLANNED_CALLS + len(MODEL_ORDER) * len(CASES),
        "source_git_sha": args.source_git_sha, "research_eligible": False,
        "environment_class": "linux-cuda-vllm-offline-four-gpu",
        "manifest_sha256": hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        "runtime_lock_sha256": hashlib.sha256(DEFAULT_LOCK.read_bytes()).hexdigest(),
        "runtime_versions": runtime, "selected_gpu_count": 4,
        "authorized_gpu_ceiling": 4, "enforced_gpu_ceiling": 4,
        "wall_time_limit_s": MAX_WALL_S, "start_time_utc": utc_now_iso(),
        "predicted_runtime_ipc_path_bytes": predicted_ipc_bytes,
        "allocated_runtime_ipc_path_bytes": None,
        "model_artifacts": [{key: models[name][key] for key in (
            "name", "model", "model_source", "model_digest", "tokenizer_revision", "chat_template"
        )} for name in MODEL_ORDER],
    }
    try:
        with tempfile.TemporaryDirectory(prefix=RUNTIME_PREFIX) as tmp:
            runtime_root = Path(tmp)
            result["allocated_runtime_ipc_path_bytes"] = check_runtime_ipc_path(runtime_root)
            shadow = write_flashinfer_shadow(runtime_root)
            try:
                start_servers_sequentially(specs, servers, runtime_root, shadow, guard, 900)
                by_endpoint = {spec.endpoint_id: spec for spec in specs}
                with (probe_dir / "attempts.jsonl").open("xb") as handle:
                    for name in MODEL_ORDER:
                        for case in CASES:
                            if time.monotonic() > deadline - 180:
                                raise PublicVllmError("wall_time_limit")
                            guard.observe(query_gpu_rows())
                            model = models[name]
                            attempt = run_one_request(model=model, case=case,
                                base_url=by_endpoint[model["endpoint_id"]].base_url, timeout_s=120)
                            attempts.append(attempt)
                            _write_attempt(handle, attempt)
                            print(f"probe {name}/{case['case']}: {attempt['result']}", flush=True)
                if len(attempts) != 9 or any(a["result"] != "pass" for a in attempts):
                    raise PublicVllmError("schema_gate_failed")
                if not public_tree_safe(probe_dir, specs):
                    raise PublicVllmError("probe_publication_boundary")
                for ordinal, (run_id, config) in enumerate(configs.items()):
                    remaining = deadline - time.monotonic() - 90
                    if remaining <= 0:
                        raise PublicVllmError("wall_time_limit")
                    source_gate(args.source_git_sha)
                    row_runtime = runtime_root / ("run-" + str(ordinal))
                    row_runtime.mkdir()
                    used_specs = specs_for_config(config, specs)
                    config_path, binding_path = write_runtime_inputs(row_runtime, config, used_specs)
                    sim = start_simulator(row_runtime, shadow, config_path, binding_path, stage_runs)
                    simulations.append(sim)
                    rows[ordinal]["status"] = "started"
                    return_code = wait_for_simulator(sim, servers, guard, remaining)
                    run_dir = stage_runs / ("output_" + run_id)
                    if not run_dir.is_dir():
                        raise PublicVllmError("simulation_created_no_run")
                    rows[ordinal].update(check_run(run_dir, config, specs, args.source_git_sha))
                    rows[ordinal]["exit_code"] = return_code
                    print(f"run {ordinal + 1}/{RUN_COUNT}: {rows[ordinal]['status']}", flush=True)
                    if return_code != 0 or not rows[ordinal]["completion_gate_passed"]:
                        raise PublicVllmError("run_completion_gate_failed")
            finally:
                try:
                    process_cleanup = stop_process_groups(simulations + servers)
                finally:
                    gpu_release = wait_for_gpu_release(guard)
    except BaseException as error:
        # Raw output remains unchanged in ignored staging, including partial attempts.
        error_code = str(error) if isinstance(error, PublicVllmError) else type(error).__name__
    # Preserve terminal states for a run interrupted between launch and validation.
    for index, row in enumerate(rows):
        raw_dir = stage_runs / ("output_" + row["run_id"])
        meta_path = raw_dir / "run_meta.json"
        if row["status"] in ("not_started", "started") and meta_path.is_file():
            meta = _load_json_object(meta_path)
            rows[index].update({"status": meta.get("status"),
                                "completed_steps": meta.get("completed_steps"),
                                "logical_llm_calls": meta.get("logical_llm_calls"),
                                "http_attempts": meta.get("http_attempts")})
        elif row["status"] == "started":
            rows[index]["status"] = "incomplete_missing_terminal_metadata"
    result.update({
        "runs": rows, "probe_attempts": len(attempts),
        "probe_passed": sum(a["result"] == "pass" for a in attempts),
        "all_process_groups_stopped": process_cleanup,
        "gpu_release_verified": gpu_release, "ports_released": ports_are_free(specs),
        "maximum_observed_active_gpu_count": guard.max_observed_active_gpu_count,
        "error_code": error_code, "end_time_utc": utc_now_iso(),
        "elapsed_wall_s": round(time.monotonic() - started, 3),
        "vllm_server_log_files_created": False,
        "runtime_binding_values_persisted": not runtime_binding_values_absent(stage, specs),
    })
    for source, target in (("http_attempts", "experiment_http_attempts"),
                           ("logical_llm_calls", "experiment_logical_llm_calls")):
        known = sum(row[source] for row in rows if type(row.get(source)) is int)
        complete = all(row["status"] == "not_started" or type(row.get(source)) is int for row in rows)
        result[target + "_accounted"] = known
        result[target] = known if complete else None
    result["total_http_attempts_accounted"] = len(attempts) + result["experiment_http_attempts_accounted"]
    result["total_http_attempts"] = (None if result["experiment_http_attempts"] is None
                                      else len(attempts) + result["experiment_http_attempts"])
    result["gate_passed"] = bool(
        not error_code and process_cleanup and gpu_release and result["ports_released"]
        and not result["runtime_binding_values_persisted"]
        and len(attempts) == 9 and result["probe_passed"] == 9
        and result["experiment_http_attempts"] == PLANNED_CALLS
        and result["experiment_logical_llm_calls"] == PLANNED_CALLS
        and result["elapsed_wall_s"] <= MAX_WALL_S and public_tree_safe(stage, specs)
        and all(r.get("completion_gate_passed") for r in rows)
    )
    safe_json(stage / "verification.json", result)
    if not result["gate_passed"]:
        print("STOP: gate failed; immutable attempted evidence retained in ignored staging", flush=True)
        return 3
    promote_batch(stage, final_evidence, configs, rows, repo_root=REPO_ROOT)
    print(f"PASS: six paired mixed runs completed; evidence={final_evidence.name}", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-git-sha")
    parser.add_argument("--gpu-indices", default="2,3,4,5")
    parser.add_argument("--base-port", type=int, default=18600)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--contract-only", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (PublicVllmError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {type(error).__name__}: warning retention execution rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
