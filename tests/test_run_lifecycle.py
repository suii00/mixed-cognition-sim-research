import copy
import base64
import hashlib
import json
import multiprocessing
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import main as cli_main
import yaml
from engine.llm_client import LLMTransportError
from engine.provenance import (
    RAW_JSONL_FILES,
    RunCollisionError,
    RunLifecycleError,
)
from engine.sim import Simulation, SimulationAbortedError


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_BINDINGS = {"mock-endpoint": {"base_url": "http://127.0.0.1:1"}}
CLI_ARGS = [
    "--config",
    "ignored.yaml",
    "--runtime-bindings",
    "bindings.yaml",
]


def make_config(run_id: str = "test-run") -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 42,
            "run_name": "test_run",
            "run_id": run_id,
            "protocol_version": "test-protocol-v1",
            "metric_version": "test-metric-v1",
        },
        "blocs": [
            {
                "name": "alpha",
                "model": "mock-model",
                "endpoint_id": "mock-endpoint",
                "num_agents": 1,
            }
        ],
        "agents": {
            "communication_radius": 1,
            "memory_limit": 2,
            "memory_size": 1,
            "message_history_limit": 2,
            "message_context_size": 1,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 32,
            "timeout_s": 1,
        },
    }


def successful_llm(**kwargs):
    telemetry = kwargs.get("telemetry")
    if telemetry is not None:
        telemetry("http_attempt", 1)
    if "Decide your next action." in kwargs["prompt"]:
        parsed = {
            "action": "stay",
            "direction": "",
            "memory": "",
            "reasoning": "",
        }
    else:
        parsed = {"message": "", "reasoning": ""}
    raw_output = json.dumps(parsed)
    emit_mock_attempt(kwargs, raw_output, valid_json=True)
    return parsed, raw_output


def emit_mock_attempt(kwargs, raw_output: str, valid_json: bool) -> None:
    observer = kwargs.get("attempt_observer")
    if observer is None:
        return
    envelope = {
        "message": {"content": raw_output},
        "done_reason": "stop",
        "prompt_eval_count": 1,
        "eval_count": 1,
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    observer({
        "generation_attempt": 1,
        "http_attempt": 1,
        "http_status": 200,
        "http_response_body_base64": base64.b64encode(body).decode("ascii"),
        "http_response_bytes": len(body),
        "http_response_sha256": hashlib.sha256(body).hexdigest(),
        "envelope": envelope,
        "raw_output": raw_output,
        "finish_reason": "stop",
        "usage": {"prompt_eval_count": 1, "eval_count": 1},
        "transport_status": "ok",
        "parse_status": "valid" if valid_json else "invalid",
        "schema_status": "not_checked",
        "failure_kind": None if valid_json else "syntax",
        "error_type": None,
    })


def load_meta(output_dir: Path) -> dict:
    with (output_dir / "run_meta.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


def directory_hashes(output_dir: Path) -> dict:
    hashes = {}
    for path in sorted(output_dir.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(output_dir))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def simulation_state(simulation: Simulation) -> dict:
    """Snapshot mutable in-memory state relevant to one-shot execution."""
    return copy.deepcopy({
        "config": simulation.config,
        "rng_state": simulation.rng.getstate(),
        "agents": [
            {
                "agent_id": agent.agent_id,
                "position": agent.position,
                "memories": agent.memories,
                "received_messages": agent.received_messages,
            }
            for agent in simulation.agents
        ],
        "parse_error_count": simulation.parse_error_count,
        "total_llm_calls": simulation.total_llm_calls,
        "lifecycle_meta": simulation.run_lifecycle.meta,
        "lifecycle_context": (
            simulation.run_lifecycle.current_step,
            simulation.run_lifecycle.current_phase,
            simulation.run_lifecycle.current_agent_id,
        ),
        "observed_agent_ids": simulation.run_lifecycle._observed_agent_ids,
        "lifecycle_terminal": simulation.run_lifecycle._terminal,
        "execution_claimed": simulation.run_lifecycle._execution_claimed,
    })


class ScriptedLLM:
    """Deterministic test double keyed by phase and agent ID."""

    _AGENT_ID = re.compile(r"^You are Agent (\d+) in a 2D grid world\.")

    def __init__(
        self,
        phase3_results: dict[int, dict],
        before_phase3=None,
        transport_failure_agents=(),
        parse_failure_agents=(),
    ):
        self.phase3_results = phase3_results
        self.before_phase3 = before_phase3
        self.transport_failure_agents = set(transport_failure_agents)
        self.parse_failure_agents = set(parse_failure_agents)

    @staticmethod
    def _emit(kwargs, event: str, amount: int = 1) -> None:
        telemetry = kwargs.get("telemetry")
        if telemetry is not None:
            telemetry(event, amount)

    def __call__(self, **kwargs):
        prompt = kwargs["prompt"]
        match = self._AGENT_ID.match(prompt)
        if match is None:
            raise AssertionError("scripted LLM received an unknown prompt")
        agent_id = int(match.group(1))

        if "Decide your next action." not in prompt:
            self._emit(kwargs, "http_attempt")
            parsed = {"message": "", "reasoning": ""}
            raw_output = json.dumps(parsed)
            emit_mock_attempt(kwargs, raw_output, valid_json=True)
            return parsed, raw_output

        if self.before_phase3 is not None:
            self.before_phase3(agent_id)

        if agent_id in self.transport_failure_agents:
            self._emit(kwargs, "http_attempt", 3)
            self._emit(kwargs, "transport_failure", 3)
            raise LLMTransportError("scripted Phase 3 transport failure")

        if agent_id in self.parse_failure_agents:
            self._emit(kwargs, "http_attempt")
            self._emit(kwargs, "syntax_parse_attempt_failure")
            emit_mock_attempt(kwargs, "not-json", valid_json=False)
            return None, "not-json"

        self._emit(kwargs, "http_attempt")
        parsed = self.phase3_results[agent_id]
        raw_output = json.dumps(parsed)
        emit_mock_attempt(kwargs, raw_output, valid_json=True)
        return parsed, raw_output


def claim_run_directory_in_process(
    config: dict,
    output_root: str,
    repo_root: str,
    start_barrier,
    result_queue,
) -> None:
    """Race one exclusive run-directory claim without probing real hardware."""
    from engine import provenance

    provenance.collect_git_info = lambda _repo_root=None: {
        "git_sha": "c" * 40,
        "git_dirty": False,
        "git_probe_status": "available",
        "git_probe_errors": [],
    }
    provenance.collect_gpu_info = lambda: {
        "status": "unavailable",
        "error": "test_disabled",
        "driver_version": None,
        "cuda_version": None,
        "devices": [],
    }

    try:
        start_barrier.wait(timeout=10)
        lifecycle = provenance.RunLifecycle.create(
            config,
            output_root=Path(output_root),
            repo_root=Path(repo_root),
        )
    except provenance.RunCollisionError:
        result_queue.put(("collision", None))
    except BaseException as error:
        result_queue.put(("error", type(error).__name__))
    else:
        result_queue.put(("created", lifecycle.meta["logical_llm_calls"]))


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.output_root = Path(self.temp_directory.name)

        git_info = {
            "git_sha": "a" * 40,
            "git_dirty": True,
            "git_probe_status": "available",
            "git_probe_errors": [],
        }
        gpu_info = {
            "status": "unavailable",
            "error": "test_disabled",
            "driver_version": None,
            "cuda_version": None,
            "devices": [],
        }
        self.git_patch = mock.patch(
            "engine.provenance.collect_git_info", return_value=git_info
        )
        self.gpu_patch = mock.patch(
            "engine.provenance.collect_gpu_info", return_value=gpu_info
        )
        self.git_patch.start()
        self.gpu_patch.start()
        self.addCleanup(self.git_patch.stop)
        self.addCleanup(self.gpu_patch.stop)

    def new_simulation(self, run_id: str) -> Simulation:
        return Simulation(
            make_config(run_id),
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            runtime_bindings=RUNTIME_BINDINGS,
        )

    def new_two_agent_simulation(self, run_id: str) -> Simulation:
        config = make_config(run_id)
        config["blocs"][0]["num_agents"] = 2
        simulation = Simulation(
            config,
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            runtime_bindings=RUNTIME_BINDINGS,
        )
        fixed_positions = {0: (-1, 0), 1: (1, 0)}
        for agent in simulation.agents:
            agent.position = fixed_positions[agent.agent_id]
        return simulation

    def assert_rerun_rejected_without_mutation(
        self,
        simulation: Simulation,
    ) -> None:
        output_dir = Path(simulation.output_dir)
        before_hashes = directory_hashes(output_dir)
        before_state = simulation_state(simulation)

        with mock.patch("engine.sim.call_ollama") as llm_mock:
            with self.assertRaisesRegex(
                RunLifecycleError,
                "execution has already been claimed",
            ):
                simulation.run()

        llm_mock.assert_not_called()
        self.assertEqual(directory_hashes(output_dir), before_hashes)
        self.assertEqual(simulation_state(simulation), before_state)

    def test_new_run_is_created_and_completed(self):
        with mock.patch("engine.sim.call_ollama", side_effect=successful_llm):
            simulation = self.new_simulation("new-completed-run")
            output_dir = self.output_root / "output_new-completed-run"
            running_meta = load_meta(output_dir)
            self.assertEqual(running_meta["status"], "running")
            self.assertIsNone(running_meta["end_time_utc"])
            for filename in RAW_JSONL_FILES:
                if filename != "termination.jsonl":
                    self.assertTrue((output_dir / filename).is_file())
            self.assertFalse((output_dir / "termination.jsonl").exists())
            simulation.run()

        self.assertEqual(Path(simulation.output_dir), output_dir)
        self.assertTrue(output_dir.is_dir())
        meta = load_meta(output_dir)
        self.assertEqual(meta["run_id"], "new-completed-run")
        self.assertEqual(meta["status"], "completed")
        self.assertFalse(meta["aborted"])
        self.assertEqual(meta["expected_steps"], 1)
        self.assertEqual(meta["completed_steps"], 1)
        self.assertEqual(meta["expected_agents"], 1)
        self.assertEqual(meta["observed_agents"], 1)

    def test_completed_instance_rejects_rerun_without_mutation(self):
        simulation = self.new_simulation("completed-one-shot-run")
        initial_position = simulation.agents[0].position
        actions = {
            0: {
                "action": "move",
                "direction": "right",
                "memory": "persisted-memory",
                "reasoning": "",
            },
        }
        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=ScriptedLLM(actions),
        ):
            simulation.run()

        self.assertEqual(
            simulation.agents[0].position,
            simulation.world.clamp(initial_position[0] + 1, initial_position[1]),
        )
        self.assertEqual(list(simulation.agents[0].memories), ["persisted-memory"])
        self.assert_rerun_rejected_without_mutation(simulation)

        from tools.validate_run import validate_run

        report = validate_run(Path(simulation.output_dir), strict=True)
        self.assertTrue(report.valid, report.errors)

    def test_aborted_instance_rejects_rerun_without_mutation(self):
        simulation = self.new_simulation("aborted-one-shot-run")
        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=LLMTransportError("synthetic transport failure"),
        ):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()

        self.assertEqual(
            load_meta(Path(simulation.output_dir))["status"],
            "aborted",
        )
        self.assert_rerun_rejected_without_mutation(simulation)

    def test_failed_instance_rejects_rerun_without_mutation(self):
        simulation = self.new_simulation("failed-one-shot-run")
        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                simulation.run()

        self.assertEqual(
            load_meta(Path(simulation.output_dir))["status"],
            "failed",
        )
        self.assert_rerun_rejected_without_mutation(simulation)

    def test_concurrent_run_calls_have_exactly_one_execution_owner(self):
        simulation = self.new_simulation("concurrent-one-shot-run")
        owner_entered_llm = threading.Event()
        release_owner = threading.Event()
        owner_errors = []

        def blocking_llm(**kwargs):
            owner_entered_llm.set()
            if not release_owner.wait(timeout=5):
                raise TimeoutError("test did not release execution owner")
            return successful_llm(**kwargs)

        def run_owner():
            try:
                simulation.run()
            except BaseException as error:
                owner_errors.append(error)

        owner_thread = threading.Thread(target=run_owner)
        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=blocking_llm,
        ) as llm_mock:
            owner_thread.start()
            try:
                self.assertTrue(owner_entered_llm.wait(timeout=5))
                before_hashes = directory_hashes(Path(simulation.output_dir))
                before_state = simulation_state(simulation)

                with self.assertRaisesRegex(
                    RunLifecycleError,
                    "execution has already been claimed",
                ):
                    simulation.run()

                self.assertEqual(
                    directory_hashes(Path(simulation.output_dir)),
                    before_hashes,
                )
                self.assertEqual(simulation_state(simulation), before_state)
            finally:
                release_owner.set()
                owner_thread.join(timeout=5)

        self.assertFalse(owner_thread.is_alive())
        self.assertFalse(owner_errors, owner_errors)
        self.assertEqual(llm_mock.call_count, 2)
        self.assertEqual(
            load_meta(Path(simulation.output_dir))["status"],
            "completed",
        )

    def test_missing_config_run_id_generates_and_persists_one(self):
        config = make_config()
        del config["simulation"]["run_id"]
        with mock.patch("engine.sim.call_ollama", side_effect=successful_llm):
            simulation = Simulation(
                config,
                output_root=self.output_root,
                repo_root=REPO_ROOT,
                runtime_bindings=RUNTIME_BINDINGS,
            )
            simulation.run()

        output_dir = Path(simulation.output_dir)
        meta = load_meta(output_dir)
        self.assertEqual(meta["run_id"], simulation.run_id)
        self.assertEqual(output_dir.name, f"output_{simulation.run_id}")
        self.assertNotIn("run_id", meta["config"]["simulation"])

    def test_failed_completed_meta_replace_never_reports_completed(self):
        simulation = self.new_simulation("atomic-finalize-failure")

        from engine import provenance

        real_atomic_write = provenance.atomic_write_json

        def fail_completed(path, value):
            if value.get("status") == "completed":
                raise OSError("synthetic atomic replace failure")
            return real_atomic_write(path, value)

        with (
            mock.patch("engine.sim.call_ollama", side_effect=successful_llm),
            mock.patch(
                "engine.provenance.atomic_write_json",
                side_effect=fail_completed,
            ),
        ):
            with self.assertRaisesRegex(OSError, "atomic replace"):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertNotEqual(meta["status"], "completed")

    def test_missing_required_raw_file_cannot_complete(self):
        simulation = self.new_simulation("missing-raw-finalize")
        (Path(simulation.output_dir) / "messages.jsonl").unlink()

        with mock.patch("engine.sim.call_ollama", side_effect=successful_llm):
            with self.assertRaisesRegex(RunLifecycleError, "missing required raw"):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])

    def test_manifest_hash_failure_still_persists_failed_meta(self):
        simulation = self.new_simulation("manifest-hash-failure")

        with (
            mock.patch("engine.sim.call_ollama", side_effect=successful_llm),
            mock.patch(
                "engine.provenance.build_raw_manifest",
                side_effect=OSError("synthetic hash failure"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "synthetic hash failure"):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertIsNone(meta["raw_manifest"])
        self.assertEqual(meta["raw_manifest_status"], "unavailable")
        self.assertEqual(meta["raw_manifest_error"], "raw_manifest_hash_failed")

    def test_collision_happens_before_llm_and_preserves_first_run(self):
        config = make_config("fixed-collision-run")
        with mock.patch("engine.sim.call_ollama", side_effect=successful_llm):
            first = Simulation(
                config,
                output_root=self.output_root,
                repo_root=REPO_ROOT,
                runtime_bindings=RUNTIME_BINDINGS,
            )
            first.run()

        output_dir = self.output_root / "output_fixed-collision-run"
        before = directory_hashes(output_dir)

        def simulation_in_test_output(loaded_config, **_options):
            return Simulation(
                loaded_config,
                output_root=self.output_root,
                repo_root=REPO_ROOT,
                runtime_bindings=RUNTIME_BINDINGS,
            )

        with (
            mock.patch.object(cli_main, "load_config", return_value=config),
            mock.patch.object(
                cli_main,
                "load_runtime_bindings",
                return_value=RUNTIME_BINDINGS,
            ),
            mock.patch.object(
                cli_main,
                "Simulation",
                side_effect=simulation_in_test_output,
            ),
            mock.patch("engine.sim.call_ollama") as llm_mock,
        ):
            exit_code = cli_main.main(CLI_ARGS)

        self.assertEqual(exit_code, 2)
        llm_mock.assert_not_called()

        self.assertEqual(directory_hashes(output_dir), before)

    def test_concurrent_processes_claim_same_run_id_exactly_once(self):
        context = multiprocessing.get_context("spawn")
        start_barrier = context.Barrier(3)
        result_queue = context.Queue()
        config = make_config("parallel-collision-run")
        processes = [
            context.Process(
                target=claim_run_directory_in_process,
                args=(
                    config,
                    str(self.output_root),
                    str(REPO_ROOT),
                    start_barrier,
                    result_queue,
                ),
            )
            for _ in range(2)
        ]

        try:
            for process in processes:
                process.start()
            start_barrier.wait(timeout=10)
            for process in processes:
                process.join(timeout=10)

            self.assertTrue(
                all(not process.is_alive() for process in processes),
                "run-directory claim process did not terminate",
            )
            self.assertTrue(
                all(process.exitcode == 0 for process in processes),
                [process.exitcode for process in processes],
            )
            outcomes = [result_queue.get(timeout=5) for _ in processes]
            self.assertCountEqual(
                outcomes,
                [("created", 0), ("collision", None)],
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()

    def test_transport_abort_leaves_aborted_meta(self):
        simulation = self.new_simulation("transport-abort-run")

        def transport_failure(**kwargs):
            telemetry = kwargs.get("telemetry")
            for _ in range(3):
                telemetry("http_attempt", 1)
                telemetry("transport_failure", 1)
            raise LLMTransportError("synthetic transport failure")

        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=transport_failure,
        ):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "aborted")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["abort_reason"], "transport_failure")
        self.assertEqual(meta["failure_step"], 1)
        self.assertEqual(meta["failure_phase"], "phase1")
        self.assertEqual(meta["failure_agent_id"], 0)
        self.assertEqual(meta["completed_steps"], 0)
        self.assertEqual(meta["logical_llm_calls"], 1)
        self.assertEqual(meta["http_attempts"], 3)
        self.assertEqual(meta["transport_failures"], 3)

    def test_phase3_decisions_all_observe_pre_movement_positions(self):
        simulation = self.new_two_agent_simulation("phase3-shared-snapshot")
        initial_positions = simulation._get_positions()
        observed_positions = []
        actions = {
            0: {
                "action": "move",
                "direction": "right",
                "memory": "agent-zero",
                "reasoning": "",
            },
            1: {
                "action": "move",
                "direction": "up",
                "memory": "agent-one",
                "reasoning": "",
            },
        }

        scripted = ScriptedLLM(
            actions,
            before_phase3=lambda _agent_id: observed_positions.append(
                simulation._get_positions()
            ),
        )
        with mock.patch("engine.sim.call_ollama", side_effect=scripted):
            simulation.run()

        self.assertEqual(
            observed_positions,
            [initial_positions, initial_positions],
        )
        self.assertEqual(
            simulation._get_positions(),
            {0: (0, 0), 1: (1, 1)},
        )

    def test_phase4_scripted_results_are_iteration_order_invariant(self):
        forward = self.new_two_agent_simulation("phase4-forward-order")
        reversed_order = self.new_two_agent_simulation("phase4-reversed-order")
        reversed_order.agents.reverse()
        actions = {
            0: {
                "action": "move",
                "direction": "right",
                "memory": "agent-zero",
                "reasoning": "zero-reasoning",
            },
            1: {
                "action": "move",
                "direction": "up",
                "memory": "agent-one",
                "reasoning": "one-reasoning",
            },
        }

        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=ScriptedLLM(actions),
        ):
            forward.run()
        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=ScriptedLLM(actions),
        ):
            reversed_order.run()

        def state_by_agent(simulation):
            return {
                agent.agent_id: {
                    "position": agent.position,
                    "memories": list(agent.memories),
                    "received_messages": list(agent.received_messages),
                }
                for agent in simulation.agents
            }

        self.assertEqual(state_by_agent(forward), state_by_agent(reversed_order))
        for filename in ("phase1_raw.jsonl", "memory_reasoning.jsonl"):
            forward_records = sorted(
                read_jsonl(Path(forward.output_dir) / filename),
                key=lambda record: (record["step"], record["agent_id"]),
            )
            reversed_records = sorted(
                read_jsonl(Path(reversed_order.output_dir) / filename),
                key=lambda record: (record["step"], record["agent_id"]),
            )
            self.assertEqual(forward_records, reversed_records)

    def test_phase3_transport_failure_applies_no_pending_movements(self):
        simulation = self.new_two_agent_simulation("phase3-transport-barrier")
        initial_positions = simulation._get_positions()
        actions = {
            0: {
                "action": "move",
                "direction": "right",
                "memory": "pending-move",
                "reasoning": "",
            },
            1: {
                "action": "stay",
                "direction": "",
                "memory": "",
                "reasoning": "",
            },
        }

        with mock.patch(
            "engine.sim.call_ollama",
            side_effect=ScriptedLLM(
                actions,
                transport_failure_agents={1},
            ),
        ):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()

        self.assertEqual(simulation._get_positions(), initial_positions)
        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual(meta["failure_phase"], "phase3")
        self.assertEqual(meta["failure_agent_id"], 1)
        self.assertEqual(meta["completed_steps"], 0)

    def test_phase3_parse_failure_aborts_without_partial_movement(self):
        simulation = self.new_two_agent_simulation("phase3-parse-barrier")
        initial_positions = simulation._get_positions()
        observed_positions = []
        actions = {
            0: {
                "action": "move",
                "direction": "right",
                "memory": "completed-move",
                "reasoning": "",
            },
        }
        scripted = ScriptedLLM(
            actions,
            before_phase3=lambda _agent_id: observed_positions.append(
                simulation._get_positions()
            ),
            parse_failure_agents={1},
        )

        with mock.patch("engine.sim.call_ollama", side_effect=scripted):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()

        self.assertEqual(
            observed_positions,
            [initial_positions, initial_positions],
        )
        self.assertEqual(
            simulation._get_positions(),
            initial_positions,
        )
        parse_errors = read_jsonl(
            Path(simulation.output_dir) / "parse_errors.jsonl"
        )
        self.assertEqual(
            parse_errors,
            [{
                "step": 1,
                "agent_id": 1,
                "phase": 3,
                "raw_output": "not-json",
            }],
        )
        self.assertEqual(
            read_jsonl(Path(simulation.output_dir) / "memory_reasoning.jsonl"),
            [],
        )
        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual(meta["failure_phase"], "phase3")
        self.assertEqual(meta["failure_agent_id"], 1)
        self.assertEqual(meta["syntax_parse_failures"], 1)

    def test_unhandled_exception_leaves_failed_meta_and_is_reraised(self):
        simulation = self.new_simulation("unexpected-failure-run")
        with mock.patch(
            "engine.sim.call_ollama", side_effect=RuntimeError("synthetic bug")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic bug"):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["abort_reason"], "unhandled_exception")
        self.assertEqual(meta["failure_exception_type"], "RuntimeError")
        self.assertEqual(meta["failure_step"], 1)
        self.assertEqual(meta["failure_phase"], "phase1")
        self.assertEqual(meta["failure_agent_id"], 0)

    def test_startup_output_exception_leaves_failed_meta(self):
        simulation = self.new_simulation("stdout-failure-run")
        with mock.patch("builtins.print", side_effect=OSError("closed pipe")):
            with self.assertRaisesRegex(OSError, "closed pipe"):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["abort_reason"], "unhandled_exception")
        self.assertEqual(meta["failure_exception_type"], "OSError")

    def test_zero_bloc_run_is_rejected_before_output_creation(self):
        config = make_config("zero-bloc-run")
        config["blocs"] = []

        with self.assertRaises(ValueError):
            Simulation(
                config,
                output_root=self.output_root,
                repo_root=REPO_ROOT,
                runtime_bindings=RUNTIME_BINDINGS,
            )

        self.assertFalse((self.output_root / "output_zero-bloc-run").exists())

    def test_keyboard_interrupt_leaves_aborted_meta(self):
        simulation = self.new_simulation("keyboard-interrupt-run")
        with mock.patch("engine.sim.call_ollama", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                simulation.run()

        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "aborted")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["abort_reason"], "keyboard_interrupt")
        self.assertEqual(meta["failure_exception_type"], "KeyboardInterrupt")
        self.assertEqual(meta["failure_step"], 1)
        self.assertEqual(meta["failure_phase"], "phase1")
        self.assertEqual(meta["failure_agent_id"], 0)

    def test_mkdir_interrupt_after_creation_leaves_aborted_meta(self):
        config = make_config("mkdir-interrupt-run")
        output_dir = self.output_root / "output_mkdir-interrupt-run"
        real_mkdir = Path.mkdir

        def create_then_interrupt(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(
            Path,
            "mkdir",
            autospec=True,
            side_effect=create_then_interrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                Simulation(
                    config,
                    output_root=self.output_root,
                    repo_root=REPO_ROOT,
                    runtime_bindings=RUNTIME_BINDINGS,
                )

        meta = load_meta(output_dir)
        self.assertEqual(meta["status"], "aborted")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["abort_reason"], "keyboard_interrupt")
        self.assertEqual(meta["failure_exception_type"], "KeyboardInterrupt")

    def test_mkdir_system_exit_after_creation_leaves_failed_meta(self):
        config = make_config("mkdir-system-exit-run")
        output_dir = self.output_root / "output_mkdir-system-exit-run"
        real_mkdir = Path.mkdir

        def create_then_exit(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            raise SystemExit(0)

        with mock.patch.object(
            Path,
            "mkdir",
            autospec=True,
            side_effect=create_then_exit,
        ):
            with self.assertRaises(SystemExit) as raised:
                Simulation(
                    config,
                    output_root=self.output_root,
                    repo_root=REPO_ROOT,
                    runtime_bindings=RUNTIME_BINDINGS,
                )

        self.assertEqual(raised.exception.code, 0)
        meta = load_meta(output_dir)
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertEqual(
            meta["abort_reason"], "run_directory_creation_failure"
        )
        self.assertEqual(meta["failure_exception_type"], "SystemExit")

    def test_system_exit_zero_leaves_failed_meta(self):
        simulation = self.new_simulation("system-exit-zero-run")
        with mock.patch("engine.sim.call_ollama", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit) as raised:
                simulation.run()

        self.assertEqual(raised.exception.code, 0)
        meta = load_meta(Path(simulation.output_dir))
        self.assertEqual(meta["status"], "failed")
        self.assertTrue(meta["aborted"])
        self.assertEqual(meta["failure_exception_type"], "SystemExit")


class CliExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.binding_patch = mock.patch.object(
            cli_main,
            "load_runtime_bindings",
            return_value=RUNTIME_BINDINGS,
        )
        self.binding_patch.start()
        self.addCleanup(self.binding_patch.stop)

    def test_output_root_is_forwarded_with_repository_root(self):
        simulation = mock.Mock()
        simulation.run_lifecycle = SimpleNamespace(
            meta={"status": "completed", "aborted": False}
        )
        config = make_config()
        with tempfile.TemporaryDirectory() as temp_dir:
            output_root = Path(temp_dir)
            with mock.patch.object(cli_main, "load_config", return_value=config), mock.patch.object(
                cli_main, "Simulation", return_value=simulation
            ) as simulation_type:
                self.assertEqual(
                    cli_main.main([
                        "--config",
                        "ignored.yaml",
                        "--runtime-bindings",
                        "bindings.yaml",
                        "--output-root",
                        str(output_root),
                    ]),
                    0,
                )
        simulation_type.assert_called_once_with(
            config,
            output_root=output_root,
            repo_root=Path(cli_main.__file__).resolve().parent,
            runtime_bindings=RUNTIME_BINDINGS,
        )

    def test_invalid_config_returns_two(self):
        with mock.patch.object(
            cli_main, "load_config", side_effect=ValueError("invalid")
        ):
            self.assertEqual(cli_main.main(CLI_ARGS), 2)

    def test_invalid_yaml_returns_two(self):
        with mock.patch.object(
            cli_main, "load_config", side_effect=yaml.YAMLError("invalid yaml")
        ):
            self.assertEqual(cli_main.main(CLI_ARGS), 2)

    def test_invalid_run_id_returns_two_before_llm(self):
        with mock.patch.object(
            cli_main, "load_config", return_value=make_config("../escape")
        ):
            with mock.patch("engine.sim.call_ollama") as llm_mock:
                self.assertEqual(cli_main.main(CLI_ARGS), 2)
                llm_mock.assert_not_called()

    def test_collision_returns_two(self):
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(
                cli_main,
                "Simulation",
                side_effect=RunCollisionError("collision"),
            ):
                self.assertEqual(cli_main.main(CLI_ARGS), 2)

    def test_controlled_transport_abort_returns_one(self):
        simulation = mock.Mock()
        simulation.run.side_effect = SimulationAbortedError("transport abort")
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", return_value=simulation):
                self.assertEqual(cli_main.main(CLI_ARGS), 1)

    def test_keyboard_interrupt_returns_130(self):
        simulation = mock.Mock()
        simulation.run.side_effect = KeyboardInterrupt
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", return_value=simulation):
                self.assertEqual(cli_main.main(CLI_ARGS), 130)

    def test_system_exit_zero_during_run_returns_nonzero(self):
        simulation = mock.Mock()
        simulation.run.side_effect = SystemExit(0)
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", return_value=simulation):
                self.assertEqual(cli_main.main(CLI_ARGS), 1)

    def test_system_exit_zero_during_start_returns_nonzero(self):
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", side_effect=SystemExit(0)):
                self.assertEqual(cli_main.main(CLI_ARGS), 1)

    def test_unhandled_exception_is_reraised(self):
        simulation = mock.Mock()
        simulation.run.side_effect = RuntimeError("unexpected")
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", return_value=simulation):
                with self.assertRaisesRegex(RuntimeError, "unexpected"):
                    cli_main.main(CLI_ARGS)

    def test_success_requires_completed_meta(self):
        completed = mock.Mock()
        completed.run_lifecycle = SimpleNamespace(
            meta={"status": "completed", "aborted": False}
        )
        incomplete = mock.Mock()
        incomplete.run_lifecycle = SimpleNamespace(
            meta={"status": "running", "aborted": False}
        )
        contradictory = mock.Mock()
        contradictory.run_lifecycle = SimpleNamespace(
            meta={"status": "completed", "aborted": True}
        )
        with mock.patch.object(cli_main, "load_config", return_value=make_config()):
            with mock.patch.object(cli_main, "Simulation", return_value=completed):
                self.assertEqual(cli_main.main(CLI_ARGS), 0)
            with mock.patch.object(cli_main, "Simulation", return_value=incomplete):
                self.assertEqual(cli_main.main(CLI_ARGS), 1)
            with mock.patch.object(
                cli_main, "Simulation", return_value=contradictory
            ):
                self.assertEqual(cli_main.main(CLI_ARGS), 1)


if __name__ == "__main__":
    unittest.main()
