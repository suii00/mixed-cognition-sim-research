"""Deterministic builder and metric core for the direction-presentation audit."""

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
from engine.execution_contracts import (  # noqa: E402
    JAPANESE_COMPACT_LR_PROMPT_CONTRACT_VERSION,
    JAPANESE_COMPACT_RL_PROMPT_CONTRACT_VERSION,
)
from engine.provenance import (  # noqa: E402
    compute_config_hash,
    compute_prompt_hash,
    normalize_run_id,
)
from engine.response_contracts import (  # noqa: E402
    COMPACT_LR_RESPONSE_CONTRACT_VERSION,
    COMPACT_RL_RESPONSE_CONTRACT_VERSION,
    response_schema_sha256,
)


PLAN_SCHEMA_VERSION = "direction-presentation-audit-plan-v2.0.0"
MANIFEST_SCHEMA_VERSION = "direction-presentation-audit-manifest-v2.0.0"
RESULT_SCHEMA_VERSION = "direction-presentation-audit-result-v1.0.0"


class DirectionPresentationAuditError(ValueError):
    """The plan, generated bundle, or raw audit input is inconsistent."""


class DuplicateJsonKeyError(DirectionPresentationAuditError):
    """A JSON document contains a duplicate object member."""


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


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
        raise DirectionPresentationAuditError(
            f"cannot load JSON object: {path}"
        ) from error
    if not isinstance(value, dict):
        raise DirectionPresentationAuditError(f"JSON root must be an object: {path}")
    return value


def read_jsonl(path: Path) -> list[Dict[str, Any]]:
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line, object_pairs_hook=_unique_object)
                if not isinstance(value, dict):
                    raise DirectionPresentationAuditError(
                        f"JSONL row is not an object: {path}:{line_number}"
                    )
                rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DirectionPresentationAuditError(f"cannot load JSONL: {path}") from error
    return rows


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
        raise DirectionPresentationAuditError(
            f"{label} fields differ ({'; '.join(details)})"
        )


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise DirectionPresentationAuditError(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise DirectionPresentationAuditError(f"{label} must be lowercase SHA-256")
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
            "presentation_conditions",
            "delivery_conditions",
            "rotations",
            "decision_rules",
            "analysis_restrictions",
        },
        "plan",
    )
    if plan["schema_version"] != PLAN_SCHEMA_VERSION:
        raise DirectionPresentationAuditError("unsupported audit plan schema")
    if plan["research_eligible"] is not False:
        raise DirectionPresentationAuditError("engineering audit must remain ineligible")
    for key in ("audit_id", "created_at", "protocol_version", "metric_version"):
        if not isinstance(plan[key], str) or not plan[key]:
            raise DirectionPresentationAuditError(f"plan.{key} must be non-empty")

    source = plan["source"]
    _require_exact_keys(
        source,
        {
            "base_config",
            "base_config_sha256",
            "source_run_id",
            "source_git_sha",
            "source_raw_manifest_sha256",
        },
        "plan.source",
    )
    base_relative = Path(source["base_config"])
    if base_relative.is_absolute() or ".." in base_relative.parts:
        raise DirectionPresentationAuditError("base config must be repository relative")
    _require_sha256(source["base_config_sha256"], "source.base_config_sha256")
    _require_sha256(
        source["source_raw_manifest_sha256"],
        "source.source_raw_manifest_sha256",
    )
    if (
        not isinstance(source["source_git_sha"], str)
        or len(source["source_git_sha"]) != 40
    ):
        raise DirectionPresentationAuditError("source.source_git_sha must be full SHA")
    normalize_run_id(source["source_run_id"])

    identity = plan["run_identity"]
    _require_exact_keys(
        identity,
        {"prefix", "date_tag", "replicate_id", "factor_order"},
        "run_identity",
    )
    if identity["factor_order"] != [
        "presentation", "delivery", "rotation", "seed", "date", "replicate"
    ]:
        raise DirectionPresentationAuditError("run identity factor order differs")
    for key in ("prefix", "date_tag", "replicate_id"):
        if not isinstance(identity[key], str) or not identity[key]:
            raise DirectionPresentationAuditError(f"run_identity.{key} must be non-empty")

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
            "max_tokens",
            "transport_behavior_version",
            "response_failure_policy",
            "communication_radius",
            "message_history_limit",
            "message_context_size",
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
        "max_tokens",
        "communication_radius",
        "message_history_limit",
        "message_context_size",
        "memory_limit",
        "memory_size",
    ):
        _require_int(frozen[key], f"plan.frozen.{key}", 1)
    if not isinstance(frozen["temperature"], (int, float)) or isinstance(
        frozen["temperature"], bool
    ):
        raise DirectionPresentationAuditError("temperature must be numeric")
    if frozen["message_context_size"] > frozen["message_history_limit"]:
        raise DirectionPresentationAuditError("message context exceeds history")
    if frozen["response_failure_policy"] != "abort_run":
        raise DirectionPresentationAuditError("response failures must abort")

    presentations = plan["presentation_conditions"]
    expected_presentations = {
        "lr": (
            ["left", "right"],
            JAPANESE_COMPACT_LR_PROMPT_CONTRACT_VERSION,
            COMPACT_LR_RESPONSE_CONTRACT_VERSION,
        ),
        "rl": (
            ["right", "left"],
            JAPANESE_COMPACT_RL_PROMPT_CONTRACT_VERSION,
            COMPACT_RL_RESPONSE_CONTRACT_VERSION,
        ),
    }
    if not isinstance(presentations, list) or len(presentations) != 2:
        raise DirectionPresentationAuditError("exactly two presentations are required")
    for row in presentations:
        _require_exact_keys(
            row,
            {
                "condition_id",
                "horizontal_order",
                "prompt_contract_version",
                "prompt_source_sha256",
                "response_contract_version",
                "response_schema_sha256",
            },
            "presentation condition",
        )
        condition_id = row["condition_id"]
        if condition_id not in expected_presentations:
            raise DirectionPresentationAuditError("unknown presentation condition")
        if tuple((row[key] for key in (
            "horizontal_order",
            "prompt_contract_version",
            "response_contract_version",
        ))) != expected_presentations[condition_id]:
            raise DirectionPresentationAuditError("presentation contract pairing differs")
        _require_sha256(
            row["prompt_source_sha256"],
            f"presentation.{condition_id}.prompt_source_sha256",
        )
        _require_sha256(
            row["response_schema_sha256"],
            f"presentation.{condition_id}.response_schema_sha256",
        )
        if row["prompt_source_sha256"] != compute_prompt_hash(
            REPO_ROOT, row["prompt_contract_version"]
        ):
            raise DirectionPresentationAuditError("prompt source SHA-256 differs")
        if row["response_schema_sha256"] != response_schema_sha256(
            row["response_contract_version"]
        ):
            raise DirectionPresentationAuditError("response schema SHA-256 differs")
    if [row["condition_id"] for row in presentations] != ["lr", "rl"]:
        raise DirectionPresentationAuditError("presentation order must be lr then rl")

    deliveries = plan["delivery_conditions"]
    expected_deliveries = {
        "com": ("full", True, True),
        "iso": ("none", True, False),
    }
    if not isinstance(deliveries, list) or len(deliveries) != 2:
        raise DirectionPresentationAuditError("exactly two delivery conditions are required")
    for row in deliveries:
        _require_exact_keys(
            row,
            {
                "condition_id",
                "edge_policy",
                "phase1_generation",
                "message_delivery",
            },
            "delivery condition",
        )
        condition_id = row["condition_id"]
        if condition_id not in expected_deliveries:
            raise DirectionPresentationAuditError("unknown delivery condition")
        if (
            row["edge_policy"],
            row["phase1_generation"],
            row["message_delivery"],
        ) != expected_deliveries[condition_id]:
            raise DirectionPresentationAuditError("delivery intervention differs")
    if [row["condition_id"] for row in deliveries] != ["com", "iso"]:
        raise DirectionPresentationAuditError("delivery order must be com then iso")

    rotations = plan["rotations"]
    if not isinstance(rotations, list) or len(rotations) != 3:
        raise DirectionPresentationAuditError("exactly three rotations are required")
    expected_blocs = {"qwen", "swallow", "elyza"}
    for index, row in enumerate(rotations):
        _require_exact_keys(row, {"rotation_id", "bloc_order"}, "rotation")
        if row["rotation_id"] != f"r{index}":
            raise DirectionPresentationAuditError("rotation IDs must be r0, r1, r2")
        if not isinstance(row["bloc_order"], list) or set(row["bloc_order"]) != expected_blocs:
            raise DirectionPresentationAuditError("rotation must contain each model bloc")

    expected_runs = len(presentations) * len(deliveries) * len(rotations)
    if frozen["expected_total_runs"] != expected_runs:
        raise DirectionPresentationAuditError("factor cross product differs from run count")
    calls = frozen["duration"] * frozen["expected_agents"] * 2
    if frozen["expected_calls_per_run"] != calls:
        raise DirectionPresentationAuditError("call count must preserve both phases")
    if frozen["expected_total_calls"] != calls * expected_runs:
        raise DirectionPresentationAuditError("total expected calls differ")

    rules = plan["decision_rules"]
    _require_exact_keys(
        rules,
        {
            "minimum_absolute_right_rate_difference",
            "minimum_supporting_rotations",
            "presentation_robust_right_rate_minimum",
            "cascade_minimum_share",
            "cascade_consecutive_steps",
            "maximum_total_contract_failures",
        },
        "decision_rules",
    )
    _require_int(rules["minimum_supporting_rotations"], "minimum rotations", 1)
    _require_int(rules["cascade_consecutive_steps"], "cascade steps", 1)
    _require_int(rules["maximum_total_contract_failures"], "contract failures")
    for key in (
        "minimum_absolute_right_rate_difference",
        "presentation_robust_right_rate_minimum",
        "cascade_minimum_share",
    ):
        value = rules[key]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= float(value) <= 1
        ):
            raise DirectionPresentationAuditError(f"decision_rules.{key} must be 0..1")
    restrictions = plan["analysis_restrictions"]
    if not isinstance(restrictions, list) or not restrictions or not all(
        isinstance(value, str) and value for value in restrictions
    ):
        raise DirectionPresentationAuditError("analysis restrictions must be text")
    return plan


def _normalized_control_config(config: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(dict(config))
    simulation = normalized["simulation"]
    for key in (
        "run_id",
        "run_name",
        "prompt_contract_version",
        "response_contract_version",
    ):
        simulation.pop(key, None)
    normalized["agents"].pop("edge_policy", None)
    normalized["blocs"] = sorted(normalized["blocs"], key=lambda row: row["name"])
    return normalized


def build_audit_bundle(plan_path: Path) -> tuple[Dict[str, Any], Dict[str, bytes]]:
    plan_path = plan_path.resolve()
    plan = load_and_validate_plan(plan_path)
    source = plan["source"]
    base_path = (REPO_ROOT / source["base_config"]).resolve()
    if REPO_ROOT not in base_path.parents:
        raise DirectionPresentationAuditError("base config escaped repository")
    if not base_path.is_file() or sha256_file(base_path) != source["base_config_sha256"]:
        raise DirectionPresentationAuditError("base config SHA-256 differs")
    base = load_json_unique(base_path)
    effective_base = build_effective_config(copy.deepcopy(base))
    validate_public_config_boundary(effective_base)
    source_meta_path = (
        REPO_ROOT
        / "runs"
        / f"output_{source['source_run_id']}"
        / "run_meta.json"
    )
    source_meta = load_json_unique(source_meta_path)
    if (
        source_meta.get("run_id") != source["source_run_id"]
        or source_meta.get("status") != "completed"
        or source_meta.get("aborted") is not False
        or source_meta.get("git_dirty") is not False
        or source_meta.get("git_sha") != source["source_git_sha"]
        or source_meta.get("config") != effective_base
    ):
        raise DirectionPresentationAuditError("source run evidence differs")
    raw_manifest = source_meta.get("raw_manifest")
    if (
        not isinstance(raw_manifest, dict)
        or sha256_bytes(canonical_json_bytes(raw_manifest))
        != source["source_raw_manifest_sha256"]
    ):
        raise DirectionPresentationAuditError("source raw manifest SHA-256 differs")
    frozen = plan["frozen"]
    if effective_base["simulation"]["seed"] != frozen["seed"]:
        raise DirectionPresentationAuditError("base seed differs")
    if effective_base["llm_defaults"]["temperature"] != frozen["temperature"]:
        raise DirectionPresentationAuditError("base temperature differs")
    if effective_base["llm_defaults"]["max_tokens"] != frozen["max_tokens"]:
        raise DirectionPresentationAuditError("base max_tokens differs")
    if sum(row["num_agents"] for row in effective_base["blocs"]) != frozen["expected_agents"]:
        raise DirectionPresentationAuditError("base agent count differs")
    if {row["name"] for row in effective_base["blocs"]} != {"qwen", "swallow", "elyza"}:
        raise DirectionPresentationAuditError("base model blocs differ")
    for key in (
        "communication_radius",
        "message_history_limit",
        "message_context_size",
        "memory_limit",
        "memory_size",
    ):
        if effective_base["agents"][key] != frozen[key]:
            raise DirectionPresentationAuditError(f"base agents.{key} differs")
    if (
        effective_base["simulation"]["transport_behavior_version"]
        != frozen["transport_behavior_version"]
    ):
        raise DirectionPresentationAuditError("base transport contract differs")
    if (
        effective_base["simulation"]["response_failure_policy"]
        != frozen["response_failure_policy"]
    ):
        raise DirectionPresentationAuditError("base failure policy differs")

    profiles = {row["name"]: row for row in base["blocs"]}
    generated: Dict[str, bytes] = {}
    rows = []
    control_hashes = set()
    identity = plan["run_identity"]
    ordinal = 0
    for presentation in plan["presentation_conditions"]:
        for delivery in plan["delivery_conditions"]:
            for rotation in plan["rotations"]:
                presentation_id = presentation["condition_id"]
                delivery_id = delivery["condition_id"]
                rotation_id = rotation["rotation_id"]
                run_id = normalize_run_id(
                    f"{identity['prefix']}-{presentation_id}-{delivery_id}-"
                    f"{rotation_id}-s{frozen['seed']}-{identity['date_tag']}-"
                    f"{identity['replicate_id']}"
                )
                config = copy.deepcopy(base)
                config["simulation"].update({
                    "duration": frozen["duration"],
                    "seed": frozen["seed"],
                    "run_id": run_id,
                    "run_name": run_id,
                    "protocol_version": plan["protocol_version"],
                    "metric_version": plan["metric_version"],
                    "prompt_contract_version": presentation[
                        "prompt_contract_version"
                    ],
                    "response_contract_version": presentation[
                        "response_contract_version"
                    ],
                    "transport_behavior_version": frozen[
                        "transport_behavior_version"
                    ],
                    "response_failure_policy": frozen["response_failure_policy"],
                    "research_eligible": False,
                })
                config["agents"].update({
                    "communication_radius": frozen["communication_radius"],
                    "edge_policy": delivery["edge_policy"],
                    "message_history_limit": frozen["message_history_limit"],
                    "message_context_size": frozen["message_context_size"],
                    "memory_limit": frozen["memory_limit"],
                    "memory_size": frozen["memory_size"],
                })
                config["llm_defaults"].update({
                    "temperature": frozen["temperature"],
                    "max_tokens": frozen["max_tokens"],
                })
                config["blocs"] = [
                    copy.deepcopy(profiles[name]) for name in rotation["bloc_order"]
                ]
                effective = build_effective_config(copy.deepcopy(config))
                validate_public_config_boundary(effective)
                if effective["simulation"]["research_eligible"] is not False:
                    raise DirectionPresentationAuditError("generated cell became eligible")
                config_data = json_bytes(config)
                relative_path = f"configs/{run_id}.json"
                generated[relative_path] = config_data
                control_hash = sha256_bytes(
                    canonical_json_bytes(_normalized_control_config(config))
                )
                control_hashes.add(control_hash)
                rows.append({
                    "ordinal": ordinal,
                    "cell_id": f"{presentation_id}-{delivery_id}-{rotation_id}",
                    "presentation_id": presentation_id,
                    "delivery_id": delivery_id,
                    "rotation_id": rotation_id,
                    "horizontal_order": presentation["horizontal_order"],
                    "edge_policy": delivery["edge_policy"],
                    "message_delivery": delivery["message_delivery"],
                    "prompt_contract_version": presentation[
                        "prompt_contract_version"
                    ],
                    "response_contract_version": presentation[
                        "response_contract_version"
                    ],
                    "bloc_order_low_to_high_agent_id": list(rotation["bloc_order"]),
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
        raise DirectionPresentationAuditError(
            "generated cells differ outside declared interventions"
        )
    if len(rows) != frozen["expected_total_runs"]:
        raise DirectionPresentationAuditError("generated run count differs")
    run_ids = [row["run_id"] for row in rows]
    config_hashes = [row["config_sha256"] for row in rows]
    if len(set(run_ids)) != len(rows) or len(set(config_hashes)) != len(rows):
        raise DirectionPresentationAuditError("run IDs and config hashes must be unique")
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
        "initial_state_input_sha256": sha256_bytes(
            canonical_json_bytes(initial_state_input)
        ),
        "expected_total_runs": frozen["expected_total_runs"],
        "expected_total_llm_calls": frozen["expected_total_calls"],
        "research_eligible": False,
        "rows": rows,
    }
    return manifest, generated


def _action_label(row: Mapping[str, Any]) -> str:
    if row.get("action") == "stay":
        return "stay"
    direction = row.get("direction")
    return direction if isinstance(direction, str) and direction else "invalid"


def _counter_dict(values: Iterable[Any]) -> Dict[str, int]:
    counts = Counter(str(value) for value in values)
    return {key: counts[key] for key in sorted(counts)}


def _rate(numerator: int | float, denominator: int | float) -> float | None:
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
            raise DirectionPresentationAuditError("message identity must be integral")
        key = (step, sender_id)
        if key <= previous_key:
            raise DirectionPresentationAuditError("messages are not canonical order")
        previous_key = key
        by_step[step].append(message)
    histories: Dict[int, list[Mapping[str, Any]]] = {
        agent_id: [] for agent_id in agent_ids
    }
    visible: Dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for step in range(1, duration + 1):
        for message in by_step.get(step, []):
            receivers = message.get("receiver_ids")
            if not isinstance(receivers, list):
                raise DirectionPresentationAuditError("receiver_ids must be an array")
            for receiver_id in receivers:
                if receiver_id not in histories:
                    raise DirectionPresentationAuditError("unknown message receiver")
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


def _validate_reasoning_empty(
    phase1_rows: Sequence[Mapping[str, Any]],
    actions: Sequence[Mapping[str, Any]],
    run_id: str,
) -> None:
    for row in phase1_rows:
        parsed = row.get("parsed")
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"message", "reasoning"}
            or not isinstance(parsed.get("message"), str)
            or parsed.get("reasoning") != ""
        ):
            raise DirectionPresentationAuditError(
                f"Phase 1 compact response differs for {run_id}"
            )
    for row in actions:
        if row.get("reasoning") != "":
            raise DirectionPresentationAuditError(
                f"Phase 3 reasoning is not empty for {run_id}"
            )


def analyze_run(
    run_dir: Path,
    config: Mapping[str, Any],
    manifest_row: Mapping[str, Any],
    rules: Mapping[str, Any],
) -> Dict[str, Any]:
    run_dir = run_dir.resolve()
    meta = load_json_unique(run_dir / "run_meta.json")
    phase1_rows = read_jsonl(run_dir / "phase1_raw.jsonl")
    actions = read_jsonl(run_dir / "memory_reasoning.jsonl")
    messages = read_jsonl(run_dir / "messages.jsonl")
    parse_errors = read_jsonl(run_dir / "parse_errors.jsonl")
    attempts = read_jsonl(run_dir / "llm_attempts.jsonl")
    run_id = manifest_row["run_id"]
    expected_steps = manifest_row["expected_steps"]
    expected_agents = manifest_row["expected_agents"]
    expected_calls = manifest_row["expected_llm_calls"]
    if meta.get("run_id") != run_id or meta.get("status") != "completed":
        raise DirectionPresentationAuditError(f"run is not completed: {run_id}")
    if meta.get("aborted") is not False or meta.get("git_dirty") is not False:
        raise DirectionPresentationAuditError(f"run is aborted or source-dirty: {run_id}")
    if meta.get("completed_steps") != expected_steps:
        raise DirectionPresentationAuditError(f"step count differs: {run_id}")
    if meta.get("expected_agents") != expected_agents:
        raise DirectionPresentationAuditError(f"agent count differs: {run_id}")
    if meta.get("logical_llm_calls") != expected_calls:
        raise DirectionPresentationAuditError(f"logical call count differs: {run_id}")
    if len(attempts) != expected_calls:
        raise DirectionPresentationAuditError(f"attempt coverage differs: {run_id}")
    if len(phase1_rows) != expected_steps * expected_agents:
        raise DirectionPresentationAuditError(f"Phase 1 coverage differs: {run_id}")
    if len(actions) != expected_steps * expected_agents:
        raise DirectionPresentationAuditError(f"Phase 3 coverage differs: {run_id}")
    if parse_errors:
        raise DirectionPresentationAuditError(f"parse_errors is non-empty: {run_id}")
    effective = build_effective_config(copy.deepcopy(dict(config)))
    if meta.get("config") != effective:
        raise DirectionPresentationAuditError(f"metadata config differs: {run_id}")
    if meta.get("config_hash") != compute_config_hash(effective):
        raise DirectionPresentationAuditError(f"metadata config hash differs: {run_id}")
    if effective["simulation"].get("research_eligible") is not False:
        raise DirectionPresentationAuditError(f"run became research eligible: {run_id}")
    prompt_version = manifest_row["prompt_contract_version"]
    response_version = manifest_row["response_contract_version"]
    if meta.get("prompt_hash") != compute_prompt_hash(REPO_ROOT, prompt_version):
        raise DirectionPresentationAuditError(f"prompt hash differs: {run_id}")
    if meta.get("response_schema_sha256") != response_schema_sha256(response_version):
        raise DirectionPresentationAuditError(f"response schema hash differs: {run_id}")
    counters = {
        key: meta.get(key)
        for key in (
            "transport_failures",
            "syntax_parse_failures",
            "schema_validation_failures",
        )
    }
    if not all(isinstance(value, int) and value >= 0 for value in counters.values()):
        raise DirectionPresentationAuditError(f"contract counters are invalid: {run_id}")
    _validate_reasoning_empty(phase1_rows, actions, run_id)

    agent_ids = list(range(expected_agents))
    expected_pairs = {
        (step, agent_id)
        for step in range(1, expected_steps + 1)
        for agent_id in agent_ids
    }
    if {
        (row.get("step"), row.get("agent_id")) for row in phase1_rows
    } != expected_pairs:
        raise DirectionPresentationAuditError(f"Phase 1 keys differ: {run_id}")
    if {
        (row.get("step"), row.get("agent_id")) for row in actions
    } != expected_pairs:
        raise DirectionPresentationAuditError(f"Phase 3 keys differ: {run_id}")
    authored_nonempty = sum(
        bool(row["parsed"]["message"]) for row in phase1_rows
    )
    if manifest_row["message_delivery"]:
        if len(messages) != authored_nonempty:
            raise DirectionPresentationAuditError(
                f"delivered message rows differ from authored outputs: {run_id}"
            )
    elif messages:
        raise DirectionPresentationAuditError(
            f"isolated condition contains delivered messages: {run_id}"
        )
    visible = _reconstruct_phase3_visible(
        messages,
        agent_ids,
        expected_steps,
        config["agents"]["message_history_limit"],
        config["agents"]["message_context_size"],
    )
    visible_slots = [
        message
        for key in sorted(visible)
        for message in visible[key]
    ]
    if not manifest_row["message_delivery"] and visible_slots:
        raise DirectionPresentationAuditError(
            f"isolated condition has reconstructed exposure: {run_id}"
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

    def summarize(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        labels = [_action_label(row) for row in rows]
        horizontal_count = labels.count("right") + labels.count("left")
        return {
            "action_counts": _counter_dict(labels),
            "right_rate": _rate(labels.count("right"), len(labels)),
            "left_rate": _rate(labels.count("left"), len(labels)),
            "horizontal_right_share": _rate(
                labels.count("right"), horizontal_count
            ),
            "signed_horizontal_choice": _signed_horizontal(rows),
        }

    model_actions = {
        bloc: summarize(rows) for bloc, rows in sorted(actions_by_bloc.items())
    }
    overall = summarize(actions)
    direction_mentions = Counter()
    for message in visible_slots:
        text = message.get("message", "")
        has_right = isinstance(text, str) and "右" in text
        has_left = isinstance(text, str) and "左" in text
        direction_mentions[
            "both" if has_right and has_left
            else "right_only" if has_right
            else "left_only" if has_left
            else "neither"
        ] += 1
    return {
        "run_id": run_id,
        "cell_id": manifest_row["cell_id"],
        "presentation_id": manifest_row["presentation_id"],
        "delivery_id": manifest_row["delivery_id"],
        "rotation_id": manifest_row["rotation_id"],
        "horizontal_order": manifest_row["horizontal_order"],
        "edge_policy": manifest_row["edge_policy"],
        "bloc_order_low_to_high_agent_id": manifest_row[
            "bloc_order_low_to_high_agent_id"
        ],
        "direct_observation": {
            "status": meta["status"],
            "aborted": meta["aborted"],
            "completed_steps": meta["completed_steps"],
            "expected_agents": meta["expected_agents"],
            "phase1_rows": len(phase1_rows),
            "action_rows": len(actions),
            "authored_nonempty_message_rows": authored_nonempty,
            "delivered_message_rows": len(messages),
            "attempt_rows": len(attempts),
            "parse_error_rows": len(parse_errors),
            **counters,
            "git_sha": meta.get("git_sha"),
            "git_dirty": meta.get("git_dirty"),
            "prompt_hash": meta.get("prompt_hash"),
            "response_schema_sha256": meta.get("response_schema_sha256"),
        },
        "mechanical_derivation": {
            "overall_actions": overall,
            "model_actions": model_actions,
            "mean_step_consensus_share": sum(
                row["dominant_share"] for row in step_summary
            ) / len(step_summary),
            "step_consensus": step_summary,
            "cascade": cascade,
            "visible_context_slot_count": len(visible_slots),
            "visible_context_slots_by_sender_bloc": _counter_dict(
                message.get("sender_bloc") for message in visible_slots
            ),
            "visible_literal_direction_mentions": {
                key: direction_mentions[key]
                for key in ("right_only", "left_only", "both", "neither")
            },
        },
    }


def _metric(run: Mapping[str, Any], key: str = "right_rate") -> float:
    value = run["mechanical_derivation"]["overall_actions"][key]
    if not isinstance(value, (int, float)):
        raise DirectionPresentationAuditError(f"metric {key} is undefined")
    return float(value)


def _model_metric(run: Mapping[str, Any], bloc: str, key: str) -> float | None:
    value = run["mechanical_derivation"]["model_actions"][bloc][key]
    return float(value) if isinstance(value, (int, float)) else None


def analyze_audit(manifest_path: Path, runs_root: Path) -> Dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = load_json_unique(manifest_path)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise DirectionPresentationAuditError("unsupported audit manifest schema")
    if manifest.get("research_eligible") is not False:
        raise DirectionPresentationAuditError("manifest became research eligible")
    plan_path = (REPO_ROOT / manifest["plan_path"]).resolve()
    if sha256_file(plan_path) != manifest.get("plan_sha256"):
        raise DirectionPresentationAuditError("plan SHA-256 differs")
    plan = load_and_validate_plan(plan_path)
    expected_manifest, expected_configs = build_audit_bundle(plan_path)
    if manifest != expected_manifest:
        raise DirectionPresentationAuditError("manifest differs from builder")
    results = []
    for row in manifest["rows"]:
        config_path = manifest_path.parent / row["config_path"]
        expected_bytes = expected_configs[row["config_path"]]
        if not config_path.is_file() or config_path.read_bytes() != expected_bytes:
            raise DirectionPresentationAuditError(
                f"generated config differs: {row['config_path']}"
            )
        config = load_json_unique(config_path)
        run_dir = runs_root.resolve() / f"output_{row['run_id']}"
        if not run_dir.is_dir():
            raise DirectionPresentationAuditError(f"missing run directory: {run_dir}")
        results.append(analyze_run(run_dir, config, row, plan["decision_rules"]))

    by_cell = {row["cell_id"]: row for row in results}
    presentation_pairs = []
    communication_pairs = []
    interactions = []
    threshold = float(
        plan["decision_rules"]["minimum_absolute_right_rate_difference"]
    )
    presentations_by_delivery: Dict[str, list[float]] = defaultdict(list)
    communications_by_presentation: Dict[str, list[float]] = defaultdict(list)
    interaction_values = []
    for rotation in plan["rotations"]:
        rotation_id = rotation["rotation_id"]
        cells = {
            (presentation, delivery): by_cell[
                f"{presentation}-{delivery}-{rotation_id}"
            ]
            for presentation in ("lr", "rl")
            for delivery in ("com", "iso")
        }
        for delivery in ("com", "iso"):
            lr = cells[("lr", delivery)]
            rl = cells[("rl", delivery)]
            difference = _metric(lr) - _metric(rl)
            presentations_by_delivery[delivery].append(difference)
            model_differences = {}
            for bloc in ("elyza", "qwen", "swallow"):
                left_value = _model_metric(lr, bloc, "right_rate")
                right_value = _model_metric(rl, bloc, "right_rate")
                model_differences[bloc] = (
                    None
                    if left_value is None or right_value is None
                    else left_value - right_value
                )
            presentation_pairs.append({
                "rotation_id": rotation_id,
                "delivery_id": delivery,
                "lr_minus_rl_overall_right_rate": difference,
                "absolute_overall_right_rate_difference": abs(difference),
                "lr_minus_rl_horizontal_right_share": (
                    _metric(lr, "horizontal_right_share")
                    - _metric(rl, "horizontal_right_share")
                ),
                "lr_minus_rl_model_right_rate": model_differences,
            })
        for presentation in ("lr", "rl"):
            communicated = cells[(presentation, "com")]
            isolated = cells[(presentation, "iso")]
            difference = _metric(communicated) - _metric(isolated)
            communications_by_presentation[presentation].append(difference)
            communication_pairs.append({
                "rotation_id": rotation_id,
                "presentation_id": presentation,
                "com_minus_iso_overall_right_rate": difference,
                "absolute_overall_right_rate_difference": abs(difference),
                "com_minus_iso_mean_consensus_share": (
                    communicated["mechanical_derivation"][
                        "mean_step_consensus_share"
                    ]
                    - isolated["mechanical_derivation"][
                        "mean_step_consensus_share"
                    ]
                ),
            })
        interaction = (
            (_metric(cells[("lr", "com")]) - _metric(cells[("rl", "com")]))
            - (_metric(cells[("lr", "iso")]) - _metric(cells[("rl", "iso")]))
        )
        interaction_values.append(interaction)
        interactions.append({
            "rotation_id": rotation_id,
            "right_rate_difference_in_differences": interaction,
            "absolute_difference_in_differences": abs(interaction),
        })

    minimum_rotations = int(plan["decision_rules"]["minimum_supporting_rotations"])
    presentation_support = {
        delivery: sum(abs(value) >= threshold for value in values)
        for delivery, values in sorted(presentations_by_delivery.items())
    }
    communication_support = {
        presentation: sum(abs(value) >= threshold for value in values)
        for presentation, values in sorted(communications_by_presentation.items())
    }
    interaction_support = sum(abs(value) >= threshold for value in interaction_values)
    total_contract_failures = sum(
        row["direct_observation"][key]
        for row in results
        for key in (
            "transport_failures",
            "syntax_parse_failures",
            "schema_validation_failures",
        )
    )
    right_minimum = float(
        plan["decision_rules"]["presentation_robust_right_rate_minimum"]
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
            "total_phase1_rows": sum(
                row["direct_observation"]["phase1_rows"] for row in results
            ),
            "total_action_rows": sum(
                row["direct_observation"]["action_rows"] for row in results
            ),
            "total_authored_nonempty_message_rows": sum(
                row["direct_observation"]["authored_nonempty_message_rows"]
                for row in results
            ),
            "total_delivered_message_rows": sum(
                row["direct_observation"]["delivered_message_rows"]
                for row in results
            ),
            "total_attempt_rows": sum(
                row["direct_observation"]["attempt_rows"] for row in results
            ),
            "total_contract_failures": total_contract_failures,
        },
        "mechanical_derivation": {
            "runs": results,
            "paired_presentation_differences": presentation_pairs,
            "paired_communication_differences": communication_pairs,
            "presentation_communication_interactions": interactions,
        },
        "engineering_decision": {
            "presentation_sensitivity_supporting_rotations_by_delivery": (
                presentation_support
            ),
            "presentation_sensitivity_rule_by_delivery": {
                key: value >= minimum_rotations
                for key, value in presentation_support.items()
            },
            "communication_sensitivity_supporting_rotations_by_presentation": (
                communication_support
            ),
            "communication_sensitivity_rule_by_presentation": {
                key: value >= minimum_rotations
                for key, value in communication_support.items()
            },
            "presentation_communication_interaction_supporting_rotations": (
                interaction_support
            ),
            "presentation_communication_interaction_rule": (
                interaction_support >= minimum_rotations
            ),
            "presentation_robust_right_pattern_rule": all(
                _metric(row) >= right_minimum for row in results
            ),
            "zero_contract_failure_rule": total_contract_failures
            <= int(plan["decision_rules"]["maximum_total_contract_failures"]),
        },
        "interpretation_boundary": (
            "Engineering diagnostics only. This single-seed audit does not establish "
            "reuse, adoption, belief change, causal social amplification, or a "
            "population-level model property."
        ),
        "analysis_restrictions_applied": plan["analysis_restrictions"],
        "research_eligible": False,
    }
