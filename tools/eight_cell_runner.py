"""CLI and lifecycle owner for the deterministic Gate 3 smoke matrix."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from engine import provenance
from engine.llm_client import TelemetryCallback
from engine.parallel_transport import LLMRequest, TransportOutcome
from engine.provenance import atomic_write_json, file_manifest, utc_now_iso
from engine.sim import Simulation, SimulationAbortedError
from tools.eight_cell_core import (
    BATCH_MANIFEST_VERSION,
    MATRIX_SPEC_VERSION,
    MatrixBundle,
    PlanValidationError,
    build_bundle,
    canonical_json_file_bytes,
    load_plan,
    sha256_file,
    write_exclusive_bytes,
    write_static_bundle,
)
from tools.validate_run import validate_run


class BatchCollisionError(FileExistsError):
    """The matrix ID already owns a batch directory."""


class BatchExecutionError(RuntimeError):
    """A claimed batch did not complete."""


class InvocationError(ValueError):
    """CLI syntax or command selection is invalid."""


class Gate3ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvocationError(message)


class ScriptedSmokeTransport:
    """No-network transport whose outputs depend only on request identity."""

    def __init__(self) -> None:
        self.call_count = 0
        self._lock = threading.Lock()

    def __call__(
        self,
        request: LLMRequest,
        telemetry: TelemetryCallback,
    ):
        with self._lock:
            self.call_count += 1
        telemetry("http_attempt", 1)
        if request.phase == "phase1":
            parsed = {
                "message": (
                    f"smoke-message-step-{request.step}-agent-{request.agent_id}"
                ),
                "reasoning": "",
            }
        elif request.phase == "phase3":
            parsed = {
                "action": "stay",
                "direction": "",
                "memory": "",
                "reasoning": "",
            }
        else:
            raise ValueError(f"unsupported scripted phase: {request.phase}")
        raw_output = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        envelope = {
            "message": {"content": raw_output},
            "done_reason": "stop",
            "prompt_eval_count": 1,
            "eval_count": 1,
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        attempt = {
            "generation_attempt": 1,
            "http_attempt": 1,
            "http_status": 200,
            "http_response_body_base64": base64.b64encode(body).decode("ascii"),
            "http_response_bytes": len(body),
            "http_response_sha256": hashlib.sha256(body).hexdigest(),
            "envelope": envelope,
            "raw_output": raw_output,
            "finish_reason": "stop",
            "usage": {"prompt_eval_count": 1, "eval_count": 1},
            "transport_status": "ok",
            "parse_status": "valid",
            "schema_status": "not_checked",
            "failure_kind": None,
            "error_type": None,
        }
        return TransportOutcome(
            parsed=parsed,
            raw_output=raw_output,
            attempts=(attempt,),
        )


def _batch_meta(
    bundle: MatrixBundle,
    git_info: Dict[str, Any],
) -> Dict[str, Any]:
    now = utc_now_iso()
    return {
        "schema_version": "eight-cell-batch-meta-v1.0.0",
        "matrix_spec_version": MATRIX_SPEC_VERSION,
        "matrix_id": bundle.plan["matrix_id"],
        "status": "running",
        "start_time_utc": now,
        "end_time_utc": None,
        "execution_mode": bundle.plan["execution_mode"],
        "research_eligible": False,
        "protocol_version": bundle.plan["protocol_version"],
        "metric_version": bundle.plan["metric_version"],
        "plan_sha256": bundle.plan_sha256,
        "matrix_spec_sha256": bundle.matrix_spec_sha256,
        "base_config_sha256": bundle.plan["base_config"]["sha256"],
        "prompt_sha256": bundle.prompt_sha256,
        "candidate_registry": copy.deepcopy(bundle.plan["candidate_registry"]),
        "backend_freeze": copy.deepcopy(bundle.plan["backend_freeze"]),
        "source_git_sha": git_info.get("git_sha"),
        "source_git_dirty": git_info.get("git_dirty"),
        "source_git_probe_status": git_info.get("git_probe_status"),
        "source_git_probe_errors": copy.deepcopy(
            git_info.get("git_probe_errors", [])
        ),
        "protocol_frozen": False,
        "matrix_plan_frozen": False,
        "run_start_approval_reference": None,
        "planned_runs": len(bundle.rows),
        "started_runs": 0,
        "completed_runs": 0,
        "failed_runs": 0,
        "aborted_runs": 0,
        "not_started_runs": len(bundle.rows),
        "plan_manifest_sha256": None,
        "batch_manifest_sha256": None,
        "failure_type": None,
    }


def _status_counts(rows: list[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "started_runs": sum(row["status"] != "not_started" for row in rows),
        "completed_runs": sum(row["status"] == "completed" for row in rows),
        "failed_runs": sum(row["status"] == "failed" for row in rows),
        "aborted_runs": sum(row["status"] == "aborted" for row in rows),
        "not_started_runs": sum(row["status"] == "not_started" for row in rows),
    }


def _manifest_row(planned: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ordinal": planned["ordinal"],
        "run_id": planned["run_id"],
        "cell_id": planned["cell_id"],
        "replicate_id": planned["replicate_id"],
        "execution_mode": planned["execution_mode"],
        "status": "not_started",
        "config_path": planned["config_path"],
        "config_sha256": planned["config_sha256"],
        "run_directory": f"runs/output_{planned['run_id']}",
        "run_meta_manifest": None,
        "raw_manifest": None,
        "strict_valid": False,
        "strict_errors": [],
        "strict_unverifiable": [],
        "smoke_valid": False,
        "smoke_errors": [],
        "smoke_unverified_research_requirements": [],
        "research_eligible": False,
    }


def _capture_terminal_run_evidence(
    simulation: Optional[Simulation],
    manifest_row: Dict[str, Any],
) -> None:
    if simulation is None:
        return
    run_dir = Path(simulation.output_dir)
    meta_path = run_dir / "run_meta.json"
    if not meta_path.is_file():
        return
    manifest_row["run_meta_manifest"] = file_manifest(meta_path)
    try:
        run_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    manifest_row["raw_manifest"] = copy.deepcopy(run_meta.get("raw_manifest"))


def _finalize_batch(
    batch_dir: Path,
    bundle: MatrixBundle,
    meta: Dict[str, Any],
    rows: list[Dict[str, Any]],
    status: str,
    failure: Optional[BaseException] = None,
) -> Dict[str, Any]:
    counts = _status_counts(rows)
    research_eligible = (
        status == "completed"
        and bool(rows)
        and all(row.get("research_eligible") is True for row in rows)
    )
    manifest = {
        "schema_version": BATCH_MANIFEST_VERSION,
        "matrix_spec_version": MATRIX_SPEC_VERSION,
        "matrix_id": bundle.plan["matrix_id"],
        "status": status,
        "execution_mode": bundle.plan["execution_mode"],
        "research_eligible": research_eligible,
        "plan_sha256": bundle.plan_sha256,
        "matrix_spec_sha256": bundle.matrix_spec_sha256,
        "base_config_sha256": bundle.plan["base_config"]["sha256"],
        "prompt_sha256": bundle.prompt_sha256,
        "plan_manifest_sha256": meta.get("plan_manifest_sha256"),
        "planned_runs": len(rows),
        **counts,
        "runs": rows,
    }
    manifest_path = batch_dir / "batch_manifest.json"
    write_exclusive_bytes(
        manifest_path,
        canonical_json_file_bytes(manifest),
    )
    meta.update(counts)
    meta["status"] = status
    meta["research_eligible"] = research_eligible
    meta["end_time_utc"] = utc_now_iso()
    meta["batch_manifest_sha256"] = sha256_file(manifest_path)
    meta["failure_type"] = type(failure).__name__ if failure is not None else None
    atomic_write_json(batch_dir / "batch_meta.json", meta)
    return manifest


def run_smoke_batch(
    bundle: MatrixBundle,
    output_root: Path | str,
    *,
    repo_root: Optional[Path] = None,
    transport: Optional[Callable] = None,
) -> Path:
    """Claim and execute one sequential scripted batch; never resume it."""
    if bundle.plan.get("execution_mode") != "scripted_smoke":
        raise PlanValidationError(
            "smoke runner requires plan execution_mode scripted_smoke"
        )
    repository = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        git_info = provenance.collect_git_info(repository)
    except Exception:
        git_info = {
            "git_sha": None,
            "git_dirty": None,
            "git_probe_status": "unavailable",
            "git_probe_errors": ["unexpected_probe_error"],
        }
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch_dir = root / f"batch_{bundle.plan['matrix_id']}"
    try:
        batch_dir.mkdir(mode=0o755, parents=False, exist_ok=False)
    except FileExistsError as error:
        raise BatchCollisionError(
            f"batch already exists for matrix ID {bundle.plan['matrix_id']!r}"
        ) from error
    meta = _batch_meta(bundle, git_info)
    atomic_write_json(batch_dir / "batch_meta.json", meta)
    manifest_rows = [_manifest_row(row) for row in bundle.rows]
    failure: Optional[BaseException] = None
    final_status = "failed"
    try:
        write_static_bundle(batch_dir, bundle)
        meta["plan_manifest_sha256"] = sha256_file(
            batch_dir / "plan_manifest.json"
        )
        atomic_write_json(batch_dir / "batch_meta.json", meta)
        runs_dir = batch_dir / "runs"
        runs_dir.mkdir(mode=0o755, parents=False, exist_ok=False)
        invoke = transport or ScriptedSmokeTransport()
        for planned, manifest_row in zip(bundle.rows, manifest_rows):
            config = copy.deepcopy(bundle.configs[planned["run_id"]])
            simulation: Optional[Simulation] = None
            try:
                simulation = Simulation(
                    config,
                    output_root=runs_dir,
                    repo_root=repository,
                    transport=invoke,
                )
                simulation.run()
                run_dir = Path(simulation.output_dir)
                strict = validate_run(run_dir, strict=True)
                from tools.research_validator import _validate_run_evidence

                smoke = _validate_run_evidence(
                    run_dir,
                    batch_dir,
                    dict(planned),
                    "smoke",
                )
                manifest_row.update({
                    "status": "completed",
                    "run_meta_manifest": file_manifest(run_dir / "run_meta.json"),
                    "raw_manifest": copy.deepcopy(
                        simulation.run_lifecycle.meta.get("raw_manifest")
                    ),
                    "strict_valid": strict.valid,
                    "strict_errors": list(strict.errors),
                    "strict_unverifiable": list(strict.unverifiable),
                    "smoke_valid": smoke.exit_code == 0,
                    "smoke_errors": list(smoke.errors),
                    "smoke_unverified_research_requirements": list(
                        smoke.unverified_research_requirements
                    ),
                    "research_eligible": bool(
                        smoke.details.get("derived_research_eligible", False)
                    ),
                })
                if not strict.valid or smoke.exit_code != 0:
                    raise BatchExecutionError(
                        f"smoke validation failed for {planned['run_id']}"
                    )
            except KeyboardInterrupt:
                manifest_row["status"] = "aborted"
                _capture_terminal_run_evidence(simulation, manifest_row)
                raise
            except SimulationAbortedError:
                manifest_row["status"] = "aborted"
                _capture_terminal_run_evidence(simulation, manifest_row)
                raise
            except BaseException:
                manifest_row["status"] = "failed"
                _capture_terminal_run_evidence(simulation, manifest_row)
                raise
            counts = _status_counts(manifest_rows)
            meta.update(counts)
            atomic_write_json(batch_dir / "batch_meta.json", meta)
        if not all(
            row["status"] == "completed"
            and row["strict_valid"]
            and row["smoke_valid"]
            for row in manifest_rows
        ):
            raise BatchExecutionError("not all planned runs passed smoke validation")
        final_status = "completed"
    except KeyboardInterrupt as error:
        failure = error
        final_status = "aborted"
    except SimulationAbortedError as error:
        failure = error
        final_status = "aborted"
    except BaseException as error:
        failure = error
        final_status = "failed"

    try:
        _finalize_batch(
            batch_dir,
            bundle,
            meta,
            manifest_rows,
            final_status,
            failure,
        )
    except BaseException as finalize_error:
        if failure is None:
            failure = finalize_error
        final_status = "failed"
        meta["status"] = "failed"
        meta["end_time_utc"] = utc_now_iso()
        meta["failure_type"] = type(failure).__name__
        try:
            atomic_write_json(batch_dir / "batch_meta.json", meta)
        except BaseException:
            pass

    if failure is not None or final_status != "completed":
        raise BatchExecutionError(
            f"batch {bundle.plan['matrix_id']} ended as {final_status}"
        ) from failure
    return batch_dir


def _verify_spec_hash(expected: str, repo_root: Path) -> str:
    if not isinstance(expected, str):
        raise PlanValidationError("matrix spec SHA-256 is required")
    spec_path = repo_root / "docs" / "EIGHT_CELL_MATRIX_SPEC.md"
    actual = sha256_file(spec_path)
    if actual != expected:
        raise PlanValidationError(
            f"matrix spec SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return actual


def build_parser() -> argparse.ArgumentParser:
    parser = Gate3ArgumentParser(
        prog="python -m tools.eight_cell_runner",
        description="Build and run the fixed Gate 3 eight-cell CPU smoke matrix",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=Gate3ArgumentParser,
    )
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--plan", required=True)
    smoke.add_argument("--plan-sha256", required=True)
    smoke.add_argument("--matrix-spec-sha256", required=True)
    smoke.add_argument("--output-root", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except InvocationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 64
    repository = Path(__file__).resolve().parents[1]
    try:
        spec_sha = _verify_spec_hash(args.matrix_spec_sha256, repository)
        loaded = load_plan(args.plan, args.plan_sha256)
        bundle = build_bundle(loaded, spec_sha, repo_root=repository)
        batch_dir = run_smoke_batch(
            bundle,
            args.output_root,
            repo_root=repository,
        )
    except BatchCollisionError as error:
        print(f"COLLISION: {error}", file=sys.stderr)
        return 3
    except PlanValidationError as error:
        print(f"INVALID: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"INVALID: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    except BatchExecutionError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ABORTED: interrupted", file=sys.stderr)
        return 1
    print(f"PASS: scripted smoke batch completed at {batch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
