import hashlib
import json
import unittest

from engine.config import build_effective_config
from tools.build_disaster_120_matrix import (
    COMPOSITIONS,
    FORMAL_SEEDS,
    MODES,
    PILOT_SEEDS,
    build_formal_files,
    build_pilot_files,
)
from tools.build_disaster_matrix import build_config as build_60_config
from tools.disaster_120_matrix_runner import select_worker_rows
from tools.disaster_metric_core import derive_disaster_metrics


class Disaster120MatrixTests(unittest.TestCase):
    def _assert_matrix(self, files, seeds, expected_runs, expected_calls, eligible):
        manifest = json.loads(files["manifest.json"])
        rows = manifest["rows"]
        self.assertEqual(manifest["duration"], 120)
        self.assertEqual(manifest["research_eligible"], eligible)
        self.assertEqual(len(rows), expected_runs)
        self.assertEqual(manifest["planned_logical_llm_calls"], expected_calls)
        self.assertEqual(
            {(row["composition"], row["communication_mode"], row["seed"])
             for row in rows},
            {(composition, mode, seed)
             for composition in COMPOSITIONS for mode in MODES for seed in seeds},
        )
        self.assertEqual(len({row["run_id"] for row in rows}), expected_runs)
        for row in rows:
            payload = files[row["filename"]]
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            config = build_effective_config(json.loads(payload))
            self.assertEqual(config["simulation"]["duration"], 120)
            self.assertEqual(sum(bloc["num_agents"] for bloc in config["blocs"]), 24)
            expected = 2880 if row["communication_mode"] == "communication_none" else 5760
            self.assertEqual(row["expected_logical_llm_calls"], expected)

    def test_pilot_is_full_4_by_3_nonresearch_matrix(self):
        self._assert_matrix(
            build_pilot_files(), PILOT_SEEDS, 12, 57600, False
        )

    def test_formal_is_full_4_by_3_by_5_matrix(self):
        self._assert_matrix(
            build_formal_files(), FORMAL_SEEDS, 60, 288000, True
        )

    def test_world_and_intervention_schedule_are_unchanged_from_60_step(self):
        files = build_formal_files()
        manifest = json.loads(files["manifest.json"])
        for composition in COMPOSITIONS:
            for mode in MODES:
                row = next(
                    row for row in manifest["rows"]
                    if row["composition"] == composition
                    and row["communication_mode"] == mode
                )
                new_config = json.loads(files[row["filename"]])
                old_config = build_60_config(composition, mode, row["seed"], row["worker_slot"])
                self.assertEqual(new_config["scenario"], old_config["scenario"])
                self.assertEqual(new_config["agents"], old_config["agents"])
                self.assertEqual(new_config["llm_defaults"], old_config["llm_defaults"])
                self.assertEqual(new_config["blocs"], old_config["blocs"])

    def test_pilot_worker_slices_are_exact_halves(self):
        manifest = json.loads(build_pilot_files()["manifest.json"])
        combined = []
        for slot in ("a", "b"):
            rows = select_worker_rows(
                manifest, slot=slot, selected_seeds=PILOT_SEEDS
            )
            self.assertEqual(len(rows), 6)
            self.assertEqual(
                sum(row["expected_logical_llm_calls"] for row in rows), 28800
            )
            combined.extend(rows)
        self.assertEqual(len({row["run_id"] for row in combined}), 12)

    def test_formal_seed_envelopes_are_exact(self):
        manifest = json.loads(build_formal_files()["manifest.json"])
        for selected_seeds, expected_runs, expected_calls in (
            ((2201, 2202, 2203), 36, 172800),
            ((2204, 2205), 24, 115200),
        ):
            combined = []
            for slot in ("a", "b"):
                rows = select_worker_rows(
                    manifest, slot=slot, selected_seeds=selected_seeds
                )
                combined.extend(rows)
            self.assertEqual(len(combined), expected_runs)
            self.assertEqual(
                sum(row["expected_logical_llm_calls"] for row in combined),
                expected_calls,
            )

    def test_metric_censoring_uses_120_step_horizon(self):
        meta = {
            "run_id": "metric-120-fixture",
            "log_schema_version": "1.2.0",
            "expected_steps": 120,
            "expected_agents": 1,
            "config": {"scenario": {
                "communication_mode": "free_text",
                "hazard": {"stages": [{"start_step": 10}]},
                "official_warning": {
                    "warning_id": "warning-inundation-1", "issue_step": 10
                },
            }},
        }
        positions = [{
            "step": 0, "phase": "initial", "agent_id": 0,
            "bloc": "qwen", "model": "m", "refuge_id": None,
            "hazardous": False, "shortest_refuge_distance": 10,
        }]
        positions.extend({
            "step": step, "phase": "post_movement", "agent_id": 0,
            "bloc": "qwen", "model": "m", "refuge_id": None,
            "hazardous": step >= 10, "shortest_refuge_distance": 10,
        } for step in range(1, 121))
        result = derive_disaster_metrics(
            run_meta=meta,
            positions=positions,
            phase1=[],
            warning_events=[{
                "event_type": "warning_exposure", "recipient_id": 0,
                "step": 10, "source_type": "official",
            }],
        )
        row = result["agents"][0]
        self.assertTrue(row["warning_reuse_right_censored"])
        self.assertEqual(row["warning_reuse_censor_step"], 120)
        self.assertEqual(row["dangerous_area_residence_steps"], 111)


if __name__ == "__main__":
    unittest.main()
