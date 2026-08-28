import json

from engine.response_contracts import (
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    LEGACY_RESPONSE_CONTRACT_VERSION,
    validate_response_contract_version,
)


PHASE1_MESSAGE_PROMPT = """\
You are Agent {agent_id} in a 2D grid world.
The grid ranges from ({neg_s}, {neg_s}) to ({pos_s}, {pos_s}).
Your current position is ({x}, {y}).

Known locations:
{places_info}

{occupancy_info}\
{environment_section}\
{memory_section}\
{messages_section}\

Decide what message to send to nearby agents. You may send an empty string if you have nothing to say.

Respond with JSON only:
{{"message": "<your message text>", "reasoning": "<your internal reasoning>"}}\
"""

PHASE3_ACTION_PROMPT = """\
You are Agent {agent_id} in a 2D grid world.
The grid ranges from ({neg_s}, {neg_s}) to ({pos_s}, {pos_s}).
Your current position is ({x}, {y}).

Known locations:
{places_info}

{occupancy_info}\
{environment_section}\
{memory_section}\
{messages_section}\

Decide your next action. You can move one step in a cardinal direction or stay.

Respond with JSON only:
{{"action": "move" or "stay", "direction": "up" or "down" or "left" or "right", "memory": "<a note to your future self>", "reasoning": "<your internal reasoning>"}}\
"""

PHASE3_CANONICAL_ACTION_PROMPT = """\
You are Agent {agent_id} in a 2D grid world.
The grid ranges from ({neg_s}, {neg_s}) to ({pos_s}, {pos_s}).
Your current position is ({x}, {y}).

Known locations:
{places_info}

{occupancy_info}\
{environment_section}\
{memory_section}\
{messages_section}\

Decide your next action. You can move one step in a cardinal direction or stay.

Respond with JSON only. If action is "move", direction must be "up", "down", "left", or "right". If action is "stay", direction must be null:
{{"action": "move" or "stay", "direction": "up" or "down" or "left" or "right" or null, "memory": "<a note to your future self>", "reasoning": "<your internal reasoning>"}}\
"""


def format_places_info(places) -> str:
    lines = []
    for p in places:
        lines.append(
            f"- {p.name}: center ({p.center_x}, {p.center_y}), "
            f"extends from ({p.x_min}, {p.y_min}) to ({p.x_max}, {p.y_max})"
        )
    return "\n".join(lines) if lines else "None"


def format_occupancy_info(place, agent_count: int) -> str:
    if place is None:
        return ""
    return (
        f"You are inside {place.name}. "
        f"Current occupants: {agent_count}/{place.capacity}.\n"
    )


def format_memory_section(memories) -> str:
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\nYour recent memories:\n{lines}\n"


def format_messages_section(messages) -> str:
    if not messages:
        return ""
    lines = "\n".join(format_message(message) for message in messages)
    return f"\nRecent messages received:\n{lines}\n"


def format_message(message) -> str:
    if message.get("source_type") == "official_warning":
        payload = message["payload"]
        if isinstance(payload, dict):
            payload = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return (
            f"- Official environment warning {message['warning_id']}, "
            f"received at step {message['step']}: {payload}"
        )
    return f"- Agent {message['sender_id']}: {message['message']}"


def format_environment_section(step, x, y, hazard_rectangles, refuges) -> str:
    if step is None:
        return ""
    current_cell_hazardous = any(
        rectangle.contains(x, y) for rectangle in hazard_rectangles
    )
    refuge_lines = [
        f"{refuge.refuge_id}: x={refuge.rectangle.x_min}..{refuge.rectangle.x_max}, "
        f"y={refuge.rectangle.y_min}..{refuge.rectangle.y_max}"
        for refuge in refuges
    ]
    refuge_text = "; ".join(refuge_lines) if refuge_lines else "None"
    return (
        f"\nEnvironment state at step {step}:\n"
        "- Current cell hazard classification: "
        f"{'hazardous' if current_cell_hazardous else 'not hazardous'}\n"
        f"- Refuge rectangles: {refuge_text}\n"
    )


def build_phase1_prompt(agent_id, x, y, half_space_size,
                        places, place, agent_count,
                        memories, messages, step=None,
                        hazard_rectangles=(), refuges=()) -> str:
    return PHASE1_MESSAGE_PROMPT.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x, y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        environment_section=format_environment_section(
            step, x, y, hazard_rectangles, refuges
        ),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )


def build_phase3_prompt(agent_id, x, y, half_space_size,
                        places, place, agent_count,
                        memories, messages, step=None,
                        hazard_rectangles=(), refuges=(),
                        response_contract_version=LEGACY_RESPONSE_CONTRACT_VERSION) -> str:
    contract_version = validate_response_contract_version(
        response_contract_version
    )
    template = (
        PHASE3_CANONICAL_ACTION_PROMPT
        if contract_version == CANONICAL_RESPONSE_CONTRACT_VERSION
        else PHASE3_ACTION_PROMPT
    )
    return template.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x, y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        environment_section=format_environment_section(
            step, x, y, hazard_rectangles, refuges
        ),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )
