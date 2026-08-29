#!/usr/bin/env python3
"""Build the standalone public configs for the ten historical replay attempts."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import load_config  # noqa: E402
from tools.run_public_vllm import required_gpu_count  # noqa: E402


CONFIG_DIR = REPO_ROOT / "configs" / "legacy_reproduction_v1"
MANIFEST_PATH = CONFIG_DIR / "manifest.json"
PROTOCOL_VERSION = "legacy-reproduction-v1.0.0"
PROMPT_CONTRACT_VERSION = "legacy-prompts-v1.0.0"
PROMPT_SHA256 = "f414ab30a963636d80239644c2d3770672c77d5b8bdde027de2eb15a0d08bc3d"
TRANSPORT_BEHAVIOR_VERSION = "legacy-subobject-generation-retry-v1.0.0"
RESPONSE_CONTRACT_VERSION = "phase-response-v1.0.0"
LOG_SCHEMA_VERSION = "2.0.0"
METRIC_VERSION = "metric-v2.0.0"
GPU_CEILING = 6

AGENTS_WIDE = {
    "communication_radius": 100,
    "edge_policy": "full",
    "memory_limit": 20,
    "memory_size": 5,
    "message_context_size": 3,
    "message_history_limit": 10,
}
AGENTS_NARROW = {**AGENTS_WIDE, "communication_radius": 5}
PILOT_PLACES = [
    {
        "name": "left_bar",
        "center_x": -15,
        "center_y": 0,
        "half_size": 5,
        "capacity": 6,
    },
    {
        "name": "right_bar",
        "center_x": 15,
        "center_y": 0,
        "half_size": 5,
        "capacity": 5,
    },
]

VLLM_MODELS: Mapping[str, Mapping[str, Any]] = {
    "qwen": {
        "backend_version": "0.27.1",
        "chat_template": "tokenizer_config.json:chat_template:sha256:cd8e9439f0570856fd70470bf8889ebd8b5d1107207f67a5efb46e342330527f",
        "data_parallel_size": 1,
        "dtype": "bfloat16",
        "flashinfer_mode": "disabled",
        "generation_config": "vllm",
        "gpu_memory_utilization": 0.9,
        "max_model_len": 4096,
        "model": "qwen2.5-7b-instruct",
        "model_digest": "a09a35458c702b33eeacc393d103063234e8bc28",
        "model_source": "Qwen/Qwen2.5-7B-Instruct",
        "name": "qwen",
        "provider": "vllm",
        "quantization": "none",
        "tensor_parallel_size": 1,
        "tokenizer_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
    },
    "llama": {
        "backend_version": "0.27.1",
        "chat_template": "tokenizer_config.json:chat_template:sha256:e10ca381b1ccc5cf9db52e371f3b6651576caee0a630b452e2816b2d404d4b65",
        "data_parallel_size": 1,
        "dtype": "bfloat16",
        "flashinfer_mode": "disabled",
        "generation_config": "vllm",
        "gpu_memory_utilization": 0.9,
        "max_model_len": 4096,
        "model": "llama-3.1-8b-instruct",
        "model_digest": "0e9e39f249a16976918f6564b8830bc894c89659",
        "model_source": "meta-llama/Llama-3.1-8B-Instruct",
        "name": "llama",
        "provider": "vllm",
        "quantization": "none",
        "tensor_parallel_size": 1,
        "tokenizer_revision": "0e9e39f249a16976918f6564b8830bc894c89659",
    },
    "gemma": {
        "backend_version": "0.27.1",
        "chat_template": "tokenizer_config.json:chat_template:sha256:ecd6ae513fe103f0eb62e8ab5bfa8d0fe45c1074fa398b089c93a7e70c15cfd6",
        "data_parallel_size": 1,
        "dtype": "bfloat16",
        "flashinfer_mode": "disabled",
        "generation_config": "vllm",
        # A fresh per-run compile cache raises the observed profiling peak.
        # 0.95 is the source r004 value that retained context 4096 on A5000;
        # 0.92 succeeded only after a persistent AOT cache was reused.
        "gpu_memory_utilization": 0.95,
        "max_model_len": 4096,
        "model": "gemma-2-9b-it",
        "model_digest": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
        "model_source": "google/gemma-2-9b-it",
        "name": "gemma",
        "provider": "vllm",
        "quantization": "none",
        "tensor_parallel_size": 1,
        "tokenizer_revision": "11c9b309abf73637e4b6f9a3fa1e92e615547819",
    },
}

OLLAMA_MODELS: Mapping[str, Mapping[str, Any]] = {
    "alpha": {
        "backend_version": "0.32.13",
        "chat_template": "sha256:eb4402837c7829a690fa845de4d7f3fd842c2adee476d5341da8a46ea9255175",
        "model": "qwen2.5:7b-instruct-fp16",
        "model_digest": "59805ce4a4046be2d8f63231a78daacd2e66f5dccf1a64d0d138ebeeb26ff16c",
        "name": "alpha",
        "provider": "ollama",
        "quantization": "F16",
    },
    "beta": {
        "backend_version": "0.32.13",
        "chat_template": "sha256:109037bec39c0becc8221222ae23557559bc594290945a2c4221ab4f303b8871",
        "model": "gemma2:9b-instruct-fp16",
        "model_digest": "28e6684b085085f78551db7c96a9daa546161b1da9d055ea01b84cb1163013cf",
        "name": "beta",
        "provider": "ollama",
        "quantization": "F16",
    },
    "neutral": {
        "backend_version": "0.32.13",
        "chat_template": "sha256:948af2743fc78a328dcb3b0f5a31b3d75f415840fdb699e8b1235978392ecf85",
        "model": "llama3.1:8b-instruct-fp16",
        "model_digest": "4aacac4194543ff7f70dab3f2ebc169c132d5319bb36f7a7e99c4ff525ebcc09",
        "name": "neutral",
        "provider": "ollama",
        "quantization": "F16",
    },
}


CASES: tuple[dict[str, Any], ...] = (
    {
        "source_run_id": "vllm-llama1x1-20260823-r001",
        "source_git_sha": "81e3cc1b43ac7a6bab9be8160d4d23dffb4186db",
        "source_config_sha256": "d90504e0ecbdebf8e091057a4ddffb66d26f5aa7f65b8ea538dcaf51489459dc",
        "source_protocol_version": "vllm-adapter-engineering-smoke-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 1,
        "provider": "vllm",
        "seed": 42,
        "duration": 1,
        "agents": AGENTS_NARROW,
        "counts": {"llama": 1},
        "max_concurrency": 1,
        "json_object": False,
        "endpoints": {"llama": 1},
    },
    {
        "source_run_id": "het12x1-ollama-20260819-r001",
        "source_git_sha": "ead21b41c327f2a2c726370a3e50131239cdba65",
        "source_config_sha256": "dee185f3c7b5e319a2f2b0d514f0c87b483c43a90eb37c171e83e35cdc5de8dc",
        "source_protocol_version": "engineering-smoke-het12x1-v1.0.0",
        "source_log_schema_version": "1.0.0",
        "source_status": "completed",
        "source_completed_steps": 1,
        "provider": "ollama",
        "seed": 42,
        "duration": 1,
        "agents": AGENTS_WIDE,
        "counts": {"alpha": 4, "beta": 4, "neutral": 4},
        "max_concurrency": 1,
        "temperature": 0.2,
        "places": [],
    },
    {
        "source_run_id": "pilot-het12x60-20260823-s1002-r002",
        "source_git_sha": "6af938a19570cfb904df97433bdf9ea8a894d818",
        "source_config_sha256": "de066432d80f2c8e030d5597e2ac8cd7ec9a2de618e37f6edc4e2904fd5fa141",
        "source_protocol_version": "pilot-het12x60-v1.1.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "aborted",
        "source_completed_steps": 1,
        "provider": "ollama",
        "seed": 1002,
        "duration": 60,
        "agents": AGENTS_NARROW,
        "counts": {"alpha": 4, "beta": 4, "neutral": 4},
        "max_concurrency": 1,
        "temperature": 0.2,
        "places": PILOT_PLACES,
    },
    {
        "source_run_id": "vllm-3model3x60-20260823-r004",
        "source_git_sha": "a9ec0391d8e46a70f8c301910bbdebd76e5f2610",
        "source_config_sha256": "e098c9c9876c4a24e535d8628198ea4654a50b118fde93490431f4c0a0a44586",
        "source_protocol_version": "vllm-3model3x60-engineering-smoke-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 1, "llama": 1, "gemma": 1},
        "max_concurrency": 3,
        "json_object": False,
        "endpoints": {"qwen": 1, "llama": 1, "gemma": 1},
    },
    {
        "source_run_id": "vllm-3model24x60-3gpu-20260823-r001",
        "source_git_sha": "1d0e4969c23cd0255ae89937b7ab0580d03288a2",
        "source_config_sha256": "1c07c859171b978fb92e2dfafe6ee16b8c39b9f5bf2315d32ac1012e3b693145",
        "source_protocol_version": "vllm-3model24x60-3gpu-scale-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": False,
        "endpoints": {"qwen": 1, "llama": 1, "gemma": 1},
    },
    {
        "source_run_id": "vllm-3model24x60-3gpu-json-20260823-r002",
        "source_git_sha": "0223cebc34da0b284238f3f74159473c0a5e0b0a",
        "source_config_sha256": "ba4c25c62d27e97097ac858327b4c3a129d549a0b3fa480c765c158a34a96472",
        "source_protocol_version": "vllm-3model24x60-3gpu-json-r002-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": True,
        "endpoints": {"qwen": 1, "llama": 1, "gemma": 1},
    },
    {
        "source_run_id": "vllm-3model24x60-7gpu-20260823-r002",
        "source_git_sha": "1fb3b14f86223f1570314d9ce4b567cfcada1c50",
        "source_config_sha256": "cc23d097e09b557bc203837ad8734f4277de6a23dfeeac577adcba7699715c94",
        "source_protocol_version": "vllm-3model24x60-7gpu-scale-r002-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "aborted",
        "source_completed_steps": 21,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": False,
        "endpoints": {"qwen": 2, "llama": 2, "gemma": 3},
    },
    {
        "source_run_id": "vllm-3model24x60-7gpu-json-20260823-r003",
        "source_git_sha": "0ddeec5541be590bcc9dc7b0c022498e5e6afc56",
        "source_config_sha256": "eda356c316823bf22636106b3a10ac3404c96542d494ca9e5a0242cbf2fed8ef",
        "source_protocol_version": "vllm-3model24x60-7gpu-json-r003-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": True,
        "endpoints": {"qwen": 2, "llama": 2, "gemma": 3},
    },
    {
        "source_run_id": "vllm-dual-worker-a-24x60-20260823-r001",
        "source_git_sha": "cf24ccf0b7bf0fac0f7fa92917c1967038ebdb1b",
        "source_config_sha256": "b0313456a72214dfe2c00b6019dc71414d537aa6d60c1d791d5471dba5cb921b",
        "source_protocol_version": "dual-worker-engineering-smoke-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": True,
        "endpoints": {"qwen": 1, "llama": 1, "gemma": 1},
        "worker_identity": "a",
    },
    {
        "source_run_id": "vllm-dual-worker-b-24x60-20260823-r001",
        "source_git_sha": "cf24ccf0b7bf0fac0f7fa92917c1967038ebdb1b",
        "source_config_sha256": "b43518f5b83f75bc46559a06dd3f8e2a559aef61df90b74a25ca6d5ebbd4a56f",
        "source_protocol_version": "dual-worker-engineering-smoke-v1.0.0",
        "source_log_schema_version": "1.1.0",
        "source_status": "completed",
        "source_completed_steps": 60,
        "provider": "vllm",
        "seed": 42,
        "duration": 60,
        "agents": AGENTS_WIDE,
        "counts": {"qwen": 8, "llama": 8, "gemma": 8},
        "max_concurrency": 24,
        "json_object": True,
        "endpoints": {"qwen": 1, "llama": 1, "gemma": 1},
        "worker_identity": "b",
    },
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _vllm_blocs(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocs = []
    for model_name, count in case["counts"].items():
        bloc = copy.deepcopy(VLLM_MODELS[model_name])
        bloc["num_agents"] = count
        endpoint_count = case["endpoints"][model_name]
        prefix = f"legacy-{case['source_run_id']}-{model_name}"
        if endpoint_count == 1:
            bloc["endpoint_id"] = prefix
            bloc["device_slot"] = f"{prefix}-device"
        else:
            bloc["endpoint_assignment_policy"] = "round_robin_by_bloc_ordinal_v1"
            bloc["endpoint_pool"] = [
                {
                    "endpoint_id": f"{prefix}-{ordinal}",
                    "device_slot": f"{prefix}-device-{ordinal}",
                }
                for ordinal in range(endpoint_count)
            ]
        if case["json_object"]:
            bloc["llm_overrides"] = {"response_format": {"type": "json_object"}}
        blocs.append(bloc)
    return blocs


def _ollama_blocs(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    blocs = []
    for model_name, count in case["counts"].items():
        bloc = copy.deepcopy(OLLAMA_MODELS[model_name])
        bloc["num_agents"] = count
        bloc["endpoint_id"] = f"legacy-{case['source_run_id']}-{model_name}"
        bloc["device_slot"] = f"legacy-{case['source_run_id']}-{model_name}-device"
        blocs.append(bloc)
    return blocs


def build_config(case: Mapping[str, Any]) -> dict[str, Any]:
    provider = case["provider"]
    blocs = _vllm_blocs(case) if provider == "vllm" else _ollama_blocs(case)
    config = {
        "agents": copy.deepcopy(case["agents"]),
        "blocs": blocs,
        "llm_defaults": {
            "max_concurrency": case["max_concurrency"],
            "max_tokens": 256,
            "temperature": case.get("temperature", 0.0),
            "timeout_s": 120,
        },
        "places": copy.deepcopy(case.get("places", [])),
        "reproduction": {
            "source_completed_steps": case["source_completed_steps"],
            "source_config_sha256": case["source_config_sha256"],
            "source_git_sha": case["source_git_sha"],
            "source_log_schema_version": case["source_log_schema_version"],
            "source_prompt_sha256": PROMPT_SHA256,
            "source_protocol_version": case["source_protocol_version"],
            "source_run_id": case["source_run_id"],
            "source_status": case["source_status"],
        },
        "simulation": {
            "duration": case["duration"],
            "execution_mode": (
                "vllm_openai_compatible" if provider == "vllm" else "ollama_native"
            ),
            "failure_thresholds": {
                "schema_validation_failures": 0,
                "syntax_parse_failures": 0,
                "transport_failures": 0,
            },
            "half_space_size": 25,
            "log_schema_version": LOG_SCHEMA_VERSION,
            "metric_version": METRIC_VERSION,
            "prompt_contract_version": PROMPT_CONTRACT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "research_eligible": False,
            "response_contract_version": RESPONSE_CONTRACT_VERSION,
            "response_failure_policy": "record_and_continue",
            "run_name": f"legacy-reproduction-{case['source_run_id']}",
            "seed": case["seed"],
            "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
        },
    }
    return config


def build_outputs() -> dict[Path, bytes]:
    prompt_bytes = (REPO_ROOT / "engine" / "legacy_prompts_v1.py").read_bytes()
    if hashlib.sha256(prompt_bytes).hexdigest() != PROMPT_SHA256:
        raise ValueError("legacy prompt module differs from the historical prompt hash")

    outputs: dict[Path, bytes] = {}
    rows = []
    for case in CASES:
        config = build_config(case)
        filename = f"legacy-reproduction-{case['source_run_id']}.json"
        path = CONFIG_DIR / filename
        outputs[path] = _json_bytes(config)
        loaded = load_config_from_value(config)
        gpu_count = (
            required_gpu_count(loaded) if case["provider"] == "vllm" else 3
        )
        logical_calls = (
            case["duration"] * sum(case["counts"].values()) * 2
        )
        rows.append({
            "config": path.relative_to(REPO_ROOT).as_posix(),
            "execution_scope": (
                "authorized_up_to_6_gpus"
                if gpu_count <= GPU_CEILING
                else "requires_separate_7_gpu_approval"
            ),
            "logical_llm_calls": logical_calls,
            "provider": case["provider"],
            "required_gpu_count": gpu_count,
            "seed": case["seed"],
            "source_run_id": case["source_run_id"],
        })
    manifest = {
        "authorized_gpu_ceiling": GPU_CEILING,
        "log_schema_version": LOG_SCHEMA_VERSION,
        "planned_attempts": len(rows),
        "planned_logical_llm_calls": sum(row["logical_llm_calls"] for row in rows),
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
        "protocol_version": PROTOCOL_VERSION,
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "rows": rows,
        "runnable_attempts_under_current_approval": sum(
            row["required_gpu_count"] <= GPU_CEILING for row in rows
        ),
        "runnable_logical_llm_calls_under_current_approval": sum(
            row["logical_llm_calls"]
            for row in rows
            if row["required_gpu_count"] <= GPU_CEILING
        ),
        "schema_version": "legacy-reproduction-manifest-v1.0.0",
        "transport_behavior_version": TRANSPORT_BEHAVIOR_VERSION,
    }
    outputs[MANIFEST_PATH] = _json_bytes(manifest)
    return outputs


def load_config_from_value(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate generated bytes through the production loader without a temp file."""
    from engine.config import build_effective_config

    return build_effective_config(dict(value))


def write_or_check(outputs: Mapping[Path, bytes], check: bool) -> None:
    expected_paths = set(outputs)
    if check:
        existing_paths = set(CONFIG_DIR.glob("*.json")) if CONFIG_DIR.is_dir() else set()
        if existing_paths != expected_paths:
            raise ValueError("legacy reproduction config file set differs from builder")
        for path, content in outputs.items():
            if path.read_bytes() != content:
                raise ValueError(f"generated file differs: {path.relative_to(REPO_ROOT)}")
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(CONFIG_DIR.glob("*.json")):
        if path not in expected_paths:
            path.unlink()
    for path, content in outputs.items():
        path.write_bytes(content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        write_or_check(build_outputs(), args.check)
    except (OSError, TypeError, ValueError) as error:
        print(f"FAIL: {error}")
        return 1
    print("PASS: legacy reproduction configs are deterministic and valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
