"""Prospective, config-owned disaster world state and neutral warning facts."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


DISASTER_SCENARIO_SCHEMA_VERSION = "disaster-scenario-v1.0.0"
DISASTER_SCENARIO_TYPE = "disaster_v1"
COMMUNICATION_MODES = frozenset({
    "free_text",
    "structured_warning",
    "communication_none",
})


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(sorted(unknown))}")


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or not all(character.isalnum() or character in "._-" for character in value)
    ):
        raise ValueError(
            f"{label} must be a 1-128 character identifier using letters, "
            "digits, '.', '_', or '-'"
        )
    return value


@dataclass(frozen=True, order=True)
class Rectangle:
    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def contains(self, x: int, y: int) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max

    def overlaps(self, other: "Rectangle") -> bool:
        return not (
            self.x_max < other.x_min
            or other.x_max < self.x_min
            or self.y_max < other.y_min
            or other.y_max < self.y_min
        )

    def cells(self) -> Iterable[tuple[int, int]]:
        for x in range(self.x_min, self.x_max + 1):
            for y in range(self.y_min, self.y_max + 1):
                yield x, y

    def manhattan_distance(self, x: int, y: int) -> int:
        dx = max(self.x_min - x, 0, x - self.x_max)
        dy = max(self.y_min - y, 0, y - self.y_max)
        return dx + dy

    def as_dict(self) -> dict[str, int]:
        return {
            "x_min": self.x_min,
            "x_max": self.x_max,
            "y_min": self.y_min,
            "y_max": self.y_max,
        }


@dataclass(frozen=True)
class HazardStage:
    start_step: int
    rectangles: tuple[Rectangle, ...]


@dataclass(frozen=True)
class Refuge:
    refuge_id: str
    rectangle: Rectangle


@dataclass(frozen=True)
class OfficialWarning:
    warning_id: str
    issue_step: int
    initial_recipient_ids: tuple[int, ...]


@dataclass(frozen=True)
class DisasterScenario:
    communication_mode: str
    hazard_id: str
    hazard_stages: tuple[HazardStage, ...]
    refuges: tuple[Refuge, ...]
    official_warning: OfficialWarning
    initial_eligible_rectangles: tuple[Rectangle, ...]

    def active_hazard_rectangles(self, step: int) -> tuple[Rectangle, ...]:
        active: tuple[Rectangle, ...] = ()
        for stage in self.hazard_stages:
            if stage.start_step > step:
                break
            active = stage.rectangles
        return active

    def is_hazardous(self, step: int, x: int, y: int) -> bool:
        return any(rectangle.contains(x, y) for rectangle in self.active_hazard_rectangles(step))

    def refuge_for(self, x: int, y: int) -> Optional[Refuge]:
        return next(
            (refuge for refuge in self.refuges if refuge.rectangle.contains(x, y)),
            None,
        )

    def shortest_refuge_distance(self, x: int, y: int) -> int:
        return min(refuge.rectangle.manhattan_distance(x, y) for refuge in self.refuges)

    def eligible_initial_cells(self) -> list[tuple[int, int]]:
        cells = {
            cell
            for rectangle in self.initial_eligible_rectangles
            for cell in rectangle.cells()
            if self.refuge_for(*cell) is None
        }
        return sorted(cells)

    def warning_facts(self) -> dict[str, Any]:
        warning = self.official_warning
        return {
            "warning_id": warning.warning_id,
            "issue_step": warning.issue_step,
            "hazard_id": self.hazard_id,
            "hazard_rectangles": [
                rectangle.as_dict()
                for rectangle in self.active_hazard_rectangles(warning.issue_step)
            ],
            "refuges": [
                {
                    "refuge_id": refuge.refuge_id,
                    "rectangle": refuge.rectangle.as_dict(),
                }
                for refuge in self.refuges
            ],
        }

    def warning_payload(self) -> str | dict[str, Any] | None:
        if self.communication_mode == "communication_none":
            return None
        facts = self.warning_facts()
        if self.communication_mode == "structured_warning":
            return copy.deepcopy(facts)
        rectangles = "; ".join(
            _rectangle_text(Rectangle(**row)) for row in facts["hazard_rectangles"]
        ) or "none"
        refuges = "; ".join(
            f"{row['refuge_id']} {_rectangle_text(Rectangle(**row['rectangle']))}"
            for row in facts["refuges"]
        )
        return (
            f"Official warning {facts['warning_id']}. At step {facts['issue_step']}, "
            f"hazard classification {facts['hazard_id']} covers {rectangles}. "
            f"Refuge areas: {refuges}."
        )


def _rectangle_text(rectangle: Rectangle) -> str:
    return (
        f"x={rectangle.x_min}..{rectangle.x_max}, "
        f"y={rectangle.y_min}..{rectangle.y_max}"
    )


def contains_warning_identifier(text: str, warning_id: str) -> bool:
    """Prospective exact-identifier detector for later reuse/relay metrics."""
    if not isinstance(text, str):
        return False
    token_character = r"A-Za-z0-9._-"
    return re.search(
        (
            rf"(?<![{token_character}]){re.escape(warning_id)}"
            r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9])"
        ),
        text,
    ) is not None


def _parse_rectangle(
    value: Any,
    label: str,
    half_space_size: int,
) -> Rectangle:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    _exact_keys(value, {"x_min", "x_max", "y_min", "y_max"}, label)
    rectangle = Rectangle(**{
        key: _integer(value[key], f"{label}.{key}")
        for key in ("x_min", "x_max", "y_min", "y_max")
    })
    if rectangle.x_min > rectangle.x_max or rectangle.y_min > rectangle.y_max:
        raise ValueError(f"{label} minimum must not exceed maximum")
    for coordinate in (
        rectangle.x_min,
        rectangle.x_max,
        rectangle.y_min,
        rectangle.y_max,
    ):
        if coordinate < -half_space_size or coordinate > half_space_size:
            raise ValueError(f"{label} lies outside the configured world")
    return rectangle


def parse_disaster_scenario(
    value: Any,
    *,
    half_space_size: int,
    duration: int,
    total_agents: int,
) -> DisasterScenario:
    if not isinstance(value, dict):
        raise ValueError("scenario must be a mapping")
    _exact_keys(
        value,
        {
            "schema_version",
            "type",
            "communication_mode",
            "hazard",
            "refuges",
            "official_warning",
            "initial_eligible_rectangles",
        },
        "scenario",
    )
    if value["schema_version"] != DISASTER_SCENARIO_SCHEMA_VERSION:
        raise ValueError(
            f"scenario.schema_version must be {DISASTER_SCENARIO_SCHEMA_VERSION!r}"
        )
    if value["type"] != DISASTER_SCENARIO_TYPE:
        raise ValueError(f"scenario.type must be {DISASTER_SCENARIO_TYPE!r}")
    mode = value["communication_mode"]
    if mode not in COMMUNICATION_MODES:
        raise ValueError(
            "scenario.communication_mode must be one of: "
            + ", ".join(sorted(COMMUNICATION_MODES))
        )

    hazard = value["hazard"]
    if not isinstance(hazard, dict):
        raise ValueError("scenario.hazard must be a mapping")
    _exact_keys(hazard, {"hazard_id", "stages"}, "scenario.hazard")
    hazard_id = _identifier(hazard["hazard_id"], "scenario.hazard.hazard_id")
    if not isinstance(hazard["stages"], list) or not hazard["stages"]:
        raise ValueError("scenario.hazard.stages must be a non-empty array")
    stages = []
    previous_start = 0
    for index, row in enumerate(hazard["stages"]):
        label = f"scenario.hazard.stages[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be a mapping")
        _exact_keys(row, {"start_step", "rectangles"}, label)
        start_step = _integer(row["start_step"], f"{label}.start_step", minimum=1)
        if start_step <= previous_start or start_step > duration:
            raise ValueError(
                f"{label}.start_step must be strictly increasing and within duration"
            )
        if not isinstance(row["rectangles"], list):
            raise ValueError(f"{label}.rectangles must be an array")
        rectangles = tuple(
            _parse_rectangle(item, f"{label}.rectangles[{item_index}]", half_space_size)
            for item_index, item in enumerate(row["rectangles"])
        )
        if len(set(rectangles)) != len(rectangles):
            raise ValueError(f"{label}.rectangles contains duplicates")
        stages.append(HazardStage(start_step, rectangles))
        previous_start = start_step

    refuge_rows = value["refuges"]
    if not isinstance(refuge_rows, list) or not refuge_rows:
        raise ValueError("scenario.refuges must be a non-empty array")
    refuges = []
    refuge_ids = set()
    for index, row in enumerate(refuge_rows):
        label = f"scenario.refuges[{index}]"
        if not isinstance(row, dict):
            raise ValueError(f"{label} must be a mapping")
        _exact_keys(row, {"refuge_id", "rectangle"}, label)
        refuge_id = _identifier(row["refuge_id"], f"{label}.refuge_id")
        if refuge_id in refuge_ids:
            raise ValueError("scenario refuge IDs must be unique")
        refuge = Refuge(
            refuge_id,
            _parse_rectangle(row["rectangle"], f"{label}.rectangle", half_space_size),
        )
        if any(refuge.rectangle.overlaps(existing.rectangle) for existing in refuges):
            raise ValueError("scenario refuge rectangles must not overlap")
        refuges.append(refuge)
        refuge_ids.add(refuge_id)

    warning_row = value["official_warning"]
    if not isinstance(warning_row, dict):
        raise ValueError("scenario.official_warning must be a mapping")
    _exact_keys(
        warning_row,
        {"warning_id", "issue_step", "initial_recipient_ids"},
        "scenario.official_warning",
    )
    issue_step = _integer(
        warning_row["issue_step"],
        "scenario.official_warning.issue_step",
        minimum=1,
    )
    if issue_step > duration:
        raise ValueError("scenario.official_warning.issue_step exceeds duration")
    recipients = warning_row["initial_recipient_ids"]
    if not isinstance(recipients, list) or not recipients:
        raise ValueError(
            "scenario.official_warning.initial_recipient_ids must be a non-empty array"
        )
    recipient_ids = tuple(
        _integer(item, f"scenario.official_warning.initial_recipient_ids[{index}]", minimum=0)
        for index, item in enumerate(recipients)
    )
    if len(set(recipient_ids)) != len(recipient_ids):
        raise ValueError("official warning recipient IDs must be unique")
    if any(agent_id >= total_agents for agent_id in recipient_ids):
        raise ValueError("official warning recipient ID exceeds configured agent count")

    initial_rows = value["initial_eligible_rectangles"]
    if not isinstance(initial_rows, list) or not initial_rows:
        raise ValueError("scenario.initial_eligible_rectangles must be a non-empty array")
    initial_rectangles = tuple(
        _parse_rectangle(
            row,
            f"scenario.initial_eligible_rectangles[{index}]",
            half_space_size,
        )
        for index, row in enumerate(initial_rows)
    )

    scenario = DisasterScenario(
        communication_mode=mode,
        hazard_id=hazard_id,
        hazard_stages=tuple(stages),
        refuges=tuple(refuges),
        official_warning=OfficialWarning(
            _identifier(
                warning_row["warning_id"],
                "scenario.official_warning.warning_id",
            ),
            issue_step,
            recipient_ids,
        ),
        initial_eligible_rectangles=initial_rectangles,
    )
    active_at_issue = scenario.active_hazard_rectangles(issue_step)
    if not active_at_issue:
        raise ValueError("hazard must have at least one active rectangle at warning issue")
    if any(
        refuge.rectangle.overlaps(hazard_rectangle)
        for refuge in scenario.refuges
        for hazard_rectangle in active_at_issue
    ):
        raise ValueError("refuges must be disjoint from hazard at warning issue")
    if len(scenario.eligible_initial_cells()) < total_agents:
        raise ValueError("scenario has too few eligible initial cells for all agents")
    return scenario
