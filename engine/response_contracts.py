"""Versioned model-response contracts and vLLM response formats."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, Optional


LEGACY_RESPONSE_CONTRACT_VERSION = "phase-response-v1.0.0"
CANONICAL_RESPONSE_CONTRACT_VERSION = "phase-response-v2.0.0"
COMPACT_LR_RESPONSE_CONTRACT_VERSION = "phase-response-compact-lr-v1.0.0"
COMPACT_RL_RESPONSE_CONTRACT_VERSION = "phase-response-compact-rl-v1.0.0"
COMPACT_RESPONSE_CONTRACT_VERSIONS = frozenset({
    COMPACT_LR_RESPONSE_CONTRACT_VERSION,
    COMPACT_RL_RESPONSE_CONTRACT_VERSION,
})
SUPPORTED_RESPONSE_CONTRACT_VERSIONS = frozenset({
    LEGACY_RESPONSE_CONTRACT_VERSION,
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    *COMPACT_RESPONSE_CONTRACT_VERSIONS,
})

LEGACY_VLLM_TRANSPORT_CONTRACT_VERSION = (
    "vllm-openai-compatible-transport-v1.1.0"
)
PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION = (
    "vllm-openai-compatible-transport-v1.2.0"
)
COMPACT_VLLM_TRANSPORT_CONTRACT_VERSION = (
    "vllm-openai-compatible-transport-v1.3.0"
)

_PHASE1_OBJECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["message", "reasoning"],
    "additionalProperties": False,
}

_PHASE3_COMMON_PROPERTIES: Dict[str, Any] = {
    "memory": {"type": "string"},
    "reasoning": {"type": "string"},
}

_PHASE3_OBJECT_SCHEMA: Dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "action": {"enum": ["move"]},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                },
                **copy.deepcopy(_PHASE3_COMMON_PROPERTIES),
            },
            "required": ["action", "direction", "memory", "reasoning"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "action": {"enum": ["stay"]},
                "direction": {"type": "null"},
                **copy.deepcopy(_PHASE3_COMMON_PROPERTIES),
            },
            "required": ["action", "direction", "memory", "reasoning"],
            "additionalProperties": False,
        },
    ]
}

_CANONICAL_RESPONSE_FORMATS: Dict[str, Dict[str, Any]] = {
    "phase1": {
        "type": "json_schema",
        "json_schema": {
            "name": "mixed_cognition_phase1_v1",
            "strict": True,
            "schema": _PHASE1_OBJECT_SCHEMA,
        },
    },
    "phase3": {
        "type": "json_schema",
        "json_schema": {
            "name": "mixed_cognition_phase3_v1",
            "strict": True,
            "schema": _PHASE3_OBJECT_SCHEMA,
        },
    },
}


def _build_compact_response_formats(
    direction_order: list[str],
) -> Dict[str, Dict[str, Any]]:
    empty_reasoning = {"type": "string", "enum": [""]}
    phase1_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "reasoning": copy.deepcopy(empty_reasoning),
        },
        "required": ["message", "reasoning"],
        "additionalProperties": False,
    }
    phase3_common = {
        "memory": {"type": "string"},
        "reasoning": copy.deepcopy(empty_reasoning),
    }
    phase3_schema: Dict[str, Any] = {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "action": {"enum": ["move"]},
                    "direction": {
                        "type": "string",
                        "enum": list(direction_order),
                    },
                    **copy.deepcopy(phase3_common),
                },
                "required": ["action", "direction", "memory", "reasoning"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "action": {"enum": ["stay"]},
                    "direction": {"type": "null"},
                    **copy.deepcopy(phase3_common),
                },
                "required": ["action", "direction", "memory", "reasoning"],
                "additionalProperties": False,
            },
        ]
    }
    return {
        "phase1": {
            "type": "json_schema",
            "json_schema": {
                "name": "mixed_cognition_phase1_compact_v1",
                "strict": True,
                "schema": phase1_schema,
            },
        },
        "phase3": {
            "type": "json_schema",
            "json_schema": {
                "name": "mixed_cognition_phase3_compact_v1",
                "strict": True,
                "schema": phase3_schema,
            },
        },
    }


_RESPONSE_FORMATS_BY_VERSION: Dict[str, Dict[str, Dict[str, Any]]] = {
    CANONICAL_RESPONSE_CONTRACT_VERSION: _CANONICAL_RESPONSE_FORMATS,
    COMPACT_LR_RESPONSE_CONTRACT_VERSION: _build_compact_response_formats(
        ["up", "down", "left", "right"]
    ),
    COMPACT_RL_RESPONSE_CONTRACT_VERSION: _build_compact_response_formats(
        ["up", "down", "right", "left"]
    ),
}


def validate_response_contract_version(value: Any) -> str:
    if value is None:
        return LEGACY_RESPONSE_CONTRACT_VERSION
    if value not in SUPPORTED_RESPONSE_CONTRACT_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_RESPONSE_CONTRACT_VERSIONS))
        raise ValueError(
            "simulation.response_contract_version must be one of: " + supported
        )
    return value


def vllm_transport_contract_version(response_contract_version: str) -> str:
    version = validate_response_contract_version(response_contract_version)
    if version == CANONICAL_RESPONSE_CONTRACT_VERSION:
        return PHASE_AWARE_VLLM_TRANSPORT_CONTRACT_VERSION
    if version in COMPACT_RESPONSE_CONTRACT_VERSIONS:
        return COMPACT_VLLM_TRANSPORT_CONTRACT_VERSION
    return LEGACY_VLLM_TRANSPORT_CONTRACT_VERSION


def response_format_for_phase(
    response_contract_version: str,
    phase: str,
) -> Optional[Dict[str, Any]]:
    version = validate_response_contract_version(response_contract_version)
    if phase not in {"phase1", "phase3"}:
        raise ValueError(f"unsupported response-contract phase: {phase!r}")
    if version == LEGACY_RESPONSE_CONTRACT_VERSION:
        return None
    return copy.deepcopy(_RESPONSE_FORMATS_BY_VERSION[version][phase])


def response_schema_sha256(response_contract_version: str) -> Optional[str]:
    version = validate_response_contract_version(response_contract_version)
    if version == LEGACY_RESPONSE_CONTRACT_VERSION:
        return None
    payload = json.dumps(
        _RESPONSE_FORMATS_BY_VERSION[version],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_phase_response_format(value: Any) -> Optional[Dict[str, Any]]:
    """Accept only an exact repository-owned phase response format."""
    if value is None:
        return None
    if not any(
        value == candidate
        for formats in _RESPONSE_FORMATS_BY_VERSION.values()
        for candidate in formats.values()
    ):
        raise ValueError(
            "phase_response_format must match an exact versioned phase schema"
        )
    return copy.deepcopy(value)


def validate_parsed_response(
    parsed: Dict[str, Any],
    phase: str,
    response_contract_version: str = LEGACY_RESPONSE_CONTRACT_VERSION,
) -> None:
    """Validate parsed content against the selected phase response contract."""
    version = validate_response_contract_version(response_contract_version)
    if phase == "phase1":
        expected = {"message", "reasoning"}
        if set(parsed) != expected:
            raise ValueError(
                "Phase 1 response fields must be exactly message and reasoning"
            )
        if not all(isinstance(parsed[key], str) for key in expected):
            raise ValueError("Phase 1 response fields must be strings")
        if (
            version in COMPACT_RESPONSE_CONTRACT_VERSIONS
            and parsed["reasoning"] != ""
        ):
            raise ValueError(
                "Phase 1 reasoning must be empty under the compact contract"
            )
        return
    if phase != "phase3":
        raise ValueError(f"unsupported response-contract phase: {phase!r}")

    expected = {"action", "direction", "memory", "reasoning"}
    if set(parsed) != expected:
        raise ValueError(
            "Phase 3 response fields must be exactly action, direction, memory, reasoning"
        )
    if not all(
        isinstance(parsed[key], str)
        for key in ("action", "memory", "reasoning")
    ):
        raise ValueError(
            "Phase 3 action, memory, and reasoning must be strings"
        )
    action = parsed["action"]
    direction = parsed["direction"]
    if action not in {"move", "stay"}:
        raise ValueError("Phase 3 action must be move or stay")
    if action == "move" and direction not in {"up", "down", "left", "right"}:
        raise ValueError("Phase 3 move direction must be cardinal")
    if (
        version
        in {
            CANONICAL_RESPONSE_CONTRACT_VERSION,
            *COMPACT_RESPONSE_CONTRACT_VERSIONS,
        }
        and action == "stay"
        and direction is not None
    ):
        raise ValueError(
            "Phase 3 stay direction must be null under phase-response-v2.0.0"
        )
    if (
        version in COMPACT_RESPONSE_CONTRACT_VERSIONS
        and parsed["reasoning"] != ""
    ):
        raise ValueError(
            "Phase 3 reasoning must be empty under the compact contract"
        )
    if (
        version == LEGACY_RESPONSE_CONTRACT_VERSION
        and action == "stay"
        and direction not in {None, "", "up", "down", "left", "right"}
    ):
        raise ValueError("Phase 3 stay direction must be empty or cardinal")
