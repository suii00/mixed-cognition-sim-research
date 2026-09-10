import copy
import hashlib
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

from engine.disaster import parse_disaster_scenario
from engine.world import World
from tools import build_warning_retention_study as study


class WarningRetentionStudyConfigTests(unittest.TestCase):
    def test_fixed_six_run_budget_order_and_provenance(self):
        files = study.build_files()
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["planned_runs"], 6)
        self.assertEqual(manifest["planned_logical_llm_calls"], 17280)
        self.assertEqual(manifest["planned_http_attempts"], 17280)
        self.assertEqual(manifest["schema_probe_http_attempts"], 9)
        self.assertEqual(manifest["total_http_attempt_cap"], 17289)
        self.assertEqual(manifest["wall_time_limit_seconds"], 10800)
        self.assertEqual(manifest["maximum_gpu_count"], 4)
        self.assertEqual(manifest["planned_gpu_count"], 4)
        self.assertEqual(manifest["batch_id_timestamp"], "20260910T080000Z")
        self.assertNotIn("frozen_at_utc", manifest)
        self.assertEqual(manifest["freeze_policy"], "clean-source-commit-before-model-requests-v1.0.0")
        self.assertFalse(manifest["research_eligible"])
        self.assertFalse(manifest["formal_eligible"])
        self.assertEqual([(r["condition"], r["seed"]) for r in manifest["rows"]],
                         [("recent", 7301), ("retained", 7301), ("retained", 7302),
                          ("recent", 7302), ("recent", 7303), ("retained", 7303)])
        self.assertEqual(len({r["run_id"] for r in manifest["rows"]}), 6)
        self.assertEqual([r["ordinal"] for r in manifest["rows"]], list(range(1, 7)))
        for row in manifest["rows"]:
            self.assertEqual(row["expected_http_attempts"], 2880)
            self.assertEqual(row["duration"], 60)
            self.assertEqual(row["layout"], "inset")
            payload = files[row["filename"]]
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            config = json.loads(payload)
            self.assertEqual(config["agents"]["message_selection_policy"], row["message_selection_policy"])
            self.assertEqual(config["agents"]["message_context_size"], 5)
            self.assertEqual(config["simulation"]["input_observability_version"], "message-presentation-v1.0.0")
            self.assertEqual(config["simulation"]["response_failure_policy"], "abort_run")

    def test_pairs_change_only_input_selection_and_run_identity(self):
        for seed in study.SEEDS:
            conditions = []
            for condition in study.CONDITIONS:
                config = study.build_config(condition, seed)
                for field in ("run_id", "run_name"):
                    del config["simulation"][field]
                del config["agents"]["message_selection_policy"]
                conditions.append(config)
            self.assertEqual(*conditions)

    def test_shared_world_and_balanced_assignment(self):
        for seed in study.SEEDS:
            initial = []
            for condition in study.CONDITIONS:
                config = study.build_config(condition, seed)
                scenario = parse_disaster_scenario(config["scenario"], half_space_size=25,
                                                    duration=60, total_agents=24)
                self.assertEqual(len(scenario.eligible_initial_cells()), 2121)
                self.assertEqual(config["scenario"]["refuges"], list(study.REFUGES_BY_LAYOUT["inset"]))
                self.assertEqual(config["scenario"]["official_warning"]["initial_recipient_ids"], [1, 5, 9, 13, 17, 21])
                world = World(25, [], disaster=scenario)
                initial.append(world.generate_initial_positions(24, random.Random(seed)))
                self.assertEqual([b["name"] for b in config["blocs"]], ["qwen", "llama", "gemma"])
                for index, bloc in enumerate(config["blocs"]):
                    self.assertEqual(bloc["num_agents"], 8)
                    self.assertEqual(bloc["model_digest"], bloc["tokenizer_revision"])
                    self.assertEqual(bloc["max_model_len"], 4096)
                    self.assertEqual(bloc["endpoint_id"], "refuge-" + bloc["name"])
                    self.assertNotIn("base_url", bloc)
                    self.assertEqual(sum(index * 8 <= i < (index + 1) * 8 for i in study.OFFICIAL_RECIPIENT_IDS), 2)
            self.assertEqual(*initial)

    def test_rejects_undeclared_condition_seed_and_mutation_leaks(self):
        for condition, seed in (("other", 7301), ("recent", 6301), ("recent", "7301"), ("retained", True)):
            with self.assertRaises(ValueError):
                study.build_config(condition, seed)
        original = study.build_config("recent", 7301)
        changed = study.build_config("recent", 7301)
        changed["scenario"]["refuges"][0]["rectangle"]["x_min"] = -20
        self.assertEqual(study.build_config("recent", 7301), original)

    def test_writer_rejects_config_and_run_collisions_without_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, runs = root / "config", root / "runs"
            study.write_files(output, runs)
            study.load_verified_manifest(output)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(FileExistsError):
                study.write_files(output, runs)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            run_id = study.build_config("recent", 7301)["simulation"]["run_id"]
            (runs / ("output_" + run_id)).mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                study.write_files(root / "absent", runs)
            self.assertFalse((root / "absent").exists())

    def test_read_only_verification_rejects_tampering_and_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            study.write_files(output, root / "runs")
            row = study.load_verified_manifest(output)["rows"][0]
            altered = output / row["filename"]
            altered.write_bytes(altered.read_bytes() + b" ")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(ValueError):
                study.load_verified_manifest(output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            (output / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file set"):
                study.load_verified_manifest(output)

    def test_unsafe_input_and_metric_drift_reject_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            unsafe = copy.deepcopy(study.base_config("mixed", "free_text", 7301, "a"))
            unsafe["blocs"][0]["hostname"] = "runtime-only"
            with mock.patch.object(study, "base_config", return_value=unsafe):
                with self.assertRaises(ValueError):
                    study.write_files(output, root / "runs")
            self.assertFalse(output.exists())
            bad_spec = root / "bad-spec.md"
            bad_spec.write_text("different", encoding="utf-8")
            with mock.patch.object(study, "RETENTION_METRIC_SPEC_PATH", bad_spec):
                with self.assertRaisesRegex(ValueError, "specification"):
                    study.write_files(output, root / "runs")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
