#!/usr/bin/env python3
"""Read-only, prospectively specified mixed-population refuge-layout analysis."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.disaster import contains_warning_identifier, parse_disaster_scenario
from engine.provenance import collect_git_info, file_manifest
from tools.artifact_boundaries import find_immutable_artifact_ancestor, is_within
from tools.build_refuge_layout_study import OUTPUT_DIR, load_verified_manifest
from tools.disaster_metric_v2_core import derive_disaster_metrics_v2, read_jsonl_source_records
from tools.run_disaster_behavior_pilot import public_tree_safe
from tools.scan_publication import scan_text
from tools.validate_run import ALL_COUNTERS, validate_run

METRIC_VERSION = "refuge-layout-study-metric-v1.0.0"
SPEC = Path("docs/REFUGE_LAYOUT_STUDY_METRIC_V1_SPEC.md")
WARNING_SPEC = Path("docs/DISASTER_METRIC_V2_SPEC.md")
INPUTS = ("positions.jsonl", "memory_reasoning.jsonl", "phase1_raw.jsonl", "messages.jsonl", "warning_events.jsonl")
IMPLEMENTATIONS = (Path(__file__).relative_to(REPO_ROOT), SPEC, WARNING_SPEC,
    Path("docs/EXPERIMENT_PROTOCOL_REFUGE_LAYOUT_STUDY_V1.md"),
    Path("tools/build_refuge_layout_study.py"), Path("tools/disaster_metric_v2_core.py"),
    Path("tools/validate_run.py"), Path("tools/scan_publication.py"),
    Path("tools/run_disaster_behavior_pilot.py"), Path("tools/run_public_vllm.py"),
    Path("tools/artifact_boundaries.py"), Path("engine/disaster.py"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def jsonl(rows):
    return b"".join((json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8") for row in rows)


def reference(source, run_id):
    return {"run_id": run_id, **dict(source.reference or {})}


def observation(source, run_id):
    return {"reference": reference(source, run_id), "raw_row": dict(source.value)}


def index_records(records):
    indexed = {(r.value["step"], r.value["agent_id"]): r for r in records}
    require(len(indexed) == len(records), "duplicate step/agent row")
    return indexed


def reject_links(paths):
    for path in paths:
        require(not path.is_symlink() and not (path.exists() and
            getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            "symlink or reparse point rejected")


def snapshot(path):
    reject_links((path, *path.parents, *path.rglob("*")))
    require(path.is_dir() and not path.is_symlink(), "input must be a regular directory")
    require(not any(p.is_symlink() for p in path.rglob("*")), "symlink in input")
    require(public_tree_safe(path, []), "input publication boundary failure")
    return {p.relative_to(path).as_posix(): file_manifest(p) for p in sorted(path.rglob("*")) if p.is_file()}


def destination_path(path, runs_root):
    require(not os.path.lexists(path), "derived output collision")
    require(re.fullmatch(re.escape(METRIC_VERSION) + r"_\d{8}T\d{6}(?:\d{6})?Z", path.name), "versioned UTC timestamp required")
    reject_links((path, *path.parents))
    resolved = path.resolve()
    require(not find_immutable_artifact_ancestor(resolved) and not is_within(resolved, runs_root.resolve()), "output inside immutable/raw data")
    require(resolved.parent == (REPO_ROOT / "derived").resolve(), "output must be directly below repository derived")
    return resolved


def verify_frozen_source(source_sha, paths):
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source_sha)), "invalid inference source SHA")
    for path in paths:
        relative = path.relative_to(REPO_ROOT).as_posix()
        result = subprocess.run(["git", "show", f"{source_sha}:{relative}"], cwd=REPO_ROOT,
            capture_output=True, check=False)
        require(result.returncode == 0 and result.stdout == path.read_bytes(),
            "analysis/config/spec bytes differ from inference source")


def calculate_run(meta, records):
    """Pure mechanical derivation after public callers validate the raw run."""
    run_id = meta["run_id"]
    duration, count = meta["expected_steps"], meta["expected_agents"]
    config = meta["config"]
    scenario = parse_disaster_scenario(config["scenario"], half_space_size=config["simulation"]["half_space_size"], duration=duration, total_agents=count)
    pos = index_records(records["positions.jsonl"])
    act = index_records(records["memory_reasoning.jsonl"])
    p1 = index_records(records["phase1_raw.jsonl"])
    require(set(pos) == {(s, a) for s in range(duration + 1) for a in range(count)}, "incomplete positions")
    expected = {(s, a) for s in range(1, duration + 1) for a in range(count)}
    require(set(act) == set(p1) == expected, "incomplete actions/messages")
    labels = {a: {"bloc": pos[0, a].value["bloc"], "model": pos[0, a].value["model"]} for a in range(count)}
    horizons = [t for t in (60, 120) if t <= duration]
    require(horizons, "study run must cover checkpoint 60")
    steps, histories = [], {a: [] for a in range(count)}
    for s in range(duration + 1):
        for a in range(count):
            source = pos[s, a]
            row = source.value
            x, y = row["position"]
            distance = scenario.shortest_refuge_distance(x, y)
            refuge = scenario.refuge_for(x, y)
            refuge_id = refuge.refuge_id if refuge else None
            hazardous = scenario.is_hazardous(s, x, y)
            require(row["shortest_refuge_distance"] == distance and row["refuge_id"] == refuge_id and row["hazardous"] is hazardous, "logged geometry disagrees with config")
            previous = pos.get((s - 1, a))
            action = act.get((s, a))
            if action:
                require(action.value["position"] == previous.value["position"], "action position is not the pre-movement position")
            moved = (row["position"] != previous.value["position"]) if previous else None
            value = {"run_id": run_id, "step": s, "agent_id": a, **labels[a],
                "position": list(row["position"]), "distance": distance,
                "hazardous": hazardous, "refuge_id": refuge_id,
                "action": action.value["action"] if action else None,
                "direction": action.value["direction"] if action else None,
                "position_changed": moved,
                "displacement": [row["position"][i] - previous.value["position"][i] for i in (0, 1)] if previous else None,
                "blocked_move": bool(action and action.value["action"] == "move" and not moved),
                "remaining_moves": {str(t): t - s for t in horizons if s <= t},
                "reachability_margin": {str(t): t - s - distance for t in horizons if s <= t},
                "position_reference": reference(source, run_id),
                "previous_position_reference": reference(previous, run_id) if previous else None,
                "action_reference": reference(action, run_id) if action else None}
            steps.append(value)
            histories[a].append(value)
    warning = derive_disaster_metrics_v2(run_meta=meta, positions=records["positions.jsonl"], phase1=records["phase1_raw.jsonl"], messages=records["messages.jsonl"], warning_events=records["warning_events.jsonl"])
    exposures = [r for r in records["warning_events.jsonl"] if r.value["event_type"] == "warning_exposure"]
    reuse = [r for r in warning["outputs"] if r["later_step_reuse_eligible"] and r["exact_warning_id_carrier"]]
    agent_checkpoints, checkpoints = [], []
    for t in horizons:
        per_agent = []
        for a in range(count):
            history = histories[a][:t + 1]
            arrival = next((r for r in history if r["refuge_id"] is not None), None)
            negative = next((r for r in history if r["reachability_margin"][str(t)] < 0), None)
            choices = Counter(r["action"] for r in history[1:])
            directions = Counter(r["direction"] for r in history[1:] if r["action"] == "move")
            exposure_rows = [r for r in exposures if r.value["recipient_id"] == a and r.value["step"] <= t]
            reuse_rows = [r for r in reuse if r["agent_id"] == a and r["step"] <= t]
            value = {"run_id": run_id, "checkpoint": t, "agent_id": a, **labels[a],
                "initial_distance": history[0]["distance"], "final_distance": history[-1]["distance"],
                "minimum_distance": min(r["distance"] for r in history),
                "first_arrival_step": arrival["step"] if arrival else None,
                "arrival_right_censored": arrival is None, "arrival_censor_step": t if arrival is None else None,
                "first_arrival_reference": arrival["position_reference"] if arrival else None,
                "final_refuge_id": history[-1]["refuge_id"],
                "hazard_agent_steps": sum(r["hazardous"] for r in history[1:]),
                "move": choices["move"], "stay": choices["stay"],
                "directions": {d: directions[d] for d in ("up", "down", "left", "right")},
                "blocked_moves": sum(r["blocked_move"] for r in history[1:]),
                "first_negative_margin_snapshot": negative["step"] if negative else None,
                "first_negative_margin_reference": negative["position_reference"] if negative else None,
                "initial_position_reference": history[0]["position_reference"],
                "final_position_reference": history[-1]["position_reference"],
                "warning_exposure_count": len(exposure_rows),
                "first_exposure_step": min((r.value["step"] for r in exposure_rows), default=None),
                "first_reuse_step": min((r["step"] for r in reuse_rows), default=None),
                "later_reuse_output_count": len(reuse_rows),
                "reuse_right_censored": bool(exposure_rows and not reuse_rows),
                "reuse_censor_step": t if exposure_rows and not reuse_rows else None}
            per_agent.append(value)
        deliveries = Counter()
        for source in records["messages.jsonl"]:
            m = source.value
            if m["step"] > t:
                continue
            sender = m["sender_id"]
            carrier = contains_warning_identifier(m["message"], scenario.official_warning.warning_id)
            for receiver in m["receiver_ids"]:
                cross = labels[sender]["model"] != labels[receiver]["model"]
                deliveries["message_delivery_edges"] += 1
                deliveries["cross_model_delivery_edges"] += int(cross)
                deliveries["exact_id_delivery_edges"] += int(carrier)
                deliveries["cross_model_exact_id_delivery_edges"] += int(cross and carrier)
        checkpoint = {"checkpoint": t, "agent_count": count,
            "arrived_agent_count": sum(r["first_arrival_step"] is not None for r in per_agent),
            "final_refuge_occupancy": sum(r["final_refuge_id"] is not None for r in per_agent),
            "hazard_agent_steps": sum(r["hazard_agent_steps"] for r in per_agent),
            "mean_final_distance": sum(r["final_distance"] for r in per_agent) / count,
            "mean_initial_distance": sum(r["initial_distance"] for r in per_agent) / count,
            "move": sum(r["move"] for r in per_agent), "stay": sum(r["stay"] for r in per_agent),
            "blocked_moves": sum(r["blocked_moves"] for r in per_agent),
            "warning_exposed_agents": sum(r["warning_exposure_count"] > 0 for r in per_agent),
            "official_exposure_events": sum(r.value["step"] <= t and r.value["source_type"] == "official" for r in exposures),
            "relay_exposure_events": sum(r.value["step"] <= t and r.value["source_type"] == "agent_relay" for r in exposures),
            "official_exposed_agents": len({r.value["recipient_id"] for r in exposures if r.value["step"] <= t and r.value["source_type"] == "official"}),
            "relay_exposed_agents": len({r.value["recipient_id"] for r in exposures if r.value["step"] <= t and r.value["source_type"] == "agent_relay"}),
            "warning_reused_agents": sum(r["first_reuse_step"] is not None for r in per_agent),
            "later_reuse_outputs": sum(r["later_reuse_output_count"] for r in per_agent),
            **{k: deliveries[k] for k in ("message_delivery_edges", "cross_model_delivery_edges", "exact_id_delivery_edges", "cross_model_exact_id_delivery_edges")}}
        agent_checkpoints.extend(per_agent)
        checkpoints.append(checkpoint)
    return {"checkpoints": checkpoints, "agent_checkpoints": agent_checkpoints, "agent_steps": steps, "warning": warning,
            "initial_signature": [{"agent_id": a, "position": pos[0, a].value["position"], **labels[a]} for a in range(count)]}


def prefix_difference(left, right, count, horizon=60):
    lp, rp = (index_records(r["positions.jsonl"]) for r in (left, right))
    lm, rm = (index_records(r["phase1_raw.jsonl"]) for r in (left, right))
    la, ra = (index_records(r["memory_reasoning.jsonl"]) for r in (left, right))
    for step in range(1, horizon + 1):
        for phase in ("phase1", "phase3", "post_movement"):
            for aid in range(count):
                key = (step, aid)
                if phase == "phase1":
                    values = [d[key].value["parsed"] for d in (lm, rm)]
                elif phase == "phase3":
                    values = [{k: d[key].value[k] for k in ("action", "direction", "memory", "reasoning")} for d in (la, ra)]
                else:
                    values = [d[key].value["position"] for d in (lp, rp)]
                if values[0] != values[1]:
                    return {"step": step, "phase": phase, "agent_id": aid}
    return None


def select_example(completed):
    for item in completed:
        if item["duration"] != 60:
            continue
        rec, run_id = item["records"], item["run_id"]
        labels = {r["agent_id"]: r["model"] for r in item["calculated"]["initial_signature"]}
        phase1 = index_records(rec["phase1_raw.jsonl"])
        warning_id = item["config"]["scenario"]["official_warning"]["warning_id"]
        for source in sorted(rec["messages.jsonl"], key=lambda r: (r.value["step"], r.value["sender_id"])):
            row = source.value
            if row["step"] < 10 or not contains_warning_identifier(row["message"], warning_id):
                continue
            for receiver in sorted(row["receiver_ids"]):
                sender, step = row["sender_id"], row["step"]
                if labels[sender] == labels[receiver]:
                    continue
                matching = [r for r in rec["warning_events.jsonl"] if r.value["event_type"] == "warning_exposure" and r.value["step"] == step and r.value["recipient_id"] == receiver and r.value.get("sender_id") == sender]
                require(matching, "exact-ID delivery lacks matching receiver exposure")
                prior = [r for r in rec["warning_events.jsonl"] if r.value["event_type"] == "warning_exposure" and r.value["recipient_id"] == sender and (r.value["step"] < step or (r.value["step"] == step and r.value["source_type"] == "official"))]
                return {"run_id": run_id, "layout": item["layout"], "step": step,
                    "sender_id": sender, "receiver_id": receiver, "sender_model": labels[sender], "receiver_model": labels[receiver],
                    "sender_had_prior_exposure": bool(prior),
                    "phase1": observation(phase1[step, sender], run_id), "delivery": observation(source, run_id),
                    "receiver_exposures": [observation(r, run_id) for r in matching],
                    "receiver_later_reuse_inferred": False}
    return None


def compare_runs(completed):
    paired, extensions, independent = [], [], []
    by_condition = {(r["seed"], r["layout"], r["duration"]): r for r in completed}
    for seed in sorted({r["seed"] for r in completed}):
        for duration in (60, 120):
            runs = [by_condition.get((seed, layout, duration)) for layout in ("edge", "inset")]
            if not all(runs):
                paired.append({"seed": seed, "duration": duration, "eligible": False})
                continue
            left, right = runs
            require(left["calculated"]["initial_signature"] == right["calculated"]["initial_signature"], "layout pair initial world/assignment mismatch")
            comparisons = []
            for lc, rc in zip(left["calculated"]["checkpoints"], right["calculated"]["checkpoints"]):
                comparisons.append({"checkpoint": lc["checkpoint"], "edge": lc, "inset": rc,
                    "inset_minus_edge": {k: rc[k] - lc[k] for k in ("arrived_agent_count", "final_refuge_occupancy", "hazard_agent_steps", "mean_final_distance")}})
            paired.append({"seed": seed, "duration": duration, "eligible": True,
                "initial_positions_and_assignment_equal": True, "run_ids": [r["run_id"] for r in runs], "comparisons": comparisons})
        for layout in ("edge", "inset"):
            short, long = (by_condition.get((seed, layout, t)) for t in (60, 120))
            if short and long:
                require(short["calculated"]["initial_signature"] == long["calculated"]["initial_signature"], "horizon pair initial world/assignment mismatch")
                difference = prefix_difference(short["records"], long["records"], short["meta"]["expected_agents"])
                independent.append({"seed": seed, "layout": layout, "run_ids": [short["run_id"], long["run_id"]],
                    "prefix60_equal": difference is None, "first_prefix_difference": difference})
            if long:
                checkpoints = long["calculated"]["checkpoints"]
                agents = [r for r in long["calculated"]["agent_checkpoints"] if r["checkpoint"] == 120]
                extensions.append({"run_id": long["run_id"], "seed": seed, "layout": layout,
                    "checkpoint60": checkpoints[0], "checkpoint120": checkpoints[1],
                    "first_arrivals_steps61_to120": sum(r["first_arrival_step"] is not None and 61 <= r["first_arrival_step"] <= 120 for r in agents)})
    return {"layout_pairs": paired, "independent_horizon_pairs": independent, "within_120_extensions": extensions}


def markdown(summary):
    lines = ["# Mixed-population refuge layout study", "", "Descriptive observations; all planned conditions are retained.", "",
        "| Layout | Duration | Status | Calls | Eligible |", "| --- | ---: | --- | ---: | --- |"]
    for r in summary["runs"]:
        lines.append(f"| {r['layout']} | {r['duration']} | {r['status']} | {r.get('calls')} | {r['eligible']} |")
    lines += ["", "| Layout | Run duration | Checkpoint | Arrived | Occupants | Mean distance | Hazard agent-steps | Exposed | Reused | Cross-model exact-ID delivery edges |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in summary["runs"]:
        for c in r.get("checkpoints", []):
            lines.append(f"| {r['layout']} | {r['duration']} | {c['checkpoint']} | {c['arrived_agent_count']} | {c['final_refuge_occupancy']} | {c['mean_final_distance']:.3f} | {c['hazard_agent_steps']} | {c['warning_exposed_agents']} | {c['warning_reused_agents']} | {c['cross_model_exact_id_delivery_edges']} |")
    lines += ["", "See summary.json for layout/horizon comparisons and example.json for the prospectively selected delivery example (including null).",
              "Raw line references, checkpoint censoring and geometric reachability margins are retained in the JSONL files.",
              "Exposure, generated exact-ID reuse, actual delivery and refuge arrival are separate observations; no causal model effect or internal cognition is inferred.", ""]
    return "\n".join(lines).encode("utf-8")


def analyze(manifest_path, runs_root, output_dir, spec_sha256):
    output = destination_path(output_dir, runs_root)
    require(re.fullmatch(r"[0-9a-f]{64}", spec_sha256) and file_manifest(REPO_ROOT / SPEC)["sha256"] == spec_sha256, "metric spec digest mismatch")
    require(manifest_path.name == "manifest.json", "frozen manifest.json required")
    manifest = load_verified_manifest(manifest_path.parent)
    watched = {manifest_path: file_manifest(manifest_path), **{REPO_ROOT / p: file_manifest(REPO_ROOT / p) for p in IMPLEMENTATIONS}}
    source_state = collect_git_info(REPO_ROOT)
    require(source_state["git_sha"] and source_state["git_dirty"] is False, "analysis requires a clean source commit")
    statuses, completed, trees = [], [], {}
    for row in manifest["rows"]:
        config_path = manifest_path.parent / row["filename"]
        watched[config_path] = file_manifest(config_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        duration = config["simulation"]["duration"]
        status = {"run_id": row["run_id"], "layout": row["layout"], "duration": duration,
            "seed": config["simulation"]["seed"], "composition": "mixed", "status": "not_started_or_unavailable", "eligible": False}
        raw = runs_root / ("output_" + row["run_id"])
        statuses.append(status)
        if not os.path.lexists(raw):
            continue
        trees[raw] = snapshot(raw)
        if "run_meta.json" not in trees[raw]:
            status["status"] = "metadata_missing"
            continue
        meta = json.loads((raw / "run_meta.json").read_text(encoding="utf-8"))
        report = validate_run(raw, strict=True)
        count = sum(b["num_agents"] for b in config["blocs"])
        counters = {key: meta.get(key) for key in ALL_COUNTERS}
        expected_calls = duration * count * 2
        status.update({"status": meta.get("status"), "calls": meta.get("logical_llm_calls"), "counters": counters,
            "strict_validation": {"valid": report.valid, "errors": report.errors, "unverifiable": report.unverifiable},
            "source_sha": meta.get("git_sha"), "source_dirty": meta.get("git_dirty"),
            "config_sha256": watched[config_path]["sha256"], "raw_file_manifests": trees[raw]})
        eligible = (report.valid and meta.get("status") == "completed" and meta.get("aborted") is False
            and meta.get("run_id") == row["run_id"] and meta.get("config") == config and meta.get("git_dirty") is False
            and isinstance(meta.get("git_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", meta["git_sha"])
            and meta.get("completed_steps") == meta.get("expected_steps") == duration
            and meta.get("observed_agents") == meta.get("expected_agents") == count
            and counters.get("logical_llm_calls") == counters.get("http_attempts") == expected_calls
            and all(value == 0 for key, value in counters.items() if key not in ("logical_llm_calls", "http_attempts")))
        if not eligible:
            if status["status"] == "completed":
                status["status"] = "completed_but_ineligible"
            continue
        records = {name: read_jsonl_source_records(raw / name) for name in INPUTS}
        calculated = calculate_run(meta, records)
        status.update({"eligible": True, "checkpoints": calculated["checkpoints"], "warning_summary": calculated["warning"]["summary"]})
        completed.append({**status, "config": config, "meta": meta, "records": records, "calculated": calculated})
    require(len({r["source_sha"] for r in completed}) <= 1, "completed runs have different inference source commits")
    for source_sha in {r["source_sha"] for r in completed}:
        verify_frozen_source(source_sha, watched)
    example = select_example(completed)
    summary = {"metric_version": METRIC_VERSION, "batch_id": manifest["batch_id"], "planned_run_count": len(statuses),
        "comparison_eligible_run_count": len(completed), "runs": statuses, **compare_runs(completed),
        "example": example, "example_absence_reason": None if example else "no_eligible_cross_model_exact_id_delivery_in_60_step_runs",
        "research_eligible": False, "formal_eligible": False}
    labels = lambda r: {"layout": r["layout"], "duration": r["duration"], "seed": r["seed"], "run_id": r["run_id"]}
    files = {"summary.json": encoded(summary), "example.json": encoded({"example": example, "absence_reason": summary["example_absence_reason"]}),
        "agent_steps.jsonl": jsonl([{**labels(r), **v} for r in completed for v in r["calculated"]["agent_steps"]]),
        "agent_checkpoints.jsonl": jsonl([{**labels(r), **v} for r in completed for v in r["calculated"]["agent_checkpoints"]]),
        "warning_agents.jsonl": jsonl([{**labels(r), **v} for r in completed for v in r["calculated"]["warning"]["agents"]]),
        "warning_outputs.jsonl": jsonl([{**labels(r), **v} for r in completed for v in r["calculated"]["warning"]["outputs"]]),
        "report.md": markdown(summary),
        "analysis_meta.json": encoded({"metric_version": METRIC_VERSION, "metric_spec_sha256": spec_sha256,
            "warning_metric_spec_sha256": file_manifest(REPO_ROOT / WARNING_SPEC)["sha256"],
            "manifest_sha256": watched[manifest_path]["sha256"], "source_state": source_state,
            "inference_source_commits": sorted({r["source_sha"] for r in completed}),
            "implementation_manifests": {p.as_posix(): watched[REPO_ROOT / p] for p in IMPLEMENTATIONS},
            "input_scan_passed": True, "raw_modified": False})}
    import hashlib
    files["derived_manifest.json"] = encoded({"metric_version": METRIC_VERSION, "algorithm": "sha256", "files": {
        n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b), "lines": b.count(b"\n")} for n, b in files.items()}})
    require(all(not scan_text(n, b.decode("utf-8")) for n, b in files.items()), "unsafe derived content")
    require(all(file_manifest(p) == before for p, before in watched.items()), "config/spec/source changed during analysis")
    require(all(snapshot(p) == before for p, before in trees.items()), "raw changed during analysis")
    destination_path(output_dir, runs_root)
    output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (output / name).open("xb") as handle:
            handle.write(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=OUTPUT_DIR / "manifest.json")
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metric-spec-sha256", required=True)
    args = parser.parse_args()
    try:
        output = analyze(args.manifest, args.runs_root, args.output_dir, args.metric_spec_sha256)
    except (ValueError, OSError, KeyError) as error:
        print(f"FAIL: refuge layout analysis rejected ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"PASS: derived observations written to {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
