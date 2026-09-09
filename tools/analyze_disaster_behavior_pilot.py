#!/usr/bin/env python3
"""Derive the prospectively specified, six-run disaster behavior pilot description."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import load_config  # noqa: E402
from engine.disaster import Rectangle  # noqa: E402
from engine.provenance import collect_git_info, compute_config_hash, file_manifest  # noqa: E402
from tools.artifact_boundaries import (  # noqa: E402
    find_immutable_artifact_ancestor, is_allowed_derived_output_root, is_within,
)
from tools.build_disaster_behavior_pilot import (  # noqa: E402
    AGENT_COUNT, CALLS_PER_RUN, DURATION, METRIC_SPEC_SHA256, MODELS,
    OUTPUT_DIR, SEEDS, load_verified_manifest,
)
from tools.disaster_metric_v2_core import (  # noqa: E402
    DISASTER_METRIC_V2_VERSION, DerivedCollisionError, InputValidationError,
    SourceRecord, canonical_json_bytes, canonical_jsonl_bytes,
    derive_disaster_metrics_v2, read_jsonl_source_records, sha256_bytes,
)
from tools.scan_publication import scan_text, scan_tree  # noqa: E402
from tools.validate_run import ALL_COUNTERS, validate_run  # noqa: E402

METRIC_VERSION = "disaster-behavior-pilot-metric-v1.0.0"
SPEC_PATH = Path("docs/DISASTER_BEHAVIOR_PILOT_METRIC_V1_SPEC.md")
WINDOWS = {"all_steps": (1, DURATION), "steps_ge_10": (10, DURATION)}
DIRECTIONS = ("up", "down", "left", "right")
INPUT_FILES = (
    "run_meta.json", "positions.jsonl", "phase1_raw.jsonl", "messages.jsonl",
    "warning_events.jsonl", "memory_reasoning.jsonl", "llm_attempts.jsonl",
)
SOURCE_PATHS = (
    Path("tools/analyze_disaster_behavior_pilot.py"), Path("tools/artifact_boundaries.py"),
    Path("tools/build_disaster_behavior_pilot.py"), Path("tools/disaster_metric_v2_core.py"),
    Path("tools/scan_publication.py"), Path("tools/validate_run.py"),
    Path("engine/disaster.py"), SPEC_PATH, Path("docs/DISASTER_METRIC_V2_SPEC.md"),
)
CONTEXT_LIMITATION = (
    "Trajectories, messages, memory and input histories may already differ. "
    "Matching positions do not establish identical model inputs; no causal "
    "model effect or internal cognition is inferred."
)


def _scan_bytes(label: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputValidationError("non-UTF-8 analysis input") from error
    findings = scan_text(label, text)
    if findings:
        raise InputValidationError(
            f"publication boundary finding: {findings[0].pattern_id}"
        )


def _reference(source: SourceRecord, run_id: str) -> dict:
    return {"run_id": run_id, **dict(source.reference or {})}


def _observation(source: SourceRecord, run_id: str) -> dict:
    return {"reference": _reference(source, run_id), "raw_row": dict(source.value)}


def _index(rows: Sequence[SourceRecord]) -> dict[tuple[int, int], SourceRecord]:
    result = {}
    for source in rows:
        key = (source.value["step"], source.value["agent_id"])
        if key in result:
            raise InputValidationError("duplicate step/agent observation")
        result[key] = source
    return result


def _action_pair(source: SourceRecord) -> tuple[str, str | None]:
    action, direction = source.value["action"], source.value["direction"]
    if action == "stay" and direction is None:
        return action, direction
    if action == "move" and direction in DIRECTIONS:
        return action, direction
    raise InputValidationError("invalid action/direction in descriptive input")


def summarize_completed_run(meta: Mapping[str, Any], records: Mapping[str, list]) -> dict:
    """Pure derivation; public callers must validate completion before calling."""
    run_id = meta["run_id"]
    actions = records["memory_reasoning.jsonl"]
    positions = records["positions.jsonl"]
    initial = {r.value["agent_id"]: r for r in positions if r.value["phase"] == "initial"}
    post = _index([r for r in positions if r.value["phase"] == "post_movement"])
    action_index = _index(actions)
    expected = {(s, a) for s in range(1, DURATION + 1) for a in range(AGENT_COUNT)}
    if set(action_index) != expected or set(post) != expected or set(initial) != set(range(AGENT_COUNT)):
        raise InputValidationError("incomplete fixed 60-step/four-agent coverage")
    windows = {}
    for name, (first, last) in WINDOWS.items():
        selected = [r for r in actions if first <= r.value["step"] <= last]
        pairs = [_action_pair(r) for r in selected]
        windows[name] = {
            "first_step": first, "last_step": last, "choice_count": len(selected),
            "action_counts": {label: sum(p[0] == label for p in pairs) for label in ("stay", "move")},
            "move_direction_counts": {label: sum(p == ("move", label) for p in pairs) for label in DIRECTIONS},
            "contributing_action_references": [_reference(r, run_id) for r in selected],
        }
    rectangles = [Rectangle(**r["rectangle"]) for r in meta["config"]["scenario"]["refuges"]]
    agents = []
    for agent_id in range(AGENT_COUNT):
        history = [initial[agent_id], *(post[(s, agent_id)] for s in range(1, DURATION + 1))]
        distances = [min(r.manhattan_distance(*p.value["position"]) for r in rectangles) for p in history]
        if any(p.value["shortest_refuge_distance"] != d for p, d in zip(history, distances)):
            raise InputValidationError("logged refuge distance disagrees with rectangle Manhattan distance")
        minimum = min(distances)
        arrival = next((p for p in history if p.value["refuge_id"] is not None), None)
        hazards = {
            name: [p for p in history[1:] if first <= p.value["step"] <= last and p.value["hazardous"]]
            for name, (first, last) in WINDOWS.items()
        }
        agents.append({
            "run_id": run_id, "agent_id": agent_id,
            "initial_nearest_refuge_distance": distances[0],
            "final_nearest_refuge_distance": distances[-1],
            "minimum_nearest_refuge_distance_over_observed_positions": minimum,
            "minimum_distance_position_references": [_reference(p, run_id) for p, d in zip(history, distances) if d == minimum],
            "initial_position": _observation(history[0], run_id),
            "final_position": _observation(history[-1], run_id),
            "hazard_residence_steps": {name: len(rows) for name, rows in hazards.items()},
            "hazard_position_references": {name: [_reference(p, run_id) for p in rows] for name, rows in hazards.items()},
            "first_refuge_arrival_step": (0 if arrival.value["phase"] == "initial" else arrival.value["step"]) if arrival else None,
            "first_refuge_arrival_position": _observation(arrival, run_id) if arrival else None,
            "refuge_arrival_right_censored": arrival is None,
            "refuge_arrival_censor_step": DURATION if arrival is None else None,
        })
    warning = derive_disaster_metrics_v2(
        run_meta=meta, positions=positions, phase1=records["phase1_raw.jsonl"],
        messages=records["messages.jsonl"], warning_events=records["warning_events.jsonl"],
    )
    return {"run_id": run_id, "action_windows": windows, "agents": agents, "warning_metric_v2": warning}


def select_example(completed: Sequence[dict]) -> tuple[dict | None, list[dict]]:
    """Apply the fixed seed/step/agent ordering, never an outcome ranking."""
    seed_checks = []
    eligible = []
    for seed in SEEDS:
        by_model = {r["model_name"]: r for r in completed if r["seed"] == seed}
        check = {"seed": seed, "completed_models": sorted(by_model), "eligible": set(by_model) == set(MODELS)}
        if not check["eligible"]:
            check["initial_positions_equal"] = None
            seed_checks.append(check)
            continue
        runs = [by_model[m] for m in MODELS]
        initials = [{p.value["agent_id"]: p.value["position"] for p in r["records"]["positions.jsonl"] if p.value["phase"] == "initial"} for r in runs]
        check["initial_positions_equal"] = all(p == initials[0] for p in initials)
        if not check["initial_positions_equal"]:
            raise InputValidationError("same-seed model conditions have different initial positions")
        eligible.append((seed, runs))
        seed_checks.append(check)
    for seed, runs in eligible:
        actions = [_index(r["records"]["memory_reasoning.jsonl"]) for r in runs]
        phase1 = [_index(r["records"]["phase1_raw.jsonl"]) for r in runs]
        for step in range(1, DURATION + 1):
            for agent_id in range(AGENT_COUNT):
                key = (step, agent_id)
                if step < 10 or len({_action_pair(a[key]) for a in actions}) == 1:
                    continue
                models = []
                for run, action, p1 in zip(runs, actions, phase1):
                    run_id = run["run_id"]
                    pos = run["records"]["positions.jsonl"]
                    before = next(p for p in pos if p.value["agent_id"] == agent_id and p.value["phase"] == "post_movement" and p.value["step"] == step - 1)
                    after = next(p for p in pos if p.value["agent_id"] == agent_id and p.value["phase"] == "post_movement" and p.value["step"] == step)
                    attempts = [r for r in run["records"]["llm_attempts.jsonl"] if r.value.get("step") == step and r.value.get("agent_id") == agent_id and r.value.get("phase") == "phase3"]
                    models.append({
                        "model_name": run["model_name"], "run_id": run_id,
                        "provenance": run["provenance"],
                        "phase1": _observation(p1[key], run_id),
                        "phase3_action": _observation(action[key], run_id),
                        "phase3_attempts": [_observation(r, run_id) for r in attempts],
                        "position_before": _observation(before, run_id),
                        "position_after": _observation(after, run_id),
                        "warning_exposures_through_step": [_observation(r, run_id) for r in run["records"]["warning_events.jsonl"] if r.value.get("event_type") == "warning_exposure" and r.value.get("recipient_id") == agent_id and r.value["step"] <= step],
                    })
                before_positions = [r["position_before"]["raw_row"]["position"] for r in models]
                return {
                    "seed": seed, "step": step, "agent_id": agent_id,
                    "selection_rule": "first-seed-step-ge10-agent-action-direction-divergence",
                    "models": models,
                    "pre_action_positions_equal": all(p == before_positions[0] for p in before_positions),
                    "first_output_disagreement_at_or_before_selected_phase3": _first_disagreement(actions, phase1, step),
                    "context_limitation": CONTEXT_LIMITATION,
                }, seed_checks
    return None, seed_checks


def _first_disagreement(actions: list, phase1: list, last_step: int) -> dict | None:
    # Phase order is global; all Phase 1 outputs precede any Phase 3 choice.
    for step in range(1, last_step + 1):
        for phase in ("phase1", "phase3"):
            for agent_id in range(AGENT_COUNT):
                key = (step, agent_id)
                values = ([a[key].value["parsed"] for a in phase1] if phase == "phase1" else
                          [{k: a[key].value[k] for k in ("action", "direction", "memory", "reasoning")} for a in actions])
                if any(v != values[0] for v in values):
                    return {"step": step, "agent_id": agent_id, "phase": phase}
    return None


def _snapshot_tree(run: Path) -> dict[str, dict]:
    if run.is_symlink() or any(p.is_symlink() for p in run.rglob("*")):
        raise InputValidationError("run inputs may not be symbolic links")
    if scan_tree(run):
        raise InputValidationError("publication boundary finding in run inputs")
    return {p.relative_to(run).as_posix(): file_manifest(p) for p in sorted(run.rglob("*")) if p.is_file()}


def _layout(output_dir: Path, runs_root: Path) -> Path:
    if os.path.lexists(output_dir):
        raise DerivedCollisionError("immutable analysis destination already exists")
    if not re.fullmatch(re.escape(METRIC_VERSION) + r"_\d{8}T\d{6}(?:\d{6})?Z", output_dir.name):
        raise InputValidationError("analysis destination needs metric version and UTC timestamp")
    resolved = output_dir.resolve(strict=False)
    if find_immutable_artifact_ancestor(resolved) or is_within(resolved, runs_root.resolve(strict=False)):
        raise InputValidationError("analysis destination may not be inside raw or immutable artifacts")
    if not is_allowed_derived_output_root(resolved, repo_root=REPO_ROOT):
        raise InputValidationError("repository analysis output must be below derived/")
    if any(p.is_symlink() for p in (output_dir, *output_dir.parents)):
        raise InputValidationError("analysis destination may not traverse symbolic links")
    return resolved


def _markdown(summary: dict) -> str:
    lines = [
        "# Disaster LLM behavior pilot: descriptive observations", "",
        f"Metric: `{METRIC_VERSION}`; warning metric: `{DISASTER_METRIC_V2_VERSION}`.", "",
        "## Direct run status", "",
        "All six pre-specified runs are retained. Missing data are not completed observations.", "",
        "| Model | Seed | Status | Eligible | Calls | HTTP attempts |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for run in summary["runs"]:
        count = run.get("counters", {})
        lines.append(f"| {run['model_name']} | {run['seed']} | {run['analysis_status']} | {run['comparison_eligible']} | {count.get('logical_llm_calls')} | {count.get('http_attempts')} |")
    lines.extend(["", "Run IDs, config hashes, source commits, complete failure counters and raw references are in `summary.json` and `analysis_meta.json`.", "", "## Mechanical derivation", "", "| Model | Seed | Window | Stay | Move | Up | Down | Left | Right |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"])
    for run in summary["runs"]:
        if not run["comparison_eligible"]:
            continue
        for name, w in run["behavior"]["action_windows"].items():
            a, d = w["action_counts"], w["move_direction_counts"]
            lines.append(f"| {run['model_name']} | {run['seed']} | {name} | {a['stay']} | {a['move']} | {d['up']} | {d['down']} | {d['left']} | {d['right']} |")
    lines.extend(["", "Per-agent distances, hazard residence, refuge arrival, exposure and later exact-ID reuse are in `agents.jsonl` and `warning_agents.jsonl`; all warning outputs are in `warning_outputs.jsonl`.", "", "## Pre-specified example", ""])
    example = summary["example"]
    if example is None:
        lines.append(f"No selected example: `{summary['example_absence_reason']}`.")
    else:
        lines.extend([f"Seed {example['seed']}, step {example['step']}, agent {example['agent_id']}.", "", "| Model | Action | Direction | Position before | Position after | Action raw line |", "| --- | --- | --- | --- | --- | --- |"])
        for row in example["models"]:
            action = row["phase3_action"]["raw_row"]
            ref = row["phase3_action"]["reference"]
            lines.append(f"| {row['model_name']} | {action['action']} | {action['direction']} | {row['position_before']['raw_row']['position']} | {row['position_after']['raw_row']['position']} | `{ref['run_id']}/{ref['file']}:{ref['line_number']}` |")
        lines.extend(["", "All three original Phase 1, Phase 3 and position rows, raw-line hashes and warning exposures are quoted in `example.json`."])
    lines.extend(["", "## Interpretation", "", CONTEXT_LIMITATION, "", "Two world seeds describe examples only. No significance test, model ranking, internal-cognition claim or real-disaster effectiveness claim is made. Exposure is not reuse or adoption. A move choice need not change position.", "", "## Proposal", "", "Use the completed observations and the fixed example, including a null example, to explain what this instrument measures. Any further experiment requires a new prospective protocol.", ""])
    return "\n".join(lines)


def analyze(*, manifest_path: Path, runs_root: Path, output_dir: Path, expected_metric_spec_sha256: str) -> Path:
    """Read/validate/scan all inputs, prepare all bytes, then create a new leaf."""
    destination = _layout(output_dir, runs_root)
    spec_digest = sha256_bytes((REPO_ROOT / SPEC_PATH).read_bytes())
    if expected_metric_spec_sha256 != spec_digest or not re.fullmatch(r"[0-9a-f]{64}", expected_metric_spec_sha256):
        raise InputValidationError("follow-up metric specification digest mismatch")
    if manifest_path.name != "manifest.json" or manifest_path.is_symlink():
        raise InputValidationError("expected a real frozen manifest.json")
    try:
        manifest = load_verified_manifest(manifest_path.parent)
    except (ValueError, OSError) as error:
        raise InputValidationError("frozen manifest/config verification failed") from error
    watched = {manifest_path: file_manifest(manifest_path), REPO_ROOT / SPEC_PATH: file_manifest(REPO_ROOT / SPEC_PATH)}
    _scan_bytes("manifest.json", manifest_path.read_bytes())
    statuses, completed = [], []
    input_trees = {}
    for plan in manifest["rows"]:
        config_path = manifest_path.parent / plan["filename"]
        _scan_bytes(plan["filename"], config_path.read_bytes())
        config = load_config(str(config_path))
        watched[config_path] = file_manifest(config_path)
        status = {
            "run_id": plan["run_id"], "model_name": plan["model_name"], "seed": plan["seed"],
            "public_config_file": plan["filename"], "public_config_sha256": plan["sha256"],
            "effective_config_sha256": compute_config_hash(config), "comparison_eligible": False,
            "analysis_status": "not_started_or_unavailable", "declared_status": None,
            "strict_validation": None, "counters": {k: None for k in ALL_COUNTERS},
            "behavior": None,
        }
        run = runs_root / f"output_{plan['run_id']}"
        if not os.path.lexists(run):
            statuses.append(status)
            continue
        if not run.is_dir() or run.is_symlink():
            raise InputValidationError("run path must be a real directory")
        tree = _snapshot_tree(run)
        input_trees[run] = tree
        status["raw_file_manifests"] = tree
        if "run_meta.json" not in tree:
            status["analysis_status"] = "incomplete_metadata_missing"
            statuses.append(status)
            continue
        meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
        report = validate_run(run, strict=True)
        status["strict_validation"] = {"valid": report.valid, "errors": report.errors, "unverifiable": report.unverifiable}
        status["declared_status"] = meta.get("status")
        status["aborted"] = meta.get("aborted")
        status["completed_steps"] = meta.get("completed_steps")
        status["observed_agents"] = meta.get("observed_agents")
        status["counters"] = {k: meta.get(k) for k in ALL_COUNTERS}
        status["analysis_status"] = meta.get("status", "incomplete_status_missing")
        provenance = {
            "run_id": plan["run_id"], "public_config_file": plan["filename"],
            "public_config_sha256": plan["sha256"], "source_config_sha256": meta.get("config_hash"),
            "source_git_sha": meta.get("git_sha"), "source_git_dirty": meta.get("git_dirty"),
            "source_protocol_version": meta.get("protocol_version"),
            "source_metric_version": meta.get("metric_version"),
            "source_log_schema_version": meta.get("log_schema_version"),
            "source_prompt_contract_version": meta.get("prompt_contract_version"),
            "source_response_contract_version": meta.get("response_contract_version"),
            "run_meta_sha256": tree["run_meta.json"]["sha256"],
            "raw_manifest": meta.get("raw_manifest"),
        }
        status["provenance"] = provenance
        same_config = meta.get("config") == config and meta.get("config_hash") == compute_config_hash(config)
        status["manifest_config_matches"] = same_config
        counters = status["counters"]
        zero_failures = all(counters[k] == 0 for k in ALL_COUNTERS if k not in ("logical_llm_calls", "http_attempts"))
        eligible = (
            report.valid and meta.get("status") == "completed" and meta.get("aborted") is False
            and same_config and meta.get("run_id") == plan["run_id"]
            and meta.get("expected_steps") == DURATION and meta.get("expected_agents") == AGENT_COUNT
            and meta.get("completed_steps") == DURATION and meta.get("observed_agents") == AGENT_COUNT
            and meta.get("git_dirty") is False and isinstance(meta.get("git_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", meta["git_sha"]) is not None
            and counters["logical_llm_calls"] == CALLS_PER_RUN and counters["http_attempts"] == CALLS_PER_RUN
            and zero_failures and meta.get("metric_version") == DISASTER_METRIC_V2_VERSION
            and config["simulation"]["metric_spec_sha256"] == METRIC_SPEC_SHA256
        )
        if meta.get("status") == "completed" and not eligible:
            status["analysis_status"] = "completed_but_ineligible"
        if eligible:
            records = {name: read_jsonl_source_records(run / name) for name in INPUT_FILES if name.endswith(".jsonl")}
            behavior = summarize_completed_run(meta, records)
            status["comparison_eligible"] = True
            status["behavior"] = behavior
            completed.append({**plan, "records": records, "provenance": provenance})
        statuses.append(status)
    if len({r["provenance"]["source_git_sha"] for r in completed}) > 1:
        raise InputValidationError("completed model conditions have different source commits")
    example, seed_checks = select_example(completed)
    eligible_seed_count = sum(c["eligible"] for c in seed_checks)
    summary = {
        "schema_version": METRIC_VERSION, "metric_version": METRIC_VERSION,
        "warning_metric_version": DISASTER_METRIC_V2_VERSION,
        "batch_id": manifest["batch_id"], "planned_run_count": len(statuses),
        "comparison_eligible_run_count": len(completed), "runs": statuses,
        "seed_checks": seed_checks, "example": example,
        "example_absence_reason": None if example else (
            "no_complete_three_model_seed" if not eligible_seed_count
            else "no_action_direction_divergence_in_eligible_seeds_steps_10_to_60"
        ),
        "context_limitation": CONTEXT_LIMITATION,
        "formal_eligible": False, "analysis_class": "prospective-descriptive-followup",
    }
    provenance = {
        "schema_version": METRIC_VERSION, "metric_spec_path": SPEC_PATH.as_posix(),
        "metric_spec_sha256": spec_digest, "warning_metric_spec_sha256": METRIC_SPEC_SHA256,
        "manifest_sha256": watched[manifest_path]["sha256"],
        "source_state": collect_git_info(REPO_ROOT),
        "implementation_manifests": {p.as_posix(): file_manifest(REPO_ROOT / p) for p in SOURCE_PATHS},
        "run_provenance": [{k: v for k, v in row.items() if k != "behavior"} for row in statuses],
        "input_publication_scan": "passed", "raw_modified": False,
    }
    agents, warning_agents, warning_outputs = [], [], []
    for row in statuses:
        if row["comparison_eligible"]:
            behavior = row["behavior"]
            label = {"run_id": row["run_id"], "model_name": row["model_name"], "seed": row["seed"]}
            agents.extend({**label, **a} for a in behavior["agents"])
            warning_agents.extend({**label, **a} for a in behavior["warning_metric_v2"]["agents"])
            warning_outputs.extend({**label, **a} for a in behavior["warning_metric_v2"]["outputs"])
    files = {
        "analysis_meta.json": canonical_json_bytes(provenance, indent=2),
        "summary.json": canonical_json_bytes(summary, indent=2),
        "example.json": canonical_json_bytes({"example": example, "absence_reason": summary["example_absence_reason"]}, indent=2),
        "agents.jsonl": canonical_jsonl_bytes(agents),
        "warning_agents.jsonl": canonical_jsonl_bytes(warning_agents),
        "warning_outputs.jsonl": canonical_jsonl_bytes(warning_outputs),
        "report.md": _markdown(summary).encode("utf-8"),
    }
    files["derived_manifest.json"] = canonical_json_bytes({
        "schema_version": METRIC_VERSION, "algorithm": "sha256",
        "files": {n: {"sha256": sha256_bytes(b), "bytes": len(b), "lines": b.count(b"\n")} for n, b in sorted(files.items())},
    }, indent=2)
    for name, content in files.items():
        _scan_bytes(name, content)
    for path, before in watched.items():
        if file_manifest(path) != before:
            raise InputValidationError("manifest/config/spec changed during analysis")
    for run, before in input_trees.items():
        if _snapshot_tree(run) != before:
            raise InputValidationError("raw inputs changed during analysis")
    _layout(output_dir, runs_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    for name, content in files.items():
        with (destination / name).open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=OUTPUT_DIR / "manifest.json")
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metric-spec-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        output = analyze(manifest_path=args.manifest, runs_root=args.runs_root, output_dir=args.output_dir, expected_metric_spec_sha256=args.metric_spec_sha256)
    except DerivedCollisionError:
        print("ERROR: immutable analysis destination already exists", file=sys.stderr)
        return 3
    except (InputValidationError, ValueError, OSError) as error:
        print(f"ERROR: analysis rejected ({type(error).__name__})", file=sys.stderr)
        return 2
    print(f"Follow-up descriptive output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
