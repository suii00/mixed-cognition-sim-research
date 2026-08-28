"""Mechanical disaster metrics derived only from schema 1.2 raw records."""

from __future__ import annotations

from typing import Any, Iterable

from engine.disaster import contains_warning_identifier


DISASTER_METRIC_VERSION = "disaster-metric-v1.0.0"


def derive_disaster_metrics(
    *,
    run_meta: dict[str, Any],
    positions: Iterable[dict[str, Any]],
    phase1: Iterable[dict[str, Any]],
    warning_events: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Return per-agent outcomes; null and censoring remain explicit."""
    config = run_meta["config"]
    expected_steps = run_meta["expected_steps"]
    expected_agents = run_meta["expected_agents"]
    scenario = config["scenario"]
    warning_id = scenario["official_warning"]["warning_id"]
    issue_step = scenario["official_warning"]["issue_step"]

    post = {
        (row["step"], row["agent_id"]): row
        for row in positions
        if row.get("phase") == "post_movement"
    }
    initial = {
        row["agent_id"]: row
        for row in positions
        if row.get("phase") == "initial"
    }
    first_exposure: dict[int, int] = {}
    first_official_exposure: dict[int, int] = {}
    for event in warning_events:
        if event.get("event_type") != "warning_exposure":
            continue
        agent_id = event["recipient_id"]
        step = event["step"]
        first_exposure[agent_id] = min(first_exposure.get(agent_id, step), step)
        if event.get("source_type") == "official":
            first_official_exposure[agent_id] = min(
                first_official_exposure.get(agent_id, step), step
            )

    phase1_by_agent: dict[int, list[dict[str, Any]]] = {
        agent_id: [] for agent_id in range(expected_agents)
    }
    for row in phase1:
        parsed = row.get("parsed")
        if isinstance(parsed, dict):
            phase1_by_agent[row["agent_id"]].append(row)

    agent_rows = []
    for agent_id in range(expected_agents):
        post_rows = [post[(step, agent_id)] for step in range(1, expected_steps + 1)]
        dangerous_steps = sum(bool(row["hazardous"]) for row in post_rows)
        final_refuge = post_rows[-1]["refuge_id"]
        success = final_refuge is not None
        completion_step = None
        if success:
            completion_step = expected_steps
            for step in range(expected_steps - 1, 0, -1):
                if post[(step, agent_id)]["refuge_id"] is None:
                    break
                completion_step = step

        exposure_step = first_exposure.get(agent_id)
        official_exposure_step = first_official_exposure.get(agent_id)
        reuse_step = None
        if exposure_step is not None:
            for row in sorted(phase1_by_agent[agent_id], key=lambda item: item["step"]):
                message = row["parsed"].get("message", "")
                if row["step"] > exposure_step and contains_warning_identifier(
                    message, warning_id
                ):
                    reuse_step = row["step"]
                    break

        response_step = None
        if exposure_step is not None:
            for step in range(exposure_step + 1, expected_steps + 1):
                before = initial[agent_id] if step == 1 else post[(step - 1, agent_id)]
                after = post[(step, agent_id)]
                if (
                    after["shortest_refuge_distance"]
                    < before["shortest_refuge_distance"]
                ):
                    response_step = step
                    break

        agent_rows.append({
            "agent_id": agent_id,
            "bloc": post_rows[-1]["bloc"],
            "model": post_rows[-1]["model"],
            "hazard_onset_step": min(
                stage["start_step"] for stage in scenario["hazard"]["stages"]
            ),
            "dangerous_area_residence_steps": dangerous_steps,
            "evacuation_success": success,
            "final_refuge_id": final_refuge,
            "evacuation_completion_step": completion_step,
            "first_warning_exposure_step": exposure_step,
            "first_official_warning_exposure_step": official_exposure_step,
            "warning_reuse_step": reuse_step,
            "warning_reuse_delay_steps": (
                reuse_step - exposure_step
                if reuse_step is not None and exposure_step is not None
                else None
            ),
            "warning_reuse_right_censored": (
                exposure_step is not None and reuse_step is None
            ),
            "warning_reuse_censor_step": (
                expected_steps
                if exposure_step is not None and reuse_step is None
                else None
            ),
            "movement_response_step": response_step,
            "movement_response_delay_steps": (
                response_step - exposure_step
                if response_step is not None and exposure_step is not None
                else None
            ),
        })

    exposed = [row for row in agent_rows if row["first_warning_exposure_step"] is not None]
    return {
        "metric_version": DISASTER_METRIC_VERSION,
        "source_run_id": run_meta["run_id"],
        "source_log_schema_version": run_meta["log_schema_version"],
        "communication_mode": scenario["communication_mode"],
        "warning_id": warning_id,
        "warning_issue_step": issue_step,
        "agents": agent_rows,
        "summary": {
            "agent_count": expected_agents,
            "dangerous_area_residence_steps_total": sum(
                row["dangerous_area_residence_steps"] for row in agent_rows
            ),
            "evacuation_success_count": sum(
                row["evacuation_success"] for row in agent_rows
            ),
            "warning_exposed_count": len(exposed),
            "warning_reuse_count": sum(
                row["warning_reuse_step"] is not None for row in agent_rows
            ),
            "warning_reuse_right_censored_count": sum(
                row["warning_reuse_right_censored"] for row in agent_rows
            ),
            "movement_response_count": sum(
                row["movement_response_step"] is not None for row in agent_rows
            ),
        },
    }
