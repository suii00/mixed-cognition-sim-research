#!/usr/bin/env python3
"""Run the frozen 60-run disaster matrix through a public-only vLLM boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import load_config, required_endpoint_ids  # noqa: E402
from engine.provenance import generate_run_id  # noqa: E402
from tools.build_public_disaster_matrix import (  # noqa: E402
    OUTPUT_DIR,
    SERVER_LAYOUT,
    load_verified_manifest,
)
from tools.public_disaster_matrix_worker import verify_one_run  # noqa: E402
from tools.run_public_vllm import (  # noqa: E402
    DEFAULT_LOCK,
    EndpointSpec,
    GpuGuard,
    PublicVllmError,
    _load_json_object,
    _tree_digest,
    attach_snapshots,
    build_child_environment,
    check_installed_runtime,
    create_gpu_guard,
    parse_gpu_indices,
    ports_are_free,
    query_gpu_rows,
    start_server,
    stop_process_groups,
    validate_runtime_lock,
    validate_vllm_config,
    wait_for_gpu_release,
    wait_for_servers,
    write_flashinfer_shadow,
)
from tools.scan_publication import scan_text, scan_tree  # noqa: E402


DEFAULT_BASE_PORT = 18200
MAXIMUM_WALL_TIMEOUT_S = 8 * 60 * 60
EVIDENCE_SCHEMA_VERSION = "public-disaster-matrix-verification-v1.1.0"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_WORKER_FAILURE_CODES = frozenset({
    "source_revision_mismatch",
    "source_worktree_not_clean",
    "worker_envelope_mismatch",
    "runtime_bindings_invalid",
    "strict_validation_failed",
    "publication_boundary_failed",
    "runtime_binding_persisted",
    "unexpected_log_file",
    "completed_run_contract_failed",
    "zero_failure_contract_failed",
    "research_eligibility_not_persisted",
    "config_hash_mismatch",
    "managed_worker_error",
    "worker_completion_envelope_mismatch",
})


def git_preflight(source_sha: str) -> None:
    if not SOURCE_SHA_RE.fullmatch(source_sha):
        raise PublicVllmError("source Git SHA must be a full lowercase digest")
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicVllmError("source Git state cannot be verified") from error
    if head != source_sha:
        raise PublicVllmError("source Git SHA differs from the approved revision")
    if dirty:
        raise PublicVllmError("formal execution requires a clean Git worktree")


def load_matrix_configs(
    manifest: Mapping[str, Any],
    lock: Mapping[str, Any],
) -> tuple[dict[str, dict], dict[str, dict]]:
    configs: dict[str, dict] = {}
    model_blocs: dict[str, dict] = {}
    endpoint_ids: set[str] = set()
    for row in manifest["rows"]:
        path = OUTPUT_DIR / row["filename"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise PublicVllmError("formal config hash differs from its manifest")
        config = load_config(str(path))
        validate_vllm_config(config, lock)
        if config["simulation"].get("research_eligible") is not True:
            raise PublicVllmError("formal config is not research eligible")
        configs[row["run_id"]] = config
        endpoint_ids.update(required_endpoint_ids(config))
        for bloc in config["blocs"]:
            name = bloc["name"]
            identity = {
                key: bloc[key]
                for key in (
                    "model",
                    "model_source",
                    "model_digest",
                    "dtype",
                    "max_model_len",
                    "tensor_parallel_size",
                    "gpu_memory_utilization",
                )
            }
            previous = model_blocs.get(name)
            if previous is not None and previous != identity:
                raise PublicVllmError("model execution identity changes within matrix")
            model_blocs[name] = identity
    layout_endpoints = {
        endpoint_id
        for server in SERVER_LAYOUT
        for endpoint_id in server["logical_endpoint_ids"]
    }
    if endpoint_ids != layout_endpoints:
        raise PublicVllmError("logical endpoint layout differs from matrix configs")
    if set(configs) != {row["run_id"] for row in manifest["rows"]}:
        raise PublicVllmError("formal run IDs are not unique")
    return configs, model_blocs


def build_server_specs(
    model_blocs: Mapping[str, Mapping[str, Any]],
    gpu_indices: tuple[int, ...],
    base_port: int,
) -> list[EndpointSpec]:
    if len(gpu_indices) != 6:
        raise PublicVllmError("formal matrix requires exactly six selected GPUs")
    if base_port < 1024 or base_port + len(SERVER_LAYOUT) - 1 > 65535:
        raise PublicVllmError("formal loopback port range is invalid")
    specs = []
    for ordinal, server in enumerate(SERVER_LAYOUT):
        bloc = model_blocs.get(server["model_name"])
        if bloc is None:
            raise PublicVllmError("server layout references an unknown model")
        assigned = tuple(gpu_indices[index] for index in server["gpu_ordinals"])
        if len(assigned) != server["tensor_parallel_size"]:
            raise PublicVllmError("server layout tensor parallelism is invalid")
        if bloc["tensor_parallel_size"] != server["tensor_parallel_size"]:
            raise PublicVllmError("config tensor parallelism differs from server layout")
        specs.append(EndpointSpec(
            endpoint_id=server["server_id"],
            served_model_name=str(bloc["model"]),
            model_source=str(bloc["model_source"]),
            model_digest=str(bloc["model_digest"]),
            dtype=str(bloc["dtype"]),
            max_model_len=int(bloc["max_model_len"]),
            tensor_parallel_size=int(bloc["tensor_parallel_size"]),
            gpu_memory_utilization=float(bloc["gpu_memory_utilization"]),
            port=base_port + ordinal,
            gpu_indices=assigned,
        ))
    if {index for spec in specs for index in spec.gpu_indices} != set(gpu_indices):
        raise PublicVllmError("server layout does not use the exact selected GPU scope")
    return specs


def write_matrix_bindings(path: Path, specs: Sequence[EndpointSpec]) -> None:
    by_server = {spec.endpoint_id: spec for spec in specs}
    endpoints = {}
    for server in SERVER_LAYOUT:
        spec = by_server[server["server_id"]]
        for endpoint_id in server["logical_endpoint_ids"]:
            endpoints[endpoint_id] = {"base_url": spec.base_url}
    path.write_text(
        yaml.safe_dump({"endpoints": endpoints}, sort_keys=True),
        encoding="utf-8",
    )


def start_worker(
    slot: str,
    worker_root: Path,
    source_sha: str,
    runtime_lock: Path,
    bindings_path: Path,
    runtime_root: Path,
    shadow_root: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "public_disaster_matrix_worker.py"),
            "--slot",
            slot,
            "--output-root",
            str(worker_root),
            "--source-git-sha",
            source_sha,
            "--runtime-lock",
            str(runtime_lock),
            "--runtime-bindings",
            str(bindings_path),
        ],
        cwd=REPO_ROOT,
        env=build_child_environment(runtime_root, shadow_root, None),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_for_workers(
    workers: Sequence[subprocess.Popen[bytes]],
    servers: Sequence[subprocess.Popen[bytes]],
    guard: GpuGuard,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        guard.observe(query_gpu_rows())
        if any(server.poll() is not None for server in servers):
            stop_process_groups(workers)
            raise PublicVllmError("a vLLM server stopped during the formal matrix")
        codes = [worker.poll() for worker in workers]
        if any(code is not None and code != 0 for code in codes):
            stop_process_groups(workers)
            raise PublicVllmError("a formal matrix worker stopped")
        if all(code == 0 for code in codes):
            return
        time.sleep(2.0)
    stop_process_groups(workers)
    raise PublicVllmError("formal matrix wall-time limit exceeded")


def read_worker_failure_codes(stage_root: Path) -> list[str]:
    codes = []
    for slot in ("a", "b"):
        path = stage_root / f"worker-{slot}" / "worker_state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        code = value.get("failure_code")
        if code in SAFE_WORKER_FAILURE_CODES:
            codes.append(code)
    return sorted(set(codes))


def verify_stage(
    stage_root: Path,
    manifest: Mapping[str, Any],
    configs: Mapping[str, Mapping[str, Any]],
    source_sha: str,
    forbidden_runtime_values: Sequence[bytes],
) -> list[dict]:
    expected = {row["run_id"]: row for row in manifest["rows"]}
    actual: dict[str, Path] = {}
    for slot in ("a", "b"):
        state_path = stage_root / f"worker-{slot}" / "worker_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if (
            state.get("status") != "completed"
            or state.get("failure_code") is not None
            or state.get("completed_runs") != 30
            or state.get("completed_logical_llm_calls") != 72000
            or state.get("completed_http_attempts") != 72000
        ):
            raise PublicVllmError("worker completion state differs from envelope")
        runs_root = stage_root / f"worker-{slot}" / "runs"
        for run_dir in sorted(runs_root.glob("output_*")):
            if not run_dir.is_dir() or run_dir.is_symlink():
                raise PublicVllmError("staged run path is not a real directory")
            run_id = run_dir.name.removeprefix("output_")
            if run_id in actual:
                raise PublicVllmError("duplicate formal run output detected")
            actual[run_id] = run_dir
    if set(actual) != set(expected):
        raise PublicVllmError("staged run set differs from the 60-run manifest")

    findings = scan_tree(stage_root)
    if findings:
        raise PublicVllmError("staged matrix failed the publication boundary")
    verified = []
    for run_id in sorted(expected):
        verified.append(verify_one_run(
            actual[run_id],
            expected[run_id],
            configs[run_id],
            source_sha,
            forbidden_runtime_values,
        ))
    if (
        len(verified) != 60
        or sum(row["logical_llm_calls"] for row in verified) != 144000
        or sum(row["http_attempts"] for row in verified) != 144000
    ):
        raise PublicVllmError("verified matrix totals differ from the approved envelope")
    return verified


def promote_runs(
    stage_root: Path,
    output_root: Path,
    verified: Sequence[Mapping[str, Any]],
) -> None:
    destinations = {
        row["run_id"]: output_root / f"output_{row['run_id']}"
        for row in verified
    }
    if any(path.exists() or path.is_symlink() for path in destinations.values()):
        raise PublicVllmError("formal run output collision detected")
    sources = {}
    for slot in ("a", "b"):
        for path in (stage_root / f"worker-{slot}" / "runs").glob("output_*"):
            sources[path.name.removeprefix("output_")] = path
    if set(sources) != set(destinations):
        raise PublicVllmError("promotion source set differs from verified runs")
    moved = []
    try:
        for run_id in sorted(destinations):
            os.replace(sources[run_id], destinations[run_id])
            moved.append(run_id)
            if _tree_digest(destinations[run_id]) != next(
                row["run_tree_sha256"]
                for row in verified
                if row["run_id"] == run_id
            ):
                raise PublicVllmError("promoted run bytes changed during move")
    except BaseException:
        rollback_failed = False
        for run_id in reversed(moved):
            try:
                os.replace(destinations[run_id], sources[run_id])
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise PublicVllmError("formal promotion rollback could not be completed")
        raise


def write_evidence(
    evidence_root: Path,
    batch_id: str,
    source_sha: str,
    manifest: Mapping[str, Any],
    lock: Mapping[str, Any],
    configs: Mapping[str, Mapping[str, Any]],
    verified: Sequence[Mapping[str, Any]],
    guard: GpuGuard,
    processes_stopped: bool,
    gpu_release_verified: bool,
    loopback_ports_released: bool,
    wall_timeout_s: float,
    elapsed_s: float,
) -> Path:
    destination = evidence_root / f"validation-vllm-matrix-{batch_id}"
    destination.mkdir(parents=True, exist_ok=False)
    lock_bytes = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "batch_id": batch_id,
        "source_git_sha": source_sha,
        "matrix_manifest_sha256": hashlib.sha256(
            (OUTPUT_DIR / "manifest.json").read_bytes()
        ).hexdigest(),
        "runtime_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "protocol_version": manifest["protocol_version"],
        "metric_version": manifest["metric_version"],
        "response_contract_version": manifest["response_contract_version"],
        "log_schema_version": manifest["log_schema_version"],
        "validation_gate_version": manifest["validation_gate_version"],
        "research_eligible": True,
        "seeds": manifest["seeds"],
        "compositions": manifest["compositions"],
        "communication_modes": manifest["communication_modes"],
        "planned_runs": 60,
        "completed_runs": 60,
        "planned_logical_llm_calls": 144000,
        "completed_logical_llm_calls": 144000,
        "planned_http_attempts": 144000,
        "completed_http_attempts": 144000,
        "generation_retries": 0,
        "transport_failures": 0,
        "syntax_parse_attempt_failures": 0,
        "syntax_parse_failures": 0,
        "schema_validation_failures": 0,
        "strict_validation_passed": True,
        "runs_with_strict_unverifiable": sum(
            row["strict_unverifiable_count"] > 0 for row in verified
        ),
        "strict_unverifiable_total_count": sum(
            row["strict_unverifiable_count"] for row in verified
        ),
        "strict_unverifiable_digest_set": sorted({
            row["strict_unverifiable_sha256"] for row in verified
        }),
        "publication_scan_finding_count": 0,
        "runtime_binding_values_persisted": False,
        "vllm_server_log_files_created": False,
        "flashinfer_mode": lock["execution_contract"]["flashinfer_mode"],
        "model_sources": sorted({
            bloc["model_source"]
            for config in configs.values()
            for bloc in config["blocs"]
        }),
        "max_model_len_values": sorted({
            bloc["max_model_len"]
            for config in configs.values()
            for bloc in config["blocs"]
        }),
        "selected_gpu_count": len(guard.selected),
        "maximum_observed_active_gpu_count": guard.max_observed_active_gpu_count,
        "server_process_count": len(SERVER_LAYOUT),
        "worker_process_count": 2,
        "all_process_groups_stopped": processes_stopped,
        "gpu_release_verified": gpu_release_verified,
        "loopback_ports_released": loopback_ports_released,
        "wall_time_limit_seconds": wall_timeout_s,
        "elapsed_seconds": round(elapsed_s, 3),
        "runs": list(verified),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if scan_text("verification.json", text):
        shutil.rmtree(destination)
        raise PublicVllmError("matrix verification evidence failed publication scan")
    path = destination / "verification.json"
    path.write_text(text, encoding="utf-8")
    return path


def run_matrix(args: argparse.Namespace) -> tuple[str, Path]:
    manifest = load_verified_manifest()
    lock = _load_json_object(args.runtime_lock.resolve())
    validate_runtime_lock(lock)
    configs, model_blocs = load_matrix_configs(manifest, lock)
    if args.contract_only:
        return "contract-only", OUTPUT_DIR / "manifest.json"
    if os.name != "posix":
        raise PublicVllmError("GPU vLLM execution requires a POSIX host")

    git_preflight(args.source_git_sha)
    check_installed_runtime(lock)
    gpu_limit = int(lock["execution_contract"]["max_gpu_count"])
    indices = parse_gpu_indices(args.gpu_indices, 6, gpu_limit)
    specs = attach_snapshots(build_server_specs(model_blocs, indices, args.base_port))
    if not ports_are_free(specs):
        raise PublicVllmError("one or more formal loopback ports are unavailable")
    guard = create_gpu_guard(indices, gpu_limit, args.max_initial_memory_mib)
    if args.preflight_only:
        return "preflight-only", OUTPUT_DIR / "manifest.json"

    output_root = args.output_root.resolve()
    evidence_root = args.evidence_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    batch_id = f"public-disaster-matrix-{generate_run_id()}"
    stage_root = output_root / ".tmp" / f"{batch_id}-stage"
    stage_root.mkdir(parents=True, exist_ok=False)
    for row in manifest["rows"]:
        destination = output_root / f"output_{row['run_id']}"
        if destination.exists() or destination.is_symlink():
            raise PublicVllmError("formal run output collision detected")

    servers: list[subprocess.Popen[bytes]] = []
    workers: list[subprocess.Popen[bytes]] = []
    execution_error: PublicVllmError | None = None
    processes_stopped = False
    gpu_release_verified = False
    loopback_ports_released = False
    server_log_files_created = False
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="public-disaster-matrix-runtime-") as temporary:
            runtime_root = Path(temporary)
            shadow_root = write_flashinfer_shadow(runtime_root)
            bindings_path = runtime_root / "runtime-bindings.yaml"
            write_matrix_bindings(bindings_path, specs)
            try:
                for spec in specs:
                    servers.append(start_server(spec, runtime_root, shadow_root))
                startup_budget = min(
                    args.startup_timeout_s,
                    max(0.001, args.wall_timeout_s - (time.monotonic() - started)),
                )
                wait_for_servers(specs, servers, guard, startup_budget)
                for slot in ("a", "b"):
                    workers.append(start_worker(
                        slot,
                        stage_root / f"worker-{slot}",
                        args.source_git_sha,
                        args.runtime_lock.resolve(),
                        bindings_path,
                        runtime_root,
                        shadow_root,
                    ))
                remaining = args.wall_timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise PublicVllmError("formal matrix wall-time limit exceeded")
                wait_for_workers(workers, servers, guard, remaining)
            except PublicVllmError as error:
                execution_error = error
            except (OSError, subprocess.SubprocessError) as error:
                execution_error = PublicVllmError(
                    "a managed formal child process could not be executed"
                )
            finally:
                processes_stopped = stop_process_groups([*workers, *servers])
                server_log_files_created = any(
                    path.is_file() and path.suffix.lower() == ".log"
                    for path in runtime_root.rglob("*")
                )
        gpu_release_verified = wait_for_gpu_release(guard)
        loopback_ports_released = ports_are_free(specs)
    finally:
        elapsed_s = time.monotonic() - started

    if scan_tree(stage_root):
        raise PublicVllmError("staged matrix failed the publication boundary")
    forbidden = tuple(spec.base_url.encode("utf-8") for spec in specs)
    if any(
        value in path.read_bytes()
        for path in stage_root.rglob("*")
        if path.is_file()
        for value in forbidden
    ):
        raise PublicVllmError("runtime binding values entered staged output")
    if execution_error is not None:
        codes = read_worker_failure_codes(stage_root)
        suffix = f" ({','.join(codes)})" if codes else ""
        raise PublicVllmError(f"{execution_error}{suffix}")
    if server_log_files_created:
        raise PublicVllmError("a managed runtime log file was created")
    if not processes_stopped:
        raise PublicVllmError("one or more formal process groups remained active")
    if not gpu_release_verified:
        raise PublicVllmError("formal GPU release could not be verified")
    if not loopback_ports_released:
        raise PublicVllmError("one or more formal loopback ports remained open")

    verified = verify_stage(
        stage_root,
        manifest,
        configs,
        args.source_git_sha,
        forbidden,
    )
    promote_runs(stage_root, output_root, verified)
    evidence_path = write_evidence(
        evidence_root,
        batch_id,
        args.source_git_sha,
        manifest,
        lock,
        configs,
        verified,
        guard,
        processes_stopped,
        gpu_release_verified,
        loopback_ports_released,
        args.wall_timeout_s,
        elapsed_s,
    )
    if scan_tree(evidence_path.parent):
        raise PublicVllmError("matrix evidence failed its final publication scan")
    return batch_id, evidence_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--evidence-root", type=Path, default=REPO_ROOT / "derived")
    parser.add_argument("--source-git-sha", required=True)
    parser.add_argument("--gpu-indices")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument("--startup-timeout-s", type=float, default=900.0)
    parser.add_argument("--wall-timeout-s", type=float, default=MAXIMUM_WALL_TIMEOUT_S)
    parser.add_argument("--max-initial-memory-mib", type=int, default=512)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--contract-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.contract_only and args.preflight_only:
        print("FAIL: choose only one validation-only mode")
        return 2
    if (
        args.startup_timeout_s <= 0
        or args.wall_timeout_s <= 0
        or args.wall_timeout_s > MAXIMUM_WALL_TIMEOUT_S
        or args.max_initial_memory_mib < 0
    ):
        print("FAIL: formal timeout or memory envelope is invalid")
        return 2
    try:
        result, evidence_path = run_matrix(args)
    except PublicVllmError as error:
        print(f"FAIL: {error}")
        return 1
    except KeyboardInterrupt:
        print("FAIL: public formal matrix execution was interrupted")
        return 130
    except BaseException as error:
        print(f"FAIL: unexpected formal launcher error ({type(error).__name__})")
        return 2
    if result == "contract-only":
        print("PASS: public formal 60-run matrix contract is internally consistent")
        return 0
    if result == "preflight-only":
        print("PASS: exact runtime, models, ports, and six-GPU scope are ready")
        return 0
    try:
        evidence = evidence_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        evidence = evidence_path.name
    print("PASS: public formal matrix completed 60 runs and 144000 calls")
    print("strict_validation=pass publication_findings=0 runtime_binding_values=absent")
    print(f"batch_id={result}")
    print(f"evidence={evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
