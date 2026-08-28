import base64
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_CONFIG = (
    REPO_ROOT
    / "configs"
    / "engineering_vllm_phase_contract_probe_3model_20260825_r002.json"
)

from engine.config import load_config
from engine.response_contracts import (
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    response_format_for_phase,
    response_schema_sha256,
)
from tools.probe_vllm_phase_contract import (
    PROBE_CASES,
    PROBE_SCHEMA_VERSION,
    run_probe,
)


def read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def contains_key(value, key):
    if isinstance(value, dict):
        return key in value or any(contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


class PhaseContractHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    invalid_stay_direction = False
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        type(self).received.append(request)
        prompt = request["messages"][0]["content"]
        if "action to move" in prompt:
            parsed = {
                "action": "move",
                "direction": "right",
                "memory": "probe-memory",
                "reasoning": "probe-reasoning",
            }
        else:
            parsed = {
                "action": "stay",
                "direction": "right" if self.invalid_stay_direction else None,
                "memory": "probe-memory",
                "reasoning": "probe-reasoning",
            }
        raw = json.dumps(parsed, separators=(",", ":"))
        envelope = {
            "id": "chatcmpl-phase-contract-probe",
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


class PhaseContractProbeTests(unittest.TestCase):
    def setUp(self):
        PhaseContractHandler.received = []
        PhaseContractHandler.invalid_stay_direction = False

    def configured_path(self, root: Path, port: int) -> tuple[Path, dict]:
        config = json.loads(PROBE_CONFIG.read_text(encoding="utf-8"))
        path = root / "probe.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        bindings = {
            bloc["endpoint_id"]: {"base_url": f"http://127.0.0.1:{port}"}
            for bloc in config["blocs"]
        }
        return path, bindings

    def test_frozen_config_selects_prospective_contract_without_overrides(self):
        config = load_config(str(PROBE_CONFIG))
        self.assertEqual(
            config["simulation"]["response_contract_version"],
            CANONICAL_RESPONSE_CONTRACT_VERSION,
        )
        self.assertEqual(config["simulation"]["log_schema_version"], "2.0.0")
        self.assertFalse(config["simulation"]["research_eligible"])
        self.assertTrue(all(bloc["llm_overrides"] == {} for bloc in config["blocs"]))

    def test_probe_exercises_both_oneof_branches_for_all_models(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), PhaseContractHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path, bindings = self.configured_path(
                    root, server.server_port
                )
                output_dir = root / "phase-contract-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "a" * 40, 5, bindings), 0
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                termination = json.loads(
                    (output_dir / "termination.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "phase_contract_attempts.jsonl")
            self.assertEqual(meta["status"], "completed")
            self.assertEqual(meta["schema_version"], PROBE_SCHEMA_VERSION)
            self.assertEqual(meta["completed_http_attempts"], 6)
            self.assertEqual(meta["attempts_lines"], 6)
            self.assertEqual(termination["status"], "completed")
            self.assertEqual(termination["attempts_sha256"], meta["attempts_sha256"])
            self.assertEqual(
                meta["response_schema_sha256"],
                response_schema_sha256(CANONICAL_RESPONSE_CONTRACT_VERSION),
            )
            self.assertEqual(
                [(row["model_name"], row["case"]) for row in records],
                [
                    (model, case)
                    for model in ("qwen", "llama", "gemma")
                    for case in PROBE_CASES
                ],
            )
            expected_format = response_format_for_phase(
                CANONICAL_RESPONSE_CONTRACT_VERSION,
                "phase3",
            )
            self.assertEqual(len(PhaseContractHandler.received), 6)
            for request, row in zip(PhaseContractHandler.received, records):
                self.assertEqual(request["response_format"], expected_format)
                self.assertFalse(contains_key(request["response_format"], "maxLength"))
                self.assertEqual(row["result"], "pass")
                self.assertEqual(row["finish_reason"], "stop")
                self.assertEqual(row["usage"]["total_tokens"], 30)
                self.assertEqual(
                    row["parsed_output"]["action"], row["expected_action"]
                )
                self.assertEqual(
                    row["parsed_output"]["direction"], row["expected_direction"]
                )
                request_body = base64.b64decode(row["request_body_base64"])
                response_body = base64.b64decode(row["response_body_base64"])
                self.assertEqual(
                    hashlib.sha256(request_body).hexdigest(), row["request_sha256"]
                )
                self.assertEqual(
                    hashlib.sha256(response_body).hexdigest(), row["response_sha256"]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_probe_stops_and_preserves_invalid_cross_field_pair(self):
        PhaseContractHandler.invalid_stay_direction = True
        server = ThreadingHTTPServer(("127.0.0.1", 0), PhaseContractHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                config_path, bindings = self.configured_path(
                    root, server.server_port
                )
                output_dir = root / "negative-evidence"
                self.assertEqual(
                    run_probe(config_path, output_dir, "b" * 40, 5, bindings), 1
                )
                meta = json.loads(
                    (output_dir / "probe_meta.json").read_text(encoding="utf-8")
                )
                termination = json.loads(
                    (output_dir / "termination.json").read_text(encoding="utf-8")
                )
                records = read_jsonl(output_dir / "phase_contract_attempts.jsonl")
            self.assertEqual(meta["status"], "failed")
            self.assertEqual(meta["completed_http_attempts"], 2)
            self.assertEqual(
                meta["failure_reason"], "phase3_contract_validation_failure"
            )
            self.assertEqual(termination["status"], "failed")
            self.assertEqual(records[-1]["parsed_output"]["direction"], "right")
            self.assertEqual(records[-1]["result"], "fail")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_probe_refuses_output_collision_before_network(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "already-exists"
            output_dir.mkdir()
            with self.assertRaises(FileExistsError):
                run_probe(PROBE_CONFIG, output_dir, "c" * 40, 1)

    def test_probe_records_interrupt_as_terminal_negative_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "tools.probe_vllm_phase_contract.requests.post",
            side_effect=KeyboardInterrupt,
        ):
            output_dir = Path(temp_dir) / "interrupted-evidence"
            self.assertEqual(run_probe(PROBE_CONFIG, output_dir, "d" * 40, 1), 1)
            meta = json.loads(
                (output_dir / "probe_meta.json").read_text(encoding="utf-8")
            )
            termination = json.loads(
                (output_dir / "termination.json").read_text(encoding="utf-8")
            )
            records = read_jsonl(output_dir / "phase_contract_attempts.jsonl")
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["failure_reason"], "interrupted")
        self.assertEqual(meta["completed_http_attempts"], 1)
        self.assertEqual(termination["status"], "failed")
        self.assertTrue(records[0]["interrupted"])
        self.assertEqual(records[0]["result"], "fail")

    def test_probe_rejects_non_object_id_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                run_probe(PROBE_CONFIG, Path(temp_dir) / "unused", "not-a-sha", 1)


if __name__ == "__main__":
    unittest.main()
