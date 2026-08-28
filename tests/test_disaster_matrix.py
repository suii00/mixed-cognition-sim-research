import hashlib
import json
import unittest

from engine.config import build_effective_config
from tools.build_disaster_matrix import (
    COMPOSITIONS,
    MODES,
    SEEDS,
    build_files,
)
from tools.build_disaster_smoke import build_files as build_smoke_files
from tools.disaster_matrix_runner import select_worker_rows


class DisasterMatrixTests(unittest.TestCase):
    def test_frozen_matrix_has_exact_cells_hashes_and_call_envelope(self):
        files = build_files()
        manifest = json.loads(files["manifest.json"])
        rows = manifest["rows"]
        self.assertEqual(len(rows), 60)
        self.assertEqual(manifest["planned_logical_llm_calls"], 144000)
        self.assertEqual(
            {(row["composition"], row["communication_mode"], row["seed"])
             for row in rows},
            {(composition, mode, seed)
             for composition in COMPOSITIONS for mode in MODES for seed in SEEDS},
        )
        self.assertEqual(len({row["run_id"] for row in rows}), 60)
        for row in rows:
            payload = files[row["filename"]]
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            config = build_effective_config(json.loads(payload))
            self.assertEqual(sum(bloc["num_agents"] for bloc in config["blocs"]), 24)
            expected = 1440 if row["communication_mode"] == "communication_none" else 2880
            self.assertEqual(row["expected_logical_llm_calls"], expected)

    def test_seed_paired_scenarios_differ_only_by_communication_mode(self):
        files = build_files()
        manifest = json.loads(files["manifest.json"])
        rows = manifest["rows"]
        for seed in SEEDS:
            for composition in COMPOSITIONS:
                scenarios = []
                for mode in MODES:
                    row = next(
                        row for row in rows
                        if row["seed"] == seed
                        and row["composition"] == composition
                        and row["communication_mode"] == mode
                    )
                    scenario = json.loads(files[row["filename"]])["scenario"]
                    scenario.pop("communication_mode")
                    scenarios.append(scenario)
                self.assertEqual(scenarios[0], scenarios[1])
                self.assertEqual(scenarios[0], scenarios[2])

    def test_engineering_smoke_is_three_modes_and_7200_calls(self):
        files = build_smoke_files()
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["planned_runs"], 3)
        self.assertEqual(manifest["planned_logical_llm_calls"], 7200)
        self.assertFalse(manifest["research_eligible"])
        self.assertEqual(
            {row["communication_mode"] for row in manifest["rows"]},
            set(MODES),
        )

    def test_first_three_seed_worker_slices_are_exact_halves(self):
        manifest = json.loads(build_files()["manifest.json"])
        selected_seeds = (2101, 2102, 2103)
        combined = []
        for slot in ("a", "b"):
            rows = select_worker_rows(
                manifest,
                slot=slot,
                selected_seeds=selected_seeds,
            )
            self.assertEqual(len(rows), 18)
            self.assertEqual(
                sum(row["expected_logical_llm_calls"] for row in rows),
                43200,
            )
            combined.extend(rows)
        self.assertEqual(len({row["run_id"] for row in combined}), 36)
        self.assertEqual(
            sum(row["expected_logical_llm_calls"] for row in combined),
            86400,
        )

    def test_last_two_seed_worker_slices_complete_the_frozen_matrix(self):
        manifest = json.loads(build_files()["manifest.json"])
        selected_seeds = (2104, 2105)
        combined = []
        for slot in ("a", "b"):
            rows = select_worker_rows(
                manifest,
                slot=slot,
                selected_seeds=selected_seeds,
            )
            self.assertEqual(len(rows), 12)
            self.assertEqual(
                sum(row["expected_logical_llm_calls"] for row in rows),
                28800,
            )
            combined.extend(rows)
        self.assertEqual(len({row["run_id"] for row in combined}), 24)
        self.assertEqual(
            sum(row["expected_logical_llm_calls"] for row in combined),
            57600,
        )
        self.assertEqual(
            {row["seed"] for row in combined},
            set(selected_seeds),
        )


if __name__ == "__main__":
    unittest.main()
