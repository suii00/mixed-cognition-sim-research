"""Versioned received-message selection and optional input evidence contracts."""

from typing import Any


RECENT_MESSAGE_SELECTION_POLICY = "recent-v1.0.0"
RETAIN_OFFICIAL_WARNING_SELECTION_POLICY = "retain-official-warning-v1.0.0"
MESSAGE_SELECTION_POLICIES = frozenset({
    RECENT_MESSAGE_SELECTION_POLICY,
    RETAIN_OFFICIAL_WARNING_SELECTION_POLICY,
})
MESSAGE_PRESENTATION_VERSION = "message-presentation-v1.0.0"
PROMPT_INPUTS_FILE = "prompt_inputs.jsonl"


def validate_message_selection_policy(value: Any) -> str:
    if not isinstance(value, str) or value not in MESSAGE_SELECTION_POLICIES:
        raise ValueError(
            "agents.message_selection_policy must be one of: "
            + ", ".join(sorted(MESSAGE_SELECTION_POLICIES))
        )
    return value


def validate_retention_limits(
    policy: str, message_history_limit: Any, message_context_size: Any
) -> None:
    # Leave omitted/explicit recent selection's historical slicing unchanged.
    if policy != RETAIN_OFFICIAL_WARNING_SELECTION_POLICY:
        return
    for field, value in (
        ("message_history_limit", message_history_limit),
        ("message_context_size", message_context_size),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"agents.{field} must be a positive integer for retention")


def validate_input_observability_version(value: Any) -> str | None:
    if value is not None and value != MESSAGE_PRESENTATION_VERSION:
        raise ValueError(
            "simulation.input_observability_version must be "
            f"'{MESSAGE_PRESENTATION_VERSION}' or absent"
        )
    return value
