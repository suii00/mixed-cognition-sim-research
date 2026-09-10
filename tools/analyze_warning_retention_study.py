#!/usr/bin/env python3
"""Read-only warning input retention replay, paired metrics and inert HTML view."""
from __future__ import annotations

import argparse
from collections import defaultdict
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.disaster import contains_warning_identifier
from engine.prompts_v3 import format_messages_section
from engine.provenance import collect_git_info, file_manifest
from tools.analyze_refuge_layout_study import (
    calculate_run, encoded, index_records, jsonl, reference, reject_links,
    require, snapshot, verify_frozen_source,
)
from tools.artifact_boundaries import find_immutable_artifact_ancestor, is_within
from tools.disaster_metric_v2_core import read_jsonl_source_records
from tools.scan_publication import scan_text
from tools.validate_run import ALL_COUNTERS, validate_run

METRIC_VERSION = "warning-retention-study-metric-v1.0.0"
HISTORY_VERSION = "warning-retention-history-v1.0.0"
SPEC = Path("docs/WARNING_RETENTION_STUDY_METRIC_V1_SPEC.md")
INPUTS = ("positions.jsonl", "memory_reasoning.jsonl", "phase1_raw.jsonl",
          "messages.jsonl", "warning_events.jsonl")
IMPLEMENTATIONS = (
    Path("tools/analyze_warning_retention_study.py"), SPEC,
    Path("tools/render_warning_retention_study.py"),
    Path("tools/analyze_refuge_layout_study.py"),
    Path("tools/disaster_metric_v2_core.py"), Path("docs/DISASTER_METRIC_V2_SPEC.md"),
    Path("tools/build_warning_retention_study.py"),
    Path("docs/EXPERIMENT_PROTOCOL_WARNING_RETENTION_STUDY_V1.md"),
    Path("engine/agent.py"), Path("engine/sim.py"), Path("engine/prompts_v3.py"),
    Path("engine/prompts.py"), Path("engine/config.py"), Path("engine/message_selection.py"),
    Path("docs/MESSAGE_PRESENTATION_V1_SPEC.md"), Path("docs/MESSAGE_SELECTION_V1_SPEC.md"),
    Path("engine/disaster.py"), Path("engine/provenance.py"),
    Path("tools/validate_run.py"), Path("tools/artifact_boundaries.py"),
    Path("tools/scan_publication.py"), Path("tools/run_disaster_behavior_pilot.py"),
)
POLICIES = {"recent": "recent-v1.0.0", "retained": "retain-official-warning-v1.0.0"}
HAZARD_WORDS = re.compile(r"(?<![a-z])(?:warning|hazard|hazardous|inundation|flood|flooding)(?![a-z])", re.I)
REFUGE_WORDS = re.compile(r"(?<![a-z])(?:refuge|refuges|shelter|shelters|evacuate|evacuation)(?![a-z])", re.I)


def lexical_flags(message, warning_id):
    exact = contains_warning_identifier(message, warning_id)
    hazard, refuge = bool(HAZARD_WORDS.search(message)), bool(REFUGE_WORDS.search(message))
    return {"exact_warning_id": exact, "hazard_word": hazard, "refuge_word": refuge,
            "non_id_review_candidate": not exact and (hazard or refuge)}


def replay_presentations(meta, records, *, observed=True):
    """Reconstruct chronology without calling Agent or its selection methods."""
    config, run_id = meta["config"], meta["run_id"]
    duration, count = meta["expected_steps"], meta["expected_agents"]
    limits = config["agents"]
    history_limit, context = limits["message_history_limit"], limits["message_context_size"]
    require(history_limit >= context >= 1, "positive bounded context required")
    policy = limits.get("message_selection_policy", POLICIES["recent"])
    require(policy in POLICIES.values(), "unknown selection policy")
    histories, retained = {a: [] for a in range(count)}, {}
    events, deliveries = defaultdict(list), defaultdict(list)
    issued = {}
    for source in records["warning_events.jsonl"]:
        row = source.value
        if row["event_type"] == "warning_issued":
            require(row["warning_id"] not in issued, "duplicate warning issuance")
            issued[row["warning_id"]] = source
        elif row["source_type"] == "official":
            events[row["step"]].append(source)
    for source in records["messages.jsonl"]:
        deliveries[source.value["step"]].append(source)
    observed_index = {}
    if observed:
        observed_index = {(r.value["step"], r.value["phase"], r.value["agent_id"]): r
                          for r in records["prompt_inputs.jsonl"]}
        expected = {(s, p, a) for s in range(1, duration + 1)
                    for p in ("phase1", "phase3") for a in range(count)}
        require(len(observed_index) == len(records["prompt_inputs.jsonl"])
                and set(observed_index) == expected, "prompt-input coverage/uniqueness mismatch")
    warning_id = config["scenario"]["official_warning"]["warning_id"]
    rows = []

    def append(a, value, refs):
        histories[a].append((value, refs))
        histories[a] = histories[a][-history_limit:]

    def phase_rows(step, phase):
        for a in range(count):
            if policy == POLICIES["retained"] and a in retained:
                peers = [item for item in histories[a] if item[0].get("source_type") != "official_warning"]
                selected = [retained[a]] + (peers[-(context - 1):] if context > 1 else [])
            else:
                selected = histories[a][-context:]
            values = [copy.deepcopy(item[0]) for item in selected]
            source = observed_index.get((step, phase, a))
            prompt_id = None
            if observed:
                actual = source.value
                require(actual["messages"] == values, "actual selection differs from independent replay")
                require(actual["message_selection_policy"] == policy, "logged selection policy mismatch")
                prompt = actual["prompt"]
                require(actual["prompt_sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                        "actual prompt hash mismatch")
                require(format_messages_section(values) in prompt, "selected items absent from actual prompt")
                prompt_id = contains_warning_identifier(prompt, warning_id)
            selected_official = any(v.get("source_type") == "official_warning"
                                    and v.get("warning_id") == warning_id for v in values)
            rows.append({"run_id": run_id, "step": step, "phase": phase, "agent_id": a,
                "evidence_class": "direct_request_input_observation" if observed else "mechanical_reconstruction",
                "official_received_before_phase": a in retained,
                "official_selected": selected_official,
                "exact_id_in_actual_prompt": prompt_id,
                "selected_message_count": len(values), "selected_messages": values,
                "selected_source_references": [item[1] for item in selected],
                "prompt_input_reference": reference(source, run_id) if source else None,
                "prompt_sha256": source.value["prompt_sha256"] if source else None})

    for step in range(1, duration + 1):
        for source in events[step]:
            event = source.value
            issuance = issued[event["warning_id"]]
            require(issuance.value["step"] == step, "official reception/issuance step mismatch")
            a = event["recipient_id"]
            require(a not in retained, "repeated direct official reception")
            value = {"source_type": "official_warning", "warning_id": event["warning_id"],
                     "payload": issuance.value["payload"], "step": step}
            refs = [reference(source, run_id), reference(issuance, run_id)]
            retained[a] = (value, refs)
            append(a, value, refs)
        phase_rows(step, "phase1")
        for source in sorted(deliveries[step], key=lambda r: r.value["sender_id"]):
            event = source.value
            for a in event["receiver_ids"]:
                append(a, {"sender_id": event["sender_id"], "message": event["message"], "step": step},
                       [reference(source, run_id)])
        phase_rows(step, "phase3")
    return rows


def calculate(meta, records, *, observed=True):
    geometry = calculate_run(meta, records)
    phases = replay_presentations(meta, records, observed=observed)
    phase_index = {(r["step"], r["phase"], r["agent_id"]): r for r in phases}
    config, run_id = meta["config"], meta["run_id"]
    warning = config["scenario"]["official_warning"]
    initial = set(warning["initial_recipient_ids"])
    issue, duration, count = warning["issue_step"], meta["expected_steps"], meta["expected_agents"]
    noninitial = set(range(count)) - initial
    agent_rows = []
    for agent in [r for r in geometry["agent_checkpoints"] if r["checkpoint"] == duration]:
        a = agent["agent_id"]
        absent = next((r for r in phases if r["agent_id"] == a and r["official_received_before_phase"]
                       and not r["official_selected"]), None)
        agent_rows.append({**agent, "initial_official_recipient": a in initial,
            "first_official_absence_after_reception": {k: absent[k] for k in
                ("step", "phase", "prompt_input_reference")} if absent else None,
            "official_presentation_count": {p: sum(r["agent_id"] == a and r["phase"] == p
                and r["official_selected"] for r in phases) for p in ("phase1", "phase3")}})
    speech, edges = [], []
    message_index = {(r.value["step"], r.value["sender_id"]): r for r in records["messages.jsonl"]}
    for source in records["phase1_raw.jsonl"]:
        row = source.value
        message = row["parsed"]["message"]
        delivery = message_index.get((row["step"], row["agent_id"]))
        speech.append({"run_id": run_id, "step": row["step"], "agent_id": row["agent_id"],
            "message": message, **lexical_flags(message, warning["warning_id"]),
            "receiver_ids": list(delivery.value["receiver_ids"]) if delivery else [],
            "speech_reference": reference(source, run_id),
            "delivery_reference": reference(delivery, run_id) if delivery else None})
        if delivery and contains_warning_identifier(message, warning["warning_id"]):
            edges.extend({"step": row["step"], "sender_id": row["agent_id"], "receiver_id": a}
                         for a in delivery.value["receiver_ids"])
    reused = [r for r in geometry["warning"]["outputs"] if r["exact_warning_id_carrier"]
              and r["later_step_reuse_eligible"]]
    primary = {"official_recipient_count": len(initial), "noninitial_agent_count": len(noninitial),
        "post_issue_phase_denominator": len(initial) * (duration - issue + 1),
        "issue_step_phase1_to_phase3_official_loss": sum(
            phase_index[issue, "phase1", a]["official_selected"] and
            not phase_index[issue, "phase3", a]["official_selected"] for a in initial),
        "noninitial_exact_id_peer_recipient_count": len({r["receiver_id"] for r in edges if r["receiver_id"] in noninitial}),
        "noninitial_exact_id_peer_delivery_edges": sum(r["receiver_id"] in noninitial for r in edges),
        "noninitial_later_step_reuse_outputs": sum(r["agent_id"] in noninitial for r in reused),
        "noninitial_later_step_reused_agents": len({r["agent_id"] for r in reused if r["agent_id"] in noninitial}),
        "auxiliary_non_id_lexical_candidate_outputs": sum(r["non_id_review_candidate"] for r in speech)}
    for p in ("phase1", "phase3"):
        selected = [r for r in phases if r["agent_id"] in initial and r["step"] >= issue and r["phase"] == p]
        primary[p + "_official_selected"] = sum(r["official_selected"] for r in selected)
        primary[p + "_actual_prompt_exact_id"] = sum(r["exact_id_in_actual_prompt"] for r in selected) if observed else None
    receptions = [{"run_id": run_id, **dict(r.value), "reference": reference(r, run_id)}
                  for r in records["warning_events.jsonl"] if r.value["event_type"] == "warning_exposure"]
    return {"primary": primary, "geometry": geometry, "agent_phases": phases,
            "agents": agent_rows, "speech": speech, "receptions": receptions}


def paired_comparisons(completed, seeds):
    indexed = {(r["seed"], r["condition"]): r for r in completed}
    pairs = []
    for seed in seeds:
        a, b = (indexed.get((seed, c)) for c in ("recent", "retained"))
        if not a or not b:
            pairs.append({"seed": seed, "eligible": False, "retained_minus_recent": None})
            continue
        require(a["calculated"]["geometry"]["initial_signature"] == b["calculated"]["geometry"]["initial_signature"],
                "paired initial world or model assignment mismatch")
        values = []
        for r in (a, b):
            values.append({**r["calculated"]["primary"], **{k: r["calculated"]["geometry"]["checkpoints"][-1][k]
                for k in ("arrived_agent_count", "final_refuge_occupancy", "mean_final_distance", "hazard_agent_steps")}})
        pairs.append({"seed": seed, "eligible": True, "run_ids": [a["run_id"], b["run_id"]],
            "initial_positions_and_assignment_equal": True,
            "recent": values[0], "retained": values[1],
            "retained_minus_recent": {k: values[1][k] - values[0][k] for k in values[0]
                                       if values[0][k] is not None and values[1][k] is not None}})
    return pairs


def safe_output(path, runs_root, version):
    require(not os.path.lexists(path), "derived output collision")
    require(re.fullmatch(re.escape(version) + r"_\d{8}T\d{6}(?:\d{6})?Z", path.name), "versioned UTC timestamp required")
    reject_links((path, *path.parents))
    output = path.resolve()
    require(output.parent == (REPO_ROOT / "derived").resolve(), "output must be directly below repository derived")
    require(not find_immutable_artifact_ancestor(output) and not is_within(output, runs_root.resolve()), "immutable/raw output rejected")
    return output


def markdown(summary):
    lines = ["# Warning input retention observations", "", "Descriptive paired runs; all planned attempts retained.", "",
        "| Run | Status | Eligible | P1 official | P3 official | Noninitial peer recipients | Noninitial later reused |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for row in summary["runs"]:
        p = row.get("primary", {})
        lines.append("| " + " | ".join(str(v) for v in (row["run_id"], row["status"], row["eligible"],
            p.get("phase1_official_selected"), p.get("phase3_official_selected"),
            p.get("noninitial_exact_id_peer_recipient_count"), p.get("noninitial_later_step_reused_agents"))) + " |")
    lines += ["", "P1/P3 counts concern the six initial official recipients after issue. Reception, presentation, generation, delivery and later-step reuse are separate measures.",
        "Review comparison.html for every agent/step and summary.json for each paired B minus A difference. No semantic adoption or internal cognition is inferred.", ""]
    return "\n".join(lines).encode("utf-8")


def analyze(manifest_path, runs_root, output_dir, spec_sha256, *, historical=False):
    version = HISTORY_VERSION if historical else METRIC_VERSION
    output = safe_output(output_dir, runs_root, version)
    require(file_manifest(REPO_ROOT / SPEC)["sha256"] == spec_sha256, "metric spec hash mismatch")
    require(manifest_path.name == "manifest.json", "manifest.json required")
    reject_links((manifest_path, *manifest_path.parents))
    # Existing manifest verifier checks exact frozen rows and public configs.
    if historical:
        from tools.build_refuge_layout_study import load_verified_manifest
    else:
        from tools.build_warning_retention_study import load_verified_manifest
    manifest = load_verified_manifest(manifest_path.parent)
    watched = {manifest_path: file_manifest(manifest_path)}
    for relative in IMPLEMENTATIONS:
        path = REPO_ROOT / relative
        if path.exists():
            watched[path] = file_manifest(path)
        else:
            require(historical, "missing prospective analysis implementation")
    for path in watched:
        reject_links((path, *path.parents))
        require(not scan_text(path.name, path.read_text(encoding="utf-8")), "unsafe analysis input")
    source_state = collect_git_info(REPO_ROOT)
    require(historical or (source_state["git_sha"] and not source_state["git_dirty"]), "prospective analysis requires clean source")
    statuses, completed, trees = [], [], {}
    for row in manifest["rows"]:
        config_path = manifest_path.parent / row["filename"]
        reject_links((config_path, *config_path.parents))
        watched[config_path] = file_manifest(config_path)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        require(not scan_text(config_path.name, config_path.read_text(encoding="utf-8")), "unsafe config")
        duration, count = config["simulation"]["duration"], sum(b["num_agents"] for b in config["blocs"])
        status = {"run_id": row["run_id"], "seed": row["seed"], "condition": row.get("condition", row.get("layout")),
            "duration": duration, "status": "not_started_or_unavailable", "eligible": False,
            "config_path": config_path.relative_to(REPO_ROOT).as_posix(), "config_sha256": watched[config_path]["sha256"]}
        statuses.append(status)
        raw = runs_root / ("output_" + row["run_id"])
        if not os.path.lexists(raw):
            continue
        trees[raw] = snapshot(raw)
        if "run_meta.json" not in trees[raw]:
            status["status"] = "metadata_missing"
            continue
        meta = json.loads((raw / "run_meta.json").read_text(encoding="utf-8"))
        report = validate_run(raw, strict=True)
        counters = {key: meta.get(key) for key in ALL_COUNTERS}
        status.update({"status": meta.get("status"), "source_sha": meta.get("git_sha"), "source_dirty": meta.get("git_dirty"),
            "calls": meta.get("logical_llm_calls"), "counters": counters, "raw_file_manifests": trees[raw],
            "strict_validation": {"valid": report.valid, "errors": report.errors, "unverifiable": report.unverifiable}})
        eligible = (report.valid and meta.get("status") == "completed" and meta.get("aborted") is False
            and meta.get("run_id") == row["run_id"] and meta.get("config") == config and meta.get("git_dirty") is False
            and isinstance(meta.get("git_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", meta["git_sha"])
            and meta.get("completed_steps") == meta.get("expected_steps") == duration
            and meta.get("observed_agents") == meta.get("expected_agents") == count
            and counters.get("logical_llm_calls") == counters.get("http_attempts") == duration * count * 2
            and all(v == 0 for k, v in counters.items() if k not in ("logical_llm_calls", "http_attempts")))
        if not eligible:
            continue
        records = {name: read_jsonl_source_records(raw / name) for name in INPUTS}
        if not historical:
            records["prompt_inputs.jsonl"] = read_jsonl_source_records(raw / "prompt_inputs.jsonl")
        calculated = calculate(meta, records, observed=not historical)
        status.update({"eligible": True, "primary": calculated["primary"],
            "checkpoints": calculated["geometry"]["checkpoints"], "warning_summary": calculated["geometry"]["warning"]["summary"]})
        completed.append({**status, "meta": meta, "config": config, "calculated": calculated})
    if not historical:
        require(len({r["source_sha"] for r in completed}) <= 1, "inference source commits differ")
        for sha in {r["source_sha"] for r in completed}:
            verify_frozen_source(sha, watched)
    summary = {"metric_version": METRIC_VERSION, "artifact_version": version, "batch_id": manifest["batch_id"],
        "evidence_class": "mechanical_reconstruction" if historical else "direct_request_input_and_mechanical_outcomes",
        "planned_run_count": len(statuses), "comparison_eligible_run_count": len(completed), "runs": statuses,
        "pairs": [] if historical else paired_comparisons(completed, sorted({r["seed"] for r in statuses})),
        "research_eligible": False, "formal_eligible": False}
    files = {"summary.json": encoded(summary), "report.md": markdown(summary)}
    for filename, key in (("agent_phases.jsonl", "agent_phases"), ("agents.jsonl", "agents"), ("speech.jsonl", "speech")):
        files[filename] = jsonl([v for r in completed for v in r["calculated"][key]])
    files["agent_steps.jsonl"] = jsonl([v for r in completed for v in r["calculated"]["geometry"]["agent_steps"]])
    files["warning_outputs.jsonl"] = jsonl([{"run_id": r["run_id"], **v} for r in completed for v in r["calculated"]["geometry"]["warning"]["outputs"]])
    if not historical:
        from tools.render_warning_retention_study import render_html
        files["comparison.html"] = render_html(summary, completed)
    files["analysis_meta.json"] = encoded({"metric_version": METRIC_VERSION, "artifact_version": version,
        "metric_spec_sha256": spec_sha256, "source_state": source_state,
        "inference_source_commits": sorted({r["source_sha"] for r in completed}),
        "implementation_manifests": {p.relative_to(REPO_ROOT).as_posix(): v for p, v in watched.items()},
        "input_scan_passed": True, "raw_modified": False})
    files["derived_manifest.json"] = encoded({"metric_version": METRIC_VERSION, "algorithm": "sha256", "files": {
        n: {"sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b), "lines": b.count(b"\n")} for n, b in files.items()}})
    require(all(not scan_text(n, b.decode("utf-8")) for n, b in files.items()), "unsafe derived content")
    require(all(file_manifest(p) == before for p, before in watched.items()), "watched input changed")
    require(all(snapshot(p) == before for p, before in trees.items()), "raw tree changed")
    require(collect_git_info(REPO_ROOT) == source_state, "analysis source state changed")
    safe_output(output_dir, runs_root, version)
    output.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        with (output / name).open("xb") as handle:
            handle.write(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metric-spec-sha256", required=True)
    parser.add_argument("--historical", action="store_true")
    args = parser.parse_args()
    try:
        output = analyze(args.manifest.resolve(), args.runs_root.resolve(), args.output_dir,
                         args.metric_spec_sha256, historical=args.historical)
        print("PASS: warning retention observations written to " + output.name)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"FAIL: warning retention analysis rejected ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
