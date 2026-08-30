#!/usr/bin/env python3
"""Report a frozen directional audit without promoting aborted cells."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import build_effective_config  # noqa: E402
from engine.provenance import compute_config_hash, file_manifest  # noqa: E402
from tools.directional_amplification_core import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION,
    DirectionalAuditError,
    analyze_run,
    build_audit_bundle,
    json_bytes,
    load_and_validate_plan,
    load_json_unique,
    read_jsonl,
    sha256_file,
)
from tools.scan_publication import scan_text, scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


RESULT_SCHEMA_VERSION = "directional-amplification-incomplete-result-v1.0.0"
DEFAULT_MANIFEST = (
    REPO_ROOT
    / "configs"
    / "directional_amplification_audit_v1"
    / "manifest.json"
)
COUNTER_FIELDS = (
    "logical_llm_calls",
    "http_attempts",
    "generation_retries",
    "transport_failures",
    "syntax_parse_attempt_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
)


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _bloc_for_agent(config: Mapping[str, Any], agent_id: int) -> str:
    first = 0
    for bloc in config["blocs"]:
        final = first + int(bloc["num_agents"])
        if first <= agent_id < final:
            return str(bloc["name"])
        first = final
    raise DirectionalAuditError(f"failure agent ID is outside configured blocs: {agent_id}")


def _verify_raw_manifest(run_dir: Path, meta: Mapping[str, Any]) -> None:
    if meta.get("raw_manifest_status") != "available":
        raise DirectionalAuditError("aborted run raw manifest is unavailable")
    if meta.get("raw_manifest_error") is not None:
        raise DirectionalAuditError("aborted run raw manifest records an error")
    manifest = meta.get("raw_manifest")
    if not isinstance(manifest, dict) or manifest.get("algorithm") != "sha256":
        raise DirectionalAuditError("aborted run raw manifest is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DirectionalAuditError("aborted run raw manifest has no files")
    actual_names = {
        path.name for path in run_dir.iterdir() if path.is_file() and path.suffix == ".jsonl"
    }
    if actual_names != set(files):
        raise DirectionalAuditError("aborted run raw file set differs from its manifest")
    for filename, expected in files.items():
        if not isinstance(expected, dict):
            raise DirectionalAuditError(f"invalid raw manifest entry: {filename}")
        actual = file_manifest(run_dir / filename)
        if any(expected.get(key) != actual[key] for key in ("sha256", "bytes", "lines")):
            raise DirectionalAuditError(f"aborted raw manifest mismatch: {filename}")


def _verify_termination(meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> None:
    if len(rows) != 1:
        raise DirectionalAuditError("aborted run must contain one termination record")
    row = rows[0]
    expected = {
        "schema_version": "1.0.0",
        "event_id": f"{meta.get('run_id')}:termination",
        "run_id": meta.get("run_id"),
        "status": meta.get("status"),
        "aborted": meta.get("aborted"),
        "reason": meta.get("abort_reason"),
        "exception_type": meta.get("failure_exception_type"),
        "failure_step": meta.get("failure_step"),
        "failure_phase": meta.get("failure_phase"),
        "failure_agent_id": meta.get("failure_agent_id"),
        "completed_steps": meta.get("completed_steps"),
        "end_time_utc": meta.get("end_time_utc"),
    }
    if row != expected:
        raise DirectionalAuditError("aborted termination record differs from metadata")


def summarize_aborted_run(
    run_dir: Path,
    config: Mapping[str, Any],
    manifest_row: Mapping[str, Any],
    evidence_root: Path,
) -> dict[str, Any]:
    """Verify an aborted raw tree and return non-semantic failure facts only."""
    if run_dir.is_symlink() or run_dir.name != f"output_{manifest_row['run_id']}":
        raise DirectionalAuditError("aborted run path does not match its immutable run ID")
    meta = load_json_unique(run_dir / "run_meta.json")
    run_id = manifest_row["run_id"]
    expected_config = build_effective_config(copy.deepcopy(dict(config)))
    if meta.get("run_id") != run_id:
        raise DirectionalAuditError(f"aborted run ID differs for {run_id}")
    if meta.get("status") != "aborted" or meta.get("aborted") is not True:
        raise DirectionalAuditError(f"run is not an aborted run: {run_id}")
    if meta.get("git_dirty") is not False:
        raise DirectionalAuditError(f"aborted run source is dirty: {run_id}")
    if meta.get("config") != expected_config:
        raise DirectionalAuditError(f"aborted run config differs: {run_id}")
    if meta.get("config_hash") != compute_config_hash(expected_config):
        raise DirectionalAuditError(f"aborted run config hash differs: {run_id}")
    if expected_config["simulation"].get("research_eligible") is not False:
        raise DirectionalAuditError(f"aborted run became research eligible: {run_id}")
    if meta.get("expected_steps") != manifest_row["expected_steps"]:
        raise DirectionalAuditError(f"aborted run expected steps differ: {run_id}")
    if meta.get("expected_agents") != manifest_row["expected_agents"]:
        raise DirectionalAuditError(f"aborted run expected agents differ: {run_id}")
    if meta.get("observed_agents") != manifest_row["expected_agents"]:
        raise DirectionalAuditError(f"aborted run observed agents differ: {run_id}")
    if not isinstance(meta.get("completed_steps"), int) or not (
        0 <= meta["completed_steps"] < manifest_row["expected_steps"]
    ):
        raise DirectionalAuditError(f"aborted run completed-step count is invalid: {run_id}")
    if scan_tree(run_dir):
        raise DirectionalAuditError(f"aborted run failed publication scan: {run_id}")

    _verify_raw_manifest(run_dir, meta)
    termination = read_jsonl(run_dir / "termination.jsonl")
    _verify_termination(meta, termination)
    attempts = read_jsonl(run_dir / "llm_attempts.jsonl")
    parse_errors = read_jsonl(run_dir / "parse_errors.jsonl")
    if len(attempts) != meta.get("http_attempts"):
        raise DirectionalAuditError(f"aborted attempt count differs: {run_id}")
    if len(parse_errors) != meta.get("syntax_parse_failures"):
        raise DirectionalAuditError(f"aborted parse-error count differs: {run_id}")
    failed = [row for row in attempts if row.get("failure_kind") is not None]
    if len(failed) != 1:
        raise DirectionalAuditError(f"expected exactly one terminal failed attempt: {run_id}")
    if meta.get("syntax_parse_attempt_failures") != len(failed):
        raise DirectionalAuditError(f"aborted failed-attempt counter differs: {run_id}")
    failure = failed[0]
    expected_failure = (
        meta.get("failure_step"),
        meta.get("failure_phase"),
        meta.get("failure_agent_id"),
    )
    observed_failure = (
        failure.get("step"),
        failure.get("phase"),
        failure.get("agent_id"),
    )
    if observed_failure != expected_failure:
        raise DirectionalAuditError(f"failed attempt differs from terminal metadata: {run_id}")
    parse_failure = parse_errors[0]
    if (
        parse_failure.get("step"),
        "phase1" if parse_failure.get("phase") == 1 else "phase3",
        parse_failure.get("agent_id"),
    ) != expected_failure:
        raise DirectionalAuditError(f"parse-error row differs from terminal metadata: {run_id}")

    evidence_path = evidence_root / f"validation-vllm-{run_id}" / "verification.json"
    evidence = load_json_unique(evidence_path)
    tree_sha = _tree_digest(run_dir)
    if evidence.get("run_tree_sha256") != tree_sha:
        raise DirectionalAuditError(f"aborted run tree hash differs from evidence: {run_id}")
    if evidence.get("run_id") != run_id or evidence.get("source_git_sha") != meta.get("git_sha"):
        raise DirectionalAuditError(f"aborted run evidence identity differs: {run_id}")
    if evidence.get("publication_scan_finding_count") != 0:
        raise DirectionalAuditError(f"aborted run evidence reports publication findings: {run_id}")
    if evidence.get("strict_validation_passed") is not False:
        raise DirectionalAuditError(f"aborted run evidence incorrectly reports strict pass: {run_id}")
    if evidence.get("gpu_release_verified") is not True:
        raise DirectionalAuditError(f"aborted run evidence lacks GPU release: {run_id}")
    if evidence.get("all_process_groups_stopped") is not True:
        raise DirectionalAuditError(f"aborted run evidence lacks process cleanup: {run_id}")

    raw_output = failure.get("raw_output")
    raw_output = raw_output if isinstance(raw_output, str) else ""
    usage = failure.get("usage") if isinstance(failure.get("usage"), dict) else {}
    return {
        "run_id": run_id,
        "cell_id": manifest_row["cell_id"],
        "context_condition_id": manifest_row["context_condition_id"],
        "rotation_id": manifest_row["rotation_id"],
        "high_agent_id_bloc": manifest_row["high_agent_id_bloc"],
        "status": "aborted",
        "abort_reason": meta.get("abort_reason"),
        "failure_exception_type": meta.get("failure_exception_type"),
        "failure_step": meta.get("failure_step"),
        "failure_phase": meta.get("failure_phase"),
        "failure_agent_id": meta.get("failure_agent_id"),
        "failure_agent_bloc": _bloc_for_agent(config, int(meta["failure_agent_id"])),
        "completed_steps": meta.get("completed_steps"),
        "expected_steps": meta.get("expected_steps"),
        "partial_action_rows": len(read_jsonl(run_dir / "memory_reasoning.jsonl")),
        "partial_message_rows": len(read_jsonl(run_dir / "messages.jsonl")),
        "parse_error_rows": len(parse_errors),
        "counters": {key: meta.get(key) for key in COUNTER_FIELDS},
        "terminal_attempt": {
            "transport_status": failure.get("transport_status"),
            "parse_status": failure.get("parse_status"),
            "schema_status": failure.get("schema_status"),
            "failure_kind": failure.get("failure_kind"),
            "finish_reason": failure.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "raw_output_characters": len(raw_output),
            "starts_with_json_object": raw_output.lstrip().startswith("{"),
            "ends_with_json_object": raw_output.rstrip().endswith("}"),
        },
        "git_sha": meta.get("git_sha"),
        "git_dirty": meta.get("git_dirty"),
        "run_tree_sha256": tree_sha,
        "strict_validation_passed": False,
        "publication_scan_finding_count": 0,
    }


def _all_threshold_outcome(
    values: Sequence[float], expected_count: int, threshold: float
) -> str:
    if any(value < threshold for value in values):
        return "failed"
    if len(values) < expected_count:
        return "indeterminate"
    return "passed"


def _minimum_support_outcome(
    supporting: int, available: int, expected_count: int, required: int
) -> str:
    if supporting >= required:
        return "passed"
    if supporting + (expected_count - available) < required:
        return "failed"
    return "indeterminate"


def analyze_incomplete_audit(
    manifest_path: Path, runs_root: Path, evidence_root: Path
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_json_unique(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise DirectionalAuditError("unsupported audit manifest schema")
    plan_path = (REPO_ROOT / manifest["plan_path"]).resolve()
    if sha256_file(plan_path) != manifest["plan_sha256"]:
        raise DirectionalAuditError("audit plan SHA-256 differs from manifest")
    plan = load_and_validate_plan(plan_path)
    expected_manifest, expected_configs = build_audit_bundle(plan_path)
    if manifest != expected_manifest:
        raise DirectionalAuditError("audit manifest differs from deterministic builder")

    completed = []
    aborted = []
    statuses: dict[str, str] = {}
    counters = {key: 0 for key in COUNTER_FIELDS}
    source_shas = set()
    for row in manifest["rows"]:
        config_path = manifest_path.parent / row["config_path"]
        if config_path.read_bytes() != expected_configs[row["config_path"]]:
            raise DirectionalAuditError(f"generated config differs: {row['config_path']}")
        config = load_json_unique(config_path)
        run_dir = runs_root.resolve() / f"output_{row['run_id']}"
        if not run_dir.is_dir():
            raise DirectionalAuditError(f"missing run directory: {run_dir}")
        meta = load_json_unique(run_dir / "run_meta.json")
        status = meta.get("status")
        statuses[row["cell_id"]] = str(status)
        for key in COUNTER_FIELDS:
            value = meta.get(key)
            if not isinstance(value, int):
                raise DirectionalAuditError(f"invalid {key} for {row['run_id']}")
            counters[key] += value
        source_shas.add(meta.get("git_sha"))
        if status == "completed":
            strict = validate_run(run_dir, strict=True)
            if not strict.valid:
                raise DirectionalAuditError(f"completed run failed strict validation: {row['run_id']}")
            if scan_tree(run_dir):
                raise DirectionalAuditError(f"completed run failed publication scan: {row['run_id']}")
            completed.append(analyze_run(run_dir, config, row, plan["decision_rules"]))
        elif status == "aborted":
            aborted.append(summarize_aborted_run(run_dir, config, row, evidence_root.resolve()))
        else:
            raise DirectionalAuditError(f"unsupported run status for {row['run_id']}: {status!r}")
    if None in source_shas or len(source_shas) != 1:
        raise DirectionalAuditError("audit runs do not share one recorded source Git SHA")

    by_cell = {row["cell_id"]: row for row in completed}
    pairs = []
    sender_differences = []
    alignment_differences = []
    for rotation in plan["rotations"]:
        rotation_id = rotation["rotation_id"]
        c03_id = f"c03-{rotation_id}"
        c23_id = f"c23-{rotation_id}"
        if c03_id not in by_cell or c23_id not in by_cell:
            pairs.append({
                "rotation_id": rotation_id,
                "status": "unavailable",
                "cell_statuses": {c03_id: statuses.get(c03_id), c23_id: statuses.get(c23_id)},
            })
            continue
        c03 = by_cell[c03_id]
        c23 = by_cell[c23_id]
        c03_metrics = c03["mechanical_derivation"]
        c23_metrics = c23["mechanical_derivation"]
        sender = (
            c03_metrics["visible_context_high_bloc_share"]
            - c23_metrics["visible_context_high_bloc_share"]
        )
        c03_alignment = c03_metrics["non_high_bloc_lag1_alignment_with_high_bloc"]
        c23_alignment = c23_metrics["non_high_bloc_lag1_alignment_with_high_bloc"]
        alignment = (
            None
            if c03_alignment is None or c23_alignment is None
            else c03_alignment - c23_alignment
        )
        sender_differences.append(sender)
        if alignment is not None:
            alignment_differences.append(alignment)
        pairs.append({
            "rotation_id": rotation_id,
            "status": "available",
            "high_agent_id_bloc": c03["high_agent_id_bloc"],
            "c03_minus_c23_visible_high_bloc_share": sender,
            "c03_minus_c23_lag1_alignment": alignment,
            "c03_minus_c23_overall_right_rate": (
                c03_metrics["overall_right_rate"] - c23_metrics["overall_right_rate"]
            ),
            "c03_minus_c23_mean_consensus_share": (
                c03_metrics["mean_step_consensus_share"]
                - c23_metrics["mean_step_consensus_share"]
            ),
        })

    rules = plan["decision_rules"]
    expected_rotations = len(plan["rotations"])
    sender_outcome = _all_threshold_outcome(
        sender_differences,
        expected_rotations,
        float(rules["mechanical_sender_order_minimum_paired_share_difference"]),
    )
    alignment_support = sum(
        value >= float(rules["behavioral_alignment_minimum_paired_difference"])
        for value in alignment_differences
    )
    alignment_outcome = _minimum_support_outcome(
        alignment_support,
        len(alignment_differences),
        expected_rotations,
        int(rules["behavioral_alignment_minimum_rotations"]),
    )
    right_rates = [
        row["mechanical_derivation"]["overall_right_rate"] for row in completed
    ]
    right_outcome = _all_threshold_outcome(
        right_rates,
        manifest["expected_total_runs"],
        float(rules["context_robust_right_rate_minimum"]),
    )

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "audit_id": manifest["audit_id"],
        "protocol_version": manifest["protocol_version"],
        "metric_version": manifest["metric_version"],
        "diagnostic_metric_version": RESULT_SCHEMA_VERSION,
        "plan_sha256": manifest["plan_sha256"],
        "paired_control_sha256": manifest["paired_control_sha256"],
        "source_git_shas": sorted(str(value) for value in source_shas),
        "direct_observation": {
            "matrix_status": "incomplete",
            "expected_runs": manifest["expected_total_runs"],
            "observed_runs": len(completed) + len(aborted),
            "completed_runs": len(completed),
            "aborted_runs": len(aborted),
            "cell_statuses": statuses,
            "aggregate_counters": counters,
            "all_git_clean": all(
                row["direct_observation"]["git_dirty"] is False for row in completed
            ) and all(row["git_dirty"] is False for row in aborted),
            "aborted_run_details": aborted,
        },
        "mechanical_derivation": {
            "completed_runs_only": completed,
            "paired_context_differences": pairs,
        },
        "engineering_decision": {
            "matrix_acceptance": "failed",
            "matrix_acceptance_reason": "one pre-registered cell aborted",
            "mechanical_sender_order_dominance_rule": sender_outcome,
            "behavioral_context_signal_rule": alignment_outcome,
            "behavioral_context_signal_supporting_rotations": alignment_support,
            "context_robust_right_pattern_rule": right_outcome,
        },
        "interpretation_boundary": (
            "Incomplete engineering diagnostics only. Partial aborted-cell actions are "
            "not compared with completed cells. Delivery is exposure, not reuse or "
            "adoption. Generated reasoning is not treated as internal reasoning."
        ),
        "analysis_restrictions_applied": plan["analysis_restrictions"],
        "research_eligible": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = args.output.resolve()
        if output.exists() or output.is_symlink():
            raise DirectionalAuditError(f"refusing to overwrite output: {output}")
        result = analyze_incomplete_audit(
            args.manifest, args.runs_root, args.evidence_root
        )
        data = json_bytes(result)
        if scan_text(output.name, data.decode("utf-8")):
            raise DirectionalAuditError("derived incomplete result failed publication scan")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    except (DirectionalAuditError, OSError, json.JSONDecodeError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: wrote {output}")
    print(f"SHA256: {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
