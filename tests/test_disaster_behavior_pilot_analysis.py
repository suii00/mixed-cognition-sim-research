"""Offline integration tests: synthetic transports, no model/GPU requests."""

import base64
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.parallel_transport import TransportOutcome
from engine.sim import Simulation
from tools.analyze_disaster_behavior_pilot import (
    METRIC_VERSION, REPO_ROOT, SPEC_PATH, DerivedCollisionError,
    InputValidationError, analyze, select_example, summarize_completed_run,
)
from tools.build_disaster_behavior_pilot import OUTPUT_DIR, build_config, load_verified_manifest
from tools.disaster_metric_v2_core import SourceRecord, read_jsonl_source_records
from tools.validate_run import validate_run


def transport(request, telemetry):
    telemetry("http_attempt", 1)
    model = request.model.casefold()
    if request.phase == "phase1":
        parsed = {"message": "warning-inundation-1" if request.agent_id == 1 and request.step in (10, 11) else "", "reasoning": ""}
    else:
        direction = None
        if "qwen" in model and request.step == 9 and request.agent_id == 2:
            direction = "left"
        if "llama" in model and request.step == 10 and request.agent_id == 1:
            direction = "up"
        if "gemma" in model and request.step == 11 and request.agent_id == 0:
            direction = "right"
        parsed = {"action": "move" if direction else "stay", "direction": direction, "memory": "", "reasoning": ""}
    raw = json.dumps(parsed, sort_keys=True)
    envelope = {
        "id": f"fixture-{request.request_id}", "model": request.model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": raw}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return TransportOutcome(parsed=parsed, raw_output=raw, attempts=({
        "generation_attempt": 1, "http_attempt": 1, "http_status": 200,
        "http_response_body_base64": base64.b64encode(body).decode("ascii"),
        "http_response_bytes": len(body), "http_response_sha256": hashlib.sha256(body).hexdigest(),
        "envelope": envelope, "raw_output": raw, "finish_reason": "stop", "usage": envelope["usage"],
        "transport_status": "ok", "parse_status": "valid", "schema_status": "not_checked",
        "failure_kind": None, "error_type": None,
    },))


class DisasterBehaviorPilotAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = REPO_ROOT / "derived"
        base.mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="test-disaster-pilot-analysis-", dir=base)
        cls.root = Path(cls.temp.name)
        cls.runs = cls.root / "runs"
        cls.runs.mkdir()
        cls.manifest = load_verified_manifest()
        cls.spec_sha = hashlib.sha256((REPO_ROOT / SPEC_PATH).read_bytes()).hexdigest()
        cls.complete = []
        with mock.patch("engine.provenance.collect_git_info", return_value={
            "git_sha": "a" * 40, "git_dirty": False, "git_probe_status": "available", "git_probe_errors": [],
        }), mock.patch("engine.provenance.collect_gpu_info", return_value={
            "status": "unavailable", "error": "test_disabled", "driver_version": None,
            "cuda_version": None, "devices": [],
        }), mock.patch("builtins.print"):
            for plan in cls.manifest["rows"]:
                simulation = Simulation(build_config(plan["model_name"], plan["seed"]), output_root=cls.runs, repo_root=REPO_ROOT, transport=transport)
                simulation.run()
                run = Path(simulation.output_dir)
                report = validate_run(run, strict=True)
                if report.errors:
                    raise AssertionError(report.errors)
                cls.complete.append({
                    **plan, "provenance": {},
                    "records": {name: read_jsonl_source_records(run / name) for name in (
                        "positions.jsonl", "phase1_raw.jsonl", "messages.jsonl", "warning_events.jsonl", "memory_reasoning.jsonl", "llm_attempts.jsonl",
                    )},
                })

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def analyze_to(self, name, runs=None, **kwargs):
        output = self.root / name / f"{METRIC_VERSION}_20260909T100000Z"
        result = analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=runs or self.runs, output_dir=output, expected_metric_spec_sha256=self.spec_sha, **kwargs)
        return result, json.loads((result / "summary.json").read_text(encoding="utf-8"))

    def test_full_real_validator_pipeline_and_original_raw_immutability(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.runs.rglob("*") if p.is_file()}
        output, summary = self.analyze_to("full")
        self.assertEqual(summary["planned_run_count"], 6)
        self.assertEqual(summary["comparison_eligible_run_count"], 6)
        example = summary["example"]
        self.assertEqual((example["seed"], example["step"], example["agent_id"]), (6201, 10, 1))
        self.assertEqual(len(example["models"]), 3)
        self.assertEqual(example["first_output_disagreement_at_or_before_selected_phase3"], {"step": 9, "agent_id": 2, "phase": "phase3"})
        for run in summary["runs"]:
            all_steps = run["behavior"]["action_windows"]["all_steps"]
            post10 = run["behavior"]["action_windows"]["steps_ge_10"]
            self.assertEqual(all_steps["choice_count"], 240)
            self.assertEqual(post10["choice_count"], 204)
            self.assertEqual(all_steps["action_counts"], {"stay": 239, "move": 1})
            if run["model_name"] == "qwen":
                self.assertEqual(post10["action_counts"]["move"], 0)
            warning = run["behavior"]["warning_metric_v2"]
            receiver = next(a for a in warning["agents"] if a["agent_id"] == 1)
            self.assertEqual(receiver["first_warning_exposure_step"], 10)
            self.assertEqual(receiver["warning_reuse_step"], 11)
        for model in example["models"]:
            self.assertEqual(len(model["phase3_attempts"]), 1)
            for name in ("phase1", "phase3_action", "position_before", "position_after"):
                observation = model[name]
                ref = observation["reference"]
                raw = (self.runs / f"output_{ref['run_id']}" / ref["file"]).read_bytes().splitlines(keepends=True)[ref["line_number"] - 1]
                self.assertEqual(hashlib.sha256(raw).hexdigest(), ref["line_sha256"])
                self.assertEqual(json.loads(raw), observation["raw_row"])
        derived = json.loads((output / "derived_manifest.json").read_text(encoding="utf-8"))
        for name, manifest in derived["files"].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), manifest["sha256"])
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_missing_and_aborted_conditions_never_enter_completed_comparison(self):
        partial = self.root / "partial-inputs"
        partial.mkdir()
        plan = self.manifest["rows"][0]
        run = partial / f"output_{plan['run_id']}"
        run.mkdir()
        original_meta = self.runs / run.name / "run_meta.json"
        meta = json.loads(original_meta.read_text(encoding="utf-8"))
        meta.update({"status": "aborted", "aborted": True, "observed_steps": 3})
        (run / "run_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        _, summary = self.analyze_to("partial", partial)
        self.assertEqual(len(summary["runs"]), 6)
        self.assertEqual(summary["comparison_eligible_run_count"], 0)
        self.assertEqual(summary["runs"][0]["analysis_status"], "aborted")
        self.assertFalse(summary["runs"][0]["strict_validation"]["valid"])
        self.assertEqual(summary["runs"][1]["analysis_status"], "not_started_or_unavailable")
        self.assertEqual(summary["example_absence_reason"], "no_complete_three_model_seed")
        self.assertTrue(all(r["behavior"] is None for r in summary["runs"]))

    def test_earliest_selection_is_order_independent_and_null_preserved(self):
        example, _ = select_example(list(reversed(self.complete)))
        self.assertEqual((example["seed"], example["step"], example["agent_id"]), (6201, 10, 1))
        no_divergence = copy.deepcopy(self.complete)
        for run in no_divergence:
            run["records"]["memory_reasoning.jsonl"] = [SourceRecord({**r.value, "action": "stay", "direction": None}, r.reference) for r in run["records"]["memory_reasoning.jsonl"]]
        example, checks = select_example(no_divergence)
        self.assertIsNone(example)
        self.assertTrue(all(c["eligible"] for c in checks))
        example, _ = select_example([r for r in self.complete if r["seed"] == 6202])
        self.assertEqual(example["seed"], 6202)

    def test_initial_mismatch_and_incomplete_choice_coverage_fail_closed(self):
        wrong = copy.deepcopy(self.complete)
        row = wrong[0]["records"]["positions.jsonl"][0]
        wrong[0]["records"]["positions.jsonl"][0] = SourceRecord({**row.value, "position": [0, 0]}, row.reference)
        with self.assertRaisesRegex(InputValidationError, "initial positions"):
            select_example(wrong)
        plan = self.complete[0]
        records = dict(plan["records"])
        records["memory_reasoning.jsonl"] = records["memory_reasoning.jsonl"][:-1]
        meta = json.loads((self.runs / f"output_{plan['run_id']}" / "run_meta.json").read_text(encoding="utf-8"))
        with self.assertRaisesRegex(InputValidationError, "coverage"):
            summarize_completed_run(meta, records)

    def test_geometry_arrival_and_hazard_windows_have_explicit_units(self):
        plan = self.complete[0]
        records = copy.deepcopy(plan["records"])
        positions = []
        for r in records["positions.jsonl"]:
            if r.value["agent_id"] != 0:
                positions.append(r)
                continue
            arrived = r.value["step"] == 60
            positions.append(SourceRecord({**r.value,
                "position": [18, 18] if arrived else [0, 0],
                "shortest_refuge_distance": 0 if arrived else 36,
                "hazardous": r.value["step"] >= 30 and not arrived,
                "refuge_id": "refuge-east" if arrived else None,
            }, r.reference))
        records["positions.jsonl"] = positions
        meta = json.loads((self.runs / f"output_{plan['run_id']}" / "run_meta.json").read_text(encoding="utf-8"))
        agent = summarize_completed_run(meta, records)["agents"][0]
        self.assertEqual(agent["initial_nearest_refuge_distance"], 36)
        self.assertEqual(agent["final_nearest_refuge_distance"], 0)
        self.assertEqual(agent["minimum_nearest_refuge_distance_over_observed_positions"], 0)
        self.assertEqual(agent["first_refuge_arrival_step"], 60)
        self.assertFalse(agent["refuge_arrival_right_censored"])
        self.assertEqual(agent["hazard_residence_steps"], {"all_steps": 30, "steps_ge_10": 30})

    def test_collision_and_unsafe_input_rejected_before_output_writes(self):
        output = self.root / "collision" / f"{METRIC_VERSION}_20260909T100000Z"
        output.mkdir(parents=True)
        sentinel = output / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(DerivedCollisionError):
            analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=self.runs, output_dir=output, expected_metric_spec_sha256=self.spec_sha)
        self.assertEqual(list(output.iterdir()), [sentinel])
        unsafe_runs = self.root / "unsafe-inputs"
        unsafe = unsafe_runs / f"output_{self.manifest['rows'][0]['run_id']}"
        unsafe.mkdir(parents=True)
        (unsafe / "run_meta.json").write_text(json.dumps({"note": "private " + "/home/" + "fixture-person/host"}), encoding="utf-8")
        unsafe_output = self.root / "must-not-exist" / f"{METRIC_VERSION}_20260909T100000Z"
        with self.assertRaisesRegex(InputValidationError, "publication boundary"):
            analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=unsafe_runs, output_dir=unsafe_output, expected_metric_spec_sha256=self.spec_sha)
        self.assertFalse(unsafe_output.parent.exists())

    def test_raw_output_nesting_and_spec_change_rejected_before_creation(self):
        run = self.runs / f"output_{self.manifest['rows'][0]['run_id']}"
        output = run / f"{METRIC_VERSION}_20260909T100000Z"
        with self.assertRaisesRegex(InputValidationError, "raw or immutable"):
            analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=self.runs, output_dir=output, expected_metric_spec_sha256=self.spec_sha)
        output = self.root / "wrong-spec" / f"{METRIC_VERSION}_20260909T100000Z"
        with self.assertRaisesRegex(InputValidationError, "digest mismatch"):
            analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=self.runs, output_dir=output, expected_metric_spec_sha256="0" * 64)
        self.assertFalse(output.parent.exists())

    def test_dirty_and_mismatched_source_states_cannot_support_comparison(self):
        copied = self.root / "source-state-fixtures"
        shutil.copytree(self.runs, copied)
        first = copied / f"output_{self.manifest['rows'][0]['run_id']}" / "run_meta.json"
        meta = json.loads(first.read_text(encoding="utf-8"))
        meta["git_dirty"] = True
        first.write_text(json.dumps(meta), encoding="utf-8")
        _, summary = self.analyze_to("dirty-source", copied)
        self.assertEqual(summary["comparison_eligible_run_count"], 5)
        self.assertEqual(summary["runs"][0]["analysis_status"], "completed_but_ineligible")
        self.assertEqual(summary["example"]["seed"], 6202)
        meta.update({"git_dirty": False, "git_sha": "b" * 40})
        first.write_text(json.dumps(meta), encoding="utf-8")
        output = self.root / "mixed-source" / f"{METRIC_VERSION}_20260909T100000Z"
        with self.assertRaisesRegex(InputValidationError, "different source commits"):
            analyze(manifest_path=OUTPUT_DIR / "manifest.json", runs_root=copied, output_dir=output, expected_metric_spec_sha256=self.spec_sha)
        self.assertFalse(output.parent.exists())


if __name__ == "__main__":
    unittest.main()
