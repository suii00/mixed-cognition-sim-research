import hashlib
import json
import shutil
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tools.disaster_metric_v2_core import (
    DERIVED_DATA_FILES,
    DISASTER_METRIC_V2_VERSION,
    DerivedCollisionError,
    InputValidationError,
    PreparedAnalysis,
    SourceRecord,
    _derived_manifest,
    canonical_json_bytes,
    canonical_warning_contract,
    classify_warning_facts,
    derive_disaster_metrics_v2,
    prepare_run_analysis,
    write_prepared_analysis,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def workspace_tempdir():
    base = REPO_ROOT / ".tmp"
    base.mkdir(exist_ok=True)
    path = base / f"disaster-metric-v2-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path)


def source(value: dict, filename: str, line: int) -> SourceRecord:
    raw = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    return SourceRecord(value, {
        "file": filename,
        "line_number": line,
        "line_sha256": hashlib.sha256(raw).hexdigest(),
        "line_bytes": len(raw),
    })


def scenario() -> dict:
    return {
        "schema_version": "disaster-scenario-v1.0.0",
        "type": "disaster_v1",
        "communication_mode": "free_text",
        "hazard": {
            "hazard_id": "hazard-1",
            "stages": [{
                "start_step": 1,
                "rectangles": [
                    {"x_min": -4, "x_max": 1, "y_min": -4, "y_max": 1}
                ],
            }],
        },
        "refuges": [{
            "refuge_id": "refuge-1",
            "rectangle": {"x_min": 4, "x_max": 5, "y_min": 4, "y_max": 5},
        }],
        "official_warning": {
            "warning_id": "warn-1",
            "issue_step": 1,
            "initial_recipient_ids": [0],
        },
        "initial_eligible_rectangles": [
            {"x_min": -5, "x_max": 3, "y_min": -5, "y_max": 3}
        ],
    }


def canonical_warning_text() -> str:
    return (
        "Official warning warn-1. At step 1, hazard classification hazard-1 "
        "covers x=-4..1, y=-4..1. Refuge areas: refuge-1 "
        "x=4..5, y=4..5."
    )


def fixture_inputs() -> tuple[dict, list, list, list, list]:
    meta = {
        "run_id": "warning-v2-fixture",
        "protocol_version": "warning-v2-test-protocol",
        "log_schema_version": "2.0.0",
        "metric_version": DISASTER_METRIC_V2_VERSION,
        "status": "completed",
        "aborted": False,
        "expected_steps": 2,
        "expected_agents": 3,
        "config": {
            "simulation": {
                "duration": 2,
                "seed": 4101,
                "research_eligible": True,
            },
            "blocs": [{"name": "a", "model": "m", "num_agents": 3}],
            "scenario": scenario(),
        },
    }
    facts = {
        "warning_id": "warn-1",
        "issue_step": 1,
        "hazard_id": "hazard-1",
        "hazard_rectangles": [
            {"x_min": -4, "x_max": 1, "y_min": -4, "y_max": 1}
        ],
        "refuges": [{
            "refuge_id": "refuge-1",
            "rectangle": {"x_min": 4, "x_max": 5, "y_min": 4, "y_max": 5},
        }],
    }
    phase1_values = [
        {"step": 1, "agent_id": 0, "bloc": "a", "model": "m",
         "parsed": {"message": canonical_warning_text(), "reasoning": ""}},
        {"step": 1, "agent_id": 1, "bloc": "a", "model": "m",
         "parsed": {"message": "warn-1", "reasoning": ""}},
        {"step": 1, "agent_id": 2, "bloc": "a", "model": "m",
         "parsed": {"message": canonical_warning_text(), "reasoning": ""}},
        {"step": 2, "agent_id": 0, "bloc": "a", "model": "m",
         "parsed": {"message": canonical_warning_text(), "reasoning": ""}},
        {"step": 2, "agent_id": 1, "bloc": "a", "model": "m",
         "parsed": {"message": json.dumps(facts, sort_keys=True), "reasoning": ""}},
        {"step": 2, "agent_id": 2, "bloc": "a", "model": "m",
         "parsed": {"message": "", "reasoning": ""}},
    ]
    phase1 = [
        source(value, "phase1_raw.jsonl", line)
        for line, value in enumerate(phase1_values, 1)
    ]
    message_values = [
        {"step": 1, "sender_id": 0, "sender_bloc": "a", "sender_model": "m",
         "receiver_ids": [1], "message": canonical_warning_text(), "reasoning": ""},
        {"step": 1, "sender_id": 2, "sender_bloc": "a", "sender_model": "m",
         "receiver_ids": [1], "message": canonical_warning_text(), "reasoning": ""},
    ]
    messages = [
        source(value, "messages.jsonl", line)
        for line, value in enumerate(message_values, 1)
    ]
    warning_values = [
        {"event_id": "issued", "event_type": "warning_issued", "step": 1,
         "warning_id": "warn-1", "source_type": "official", "recipient_ids": [0],
         "payload": canonical_warning_text(), "facts": facts},
        {"event_id": "official-0", "event_type": "warning_exposure", "step": 1,
         "warning_id": "warn-1", "recipient_id": 0, "source_type": "official",
         "sender_id": None},
        {"event_id": "relay-0-1", "event_type": "warning_exposure", "step": 1,
         "warning_id": "warn-1", "recipient_id": 1,
         "source_type": "agent_relay", "sender_id": 0},
        {"event_id": "relay-2-1", "event_type": "warning_exposure", "step": 1,
         "warning_id": "warn-1", "recipient_id": 1,
         "source_type": "agent_relay", "sender_id": 2},
    ]
    warning_events = [
        source(value, "warning_events.jsonl", line)
        for line, value in enumerate(warning_values, 1)
    ]
    positions = []
    line = 1
    for agent_id in range(3):
        positions.append(source({
            "step": 0, "phase": "initial", "agent_id": agent_id,
            "bloc": "a", "model": "m", "position": [0, 0],
            "hazardous": True, "refuge_id": None,
            "shortest_refuge_distance": 3,
        }, "positions.jsonl", line))
        line += 1
    for step in (1, 2):
        for agent_id in range(3):
            positions.append(source({
                "step": step, "phase": "post_movement", "agent_id": agent_id,
                "bloc": "a", "model": "m", "position": [step, 0],
                "hazardous": True, "refuge_id": None,
                "shortest_refuge_distance": 3 - step,
            }, "positions.jsonl", line))
            line += 1
    return meta, positions, phase1, messages, warning_events


class DisasterMetricV2Tests(unittest.TestCase):
    def test_frozen_surface_statuses_match_conflict_mixed_and_unrecognized(self):
        contract = canonical_warning_contract(scenario())
        free = classify_warning_facts(canonical_warning_text(), contract)
        structured = classify_warning_facts(json.dumps({
            "warning_id": "warn-1",
            "issue_step": 1,
            "hazard_id": "hazard-1",
            "hazard_rectangles": [
                {"x_min": -4, "x_max": 1, "y_min": -4, "y_max": 1}
            ],
            "refuges": [{
                "refuge_id": "refuge-1",
                "rectangle": {"x_min": 4, "x_max": 5, "y_min": 4, "y_max": 5},
            }],
        }), contract)
        self.assertTrue(all(
            value == "match" for value in free["warning_specific"].values()
        ))
        self.assertTrue(all(
            value == "match" for value in structured["warning_specific"].values()
        ))
        self.assertEqual(free["shared_context"]["refuge:refuge-1"], "match")

        conflict_object = {
            "warning_id": "warn-2",
            "issue_step": 2,
            "hazard_id": "hazard-2",
            "hazard_rectangles": [
                {"x_min": -4, "x_max": 2, "y_min": -4, "y_max": 2}
            ],
            "refuges": [{
                "refuge_id": "refuge-1",
                "rectangle": {"x_min": 3, "x_max": 5, "y_min": 4, "y_max": 5},
            }],
        }
        conflict = classify_warning_facts(json.dumps(conflict_object), contract)
        self.assertTrue(all(
            value == "recognized_conflict"
            for value in conflict["warning_specific"].values()
        ))
        self.assertEqual(
            conflict["shared_context"]["refuge:refuge-1"],
            "recognized_conflict",
        )

        mixed = classify_warning_facts(
            canonical_warning_text() + " " + json.dumps(conflict_object),
            contract,
        )
        self.assertTrue(all(
            value == "mixed" for value in mixed["warning_specific"].values()
        ))
        self.assertEqual(mixed["shared_context"]["refuge:refuge-1"], "mixed")
        unrecognized = classify_warning_facts(
            "A flood notice described unsafe low ground.", contract
        )
        self.assertTrue(all(
            value == "unrecognized"
            for value in unrecognized["warning_specific"].values()
        ))

    def test_phase_aware_outputs_relays_movement_and_multiple_exposures(self):
        meta, positions, phase1, messages, warning_events = fixture_inputs()
        result = derive_disaster_metrics_v2(
            run_meta=meta,
            positions=positions,
            phase1=phase1,
            messages=messages,
            warning_events=warning_events,
        )
        agents = {row["agent_id"]: row for row in result["agents"]}
        outputs = {
            (row["step"], row["agent_id"]): row for row in result["outputs"]
        }

        # Official exposure precedes same-step Phase 1 and Phase 3, but the
        # project contract reserves "reuse" for a strictly later step.
        self.assertEqual(agents[0]["warning_reuse_step"], 2)
        self.assertEqual(agents[0]["warning_reuse_delay_steps"], 1)
        self.assertEqual(
            agents[0][
                "first_post_exposure_refuge_distance_decrease_step"
            ],
            1,
        )
        self.assertEqual(
            agents[0][
                "first_post_exposure_refuge_distance_decrease_before_reference"
            ]["file"],
            "positions.jsonl",
        )
        self.assertEqual(outputs[(1, 0)]["relay_status"], "generated_and_delivered")
        self.assertFalse(outputs[(1, 0)]["later_step_reuse_eligible"])
        self.assertTrue(outputs[(2, 0)]["later_step_reuse_eligible"])

        # Relay delivery follows same-step Phase 1, so agent 1 first becomes
        # output-eligible at step 2, while same-step Phase 3 remains eligible.
        self.assertEqual(
            outputs[(1, 1)]["source_classification"],
            "unattributed_exact_id_carrier",
        )
        self.assertFalse(outputs[(1, 1)]["post_exposure_eligible"])
        self.assertEqual(agents[1]["first_eligible_output_event_id"], outputs[(2, 1)]["event_id"])
        self.assertEqual(agents[1]["warning_reuse_step"], 2)
        self.assertEqual(agents[1]["warning_reuse_delay_steps"], 1)
        self.assertEqual(
            agents[1][
                "first_post_exposure_refuge_distance_decrease_step"
            ],
            1,
        )
        self.assertEqual(outputs[(2, 1)]["eligible_prior_exposure_count"], 2)
        self.assertEqual(
            outputs[(2, 1)]["eligible_prior_exposure_event_ids"],
            ["relay-0-1", "relay-2-1"],
        )
        self.assertFalse(outputs[(2, 1)]["causal_parent_inferred"])
        self.assertEqual(outputs[(2, 1)]["relay_status"], "generated_not_delivered")
        self.assertEqual(
            outputs[(2, 1)]["message_reference"]["file"],
            "phase1_raw.jsonl",
        )
        self.assertEqual(len(outputs[(2, 1)]["message_reference"]["line_sha256"]), 64)

        self.assertIsNone(agents[2]["first_warning_exposure_step"])
        self.assertEqual(
            outputs[(1, 2)]["source_classification"],
            "unattributed_exact_id_carrier",
        )
        self.assertFalse(outputs[(1, 2)]["post_exposure_eligible"])
        self.assertEqual(
            outputs[(1, 2)]["relay_status"], "generated_and_delivered"
        )
        self.assertEqual(result["summary"]["warning_exposed_agent_count"], 2)
        self.assertEqual(result["summary"]["warning_reused_agent_count"], 2)
        self.assertEqual(
            result["summary"][
                "same_step_post_exposure_exact_id_carrier_count"
            ],
            1,
        )
        self.assertEqual(
            result["summary"]["later_step_exact_id_reuse_output_count"],
            2,
        )
        self.assertEqual(
            result["summary"]["unattributed_exact_id_carrier_output_count"],
            2,
        )
        self.assertEqual(
            result["summary"]["all_exact_id_relay_exposure_edge_count"],
            2,
        )
        self.assertEqual(
            result["summary"][
                "post_exposure_exact_id_relay_exposure_edge_count"
            ],
            1,
        )
        self.assertEqual(
            result["canonical_warning_issue_reference"]["file"],
            "warning_events.jsonl",
        )
        self.assertFalse(result["fact_interpretation"]["llm_judge_used"])
        self.assertEqual(
            result["fact_interpretation"]["refuge_fact_scope"], "shared_context"
        )

    def test_first_eligible_output_is_unconditional_on_exact_id_reuse(self):
        meta, positions, phase1, messages, warning_events = fixture_inputs()
        phase1[0] = source({
            "step": 1, "agent_id": 0, "bloc": "a", "model": "m",
            "parsed": {"message": "", "reasoning": ""},
        }, "phase1_raw.jsonl", 1)
        phase1[3] = source({
            "step": 2, "agent_id": 0, "bloc": "a", "model": "m",
            "parsed": {"message": canonical_warning_text(), "reasoning": ""},
        }, "phase1_raw.jsonl", 4)
        messages = [row for row in messages if row.value["sender_id"] != 0]
        result = derive_disaster_metrics_v2(
            run_meta=meta,
            positions=positions,
            phase1=phase1,
            messages=messages,
            warning_events=warning_events,
        )
        agent = result["agents"][0]
        self.assertIn(":000001:", agent["first_eligible_output_event_id"])
        self.assertIn(":000002:", agent["first_exact_id_reuse_event_id"])
        self.assertEqual(agent["warning_reuse_delay_steps"], 1)
        first = next(
            row for row in result["outputs"]
            if row["event_id"] == agent["first_eligible_output_event_id"]
        )
        self.assertFalse(first["exact_warning_id_carrier"])

    def test_versioned_publication_is_outside_raw_and_collision_immutable(self):
        files = {
            filename: canonical_json_bytes({"file": filename})
            for filename in DERIVED_DATA_FILES
        }
        files["derived_manifest.json"] = _derived_manifest(files)
        prepared = PreparedAnalysis("publication-fixture", files)
        with workspace_tempdir() as root:
            raw = root / "output_publication-fixture"
            raw.mkdir()
            derived = root / "derived"
            leaf = write_prepared_analysis(prepared, raw, derived)
            self.assertEqual(
                leaf,
                derived / DISASTER_METRIC_V2_VERSION / "publication-fixture",
            )
            self.assertEqual(
                {path.name for path in leaf.iterdir()},
                {*DERIVED_DATA_FILES, "derived_manifest.json"},
            )
            with self.assertRaises(DerivedCollisionError):
                write_prepared_analysis(prepared, raw, derived)
            with self.assertRaises(InputValidationError):
                write_prepared_analysis(
                    PreparedAnalysis("inside-raw", files),
                    raw,
                    raw / "derived",
                )
            other_raw = root / "runs" / "output_other-run"
            other_raw.mkdir(parents=True)
            (other_raw / "run_meta.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(InputValidationError):
                write_prepared_analysis(
                    PreparedAnalysis("inside-other-raw", files),
                    raw,
                    other_raw / "derived",
                )

    def test_prepare_accepts_completed_log_schema_2_and_records_matrix_keys(self):
        meta, positions, phase1, messages, warning_events = fixture_inputs()
        meta.update({
            "config_hash": "c" * 64,
            "git_sha": "d" * 40,
            "git_dirty": False,
            "raw_manifest": {"algorithm": "sha256", "files": {}},
        })
        spec_path = REPO_ROOT / "docs" / "DISASTER_METRIC_V2_SPEC.md"
        spec_sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        meta["config"]["simulation"].update({
            "metric_version": DISASTER_METRIC_V2_VERSION,
            "metric_spec_sha256": spec_sha,
        })
        with workspace_tempdir() as root:
            run = root / "output_warning-v2-fixture"
            run.mkdir()
            (run / "run_meta.json").write_text(
                json.dumps(meta) + "\n", encoding="utf-8"
            )
            for filename, rows in (
                ("positions.jsonl", positions),
                ("phase1_raw.jsonl", phase1),
                ("messages.jsonl", messages),
                ("warning_events.jsonl", warning_events),
            ):
                (run / filename).write_text(
                    "".join(json.dumps(dict(row.value)) + "\n" for row in rows),
                    encoding="utf-8",
                )
            valid = types.SimpleNamespace(valid=True, errors=[])
            with mock.patch(
                "tools.disaster_metric_v2_core.validate_run", return_value=valid
            ):
                prepared = prepare_run_analysis(
                    run,
                    spec_sha,
                    require_declared_metric=True,
                )
            analysis_meta = json.loads(prepared.files["analysis_meta.json"])
            self.assertEqual(analysis_meta["source_log_schema_version"], "2.0.0")
            self.assertTrue(analysis_meta["source_declared_metric_matches_analysis"])
            self.assertTrue(
                analysis_meta["source_declared_metric_spec_matches_analysis"]
            )
            self.assertEqual(
                analysis_meta["source_declared_metric_spec_sha256"], spec_sha
            )
            self.assertEqual(
                set(analysis_meta["implementation_manifests"]),
                {
                    "engine/disaster.py",
                    "tools/disaster_metric_v2.py",
                    "tools/disaster_metric_v2_core.py",
                },
            )
            self.assertEqual(
                analysis_meta["matrix_keys"]["communication_mode"], "free_text"
            )
            self.assertEqual(analysis_meta["matrix_keys"]["seed"], 4101)
            self.assertEqual(
                set(analysis_meta["input_manifests"]),
                {
                    "run_meta.json",
                    "phase1_raw.jsonl",
                    "messages.jsonl",
                    "warning_events.jsonl",
                    "positions.jsonl",
                },
            )

            meta["config"]["simulation"]["metric_spec_sha256"] = "0" * 64
            (run / "run_meta.json").write_text(
                json.dumps(meta) + "\n", encoding="utf-8"
            )
            with mock.patch(
                "tools.disaster_metric_v2_core.validate_run", return_value=valid
            ), self.assertRaisesRegex(
                InputValidationError, "exact disaster-metric-v2.0.0"
            ):
                prepare_run_analysis(
                    run,
                    spec_sha,
                    require_declared_metric=True,
                )


if __name__ == "__main__":
    unittest.main()
