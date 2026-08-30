"""Compact Japanese prompt contract with right before left presentation."""

import json

from engine.response_contracts import (
    COMPACT_RL_RESPONSE_CONTRACT_VERSION,
    LEGACY_RESPONSE_CONTRACT_VERSION,
    validate_response_contract_version,
)


PHASE1_MESSAGE_PROMPT = """\
あなたは2次元グリッド世界のエージェント{agent_id}です。
グリッドの範囲は({neg_s}, {neg_s})から({pos_s}, {pos_s})です。
現在位置は({x}, {y})です。

既知の場所:
{places_info}

{occupancy_info}\
{environment_section}\
{memory_section}\
{messages_section}\

近くのエージェントへ送るメッセージを決めてください。伝えることがなければ空文字列にできます。
`message`は重複を避けた80文字以内の簡潔な日本語とし、`reasoning`は必ず空文字列にしてください。
JSONのキーは次の英字表記をそのまま使用し、JSON object以外は出力しないでください。

{{"message": "<日本語のメッセージ>", "reasoning": ""}}\
"""

PHASE3_ACTION_PROMPT = """\
あなたは2次元グリッド世界のエージェント{agent_id}です。
グリッドの範囲は({neg_s}, {neg_s})から({pos_s}, {pos_s})です。
現在位置は({x}, {y})です。

既知の場所:
{places_info}

{occupancy_info}\
{environment_section}\
{memory_section}\
{messages_section}\

次の候補から行動を1つ決めてください。候補の表示順は次のとおりです。
1. `action`="move", `direction`="up"（上へ1マス）
2. `action`="move", `direction`="down"（下へ1マス）
3. `action`="move", `direction`="right"（右へ1マス）
4. `action`="move", `direction`="left"（左へ1マス）
5. `action`="stay", `direction`=null（その場に留まる）

`memory`は将来の自分に必要な場合だけ、重複を避けた80文字以内の簡潔な日本語にしてください。不要なら空文字列にできます。
`reasoning`は必ず空文字列にしてください。キーは`action`、`direction`、`memory`、`reasoning`だけとし、JSON object以外は出力しないでください。\
"""


def format_places_info(places) -> str:
    lines = []
    for place in places:
        lines.append(
            f"- {place.name}: 中心({place.center_x}, {place.center_y})、"
            f"範囲({place.x_min}, {place.y_min})から"
            f"({place.x_max}, {place.y_max})"
        )
    return "\n".join(lines) if lines else "なし"


def format_occupancy_info(place, agent_count: int) -> str:
    if place is None:
        return ""
    return (
        f"あなたは{place.name}の内部にいます。"
        f"現在の滞在者数: {agent_count}/{place.capacity}。\n"
    )


def format_memory_section(memories) -> str:
    if not memories:
        return ""
    lines = "\n".join(f"- {memory}" for memory in memories)
    return f"\n最近の記憶:\n{lines}\n"


def format_messages_section(messages) -> str:
    if not messages:
        return ""
    lines = "\n".join(format_message(message) for message in messages)
    return f"\n最近受信したメッセージ:\n{lines}\n"


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
            f"- 公式の環境警報 {message['warning_id']}、"
            f"ステップ{message['step']}で受信: {payload}"
        )
    return f"- エージェント{message['sender_id']}: {message['message']}"


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
    refuge_text = "; ".join(refuge_lines) if refuge_lines else "なし"
    return (
        f"\nステップ{step}の環境状態:\n"
        "- 現在セルの危険判定: "
        f"{'危険' if current_cell_hazardous else '危険ではない'}\n"
        f"- 避難所の矩形: {refuge_text}\n"
    )


def build_phase1_prompt(
    agent_id,
    x,
    y,
    half_space_size,
    places,
    place,
    agent_count,
    memories,
    messages,
    step=None,
    hazard_rectangles=(),
    refuges=(),
) -> str:
    return PHASE1_MESSAGE_PROMPT.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x,
        y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        environment_section=format_environment_section(
            step, x, y, hazard_rectangles, refuges
        ),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )


def build_phase3_prompt(
    agent_id,
    x,
    y,
    half_space_size,
    places,
    place,
    agent_count,
    memories,
    messages,
    step=None,
    hazard_rectangles=(),
    refuges=(),
    response_contract_version=LEGACY_RESPONSE_CONTRACT_VERSION,
) -> str:
    contract_version = validate_response_contract_version(
        response_contract_version
    )
    if contract_version != COMPACT_RL_RESPONSE_CONTRACT_VERSION:
        raise ValueError(
            "right-before-left compact prompt requires its paired response contract"
        )
    return PHASE3_ACTION_PROMPT.format(
        agent_id=agent_id,
        neg_s=-half_space_size,
        pos_s=half_space_size,
        x=x,
        y=y,
        places_info=format_places_info(places),
        occupancy_info=format_occupancy_info(place, agent_count),
        environment_section=format_environment_section(
            step, x, y, hazard_rectangles, refuges
        ),
        memory_section=format_memory_section(memories),
        messages_section=format_messages_section(messages),
    )
