"""Phase-aware, mechanical warning-fidelity metrics for disaster runs.

This module is deliberately separate from ``disaster_metric_core``.  Version
1 is a frozen exact-warning-identifier metric; version 2 adds a prospective
surface-fact contract without changing or reinterpreting version 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from engine.disaster import contains_warning_identifier
from engine.provenance import collect_git_info, file_manifest
from tools.validate_run import validate_run


DISASTER_METRIC_V2_VERSION = "disaster-metric-v2.0.0"
ANALYSIS_SCHEMA_VERSION = "disaster-metric-v2-analysis-v1.0.0"
DERIVED_MANIFEST_SCHEMA_VERSION = "disaster-metric-v2-manifest-v1.0.0"
METRIC_SPEC_RELATIVE_PATH = Path("docs") / "DISASTER_METRIC_V2_SPEC.md"
SUPPORTED_LOG_SCHEMA_VERSION = "2.0.0"
FACT_STATUSES = (
    "match",
    "recognized_conflict",
    "mixed",
    "unrecognized",
)
REQUIRED_INPUT_FILES = (
    "run_meta.json",
    "phase1_raw.jsonl",
    "messages.jsonl",
    "warning_events.jsonl",
    "positions.jsonl",
)
DERIVED_DATA_FILES = (
    "analysis_meta.json",
    "agents.jsonl",
    "warning_outputs.jsonl",
    "summary.json",
)
IMPLEMENTATION_RELATIVE_PATHS = (
    Path("tools") / "disaster_metric_v2_core.py",
    Path("tools") / "disaster_metric_v2.py",
    Path("engine") / "disaster.py",
)


class InputValidationError(RuntimeError):
    """The raw run or requested publication layout is not eligible."""


class DerivedCollisionError(RuntimeError):
    """The immutable per-run derived leaf already exists."""


class DerivedPublicationError(RuntimeError):
    """A prepared derived artifact could not be atomically published."""


@dataclass(frozen=True)
class SourceRecord:
    """One parsed JSONL object and its immutable raw-line reference."""

    value: Mapping[str, Any]
    reference: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class PreparedAnalysis:
    run_id: str
    files: Mapping[str, bytes]


RectangleValue = tuple[int, int, int, int]
GeometryValue = tuple[RectangleValue, ...]


_IDENTIFIER_CHARACTERS = r"A-Za-z0-9._-"
_PROSE_IDENTIFIER_VALUE = (
    r"[A-Za-z0-9_-](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?"
)
_RECTANGLE_RE = re.compile(
    r"x\s*=\s*(-?\d+)\s*\.\.\s*(-?\d+)\s*,\s*"
    r"y\s*=\s*(-?\d+)\s*\.\.\s*(-?\d+)"
)
_OFFICIAL_WARNING_RE = re.compile(
    rf"Official warning\s+(?P<value>{_PROSE_IDENTIFIER_VALUE})"
)
_ISSUE_STEP_RE = re.compile(r"At step\s+(?P<value>-?\d+)\b")
_HAZARD_CLAUSE_RE = re.compile(
    rf"hazard classification\s+(?P<identifier>{_PROSE_IDENTIFIER_VALUE})\s+"
    r"covers\s+(?P<geometry>.*?)"
    r"(?=\.\s+Refuge areas:|$)",
    re.DOTALL,
)
_REFUGE_CLAUSE_RE = re.compile(
    r"Refuge areas:\s*(?P<refuges>.*?)(?=\.(?:\s|$)|$)",
    re.DOTALL,
)
_REFUGE_ITEM_RE = re.compile(
    rf"(?P<identifier>{_PROSE_IDENTIFIER_VALUE})\s+"
    r"x\s*=\s*(?P<x_min>-?\d+)\s*\.\.\s*(?P<x_max>-?\d+)\s*,\s*"
    r"y\s*=\s*(?P<y_min>-?\d+)\s*\.\.\s*(?P<y_max>-?\d+)"
)


def canonical_json_bytes(value: Any, *, indent: Optional[int] = None) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=indent,
        separators=(",", ":") if indent is None else None,
    )
    return (text + "\n").encode("utf-8")


def canonical_jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for row in rows
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _value(record: SourceRecord | Mapping[str, Any]) -> Mapping[str, Any]:
    return record.value if isinstance(record, SourceRecord) else record


def _reference(
    record: SourceRecord | Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    if not isinstance(record, SourceRecord) or record.reference is None:
        return None
    return dict(record.reference)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _rectangle_value(value: Any) -> Optional[RectangleValue]:
    if not isinstance(value, Mapping):
        return None
    keys = ("x_min", "x_max", "y_min", "y_max")
    if not all(_is_integer(value.get(key)) for key in keys):
        return None
    return tuple(int(value[key]) for key in keys)  # type: ignore[return-value]


def _geometry_value(value: Any) -> Optional[GeometryValue]:
    if not isinstance(value, list):
        return None
    rectangles = [_rectangle_value(item) for item in value]
    if any(item is None for item in rectangles):
        return None
    return tuple(sorted(item for item in rectangles if item is not None))


def _geometry_from_text(value: str) -> Optional[GeometryValue]:
    rectangles = [
        tuple(int(part) for part in match.groups())
        for match in _RECTANGLE_RE.finditer(value)
    ]
    return tuple(sorted(rectangles)) if rectangles else None


def _contains_identifier(text: str, identifier: str) -> bool:
    token_character = _IDENTIFIER_CHARACTERS
    return re.search(
        (
            rf"(?<![{token_character}]){re.escape(identifier)}"
            r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
        ),
        text,
    ) is not None


def _json_values(text: str) -> Iterable[Any]:
    """Yield JSON values beginning at visible object/array delimiters.

    The grammar is intentionally bounded.  It does not repair JSON, interpret
    markdown, or ask a model to judge prose.
    """

    decoder = json.JSONDecoder()
    seen: set[tuple[int, int]] = set()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        identity = (index, index + end)
        if identity not in seen:
            seen.add(identity)
            yield value


def _walk_json(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _status(expected: Any, claims: Iterable[Any]) -> str:
    unique: list[Any] = []
    for claim in claims:
        if claim not in unique:
            unique.append(claim)
    if not unique:
        return "unrecognized"
    matched = any(claim == expected for claim in unique)
    conflicted = any(claim != expected for claim in unique)
    if matched and conflicted:
        return "mixed"
    return "match" if matched else "recognized_conflict"


def canonical_warning_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Build slots from a warning-facts object or its owning scenario."""

    if "official_warning" in value:
        warning = value["official_warning"]
        warning_id = warning["warning_id"]
        issue_step = warning["issue_step"]
        hazard_id = value["hazard"]["hazard_id"]
        active_rectangles: list[Mapping[str, Any]] = []
        for stage in value["hazard"]["stages"]:
            if stage["start_step"] > issue_step:
                break
            active_rectangles = stage["rectangles"]
        refuge_rows = value["refuges"]
    else:
        warning_id = value["warning_id"]
        issue_step = value["issue_step"]
        hazard_id = value["hazard_id"]
        active_rectangles = value["hazard_rectangles"]
        refuge_rows = value["refuges"]
    geometry = _geometry_value(active_rectangles)
    if geometry is None or not geometry:
        raise InputValidationError(
            "official warning has no mechanically valid issue-time hazard geometry"
        )
    refuges: dict[str, RectangleValue] = {}
    for refuge in refuge_rows:
        refuge_id = refuge.get("refuge_id")
        rectangle = _rectangle_value(refuge.get("rectangle"))
        if not isinstance(refuge_id, str) or rectangle is None:
            raise InputValidationError("scenario refuge facts are invalid")
        refuges[refuge_id] = rectangle
    return {
        "warning_specific": {
            "warning_id": warning_id,
            "issue_step": issue_step,
            "hazard_id": hazard_id,
            "hazard_geometry_at_issue": geometry,
        },
        "shared_context": {
            f"refuge:{refuge_id}": {
                "refuge_id": refuge_id,
                "rectangle": rectangle,
            }
            for refuge_id, rectangle in sorted(refuges.items())
        },
    }


def _public_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    warning = contract["warning_specific"]
    return {
        "warning_specific": {
            "warning_id": warning["warning_id"],
            "issue_step": warning["issue_step"],
            "hazard_id": warning["hazard_id"],
            "hazard_geometry_at_issue": [
                {
                    "x_min": rectangle[0],
                    "x_max": rectangle[1],
                    "y_min": rectangle[2],
                    "y_max": rectangle[3],
                }
                for rectangle in warning["hazard_geometry_at_issue"]
            ],
        },
        "shared_context": {
            slot: {
                "refuge_id": value["refuge_id"],
                "rectangle": {
                    "x_min": value["rectangle"][0],
                    "x_max": value["rectangle"][1],
                    "y_min": value["rectangle"][2],
                    "y_max": value["rectangle"][3],
                },
            }
            for slot, value in contract["shared_context"].items()
        },
    }


def classify_warning_facts(
    text: str,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify frozen surface claims; never infer semantic equivalence."""

    if not isinstance(text, str):
        raise TypeError("warning-fidelity input must be a string")
    warning = contract["warning_specific"]
    claims: dict[str, list[Any]] = {
        "warning_id": [],
        "issue_step": [],
        "hazard_id": [],
        "hazard_geometry_at_issue": [],
    }
    refuge_claims: dict[str, list[dict[str, Any]]] = {
        slot: [] for slot in contract["shared_context"]
    }

    if contains_warning_identifier(text, warning["warning_id"]):
        claims["warning_id"].append(warning["warning_id"])
    if _contains_identifier(text, warning["hazard_id"]):
        claims["hazard_id"].append(warning["hazard_id"])

    for match in _OFFICIAL_WARNING_RE.finditer(text):
        claims["warning_id"].append(match.group("value"))
    for match in _ISSUE_STEP_RE.finditer(text):
        claims["issue_step"].append(int(match.group("value")))
    for match in _HAZARD_CLAUSE_RE.finditer(text):
        claims["hazard_id"].append(match.group("identifier"))
        geometry = _geometry_from_text(match.group("geometry"))
        if geometry is not None:
            claims["hazard_geometry_at_issue"].append(geometry)
    for match in _REFUGE_CLAUSE_RE.finditer(text):
        for item in _REFUGE_ITEM_RE.finditer(match.group("refuges")):
            refuge_id = item.group("identifier")
            slot = f"refuge:{refuge_id}"
            if slot in refuge_claims:
                refuge_claims[slot].append({
                    "refuge_id": refuge_id,
                    "rectangle": tuple(
                        int(item.group(key))
                        for key in ("x_min", "x_max", "y_min", "y_max")
                    ),
                })

    for json_value in _json_values(text):
        for row in _walk_json(json_value):
            if isinstance(row.get("warning_id"), str):
                claims["warning_id"].append(row["warning_id"])
            if _is_integer(row.get("issue_step")):
                claims["issue_step"].append(row["issue_step"])
            if isinstance(row.get("hazard_id"), str):
                claims["hazard_id"].append(row["hazard_id"])
            if "hazard_rectangles" in row:
                geometry = _geometry_value(row["hazard_rectangles"])
                if geometry is not None:
                    claims["hazard_geometry_at_issue"].append(geometry)
            refuge_id = row.get("refuge_id")
            rectangle = _rectangle_value(row.get("rectangle"))
            slot = f"refuge:{refuge_id}"
            if isinstance(refuge_id, str) and rectangle is not None and slot in refuge_claims:
                refuge_claims[slot].append({
                    "refuge_id": refuge_id,
                    "rectangle": rectangle,
                })

    warning_statuses = {
        slot: _status(warning[slot], claims[slot])
        for slot in (
            "warning_id",
            "issue_step",
            "hazard_id",
            "hazard_geometry_at_issue",
        )
    }
    shared_statuses = {
        slot: _status(expected, refuge_claims[slot])
        for slot, expected in contract["shared_context"].items()
    }
    return {
        "contract": "frozen-surface-claims-v1.0.0",
        "warning_specific": warning_statuses,
        "shared_context": shared_statuses,
        "warning_specific_status_counts": {
            status: sum(value == status for value in warning_statuses.values())
            for status in FACT_STATUSES
        },
        "shared_context_status_counts": {
            status: sum(value == status for value in shared_statuses.values())
            for status in FACT_STATUSES
        },
    }


def _exposure_phase_order(source_type: str) -> int:
    if source_type == "official":
        return 0  # official delivery occurs before Phase 1
    if source_type == "agent_relay":
        return 2  # relay delivery occurs after Phase 1 and before Phase 3
    raise InputValidationError(f"unknown warning exposure source_type: {source_type!r}")


def _clock_precedes(exposure: Mapping[str, Any], step: int, phase_order: int) -> bool:
    return (exposure["step"], exposure["phase_order"]) < (step, phase_order)


def _stable_output_id(run_id: str, step: int, agent_id: int) -> str:
    return f"{run_id}:warning_output:{step:06d}:agent-{agent_id:06d}"


def _position_rows(
    positions: Sequence[SourceRecord | Mapping[str, Any]],
) -> tuple[
    dict[int, SourceRecord | Mapping[str, Any]],
    dict[tuple[int, int], SourceRecord | Mapping[str, Any]],
]:
    initial: dict[int, SourceRecord | Mapping[str, Any]] = {}
    post: dict[tuple[int, int], SourceRecord | Mapping[str, Any]] = {}
    for source in positions:
        row = _value(source)
        if row.get("phase") == "initial":
            initial[int(row["agent_id"])] = source
        elif row.get("phase") == "post_movement":
            post[(int(row["step"]), int(row["agent_id"]))] = source
    return initial, post


def derive_disaster_metrics_v2(
    *,
    run_meta: Mapping[str, Any],
    positions: Sequence[SourceRecord | Mapping[str, Any]],
    phase1: Sequence[SourceRecord | Mapping[str, Any]],
    messages: Sequence[SourceRecord | Mapping[str, Any]],
    warning_events: Sequence[SourceRecord | Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive deterministic per-output and per-agent v2 observations."""

    config = run_meta["config"]
    scenario = config["scenario"]
    warning_id = scenario["official_warning"]["warning_id"]
    expected_steps = int(run_meta["expected_steps"])
    expected_agents = int(run_meta["expected_agents"])
    run_id = str(run_meta["run_id"])
    issue_sources = [
        source
        for source in warning_events
        if _value(source).get("event_type") == "warning_issued"
    ]
    if (
        len(issue_sources) != 1
        or not isinstance(_value(issue_sources[0]).get("facts"), Mapping)
    ):
        raise InputValidationError("warning events must contain one canonical facts row")
    issue_row = _value(issue_sources[0])
    contract = canonical_warning_contract(issue_row["facts"])
    if contract != canonical_warning_contract(scenario):
        raise InputValidationError("issued warning facts differ from the public scenario")

    exposure_by_agent: dict[int, list[dict[str, Any]]] = {
        agent_id: [] for agent_id in range(expected_agents)
    }
    relay_exposures_by_sender_step: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for source in warning_events:
        row = _value(source)
        if row.get("event_type") != "warning_exposure":
            continue
        if row.get("warning_id") != warning_id:
            raise InputValidationError("warning exposure identifier differs from scenario")
        source_type = row.get("source_type")
        phase_order = _exposure_phase_order(str(source_type))
        exposure = {
            "event_id": row.get("event_id"),
            "step": int(row["step"]),
            "phase": "pre_phase1" if phase_order == 0 else "phase2_delivery",
            "phase_order": phase_order,
            "source_type": source_type,
            "sender_id": row.get("sender_id"),
            "recipient_id": int(row["recipient_id"]),
            "source_reference": _reference(source),
        }
        exposure_by_agent[exposure["recipient_id"]].append(exposure)
        if source_type == "agent_relay" and _is_integer(row.get("sender_id")):
            relay_exposures_by_sender_step.setdefault(
                (int(row["step"]), int(row["sender_id"])), []
            ).append(exposure)
    for rows in exposure_by_agent.values():
        rows.sort(key=lambda row: (
            row["step"], row["phase_order"], str(row["event_id"])
        ))

    delivered_by_sender_step: dict[
        tuple[int, int], SourceRecord | Mapping[str, Any]
    ] = {}
    for source in messages:
        row = _value(source)
        key = (int(row["step"]), int(row["sender_id"]))
        if key in delivered_by_sender_step:
            raise InputValidationError(f"duplicate delivered message key: {key!r}")
        delivered_by_sender_step[key] = source

    phase1_by_agent: dict[int, list[SourceRecord | Mapping[str, Any]]] = {
        agent_id: [] for agent_id in range(expected_agents)
    }
    for source in phase1:
        row = _value(source)
        phase1_by_agent[int(row["agent_id"])].append(source)
    for rows in phase1_by_agent.values():
        rows.sort(key=lambda source: int(_value(source)["step"]))

    outputs: list[dict[str, Any]] = []
    output_by_agent: dict[int, list[dict[str, Any]]] = {
        agent_id: [] for agent_id in range(expected_agents)
    }
    for agent_id in range(expected_agents):
        exposures = exposure_by_agent[agent_id]
        for source in phase1_by_agent[agent_id]:
            row = _value(source)
            step = int(row["step"])
            prior = [
                exposure for exposure in exposures
                if _clock_precedes(exposure, step, 1)
            ]
            parsed = row.get("parsed")
            if not isinstance(parsed, Mapping) or not isinstance(parsed.get("message"), str):
                raise InputValidationError("completed Phase 1 row has no parsed message")
            text = parsed["message"]
            carrier = contains_warning_identifier(text, warning_id)
            if not prior and not carrier:
                continue
            delivery_source = delivered_by_sender_step.get((step, agent_id))
            delivery = _value(delivery_source) if delivery_source is not None else None
            if delivery is not None and delivery.get("message") != text:
                raise InputValidationError("delivered message differs from Phase 1 output")
            receiver_ids = list(delivery.get("receiver_ids", [])) if delivery else []
            relay_exposures = relay_exposures_by_sender_step.get((step, agent_id), [])
            if carrier and delivery is not None:
                relay_status = "generated_and_delivered"
            elif carrier:
                relay_status = "generated_not_delivered"
            else:
                relay_status = "not_exact_id_carrier"
            output = {
                "event_id": _stable_output_id(run_id, step, agent_id),
                "step": step,
                "phase": "phase1",
                "agent_id": agent_id,
                "bloc": row.get("bloc"),
                "model": row.get("model"),
                "message_reference": _reference(source),
                "message_sha256": sha256_bytes(text.encode("utf-8")),
                "message_characters": len(text),
                "source_classification": (
                    "post_exposure_output"
                    if prior
                    else "unattributed_exact_id_carrier"
                ),
                "post_exposure_eligible": bool(prior),
                "exact_warning_id_carrier": carrier,
                "later_step_reuse_eligible": any(
                    exposure["step"] < step for exposure in prior
                ),
                "fact_statuses": classify_warning_facts(text, contract),
                "eligible_prior_exposure_count": len(prior),
                "eligible_prior_exposure_event_ids": [
                    exposure["event_id"] for exposure in prior
                ],
                "eligible_prior_exposure_references": [
                    exposure["source_reference"] for exposure in prior
                    if exposure["source_reference"] is not None
                ],
                "causal_parent_inferred": False,
                "relay_status": relay_status,
                "delivered_receiver_ids": receiver_ids,
                "delivered_receiver_count": len(receiver_ids),
                "delivery_reference": (
                    _reference(delivery_source) if delivery_source is not None else None
                ),
                "relay_exposure_event_ids": [
                    exposure["event_id"] for exposure in relay_exposures
                ],
            }
            outputs.append(output)
            if prior:
                output_by_agent[agent_id].append(output)

    initial, post = _position_rows(positions)
    agents: list[dict[str, Any]] = []
    for agent_id in range(expected_agents):
        post_sources = [
            post[(step, agent_id)] for step in range(1, expected_steps + 1)
        ]
        post_rows = [_value(source) for source in post_sources]
        exposures = exposure_by_agent[agent_id]
        first_clock = (
            (exposures[0]["step"], exposures[0]["phase_order"])
            if exposures else None
        )
        first_exposures = (
            [
                exposure for exposure in exposures
                if (exposure["step"], exposure["phase_order"]) == first_clock
            ]
            if first_clock is not None else []
        )
        eligible_outputs = output_by_agent[agent_id]
        first_output = eligible_outputs[0] if eligible_outputs else None
        first_reuse = next(
            (
                output for output in eligible_outputs
                if output["exact_warning_id_carrier"]
                and output["later_step_reuse_eligible"]
            ),
            None,
        )

        distance_decrease_step = None
        distance_decrease_before_reference = None
        distance_decrease_after_reference = None
        if exposures:
            for step in range(1, expected_steps + 1):
                if not any(
                    _clock_precedes(exposure, step, 3) for exposure in exposures
                ):
                    continue
                before_source = (
                    initial[agent_id]
                    if step == 1
                    else post[(step - 1, agent_id)]
                )
                after_source = post[(step, agent_id)]
                before = _value(before_source)
                after = _value(after_source)
                if (
                    after["shortest_refuge_distance"]
                    < before["shortest_refuge_distance"]
                ):
                    distance_decrease_step = step
                    distance_decrease_before_reference = _reference(before_source)
                    distance_decrease_after_reference = _reference(after_source)
                    break

        final_refuge = post_rows[-1]["refuge_id"]
        completion_step = None
        if final_refuge is not None:
            completion_step = expected_steps
            for step in range(expected_steps - 1, 0, -1):
                if _value(post[(step, agent_id)])["refuge_id"] is None:
                    break
                completion_step = step
        first_exposure_step = first_clock[0] if first_clock else None
        agents.append({
            "agent_id": agent_id,
            "bloc": post_rows[-1]["bloc"],
            "model": post_rows[-1]["model"],
            "first_warning_exposure_step": first_exposure_step,
            "first_warning_exposure_phase": (
                first_exposures[0]["phase"] if first_exposures else None
            ),
            "first_warning_exposure_event_ids": [
                exposure["event_id"] for exposure in first_exposures
            ],
            "warning_exposure_count": len(exposures),
            "first_eligible_output_event_id": (
                first_output["event_id"] if first_output else None
            ),
            "first_eligible_output_fact_statuses": (
                first_output["fact_statuses"] if first_output else None
            ),
            "first_exact_id_reuse_event_id": (
                first_reuse["event_id"] if first_reuse else None
            ),
            "warning_reuse_step": first_reuse["step"] if first_reuse else None,
            "warning_reuse_delay_steps": (
                first_reuse["step"] - first_exposure_step
                if first_reuse is not None and first_exposure_step is not None
                else None
            ),
            "warning_reuse_right_censored": bool(exposures and first_reuse is None),
            "warning_reuse_censor_step": (
                expected_steps if exposures and first_reuse is None else None
            ),
            "causal_parent_inferred": False,
            "first_post_exposure_refuge_distance_decrease_step": (
                distance_decrease_step
            ),
            "first_post_exposure_refuge_distance_decrease_delay_steps": (
                distance_decrease_step - first_exposure_step
                if distance_decrease_step is not None
                and first_exposure_step is not None
                else None
            ),
            "first_post_exposure_refuge_distance_decrease_before_reference": (
                distance_decrease_before_reference
            ),
            "first_post_exposure_refuge_distance_decrease_after_reference": (
                distance_decrease_after_reference
            ),
            "dangerous_area_residence_steps": sum(
                bool(row["hazardous"]) for row in post_rows
            ),
            "evacuation_success": final_refuge is not None,
            "final_refuge_id": final_refuge,
            "evacuation_completion_step": completion_step,
            "final_position_reference": _reference(post_sources[-1]),
            "evacuation_completion_position_reference": (
                _reference(post[(completion_step, agent_id)])
                if completion_step is not None
                else None
            ),
        })

    outputs.sort(key=lambda row: (row["step"], row["agent_id"]))
    agents.sort(key=lambda row: row["agent_id"])
    exposed_agents = [row for row in agents if row["first_warning_exposure_step"] is not None]
    reused_agents = [row for row in agents if row["warning_reuse_step"] is not None]
    post_exposure_outputs = [
        output for output in outputs if output["post_exposure_eligible"]
    ]
    unattributed_carriers = [
        output
        for output in outputs
        if output["source_classification"] == "unattributed_exact_id_carrier"
    ]
    status_totals = {
        group: {
            status: sum(
                output["fact_statuses"][group][slot] == status
                for output in post_exposure_outputs
                for slot in output["fact_statuses"][group]
            )
            for status in FACT_STATUSES
        }
        for group in ("warning_specific", "shared_context")
    }
    summary = {
        "schema_version": "disaster-metric-v2-summary-v1.0.0",
        "metric_version": DISASTER_METRIC_V2_VERSION,
        "source_run_id": run_id,
        "communication_mode": scenario["communication_mode"],
        "agent_count": expected_agents,
        "warning_exposed_agent_count": len(exposed_agents),
        "eligible_warning_output_count": len(post_exposure_outputs),
        "unattributed_exact_id_carrier_output_count": len(
            unattributed_carriers
        ),
        "agents_with_first_eligible_output_count": sum(
            row["first_eligible_output_event_id"] is not None for row in agents
        ),
        "warning_reused_agent_count": len(reused_agents),
        "warning_reuse_right_censored_count": sum(
            row["warning_reuse_right_censored"] for row in agents
        ),
        "exact_id_carrier_output_count": sum(
            output["exact_warning_id_carrier"] for output in outputs
        ),
        "same_step_post_exposure_exact_id_carrier_count": sum(
            output["exact_warning_id_carrier"]
            and not output["later_step_reuse_eligible"]
            for output in post_exposure_outputs
        ),
        "later_step_exact_id_reuse_output_count": sum(
            output["exact_warning_id_carrier"]
            and output["later_step_reuse_eligible"]
            for output in outputs
        ),
        "exact_id_carrier_delivered_message_count": sum(
            output["relay_status"] == "generated_and_delivered" for output in outputs
        ),
        "exact_id_carrier_generated_not_delivered_count": sum(
            output["relay_status"] == "generated_not_delivered" for output in outputs
        ),
        "all_exact_id_relay_exposure_edge_count": sum(
            len(output["relay_exposure_event_ids"]) for output in outputs
        ),
        "post_exposure_exact_id_relay_exposure_edge_count": sum(
            len(output["relay_exposure_event_ids"])
            for output in post_exposure_outputs
        ),
        "post_exposure_refuge_distance_decrease_agent_count": sum(
            row["first_post_exposure_refuge_distance_decrease_step"] is not None
            for row in agents
        ),
        "evacuation_success_count": sum(row["evacuation_success"] for row in agents),
        "dangerous_area_residence_steps_total": sum(
            row["dangerous_area_residence_steps"] for row in agents
        ),
        "fact_status_totals": status_totals,
        "causal_parent_inferred": False,
    }
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "metric_version": DISASTER_METRIC_V2_VERSION,
        "source_run_id": run_id,
        "source_log_schema_version": run_meta.get("log_schema_version"),
        "source_declared_metric_version": run_meta.get("metric_version"),
        "communication_mode": scenario["communication_mode"],
        "warning_id": warning_id,
        "canonical_warning_issue_reference": _reference(issue_sources[0]),
        "canonical_fact_contract": _public_contract(contract),
        "fact_interpretation": {
            "method": "frozen_mechanical_surface_recognition",
            "llm_judge_used": False,
            "semantic_equivalence_inferred": False,
            "refuge_fact_scope": "shared_context",
            "causal_parent_inferred": False,
        },
        "agents": agents,
        "outputs": outputs,
        "summary": summary,
    }


def read_jsonl_source_records(path: Path) -> list[SourceRecord]:
    rows: list[SourceRecord] = []
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                raise InputValidationError(f"blank JSONL row: {path.name}:{line_number}")
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InputValidationError(
                    f"invalid JSONL row: {path.name}:{line_number}"
                ) from error
            if not isinstance(value, dict):
                raise InputValidationError(
                    f"non-object JSONL row: {path.name}:{line_number}"
                )
            rows.append(SourceRecord(
                value=value,
                reference={
                    "file": path.name,
                    "line_number": line_number,
                    "line_sha256": sha256_bytes(raw_line),
                    "line_bytes": len(raw_line),
                },
            ))
    return rows


def _metric_spec_path(repo_root: Path) -> Path:
    return repo_root / METRIC_SPEC_RELATIVE_PATH


def _validate_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InputValidationError(f"{label} must be a lowercase SHA-256 digest")


def _derived_manifest(files: Mapping[str, bytes]) -> bytes:
    return canonical_json_bytes({
        "schema_version": DERIVED_MANIFEST_SCHEMA_VERSION,
        "algorithm": "sha256",
        "files": {
            filename: {
                "sha256": sha256_bytes(content),
                "bytes": len(content),
                "lines": content.count(b"\n"),
            }
            for filename, content in sorted(files.items())
        },
    }, indent=2)


def prepare_run_analysis(
    run_dir: Path | str,
    expected_metric_spec_sha256: str,
    *,
    require_declared_metric: bool = False,
) -> PreparedAnalysis:
    _validate_sha256(expected_metric_spec_sha256, "metric spec SHA-256")
    raw_dir = Path(run_dir).resolve(strict=True)
    if not raw_dir.is_dir() or raw_dir.is_symlink():
        raise InputValidationError("run directory must be a real directory")
    report = validate_run(raw_dir, strict=True)
    if not report.valid:
        raise InputValidationError(
            "strict raw-run validation failed: " + "; ".join(report.errors[:3])
        )
    meta_path = raw_dir / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    run_id = meta.get("run_id")
    if not isinstance(run_id, str) or raw_dir.name != f"output_{run_id}":
        raise InputValidationError("run directory identity does not match run metadata")
    if meta.get("status") != "completed" or meta.get("aborted") is not False:
        raise InputValidationError("disaster metric v2 requires a completed run")
    if meta.get("log_schema_version") != SUPPORTED_LOG_SCHEMA_VERSION:
        raise InputValidationError(
            "disaster metric v2 requires completed log schema 2.0.0"
        )
    config = meta.get("config")
    if not isinstance(config, dict) or not isinstance(config.get("scenario"), dict):
        raise InputValidationError("disaster metric v2 requires a disaster scenario")
    simulation = config.get("simulation")
    if not isinstance(simulation, dict):
        raise InputValidationError("source run simulation config is invalid")

    repo_root = Path(__file__).resolve().parents[1]
    spec_path = _metric_spec_path(repo_root)
    actual_spec_sha256 = sha256_bytes(spec_path.read_bytes())
    if actual_spec_sha256 != expected_metric_spec_sha256:
        raise InputValidationError("metric spec SHA-256 differs from the expected digest")
    declared_metric_version_matches = (
        meta.get("metric_version") == DISASTER_METRIC_V2_VERSION
        and simulation.get("metric_version") == DISASTER_METRIC_V2_VERSION
    )
    declared_metric_spec_sha256 = simulation.get("metric_spec_sha256")
    declared_metric_spec_matches = (
        declared_metric_spec_sha256 == actual_spec_sha256
    )
    declared_matches = (
        declared_metric_version_matches and declared_metric_spec_matches
    )
    if require_declared_metric and not declared_matches:
        raise InputValidationError(
            "source run did not prospectively declare the exact "
            "disaster-metric-v2.0.0 specification"
        )
    for filename in REQUIRED_INPUT_FILES:
        if not (raw_dir / filename).is_file():
            raise InputValidationError(f"required metric input is missing: {filename}")

    result = derive_disaster_metrics_v2(
        run_meta=meta,
        positions=read_jsonl_source_records(raw_dir / "positions.jsonl"),
        phase1=read_jsonl_source_records(raw_dir / "phase1_raw.jsonl"),
        messages=read_jsonl_source_records(raw_dir / "messages.jsonl"),
        warning_events=read_jsonl_source_records(raw_dir / "warning_events.jsonl"),
    )
    input_manifests = {
        filename: file_manifest(raw_dir / filename)
        for filename in REQUIRED_INPUT_FILES
    }
    blocs = config.get("blocs", [])
    analyzer_git = collect_git_info(repo_root)
    implementation_manifests = {
        path.as_posix(): file_manifest(repo_root / path)
        for path in IMPLEMENTATION_RELATIVE_PATHS
    }
    analysis_meta = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "status": "completed",
        "metric_version": DISASTER_METRIC_V2_VERSION,
        "metric_spec_path": METRIC_SPEC_RELATIVE_PATH.as_posix(),
        "metric_spec_sha256": actual_spec_sha256,
        "source_run_id": run_id,
        "source_protocol_version": meta.get("protocol_version"),
        "source_log_schema_version": meta.get("log_schema_version"),
        "source_declared_metric_version": meta.get("metric_version"),
        "source_declared_metric_spec_sha256": declared_metric_spec_sha256,
        "source_declared_metric_version_matches_analysis": (
            declared_metric_version_matches
        ),
        "source_declared_metric_spec_matches_analysis": (
            declared_metric_spec_matches
        ),
        "source_declared_metric_matches_analysis": declared_matches,
        "source_research_eligible": simulation.get("research_eligible") is True,
        "source_config_sha256": meta.get("config_hash"),
        "source_git_sha": meta.get("git_sha"),
        "source_git_dirty": meta.get("git_dirty"),
        "source_raw_manifest": meta.get("raw_manifest"),
        "input_manifests": input_manifests,
        "analyzer_git_sha": analyzer_git["git_sha"],
        "analyzer_git_dirty": analyzer_git["git_dirty"],
        "analyzer_git_probe_status": analyzer_git["git_probe_status"],
        "analyzer_git_probe_errors": analyzer_git["git_probe_errors"],
        "implementation_manifests": implementation_manifests,
        "matrix_keys": {
            "run_id": run_id,
            "seed": simulation.get("seed"),
            "communication_mode": meta["config"]["scenario"]["communication_mode"],
            "blocs": [
                {
                    "name": bloc.get("name"),
                    "model": bloc.get("model"),
                    "num_agents": bloc.get("num_agents"),
                }
                for bloc in blocs
            ],
        },
        "mechanical_surface_recognition_only": True,
        "llm_judge_used": False,
        "causal_parent_inferred": False,
        "refuge_fact_scope": "shared_context",
    }
    files: dict[str, bytes] = {
        "analysis_meta.json": canonical_json_bytes(analysis_meta, indent=2),
        "agents.jsonl": canonical_jsonl_bytes(result["agents"]),
        "warning_outputs.jsonl": canonical_jsonl_bytes(result["outputs"]),
        "summary.json": canonical_json_bytes({
            **result["summary"],
            "canonical_fact_contract": result["canonical_fact_contract"],
            "canonical_warning_issue_reference": result[
                "canonical_warning_issue_reference"
            ],
            "fact_interpretation": result["fact_interpretation"],
            "source_declared_metric_spec_matches_analysis": (
                declared_metric_spec_matches
            ),
            "source_declared_metric_matches_analysis": declared_matches,
        }, indent=2),
    }
    files["derived_manifest.json"] = _derived_manifest(files)
    return PreparedAnalysis(run_id=run_id, files=files)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _containing_raw_run(path: Path) -> Optional[Path]:
    for candidate in (path, *path.parents):
        if (
            candidate.name.startswith("output_")
            and (candidate / "run_meta.json").is_file()
        ):
            return candidate
    return None


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_prepared_files(prepared: PreparedAnalysis) -> None:
    required = {*DERIVED_DATA_FILES, "derived_manifest.json"}
    if set(prepared.files) != required:
        raise DerivedPublicationError("prepared derived file set is invalid")
    manifest = json.loads(prepared.files["derived_manifest.json"])
    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != set(DERIVED_DATA_FILES):
        raise DerivedPublicationError("prepared derived manifest is invalid")
    for filename in DERIVED_DATA_FILES:
        content = prepared.files[filename]
        if entries[filename] != {
            "sha256": sha256_bytes(content),
            "bytes": len(content),
            "lines": content.count(b"\n"),
        }:
            raise DerivedPublicationError(f"prepared hash differs: {filename}")


def write_prepared_analysis(
    prepared: PreparedAnalysis,
    run_dir: Path | str,
    derived_root: Path | str,
) -> Path:
    """Publish through same-filesystem staging; never overwrite a final leaf."""

    _verify_prepared_files(prepared)
    raw_dir = Path(run_dir).resolve(strict=True)
    root = Path(derived_root)
    if root.exists() and root.is_symlink():
        raise InputValidationError("derived root may not be a symbolic link")
    unresolved_root = root.resolve(strict=False)
    if (
        _is_within(unresolved_root, raw_dir)
        or _containing_raw_run(unresolved_root) is not None
    ):
        raise InputValidationError("derived root may not be inside any raw run")
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve(strict=True)
    if (
        root.is_symlink()
        or _is_within(resolved_root, raw_dir)
        or _containing_raw_run(resolved_root) is not None
    ):
        raise InputValidationError("derived root resolves inside a raw run")
    version_dir = root / DISASTER_METRIC_V2_VERSION
    version_dir.mkdir(exist_ok=True)
    if version_dir.is_symlink():
        raise InputValidationError("metric version directory may not be a symlink")
    staging_dir = version_dir / ".staging"
    staging_dir.mkdir(exist_ok=True)
    if staging_dir.is_symlink():
        raise InputValidationError("metric staging directory may not be a symlink")
    final_leaf = version_dir / prepared.run_id
    if os.path.lexists(final_leaf):
        raise DerivedCollisionError(
            f"derived output already exists for run ID {prepared.run_id!r}"
        )
    staging_leaf = staging_dir / f"{prepared.run_id}-{uuid.uuid4().hex}"
    staging_leaf.mkdir(exist_ok=False)
    for filename in (*DERIVED_DATA_FILES, "derived_manifest.json"):
        _write_fsynced(staging_leaf / filename, prepared.files[filename])
    staged = {
        path.name: path.read_bytes()
        for path in staging_leaf.iterdir()
        if path.is_file()
    }
    if staged != dict(prepared.files):
        raise DerivedPublicationError("staged derived bytes differ from preparation")
    if os.path.lexists(final_leaf):
        raise DerivedCollisionError(
            f"derived output already exists for run ID {prepared.run_id!r}"
        )
    try:
        os.rename(staging_leaf, final_leaf)
    except OSError as error:
        if os.path.lexists(final_leaf):
            raise DerivedCollisionError(
                f"derived output already exists for run ID {prepared.run_id!r}"
            ) from error
        raise DerivedPublicationError(
            f"derived output could not be published: {type(error).__name__}"
        ) from error
    return final_leaf


def analyze_run(
    run_dir: Path | str,
    derived_root: Path | str,
    expected_metric_spec_sha256: str,
    *,
    require_declared_metric: bool = False,
) -> Path:
    prepared = prepare_run_analysis(
        run_dir,
        expected_metric_spec_sha256,
        require_declared_metric=require_declared_metric,
    )
    return write_prepared_analysis(prepared, run_dir, derived_root)
