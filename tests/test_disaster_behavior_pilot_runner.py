import argparse
import base64
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import disaster_behavior_schema_probe as probe
from tools import run_disaster_behavior_pilot as runner
from tools.build_disaster_behavior_pilot import BATCH_ID, CALLS_PER_RUN
from tools.build_disaster_behavior_pilot import OUTPUT_DIR, WALL_TIME_LIMIT_SECONDS


class FollowupRunnerTests(unittest.TestCase):
    def test_runner_uses_active_manifest_and_remaining_wall_budget(self):
        self.assertEqual(runner.MANIFEST, OUTPUT_DIR / "manifest.json")
        self.assertEqual(runner.MAX_WALL_S, WALL_TIME_LIMIT_SECONDS)

    def test_ipc_path_guard_counts_bytes_and_accepts_exact_boundary(self):
        self.assertEqual(runner.check_runtime_ipc_path(Path("x" * 66)), 107)
        for root in (Path("x" * 67), Path("記" * 23)):
            with self.subTest(root=root), self.assertRaises(runner.PublicVllmError):
                runner.check_runtime_ipc_path(root)

    def test_long_predicted_temp_path_rejects_before_source_or_gpu_checks(self):
        args = argparse.Namespace(gpu_indices="0,1,2,3", base_port=18500,
                                  source_git_sha="a" * 40, contract_only=False, preflight_only=True)
        with mock.patch.object(runner, "os", SimpleNamespace(name="posix")), \
             mock.patch.object(runner.tempfile, "gettempdir", return_value="x" * 80), \
             mock.patch.object(runner, "source_gate", autospec=True) as source, \
             mock.patch.object(runner, "create_gpu_guard", autospec=True) as gpu, \
             mock.patch.object(runner.tempfile, "TemporaryDirectory", autospec=True) as allocate:
            with self.assertRaises(runner.PublicVllmError):
                runner.run(args)
        source.assert_not_called()
        gpu.assert_not_called()
        allocate.assert_not_called()

    def test_missing_source_sha_is_a_safe_rejection(self):
        with self.assertRaises(runner.PublicVllmError):
            runner.source_gate(None)

    def test_run_gate_requires_480_calls_and_exact_identity(self):
        config = {"simulation": {"run_id": "fixture"}}
        meta = {
            "logical_llm_calls": CALLS_PER_RUN, "http_attempts": CALLS_PER_RUN,
            "git_sha": "a" * 40, "git_dirty": False, "config": config,
            **{key: 0 for key in runner.FAILURE_COUNTERS},
        }
        with mock.patch.object(runner, "verify_completed_run", return_value=(True, 5)), \
             mock.patch.object(runner, "_load_json_object", return_value=meta), \
             mock.patch.object(runner, "scan_tree", return_value=[]), \
             mock.patch.object(runner, "public_tree_safe", return_value=True), \
             mock.patch.object(runner, "_tree_digest", return_value="digest"):
            self.assertTrue(runner.check_run(Path("fixture"), config, [], "a" * 40)["completion_gate_passed"])
            meta["http_attempts"] = 720
            self.assertFalse(runner.check_run(Path("fixture"), config, [], "a" * 40)["completion_gate_passed"])
            meta["http_attempts"] = CALLS_PER_RUN
            meta["git_dirty"] = True
            self.assertFalse(runner.check_run(Path("fixture"), config, [], "a" * 40)["completion_gate_passed"])

    def test_probe_is_one_direct_request_with_no_redirect_or_retry(self):
        model = {"name": "qwen", "model": "model", "model_source": "test/model", "model_digest": "a" * 40}
        response = SimpleNamespace(status_code=307, content=b'{"redirect":"blocked"}')
        with mock.patch.object(probe.PROBE_HTTP_SESSION, "post", return_value=response) as post:
            result = probe.run_one_request(model=model, case=probe.CASES[0], base_url="http://127.0.0.1:1", timeout_s=5)
        self.assertFalse(probe.PROBE_HTTP_SESSION.trust_env)
        self.assertEqual(post.call_count, 1)
        self.assertIs(post.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(result["failure_code"], "http_non_2xx")
        self.assertEqual(result["response_body_base64"], base64.b64encode(response.content).decode())

    def test_decoded_runtime_binding_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            encoded = base64.b64encode(b'{"url":"http:\\/\\/127.0.0.1:18500"}').decode()
            (root / "probe.json").write_text(json.dumps({"response_body_base64": encoded}))
            self.assertFalse(runner.public_tree_safe(root, [SimpleNamespace(base_url="http://127.0.0.1:18500")]))

    def test_probe_rejects_truncation_invalid_usage_and_wrong_model(self):
        model = {"name": "qwen", "model": "model", "model_source": "test/model", "model_digest": "a" * 40}
        base_envelope = {
            "model": "model", "choices": [{"finish_reason": "stop", "message": {
                "content": '{"message":"probe-ok","reasoning":""}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        for kind, expected in (("length", "finish_reason_not_stop"),
                               ("usage", "usage_total_mismatch"),
                               ("model", "served_model_identity_mismatch")):
            with self.subTest(kind=kind):
                envelope = json.loads(json.dumps(base_envelope))
                if kind == "length":
                    envelope["choices"][0]["finish_reason"] = "length"
                elif kind == "usage":
                    envelope["usage"]["total_tokens"] = 6
                else:
                    envelope["model"] = "different-model"
                response = SimpleNamespace(status_code=200, content=json.dumps(envelope).encode())
                with mock.patch.object(probe.PROBE_HTTP_SESSION, "post", return_value=response) as post:
                    result = probe.run_one_request(model=model, case=probe.CASES[0], base_url="http://127.0.0.1:1", timeout_s=5)
                self.assertEqual(post.call_count, 1)
                self.assertEqual(result["failure_code"], expected)

    def test_complete_execution_gates_nine_probes_then_six_runs_and_promotes_all(self):
        self._mock_execution()

    def test_failed_probe_retains_nine_attempts_and_starts_no_simulation(self):
        self._mock_execution(failed_probe=True)

    def test_failed_run_stops_batch_without_retry_or_partial_promotion(self):
        self._mock_execution(failed_run=True)

    def test_actual_allocated_path_is_checked_before_any_server_starts(self):
        self._mock_execution(invalid_allocated_path=True)

    def test_existing_batch_collision_does_not_create_more_output(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(runner, "REPO_ROOT", Path(temp)):
            root = Path(temp)
            stage = root / ".tmp" / BATCH_ID
            stage.mkdir(parents=True)
            (stage / "retained.txt").write_text("unchanged")
            with self.assertRaises(runner.PublicVllmError):
                runner.checked_output_paths(BATCH_ID, ["run-one"])
            self.assertFalse((root / "runs").exists())
            self.assertFalse((root / "derived").exists())
            self.assertEqual((stage / "retained.txt").read_text(), "unchanged")

    def _mock_execution(self, failed_probe=False, failed_run=False, invalid_allocated_path=False):
        manifest, configs, models, union_config, lock = runner.load_inputs()
        events = []
        specs = [SimpleNamespace(endpoint_id=model["endpoint_id"], base_url=f"http://127.0.0.1:{18500+i}")
                 for i, model in enumerate(models.values())]

        def request(**kwargs):
            events.append("probe")
            return {"result": "fail" if failed_probe and len(events) == 1 else "pass"}

        def start_sim(runtime_root, shadow, config_path, binding_path, output_root):
            events.append("simulation")
            config = json.loads(config_path.read_text())
            raw = output_root / ("output_" + config["simulation"]["run_id"])
            raw.mkdir()
            (raw / "fixture.json").write_text("{}")
            return SimpleNamespace()

        def check_run(raw, config, specs, sha):
            passed = not (failed_run and events.count("simulation") == 2)
            return {"run_id": config["simulation"]["run_id"], "status": "completed" if passed else "aborted",
                    "completion_gate_passed": passed, "http_attempts": CALLS_PER_RUN,
                    "logical_llm_calls": CALLS_PER_RUN, "run_tree_sha256": runner._tree_digest(raw)}

        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            allocate = stack.enter_context(mock.patch.object(
                runner.tempfile, "TemporaryDirectory", wraps=tempfile.TemporaryDirectory))
            if invalid_allocated_path:
                allocate.return_value = nullcontext(root / ("x" * 80))
            stack.enter_context(mock.patch.object(runner, "REPO_ROOT", root))
            stack.enter_context(mock.patch.object(runner, "os", SimpleNamespace(name="posix")))
            replacements = {
                "load_inputs": (manifest, configs, models, union_config, lock),
                "predicted_runtime_ipc_path_bytes": 68,
                "source_gate": None, "check_installed_runtime": {}, "attach_snapshots": specs,
                "build_endpoint_specs": specs, "ports_are_free": True,
                "create_gpu_guard": SimpleNamespace(observe=lambda rows: None, max_observed_active_gpu_count=4),
                "query_gpu_rows": {}, "write_flashinfer_shadow": None,
                "start_servers_sequentially": None, "wait_for_simulator": 0,
                "stop_process_groups": True, "wait_for_gpu_release": True,
            }
            if not invalid_allocated_path:
                # This flow fixture isolates host TEMP length; the dedicated
                # guard tests exercise real predicted and allocated paths.
                replacements["check_runtime_ipc_path"] = 68
            mocks = {name: stack.enter_context(mock.patch.object(runner, name, return_value=value, autospec=True))
                     for name, value in replacements.items()}
            stack.enter_context(mock.patch.object(runner, "run_one_request", side_effect=request))
            stack.enter_context(mock.patch.object(runner, "start_simulator", side_effect=start_sim))
            stack.enter_context(mock.patch.object(runner, "check_run", side_effect=check_run))
            args = argparse.Namespace(gpu_indices="0,1,2,3", base_port=18500, source_git_sha="a" * 40,
                                      contract_only=False, preflight_only=False)
            failed = failed_probe or failed_run or invalid_allocated_path
            self.assertEqual(runner.run(args), 3 if failed else 0)
            allocate.assert_called_once_with(prefix=runner.RUNTIME_PREFIX)
            mocks["create_gpu_guard"].assert_called_once_with((0, 1, 2, 3), 4, 256)
            if invalid_allocated_path:
                mocks["start_servers_sequentially"].assert_not_called()
                mocks["stop_process_groups"].assert_not_called()
                mocks["wait_for_gpu_release"].assert_not_called()
            else:
                mocks["stop_process_groups"].assert_called_once()
                mocks["wait_for_gpu_release"].assert_called_once()
            if failed:
                expected_events = [] if invalid_allocated_path else ["probe"] * 9 + (["simulation"] * 2 if failed_run else [])
                self.assertEqual(events, expected_events)
                self.assertFalse((root / "runs").exists())
                evidence = root / ".tmp" / BATCH_ID / "verification.json"
            else:
                self.assertEqual(events, ["probe"] * 9 + ["simulation"] * 6)
                self.assertEqual(len(list((root / "runs").glob("output_*"))), 6)
                evidence = root / "derived" / ("validation-" + BATCH_ID) / "verification.json"
                artifact_manifest = json.loads((evidence.parent / "artifact_manifest.json").read_text())
                self.assertEqual(set(artifact_manifest["files"]), {"probe/attempts.jsonl", "verification.json"})
            result = json.loads(evidence.read_text())
            self.assertEqual(result["probe_attempts"], 0 if invalid_allocated_path else 9)
            self.assertEqual(result["total_http_attempts"], 0 if invalid_allocated_path else 9 if failed_probe else 969 if failed_run else 2889)
            self.assertEqual(result["gate_passed"], not failed)
            if failed_run:
                self.assertEqual([row["status"] for row in result["runs"]],
                                 ["completed", "aborted"] + ["not_started"] * 4)

    def test_promotion_failure_rolls_back_every_moved_run(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(runner, "REPO_ROOT", Path(temp)):
            root = Path(temp)
            stage = root / ".tmp" / "batch"
            (stage / "probe").mkdir(parents=True)
            (stage / "probe" / "attempts.jsonl").write_text("{}\n")
            (stage / "verification.json").write_text("{}")
            rows = []
            for name in ("one", "two"):
                raw = stage / "runs" / ("output_" + name)
                raw.mkdir(parents=True)
                (raw / "raw.jsonl").write_text("{}\n")
                rows.append({"run_tree_sha256": runner._tree_digest(raw)})
            original_rename = Path.rename

            def fail_second(source, destination):
                if source.name == "output_two":
                    raise OSError("injected move failure")
                return original_rename(source, destination)

            with mock.patch.object(Path, "rename", fail_second), self.assertRaises(runner.PublicVllmError):
                runner.promote_batch(stage, root / "derived" / "validation-batch", ["one", "two"], rows)
            self.assertEqual(list((root / "runs").iterdir()), [])
            self.assertTrue((stage / "runs" / "output_one" / "raw.jsonl").is_file())
            failure = json.loads((stage / "promotion_failure.json").read_text())
            self.assertTrue(failure["publication_blocked"])
            self.assertFalse(failure["partial_promotion_detected"])
