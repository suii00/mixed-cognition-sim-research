import copy
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.disaster import parse_disaster_scenario
from engine.world import World
from tools import build_disaster_behavior_pilot as followup


class DisasterBehaviorPilotConfigTests(unittest.TestCase):
    def test_frozen_complete_six_run_envelope_and_research_boundary(self):
        manifest = followup.load_verified_manifest()
        self.assertEqual(manifest["planned_runs"], 6)
        self.assertEqual(manifest["planned_http_attempts"], 2880)
        self.assertEqual(manifest["planned_logical_llm_calls"], 2880)
        self.assertEqual(manifest["schema_probe_http_attempts"], 9)
        self.assertEqual(manifest["total_http_attempt_cap"], 2889)
        self.assertEqual(manifest["wall_time_limit_seconds"], 3600)
        self.assertNotIn("supersedes_unstarted_batch", manifest)
        self.assertNotIn("operational_attempt_version", manifest)
        self.assertEqual(manifest["planned_gpu_count"], 4)
        self.assertEqual(manifest["maximum_gpu_count"], 4)
        self.assertFalse(manifest["research_eligible"])
        self.assertFalse(manifest["formal_eligible"])
        self.assertEqual(
            {(r["seed"], r["model_name"]) for r in manifest["rows"]},
            {(s, m) for s in (6201, 6202) for m in ("qwen", "llama", "gemma")},
        )
        self.assertEqual(len({r["run_id"] for r in manifest["rows"]}), 6)
        self.assertTrue(all(r["expected_http_attempts"] == 480 for r in manifest["rows"]))
        for row in manifest["rows"]:
            payload = (followup.OUTPUT_DIR / row["filename"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            config = json.loads(payload)
            self.assertFalse(config["simulation"]["research_eligible"])
            self.assertEqual(config["simulation"]["response_failure_policy"], "abort_run")
            self.assertEqual(config["simulation"]["response_contract_version"], "phase-response-v3.0.0")
            self.assertEqual(config["simulation"]["prompt_contract_version"], "bounded-prompts-v3.0.0")
            self.assertEqual(config["llm_defaults"]["max_tokens"], 1024)

    def test_previous_world_seeds_cannot_enter_the_new_frozen_batch(self):
        for previous_seed in (6101, 6102):
            with self.subTest(seed=previous_seed), self.assertRaises(ValueError):
                followup.build_config("qwen", previous_seed)
        manifest = followup.load_verified_manifest()
        self.assertEqual(manifest["seeds"], [6201, 6202])
        self.assertEqual(manifest["batch_id"], "disaster-llm-behavior-pilot-v1-20260909T120500Z")
        self.assertEqual(manifest["protocol_version"], "disaster-llm-behavior-pilot-v1.0.0")

    def test_same_seed_models_have_identical_nonmodel_conditions_and_initial_world(self):
        for seed in (6201, 6202):
            conditions = []
            positions = []
            for model in ("qwen", "llama", "gemma"):
                config = followup.build_config(model, seed)
                scenario = parse_disaster_scenario(
                    config["scenario"], half_space_size=25, duration=60, total_agents=4,
                )
                world = World(25, [], disaster=scenario)
                positions.append(world.generate_initial_positions(4, random.Random(seed)))
                self.assertFalse(world.is_hazardous(9, 0, -10))
                self.assertTrue(world.is_hazardous(10, 0, -10))
                self.assertFalse(world.is_hazardous(29, 0, -1))
                self.assertTrue(world.is_hazardous(30, 0, -1))
                self.assertEqual(scenario.official_warning.initial_recipient_ids, (1,))
                self.assertEqual(scenario.official_warning.issue_step, 10)
                self.assertEqual(scenario.communication_mode, "free_text")
                self.assertEqual(config["blocs"][0]["num_agents"], 4)
                comparable = copy.deepcopy(config)
                del comparable["blocs"]
                del comparable["simulation"]["run_id"]
                del comparable["simulation"]["run_name"]
                conditions.append(comparable)
            self.assertEqual(conditions[0], conditions[1])
            self.assertEqual(conditions[1], conditions[2])
            self.assertEqual(positions[0], positions[1])
            self.assertEqual(positions[1], positions[2])

    def test_models_are_exact_existing_snapshots_without_generation_override(self):
        revisions = {
            "qwen": "a09a35458c702b33eeacc393d103063234e8bc28",
            "llama": "0e9e39f249a16976918f6564b8830bc894c89659",
            "gemma": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
        }
        for model, revision in revisions.items():
            bloc = followup.build_config(model, 6201)["blocs"][0]
            self.assertEqual(bloc["model_digest"], revision)
            self.assertEqual(bloc["tokenizer_revision"], revision)
            self.assertEqual(bloc["llm_overrides"], {})
            self.assertNotIn("base_url", bloc)

    def test_writer_refuses_existing_config_and_run_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            runs = root / "runs"
            followup.write_files(output, runs)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(FileExistsError):
                followup.write_files(output, runs)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            run_id = followup.build_config("qwen", 6201)["simulation"]["run_id"]
            (runs / f"output_{run_id}").mkdir(parents=True)
            absent = root / "other-config"
            with self.assertRaises(FileExistsError):
                followup.write_files(absent, runs)
            self.assertFalse(absent.exists())

    def test_check_rejects_tampering_and_extra_members_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "config"
            followup.write_files(output, Path(temporary) / "runs")
            manifest = followup.load_verified_manifest(output)
            altered = output / manifest["rows"][0]["filename"]
            altered.write_bytes(altered.read_bytes() + b" ")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(ValueError):
                followup.load_verified_manifest(output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            (output / "unexpected.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file set"):
                followup.load_verified_manifest(output)

    def test_unsafe_input_or_metric_mismatch_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            unsafe = followup.base_config("qwen_only", "free_text", 6201, "a")
            unsafe["blocs"][0]["hostname"] = "runtime-only"
            with mock.patch.object(followup, "base_config", return_value=unsafe):
                with self.assertRaises(ValueError):
                    followup.write_files(output, root / "runs")
            self.assertFalse(output.exists())
            bad_spec = root / "bad-spec.md"
            bad_spec.write_text("different specification", encoding="utf-8")
            with mock.patch.object(followup, "METRIC_SPEC_PATH", bad_spec):
                with self.assertRaisesRegex(ValueError, "specification"):
                    followup.write_files(output, root / "runs")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
