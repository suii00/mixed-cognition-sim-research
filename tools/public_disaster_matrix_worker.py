#!/usr/bin/env python3
"""Run one frozen public-disaster worker slice inside ignored staging."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import (  # noqa: E402
    load_config,
    load_runtime_bindings,
    required_endpoint_ids,
)
from engine.provenance import atomic_write_json, utc_now_iso  # noqa: E402
from engine.sim import Simulation  # noqa: E402
from tools.build_public_disaster_matrix import (  # noqa: E402
    OUTPUT_DIR,
    SEEDS,
    load_verified_manifest,
)
from tools.run_public_vllm import (  # noqa: E402
    PublicVllmError,
    _load_json_object,
    _tree_digest,
    validate_runtime_lock,
    validate_vllm_config,
)
from tools.scan_publication import scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


STATE_SCHEMA_VERSION = "public-disaster-worker-state-v1.0.0"
FAILURE_COUNTERS = (
    "generation_retries",
    "transport_failures",
    "syntax_parse_attempt_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
)


class WorkerError(RuntimeError):
    """A fixed-code failure that never embeds generated or environment data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def git_preflight(source_sha: str) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    if head != source_sha:
        raise WorkerError("source_revision_mismatch")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    if dirty:
        raise WorkerError("source_worktree_not_clean")


def select_worker_rows(manifest: Mapping[str, object], slot: str) -> list[dict]:
    rows = [
        row
        for row in manifest["rows"]
        if row["worker_slot"] == slot and row["seed"] in SEEDS
    ]
    calls = sum(row["expected_logical_llm_calls"] for row in rows)
    attempts = sum(row["expected_http_attempts"] for row in rows)
    if len(rows) != 30 or calls != 72000 or attempts != 72000:
        raise WorkerError("worker_envelope_mismatch")
    return rows


def load_forbidden_runtime_values(path: Path) -> tuple[bytes, ...]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("endpoints"), dict):
        raise WorkerError("runtime_bindings_invalid")
    result = []
    for endpoint in value["endpoints"].values():
        if not isinstance(endpoint, dict):
            raise WorkerError("runtime_bindings_invalid")
        base_url = endpoint.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise WorkerError("runtime_bindings_invalid")
        result.append(base_url.encode("utf-8"))
    return tuple(sorted(set(result)))


def runtime_values_absent(run_dir: Path, forbidden: Sequence[bytes]) -> bool:
    for path in sorted(item for item in run_dir.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if any(value in content for value in forbidden):
            return False
    return True


def verify_one_run(
    run_dir: Path,
    row: Mapping[str, object],
    config: Mapping[str, object],
    source_sha: str,
    forbidden_runtime_values: Sequence[bytes],
) -> dict:
    report = validate_run(run_dir, strict=True)
    if not report.valid or report.unverifiable:
        raise WorkerError("strict_validation_failed")
    findings = scan_tree(run_dir)
    if findings:
        raise WorkerError("publication_boundary_failed")
    if not runtime_values_absent(run_dir, forbidden_runtime_values):
        raise WorkerError("runtime_binding_persisted")
    if any(path.suffix.lower() == ".log" for path in run_dir.rglob("*")):
        raise WorkerError("unexpected_log_file")

    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    expected_calls = int(row["expected_logical_llm_calls"])
    exact = {
        "run_id": row["run_id"],
        "status": "completed",
        "aborted": False,
        "expected_steps": int(config["simulation"]["duration"]),
        "completed_steps": int(config["simulation"]["duration"]),
        "expected_agents": 24,
        "observed_agents": 24,
        "logical_llm_calls": expected_calls,
        "http_attempts": int(row["expected_http_attempts"]),
        "git_sha": source_sha,
        "git_dirty": False,
        "raw_manifest_status": "available",
        "response_contract_version": "phase-response-v2.0.0",
        "log_schema_version": "2.0.0",
    }
    if any(meta.get(key) != value for key, value in exact.items()):
        raise WorkerError("completed_run_contract_failed")
    if any(meta.get(key) != 0 for key in FAILURE_COUNTERS):
        raise WorkerError("zero_failure_contract_failed")
    persisted_config = meta.get("config")
    if (
        not isinstance(persisted_config, dict)
        or persisted_config.get("simulation", {}).get("research_eligible") is not True
    ):
        raise WorkerError("research_eligibility_not_persisted")
    return {
        "run_id": row["run_id"],
        "config_sha256": row["sha256"],
        "run_tree_sha256": _tree_digest(run_dir),
        "logical_llm_calls": expected_calls,
        "http_attempts": int(row["expected_http_attempts"]),
        "strict_validation_passed": True,
        "strict_unverifiable_count": 0,
        "publication_scan_finding_count": 0,
        "runtime_binding_values_persisted": False,
    }


def run_worker(args: argparse.Namespace) -> int:
    manifest = load_verified_manifest()
    rows = select_worker_rows(manifest, args.slot)
    git_preflight(args.source_git_sha)
    lock = _load_json_object(args.runtime_lock.resolve())
    validate_runtime_lock(lock)
    forbidden = load_forbidden_runtime_values(args.runtime_bindings.resolve())

    worker_root = args.output_root.resolve()
    worker_root.mkdir(parents=True, exist_ok=False)
    runs_root = worker_root / "runs"
    runs_root.mkdir()
    state_path = worker_root / "worker_state.json"
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "status": "running",
        "failure_code": None,
        "worker_slot": args.slot,
        "source_git_sha": args.source_git_sha,
        "seeds": list(SEEDS),
        "planned_runs": len(rows),
        "planned_logical_llm_calls": 72000,
        "planned_http_attempts": 72000,
        "completed_runs": 0,
        "completed_logical_llm_calls": 0,
        "completed_http_attempts": 0,
        "start_time_utc": utc_now_iso(),
        "end_time_utc": None,
        "runs": [],
    }
    atomic_write_json(state_path, state)
    try:
        for row in rows:
            config_path = OUTPUT_DIR / row["filename"]
            if hashlib.sha256(config_path.read_bytes()).hexdigest() != row["sha256"]:
                raise WorkerError("config_hash_mismatch")
            config = load_config(str(config_path))
            validate_vllm_config(config, lock)
            bindings = load_runtime_bindings(
                args.runtime_bindings.resolve(),
                required_endpoint_ids(config),
            )
            simulation = Simulation(
                config,
                output_root=runs_root,
                repo_root=REPO_ROOT,
                runtime_bindings=bindings,
            )
            simulation.run()
            run_dir = Path(simulation.output_dir)
            verified = verify_one_run(
                run_dir,
                row,
                config,
                args.source_git_sha,
                forbidden,
            )
            state["runs"].append(verified)
            state["completed_runs"] += 1
            state["completed_logical_llm_calls"] += verified["logical_llm_calls"]
            state["completed_http_attempts"] += verified["http_attempts"]
            atomic_write_json(state_path, state)
    except WorkerError as error:
        state["status"] = "failed"
        state["failure_code"] = error.code
        state["end_time_utc"] = utc_now_iso()
        atomic_write_json(state_path, state)
        raise
    except BaseException:
        state["status"] = "failed"
        state["failure_code"] = "managed_worker_error"
        state["end_time_utc"] = utc_now_iso()
        atomic_write_json(state_path, state)
        raise

    if (
        state["completed_runs"] != 30
        or state["completed_logical_llm_calls"] != 72000
        or state["completed_http_attempts"] != 72000
    ):
        state["status"] = "failed"
        state["failure_code"] = "worker_completion_envelope_mismatch"
        state["end_time_utc"] = utc_now_iso()
        atomic_write_json(state_path, state)
        raise WorkerError("worker_completion_envelope_mismatch")
    state["status"] = "completed"
    state["end_time_utc"] = utc_now_iso()
    atomic_write_json(state_path, state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", choices=("a", "b"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-git-sha", required=True)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--runtime-bindings", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(args)
    except (WorkerError, PublicVllmError, OSError, ValueError, subprocess.SubprocessError):
        print("FAIL: public formal matrix worker stopped")
        return 1
    except KeyboardInterrupt:
        print("FAIL: public formal matrix worker interrupted")
        return 130
    except BaseException:
        print("FAIL: unexpected public formal matrix worker error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
