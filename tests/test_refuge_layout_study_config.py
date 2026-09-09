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
from tools import build_refuge_layout_study as study


def scenario_for(config):
    return parse_disaster_scenario(
        config["scenario"], half_space_size=25,
        duration=config["simulation"]["duration"], total_agents=24,
    )


class RefugeLayoutStudyConfigTests(unittest.TestCase):
    def test_complete_fixed_four_run_envelope_and_no_false_freeze_time(self):
        files = study.build_files()
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["planned_runs"], 4)
        self.assertEqual(manifest["planned_http_attempts"], 17280)
        self.assertEqual(manifest["planned_logical_llm_calls"], 17280)
        self.assertEqual(manifest["schema_probe_http_attempts"], 9)
        self.assertEqual(manifest["total_http_attempt_cap"], 17289)
        self.assertEqual(manifest["wall_time_limit_seconds"], 10800)
        self.assertEqual(manifest["planned_gpu_count"], 4)
        self.assertEqual(manifest["maximum_gpu_count"], 4)
        self.assertEqual(manifest["agent_count"], 24)
        self.assertEqual(manifest["agents_per_model"], 8)
        self.assertEqual(manifest["max_concurrency"], 24)
        self.assertFalse(manifest["research_eligible"])
        self.assertFalse(manifest["formal_eligible"])
        self.assertNotIn("frozen_at_utc", manifest)
        self.assertEqual(manifest["batch_id_timestamp"], "20260909T162300Z")
        self.assertEqual(manifest["freeze_policy"], "clean-source-commit-before-model-requests-v1.0.0")
        self.assertEqual(manifest["batch_id"], "refuge-layout-study-v1-20260909T162300Z")
        self.assertEqual(manifest["protocol_version"], "refuge-layout-study-v1.0.0")
        self.assertEqual(
            [(r["composition"], r["layout"], r["duration"], r["seed"]) for r in manifest["rows"]],
            [("mixed", "edge", 60, 6301), ("mixed", "inset", 60, 6301),
             ("mixed", "inset", 120, 6301), ("mixed", "edge", 120, 6301)],
        )
        self.assertEqual([r["ordinal"] for r in manifest["rows"]], [1, 2, 3, 4])
        self.assertEqual(len({r["run_id"] for r in manifest["rows"]}), 4)
        self.assertEqual([r["expected_http_attempts"] for r in manifest["rows"]], [2880, 2880, 5760, 5760])
        for row in manifest["rows"]:
            payload = files[row["filename"]]
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            config = json.loads(payload)
            self.assertEqual(config["simulation"]["duration"], row["duration"])
            self.assertEqual(config["simulation"]["run_id"], row["run_id"])
            self.assertFalse(config["simulation"]["research_eligible"])
            self.assertEqual(config["simulation"]["response_failure_policy"], "abort_run")
            self.assertEqual(config["simulation"]["response_contract_version"], "phase-response-v3.0.0")
            self.assertEqual(config["simulation"]["prompt_contract_version"], "bounded-prompts-v3.0.0")
            self.assertEqual(config["llm_defaults"]["max_tokens"], 1024)
            self.assertEqual(config["llm_defaults"]["max_concurrency"], 24)

    def test_only_declared_new_conditions_are_accepted(self):
        invalid = [("qwen_only", "edge", 60, 6301), ("mixed", "near", 60, 6301),
                   ("mixed", "edge", 61, 6301), ("mixed", "edge", 60, 6201),
                   ("mixed", "edge", 60, 6202), ("mixed", "edge", 60, 6302)]
        for condition in invalid:
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                study.build_config(*condition)

    def test_common_eligible_set_excludes_both_refuges_and_preserves_all_initial_positions(self):
        expected_cells = {
            (x, y) for x in range(-25, 26) for y in range(-25, 18)
            if not (6 <= y <= 11 and (-10 <= x <= -5 or 5 <= x <= 10))
        }
        self.assertEqual(len(expected_cells), 2121)
        initial_positions = []
        for condition in study.RUN_ORDER:
            config = study.build_config(*condition)
            scenario = scenario_for(config)
            self.assertEqual(set(scenario.eligible_initial_cells()), expected_cells)
            self.assertEqual(len(config["scenario"]["initial_eligible_rectangles"]), 5)
            world = World(25, [], disaster=scenario)
            initial_positions.append(world.generate_initial_positions(24, random.Random(6301)))
            self.assertFalse(world.is_hazardous(9, 0, -10))
            self.assertTrue(world.is_hazardous(10, 0, -10))
            self.assertFalse(world.is_hazardous(29, 0, -1))
            self.assertTrue(world.is_hazardous(30, 0, -1))
            self.assertEqual(scenario.official_warning.initial_recipient_ids, (1, 5, 9, 13, 17, 21))
            self.assertEqual(scenario.official_warning.issue_step, 10)
            self.assertEqual(scenario.communication_mode, "free_text")
            for refuge in scenario.refuges:
                self.assertEqual(len(list(refuge.rectangle.cells())), 36)
                self.assertTrue(all(not world.is_hazardous(120, *p) for p in refuge.rectangle.cells()))
                self.assertFalse(set(refuge.rectangle.cells()) & expected_cells)
        self.assertTrue(all(p == initial_positions[0] for p in initial_positions))
        self.assertEqual(len(set(initial_positions[0])), 24)

    def test_layout_is_the_only_condition_change_at_each_fixed_horizon(self):
        for duration in (60, 120):
            conditions = []
            for layout in ("edge", "inset"):
                config = study.build_config("mixed", layout, duration, 6301)
                del config["simulation"]["run_id"]
                del config["simulation"]["run_name"]
                del config["scenario"]["refuges"]
                conditions.append(config)
            self.assertEqual(*conditions)
        for layout in ("edge", "inset"):
            conditions = []
            for duration in (60, 120):
                config = study.build_config("mixed", layout, duration, 6301)
                for field in ("run_id", "run_name", "duration"):
                    del config["simulation"][field]
                conditions.append(config)
            self.assertEqual(*conditions)

    def test_geometric_reachability_claims_and_mutation_isolation(self):
        expected = {"edge": (61, 1), "inset": (46, 0)}
        for layout, (maximum, over60) in expected.items():
            config = study.build_config("mixed", layout, 60, 6301)
            scenario = scenario_for(config)
            distances = [scenario.shortest_refuge_distance(*p) for p in scenario.eligible_initial_cells()]
            self.assertEqual(max(distances), maximum)
            self.assertEqual(sum(d > 60 for d in distances), over60)
            original = study.build_config("mixed", layout, 60, 6301)
            config["scenario"]["refuges"][0]["rectangle"]["x_min"] = -24
            config["scenario"]["initial_eligible_rectangles"][0]["x_min"] = -24
            self.assertEqual(study.build_config("mixed", layout, 60, 6301), original)

    def test_fixed_model_artifacts_assignment_and_balanced_initial_recipients(self):
        revisions = {
            "qwen": "a09a35458c702b33eeacc393d103063234e8bc28",
            "llama": "0e9e39f249a16976918f6564b8830bc894c89659",
            "gemma": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
        }
        config = study.build_config("mixed", "edge", 60, 6301)
        self.assertEqual([b["name"] for b in config["blocs"]], ["qwen", "llama", "gemma"])
        for index, bloc in enumerate(config["blocs"]):
            model = bloc["name"]
            self.assertEqual(bloc["num_agents"], 8)
            self.assertEqual(bloc["model_digest"], revisions[model])
            self.assertEqual(bloc["tokenizer_revision"], revisions[model])
            self.assertEqual(bloc["max_model_len"], 4096)
            self.assertEqual(bloc["llm_overrides"], {})
            self.assertEqual(bloc["endpoint_id"], f"refuge-{model}")
            self.assertEqual(bloc["tensor_parallel_size"], 2 if model == "gemma" else 1)
            self.assertNotIn("base_url", bloc)
            self.assertEqual(sum(index * 8 <= i < (index + 1) * 8 for i in study.OFFICIAL_RECIPIENT_IDS), 2)

    def test_writer_refuses_existing_config_and_run_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            runs = root / "runs"
            study.write_files(output, runs)
            study.load_verified_manifest(output)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(FileExistsError):
                study.write_files(output, runs)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            run_id = study.build_config("mixed", "edge", 60, 6301)["simulation"]["run_id"]
            (runs / f"output_{run_id}").mkdir(parents=True)
            absent = root / "other-config"
            with self.assertRaises(FileExistsError):
                study.write_files(absent, runs)
            self.assertFalse(absent.exists())

    def test_read_only_check_rejects_tampering_and_extra_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "config"
            study.write_files(output, Path(temporary) / "runs")
            manifest = study.load_verified_manifest(output)
            altered = output / manifest["rows"][0]["filename"]
            altered.write_bytes(altered.read_bytes() + b" ")
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(ValueError):
                study.load_verified_manifest(output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            (output / "unexpected.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file set"):
                study.load_verified_manifest(output)

    def test_unsafe_input_or_spec_mismatch_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "config"
            unsafe = copy.deepcopy(study.base_config("mixed", "free_text", 6301, "a"))
            unsafe["blocs"][0]["hostname"] = "runtime-only"
            with mock.patch.object(study, "base_config", return_value=unsafe):
                with self.assertRaises(ValueError):
                    study.write_files(output, root / "runs")
            self.assertFalse(output.exists())
            bad_spec = root / "bad-spec.md"
            bad_spec.write_text("different specification", encoding="utf-8")
            with mock.patch.object(study, "METRIC_SPEC_PATH", bad_spec):
                with self.assertRaisesRegex(ValueError, "specification"):
                    study.write_files(output, root / "runs")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
