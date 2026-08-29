PHASE1_MESSAGE_PROMPT = """\
You are Agent {agent_id} in a 2D grid world.
The grid ranges from ({neg_s}, {neg_s}) to ({pos_s}, {pos_s}).
Your current position is ({x}, {y}).

Known locations:
{places_info}

{occupancy_info}\
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
{memory_section}\
{messages_section}\

Decide your next action. You can move one step in a cardinal direction or stay.

Respond with JSON only:
{{"action": "move" or "stay", "direction": "up" or "down" or "left" or "right", "memory": "<a note to your future self>", "reasoning": "<your internal reasoning>"}}\
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
    lines = "\n".join(
        f"- Agent {m['sender_id']}: {m['message']}" for m in messages
    )
    return f"\nRecent messages received:\n{lines}\n"


def build_phase1_prompt(agent_id, x, y, half_space_size,
                        places, place, agent_count,
                        memories, messages) -> str:
    return PHASE1_MESSAGE_PROMPT.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x, y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )


def build_phase3_prompt(agent_id, x, y, half_space_size,
                        places, place, agent_count,
                        memories, messages) -> str:
    return PHASE3_ACTION_PROMPT.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x, y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )
