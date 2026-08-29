"""Versioned prompt, transport, and response-failure execution policies."""

from __future__ import annotations

from typing import Any


CURRENT_PROMPT_CONTRACT_VERSION = "current-prompts-v2.0.0"
JAPANESE_PROMPT_CONTRACT_VERSION = "japanese-prompts-v1.0.0"
LEGACY_PROMPT_CONTRACT_VERSION = "legacy-prompts-v1.0.0"
SUPPORTED_PROMPT_CONTRACT_VERSIONS = frozenset({
    CURRENT_PROMPT_CONTRACT_VERSION,
    JAPANESE_PROMPT_CONTRACT_VERSION,
    LEGACY_PROMPT_CONTRACT_VERSION,
})

CURRENT_TRANSPORT_BEHAVIOR_VERSION = "single-generation-strict-json-v2.0.0"
LEGACY_TRANSPORT_BEHAVIOR_VERSION = "legacy-subobject-generation-retry-v1.0.0"
SUPPORTED_TRANSPORT_BEHAVIOR_VERSIONS = frozenset({
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    LEGACY_TRANSPORT_BEHAVIOR_VERSION,
})

ABORT_RUN_RESPONSE_FAILURE_POLICY = "abort_run"
RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY = "record_and_continue"
SUPPORTED_RESPONSE_FAILURE_POLICIES = frozenset({
    ABORT_RUN_RESPONSE_FAILURE_POLICY,
    RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
})


def _validate(value: Any, supported: frozenset[str], label: str, default: str) -> str:
    if value is None:
        return default
    if value not in supported:
        raise ValueError(
            f"simulation.{label} must be one of: " + ", ".join(sorted(supported))
        )
    return value


def validate_prompt_contract_version(value: Any) -> str:
    return _validate(
        value,
        SUPPORTED_PROMPT_CONTRACT_VERSIONS,
        "prompt_contract_version",
        CURRENT_PROMPT_CONTRACT_VERSION,
    )


def validate_transport_behavior_version(value: Any) -> str:
    return _validate(
        value,
        SUPPORTED_TRANSPORT_BEHAVIOR_VERSIONS,
        "transport_behavior_version",
        CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    )


def validate_response_failure_policy(value: Any) -> str:
    return _validate(
        value,
        SUPPORTED_RESPONSE_FAILURE_POLICIES,
        "response_failure_policy",
        ABORT_RUN_RESPONSE_FAILURE_POLICY,
    )
