import base64
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.config import (
    ENDPOINT_ASSIGNMENT_POLICY,
    build_effective_config,
    required_endpoint_ids,
)
from engine.provenance import OBSERVABILITY_LOG_SCHEMA_VERSION, file_manifest
from engine.parallel_transport import TransportOutcome
from engine.sim import Simulation
from tools.validate_run import main as validator_main
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "tools" / "validate_run.py"


def make_config(run_id: str) -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 7,
            "run_name": "validator_fixture",
            "run_id": run_id,
            "protocol_version": "test-protocol-v1",
            "metric_version": "test-metric-v1",
            "log_schema_version": "2.0.0",
            "response_contract_version": "phase-response-v2.0.0",
        },
        "blocs": [
            {
                "name": "alpha",
                "model": "mock-model",
                "provider": "vllm",
                "model_source": "test/mock-model",
                "model_digest": "a" * 40,
                "tokenizer_revision": "a" * 40,
                "backend_version": "test-backend",
                "dtype": "float32",
                "quantization": "none",
                "chat_template": "test-template",
                "generation_config": "vllm",
                "max_model_len": 1024,
                "tensor_parallel_size": 1,
                "data_parallel_size": 1,
                "endpoint_id": "validator-endpoint",
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


def offline_llm(**kwargs):
    telemetry = kwargs.get("telemetry")
    if telemetry is not None:
        telemetry("http_attempt", 1)
    parsed = (
        {"message": "", "reasoning": ""}
        if kwargs["phase"] == "phase1"
        else {
            "action": "stay",
            "direction": None,
            "memory": "",
            "reasoning": "",
        }
    )
    return parsed, json.dumps(parsed)


def nonempty_message_llm(**kwargs):
    telemetry = kwargs.get("telemetry")
    if telemetry is not None:
        telemetry("http_attempt", 1)
    parsed = (
        {"message": "shared-message", "reasoning": "shared-reasoning"}
        if kwargs["phase"] == "phase1"
        else {
            "action": "stay",
            "direction": None,
            "memory": "",
            "reasoning": "shared-reasoning",
        }
    )
    return parsed, json.dumps(parsed)


def null_stay_llm(**kwargs):
    telemetry = kwargs.get("telemetry")
    if telemetry is not None:
        telemetry("http_attempt", 1)
    parsed = (
        {"message": "", "reasoning": ""}
        if kwargs["phase"] == "phase1"
        else {
            "action": "stay",
            "direction": None,
            "memory": "",
            "reasoning": "",
        }
    )
    return parsed, json.dumps(parsed)


class ValidateRunTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.output_root = Path(self.temp_directory.name)

        git_info = {
            "git_sha": "b" * 40,
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

    def create_fixture(
        self,
        run_id: str,
        config: dict | None = None,
        llm=offline_llm,
    ) -> Path:
        fixture_config = config or make_config(run_id)
        effective_config = build_effective_config(fixture_config)
        self.assertTrue(required_endpoint_ids(effective_config))

        def transport(request, telemetry):
            parsed, raw_output = llm(phase=request.phase, telemetry=telemetry)
            envelope = {
                "id": f"fixture-{request.request_id}",
                "model": request.model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": raw_output},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
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
                "raw_output": raw_output,
                "finish_reason": "stop",
                "usage": envelope["usage"],
                "transport_status": "ok",
                "parse_status": "valid" if parsed is not None else "invalid",
                "schema_status": "not_checked",
                "failure_kind": None if parsed is not None else "syntax",
                "error_type": None,
            }
            return TransportOutcome(
                parsed=parsed,
                raw_output=raw_output,
                attempts=(attempt,),
            )

        with contextlib.redirect_stdout(io.StringIO()):
            simulation = Simulation(
                fixture_config,
                output_root=self.output_root,
                repo_root=REPO_ROOT,
                transport=transport,
            )
            simulation.run()
        return Path(simulation.output_dir)

    def rewrite_first_phase3_record(
        self,
        run_dir: Path,
        *,
        action: str,
        direction: str | None,
        log_schema_version: str | None = None,
    ) -> None:
        raw_path = run_dir / "memory_reasoning.jsonl"
        records = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
        ]
        records[0]["action"] = action
        records[0]["direction"] = direction
        raw_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if log_schema_version is not None:
            meta["log_schema_version"] = log_schema_version
        meta["raw_manifest"]["files"][raw_path.name] = file_manifest(raw_path)
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

    def test_strict_fixture_passes_and_reports_unverifiable_limits(self):
        run_dir = self.create_fixture("validator-pass")

        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)
        self.assertFalse(report.errors)
        self.assertTrue(report.unverifiable)
        self.assertTrue(
            any("event identity" in message for message in report.unverifiable),
            report.unverifiable,
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = validator_main([str(run_dir), "--strict"])
        rendered = output.getvalue()
        self.assertEqual(exit_code, 0, rendered)
        self.assertIn("PASS:", rendered)
        self.assertIn("UNVERIFIABLE:", rendered)

    def test_strict_validator_recomputes_endpoint_pool_provenance(self):
        config = make_config("validator-endpoint-pool")
        bloc = config["blocs"][0]
        bloc["num_agents"] = 2
        bloc.pop("endpoint_id")
        bloc["endpoint_assignment_policy"] = ENDPOINT_ASSIGNMENT_POLICY
        bloc["endpoint_pool"] = [
            {
                "endpoint_id": "alpha-a",
                "device_slot": "slot-a",
            },
            {
                "endpoint_id": "alpha-b",
                "device_slot": "slot-b",
            },
        ]
        run_dir = self.create_fixture(
            "validator-endpoint-pool",
            config=config,
        )

        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [model["endpoint_id"] for model in meta["models"]],
            ["alpha-a", "alpha-b"],
        )

        meta["models"][1]["endpoint_id"] = "tampered"
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        tampered = validate_run(run_dir, strict=True)
        self.assertFalse(tampered.valid)
        self.assertTrue(
            any("models metadata differs" in error for error in tampered.errors),
            tampered.errors,
        )

    def test_log_schema_2_accepts_null_direction_only_for_stay(self):
        run_dir = self.create_fixture(
            "validator-schema-1-1-null-stay",
            llm=null_stay_llm,
        )
        meta = json.loads(
            (run_dir / "run_meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            meta["log_schema_version"], OBSERVABILITY_LOG_SCHEMA_VERSION
        )
        phase3_record = json.loads(
            (run_dir / "memory_reasoning.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        self.assertEqual(phase3_record["action"], "stay")
        self.assertIsNone(phase3_record["direction"])

        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)

    def test_log_schema_2_rejects_null_direction_for_move(self):
        run_dir = self.create_fixture("validator-schema-1-1-null-move")
        self.rewrite_first_phase3_record(
            run_dir,
            action="move",
            direction=None,
        )

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "direction must be a string, or null only when action is "
                "'stay'" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_unknown_log_schema_version_fails_closed(self):
        run_dir = self.create_fixture("validator-schema-unknown")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["log_schema_version"] = "999.0.0"
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any("unsupported log_schema_version" in error for error in report.errors),
            report.errors,
        )

    def test_missing_required_raw_file_fails(self):
        run_dir = self.create_fixture("validator-missing")
        (run_dir / "messages.jsonl").unlink()

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any("required raw files are missing" in error for error in report.errors),
            report.errors,
        )

    def test_raw_modification_fails_manifest_validation(self):
        run_dir = self.create_fixture("validator-tampered")
        raw_path = run_dir / "phase1_raw.jsonl"
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write('{"tampered":true}\n')

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any("raw manifest mismatch" in error for error in report.errors),
            report.errors,
        )

    def test_manifest_counts_reject_boolean_values(self):
        run_dir = self.create_fixture("validator-manifest-types")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        empty_entry = meta["raw_manifest"]["files"]["messages.jsonl"]
        self.assertEqual(empty_entry["bytes"], 0)
        self.assertEqual(empty_entry["lines"], 0)
        empty_entry["bytes"] = False
        empty_entry["lines"] = False
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "must be a non-negative integer" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_duplicate_natural_key_fails_after_manifest_is_recomputed(self):
        run_dir = self.create_fixture("validator-duplicate")
        raw_path = run_dir / "phase1_raw.jsonl"
        first_line = raw_path.read_text(encoding="utf-8").splitlines()[0]
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(first_line + "\n")

        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"]["files"][raw_path.name] = file_manifest(raw_path)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertFalse(
            any("raw manifest mismatch" in error for error in report.errors),
            report.errors,
        )
        self.assertTrue(
            any("duplicates natural key" in error for error in report.errors),
            report.errors,
        )

    def test_message_must_match_phase1_after_manifest_is_recomputed(self):
        config = make_config("validator-message-tamper")
        config["blocs"][0]["num_agents"] = 2
        config["agents"]["communication_radius"] = 100
        run_dir = self.create_fixture(
            "validator-message-tamper",
            config=config,
            llm=nonempty_message_llm,
        )
        message_path = run_dir / "messages.jsonl"
        records = [
            json.loads(line)
            for line in message_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(records)
        records[0]["message"] = "TAMPERED_NOT_IN_PHASE1"
        message_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"]["files"][message_path.name] = file_manifest(
            message_path
        )
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any("differs from the matching Phase 1" in error for error in report.errors),
            report.errors,
        )

    def test_deleted_message_fails_after_manifest_is_recomputed(self):
        config = make_config("validator-message-deleted")
        config["blocs"][0]["num_agents"] = 2
        config["agents"]["communication_radius"] = 100
        run_dir = self.create_fixture(
            "validator-message-deleted",
            config=config,
            llm=nonempty_message_llm,
        )
        message_path = run_dir / "messages.jsonl"
        lines = message_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        message_path.write_text(lines[1] + "\n", encoding="utf-8")

        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"]["files"][message_path.name] = file_manifest(
            message_path
        )
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertFalse(
            any("raw manifest mismatch" in error for error in report.errors),
            report.errors,
        )
        self.assertTrue(
            any(
                "expected message natural keys" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_receiver_subset_fails_after_manifest_is_recomputed(self):
        config = make_config("validator-receiver-subset")
        config["blocs"][0]["num_agents"] = 3
        config["agents"]["communication_radius"] = 100
        run_dir = self.create_fixture(
            "validator-receiver-subset",
            config=config,
            llm=nonempty_message_llm,
        )
        message_path = run_dir / "messages.jsonl"
        records = [
            json.loads(line)
            for line in message_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["receiver_ids"], [1, 2])
        records[0]["receiver_ids"] = [1]
        message_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"]["files"][message_path.name] = file_manifest(
            message_path
        )
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertFalse(
            any("raw manifest mismatch" in error for error in report.errors),
            report.errors,
        )
        self.assertTrue(
            any(
                "reconstructed communication boundary" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_unexpected_message_fails_after_manifest_is_recomputed(self):
        config = make_config("validator-message-unexpected")
        config["blocs"][0]["num_agents"] = 2
        config["agents"]["communication_radius"] = 0
        run_dir = self.create_fixture(
            "validator-message-unexpected",
            config=config,
            llm=nonempty_message_llm,
        )
        message_path = run_dir / "messages.jsonl"
        self.assertEqual(message_path.read_text(encoding="utf-8"), "")
        message_path.write_text(
            json.dumps({
                "step": 1,
                "sender_id": 0,
                "sender_bloc": "alpha",
                "sender_model": "mock-model",
                "receiver_ids": [1],
                "message": "shared-message",
                "reasoning": "shared-reasoning",
            }) + "\n",
            encoding="utf-8",
        )

        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"]["files"][message_path.name] = file_manifest(
            message_path
        )
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "unexpected message natural keys" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_message_reconstruction_exception_fails_closed(self):
        run_dir = self.create_fixture("validator-message-reconstruct-error")
        with mock.patch(
            "tools.validate_run.World",
            side_effect=RuntimeError("must-not-be-reported"),
        ):
            report = validate_run(run_dir, strict=True)

        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                error == "cannot reconstruct expected messages: RuntimeError"
                for error in report.errors
            ),
            report.errors,
        )
        self.assertNotIn("must-not-be-reported", "\n".join(report.errors))

    def test_unavailable_dependency_and_cuda_probes_are_explicit(self):
        run_dir = self.create_fixture("validator-partial-provenance")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        dependency_names = list(meta["dependencies"])
        meta["dependencies"] = {name: None for name in dependency_names}
        meta["dependencies_probe_status"] = "unavailable"
        meta["dependencies_probe_errors"] = [
            f"{name}:version_unavailable" for name in dependency_names
        ]
        meta["gpu_info"] = {
            "status": "available",
            "error": "cuda_version_not_reported",
            "driver_version": "999.0",
            "cuda_version": None,
            "cuda_probe_status": "unavailable",
            "cuda_probe_error": "cuda_version_not_reported",
            "malformed_device_rows": 0,
            "devices": [{
                "index": "0",
                "name": "mock-gpu",
                "memory_total_mib": "1024",
            }],
        }
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)
        rendered = "\n".join(report.unverifiable)
        self.assertIn("dependency environment", rendered)
        self.assertIn("CUDA version", rendered)

    def test_partial_gpu_inventory_is_explicitly_unverifiable(self):
        run_dir = self.create_fixture("validator-partial-gpu")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["gpu_info"] = {
            "status": "partial",
            "error": "malformed_device_rows",
            "driver_version": "999.0",
            "cuda_version": "99.0",
            "cuda_probe_status": "available",
            "cuda_probe_error": None,
            "malformed_device_rows": 1,
            "devices": [{
                "index": "0",
                "name": "mock-gpu",
                "memory_total_mib": "1024",
            }],
        }
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)
        self.assertTrue(
            any("complete GPU inventory" in item for item in report.unverifiable),
            report.unverifiable,
        )

    def test_available_gpu_cannot_silently_report_malformed_rows(self):
        run_dir = self.create_fixture("validator-silent-partial-gpu")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["gpu_info"] = {
            "status": "available",
            "error": None,
            "driver_version": "999.0",
            "cuda_version": "99.0",
            "cuda_probe_status": "available",
            "cuda_probe_error": None,
            "malformed_device_rows": 1,
            "devices": [{
                "index": "0",
                "name": "mock-gpu",
                "memory_total_mib": "1024",
            }],
        }
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any("cannot contain malformed" in item for item in report.errors),
            report.errors,
        )

    def test_blank_dependency_version_fails(self):
        run_dir = self.create_fixture("validator-blank-dependency")
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["dependencies"] = {
            name: "" for name in meta["dependencies"]
        }
        meta["dependencies_probe_status"] = "available"
        meta["dependencies_probe_errors"] = []
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")

        report = validate_run(run_dir, strict=True)
        self.assertFalse(report.valid)
        self.assertTrue(
            any(
                "non-empty version strings" in error
                for error in report.errors
            ),
            report.errors,
        )

    def test_real_validator_subprocess_returns_zero(self):
        run_dir = self.create_fixture("validator-subprocess")
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        completed = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                str(run_dir),
                "--strict",
            ],
            cwd=str(REPO_ROOT),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS:", completed.stdout)
        self.assertIn("UNVERIFIABLE:", completed.stdout)


if __name__ == "__main__":
    unittest.main()
