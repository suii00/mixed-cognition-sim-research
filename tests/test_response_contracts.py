import base64
import copy
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.config import build_effective_config
from engine.execution_contracts import (
    BOUNDED_PROMPT_CONTRACT_VERSION,
    CURRENT_PROMPT_CONTRACT_VERSION,
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION,
)
from engine.llm_client import build_vllm_chat_payload
from engine.parallel_transport import LLMRequest, LLMResponseSchemaError
from engine.prompts import build_phase3_prompt
from engine.provenance import RunLifecycle, compute_prompt_hash
from engine.response_contracts import (
    BOUNDED_RESPONSE_CONTRACT_VERSION,
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    LEGACY_RESPONSE_CONTRACT_VERSION,
    MAX_MEMORY_LENGTH,
    MAX_MESSAGE_LENGTH,
    PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION,
    response_format_for_phase,
    response_schema_sha256,
    validate_parsed_response,
    vllm_transport_contract_version,
)
from engine.sim import Simulation, SimulationAbortedError
from tools.build_disaster_matrix import build_config
from tools.validate_run import ValidationReport, _check_primary_records
from tools.validate_run import validate_run


PROSPECTIVE_TEST_PROTOCOL = "engineering-response-contract-test-v1.0.0"
REPO_ROOT = Path(__file__).resolve().parents[1]


def prospective_simulation_config() -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 7,
            "run_name": "response-contract-integration",
            "run_id": "response-contract-integration",
            "protocol_version": PROSPECTIVE_TEST_PROTOCOL,
            "metric_version": "test-metric-v1.0.0",
            "log_schema_version": "2.0.0",
            "response_contract_version": CANONICAL_RESPONSE_CONTRACT_VERSION,
            "failure_thresholds": {
                "transport_failures": 0,
                "syntax_parse_failures": 0,
                "schema_validation_failures": 0,
            },
        },
        "blocs": [{
            "name": "alpha",
            "provider": "vllm",
            "model": "model",
            "model_source": "test/model",
            "model_digest": "a" * 40,
            "tokenizer_revision": "a" * 40,
            "backend_version": "0.27.1",
            "dtype": "bfloat16",
            "quantization": "none",
            "chat_template": "test-template",
            "generation_config": "vllm",
            "max_model_len": 4096,
            "tensor_parallel_size": 1,
            "data_parallel_size": 1,
            "endpoint_id": "contract-endpoint",
            "num_agents": 1,
        }],
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
            "max_tokens": 64,
            "timeout_s": 10,
        },
    }


def bounded_simulation_config() -> dict:
    config = prospective_simulation_config()
    config["simulation"].update({
        "run_name": "bounded-response-contract-integration",
        "run_id": "bounded-response-contract-integration",
        "response_contract_version": BOUNDED_RESPONSE_CONTRACT_VERSION,
        "prompt_contract_version": BOUNDED_PROMPT_CONTRACT_VERSION,
        "transport_behavior_version": NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION,
    })
    return config


def successful_phase_call(parsed_override=None, **kwargs):
    bounded = "reasoning must be exactly the empty string" in kwargs["prompt"]
    if "Decide what message" in kwargs["prompt"]:
        parsed = {"message": "", "reasoning": "" if bounded else "phase1"}
    else:
        parsed = {
            "action": "stay",
            "direction": None,
            "memory": "memory",
            "reasoning": "" if bounded else "phase3",
        }
    if parsed_override is not None:
        parsed = parsed_override
    raw_output = json.dumps(parsed, separators=(",", ":"))
    envelope = {
        "id": "chatcmpl-test",
        "model": kwargs["model"],
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": raw_output},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    kwargs["telemetry"]("http_attempt", 1)
    kwargs["attempt_observer"]({
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
        "parse_status": "valid",
        "schema_status": "not_checked",
        "failure_kind": None,
        "error_type": None,
    })
    return parsed, raw_output


def phase3_prompt(version: str) -> str:
    return build_phase3_prompt(
        agent_id=0,
        x=0,
        y=0,
        half_space_size=2,
        places=[],
        place=None,
        agent_count=0,
        memories=[],
        messages=[],
        response_contract_version=version,
    )


class ResponseContractTests(unittest.TestCase):
    def test_schema_bundle_is_versioned_stable_and_has_no_max_length(self):
        self.assertEqual(
            response_schema_sha256(CANONICAL_RESPONSE_CONTRACT_VERSION),
            "964a9d3fbe3932fd9cc0b8bee03d7dfc3680031383ccfc6c0e0165e7a24e889d",
        )
        formats = {
            phase: response_format_for_phase(
                CANONICAL_RESPONSE_CONTRACT_VERSION, phase
            )
            for phase in ("phase1", "phase3")
        }
        self.assertNotIn("maxLength", repr(formats))
        self.assertEqual(
            formats["phase3"]["json_schema"]["schema"].keys(),
            {"oneOf"},
        )
        self.assertEqual(
            vllm_transport_contract_version(
                CANONICAL_RESPONSE_CONTRACT_VERSION
            ),
            PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION,
        )

    def test_existing_prompt_hash_is_stable_and_bounded_prompt_is_separate(self):
        self.assertEqual(
            compute_prompt_hash(REPO_ROOT, CURRENT_PROMPT_CONTRACT_VERSION),
            "4b1f0bb63015239d57b9a3af296e0cab7da19b2240776cd860abbdae4425655b",
        )
        self.assertEqual(
            compute_prompt_hash(REPO_ROOT, BOUNDED_PROMPT_CONTRACT_VERSION),
            hashlib.sha256(
                (REPO_ROOT / "engine" / "prompts_v3.py").read_bytes()
            ).hexdigest(),
        )

    def test_bounded_schema_is_versioned_and_encodes_exact_limits(self):
        self.assertEqual(
            response_schema_sha256(BOUNDED_RESPONSE_CONTRACT_VERSION),
            "b5c4011a7c64bf7c5556ed8bdb20a6942b3318e295adaa86dcc109301951698e",
        )
        phase1 = response_format_for_phase(
            BOUNDED_RESPONSE_CONTRACT_VERSION, "phase1"
        )["json_schema"]["schema"]
        self.assertEqual(
            phase1["properties"]["message"]["maxLength"],
            MAX_MESSAGE_LENGTH,
        )
        self.assertEqual(phase1["properties"]["reasoning"]["maxLength"], 0)
        self.assertEqual(phase1["required"], ["message", "reasoning"])
        self.assertFalse(phase1["additionalProperties"])

        phase3 = response_format_for_phase(
            BOUNDED_RESPONSE_CONTRACT_VERSION, "phase3"
        )["json_schema"]["schema"]
        for branch in phase3["oneOf"]:
            with self.subTest(action=branch["properties"]["action"]["enum"][0]):
                self.assertEqual(
                    branch["properties"]["memory"]["maxLength"],
                    MAX_MEMORY_LENGTH,
                )
                self.assertEqual(
                    branch["properties"]["reasoning"]["maxLength"], 0
                )
                self.assertEqual(
                    branch["required"],
                    ["action", "direction", "memory", "reasoning"],
                )
                self.assertFalse(branch["additionalProperties"])
        self.assertEqual(
            vllm_transport_contract_version(BOUNDED_RESPONSE_CONTRACT_VERSION),
            PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION,
        )

    def test_bounded_runtime_validation_matches_schema_boundaries(self):
        validate_parsed_response(
            {"message": "あ" * MAX_MESSAGE_LENGTH, "reasoning": ""},
            "phase1",
            BOUNDED_RESPONSE_CONTRACT_VERSION,
        )
        with self.assertRaisesRegex(ValueError, "message exceeds"):
            validate_parsed_response(
                {"message": "あ" * (MAX_MESSAGE_LENGTH + 1), "reasoning": ""},
                "phase1",
                BOUNDED_RESPONSE_CONTRACT_VERSION,
            )
        with self.assertRaisesRegex(ValueError, "reasoning must be empty"):
            validate_parsed_response(
                {"message": "", "reasoning": "explanation"},
                "phase1",
                BOUNDED_RESPONSE_CONTRACT_VERSION,
            )

        valid_phase3 = {
            "action": "stay",
            "direction": None,
            "memory": "記" * MAX_MEMORY_LENGTH,
            "reasoning": "",
        }
        validate_parsed_response(
            valid_phase3, "phase3", BOUNDED_RESPONSE_CONTRACT_VERSION
        )
        with self.assertRaisesRegex(ValueError, "memory exceeds"):
            validate_parsed_response(
                {**valid_phase3, "memory": "記" * (MAX_MEMORY_LENGTH + 1)},
                "phase3",
                BOUNDED_RESPONSE_CONTRACT_VERSION,
            )
        with self.assertRaisesRegex(ValueError, "reasoning must be empty"):
            validate_parsed_response(
                {**valid_phase3, "reasoning": "explanation"},
                "phase3",
                BOUNDED_RESPONSE_CONTRACT_VERSION,
            )

    def test_response_formats_are_owned_deep_copies(self):
        first = response_format_for_phase(
            CANONICAL_RESPONSE_CONTRACT_VERSION, "phase1"
        )
        first["json_schema"]["schema"]["required"].append("mutation")
        second = response_format_for_phase(
            CANONICAL_RESPONSE_CONTRACT_VERSION, "phase1"
        )
        self.assertEqual(second["json_schema"]["schema"]["required"], [
            "message",
            "reasoning",
        ])

    def test_legacy_and_canonical_stay_contracts_are_distinct(self):
        legacy_stay = {
            "action": "stay",
            "direction": "",
            "memory": "",
            "reasoning": "",
        }
        validate_parsed_response(
            legacy_stay, "phase3", LEGACY_RESPONSE_CONTRACT_VERSION
        )
        with self.assertRaisesRegex(ValueError, "must be null"):
            validate_parsed_response(
                legacy_stay,
                "phase3",
                CANONICAL_RESPONSE_CONTRACT_VERSION,
            )
        canonical_stay = {**legacy_stay, "direction": None}
        validate_parsed_response(
            canonical_stay,
            "phase3",
            CANONICAL_RESPONSE_CONTRACT_VERSION,
        )

    def test_canonical_move_requires_cardinal_direction(self):
        valid = {
            "action": "move",
            "direction": "left",
            "memory": "note",
            "reasoning": "reason",
        }
        validate_parsed_response(
            valid, "phase3", CANONICAL_RESPONSE_CONTRACT_VERSION
        )
        with self.assertRaisesRegex(ValueError, "must be cardinal"):
            validate_parsed_response(
                {**valid, "direction": None},
                "phase3",
                CANONICAL_RESPONSE_CONTRACT_VERSION,
            )

    def test_prompt_change_is_version_dispatched(self):
        legacy = phase3_prompt(LEGACY_RESPONSE_CONTRACT_VERSION)
        canonical = phase3_prompt(CANONICAL_RESPONSE_CONTRACT_VERSION)
        self.assertNotIn('If action is "stay"', legacy)
        self.assertIn('If action is "stay", direction must be null', canonical)
        self.assertIn('"direction": "up"', legacy)
        self.assertIn('"direction": "up"', canonical)

    def test_matrix_builder_preserves_legacy_and_prepares_canonical(self):
        legacy = build_config("qwen_only", "free_text", 7, "a")
        canonical = build_config(
            "qwen_only",
            "free_text",
            7,
            "a",
            response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
            protocol_version=PROSPECTIVE_TEST_PROTOCOL,
        )
        self.assertNotIn("response_contract_version", legacy["simulation"])
        self.assertEqual(
            legacy["blocs"][0]["llm_overrides"]["response_format"],
            {"type": "json_object"},
        )
        self.assertEqual(
            canonical["simulation"]["response_contract_version"],
            CANONICAL_RESPONSE_CONTRACT_VERSION,
        )
        self.assertEqual(canonical["simulation"]["log_schema_version"], "2.0.0")
        self.assertEqual(
            canonical["simulation"]["protocol_version"],
            PROSPECTIVE_TEST_PROTOCOL,
        )
        self.assertNotIn("llm_overrides", canonical["blocs"][0])
        effective = build_effective_config(canonical)
        self.assertEqual(effective["blocs"][0]["llm_overrides"], {})

    def test_canonical_config_rejects_bloc_owned_response_format(self):
        config = build_config(
            "qwen_only",
            "free_text",
            7,
            "a",
            response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
            protocol_version=PROSPECTIVE_TEST_PROTOCOL,
        )
        config["blocs"][0]["llm_overrides"] = {
            "response_format": {"type": "json_object"}
        }
        with self.assertRaisesRegex(ValueError, "owns phase-specific"):
            build_effective_config(config)

    def test_canonical_config_requires_protocol_and_defaults_observability_schema(self):
        with self.assertRaisesRegex(ValueError, "prospective protocol_version"):
            build_config(
                "qwen_only",
                "free_text",
                7,
                "a",
                response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
            )
        config = build_config(
            "qwen_only",
            "free_text",
            7,
            "a",
            response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
            protocol_version=PROSPECTIVE_TEST_PROTOCOL,
        )
        del config["simulation"]["log_schema_version"]
        effective = build_effective_config(config)
        self.assertEqual(effective["simulation"]["log_schema_version"], "2.0.0")

    def test_bounded_response_and_prompt_contracts_are_exactly_paired(self):
        config = bounded_simulation_config()
        effective = build_effective_config(config)
        self.assertEqual(
            effective["simulation"]["response_contract_version"],
            BOUNDED_RESPONSE_CONTRACT_VERSION,
        )
        self.assertEqual(
            effective["simulation"]["prompt_contract_version"],
            BOUNDED_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            effective["simulation"]["transport_behavior_version"],
            NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION,
        )

        wrong_v3_prompt = bounded_simulation_config()
        wrong_v3_prompt["simulation"][
            "prompt_contract_version"
        ] = CURRENT_PROMPT_CONTRACT_VERSION
        with self.assertRaisesRegex(ValueError, "bounded prompt contract"):
            build_effective_config(wrong_v3_prompt)

        wrong_v2_prompt = prospective_simulation_config()
        wrong_v2_prompt["simulation"][
            "prompt_contract_version"
        ] = BOUNDED_PROMPT_CONTRACT_VERSION
        with self.assertRaisesRegex(ValueError, "current prompt contract"):
            build_effective_config(wrong_v2_prompt)

        wrong_v1_response = bounded_simulation_config()
        wrong_v1_response["simulation"][
            "response_contract_version"
        ] = LEGACY_RESPONSE_CONTRACT_VERSION
        with self.assertRaisesRegex(
            ValueError, "requires phase-response-v3.0.0"
        ):
            build_effective_config(wrong_v1_response)

        wrong_v3_transport = bounded_simulation_config()
        wrong_v3_transport["simulation"][
            "transport_behavior_version"
        ] = CURRENT_TRANSPORT_BEHAVIOR_VERSION
        with self.assertRaisesRegex(ValueError, "no-redirect-v3.0.0"):
            build_effective_config(wrong_v3_transport)

        wrong_v2_transport = prospective_simulation_config()
        wrong_v2_transport["simulation"][
            "transport_behavior_version"
        ] = NO_REDIRECT_TRANSPORT_BEHAVIOR_VERSION
        with self.assertRaisesRegex(
            ValueError, "single-generation-strict-json-v2.0.0"
        ):
            build_effective_config(wrong_v2_transport)

    def test_bounded_config_rejects_runtime_values_and_schema_overrides_before_output(self):
        mutations = (
            {"base_url": "http://127.0.0.1:8000"},
            {"provider": "ollama"},
            {"llm_overrides": {"response_format": {"type": "json_object"}}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                config = bounded_simulation_config()
                config["blocs"][0].update(mutation)
                output_root = Path(temp) / "not-created"
                with self.assertRaises(ValueError):
                    Simulation(config, output_root=output_root)
                self.assertFalse(output_root.exists())

    def test_bounded_schema_failure_aborts_without_retry_or_movement(self):
        invalid_responses = {
            "phase1": {"message": "x" * (MAX_MESSAGE_LENGTH + 1), "reasoning": ""},
            "phase3": {
                "action": "move", "direction": "diagonal", "memory": "", "reasoning": ""
            },
        }
        for invalid_phase, invalid_parsed in invalid_responses.items():
            def phase_call(**kwargs):
                phase = "phase1" if "Decide what message" in kwargs["prompt"] else "phase3"
                return successful_phase_call(
                    parsed_override=invalid_parsed if phase == invalid_phase else None,
                    **kwargs,
                )

            with self.subTest(phase=invalid_phase), tempfile.TemporaryDirectory() as temp:
                with mock.patch(
                    "engine.provenance.collect_gpu_info",
                    return_value={"status": "unavailable", "error": "test_disabled", "devices": []},
                ), mock.patch("engine.sim.call_vllm", side_effect=phase_call) as transport:
                    simulation = Simulation(
                        bounded_simulation_config(),
                        output_root=Path(temp),
                        runtime_bindings={
                            "contract-endpoint": {"base_url": "http://127.0.0.1:8000"}
                        },
                    )
                    initial = [agent.position for agent in simulation.agents]
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SimulationAbortedError):
                        simulation.run()
                meta = simulation.run_lifecycle.meta
                expected_attempts = 1 if invalid_phase == "phase1" else 2
                self.assertEqual(meta["status"], "aborted")
                self.assertEqual(meta["abort_reason"], "schema_validation_failure")
                self.assertEqual(meta["completed_steps"], 0)
                self.assertEqual(meta["schema_validation_failures"], 1)
                self.assertEqual(meta["generation_retries"], 0)
                self.assertEqual(meta["http_attempts"], expected_attempts)
                self.assertEqual(transport.call_count, expected_attempts)
                self.assertEqual([agent.position for agent in simulation.agents], initial)
                run_dir = Path(simulation.output_dir)
                self.assertEqual((run_dir / "memory_reasoning.jsonl").read_bytes(), b"")
                attempts = [json.loads(line) for line in (run_dir / "llm_attempts.jsonl").read_text().splitlines()]
                self.assertEqual(json.loads(attempts[-1]["raw_output"]), invalid_parsed)
                self.assertEqual(attempts[-1]["schema_status"], "invalid")
                self.assertEqual(meta["raw_manifest_status"], "available")

    def test_payload_accepts_only_repository_owned_phase_schema(self):
        phase_format = response_format_for_phase(
            CANONICAL_RESPONSE_CONTRACT_VERSION, "phase3"
        )
        original = copy.deepcopy(phase_format)
        payload = build_vllm_chat_payload(
            prompt="prompt",
            model="model",
            phase_response_format=phase_format,
        )
        self.assertEqual(payload["response_format"], original)
        payload["response_format"]["json_schema"]["name"] = "mutation"
        self.assertEqual(phase_format, original)

        arbitrary = copy.deepcopy(original)
        arbitrary["json_schema"]["name"] = "arbitrary"
        with self.assertRaisesRegex(ValueError, "exact versioned"):
            build_vllm_chat_payload(
                prompt="prompt",
                model="model",
                phase_response_format=arbitrary,
            )
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            build_vllm_chat_payload(
                prompt="prompt",
                model="model",
                llm_overrides={"response_format": {"type": "json_object"}},
                phase_response_format=original,
            )

    def test_default_transport_selects_schema_from_request_phase(self):
        for phase in ("phase1", "phase3"):
            with self.subTest(phase=phase), mock.patch(
                "engine.sim.call_vllm",
                return_value=({"message": "", "reasoning": ""}, "{}"),
            ) as transport:
                request = LLMRequest(
                    request_id=f"step-000001:{phase}:agent-000000",
                    step=1,
                    phase=phase,
                    agent_id=0,
                    model="model",
                    base_url="http://127.0.0.1:8000",
                    prompt="prompt",
                    temperature=0.0,
                    max_tokens=32,
                    timeout_s=10,
                    provider="vllm",
                    response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
                )
                Simulation._default_transport(request, lambda *_: None)
                self.assertEqual(
                    transport.call_args.kwargs["phase_response_format"],
                    response_format_for_phase(
                        CANONICAL_RESPONSE_CONTRACT_VERSION, phase
                    ),
                )

    def test_runtime_error_type_wraps_shared_contract(self):
        from engine.parallel_transport import _validate_response_schema

        with self.assertRaisesRegex(LLMResponseSchemaError, "must be null"):
            _validate_response_schema(
                {
                    "action": "stay",
                    "direction": "none",
                    "memory": "",
                    "reasoning": "",
                },
                "phase3",
                CANONICAL_RESPONSE_CONTRACT_VERSION,
            )

    def test_run_meta_records_contract_schema_and_transport_versions(self):
        config = build_config(
            "qwen_only",
            "free_text",
            7,
            "a",
            response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
            protocol_version=PROSPECTIVE_TEST_PROTOCOL,
        )
        config["simulation"]["run_id"] = "response-contract-provenance"
        config["simulation"]["run_name"] = "response-contract-provenance"
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "engine.provenance.collect_git_info",
            return_value={
                "git_sha": "a" * 40,
                "git_dirty": False,
                "git_probe_status": "available",
                "git_probe_errors": [],
            },
        ), mock.patch(
            "engine.provenance.collect_gpu_info",
            return_value={
                "status": "unavailable",
                "error": "test_disabled",
                "driver_version": None,
                "cuda_version": None,
                "devices": [],
            },
        ):
            lifecycle = RunLifecycle.create(
                build_effective_config(config),
                output_root=Path(temp_dir),
            )
            self.assertEqual(
                lifecycle.meta["response_contract_version"],
                CANONICAL_RESPONSE_CONTRACT_VERSION,
            )
            self.assertEqual(
                lifecycle.meta["response_schema_sha256"],
                response_schema_sha256(CANONICAL_RESPONSE_CONTRACT_VERSION),
            )
            self.assertEqual(
                lifecycle.meta["response_schema_hash_algorithm"],
                "sha256-canonical-json-v1",
            )
            self.assertEqual(
                lifecycle.meta["vllm_transport_contract_version"],
                PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION,
            )

    def test_offline_validator_dispatches_canonical_cross_field_contract(self):
        record = {
            "step": 1,
            "agent_id": 0,
            "bloc": "alpha",
            "model": "model",
            "position": [0, 0],
            "action": "stay",
            "direction": "none",
            "memory": "",
            "reasoning": "",
        }
        canonical_report = ValidationReport(Path("."), strict=True)
        _check_primary_records(
            "memory_reasoning.jsonl",
            [(1, record)],
            1,
            1,
            {0: ("alpha", "model")},
            "2.0.0",
            CANONICAL_RESPONSE_CONTRACT_VERSION,
            canonical_report,
        )
        self.assertTrue(
            any("stay direction must be null" in error for error in canonical_report.errors)
        )

        legacy_report = ValidationReport(Path("."), strict=True)
        _check_primary_records(
            "memory_reasoning.jsonl",
            [(1, record)],
            1,
            1,
            {0: ("alpha", "model")},
            "2.0.0",
            LEGACY_RESPONSE_CONTRACT_VERSION,
            legacy_report,
        )
        self.assertEqual(legacy_report.errors, [])

        bounded_record = {
            **record,
            "direction": None,
            "reasoning": "explanation",
        }
        bounded_report = ValidationReport(Path("."), strict=True)
        _check_primary_records(
            "memory_reasoning.jsonl",
            [(1, bounded_record)],
            1,
            1,
            {0: ("alpha", "model")},
            "2.0.0",
            BOUNDED_RESPONSE_CONTRACT_VERSION,
            bounded_report,
        )
        self.assertTrue(
            any(
                "reasoning must be empty" in error
                for error in bounded_report.errors
            )
        )

    def test_prospective_simulation_and_offline_validator_agree(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "engine.provenance.collect_git_info",
            return_value={
                "git_sha": "a" * 40,
                "git_dirty": False,
                "git_probe_status": "available",
                "git_probe_errors": [],
            },
        ), mock.patch(
            "engine.provenance.collect_gpu_info",
            return_value={
                "status": "unavailable",
                "error": "test_disabled",
                "driver_version": None,
                "cuda_version": None,
                "devices": [],
            },
        ), mock.patch(
            "engine.sim.call_vllm", side_effect=successful_phase_call
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                simulation = Simulation(
                    prospective_simulation_config(),
                    output_root=Path(temp_dir),
                    runtime_bindings={
                        "contract-endpoint": {
                            "base_url": "http://127.0.0.1:8000"
                        }
                    },
                )
                simulation.run()
            report = validate_run(simulation.output_dir, strict=True)
            self.assertTrue(report.valid, report.errors)
            self.assertEqual(simulation.run_lifecycle.meta["completed_steps"], 1)
            self.assertEqual(simulation.run_lifecycle.meta["http_attempts"], 2)
            self.assertEqual(
                simulation.run_lifecycle.meta["schema_validation_failures"], 0
            )

    def test_bounded_simulation_routes_prompts_and_logs_empty_placeholders(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "engine.provenance.collect_git_info",
            return_value={
                "git_sha": "a" * 40,
                "git_dirty": False,
                "git_probe_status": "available",
                "git_probe_errors": [],
            },
        ), mock.patch(
            "engine.provenance.collect_gpu_info",
            return_value={
                "status": "unavailable",
                "error": "test_disabled",
                "driver_version": None,
                "cuda_version": None,
                "devices": [],
            },
        ), mock.patch(
            "engine.sim.call_vllm", side_effect=successful_phase_call
        ) as transport:
            with contextlib.redirect_stdout(io.StringIO()):
                simulation = Simulation(
                    bounded_simulation_config(),
                    output_root=Path(temp_dir),
                    runtime_bindings={
                        "contract-endpoint": {
                            "base_url": "http://127.0.0.1:8000"
                        }
                    },
                )
                simulation.run()

            prompts = [call.kwargs["prompt"] for call in transport.call_args_list]
            self.assertIn("at most 512 Unicode characters", prompts[0])
            self.assertIn("at most 256 Unicode characters", prompts[1])
            self.assertTrue(all('"reasoning": ""' in prompt for prompt in prompts))
            self.assertTrue(all("internal reasoning" not in prompt for prompt in prompts))
            self.assertEqual(
                transport.call_args_list[0].kwargs["phase_response_format"],
                response_format_for_phase(
                    BOUNDED_RESPONSE_CONTRACT_VERSION, "phase1"
                ),
            )
            self.assertEqual(
                transport.call_args_list[1].kwargs["phase_response_format"],
                response_format_for_phase(
                    BOUNDED_RESPONSE_CONTRACT_VERSION, "phase3"
                ),
            )

            phase1_record = json.loads(
                (Path(simulation.output_dir) / "phase1_raw.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            phase3_record = json.loads(
                (Path(simulation.output_dir) / "memory_reasoning.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertEqual(phase1_record["parsed"]["reasoning"], "")
            self.assertEqual(phase3_record["reasoning"], "")
            self.assertEqual(
                simulation.run_lifecycle.meta["prompt_contract_version"],
                BOUNDED_PROMPT_CONTRACT_VERSION,
            )
            self.assertEqual(
                simulation.run_lifecycle.meta["response_contract_version"],
                BOUNDED_RESPONSE_CONTRACT_VERSION,
            )
            report = validate_run(simulation.output_dir, strict=True)
            self.assertTrue(report.valid, report.errors)


if __name__ == "__main__":
    unittest.main()
