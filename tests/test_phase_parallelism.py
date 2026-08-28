import copy
import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

from engine.config import (
    ENDPOINT_ASSIGNMENT_POLICY,
    build_effective_config,
)
from engine.llm_client import LLMTransportError
from engine.parallel_transport import (
    LLMRequest,
    THREAD_NAME_PREFIX,
    execute_llm_batch,
)
from engine.provenance import (
    RAW_JSONL_FILES,
    collect_bloc_models,
    compute_config_hash,
)
from engine.sim import Simulation, SimulationAbortedError


REPO_ROOT = Path(__file__).resolve().parents[1]
COUNTER_FIELDS = (
    "logical_llm_calls",
    "http_attempts",
    "generation_retries",
    "transport_failures",
    "syntax_parse_attempt_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
    "completed_steps",
    "observed_agents",
)


def make_config(
    run_id: str,
    *,
    concurrency=1,
    num_agents: int = 3,
    duration: int = 1,
) -> dict:
    return {
        "simulation": {
            "duration": duration,
            "half_space_size": 4,
            "seed": 31415,
            "run_name": "phase_parallelism_fixture",
            "run_id": run_id,
            "protocol_version": "phase-parallelism-test-v1",
            "metric_version": "metric-v2.0.0",
        },
        "blocs": [{
            "name": "alpha",
            "model": "scripted-model",
            "endpoint_id": "scripted-endpoint",
            "num_agents": num_agents,
            "llm_overrides": {"fixture": ["owned"]},
        }],
        "agents": {
            "communication_radius": 20,
            "memory_limit": 8,
            "memory_size": 8,
            "message_history_limit": 20,
            "message_context_size": 20,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 64,
            "timeout_s": 1,
            "max_concurrency": concurrency,
        },
    }


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def request(agent_id: int, phase: str = "phase1") -> LLMRequest:
    return LLMRequest(
        request_id=f"step-000001:{phase}:agent-{agent_id:06d}",
        step=1,
        phase=phase,
        agent_id=agent_id,
        model="scripted",
        base_url="http://127.0.0.1:1",
        prompt=f"agent {agent_id}",
        temperature=0.0,
        max_tokens=8,
        timeout_s=1,
        llm_overrides={"nested": [agent_id]},
    )


class DeterministicTransport:
    """CPU-only response fixture keyed solely by the immutable request."""

    def __init__(self, phase1_parse=(), phase3_parse=()):
        self.phase1_parse = set(phase1_parse)
        self.phase3_parse = set(phase3_parse)
        self.transcript = []
        self._lock = threading.Lock()

    def __call__(self, item: LLMRequest, telemetry):
        with self._lock:
            self.transcript.append({
                "step": item.step,
                "phase": item.phase,
                "agent_id": item.agent_id,
                "request_id": item.request_id,
                "provider": item.provider,
                "prompt": item.prompt,
                "model": item.model,
                "base_url": item.base_url,
                "temperature": item.temperature,
                "max_tokens": item.max_tokens,
                "timeout_s": item.timeout_s,
                "llm_overrides": copy.deepcopy(item.llm_overrides),
            })
        telemetry("http_attempt", 1)
        parse_agents = (
            self.phase1_parse if item.phase == "phase1" else self.phase3_parse
        )
        if item.agent_id in parse_agents:
            telemetry("syntax_parse_attempt_failure", 1)
            return None, f"not-json:{item.step}:{item.phase}:{item.agent_id}"
        if item.phase == "phase1":
            parsed = {
                "message": f"message-{item.step}-{item.agent_id}",
                "reasoning": f"phase1-reason-{item.step}-{item.agent_id}",
            }
        else:
            directions = ("right", "up", "left")
            parsed = {
                "action": "move",
                "direction": directions[item.agent_id % len(directions)],
                "memory": f"memory-{item.step}-{item.agent_id}",
                "reasoning": f"phase3-reason-{item.step}-{item.agent_id}",
            }
        return parsed, json.dumps(parsed, sort_keys=True)


class PhaseParallelismTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.output_root = Path(self.temp_directory.name)
        git_info = {
            "git_sha": "2" * 40,
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
        for patcher in (
            mock.patch("engine.provenance.collect_git_info", return_value=git_info),
            mock.patch("engine.provenance.collect_gpu_info", return_value=gpu_info),
            mock.patch(
                "engine.llm_client.requests.post",
                side_effect=AssertionError("real network is forbidden in Gate 2 tests"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def new_simulation(
        self,
        run_id: str,
        *,
        concurrency=1,
        num_agents=3,
        duration=1,
        transport=None,
        output_root=None,
    ) -> Simulation:
        simulation = Simulation(
            make_config(
                run_id,
                concurrency=concurrency,
                num_agents=num_agents,
                duration=duration,
            ),
            output_root=output_root or self.output_root,
            repo_root=REPO_ROOT,
            transport=transport or DeterministicTransport(),
        )
        fixed = [(-2, 0), (0, 0), (2, 0), (0, 2), (0, -2)]
        for agent in simulation.agents:
            agent.position = fixed[agent.agent_id]
        return simulation

    @staticmethod
    def run_quietly(simulation: Simulation) -> None:
        with mock.patch("builtins.print"):
            simulation.run()

    def assert_no_gate2_threads(self):
        self.assertEqual(
            [
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith(THREAD_NAME_PREFIX)
            ],
            [],
        )

    def test_config_default_explicit_values_and_input_ownership(self):
        omitted = make_config("omitted")
        del omitted["llm_defaults"]["max_concurrency"]
        original = copy.deepcopy(omitted)
        self.assertEqual(
            build_effective_config(omitted)["llm_defaults"]["max_concurrency"],
            1,
        )
        self.assertEqual(omitted, original)
        for value in (1, 4):
            configured = make_config(f"explicit-{value}", concurrency=value)
            effective = build_effective_config(configured)
            self.assertEqual(effective["llm_defaults"]["max_concurrency"], value)
            self.assertIsNot(effective, configured)

    def test_invalid_concurrency_values_are_rejected(self):
        for value in (0, -1, True, False, 1.5, "3", None):
            with self.subTest(value=value):
                config = make_config("invalid", concurrency=value)
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    build_effective_config(config)

    @staticmethod
    def add_endpoint_pool(config: dict) -> dict:
        bloc = config["blocs"][0]
        bloc.pop("endpoint_id")
        bloc["endpoint_assignment_policy"] = ENDPOINT_ASSIGNMENT_POLICY
        bloc["endpoint_pool"] = [
            {
                "endpoint_id": "alpha-a",
                "device_slot": "slot-alpha",
            },
            {
                "endpoint_id": "alpha-b",
                "device_slot": "slot-beta",
            },
            {
                "endpoint_id": "alpha-c",
                "device_slot": "slot-gamma",
            },
        ]
        return config

    def test_endpoint_pool_round_robin_assignment_is_deterministic(self):
        config = self.add_endpoint_pool(
            make_config("endpoint-pool", concurrency=5, num_agents=5)
        )
        transport = DeterministicTransport()
        simulation = Simulation(
            config,
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            transport=transport,
            runtime_bindings={
                "alpha-a": {"base_url": "http://127.0.0.1:8000"},
                "alpha-b": {"base_url": "http://127.0.0.1:8001"},
                "alpha-c": {"base_url": "http://127.0.0.1:8002"},
            },
        )
        self.assertEqual(
            [agent.endpoint_id for agent in simulation.agents],
            ["alpha-a", "alpha-b", "alpha-c", "alpha-a", "alpha-b"],
        )
        self.assertEqual(
            [agent.base_url for agent in simulation.agents],
            [
                "http://127.0.0.1:8000",
                "http://127.0.0.1:8001",
                "http://127.0.0.1:8002",
                "http://127.0.0.1:8000",
                "http://127.0.0.1:8001",
            ],
        )
        self.run_quietly(simulation)
        for phase in ("phase1", "phase3"):
            observed = {
                row["agent_id"]: row["base_url"]
                for row in transport.transcript
                if row["phase"] == phase
            }
            self.assertEqual(
                observed,
                {
                    0: "http://127.0.0.1:8000",
                    1: "http://127.0.0.1:8001",
                    2: "http://127.0.0.1:8002",
                    3: "http://127.0.0.1:8000",
                    4: "http://127.0.0.1:8001",
                },
            )

        models = collect_bloc_models(simulation.config)
        self.assertEqual(len(models), 3)
        self.assertEqual(
            [model["endpoint_id"] for model in models],
            ["alpha-a", "alpha-b", "alpha-c"],
        )
        self.assertEqual(
            [model["device_slot"] for model in models],
            ["slot-alpha", "slot-beta", "slot-gamma"],
        )
        self.assertTrue(
            all(
                model["endpoint_assignment_policy"]
                == ENDPOINT_ASSIGNMENT_POLICY
                for model in models
            )
        )

    def test_endpoint_pool_invalid_or_ambiguous_config_is_rejected(self):
        cases = []

        empty = self.add_endpoint_pool(make_config("empty-pool"))
        empty["blocs"][0]["endpoint_pool"] = []
        cases.append((empty, "non-empty array"))

        wrong_policy = self.add_endpoint_pool(make_config("wrong-policy"))
        wrong_policy["blocs"][0]["endpoint_assignment_policy"] = "random"
        cases.append((wrong_policy, "must be exactly"))

        duplicate_id = self.add_endpoint_pool(make_config("duplicate-id"))
        duplicate_id["blocs"][0]["endpoint_pool"][1]["endpoint_id"] = "alpha-a"
        cases.append((duplicate_id, "endpoint_id must be unique"))

        duplicate_slot = self.add_endpoint_pool(make_config("duplicate-slot"))
        duplicate_slot["blocs"][0]["endpoint_pool"][1]["device_slot"] = (
            "slot-alpha"
        )
        cases.append((duplicate_slot, "device_slot must be unique"))

        duplicated_identity = self.add_endpoint_pool(
            make_config("duplicated-identity")
        )
        duplicated_identity["blocs"][0]["endpoint_id"] = "alpha-a"
        cases.append((duplicated_identity, "must not duplicate endpoint identity"))

        for config, message in cases:
            with self.subTest(run_id=config["simulation"]["run_id"]):
                with self.assertRaisesRegex(ValueError, message):
                    build_effective_config(config)

    def test_effective_concurrency_is_persisted_and_hashed(self):
        config = make_config("effective-config")
        del config["llm_defaults"]["max_concurrency"]
        simulation = Simulation(
            config,
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            transport=DeterministicTransport(),
        )
        saved = simulation.run_lifecycle.meta["config"]
        self.assertEqual(saved["llm_defaults"]["max_concurrency"], 1)
        one = build_effective_config(make_config("same", concurrency=1))
        three = build_effective_config(make_config("same", concurrency=3))
        self.assertNotEqual(compute_config_hash(one), compute_config_hash(three))

    def test_request_owns_overrides_and_batch_rejects_duplicates(self):
        overrides = {"nested": [1]}
        item = request(0)
        source = LLMRequest(
            **{**item.__dict__, "llm_overrides": overrides}
        )
        overrides["nested"].append(2)
        self.assertEqual(source.llm_overrides, {"nested": [1]})
        with self.assertRaisesRegex(ValueError, "request_id"):
            execute_llm_batch([source, source], 2, lambda *_: ({}, "{}"))
        duplicate_agent = LLMRequest(
            **{**request(1).__dict__, "agent_id": 0}
        )
        with self.assertRaisesRegex(ValueError, "agent_id"):
            execute_llm_batch(
                [source, duplicate_agent], 2, lambda *_: ({}, "{}")
            )
        self.assertEqual(execute_llm_batch([], 8, lambda *_: ({}, "{}")), [])

    def test_worker_telemetry_remains_local_until_batch_settles(self):
        local_ready = threading.Event()
        release = threading.Event()

        def blocking_transport(item, telemetry):
            telemetry("http_attempt", 2)
            if item.phase == "phase1":
                local_ready.set()
                self.assertTrue(release.wait(timeout=5))
            parsed = (
                {"message": "", "reasoning": ""}
                if item.phase == "phase1"
                else {
                    "action": "stay", "direction": "", "memory": "",
                    "reasoning": "",
                }
            )
            return parsed, json.dumps(parsed)

        simulation = self.new_simulation(
            "worker-local", num_agents=1, transport=blocking_transport
        )
        before_state = copy.deepcopy((
            simulation.agents[0].position,
            simulation.agents[0].memories,
            simulation.agents[0].received_messages,
        ))
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_thread_error(simulation.run, errors),
            name="fixture-coordinator",
        )
        with mock.patch("builtins.print"):
            thread.start()
            self.assertTrue(local_ready.wait(timeout=5))
            self.assertEqual(simulation.total_llm_calls, 0)
            self.assertEqual(simulation.parse_error_count, 0)
            self.assertEqual(simulation.run_lifecycle.meta["logical_llm_calls"], 0)
            self.assertEqual(simulation.run_lifecycle.meta["http_attempts"], 0)
            self.assertEqual(
                (simulation.agents[0].position, simulation.agents[0].memories,
                 simulation.agents[0].received_messages),
                before_state,
            )
            self.assertEqual(
                (Path(simulation.output_dir) / "phase1_raw.jsonl").read_bytes(),
                b"",
            )
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(simulation.total_llm_calls, 2)
        self.assertEqual(simulation.run_lifecycle.meta["logical_llm_calls"], 2)
        self.assertEqual(simulation.run_lifecycle.meta["http_attempts"], 4)

    def test_coordinator_interrupt_retains_settled_phase_telemetry(self):
        simulation = self.new_simulation(
            "coordinator-interrupt",
            concurrency=1,
            num_agents=3,
            transport=DeterministicTransport(),
        )
        coordinator_id = threading.get_ident()
        real_result = Future.result
        interrupted = False

        def interrupt_first_coordinator_wait(future, *args, **kwargs):
            nonlocal interrupted
            if threading.get_ident() == coordinator_id and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return real_result(future, *args, **kwargs)

        with mock.patch.object(
            Future,
            "result",
            new=interrupt_first_coordinator_wait,
        ), mock.patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                simulation.run()

        meta = simulation.run_lifecycle.meta
        self.assertTrue(interrupted)
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual(meta["abort_reason"], "keyboard_interrupt")
        self.assertEqual(meta["failure_phase"], "step_start")
        self.assertEqual(meta["logical_llm_calls"], 3)
        self.assertEqual(meta["http_attempts"], 3)
        self.assertEqual(meta["completed_steps"], 0)
        self.assertEqual(simulation.total_llm_calls, 3)
        self.assertEqual(
            (Path(simulation.output_dir) / "phase1_raw.jsonl").read_bytes(),
            b"",
        )
        self.assert_no_gate2_threads()

    @staticmethod
    def _capture_thread_error(callable_, errors):
        try:
            callable_()
        except BaseException as error:
            errors.append(error)

    def test_shared_mutations_are_coordinator_only(self):
        coordinator_id = threading.get_ident()
        calls = []
        transport = DeterministicTransport()
        simulation = self.new_simulation(
            "coordinator-only", concurrency=3, transport=transport
        )

        def instrument(obj, name):
            original = getattr(obj, name)

            def wrapper(*args, **kwargs):
                calls.append((name, threading.get_ident()))
                return original(*args, **kwargs)

            setattr(obj, name, wrapper)

        instrument(simulation.run_lifecycle, "increment")
        instrument(simulation.run_lifecycle, "record_llm_telemetry")
        instrument(simulation, "_log_jsonl")
        instrument(simulation, "_apply_movement")
        for agent in simulation.agents:
            instrument(agent, "add_memory")
            instrument(agent, "add_received_message")
        self.run_quietly(simulation)
        self.assertTrue(calls)
        self.assertTrue(all(thread_id == coordinator_id for _, thread_id in calls))
        self.assertFalse(
            any(name == "record_llm_telemetry" for name, _ in calls),
            "workers must receive a local telemetry callback",
        )

    def test_concurrency_bound_and_overlap(self):
        def measured_batch(count, maximum):
            lock = threading.Lock()
            active = 0
            peak = 0
            barrier = threading.Barrier(count) if maximum > 1 else None

            def invoke(_item, _telemetry):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                if barrier is not None:
                    barrier.wait(timeout=5)
                with lock:
                    active -= 1
                return {}, "{}"

            execute_llm_batch([request(i) for i in range(count)], maximum, invoke)
            return peak

        self.assertEqual(measured_batch(3, 1), 1)
        self.assertEqual(measured_batch(3, 3), 3)
        self.assertEqual(measured_batch(2, 8), 2)

    def test_all_prompts_exist_before_each_dispatch(self):
        simulation = self.new_simulation("prompts-first", concurrency=3)
        observed = []
        real_execute = execute_llm_batch

        def inspect_batch(items, maximum, invoke, **kwargs):
            observed.append((items[0].phase, [item.agent_id for item in items]))
            self.assertEqual(len(items), 3)
            self.assertTrue(all(item.prompt for item in items))
            return real_execute(items, maximum, invoke, **kwargs)

        with mock.patch("engine.sim.execute_llm_batch", side_effect=inspect_batch):
            self.run_quietly(simulation)
        self.assertEqual(
            observed,
            [("phase1", [0, 1, 2]), ("phase3", [0, 1, 2])],
        )

    def test_reverse_completion_commits_canonical_order(self):
        barriers = {phase: threading.Barrier(3) for phase in ("phase1", "phase3")}
        gates = {
            phase: {0: threading.Event(), 1: threading.Event()}
            for phase in ("phase1", "phase3")
        }
        completion = []
        lock = threading.Lock()
        base = DeterministicTransport()

        def reverse(item, telemetry):
            barriers[item.phase].wait(timeout=5)
            if item.agent_id < 2:
                self.assertTrue(gates[item.phase][item.agent_id].wait(timeout=5))
            result = base(item, telemetry)
            with lock:
                completion.append((item.phase, item.agent_id))
            if item.agent_id > 0:
                gates[item.phase][item.agent_id - 1].set()
            return result

        simulation = self.new_simulation(
            "reverse-completion", concurrency=3, transport=reverse
        )
        observed = []
        original_observe = simulation.run_lifecycle.observe_agent

        def observe(agent_id):
            observed.append(agent_id)
            original_observe(agent_id)

        simulation.run_lifecycle.observe_agent = observe
        printed = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            simulation.run()
        self.assertEqual(
            completion,
            [("phase1", 2), ("phase1", 1), ("phase1", 0),
             ("phase3", 2), ("phase3", 1), ("phase3", 0)],
        )
        self.assertEqual(observed, [0, 1, 2, 0, 1, 2])
        for filename, identity in (
            ("phase1_raw.jsonl", "agent_id"),
            ("memory_reasoning.jsonl", "agent_id"),
            ("messages.jsonl", "sender_id"),
        ):
            self.assertEqual(
                [row[identity] for row in read_jsonl(Path(simulation.output_dir) / filename)],
                [0, 1, 2],
            )
        progress_ids = [
            int(match.group(1))
            for line in printed
            if (match := re.search(r"Phase 1: Agent (\d+)", line))
        ]
        self.assertEqual(progress_ids, [0, 1, 2])

    def _completed_view(self, simulation: Simulation, transcript) -> dict:
        meta = simulation.run_lifecycle.meta
        return {
            "agents": {
                agent.agent_id: {
                    "position": agent.position,
                    "memories": copy.deepcopy(agent.memories),
                    "received_messages": copy.deepcopy(agent.received_messages),
                }
                for agent in simulation.agents
            },
            "rng": simulation.rng.getstate(),
            "parse_error_count": simulation.parse_error_count,
            "total_llm_calls": simulation.total_llm_calls,
            "counters": {field: meta[field] for field in COUNTER_FIELDS},
            "observed_ids": set(simulation.run_lifecycle._observed_agent_ids),
            "manifest": {
                filename: entry
                for filename, entry in meta["raw_manifest"]["files"].items()
                if filename not in {"llm_attempts.jsonl", "termination.jsonl"}
            },
            "transcript": sorted(
                transcript,
                key=lambda row: (row["step"], row["phase"], row["agent_id"]),
            ),
            "status": meta["status"],
        }

    def _assert_raw_equal(self, first: Simulation, second: Simulation):
        for filename in RAW_JSONL_FILES:
            if filename in {"llm_attempts.jsonl", "termination.jsonl"}:
                continue
            self.assertEqual(
                (Path(first.output_dir) / filename).read_bytes(),
                (Path(second.output_dir) / filename).read_bytes(),
                filename,
            )

    def test_concurrency_one_and_n_are_deterministically_equivalent(self):
        serial_transport = DeterministicTransport()
        parallel_transport = DeterministicTransport()
        serial = self.new_simulation(
            "equivalence-serial", concurrency=1, duration=2,
            transport=serial_transport,
        )
        parallel = self.new_simulation(
            "equivalence-parallel", concurrency=3, duration=2,
            transport=parallel_transport,
        )
        self.run_quietly(serial)
        self.run_quietly(parallel)
        self._assert_raw_equal(serial, parallel)
        self.assertEqual(
            self._completed_view(serial, serial_transport.transcript),
            self._completed_view(parallel, parallel_transport.transcript),
        )

    def test_agent_list_order_is_not_semantic(self):
        first_transport = DeterministicTransport()
        second_transport = DeterministicTransport()
        first = self.new_simulation("agent-order-forward", transport=first_transport)
        second = self.new_simulation(
            "agent-order-reverse", concurrency=3, transport=second_transport
        )
        second.agents.reverse()
        self.run_quietly(first)
        self.run_quietly(second)
        self._assert_raw_equal(first, second)
        self.assertEqual(
            self._completed_view(first, first_transport.transcript),
            self._completed_view(second, second_transport.transcript),
        )
        self.assertEqual(
            [row["receiver_ids"] for row in read_jsonl(Path(second.output_dir) / "messages.jsonl")],
            [[1, 2], [0, 2], [0, 1]],
        )

    def test_parse_failures_are_equivalent_at_concurrency_one_and_n(self):
        first_transport = DeterministicTransport(phase1_parse={1}, phase3_parse={2})
        second_transport = DeterministicTransport(phase1_parse={1}, phase3_parse={2})
        first = self.new_simulation(
            "parse-serial", concurrency=1, transport=first_transport
        )
        second = self.new_simulation(
            "parse-parallel", concurrency=3, transport=second_transport
        )
        for simulation in (first, second):
            with mock.patch("builtins.print"):
                with self.assertRaises(SimulationAbortedError):
                    simulation.run()
        self._assert_raw_equal(first, second)
        for simulation in (first, second):
            meta = simulation.run_lifecycle.meta
            self.assertEqual(meta["status"], "aborted")
            self.assertEqual(meta["abort_reason"], "syntax_parse_failure")
            self.assertEqual(meta["failure_phase"], "phase1")
            self.assertEqual(meta["failure_agent_id"], 1)
        self.assertEqual(
            {field: first.run_lifecycle.meta[field] for field in COUNTER_FIELDS},
            {field: second.run_lifecycle.meta[field] for field in COUNTER_FIELDS},
        )

    def test_phase1_transport_failure_is_phase_atomic_and_settled(self):
        barrier = threading.Barrier(3)
        settled = []
        lock = threading.Lock()

        def failing(item, telemetry):
            barrier.wait(timeout=5)
            telemetry("http_attempt", item.agent_id + 1)
            try:
                if item.agent_id in {0, 2}:
                    telemetry("transport_failure", 1)
                    raise LLMTransportError(f"transport-{item.agent_id}")
                return {"message": "should-not-publish", "reasoning": ""}, "{}"
            finally:
                with lock:
                    settled.append(item.agent_id)

        simulation = self.new_simulation(
            "phase1-atomic", concurrency=3, transport=failing
        )
        initial_positions = simulation._get_positions()
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()
        meta = simulation.run_lifecycle.meta
        self.assertCountEqual(settled, [0, 1, 2])
        self.assertEqual(meta["logical_llm_calls"], 3)
        self.assertEqual(meta["http_attempts"], 6)
        self.assertEqual(meta["transport_failures"], 2)
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual((meta["failure_phase"], meta["failure_agent_id"]), ("phase1", 0))
        self.assertEqual(meta["completed_steps"], 0)
        self.assertEqual(simulation._get_positions(), initial_positions)
        self.assertTrue(all(not agent.received_messages for agent in simulation.agents))
        self.assertEqual((Path(simulation.output_dir) / "phase1_raw.jsonl").read_bytes(), b"")
        self.assertEqual((Path(simulation.output_dir) / "messages.jsonl").read_bytes(), b"")
        self.assert_no_gate2_threads()

    def test_phase3_transport_failure_preserves_only_phase1_and_delivery(self):
        base = DeterministicTransport()
        phase3_barrier = threading.Barrier(3)
        settled = []

        def failing(item, telemetry):
            if item.phase == "phase1":
                return base(item, telemetry)
            phase3_barrier.wait(timeout=5)
            settled.append(item.agent_id)
            telemetry("http_attempt", 1)
            if item.agent_id == 1:
                telemetry("transport_failure", 1)
                raise LLMTransportError("phase3 transport")
            return base(item, lambda *_: None)

        simulation = self.new_simulation(
            "phase3-atomic", concurrency=3, transport=failing
        )
        initial_positions = simulation._get_positions()
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()
        meta = simulation.run_lifecycle.meta
        self.assertCountEqual(settled, [0, 1, 2])
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual((meta["failure_phase"], meta["failure_agent_id"]), ("phase3", 1))
        self.assertEqual(meta["completed_steps"], 0)
        self.assertEqual(len(read_jsonl(Path(simulation.output_dir) / "phase1_raw.jsonl")), 3)
        self.assertEqual(len(read_jsonl(Path(simulation.output_dir) / "messages.jsonl")), 3)
        self.assertEqual((Path(simulation.output_dir) / "memory_reasoning.jsonl").read_bytes(), b"")
        self.assertEqual(simulation._get_positions(), initial_positions)
        self.assertTrue(all(not agent.memories for agent in simulation.agents))
        self.assertTrue(all(agent.received_messages for agent in simulation.agents))

    def test_multiple_transport_failures_select_minimum_agent_for_one_and_n(self):
        for concurrency in (1, 3):
            with self.subTest(concurrency=concurrency):
                calls = []

                def failing(item, telemetry):
                    calls.append(item.agent_id)
                    telemetry("http_attempt", 1)
                    if item.agent_id in {0, 2}:
                        telemetry("transport_failure", 1)
                        raise LLMTransportError(str(item.agent_id))
                    return {"message": "", "reasoning": ""}, "{}"

                simulation = self.new_simulation(
                    f"multi-transport-{concurrency}",
                    concurrency=concurrency,
                    transport=failing,
                )
                with mock.patch("builtins.print"):
                    with self.assertRaises(SimulationAbortedError):
                        simulation.run()
                self.assertCountEqual(calls, [0, 1, 2])
                self.assertEqual(simulation.run_lifecycle.meta["failure_agent_id"], 0)

    def test_unexpected_error_wins_over_transport_and_is_reraised(self):
        settled = []
        barrier = threading.Barrier(3)

        def mixed(item, telemetry):
            barrier.wait(timeout=5)
            telemetry("http_attempt", 1)
            try:
                if item.agent_id == 0:
                    telemetry("transport_failure", 1)
                    raise LLMTransportError("masked transport")
                if item.agent_id == 2:
                    raise RuntimeError("primary unexpected")
                return {"message": "", "reasoning": ""}, "{}"
            finally:
                settled.append(item.agent_id)

        simulation = self.new_simulation(
            "unexpected-priority", concurrency=3, transport=mixed
        )
        with mock.patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "primary unexpected"):
                simulation.run()
        meta = simulation.run_lifecycle.meta
        self.assertCountEqual(settled, [0, 1, 2])
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["abort_reason"], "unhandled_exception")
        self.assertEqual(meta["failure_exception_type"], "RuntimeError")
        self.assertEqual((meta["failure_phase"], meta["failure_agent_id"]), ("phase1", 2))
        self.assertEqual((Path(simulation.output_dir) / "phase1_raw.jsonl").read_bytes(), b"")
        self.assert_no_gate2_threads()

    def test_multiple_unexpected_errors_select_minimum_unexpected_agent(self):
        calls = []

        def failing(item, telemetry):
            calls.append(item.agent_id)
            telemetry("http_attempt", 1)
            if item.agent_id in {0, 2}:
                raise RuntimeError(f"unexpected-{item.agent_id}")
            return {"message": "", "reasoning": ""}, "{}"

        simulation = self.new_simulation(
            "multi-unexpected", concurrency=3, transport=failing
        )
        with mock.patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "unexpected-0"):
                simulation.run()
        self.assertCountEqual(calls, [0, 1, 2])
        self.assertEqual(simulation.run_lifecycle.meta["failure_agent_id"], 0)
        self.assertEqual(simulation.run_lifecycle.meta["status"], "failed")

    def test_phase3_snapshot_delivery_and_movement_barriers(self):
        phase1_release = threading.Event()
        phase1_blocked = threading.Event()
        phase3_release = threading.Event()
        phase3_peer_done = threading.Event()
        phase3_started = threading.Event()
        base = DeterministicTransport()

        def blocked(item, telemetry):
            if item.phase == "phase1" and item.agent_id == 0:
                phase1_blocked.set()
                self.assertTrue(phase1_release.wait(timeout=5))
            if item.phase == "phase3":
                phase3_started.set()
                if item.agent_id == 0:
                    self.assertTrue(phase3_release.wait(timeout=5))
                elif item.agent_id == 2:
                    phase3_peer_done.set()
            return base(item, telemetry)

        simulation = self.new_simulation(
            "phase-barriers", concurrency=3, transport=blocked
        )
        initial_positions = simulation._get_positions()
        errors = []
        with mock.patch("builtins.print"):
            coordinator = threading.Thread(
                target=lambda: self._capture_thread_error(simulation.run, errors),
                name="fixture-coordinator",
            )
            coordinator.start()
            self.assertTrue(phase1_blocked.wait(timeout=5))
            self.assertTrue(all(not agent.received_messages for agent in simulation.agents))
            self.assertEqual((Path(simulation.output_dir) / "messages.jsonl").read_bytes(), b"")
            self.assertFalse(phase3_started.is_set())
            phase1_release.set()
            self.assertTrue(phase3_peer_done.wait(timeout=5))
            self.assertEqual(simulation._get_positions(), initial_positions)
            self.assertTrue(all(not agent.memories for agent in simulation.agents))
            phase3_release.set()
            coordinator.join(timeout=5)
        self.assertFalse(coordinator.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(all(agent.received_messages for agent in simulation.agents))
        self.assertNotEqual(simulation._get_positions(), initial_positions)

    def test_no_thread_leak_for_normal_parse_transport_and_unexpected(self):
        normal = self.new_simulation("leak-normal", concurrency=3)
        self.run_quietly(normal)
        self.assert_no_gate2_threads()

        parsed = self.new_simulation(
            "leak-parse", concurrency=3,
            transport=DeterministicTransport(phase1_parse={1}),
        )
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationAbortedError):
                parsed.run()
        self.assert_no_gate2_threads()

        for run_id, error in (
            ("leak-transport", LLMTransportError("terminal")),
            ("leak-unexpected", RuntimeError("unexpected")),
        ):
            def fail(_item, _telemetry, raised=error):
                raise raised

            simulation = self.new_simulation(
                run_id, concurrency=3, transport=fail
            )
            with mock.patch("builtins.print"):
                with self.assertRaises((SimulationAbortedError, RuntimeError)):
                    simulation.run()
            self.assert_no_gate2_threads()


if __name__ == "__main__":
    unittest.main()
