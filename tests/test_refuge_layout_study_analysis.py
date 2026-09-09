"""Synthetic trajectories verify the prospective measurements, not model behavior."""
import hashlib
import json
from unittest import mock
from types import SimpleNamespace

import pytest

from engine.disaster import parse_disaster_scenario
from tools import analyze_refuge_layout_study as analysis
from tools.disaster_metric_v2_core import SourceRecord


def source(value, filename, line):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    return SourceRecord(value, {"file": filename, "line_number": line,
        "line_sha256": hashlib.sha256(raw).hexdigest(), "line_bytes": len(raw)})


def fixture(duration=120, run_id="synthetic-layout"):
    scenario_config = {"schema_version": "disaster-scenario-v1.0.0", "type": "disaster_v1",
        "communication_mode": "free_text",
        "hazard": {"hazard_id": "hazard-1", "stages": [{"start_step": 10,
            "rectangles": [{"x_min": -25, "x_max": 25, "y_min": -25, "y_max": 0}]}]},
        "refuges": [{"refuge_id": "refuge-1", "rectangle":
            {"x_min": -24, "x_max": -20, "y_min": 1, "y_max": 2}}],
        "official_warning": {"warning_id": "warn-1", "issue_step": 10, "initial_recipient_ids": [0]},
        "initial_eligible_rectangles": [{"x_min": -25, "x_max": -20, "y_min": -4, "y_max": -4}]}
    meta = {"run_id": run_id, "protocol_version": "synthetic-test", "log_schema_version": "2.0.0",
        "metric_version": "disaster-metric-v2.0.0", "status": "completed", "aborted": False,
        "expected_steps": duration, "expected_agents": 3,
        "config": {"simulation": {"duration": duration, "half_space_size": 25, "seed": 6301,
            "research_eligible": False}, "scenario": scenario_config,
            "blocs": [{"name": f"b{a}", "model": f"m{a}", "num_agents": 1} for a in range(3)]}}
    scenario = parse_disaster_scenario(scenario_config, half_space_size=25, duration=duration, total_agents=3)
    values = {name: [] for name in analysis.INPUTS}
    current = [[-24, -4], [-25, -4], [-23, -4]]
    for step in range(duration + 1):
        for aid in range(3):
            label = {"bloc": f"b{aid}", "model": f"m{aid}"}
            if step:
                move = (aid == 0 and 61 <= step <= 65) or (aid == 1 and step == 1)
                direction = ("up" if aid == 0 else "left") if move else None
                values["memory_reasoning.jsonl"].append({"step": step, "agent_id": aid, **label,
                    "position": list(current[aid]), "action": "move" if move else "stay",
                    "direction": direction, "memory": "", "reasoning": ""})
                message = "warn-1" if (aid == 0 and step in (10, 11)) or (aid == 1 and step in (11, 12)) else ""
                values["phase1_raw.jsonl"].append({"step": step, "agent_id": aid, **label,
                    "parsed": {"message": message, "reasoning": ""}})
                if move and aid == 0:
                    current[aid][1] += 1
            x, y = current[aid]
            refuge = scenario.refuge_for(x, y)
            values["positions.jsonl"].append({"step": step, "agent_id": aid, **label,
                "phase": "post_movement" if step else "initial", "position": [x, y],
                "hazardous": scenario.is_hazardous(step, x, y),
                "refuge_id": refuge.refuge_id if refuge else None,
                "shortest_refuge_distance": scenario.shortest_refuge_distance(x, y)})
    values["messages.jsonl"] = [{"step": 11, "sender_id": 0, "sender_bloc": "b0", "sender_model": "m0",
        "receiver_ids": [1], "message": "warn-1", "reasoning": ""}]
    values["warning_events.jsonl"] = [
        {"event_id": "issued", "event_type": "warning_issued", "step": 10, "warning_id": "warn-1",
            "source_type": "official", "recipient_ids": [0], "payload": scenario.warning_payload(), "facts": scenario.warning_facts()},
        {"event_id": "official-0", "event_type": "warning_exposure", "step": 10, "warning_id": "warn-1",
            "recipient_id": 0, "source_type": "official", "sender_id": None},
        {"event_id": "relay-0-1", "event_type": "warning_exposure", "step": 11, "warning_id": "warn-1",
            "recipient_id": 1, "source_type": "agent_relay", "sender_id": 0}]
    records = {name: [source(value, name, line) for line, value in enumerate(rows, 1)] for name, rows in values.items()}
    return meta, records


def item(duration=120, layout="edge"):
    meta, records = fixture(duration, f"synthetic-{layout}-{duration}")
    return {"run_id": meta["run_id"], "seed": 6301, "layout": layout, "duration": duration,
        "meta": meta, "config": meta["config"], "records": records, "calculated": analysis.calculate_run(meta, records)}


def test_arrival_censoring_extension_and_geometry_budget():
    run = item()
    calc = run["calculated"]
    short, long = calc["checkpoints"]
    assert short["arrived_agent_count"] == 0
    assert long["arrived_agent_count"] == long["final_refuge_occupancy"] == 1
    a60, a120 = [a for a in calc["agent_checkpoints"] if a["agent_id"] == 0]
    assert a60["first_arrival_step"] is None and a60["arrival_censor_step"] == 60
    assert a120["first_arrival_step"] == 65 and a120["arrival_censor_step"] is None
    assert a60["hazard_agent_steps"] == 51
    assert a120["hazard_agent_steps"] == 55
    assert a60["first_negative_margin_snapshot"] == 56
    snapshot = next(r for r in calc["agent_steps"] if r["agent_id"] == 0 and r["step"] == 60)
    assert snapshot["remaining_moves"] == {"60": 0, "120": 60}
    assert snapshot["reachability_margin"] == {"60": -5, "120": 55}
    assert snapshot["displacement"] == [0, 0]
    result = analysis.compare_runs([run])
    assert result["within_120_extensions"][0]["first_arrivals_steps61_to120"] == 1


def test_blocked_move_is_not_stay():
    agents = item()["calculated"]["agent_checkpoints"]
    row = next(a for a in agents if a["agent_id"] == 1 and a["checkpoint"] == 60)
    assert (row["move"], row["stay"], row["blocked_moves"]) == (1, 59, 1)
    assert row["directions"]["left"] == 1


def test_exposure_same_step_output_and_later_reuse_remain_distinct():
    calc = item()["calculated"]
    checkpoint = calc["checkpoints"][0]
    assert checkpoint["official_exposure_events"] == checkpoint["relay_exposure_events"] == 1
    assert checkpoint["official_exposed_agents"] == checkpoint["relay_exposed_agents"] == 1
    assert checkpoint["warning_exposed_agents"] == checkpoint["warning_reused_agents"] == 2
    assert checkpoint["later_reuse_outputs"] == 2
    assert checkpoint["cross_model_exact_id_delivery_edges"] == 1
    rows = [a for a in calc["agent_checkpoints"] if a["checkpoint"] == 60]
    assert (rows[0]["first_exposure_step"], rows[0]["first_reuse_step"]) == (10, 11)
    assert (rows[1]["first_exposure_step"], rows[1]["first_reuse_step"]) == (11, 12)


def test_example_uses_actual_delivery_and_manifest_order():
    first, second = item(60, "edge"), item(60, "inset")
    example = analysis.select_example([item(120), first, second])
    assert example["run_id"] == first["run_id"]
    assert (example["step"], example["sender_id"], example["receiver_id"]) == (11, 0, 1)
    assert example["sender_had_prior_exposure"] is True
    assert example["receiver_later_reuse_inferred"] is False
    assert example["delivery"]["reference"]["line_sha256"] == first["records"]["messages.jsonl"][0].reference["line_sha256"]
    first["records"]["messages.jsonl"] = []
    assert analysis.select_example([first, item(120)]) is None


def test_exact_id_delivery_without_exposure_is_rejected():
    run = item(60)
    run["records"]["warning_events.jsonl"] = run["records"]["warning_events.jsonl"][:2]
    with pytest.raises(ValueError, match="matching receiver exposure"):
        analysis.select_example([run])


def test_independent_prefix_equality_and_first_difference_order():
    short, long = item(60), item(120)
    comparison = analysis.compare_runs([short, long])
    assert comparison["independent_horizon_pairs"][0]["prefix60_equal"] is True
    long["records"]["phase1_raw.jsonl"][2].value["parsed"]["message"] = "different"
    long["records"]["memory_reasoning.jsonl"][0].value["memory"] = "different"
    comparison = analysis.compare_runs([short, long])
    assert comparison["independent_horizon_pairs"][0]["first_prefix_difference"] == {"step": 1, "phase": "phase1", "agent_id": 2}
    assert comparison["within_120_extensions"][0]["first_arrivals_steps61_to120"] == 1


def test_initial_world_or_assignment_mismatch_rejected():
    edge, inset = item(60, "edge"), item(60, "inset")
    assert analysis.compare_runs([edge, inset])["layout_pairs"][0]["comparisons"][0]["inset_minus_edge"]["arrived_agent_count"] == 0
    inset["calculated"]["initial_signature"][0]["model"] = "another"
    with pytest.raises(ValueError, match="initial world/assignment mismatch"):
        analysis.compare_runs([edge, inset])


@pytest.mark.parametrize("mutation,error", [
    ("geometry", "logged geometry"), ("coverage", "incomplete positions"),
    ("duplicate", "duplicate step/agent"), ("preposition", "pre-movement position")])
def test_invalid_trajectory_rejected(mutation, error):
    meta, records = fixture(60)
    if mutation == "geometry":
        records["positions.jsonl"][0].value["shortest_refuge_distance"] = 999
    elif mutation == "coverage":
        records["positions.jsonl"].pop()
    elif mutation == "duplicate":
        records["positions.jsonl"].append(records["positions.jsonl"][0])
    else:
        records["memory_reasoning.jsonl"][0].value["position"] = [0, 0]
    with pytest.raises(ValueError, match=error):
        analysis.calculate_run(meta, records)


def test_raw_output_and_collision_rejected(tmp_path):
    name = analysis.METRIC_VERSION + "_20260909T180000Z"
    raw = tmp_path / "runs"
    raw.mkdir()
    with pytest.raises(ValueError, match="immutable/raw"):
        analysis.destination_path(raw / name, raw)
    existing = tmp_path / name
    existing.mkdir()
    with pytest.raises(ValueError, match="collision"):
        analysis.destination_path(existing, raw)
    with pytest.raises(ValueError, match="directly below repository derived"):
        analysis.destination_path(tmp_path / "elsewhere" / name, raw)


def test_inference_commit_must_contain_exact_analysis_bytes():
    path = analysis.REPO_ROOT / analysis.SPEC
    with mock.patch.object(analysis.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=path.read_bytes())):
        analysis.verify_frozen_source("1" * 40, [path])
    with mock.patch.object(analysis.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"post-run change")):
        with pytest.raises(ValueError, match="differ from inference source"):
            analysis.verify_frozen_source("1" * 40, [path])


def test_reparse_point_rejected_without_creation_privilege():
    path = mock.Mock()
    path.is_symlink.return_value = False
    path.exists.return_value = True
    path.lstat.return_value = SimpleNamespace(st_file_attributes=1024)
    with mock.patch.object(analysis.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024, create=True):
        with pytest.raises(ValueError, match="reparse point"):
            analysis.reject_links([path])
