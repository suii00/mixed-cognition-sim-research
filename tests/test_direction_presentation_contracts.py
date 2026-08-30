import base64
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from engine.config import build_effective_config
from engine.execution_contracts import (
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    JAPANESE_COMPACT_LR_PROMPT_CONTRACT_VERSION,
    JAPANESE_COMPACT_RL_PROMPT_CONTRACT_VERSION,
)
from engine.japanese_compact_lr_prompts_v2 import (
    build_phase1_prompt as build_lr_phase1_prompt,
)
from engine.japanese_compact_lr_prompts_v2 import (
    build_phase3_prompt as build_lr_phase3_prompt,
)
from engine.japanese_compact_rl_prompts_v2 import (
    build_phase1_prompt as build_rl_phase1_prompt,
)
from engine.japanese_compact_rl_prompts_v2 import (
    build_phase3_prompt as build_rl_phase3_prompt,
)
from engine.provenance import compute_prompt_hash
from engine.response_contracts import (
    COMPACT_LR_RESPONSE_CONTRACT_VERSION,
    COMPACT_RL_RESPONSE_CONTRACT_VERSION,
    COMPACT_VLLM_TRANSPORT_CONTRACT_VERSION,
    response_format_for_phase,
    response_schema_sha256,
    validate_parsed_response,
    vllm_transport_contract_version,
)
from engine.sim import Simulation
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]


def compact_config(order: str) -> dict:
    if order == "lr":
        prompt_version = JAPANESE_COMPACT_LR_PROMPT_CONTRACT_VERSION
        response_version = COMPACT_LR_RESPONSE_CONTRACT_VERSION
    else:
        prompt_version = JAPANESE_COMPACT_RL_PROMPT_CONTRACT_VERSION
        response_version = COMPACT_RL_RESPONSE_CONTRACT_VERSION
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 2403,
            "run_name": f"compact-{order}",
            "run_id": f"compact-{order}",
            "protocol_version": "engineering-direction-presentation-audit-v2.0.0",
            "metric_version": "direction-presentation-audit-metric-v1.0.0",
            "log_schema_version": "2.0.0",
            "prompt_contract_version": prompt_version,
            "response_contract_version": response_version,
            "research_eligible": False,
        },
        "blocs": [{
            "backend_version": "0.27.1",
            "chat_template": "synthetic-test-template",
            "data_parallel_size": 1,
            "dtype": "bfloat16",
            "endpoint_id": "logical-endpoint",
            "generation_config": "vllm",
            "max_model_len": 4096,
            "model": "served-model",
            "model_digest": "a" * 40,
            "model_source": "example/served-model",
            "name": "condition-a",
            "num_agents": 1,
            "provider": "vllm",
            "quantization": "none",
            "tensor_parallel_size": 1,
            "tokenizer_revision": "a" * 40,
        }],
        "agents": {
            "communication_radius": 4,
            "edge_policy": "full",
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 4,
            "message_context_size": 4,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 128,
            "timeout_s": 5,
            "max_concurrency": 1,
        },
    }


def prompt_args() -> dict:
    return {
        "agent_id": 3,
        "x": -1,
        "y": 2,
        "half_space_size": 5,
        "places": [],
        "place": None,
        "agent_count": 0,
        "memories": ["前の位置を確認した"],
        "messages": [{"sender_id": 2, "message": "周囲を確認する"}],
    }


def swap_horizontal_words(value: str) -> str:
    return (
        value.replace('"left"', '"__horizontal_english__"')
        .replace('"right"', '"left"')
        .replace('"__horizontal_english__"', '"right"')
        .replace("左", "__horizontal_japanese__")
        .replace("右", "左")
        .replace("__horizontal_japanese__", "右")
    )


def successful_compact_phase_call(**kwargs):
    if "近くのエージェントへ送るメッセージ" in kwargs["prompt"]:
        parsed = {"message": "周囲を確認する", "reasoning": ""}
    else:
        parsed = {
            "action": "stay",
            "direction": None,
            "memory": "位置を記録する",
            "reasoning": "",
        }
    raw_output = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    envelope = {
        "id": "chatcmpl-compact-test",
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


class DirectionPresentationContractTests(unittest.TestCase):
    def test_phase1_is_identical_and_phase3_is_exact_horizontal_mirror(self):
        args = prompt_args()
        self.assertEqual(
            build_lr_phase1_prompt(**args),
            build_rl_phase1_prompt(**args),
        )
        lr = build_lr_phase3_prompt(
            **args,
            response_contract_version=COMPACT_LR_RESPONSE_CONTRACT_VERSION,
        )
        rl = build_rl_phase3_prompt(
            **args,
            response_contract_version=COMPACT_RL_RESPONSE_CONTRACT_VERSION,
        )
        self.assertEqual(swap_horizontal_words(lr), rl)
        self.assertLess(lr.index('"left"'), lr.index('"right"'))
        self.assertLess(rl.index('"right"'), rl.index('"left"'))
        self.assertIn("`reasoning`は必ず空文字列", lr)

    def test_prompt_hashes_bind_each_standalone_source(self):
        pairs = (
            (
                JAPANESE_COMPACT_LR_PROMPT_CONTRACT_VERSION,
                "japanese_compact_lr_prompts_v2.py",
            ),
            (
                JAPANESE_COMPACT_RL_PROMPT_CONTRACT_VERSION,
                "japanese_compact_rl_prompts_v2.py",
            ),
        )
        for version, filename in pairs:
            with self.subTest(version=version):
                expected = hashlib.sha256(
                    (REPO_ROOT / "engine" / filename).read_bytes()
                ).hexdigest()
                self.assertEqual(compute_prompt_hash(REPO_ROOT, version), expected)

    def test_response_schema_mirrors_direction_order_and_forces_empty_reasoning(self):
        self.assertEqual(
            response_format_for_phase(
                COMPACT_LR_RESPONSE_CONTRACT_VERSION, "phase1"
            ),
            response_format_for_phase(
                COMPACT_RL_RESPONSE_CONTRACT_VERSION, "phase1"
            ),
        )
        lr = response_format_for_phase(
            COMPACT_LR_RESPONSE_CONTRACT_VERSION, "phase3"
        )
        rl = response_format_for_phase(
            COMPACT_RL_RESPONSE_CONTRACT_VERSION, "phase3"
        )
        lr_move = lr["json_schema"]["schema"]["oneOf"][0]["properties"]
        rl_move = rl["json_schema"]["schema"]["oneOf"][0]["properties"]
        self.assertEqual(
            lr_move["direction"]["enum"],
            ["up", "down", "left", "right"],
        )
        self.assertEqual(
            rl_move["direction"]["enum"],
            ["up", "down", "right", "left"],
        )
        self.assertEqual(lr_move["reasoning"]["enum"], [""])
        self.assertEqual(rl_move["reasoning"]["enum"], [""])
        self.assertNotEqual(
            response_schema_sha256(COMPACT_LR_RESPONSE_CONTRACT_VERSION),
            response_schema_sha256(COMPACT_RL_RESPONSE_CONTRACT_VERSION),
        )
        for version in (
            COMPACT_LR_RESPONSE_CONTRACT_VERSION,
            COMPACT_RL_RESPONSE_CONTRACT_VERSION,
        ):
            self.assertEqual(
                vllm_transport_contract_version(version),
                COMPACT_VLLM_TRANSPORT_CONTRACT_VERSION,
            )

    def test_runtime_validator_rejects_nonempty_compact_reasoning(self):
        valid_phase1 = {"message": "周囲を確認する", "reasoning": ""}
        valid_phase3 = {
            "action": "move",
            "direction": "left",
            "memory": "位置を記録する",
            "reasoning": "",
        }
        for version in (
            COMPACT_LR_RESPONSE_CONTRACT_VERSION,
            COMPACT_RL_RESPONSE_CONTRACT_VERSION,
        ):
            validate_parsed_response(valid_phase1, "phase1", version)
            validate_parsed_response(valid_phase3, "phase3", version)
            with self.assertRaisesRegex(ValueError, "reasoning must be empty"):
                validate_parsed_response(
                    {**valid_phase1, "reasoning": "説明"}, "phase1", version
                )
            with self.assertRaisesRegex(ValueError, "reasoning must be empty"):
                validate_parsed_response(
                    {**valid_phase3, "reasoning": "説明"}, "phase3", version
                )

    def test_config_requires_matched_prompt_and_response_order(self):
        for order in ("lr", "rl"):
            effective = build_effective_config(compact_config(order))
            self.assertFalse(effective["simulation"]["research_eligible"])
        mismatched = compact_config("lr")
        mismatched["simulation"]["response_contract_version"] = (
            COMPACT_RL_RESPONSE_CONTRACT_VERSION
        )
        with self.assertRaisesRegex(ValueError, "requires japanese-compact-rl"):
            build_effective_config(mismatched)

    def test_simulation_dispatches_both_compact_prompt_contracts(self):
        for order in ("lr", "rl"):
            with self.subTest(order=order):
                config = build_effective_config(compact_config(order))
                simulation = Simulation.__new__(Simulation)
                simulation.agents = [SimpleNamespace(
                    agent_id=0,
                    model="served-model",
                    base_url="http://127.0.0.1:1",
                    provider="vllm",
                    llm_overrides=None,
                    endpoint_id="logical-endpoint",
                    device_slot=None,
                )]
                simulation.half_space_size = 2
                simulation.disaster = None
                simulation.prompt_contract_version = config["simulation"][
                    "prompt_contract_version"
                ]
                simulation.response_contract_version = config["simulation"][
                    "response_contract_version"
                ]
                simulation.transport_behavior_version = (
                    CURRENT_TRANSPORT_BEHAVIOR_VERSION
                )
                simulation.temperature = 0.0
                simulation.max_tokens = 128
                simulation.timeout_s = 5
                simulation.strict_response_validation = True
                snapshot = {
                    "places": [],
                    "agents": {0: {
                        "position": (0, 0),
                        "place": None,
                        "agent_count": 0,
                        "memories": [],
                        "messages": [],
                    }},
                }
                request = simulation._build_phase_requests(
                    1, "phase3", snapshot
                )[0]
                first = '"left"' if order == "lr" else '"right"'
                second = '"right"' if order == "lr" else '"left"'
                self.assertLess(
                    request.prompt.index(first), request.prompt.index(second)
                )

    def test_compact_contract_and_delivery_control_pass_strict_run_validation(self):
        clean_git = {
            "git_sha": "a" * 40,
            "git_dirty": False,
            "git_probe_status": "available",
            "git_probe_errors": [],
        }
        no_gpu = {
            "status": "unavailable",
            "error": "test_disabled",
            "driver_version": None,
            "cuda_version": None,
            "devices": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "engine.provenance.collect_git_info", return_value=clean_git
        ), mock.patch(
            "engine.provenance.collect_gpu_info", return_value=no_gpu
        ), mock.patch(
            "engine.sim.call_vllm", side_effect=successful_compact_phase_call
        ):
            for order, edge_policy in (("lr", "full"), ("rl", "none")):
                config = compact_config(order)
                config["simulation"]["run_id"] = f"compact-integration-{order}"
                config["simulation"]["run_name"] = f"compact-integration-{order}"
                config["agents"]["edge_policy"] = edge_policy
                config["blocs"][0]["num_agents"] = 2
                with contextlib.redirect_stdout(io.StringIO()):
                    simulation = Simulation(
                        config,
                        output_root=Path(temp_dir),
                        runtime_bindings={
                            "logical-endpoint": {
                                "base_url": "http://127.0.0.1:8000"
                            }
                        },
                    )
                    simulation.run()
                report = validate_run(simulation.output_dir, strict=True)
                self.assertTrue(report.valid, report.errors)
                message_lines = (
                    Path(simulation.output_dir) / "messages.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(message_lines), 2 if edge_policy == "full" else 0)


if __name__ == "__main__":
    unittest.main()
