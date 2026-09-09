#!/usr/bin/env python3
"""Run the frozen four-run mixed-model refuge layout study behind a nine-request gate."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import endpoint_rows, load_config
from engine.provenance import file_manifest
from tools.build_refuge_layout_study import (
    AGENT_COUNT, BATCH_ID, OUTPUT_DIR, PLANNED_CALLS, PROTOCOL_VERSION,
    WALL_TIME_LIMIT_SECONDS as MAX_WALL_S, load_verified_manifest,
)
from tools.disaster_behavior_schema_probe import CASES, canonical_bytes, run_one_request, utc_now_iso, _write_attempt
from tools.run_public_vllm import (
    DEFAULT_LOCK, PublicVllmError, _load_json_object, _tree_digest,
    attach_snapshots, build_endpoint_specs, check_installed_runtime,
    create_gpu_guard, parse_gpu_indices, ports_are_free,
    runtime_binding_values_absent, start_servers_sequentially, start_simulator,
    stop_process_groups, validate_runtime_lock, validate_vllm_config,
    verify_completed_run, wait_for_gpu_release, wait_for_simulator,
    write_flashinfer_shadow, write_runtime_inputs, query_gpu_rows,
)
from tools.scan_publication import scan_text, scan_tree

MANIFEST = OUTPUT_DIR / "manifest.json"
RUNTIME_PREFIX = "refuge-study-runtime-"
RUN_COUNT = 4
MAX_IPC_PATH_BYTES = 107
MODEL_ORDER = ("qwen", "llama", "gemma")
FAILURE_COUNTERS = (
    "generation_retries", "transport_failures", "syntax_parse_failures",
    "syntax_parse_attempt_failures", "schema_validation_failures",
)


def source_gate(source_sha: str) -> None:
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise PublicVllmError("source SHA must be 40 lowercase hex characters")
    def git(*args):
        return subprocess.run(["git", *args], cwd=REPO_ROOT, check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    try:
        if git("rev-parse", "HEAD") != source_sha or git("status", "--porcelain"):
            raise PublicVllmError("execution requires the frozen clean source commit")
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicVllmError("source inspection failed") from error


def check_runtime_ipc_path(runtime_root: Path) -> int:
    """Check vLLM's TMPDIR/UUID socket path without creating a socket or file."""
    socket_path = runtime_root / "tmp" / ("0" * 36)
    size = len(str(socket_path).encode("utf-8"))
    if size > MAX_IPC_PATH_BYTES:
        raise PublicVllmError("runtime IPC path exceeds the 107-byte limit")
    return size


def predicted_runtime_ipc_path_bytes() -> int:
    # The locked CPython tempfile implementation uses eight random characters.
    # The allocated path is checked again before any server process starts.
    root = Path(tempfile.gettempdir()) / (RUNTIME_PREFIX + "0" * 8)
    return check_runtime_ipc_path(root)


def expected_calls(config):
    """Derive the two decision-phase request count from the complete mixed population."""
    duration = config["simulation"]["duration"]
    blocs = config["blocs"]
    if (type(duration) is not int or duration not in (60, 120)
            or len(blocs) != len(MODEL_ORDER)
            or tuple(bloc["name"] for bloc in blocs) != MODEL_ORDER
            or any(type(bloc["num_agents"]) is not int or bloc["num_agents"] != 8 for bloc in blocs)
            or sum(bloc["num_agents"] for bloc in blocs) != AGENT_COUNT):
        raise PublicVllmError("expected the frozen 24-agent mixed population and 60/120 steps")
    return duration * AGENT_COUNT * 2


def specs_for_config(config, specs):
    """Keep every mixed-model endpoint; reject incomplete or duplicated runtime routing."""
    required = {str(row["endpoint_id"]) for bloc in config["blocs"] for row in endpoint_rows(bloc)}
    selected = [spec for spec in specs if spec.endpoint_id in required]
    if len(selected) != len(required) or {spec.endpoint_id for spec in selected} != required:
        raise PublicVllmError("runtime routing does not cover the mixed-model config exactly once")
    return selected


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
        raise PublicVllmError("expected four frozen mixed runs and 17280 experiment requests")
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


def checked_output_paths(batch, run_ids):
    root = REPO_ROOT.resolve(strict=True)
    parents = [REPO_ROOT / name for name in (".tmp", "runs", "derived")]
    for parent in parents:
        if parent.is_symlink() or parent.resolve(strict=False) != root / parent.name:
            raise PublicVllmError("output ancestor is symlinked or outside source checkout")
        if parent.exists() and not parent.is_dir():
            raise PublicVllmError("output ancestor is not a directory")
    stage = parents[0] / batch
    evidence = parents[2] / ("validation-" + batch)
    targets = [stage, evidence, *(parents[1] / ("output_" + name) for name in run_ids)]
    if any(path.exists() or path.is_symlink() for path in targets):
        raise PublicVllmError("batch or run output collision")
    devices = {parent.stat().st_dev if parent.exists() else root.stat().st_dev for parent in parents}
    if len(devices) != 1:
        raise PublicVllmError("staging and publication must share one filesystem")
    return stage, evidence


def public_tree_safe(root, specs):
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        return False
    if scan_tree(root) or not runtime_binding_values_absent(root, specs):
        return False
    forbidden = [spec.base_url for spec in specs]

    def safe(value, depth=0):
        if depth > 12:
            return False
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("_body_base64") and isinstance(child, str):
                    try:
                        decoded = base64.b64decode(child, validate=True).decode("utf-8")
                    except (ValueError, UnicodeError):
                        return False
                    if not safe(decoded, depth + 1):
                        return False
                elif not safe(child, depth + 1):
                    return False
        elif isinstance(value, list):
            return all(safe(child, depth + 1) for child in value)
        elif isinstance(value, str):
            if any(marker in value for marker in forbidden) or scan_text("decoded-json", value):
                return False
            if value[:1] in ("{", "["):
                try:
                    nested = json.loads(value)
                except (ValueError, RecursionError):
                    pass
                else:
                    return safe(nested, depth + 1)
        return True

    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".jsonl"):
                text = path.read_text(encoding="utf-8")
                values = [json.loads(line) for line in text.splitlines()] if path.suffix == ".jsonl" else [json.loads(text)]
                if not all(safe(value) for value in values):
                    return False
    except (OSError, ValueError, RecursionError):
        return False
    return True


def promote_batch(stage, final_evidence, run_ids, rows):
    evidence_stage = stage / "evidence"
    evidence_stage.mkdir(exist_ok=False)
    (stage / "probe").rename(evidence_stage / "probe")
    (stage / "verification.json").rename(evidence_stage / "verification.json")
    safe_json(evidence_stage / "artifact_manifest.json", {
        "algorithm": "sha256",
        "files": {path.relative_to(evidence_stage).as_posix(): file_manifest(path)
                  for path in sorted(evidence_stage.rglob("*")) if path.is_file()},
    })
    sources = [stage / "runs" / ("output_" + name) for name in run_ids] + [evidence_stage]
    destinations = [REPO_ROOT / "runs" / source.name for source in sources[:-1]] + [final_evidence]
    expected_hashes = [row["run_tree_sha256"] for row in rows] + [_tree_digest(evidence_stage)]
    for source, destination, expected in zip(sources, destinations, expected_hashes):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink() or source.stat().st_dev != destination.parent.stat().st_dev:
            raise PublicVllmError("publication collision or filesystem mismatch")
        if _tree_digest(source) != expected:
            raise PublicVllmError("artifact changed before promotion")
    moved = []
    try:
        for source, destination, expected in zip(sources, destinations, expected_hashes):
            if destination.exists() or destination.is_symlink():
                raise PublicVllmError("publication collision during promotion")
            source.rename(destination)
            moved.append((source, destination))
            if _tree_digest(destination) != expected:
                raise PublicVllmError("artifact changed during promotion")
    except BaseException as error:
        rollback_failed = []
        for source, destination in reversed(moved):
            try:
                destination.rename(source)
            except OSError:
                rollback_failed.append(destination.name)
        safe_json(stage / "promotion_failure.json", {
            "error_code": type(error).__name__, "publication_blocked": True,
            "partial_promotion_detected": bool(rollback_failed),
            "remaining_destination_names": rollback_failed,
        })
        raise PublicVllmError("promotion failed; see retained promotion failure evidence") from error


def safe_json(path, value):
    content = canonical_bytes(value) + b"\n"
    if scan_text(path.name, content.decode("utf-8")):
        raise PublicVllmError("verification failed public boundary")
    with path.open("xb") as handle:
        handle.write(content)


def run(args):
    manifest, configs, models, union_config, lock = load_inputs()
    indices = parse_gpu_indices(args.gpu_indices, 4, 4)
    specs = build_endpoint_specs(union_config, indices, args.base_port)
    if args.contract_only:
        print("PASS: four mixed configs, 17280 experiment requests, nine gate requests, four GPUs")
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
    stage, final_evidence = checked_output_paths(batch, configs)
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
        "run_id", "composition", "layout", "duration",
        "expected_logical_llm_calls", "expected_http_attempts",
    )}, "status": "not_started"} for row in manifest["rows"]]
    attempts = []
    error_code = None
    process_cleanup = False
    gpu_release = False
    result = {
        "schema_version": "refuge-layout-study-verification-v1.0.0", "batch_id": batch,
        "protocol_version": PROTOCOL_VERSION,
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
        if row["status"] == "not_started" and meta_path.is_file():
            meta = _load_json_object(meta_path)
            rows[index].update({"status": meta.get("status"),
                                "completed_steps": meta.get("completed_steps"),
                                "logical_llm_calls": meta.get("logical_llm_calls"),
                                "http_attempts": meta.get("http_attempts")})
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
    result["experiment_http_attempts"] = sum(row.get("http_attempts") or 0 for row in rows)
    result["experiment_logical_llm_calls"] = sum(row.get("logical_llm_calls") or 0 for row in rows)
    result["total_http_attempts"] = len(attempts) + result["experiment_http_attempts"]
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
    promote_batch(stage, final_evidence, configs, rows)
    print(f"PASS: four mixed runs completed; evidence={final_evidence.name}", flush=True)
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
        print(f"ERROR: {type(error).__name__}: refuge layout execution rejected", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
