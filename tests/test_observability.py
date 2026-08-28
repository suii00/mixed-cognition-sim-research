import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import main as cli_main
from engine.config import load_config
from engine.llm_client import extract_json
from engine.parallel_transport import TransportOutcome
from engine.sim import (
    Simulation,
    SimulationAbortedError,
    SimulationSignalInterrupt,
)
from tools.probe_vllm_json_schema import (
    MAX_NOTE_LENGTH,
    PROBE_SCHEMA_VERSION,
    REQUESTED_NOTE,
    run_probe,
)
from tools.build_vllm_observability_probe_r002 import (
    R002_CONFIG,
    R002_PROTOCOL_VERSION,
    R002_RUN_ID,
    build_r002_bytes,
)
from tools.build_vllm_observability_probe_r003 import (
    R003_CONFIG,
    R003_PROTOCOL_VERSION,
    R003_RUN_ID,
    build_r003_bytes,
)
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_CONFIG_R001 = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_observability_probe_3model_s2300_r001.json"
)
PROBE_CONFIG = R003_CONFIG


def make_config(run_id: str, *, agents: int = 1, concurrency: int = 1) -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 3,
            "seed": 31415,
            "run_name": run_id,
            "run_id": run_id,
            "protocol_version": "engineering-observability-v1.0.0",
            "log_schema_version": "2.0.0",
            "metric_version": "test-metric-v1",
        },
        "blocs": [{
            "name": "test",
            "model": "mock-vllm-model",
            "endpoint_id": "cpu-mock-endpoint",
            "device_slot": "cpu-mock-slot",
            "num_agents": agents,
            "provider": "vllm",
            "llm_overrides": {"response_format": {"type": "json_object"}},
            "backend_version": "test-vllm",
            "chat_template": "test-template",
            "dtype": "float32",
            "generation_config": "vllm",
            "model_digest": "test-digest",
            "model_source": "local-test",
            "quantization": "none",
            "tokenizer_revision": "test-revision",
            "max_model_len": 4096,
            "tensor_parallel_size": 1,
            "data_parallel_size": 1,
        }],
        "agents": {
            "communication_radius": 100,
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 4,
            "message_context_size": 2,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 64,
            "timeout_s": 5,
            "max_concurrency": concurrency,
        },
    }


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def runtime_bindings(config: dict, base_url: str) -> dict:
    return {
        bloc["endpoint_id"]: {"base_url": base_url}
        for bloc in config["blocs"]
    }


def instrumented_outcome(item, parsed, raw_output=None, *, parse_valid=True):
    raw = raw_output if raw_output is not None else json.dumps(parsed)
    envelope = {
        "id": f"chatcmpl-{item.agent_id}",
        "model": item.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": raw},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
        },
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    attempt = {
        "generation_attempt": 1,
        "http_attempt": 1,
        "http_status": 200,
        "http_response_body_base64": base64.b64encode(body).decode("ascii"),
        "http_response_bytes": len(body),
        "http_response_sha256": hashlib.sha256(body).hexdigest(),
        "envelope": envelope,
        "raw_output": raw,
        "finish_reason": "stop",
        "usage": envelope["usage"],
        "transport_status": "ok",
        "parse_status": "valid" if parse_valid else "invalid",
        "schema_status": "not_checked",
        "failure_kind": None if parse_valid else "syntax",
        "error_type": None,
    }
    return TransportOutcome(
        parsed=parsed,
        raw_output=raw,
        attempts=(attempt,),
    )


def valid_response(item):
    if item.phase == "phase1":
        return {"message": f"message-{item.agent_id}", "reasoning": ""}
    return {
        "action": "move",
        "direction": "right",
        "memory": f"memory-{item.agent_id}",
        "reasoning": "",
    }


class ProbeConfigTests(unittest.TestCase):
    def test_direct_probe_cli_imports_without_pythonpath(self):
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "tools" / "probe_vllm_json_schema.py"),
                "--help",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_three_model_probe_is_small_nonresearch_and_schema_2_0(self):
        config = load_config(str(PROBE_CONFIG))
        self.assertEqual(config["simulation"]["duration"], 1)
        self.assertEqual(config["simulation"]["log_schema_version"], "2.0.0")
        self.assertFalse(config["simulation"]["research_eligible"])
        self.assertEqual(sum(bloc["num_agents"] for bloc in config["blocs"]), 3)
        self.assertEqual(config["llm_defaults"]["max_tokens"], 512)
        self.assertEqual(config["llm_defaults"]["temperature"], 0.0)
        self.assertEqual(
            [bloc["endpoint_id"] for bloc in config["blocs"]],
            ["probe-qwen", "probe-llama", "probe-gemma"],
        )
        self.assertTrue(all(
            bloc["llm_overrides"]
            == {"response_format": {"type": "json_object"}}
            for bloc in config["blocs"]
        ))

    def test_r002_is_fresh_and_changes_only_probe_identifiers(self):
        self.assertEqual(R002_CONFIG.read_bytes(), build_r002_bytes())
        r001 = json.loads(PROBE_CONFIG_R001.read_text(encoding="utf-8"))
        r002 = json.loads(R002_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(r002["simulation"]["run_id"], R002_RUN_ID)
        self.assertEqual(
            r002["simulation"]["protocol_version"], R002_PROTOCOL_VERSION
        )
        for config in (r001, r002):
            config["simulation"].pop("protocol_version")
            config["simulation"].pop("run_id")
            config["simulation"].pop("run_name")
        self.assertEqual(r002, r001)

    def test_r003_is_fresh_and_changes_only_probe_identifiers(self):
        self.assertEqual(PROBE_CONFIG.read_bytes(), build_r003_bytes())
        r002 = json.loads(R002_CONFIG.read_text(encoding="utf-8"))
        r003 = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(r003["simulation"]["run_id"], R003_RUN_ID)
        self.assertEqual(
            r003["simulation"]["protocol_version"], R003_PROTOCOL_VERSION
        )
        for config in (r002, r003):
            config["simulation"].pop("protocol_version")
            config["simulation"].pop("run_id")
            config["simulation"].pop("run_name")
        self.assertEqual(r003, r002)

class ObservabilityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.output_root = Path(self.temp_directory.name)

    def new_simulation(self, run_id, transport, *, agents=1, concurrency=1):
        return Simulation(
            make_config(run_id, agents=agents, concurrency=concurrency),
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            transport=transport,
        )

    def test_strict_whole_json_handles_string_braces_and_rejects_wrappers(self):
        exact = '{"message":"literal { brace } and \\\"quote\\\"","reasoning":""}'
        self.assertEqual(
            extract_json(exact),
            {"message": 'literal { brace } and "quote"', "reasoning": ""},
        )
        self.assertIsNone(extract_json("prefix " + exact))
        self.assertIsNone(extract_json(exact + " suffix"))
        self.assertIsNone(extract_json('{"message":"unterminated"'))

    def test_phase1_partial_output_preserved_and_no_delivery_occurs(self):
        initial_positions = None

        def transport(item, telemetry):
            telemetry("http_attempt", 1)
            if item.phase == "phase1" and item.agent_id == 1:
                telemetry("syntax_parse_attempt_failure", 1)
                return instrumented_outcome(
                    item,
                    None,
                    '{"message":"unterminated"',
                    parse_valid=False,
                )
            return instrumented_outcome(item, valid_response(item))

        simulation = self.new_simulation(
            "observe-partial-phase1", transport, agents=2, concurrency=2
        )
        initial_positions = simulation._get_positions()
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()

        self.assertEqual(simulation._get_positions(), initial_positions)
        self.assertTrue(all(not agent.received_messages for agent in simulation.agents))
        attempts = read_jsonl(Path(simulation.output_dir) / "llm_attempts.jsonl")
        self.assertEqual([row["agent_id"] for row in attempts], [0, 1])
        failed = attempts[1]
        self.assertEqual(failed["raw_output"], '{"message":"unterminated"')
        self.assertEqual(failed["parse_status"], "invalid")
        self.assertEqual(failed["schema_status"], "not_reached")
        self.assertEqual(failed["failure_kind"], "syntax")
        meta = simulation.run_lifecycle.meta
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual(meta["abort_reason"], "syntax_parse_failure")
        self.assertEqual(meta["generation_retries"], 0)
        self.assertEqual(meta["syntax_parse_failures"], 1)

    def test_phase3_failure_applies_no_memory_or_movement(self):
        def transport(item, telemetry):
            telemetry("http_attempt", 1)
            if item.phase == "phase3" and item.agent_id == 1:
                telemetry("syntax_parse_attempt_failure", 1)
                return instrumented_outcome(item, None, "not-json", parse_valid=False)
            return instrumented_outcome(item, valid_response(item))

        simulation = self.new_simulation(
            "observe-partial-phase3", transport, agents=2, concurrency=2
        )
        initial_positions = simulation._get_positions()
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationAbortedError):
                simulation.run()
        self.assertEqual(simulation._get_positions(), initial_positions)
        self.assertTrue(all(not agent.memories for agent in simulation.agents))
        self.assertEqual(
            read_jsonl(Path(simulation.output_dir) / "memory_reasoning.jsonl"),
            [],
        )

    def test_missing_field_and_invalid_enum_are_terminal_schema_failures(self):
        invalid_values = (
            ("phase1", {"message": "missing reasoning"}),
            ("phase3", {
                "action": "teleport",
                "direction": "right",
                "memory": "",
                "reasoning": "",
            }),
        )
        for ordinal, (target_phase, invalid) in enumerate(invalid_values):
            with self.subTest(target_phase=target_phase):
                def transport(item, telemetry, phase=target_phase, value=invalid):
                    telemetry("http_attempt", 1)
                    parsed = value if item.phase == phase else valid_response(item)
                    return instrumented_outcome(item, parsed)

                simulation = self.new_simulation(
                    f"observe-schema-{ordinal}", transport
                )
                with mock.patch("builtins.print"):
                    with self.assertRaises(SimulationAbortedError):
                        simulation.run()
                self.assertEqual(
                    simulation.run_lifecycle.meta["abort_reason"],
                    "schema_validation_failure",
                )
                self.assertEqual(
                    simulation.run_lifecycle.meta["schema_validation_failures"],
                    1,
                )
                attempts = read_jsonl(
                    Path(simulation.output_dir) / "llm_attempts.jsonl"
                )
                failed = next(row for row in attempts if row["phase"] == target_phase)
                self.assertEqual(failed["schema_status"], "invalid")
                self.assertEqual(failed["failure_kind"], "schema")

    def test_parallel_attempt_log_is_canonical_and_not_mixed(self):
        barrier = threading.Barrier(3)

        def transport(item, telemetry):
            telemetry("http_attempt", 1)
            if item.phase == "phase1":
                barrier.wait(timeout=5)
                time.sleep((2 - item.agent_id) * 0.01)
            return instrumented_outcome(item, valid_response(item))

        simulation = self.new_simulation(
            "observe-parallel-order", transport, agents=3, concurrency=3
        )
        with mock.patch("builtins.print"):
            simulation.run()
        attempts = read_jsonl(Path(simulation.output_dir) / "llm_attempts.jsonl")
        keys = [(row["step"], row["phase"], row["agent_id"]) for row in attempts]
        self.assertEqual(keys, [
            (1, "phase1", 0),
            (1, "phase1", 1),
            (1, "phase1", 2),
            (1, "phase3", 0),
            (1, "phase3", 1),
            (1, "phase3", 2),
        ])
        self.assertEqual(len({row["event_id"] for row in attempts}), 6)
        for row in attempts:
            self.assertIn(f"agent-{row['agent_id']:06d}", row["request_id"])
            self.assertEqual(row["endpoint_id"], "cpu-mock-endpoint")
            self.assertEqual(row["device_slot"], "cpu-mock-slot")
            self.assertEqual(
                row["raw_output"],
                row["envelope"]["choices"][0]["message"]["content"],
            )

    def test_sigterm_style_interrupt_writes_terminal_record_and_meta(self):
        def interrupted(_item, _telemetry):
            raise SimulationSignalInterrupt("SIGTERM")

        simulation = self.new_simulation("observe-sigterm", interrupted)
        with mock.patch("builtins.print"):
            with self.assertRaises(SimulationSignalInterrupt):
                simulation.run()
        meta = simulation.run_lifecycle.meta
        self.assertEqual(meta["status"], "aborted")
        self.assertEqual(meta["abort_reason"], "signal_sigterm")
        terminal = read_jsonl(
            Path(simulation.output_dir) / "termination.jsonl"
        )
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["reason"], "signal_sigterm")
        self.assertEqual(terminal[0]["status"], "aborted")
        self.assertEqual(terminal[0]["exception_type"], "SimulationSignalInterrupt")

    def test_sigterm_handler_raises_typed_interrupt_and_restores_handler(self):
        previous_handler = object()
        installed = []

        def install(sig, handler):
            installed.append((sig, handler))

        with mock.patch(
            "main.signal.getsignal", return_value=previous_handler
        ), mock.patch("main.signal.signal", side_effect=install):
            with self.assertRaises(SimulationSignalInterrupt) as raised:
                with cli_main._translate_sigterm():
                    installed[-1][1](signal.SIGTERM, None)

        self.assertEqual(raised.exception.abort_reason, "signal_sigterm")
        self.assertEqual(installed[0][0], signal.SIGTERM)
        self.assertIs(installed[-1][1], previous_handler)


class MockVllmHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        prompt = request["messages"][0]["content"]
        if "Decide your next action." in prompt:
            parsed = {
                "action": "stay",
                "direction": "",
                "memory": "cpu-smoke",
                "reasoning": "",
            }
        else:
            parsed = {"message": "cpu-smoke", "reasoning": ""}
        raw = json.dumps(parsed, separators=(",", ":"))
        envelope = {
            "id": "chatcmpl-cpu-smoke",
            "object": "chat.completion",
            "model": request["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": raw},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class MockStructuredOutputHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ignore_max_length = False
    control_note = REQUESTED_NOTE

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        schema_case = request["response_format"]["type"] == "json_schema"
        note = (
            self.control_note
            if not schema_case or self.ignore_max_length
            else REQUESTED_NOTE[:8]
        )
        raw = json.dumps(
            {"status": "ok", "note": note}, separators=(",", ":")
        )
        envelope = {
            "id": "chatcmpl-structured-smoke",
            "object": "chat.completion",
            "model": request["model"],
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": raw},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class CpuMockSmokeTests(unittest.TestCase):
    def test_real_http_vllm_path_completes_and_strict_validates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = ThreadingHTTPServer(("127.0.0.1", 0), MockVllmHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            config = make_config("cpu-vllm-observability-smoke")
            binding = runtime_bindings(
                config, f"http://127.0.0.1:{server.server_address[1]}"
            )
            simulation = Simulation(
                config,
                output_root=Path(temp_dir),
                repo_root=REPO_ROOT,
                runtime_bindings=binding,
            )
            with mock.patch("builtins.print"):
                simulation.run()

            output_dir = Path(simulation.output_dir)
            report = validate_run(output_dir, strict=True)
            self.assertTrue(report.valid, report.errors)
            attempts = read_jsonl(output_dir / "llm_attempts.jsonl")
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(row["finish_reason"] == "stop" for row in attempts))
            self.assertTrue(all(row["usage"]["total_tokens"] == 30 for row in attempts))
            self.assertEqual(
                read_jsonl(output_dir / "termination.jsonl")[0]["status"],
                "completed",
            )

    def test_paired_json_schema_probe_preserves_evidence_for_all_models(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockStructuredOutputHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
            binding = runtime_bindings(
                config, f"http://127.0.0.1:{server.server_port}"
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = root / "probe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_dir = root / "structured-output-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "a" * 40, 5, binding),
                    0,
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "compatibility_attempts.jsonl")
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["schema_version"], PROBE_SCHEMA_VERSION)
            self.assertEqual(meta["completed_http_attempts"], 6)
            self.assertEqual(len(records), 6)
            self.assertEqual(
                [(row["model_name"], row["case"]) for row in records],
                [
                    (model, case)
                    for model in ("qwen", "llama", "gemma")
                    for case in (
                        "json_object_control",
                        "json_schema_max_length",
                    )
                ],
            )
            for row in records:
                self.assertEqual(row["schema_version"], PROBE_SCHEMA_VERSION)
                body = base64.b64decode(row["response_body_base64"])
                self.assertEqual(
                    hashlib.sha256(body).hexdigest(), row["response_sha256"]
                )
                note = row["parsed_output"]["note"]
                self.assertEqual(row["note_length"], len(note))
                self.assertEqual(
                    row["requested_note_exact_match"], note == REQUESTED_NOTE
                )
                self.assertEqual(
                    row["note_exceeds_comparison_max_length"],
                    len(note) > MAX_NOTE_LENGTH,
                )
                if row["case"] == "json_object_control":
                    self.assertGreater(len(note), MAX_NOTE_LENGTH)
                else:
                    self.assertLessEqual(len(note), MAX_NOTE_LENGTH)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_json_schema_probe_accepts_nonidentical_long_control(self):
        class NonidenticalLongControlHandler(MockStructuredOutputHandler):
            control_note = "abcdefghijklmnopqrstuvwxyz"

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), NonidenticalLongControlHandler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
            binding = runtime_bindings(
                config, f"http://127.0.0.1:{server.server_port}"
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = root / "probe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_dir = root / "nonidentical-control-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "c" * 40, 5, binding),
                    0,
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "compatibility_attempts.jsonl")
            self.assertEqual(meta["completed_http_attempts"], 6)
            control = records[0]
            self.assertEqual(control["case"], "json_object_control")
            self.assertFalse(control["requested_note_exact_match"])
            self.assertEqual(control["note_length"], 26)
            self.assertTrue(control["note_exceeds_comparison_max_length"])
            self.assertEqual(control["result"], "pass")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_json_schema_probe_rejects_control_not_longer_than_limit(self):
        class ShortControlHandler(MockStructuredOutputHandler):
            control_note = REQUESTED_NOTE[:MAX_NOTE_LENGTH]

        server = ThreadingHTTPServer(("127.0.0.1", 0), ShortControlHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
            binding = runtime_bindings(
                config, f"http://127.0.0.1:{server.server_port}"
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = root / "probe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_dir = root / "short-control-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "d" * 40, 5, binding),
                    1,
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "compatibility_attempts.jsonl")
            self.assertEqual(meta["completed_http_attempts"], 1)
            self.assertEqual(
                meta["failure_reason"],
                "control_note_not_longer_than_max_length",
            )
            self.assertFalse(records[0]["note_exceeds_comparison_max_length"])
            self.assertEqual(records[0]["result"], "fail")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_json_schema_probe_fails_when_max_length_is_not_enforced(self):
        class IgnoringHandler(MockStructuredOutputHandler):
            ignore_max_length = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), IgnoringHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
            binding = runtime_bindings(
                config, f"http://127.0.0.1:{server.server_port}"
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path = root / "probe.json"
                config_path.write_text(json.dumps(config), encoding="utf-8")
                output_dir = root / "failed-structured-output-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "b" * 40, 5, binding),
                    1,
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "compatibility_attempts.jsonl")
            self.assertEqual(meta["status"], "failed")
            self.assertEqual(meta["failure_reason"], "max_length_not_enforced")
            self.assertEqual(meta["completed_http_attempts"], 2)
            self.assertEqual(records[-1]["result"], "fail")
            self.assertEqual(
                records[-1]["failure_reason"], "max_length_not_enforced"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
