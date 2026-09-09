import argparse
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import yaml

from tools import run_refuge_layout_study as runner


class RefugeLayoutStudyRunnerTests(unittest.TestCase):
    def args(self, **overrides):
        values = dict(gpu_indices="2,3,4,5", base_port=18600,
                      source_git_sha="a" * 40, contract_only=False, preflight_only=False)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_contract_only_never_inspects_host_or_creates_output(self):
        with mock.patch.object(runner, "source_gate") as source, \
             mock.patch.object(runner, "create_gpu_guard") as gpu, \
             mock.patch.object(runner, "checked_output_paths") as output, \
             mock.patch.object(runner.tempfile, "TemporaryDirectory") as allocate:
            self.assertEqual(runner.run(self.args(contract_only=True)), 0)
        source.assert_not_called()
        gpu.assert_not_called()
        output.assert_not_called()
        allocate.assert_not_called()

    def test_contract_rejects_more_than_four_gpus(self):
        with self.assertRaises(runner.PublicVllmError):
            runner.run(self.args(contract_only=True, gpu_indices="0,1,2,3,4"))

    def test_frozen_mixed_rows_derive_variable_request_counts(self):
        manifest, configs, models, union, _ = runner.load_inputs()
        self.assertEqual(len(configs), 4)
        self.assertEqual(tuple(models), ("qwen", "llama", "gemma"))
        self.assertEqual([runner.expected_calls(c) for c in configs.values()],
                         [2880, 2880, 5760, 5760])
        self.assertEqual(sum(runner.expected_calls(c) for c in configs.values()), 17280)
        self.assertEqual(manifest["total_http_attempt_cap"], 17289)
        self.assertEqual(runner.MAX_WALL_S, 10800)
        self.assertEqual(sum(bloc["num_agents"] for bloc in union["blocs"]), 24)

    def test_call_derivation_rejects_incomplete_population_and_wrong_duration(self):
        config = next(iter(runner.load_inputs()[1].values()))
        for change in ("duration", "single_model", "unequal_bloc"):
            with self.subTest(change=change):
                altered = copy.deepcopy(config)
                if change == "duration":
                    altered["simulation"]["duration"] = 90
                elif change == "single_model":
                    altered["blocs"] = altered["blocs"][:1]
                else:
                    altered["blocs"][0]["num_agents"] = 4
                with self.assertRaises(runner.PublicVllmError):
                    runner.expected_calls(altered)

    def test_manifest_call_mismatch_is_rejected_before_runtime(self):
        manifest = copy.deepcopy(runner.load_inputs()[0])
        manifest["rows"][0]["expected_http_attempts"] = 480
        with mock.patch.object(runner, "load_verified_manifest", return_value=manifest), \
             mock.patch.object(runner, "create_gpu_guard") as gpu:
            with self.assertRaisesRegex(runner.PublicVllmError, "manifest row"):
                runner.run(self.args(contract_only=True))
        gpu.assert_not_called()

    def test_mixed_binding_selection_includes_all_models_and_rejects_missing_or_duplicate(self):
        _, configs, models, union, _ = runner.load_inputs()
        specs = runner.build_endpoint_specs(union, (2, 3, 4, 5), 18600)
        required = {model["endpoint_id"] for model in models.values()}
        for config in configs.values():
            self.assertEqual({s.endpoint_id for s in runner.specs_for_config(config, specs)}, required)
            for incomplete in (specs[:1], specs[:-1], specs + specs[:1]):
                with self.subTest(run=config["simulation"]["run_id"], count=len(incomplete)):
                    with self.assertRaisesRegex(runner.PublicVllmError, "routing"):
                        runner.specs_for_config(config, incomplete)

    def test_run_completion_requires_each_duration_budget_and_exact_source(self):
        configs = runner.load_inputs()[1]
        for config in (next(c for c in configs.values() if c["simulation"]["duration"] == d)
                       for d in (60, 120)):
            calls = runner.expected_calls(config)
            meta = {"status": "completed", "completed_steps": config["simulation"]["duration"],
                    "logical_llm_calls": calls, "http_attempts": calls,
                    "git_sha": "a" * 40, "git_dirty": False, "config": config,
                    **{key: 0 for key in runner.FAILURE_COUNTERS}}
            with mock.patch.object(runner, "verify_completed_run", return_value=(True, 5)), \
                 mock.patch.object(runner, "_load_json_object", return_value=meta), \
                 mock.patch.object(runner, "scan_tree", return_value=[]), \
                 mock.patch.object(runner, "public_tree_safe", return_value=True), \
                 mock.patch.object(runner, "_tree_digest", return_value="digest"):
                row = runner.check_run(Path("fixture"), config, [], "a" * 40)
                self.assertTrue(row["completion_gate_passed"])
                self.assertEqual(row["expected_http_attempts"], calls)
                self.assertEqual(row["expected_agents"], 24)
                meta["http_attempts"] = calls - 1
                self.assertFalse(runner.check_run(Path("fixture"), config, [], "a" * 40)["completion_gate_passed"])
                meta["http_attempts"] = calls
                meta["git_dirty"] = True
                self.assertFalse(runner.check_run(Path("fixture"), config, [], "a" * 40)["completion_gate_passed"])

    def test_long_runtime_path_rejects_before_source_or_gpu_access(self):
        with mock.patch.object(runner, "os", SimpleNamespace(name="posix")), \
             mock.patch.object(runner.tempfile, "gettempdir", return_value="x" * 80), \
             mock.patch.object(runner, "source_gate") as source, \
             mock.patch.object(runner, "create_gpu_guard") as gpu:
            with self.assertRaises(runner.PublicVllmError):
                runner.run(self.args(preflight_only=True))
        source.assert_not_called()
        gpu.assert_not_called()

    def test_run_and_batch_collisions_preserve_existing_bytes(self):
        for location in ("stage", "run"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temp, \
                 mock.patch.object(runner, "REPO_ROOT", Path(temp)):
                root = Path(temp)
                retained = root / ".tmp" / runner.BATCH_ID if location == "stage" else root / "runs" / "output_one"
                retained.mkdir(parents=True)
                (retained / "retained.jsonl").write_bytes(b"unchanged\n")
                before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with self.assertRaises(runner.PublicVllmError):
                    runner.checked_output_paths(runner.BATCH_ID, ["one"])
                self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()})
                self.assertFalse((root / "derived").exists())

    def test_complete_batch_gates_nine_probes_then_all_four_mixed_runs(self):
        self.mock_execution()

    def test_probe_failure_starts_no_simulation_and_preserves_all_attempts(self):
        self.mock_execution("probe")

    def test_interrupted_second_run_is_retained_without_retry_or_promotion(self):
        self.mock_execution("interrupt")

    def test_cleanup_failure_preserves_four_completed_runs_without_promotion(self):
        self.mock_execution("cleanup")

    def test_promotion_error_rolls_back_prior_moves_without_editing_raw(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(runner, "REPO_ROOT", Path(temp)):
            root = Path(temp)
            stage = root / ".tmp" / "fixture-batch"
            (stage / "probe").mkdir(parents=True)
            (stage / "probe" / "attempts.jsonl").write_bytes(b"{}\n")
            (stage / "verification.json").write_bytes(b"{}\n")
            rows = []
            for name in ("first", "second"):
                raw = stage / "runs" / ("output_" + name)
                raw.mkdir(parents=True)
                (raw / "retained.jsonl").write_bytes(b"{\"unchanged\":true}\n")
                rows.append({"run_tree_sha256": runner._tree_digest(raw)})
            original = Path.rename

            def fail_second(path, destination):
                if path.name == "output_second":
                    raise OSError("injected promotion failure")
                return original(path, destination)

            with mock.patch.object(Path, "rename", fail_second), self.assertRaises(runner.PublicVllmError):
                runner.promote_batch(stage, root / "derived" / "validation-fixture", ["first", "second"], rows)
            self.assertFalse(list((root / "runs").iterdir()))
            for name, row in zip(("first", "second"), rows):
                self.assertEqual(runner._tree_digest(stage / "runs" / ("output_" + name)), row["run_tree_sha256"])
            failure = json.loads((stage / "promotion_failure.json").read_text())
            self.assertTrue(failure["publication_blocked"])
            self.assertFalse(failure["partial_promotion_detected"])

    def mock_execution(self, failure=None):
        manifest, configs, models, union, lock = runner.load_inputs()
        specs = runner.build_endpoint_specs(union, (2, 3, 4, 5), 18600)
        events = []
        bindings_seen = []

        def request(**kwargs):
            events.append("probe")
            return {"result": "fail" if failure == "probe" and len(events) == 1 else "pass"}

        def start_sim(runtime_root, shadow, config_path, binding_path, output_root):
            self.assertEqual(events[:9], ["probe"] * 9)
            events.append("simulation")
            config = json.loads(config_path.read_text())
            bindings = yaml.safe_load(binding_path.read_text())
            bindings_seen.append(set(bindings["endpoints"]))
            run_id = config["simulation"]["run_id"]
            raw = output_root / ("output_" + run_id)
            raw.mkdir()
            interrupted = failure == "interrupt" and events.count("simulation") == 2
            calls = 96 if interrupted else runner.expected_calls(config)
            meta = {"run_id": run_id, "status": "aborted" if interrupted else "completed",
                    "completed_steps": 2 if interrupted else config["simulation"]["duration"],
                    "logical_llm_calls": calls, "http_attempts": calls}
            (raw / "run_meta.json").write_text(json.dumps(meta))
            (raw / "retained.jsonl").write_bytes(b"{\"immutable\":true}\n")
            return SimpleNamespace(interrupted=interrupted)

        def wait_sim(sim, servers, guard, remaining):
            if sim.interrupted:
                raise runner.PublicVllmError("fixture interruption")
            return 0

        def check_run(raw, config, specs, sha):
            meta = json.loads((raw / "run_meta.json").read_text())
            return {**meta, "completion_gate_passed": True, "run_tree_sha256": runner._tree_digest(raw)}

        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            stack.enter_context(mock.patch.object(runner, "REPO_ROOT", root))
            stack.enter_context(mock.patch.object(runner, "os", SimpleNamespace(name="posix")))
            replacements = {
                "load_inputs": (manifest, configs, models, union, lock),
                "predicted_runtime_ipc_path_bytes": 68, "check_runtime_ipc_path": 68,
                "source_gate": None, "check_installed_runtime": lock["packages"],
                "attach_snapshots": specs, "ports_are_free": True,
                "create_gpu_guard": SimpleNamespace(observe=lambda rows: None, max_observed_active_gpu_count=4),
                "query_gpu_rows": {}, "write_flashinfer_shadow": None,
                "start_servers_sequentially": None, "stop_process_groups": failure != "cleanup",
                "wait_for_gpu_release": True,
            }
            mocks = {name: stack.enter_context(mock.patch.object(runner, name, return_value=value, autospec=True))
                     for name, value in replacements.items()}
            stack.enter_context(mock.patch.object(runner, "run_one_request", side_effect=request))
            stack.enter_context(mock.patch.object(runner, "start_simulator", side_effect=start_sim))
            stack.enter_context(mock.patch.object(runner, "wait_for_simulator", side_effect=wait_sim))
            stack.enter_context(mock.patch.object(runner, "check_run", side_effect=check_run))
            self.assertEqual(runner.run(self.args()), 3 if failure else 0)
            mocks["create_gpu_guard"].assert_called_once_with((2, 3, 4, 5), 4, 256)
            mocks["stop_process_groups"].assert_called_once()
            mocks["wait_for_gpu_release"].assert_called_once()
            expected_simulations = 0 if failure == "probe" else 2 if failure == "interrupt" else 4
            self.assertEqual(events, ["probe"] * 9 + ["simulation"] * expected_simulations)
            self.assertTrue(all(row == {model["endpoint_id"] for model in models.values()} for row in bindings_seen))
            if failure:
                self.assertFalse((root / "runs").exists())
                evidence = root / ".tmp" / runner.BATCH_ID / "verification.json"
            else:
                self.assertEqual(len(list((root / "runs").glob("output_*"))), 4)
                evidence = root / "derived" / ("validation-" + runner.BATCH_ID) / "verification.json"
            result = json.loads(evidence.read_text())
            self.assertEqual(result["gate_passed"], not failure)
            expected_attempts = 9 if failure == "probe" else 2985 if failure == "interrupt" else 17289
            self.assertEqual(result["total_http_attempts"], expected_attempts)
            self.assertEqual(result["total_http_attempt_cap"], 17289)
            if failure == "interrupt":
                self.assertEqual([row["status"] for row in result["runs"]],
                                 ["completed", "aborted", "not_started", "not_started"])
                retained = evidence.parent / "runs" / ("output_" + list(configs)[1]) / "retained.jsonl"
                self.assertEqual(retained.read_bytes(), b"{\"immutable\":true}\n")


if __name__ == "__main__":
    unittest.main()
