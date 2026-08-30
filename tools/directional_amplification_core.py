"""Deterministic builder and metric core for the direction-amplification audit."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import (  # noqa: E402
    build_effective_config,
    validate_public_config_boundary,
)
from engine.provenance import normalize_run_id  # noqa: E402


PLAN_SCHEMA_VERSION = "directional-amplification-audit-plan-v1.0.0"
MANIFEST_SCHEMA_VERSION = "directional-amplification-audit-manifest-v1.0.0"
RESULT_SCHEMA_VERSION = "directional-amplification-audit-result-v1.0.0"


class DirectionalAuditError(ValueError):
    """The audit plan, generated bundle, or raw run is inconsistent."""


class DuplicateJsonKeyError(DirectionalAuditError):
    """A JSON document contains a duplicate object member."""


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_unique(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DirectionalAuditError(f"cannot load JSON object: {path}") from error
    if not isinstance(value, dict):
        raise DirectionalAuditError(f"JSON root must be an object: {path}")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        unknown = sorted(observed - expected)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise DirectionalAuditError(f"{label} fields differ ({'; '.join(details)})")


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise DirectionalAuditError(f"{label} must be an integer >= {minimum}")
    return value


def load_and_validate_plan(plan_path: Path) -> Dict[str, Any]:
    plan = load_json_unique(plan_path)
    _require_exact_keys(
        plan,
        {
            "schema_version",
            "audit_id",
            "created_at",
            "protocol_version",
            "metric_version",
            "research_eligible",
            "source",
            "run_identity",
            "frozen",
            "message_context_conditions",
            "rotations",
            "decision_rules",
            "analysis_restrictions",
        },
        "plan",
    )
    if plan["schema_version"] != PLAN_SCHEMA_VERSION:
        raise DirectionalAuditError("unsupported audit plan schema")
    if plan["research_eligible"] is not False:
        raise DirectionalAuditError("engineering audit must remain research ineligible")
    for key in ("audit_id", "created_at", "protocol_version", "metric_version"):
        if not isinstance(plan[key], str) or not plan[key]:
            raise DirectionalAuditError(f"plan.{key} must be non-empty")

    source = plan["source"]
    _require_exact_keys(
        source,
        {
            "base_config",
            "base_config_sha256",
            "source_run_id",
            "source_run_tree_sha256",
        },
        "plan.source",
    )
    base_relative = Path(source["base_config"])
    if base_relative.is_absolute() or ".." in base_relative.parts:
        raise DirectionalAuditError("source base_config must be repository relative")
    for key in ("base_config_sha256", "source_run_tree_sha256"):
        value = source[key]
        if not isinstance(value, str) or len(value) != 64:
            raise DirectionalAuditError(f"plan.source.{key} must be SHA-256")

    identity = plan["run_identity"]
    _require_exact_keys(identity, {"prefix", "date_tag", "replicate_id"}, "run_identity")
    for key in identity:
        if not isinstance(identity[key], str) or not identity[key]:
            raise DirectionalAuditError(f"run_identity.{key} must be non-empty")

    frozen = plan["frozen"]
    _require_exact_keys(
        frozen,
        {
            "seed",
            "duration",
            "expected_agents",
            "expected_calls_per_run",
            "expected_total_runs",
            "expected_total_calls",
            "temperature",
            "prompt_contract_version",
            "response_contract_version",
            "transport_behavior_version",
            "edge_policy",
            "communication_radius",
            "memory_limit",
            "memory_size",
        },
        "plan.frozen",
    )
    for key in (
        "seed",
        "duration",
        "expected_agents",
        "expected_calls_per_run",
        "expected_total_runs",
        "expected_total_calls",
        "communication_radius",
        "memory_limit",
        "memory_size",
    ):
        _require_int(frozen[key], f"plan.frozen.{key}")
    if not isinstance(frozen["temperature"], (int, float)):
        raise DirectionalAuditError("plan.frozen.temperature must be numeric")

    contexts = plan["message_context_conditions"]
    rotations = plan["rotations"]
    if not isinstance(contexts, list) or not isinstance(rotations, list):
        raise DirectionalAuditError("context conditions and rotations must be arrays")
    if len(contexts) * len(rotations) != frozen["expected_total_runs"]:
        raise DirectionalAuditError("factor cross product differs from expected_total_runs")
    context_ids = []
    for index, condition in enumerate(contexts):
        if not isinstance(condition, dict):
            raise DirectionalAuditError(f"context condition {index} must be an object")
        _require_exact_keys(
            condition,
            {"condition_id", "message_history_limit", "message_context_size"},
            f"context condition {index}",
        )
        context_ids.append(condition["condition_id"])
        history_limit = _require_int(
            condition["message_history_limit"],
            f"context condition {index}.message_history_limit",
            1,
        )
        context_size = _require_int(
            condition["message_context_size"],
            f"context condition {index}.message_context_size",
            1,
        )
        if context_size > history_limit:
            raise DirectionalAuditError("message context cannot exceed history limit")
    if len(set(context_ids)) != len(context_ids):
        raise DirectionalAuditError("context condition IDs must be unique")

    rotation_ids = []
    expected_blocs = {"qwen", "swallow", "elyza"}
    for index, rotation in enumerate(rotations):
        if not isinstance(rotation, dict):
            raise DirectionalAuditError(f"rotation {index} must be an object")
        _require_exact_keys(rotation, {"rotation_id", "bloc_order"}, f"rotation {index}")
        rotation_ids.append(rotation["rotation_id"])
        if (
            not isinstance(rotation["bloc_order"], list)
            or len(rotation["bloc_order"]) != 3
            or set(rotation["bloc_order"]) != expected_blocs
        ):
            raise DirectionalAuditError("each rotation must contain the three model blocs once")
    if len(set(rotation_ids)) != len(rotation_ids):
        raise DirectionalAuditError("rotation IDs must be unique")

    rules = plan["decision_rules"]
    _require_exact_keys(
        rules,
        {
            "mechanical_sender_order_minimum_paired_share_difference",
            "behavioral_alignment_minimum_paired_difference",
            "behavioral_alignment_minimum_rotations",
            "context_robust_right_rate_minimum",
            "cascade_minimum_share",
            "cascade_consecutive_steps",
        },
        "decision_rules",
    )
    if not isinstance(plan["analysis_restrictions"], list) or not plan["analysis_restrictions"]:
        raise DirectionalAuditError("analysis_restrictions must be a non-empty array")
    return plan


def _normalized_control_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(dict(config))
    simulation = normalized["simulation"]
    simulation.pop("run_id", None)
    simulation.pop("run_name", None)
    agents = normalized["agents"]
    agents.pop("message_history_limit", None)
    agents.pop("message_context_size", None)
    normalized["blocs"] = sorted(normalized["blocs"], key=lambda row: row["name"])
    return normalized


def build_audit_bundle(plan_path: Path) -> tuple[Dict[str, Any], Dict[str, bytes]]:
    plan_path = plan_path.resolve()
    plan = load_and_validate_plan(plan_path)
    source = plan["source"]
    base_path = (REPO_ROOT / source["base_config"]).resolve()
    if REPO_ROOT not in base_path.parents:
        raise DirectionalAuditError("base config escaped repository")
    if sha256_file(base_path) != source["base_config_sha256"]:
        raise DirectionalAuditError("base config SHA-256 differs from plan")
    base = load_json_unique(base_path)
    effective_base = build_effective_config(copy.deepcopy(base))
    validate_public_config_boundary(effective_base)

    frozen = plan["frozen"]
    if effective_base["simulation"]["seed"] != frozen["seed"]:
        raise DirectionalAuditError("base seed differs from frozen seed")
    if effective_base["llm_defaults"]["temperature"] != frozen["temperature"]:
        raise DirectionalAuditError("base temperature differs from frozen temperature")
    if sum(row["num_agents"] for row in effective_base["blocs"]) != frozen["expected_agents"]:
        raise DirectionalAuditError("base agent count differs from plan")
    if {row["name"] for row in effective_base["blocs"]} != {"qwen", "swallow", "elyza"}:
        raise DirectionalAuditError("base config does not contain the expected model blocs")
    for key in ("communication_radius", "memory_limit", "memory_size", "edge_policy"):
        if effective_base["agents"][key] != frozen[key]:
            raise DirectionalAuditError(f"base agents.{key} differs from plan")
    for key in (
        "prompt_contract_version",
        "response_contract_version",
        "transport_behavior_version",
    ):
        if effective_base["simulation"][key] != frozen[key]:
            raise DirectionalAuditError(f"base simulation.{key} differs from plan")

    expected_calls = frozen["duration"] * frozen["expected_agents"] * 2
    if expected_calls != frozen["expected_calls_per_run"]:
        raise DirectionalAuditError("expected call count does not match two-phase execution")
    if expected_calls * frozen["expected_total_runs"] != frozen["expected_total_calls"]:
        raise DirectionalAuditError("total expected call count is inconsistent")

    profiles = {row["name"]: row for row in base["blocs"]}
    generated: Dict[str, bytes] = {}
    rows = []
    ordinal = 0
    control_hashes = set()
    identity = plan["run_identity"]
    for context in plan["message_context_conditions"]:
        for rotation in plan["rotations"]:
            condition_id = context["condition_id"]
            rotation_id = rotation["rotation_id"]
            run_id = normalize_run_id(
                f"{identity['prefix']}-{condition_id}-{rotation_id}-"
                f"s{frozen['seed']}-{identity['date_tag']}-{identity['replicate_id']}"
            )
            config = copy.deepcopy(base)
            config["simulation"].update({
                "duration": frozen["duration"],
                "seed": frozen["seed"],
                "run_id": run_id,
                "run_name": run_id,
                "protocol_version": plan["protocol_version"],
                "metric_version": plan["metric_version"],
                "research_eligible": False,
            })
            config["agents"].update({
                "message_history_limit": context["message_history_limit"],
                "message_context_size": context["message_context_size"],
            })
            config["blocs"] = [
                copy.deepcopy(profiles[name]) for name in rotation["bloc_order"]
            ]
            effective = build_effective_config(copy.deepcopy(config))
            validate_public_config_boundary(effective)
            if effective["simulation"]["research_eligible"] is not False:
                raise DirectionalAuditError("generated config became research eligible")
            config_data = json_bytes(config)
            relative_path = f"configs/{run_id}.json"
            generated[relative_path] = config_data
            control_hash = sha256_bytes(json_bytes(_normalized_control_config(config)))
            control_hashes.add(control_hash)
            rows.append({
                "ordinal": ordinal,
                "cell_id": f"{condition_id}-{rotation_id}",
                "context_condition_id": condition_id,
                "rotation_id": rotation_id,
                "bloc_order_low_to_high_agent_id": list(rotation["bloc_order"]),
                "high_agent_id_bloc": rotation["bloc_order"][-1],
                "run_id": run_id,
                "config_path": relative_path,
                "config_sha256": sha256_bytes(config_data),
                "paired_control_sha256": control_hash,
                "expected_steps": frozen["duration"],
                "expected_agents": frozen["expected_agents"],
                "expected_llm_calls": frozen["expected_calls_per_run"],
                "research_eligible": False,
            })
            ordinal += 1
    if len(control_hashes) != 1:
        raise DirectionalAuditError("generated cells differ outside declared interventions")
    run_ids = [row["run_id"] for row in rows]
    config_hashes = [row["config_sha256"] for row in rows]
    if len(set(run_ids)) != len(run_ids) or len(set(config_hashes)) != len(config_hashes):
        raise DirectionalAuditError("generated run IDs and config hashes must be unique")

    initial_state_input = {
        "seed": frozen["seed"],
        "half_space_size": effective_base["simulation"]["half_space_size"],
        "places": effective_base["places"],
        "agent_count": frozen["expected_agents"],
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "audit_id": plan["audit_id"],
        "plan_path": str(plan_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "plan_sha256": sha256_file(plan_path),
        "base_config_path": source["base_config"],
        "base_config_sha256": source["base_config_sha256"],
        "protocol_version": plan["protocol_version"],
        "metric_version": plan["metric_version"],
        "paired_control_sha256": next(iter(control_hashes)),
        "initial_state_input_sha256": sha256_bytes(json_bytes(initial_state_input)),
        "expected_total_runs": frozen["expected_total_runs"],
        "expected_total_llm_calls": frozen["expected_total_calls"],
        "research_eligible": False,
        "rows": rows,
    }
    return manifest, generated


def read_jsonl(path: Path) -> list[Dict[str, Any]]:
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise DirectionalAuditError(
                        f"JSONL row is not an object: {path}:{line_number}"
                    )
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DirectionalAuditError(f"cannot load JSONL: {path}") from error
    return rows


def _action_label(row: Mapping[str, Any]) -> str:
    if row.get("action") == "stay":
        return "stay"
    direction = row.get("direction")
    return direction if isinstance(direction, str) and direction else "invalid"


def _counter_dict(values: Iterable[Any]) -> Dict[str, int]:
    counter = Counter(str(value) for value in values)
    return {key: counter[key] for key in sorted(counter)}


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _signed_horizontal(rows: Iterable[Mapping[str, Any]]) -> float | None:
    labels = [_action_label(row) for row in rows]
    right = labels.count("right")
    left = labels.count("left")
    return _rate(right - left, right + left)


def _unique_mode(labels: Iterable[str]) -> str | None:
    counts = Counter(labels)
    if not counts:
        return None
    ordered = counts.most_common()
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        return None
    return ordered[0][0]


def _reconstruct_phase3_visible(
    messages: Sequence[Mapping[str, Any]],
    agent_ids: Sequence[int],
    duration: int,
    history_limit: int,
    context_size: int,
) -> Dict[tuple[int, int], list[Mapping[str, Any]]]:
    by_step: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    previous_key = (0, -1)
    for message in messages:
        step = message.get("step")
        sender_id = message.get("sender_id")
        if not isinstance(step, int) or not isinstance(sender_id, int):
            raise DirectionalAuditError("message step and sender_id must be integers")
        key = (step, sender_id)
        if key <= previous_key:
            raise DirectionalAuditError("messages must be in canonical step/sender order")
        previous_key = key
        by_step[step].append(message)

    histories: Dict[int, list[Mapping[str, Any]]] = {agent_id: [] for agent_id in agent_ids}
    visible: Dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for step in range(1, duration + 1):
        for message in by_step.get(step, []):
            receivers = message.get("receiver_ids")
            if not isinstance(receivers, list):
                raise DirectionalAuditError("message receiver_ids must be an array")
            for receiver_id in receivers:
                if receiver_id not in histories:
                    raise DirectionalAuditError("message references an unknown receiver")
                histories[receiver_id].append(message)
                if len(histories[receiver_id]) > history_limit:
                    histories[receiver_id] = histories[receiver_id][-history_limit:]
        for agent_id in agent_ids:
            visible[(step, agent_id)] = list(histories[agent_id][-context_size:])
    return visible


def _cascade_summary(
    actions_by_step: Mapping[int, Sequence[Mapping[str, Any]]],
    duration: int,
    minimum_share: float,
    consecutive_steps: int,
) -> tuple[list[Dict[str, Any]], Dict[str, Any] | None]:
    steps = []
    streak_direction = None
    streak_start = None
    streak_length = 0
    onset = None
    for step in range(1, duration + 1):
        labels = [_action_label(row) for row in actions_by_step[step]]
        counts = Counter(labels)
        dominant = _unique_mode(labels)
        dominant_count = 0 if dominant is None else counts[dominant]
        share = _rate(dominant_count, len(labels)) or 0.0
        steps.append({
            "step": step,
            "counts": {key: counts[key] for key in sorted(counts)},
            "dominant_direction": dominant,
            "dominant_share": share,
        })
        if dominant is not None and share >= minimum_share:
            if dominant == streak_direction:
                streak_length += 1
            else:
                streak_direction = dominant
                streak_start = step
                streak_length = 1
        else:
            streak_direction = None
            streak_start = None
            streak_length = 0
        if onset is None and streak_length >= consecutive_steps:
            onset = {
                "direction": streak_direction,
                "start_step": streak_start,
                "confirmed_step": step,
            }
    return steps, onset


def analyze_run(
    run_dir: Path,
    config: Mapping[str, Any],
    manifest_row: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> Dict[str, Any]:
    run_dir = run_dir.resolve()
    meta = load_json_unique(run_dir / "run_meta.json")
    actions = read_jsonl(run_dir / "memory_reasoning.jsonl")
    messages = read_jsonl(run_dir / "messages.jsonl")
    parse_errors = read_jsonl(run_dir / "parse_errors.jsonl")
    run_id = manifest_row["run_id"]
    expected_steps = manifest_row["expected_steps"]
    expected_agents = manifest_row["expected_agents"]
    if meta.get("run_id") != run_id or meta.get("status") != "completed":
        raise DirectionalAuditError(f"run metadata is not completed for {run_id}")
    if meta.get("aborted") is not False or meta.get("git_dirty") is not False:
        raise DirectionalAuditError(f"run is aborted or source-dirty for {run_id}")
    if meta.get("completed_steps") != expected_steps:
        raise DirectionalAuditError(f"completed step count differs for {run_id}")
    if meta.get("expected_agents") != expected_agents:
        raise DirectionalAuditError(f"expected agent count differs for {run_id}")
    if meta.get("config", {}).get("simulation", {}).get("research_eligible") is not False:
        raise DirectionalAuditError(f"run became research eligible: {run_id}")
    if parse_errors:
        raise DirectionalAuditError(f"parse_errors.jsonl is non-empty for {run_id}")
    if len(actions) != expected_steps * expected_agents:
        raise DirectionalAuditError(f"action coverage differs for {run_id}")

    agent_ids = sorted({row.get("agent_id") for row in actions})
    if agent_ids != list(range(expected_agents)):
        raise DirectionalAuditError(f"agent IDs are incomplete for {run_id}")
    expected_pairs = {
        (step, agent_id)
        for step in range(1, expected_steps + 1)
        for agent_id in agent_ids
    }
    observed_pairs = {(row.get("step"), row.get("agent_id")) for row in actions}
    if observed_pairs != expected_pairs:
        raise DirectionalAuditError(f"step/agent action coverage differs for {run_id}")

    agents_config = config["agents"]
    visible = _reconstruct_phase3_visible(
        messages,
        agent_ids,
        expected_steps,
        agents_config["message_history_limit"],
        agents_config["message_context_size"],
    )
    visible_slots = [
        message
        for step_agent in sorted(visible)
        for message in visible[step_agent]
    ]
    high_bloc = manifest_row["high_agent_id_bloc"]
    high_slots = sum(message.get("sender_bloc") == high_bloc for message in visible_slots)
    current_step_slots = sum(
        message.get("step") == step
        for (step, _agent_id), rows in visible.items()
        for message in rows
    )
    all_high_contexts = sum(
        bool(rows) and all(message.get("sender_bloc") == high_bloc for message in rows)
        for rows in visible.values()
    )

    actions_by_step: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    actions_by_bloc: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in actions:
        actions_by_step[row["step"]].append(row)
        actions_by_bloc[row["bloc"]].append(row)
    step_summary, cascade = _cascade_summary(
        actions_by_step,
        expected_steps,
        float(rules["cascade_minimum_share"]),
        int(rules["cascade_consecutive_steps"]),
    )

    model_summary = {}
    for bloc in sorted(actions_by_bloc):
        bloc_rows = actions_by_bloc[bloc]
        labels = [_action_label(row) for row in bloc_rows]
        horizontal_pre_boundary = [
            row
            for row in bloc_rows
            if _action_label(row) in {"left", "right"}
            and abs(int(row["position"][0])) < int(config["simulation"]["half_space_size"])
        ]
        model_summary[bloc] = {
            "action_counts": _counter_dict(labels),
            "right_rate": _rate(labels.count("right"), len(labels)),
            "signed_horizontal_choice": _signed_horizontal(bloc_rows),
            "signed_horizontal_choice_before_x_boundary": _signed_horizontal(
                horizontal_pre_boundary
            ),
        }

    all_labels = [_action_label(row) for row in actions]
    half_space = int(config["simulation"]["half_space_size"])
    boundary_outward = [
        row
        for row in actions
        if (
            (row["position"][0] == half_space and _action_label(row) == "right")
            or (row["position"][0] == -half_space and _action_label(row) == "left")
            or (row["position"][1] == half_space and _action_label(row) == "up")
            or (row["position"][1] == -half_space and _action_label(row) == "down")
        )
    ]

    high_modal_by_step = {}
    for step in range(1, expected_steps + 1):
        high_modal_by_step[step] = _unique_mode(
            _action_label(row)
            for row in actions_by_step[step]
            if row["bloc"] == high_bloc
        )
    alignment_numerator = 0
    alignment_denominator = 0
    for step in range(2, expected_steps + 1):
        previous_mode = high_modal_by_step[step - 1]
        if previous_mode is None:
            continue
        for row in actions_by_step[step]:
            if row["bloc"] == high_bloc:
                continue
            alignment_denominator += 1
            alignment_numerator += _action_label(row) == previous_mode

    direction_mentions = Counter()
    for message in visible_slots:
        text = message.get("message", "")
        has_right = isinstance(text, str) and "右" in text
        has_left = isinstance(text, str) and "左" in text
        if has_right and has_left:
            direction_mentions["both"] += 1
        elif has_right:
            direction_mentions["right_only"] += 1
        elif has_left:
            direction_mentions["left_only"] += 1
        else:
            direction_mentions["neither"] += 1

    mean_consensus = sum(row["dominant_share"] for row in step_summary) / len(step_summary)
    return {
        "run_id": run_id,
        "cell_id": manifest_row["cell_id"],
        "context_condition_id": manifest_row["context_condition_id"],
        "rotation_id": manifest_row["rotation_id"],
        "bloc_order_low_to_high_agent_id": manifest_row[
            "bloc_order_low_to_high_agent_id"
        ],
        "high_agent_id_bloc": high_bloc,
        "direct_observation": {
            "status": meta["status"],
            "aborted": meta["aborted"],
            "completed_steps": meta["completed_steps"],
            "expected_agents": meta["expected_agents"],
            "action_rows": len(actions),
            "message_rows": len(messages),
            "parse_error_rows": len(parse_errors),
            "transport_failures": meta.get("transport_failures"),
            "syntax_parse_failures": meta.get("syntax_parse_failures"),
            "schema_validation_failures": meta.get("schema_validation_failures"),
            "git_sha": meta.get("git_sha"),
            "git_dirty": meta.get("git_dirty"),
        },
        "mechanical_derivation": {
            "model_actions": model_summary,
            "overall_action_counts": _counter_dict(all_labels),
            "overall_right_rate": _rate(all_labels.count("right"), len(all_labels)),
            "overall_signed_horizontal_choice": _signed_horizontal(actions),
            "mean_step_consensus_share": mean_consensus,
            "step_consensus": step_summary,
            "cascade": cascade,
            "boundary_outward_action_rows": len(boundary_outward),
            "visible_context_slot_count": len(visible_slots),
            "visible_context_slots_by_sender_bloc": _counter_dict(
                message.get("sender_bloc") for message in visible_slots
            ),
            "visible_context_high_bloc_share": _rate(high_slots, len(visible_slots)),
            "recipient_step_context_count": len(visible),
            "recipient_step_contexts_all_high_bloc": all_high_contexts,
            "visible_current_step_slot_count": current_step_slots,
            "visible_older_slot_count": len(visible_slots) - current_step_slots,
            "visible_literal_direction_mentions": {
                key: direction_mentions[key]
                for key in ("right_only", "left_only", "both", "neither")
            },
            "non_high_bloc_lag1_alignment_with_high_bloc": _rate(
                alignment_numerator,
                alignment_denominator,
            ),
            "non_high_bloc_lag1_alignment_numerator": alignment_numerator,
            "non_high_bloc_lag1_alignment_denominator": alignment_denominator,
        },
    }


def analyze_audit(manifest_path: Path, runs_root: Path) -> Dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_json_unique(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise DirectionalAuditError("unsupported audit manifest schema")
    if manifest.get("research_eligible") is not False:
        raise DirectionalAuditError("audit manifest became research eligible")
    plan_path = (REPO_ROOT / manifest["plan_path"]).resolve()
    if sha256_file(plan_path) != manifest["plan_sha256"]:
        raise DirectionalAuditError("audit plan SHA-256 differs from manifest")
    plan = load_and_validate_plan(plan_path)
    expected_manifest, expected_configs = build_audit_bundle(plan_path)
    if manifest != expected_manifest:
        raise DirectionalAuditError("audit manifest differs from deterministic builder")

    results = []
    for row in manifest["rows"]:
        config_path = manifest_path.parent / row["config_path"]
        expected_bytes = expected_configs[row["config_path"]]
        if not config_path.is_file() or config_path.read_bytes() != expected_bytes:
            raise DirectionalAuditError(f"generated config differs: {row['config_path']}")
        config = load_json_unique(config_path)
        run_dir = runs_root.resolve() / f"output_{row['run_id']}"
        if not run_dir.is_dir():
            raise DirectionalAuditError(f"missing run directory: {run_dir}")
        results.append(analyze_run(run_dir, config, row, plan["decision_rules"]))

    by_cell = {row["cell_id"]: row for row in results}
    pairs = []
    mechanical_differences = []
    alignment_differences = []
    for rotation in plan["rotations"]:
        rotation_id = rotation["rotation_id"]
        c03 = by_cell[f"c03-{rotation_id}"]
        c23 = by_cell[f"c23-{rotation_id}"]
        c03_metrics = c03["mechanical_derivation"]
        c23_metrics = c23["mechanical_derivation"]
        sender_difference = (
            c03_metrics["visible_context_high_bloc_share"]
            - c23_metrics["visible_context_high_bloc_share"]
        )
        c03_alignment = c03_metrics["non_high_bloc_lag1_alignment_with_high_bloc"]
        c23_alignment = c23_metrics["non_high_bloc_lag1_alignment_with_high_bloc"]
        alignment_difference = (
            None
            if c03_alignment is None or c23_alignment is None
            else c03_alignment - c23_alignment
        )
        mechanical_differences.append(sender_difference)
        if alignment_difference is not None:
            alignment_differences.append(alignment_difference)
        pairs.append({
            "rotation_id": rotation_id,
            "high_agent_id_bloc": c03["high_agent_id_bloc"],
            "c03_minus_c23_visible_high_bloc_share": sender_difference,
            "c03_minus_c23_lag1_alignment": alignment_difference,
            "c03_minus_c23_overall_right_rate": (
                c03_metrics["overall_right_rate"] - c23_metrics["overall_right_rate"]
            ),
            "c03_minus_c23_mean_consensus_share": (
                c03_metrics["mean_step_consensus_share"]
                - c23_metrics["mean_step_consensus_share"]
            ),
        })

    rules = plan["decision_rules"]
    mechanical_rule = all(
        value
        >= float(rules["mechanical_sender_order_minimum_paired_share_difference"])
        for value in mechanical_differences
    )
    behavioral_support_count = sum(
        value >= float(rules["behavioral_alignment_minimum_paired_difference"])
        for value in alignment_differences
    )
    behavioral_rule = behavioral_support_count >= int(
        rules["behavioral_alignment_minimum_rotations"]
    )
    right_rule = all(
        row["mechanical_derivation"]["overall_right_rate"]
        >= float(rules["context_robust_right_rate_minimum"])
        for row in results
    )
    source_shas = sorted({row["direct_observation"]["git_sha"] for row in results})
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "audit_id": manifest["audit_id"],
        "protocol_version": manifest["protocol_version"],
        "metric_version": manifest["metric_version"],
        "plan_sha256": manifest["plan_sha256"],
        "paired_control_sha256": manifest["paired_control_sha256"],
        "source_git_shas": source_shas,
        "direct_observation": {
            "expected_runs": manifest["expected_total_runs"],
            "observed_runs": len(results),
            "all_completed": all(
                row["direct_observation"]["status"] == "completed"
                and row["direct_observation"]["aborted"] is False
                for row in results
            ),
            "all_git_clean": all(
                row["direct_observation"]["git_dirty"] is False for row in results
            ),
            "total_action_rows": sum(
                row["direct_observation"]["action_rows"] for row in results
            ),
            "total_message_rows": sum(
                row["direct_observation"]["message_rows"] for row in results
            ),
            "total_parse_error_rows": sum(
                row["direct_observation"]["parse_error_rows"] for row in results
            ),
        },
        "mechanical_derivation": {
            "runs": results,
            "paired_context_differences": pairs,
        },
        "engineering_decision": {
            "mechanical_sender_order_dominance_rule": mechanical_rule,
            "behavioral_context_signal_rule": behavioral_rule,
            "behavioral_context_signal_supporting_rotations": behavioral_support_count,
            "context_robust_right_pattern_rule": right_rule,
        },
        "interpretation_boundary": (
            "Engineering diagnostics only. The audit does not establish reuse, "
            "adoption, belief change, causal social amplification, or a research result."
        ),
        "analysis_restrictions_applied": plan["analysis_restrictions"],
        "research_eligible": False,
    }
