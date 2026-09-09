#!/usr/bin/env python3
"""Post-execution independent audit of the four frozen refuge-layout runs.

No engine, builder, metric core or production analyzer is imported. Production
summary files are opened only after all independent raw calculations finish.
Numerical calculations use the Python standard library; repository imports are
limited to publication/immutable-artifact boundary checks. Raw/config/source
inputs are read only; output is a new repository-derived timestamp leaf. This
additional audit does not amend the pre-execution primary measurement spec.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import stat
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.artifact_boundaries import find_immutable_artifact_ancestor  # noqa: E402
from tools.scan_publication import scan_text  # noqa: E402

BOUNDARY_IMPLEMENTATIONS = ("tools/artifact_boundaries.py", "tools/scan_publication.py")
BATCH_ID = "refuge-layout-study-v1-20260909T162300Z"
SOURCE_SHA = "a5f1421818bbbed15fa810b0906fddcc991fa885"
MANIFEST_SHA256 = "e8d76a2eaa314c7daec25c171090c7dd088c66342d20387418936b3a8ad08666"
CONDITIONS = (("edge", 60), ("inset", 60), ("inset", 120), ("edge", 120))
INPUT_NAMES = ("positions.jsonl", "memory_reasoning.jsonl", "phase1_raw.jsonl",
               "messages.jsonl", "warning_events.jsonl")
AUDIT_VERSION = "refuge-layout-study-independent-v1.0.0"


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def fingerprint(blob):
    return {"sha256": sha(blob), "bytes": len(blob), "lines": blob.count(b"\n")}


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def reject_links(path):
    for item in (path, *path.parents):
        need(not item.is_symlink(), "symbolic link rejected")
        if item.exists():
            need(not (getattr(item.lstat(), "st_file_attributes", 0)
                      & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)), "reparse point rejected")


def public_value_safe(value, depth=0):
    need(depth <= 12, "decoded public input nesting limit exceeded")
    if isinstance(value, dict):
        for key, child in value.items():
            need(not scan_text("json-key", str(key)), "unsafe public JSON key")
            if key.endswith("_body_base64") and isinstance(child, str):
                try:
                    decoded = base64.b64decode(child, validate=True).decode("utf-8")
                except (ValueError, UnicodeError) as error:
                    raise ValueError("invalid encoded response body") from error
                public_value_safe(decoded, depth + 1)
            else:
                public_value_safe(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            public_value_safe(child, depth + 1)
    elif isinstance(value, str):
        need(not scan_text("decoded-json", value), "unsafe decoded public string")
        candidate = value.lstrip()
        if candidate[:1] in ("{", "[", '"'):
            try:
                decoded = json.loads(candidate)
            except (ValueError, RecursionError):
                pass
            else:
                public_value_safe(decoded, depth + 1)


def inspect_public_payload(name, blob):
    need(not scan_text("filename", name), "unsafe public file name")
    try:
        text = blob.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("public audit input must be UTF-8 text") from error
    need(not scan_text(name, text), "unsafe public text")
    if name.endswith(".jsonl"):
        for line in text.splitlines():
            need(line.strip(), "blank public JSONL row")
            public_value_safe(json.loads(line))
    elif name.endswith(".json"):
        public_value_safe(json.loads(text))


def inspect_public_file(path):
    reject_links(path)
    need(path.is_file(), "public input must be a regular file")
    blob = path.read_bytes()
    inspect_public_payload(path.name, blob)
    return blob


def inspect_manifest_inputs(path):
    blob = inspect_public_file(path)
    need(sha(blob) == MANIFEST_SHA256, "manifest is not the pre-pinned four-run manifest")
    manifest = json.loads(blob)
    for row in manifest["rows"]:
        filename = row["filename"]
        need(Path(filename).name == filename, "config member is not a basename")
        config_blob = inspect_public_file(path.parent / filename)
        need(sha(config_blob) == row["sha256"], "public config digest mismatch")


def audit_source_state():
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, check=False)
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPO_ROOT, capture_output=True, check=False)
    need(head.returncode == status.returncode == 0, "audit source identity unavailable")
    identity = head.stdout.decode("ascii").strip()
    need(re.fullmatch(r"[0-9a-f]{40}", identity), "invalid audit source identity")
    return {"git_sha": identity, "git_dirty": bool(status.stdout.strip())}


def tree_hashes(directory):
    reject_links(directory)
    need(directory.is_dir(), "input run directory missing")
    rows = {}
    for path in sorted(directory.rglob("*")):
        reject_links(path)
        if path.is_file():
            rows[path.relative_to(directory).as_posix()] = fingerprint(inspect_public_file(path))
    return rows


def safe_output(path):
    reject_links(path)
    need(not os.path.lexists(path), "audit output collision")
    need(path.resolve().parent == (REPO_ROOT / "derived").resolve(), "audit output must be directly below repository derived")
    need(find_immutable_artifact_ancestor(path.resolve()) is None, "audit output inside immutable data")
    need(re.fullmatch(re.escape(AUDIT_VERSION) + r"_\d{8}T\d{6}(?:\d{6})?Z", path.name),
         "audit output requires a versioned UTC timestamp")
    return path.resolve()


def read_records(path, run_id):
    blob = path.read_bytes()
    need(not blob or blob.endswith(b"\n"), "raw JSONL lacks terminal newline")
    records = []
    for number, line in enumerate(blob.splitlines(keepends=True), 1):
        need(line.strip(), "blank raw JSONL row")
        row = json.loads(line)
        need(isinstance(row, dict), "non-object raw JSONL row")
        records.append({"value": row, "reference": {"run_id": run_id, "file": path.name,
            "line_number": number, "line_sha256": sha(line), "line_bytes": len(line)}})
    return records


def indexed(records, key_fields):
    output = {}
    for record in records:
        key = tuple(record["value"][field] for field in key_fields)
        need(key not in output, "duplicate raw natural key")
        output[key] = record
    return output


def inside(point, rectangle):
    x, y = point
    return rectangle["x_min"] <= x <= rectangle["x_max"] and rectangle["y_min"] <= y <= rectangle["y_max"]


def distance(point, refuges):
    x, y = point
    return min(max(r["rectangle"]["x_min"] - x, 0, x - r["rectangle"]["x_max"])
               + max(r["rectangle"]["y_min"] - y, 0, y - r["rectangle"]["y_max"]) for r in refuges)


def refuge_at(point, refuges):
    return next((r["refuge_id"] for r in refuges if inside(point, r["rectangle"])), None)


def hazard_at(step, point, stages):
    current = []
    for stage in stages:
        if stage["start_step"] <= step:
            current = stage["rectangles"]
    return any(inside(point, r) for r in current)


def exact_identifier(text, identifier):
    """Independently implement the documented ASCII token-boundary rule."""
    left_tokens = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    right_tokens = left_tokens.replace(".", "")
    alnum = right_tokens.replace("_", "").replace("-", "")
    start = 0
    while True:
        at = text.find(identifier, start)
        if at < 0:
            return False
        after = at + len(identifier)
        left_ok = at == 0 or text[at - 1] not in left_tokens
        right_ok = after == len(text) or text[after] not in right_tokens
        dotted_suffix = after + 1 < len(text) and text[after] == "." and text[after + 1] in alnum
        if left_ok and right_ok and not dotted_suffix:
            return True
        start = at + 1


def model_labels(config):
    return [{"bloc": bloc["name"], "model": bloc["model"]}
            for bloc in config["blocs"] for _ in range(bloc["num_agents"])]


def reconstructed_initial(config):
    scenario = config["scenario"]
    cells = set()
    for r in scenario["initial_eligible_rectangles"]:
        for x in range(r["x_min"], r["x_max"] + 1):
            for y in range(r["y_min"], r["y_max"] + 1):
                if refuge_at((x, y), scenario["refuges"]) is None:
                    cells.add((x, y))
    need(len(cells) == 2121 and config["places"] == [], "unexpected common eligible world")
    ordered = sorted(cells)
    random.Random(config["simulation"]["seed"]).shuffle(ordered)
    return [list(p) for p in ordered[:len(model_labels(config))]]


def calculate(config, records, horizons):
    """Derive directly from raw coordinates, actions, deliveries and exposures."""
    labels = model_labels(config)
    count, duration = len(labels), config["simulation"]["duration"]
    scenario = config["scenario"]
    refuges, stages = scenario["refuges"], scenario["hazard"]["stages"]
    warning_id = scenario["official_warning"]["warning_id"]
    pos = indexed(records["positions.jsonl"], ("step", "agent_id"))
    actions = indexed(records["memory_reasoning.jsonl"], ("step", "agent_id"))
    outputs = indexed(records["phase1_raw.jsonl"], ("step", "agent_id"))
    need(set(pos) == {(s, a) for s in range(duration + 1) for a in range(count)}, "position coverage mismatch")
    need(set(actions) == set(outputs) == {(s, a) for s in range(1, duration + 1) for a in range(count)},
         "action/Phase1 coverage mismatch")
    geometry = {}
    move_vectors = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}
    for key, record in pos.items():
        step, agent = key
        row, point = record["value"], record["value"]["position"]
        d, refuge, hazardous = distance(point, refuges), refuge_at(point, refuges), hazard_at(step, point, stages)
        need((row["bloc"], row["model"]) == (labels[agent]["bloc"], labels[agent]["model"]), "position model assignment mismatch")
        need(row["shortest_refuge_distance"] == d and row["refuge_id"] == refuge and row["hazardous"] == hazardous,
             "raw geometry differs from independent geometry")
        geometry[key] = {"distance": d, "refuge_id": refuge, "hazardous": hazardous}
        if step:
            previous = pos[step - 1, agent]["value"]["position"]
            action = actions[key]["value"]
            need(action["position"] == previous, "action not at pre-movement position")
            need(action["action"] in ("move", "stay"), "unknown action")
            if action["action"] == "move":
                need(action["direction"] in move_vectors, "unknown move direction")
                dx, dy = move_vectors[action["direction"]]
                size = config["simulation"]["half_space_size"]
                predicted = [max(-size, min(size, previous[0] + dx)), max(-size, min(size, previous[1] + dy))]
            else:
                need(action["direction"] is None, "stay direction must be null")
                predicted = previous
            need(point == predicted, "post-movement position does not follow action")
            for primary in (action, outputs[key]["value"]):
                need((primary["bloc"], primary["model"]) == (labels[agent]["bloc"], labels[agent]["model"]),
                     "primary model assignment mismatch")
    exposures = [r for r in records["warning_events.jsonl"] if r["value"]["event_type"] == "warning_exposure"]
    for record in exposures:
        row = record["value"]
        need(row["warning_id"] == warning_id and row["source_type"] in ("official", "agent_relay"), "unknown warning exposure")
        need(1 <= row["step"] <= duration and 0 <= row["recipient_id"] < count, "exposure outside run")
    exposure_by_agent = {a: [r for r in exposures if r["value"]["recipient_id"] == a] for a in range(count)}
    later_id_outputs = []
    for (step, agent), record in sorted(outputs.items()):
        message = record["value"]["parsed"]["message"]
        if exact_identifier(message, warning_id) and any(e["value"]["step"] < step for e in exposure_by_agent[agent]):
            later_id_outputs.append({"step": step, "agent_id": agent, "reference": record["reference"]})
    delivery_records = indexed(records["messages.jsonl"], ("step", "sender_id"))
    edges = []
    for (step, sender), record in sorted(delivery_records.items()):
        row = record["value"]
        need(row["message"] == outputs[step, sender]["value"]["parsed"]["message"], "delivery and generated message differ")
        receivers = row["receiver_ids"]
        need(len(receivers) == len(set(receivers)) and sender not in receivers, "duplicate/self delivery")
        for receiver in receivers:
            need(0 <= receiver < count, "receiver outside population")
            p, q = pos[step - 1, sender]["value"]["position"], pos[step - 1, receiver]["value"]["position"]
            need(sum((x - y) ** 2 for x, y in zip(p, q)) <= config["agents"]["communication_radius"] ** 2,
                 "delivery exceeds pre-movement communication radius")
            edges.append({"step": step, "sender_id": sender, "receiver_id": receiver,
                "cross_model": labels[sender]["model"] != labels[receiver]["model"],
                "exact_id": exact_identifier(row["message"], warning_id), "reference": record["reference"]})
    observed_relay = Counter((r["value"]["step"], r["value"]["sender_id"], r["value"]["recipient_id"])
                             for r in exposures if r["value"]["source_type"] == "agent_relay")
    expected_relay = Counter((e["step"], e["sender_id"], e["receiver_id"]) for e in edges if e["exact_id"])
    need(observed_relay == expected_relay, "relay exposures do not match actual exact-ID delivery edges")
    checkpoints, agent_checkpoints = [], []
    for horizon in horizons:
        agents = []
        for agent in range(count):
            positions = [pos[s, agent] for s in range(horizon + 1)]
            states = [geometry[s, agent] for s in range(horizon + 1)]
            arrival = next((s for s, g in enumerate(states) if g["refuge_id"] is not None), None)
            negative = next((s for s, g in enumerate(states) if horizon - s - g["distance"] < 0), None)
            choices = [actions[s, agent]["value"] for s in range(1, horizon + 1)]
            moves = sum(a["action"] == "move" for a in choices)
            directions = Counter(a["direction"] for a in choices if a["action"] == "move")
            exp = [r for r in exposure_by_agent[agent] if r["value"]["step"] <= horizon]
            reused = [r for r in later_id_outputs if r["agent_id"] == agent and r["step"] <= horizon]
            row = {"run_id": config["simulation"]["run_id"], "checkpoint": horizon, "agent_id": agent, **labels[agent],
                "initial_distance": states[0]["distance"], "final_distance": states[-1]["distance"],
                "minimum_distance": min(g["distance"] for g in states), "first_arrival_step": arrival,
                "arrival_right_censored": arrival is None, "arrival_censor_step": horizon if arrival is None else None,
                "first_arrival_reference": positions[arrival]["reference"] if arrival is not None else None,
                "final_refuge_id": states[-1]["refuge_id"],
                "hazard_agent_steps": sum(g["hazardous"] for g in states[1:]),
                "move": moves, "stay": horizon - moves, "directions": {d: directions[d] for d in move_vectors},
                "blocked_moves": sum(choices[s - 1]["action"] == "move" and positions[s]["value"]["position"] == positions[s - 1]["value"]["position"] for s in range(1, horizon + 1)),
                "first_negative_margin_snapshot": negative,
                "first_negative_margin_reference": positions[negative]["reference"] if negative is not None else None,
                "initial_position_reference": positions[0]["reference"], "final_position_reference": positions[-1]["reference"],
                "warning_exposure_count": len(exp), "first_exposure_step": min((r["value"]["step"] for r in exp), default=None),
                "first_reuse_step": min((r["step"] for r in reused), default=None), "later_reuse_output_count": len(reused),
                "reuse_right_censored": bool(exp and not reused), "reuse_censor_step": horizon if exp and not reused else None}
            agents.append(row)
        cut_exposures = [r["value"] for r in exposures if r["value"]["step"] <= horizon]
        cut_edges = [e for e in edges if e["step"] <= horizon]
        totals = {"checkpoint": horizon, "agent_count": count,
            "arrived_agent_count": sum(a["first_arrival_step"] is not None for a in agents),
            "final_refuge_occupancy": sum(a["final_refuge_id"] is not None for a in agents),
            "mean_initial_distance": sum(a["initial_distance"] for a in agents) / count,
            "mean_final_distance": sum(a["final_distance"] for a in agents) / count,
            **{field: sum(a[field] for a in agents) for field in ("hazard_agent_steps", "move", "stay", "blocked_moves")},
            "warning_exposed_agents": len({e["recipient_id"] for e in cut_exposures}),
            "official_exposure_events": sum(e["source_type"] == "official" for e in cut_exposures),
            "relay_exposure_events": sum(e["source_type"] == "agent_relay" for e in cut_exposures),
            "official_exposed_agents": len({e["recipient_id"] for e in cut_exposures if e["source_type"] == "official"}),
            "relay_exposed_agents": len({e["recipient_id"] for e in cut_exposures if e["source_type"] == "agent_relay"}),
            "warning_reused_agents": sum(a["first_reuse_step"] is not None for a in agents),
            "later_reuse_outputs": sum(a["later_reuse_output_count"] for a in agents),
            "message_delivery_edges": len(cut_edges), "cross_model_delivery_edges": sum(e["cross_model"] for e in cut_edges),
            "exact_id_delivery_edges": sum(e["exact_id"] for e in cut_edges),
            "cross_model_exact_id_delivery_edges": sum(e["cross_model"] and e["exact_id"] for e in cut_edges)}
        checkpoints.append(totals)
        agent_checkpoints.extend(agents)
    return {"checkpoints": checkpoints, "agent_checkpoints": agent_checkpoints,
        "initial_signature": [{"agent_id": a, "position": pos[0, a]["value"]["position"], **labels[a]} for a in range(count)],
        "later_exact_id_outputs": later_id_outputs, "edges": edges}


def prefix_difference(left, right, horizon=60):
    indices = [{name: indexed(data[name], ("step", "agent_id")) for name in INPUT_NAMES[:3]} for data in (left, right)]
    count = len([r for r in left["positions.jsonl"] if r["value"]["step"] == 0])
    candidates = []
    for step in range(1, horizon + 1):
        for agent in range(count):
            key = (step, agent)
            for phase_order, (phase, name) in enumerate((("phase1", "phase1_raw.jsonl"), ("phase3", "memory_reasoning.jsonl"), ("post_movement", "positions.jsonl"))):
                rows = [index[name][key]["value"] for index in indices]
                if phase == "phase1":
                    values = [r["parsed"] for r in rows]
                elif phase == "phase3":
                    values = [tuple(r[k] for k in ("action", "direction", "memory", "reasoning")) for r in rows]
                else:
                    values = [r["position"] for r in rows]
                if values[0] != values[1]:
                    candidates.append((step, phase_order, agent, phase))
        if candidates:
            first = min(candidates)
            return {"step": first[0], "phase": first[3], "agent_id": first[2]}
    return None


def load_runs(manifest_path, runs_root):
    reject_links(manifest_path)
    manifest_blob = manifest_path.read_bytes()
    need(sha(manifest_blob) == MANIFEST_SHA256, "manifest is not the pre-pinned four-run manifest")
    manifest = json.loads(manifest_blob)
    need(manifest["batch_id"] == BATCH_ID, "wrong batch")
    need([(r["layout"], r["duration"]) for r in manifest["rows"]] == list(CONDITIONS), "wrong condition order")
    watched = {manifest_path: fingerprint(manifest_blob), Path(__file__): fingerprint(Path(__file__).read_bytes())}
    runs, trees = [], {}
    for plan in manifest["rows"]:
        need(plan["seed"] == 6301 and plan["composition"] == "mixed", "wrong seed/composition")
        config_path = manifest_path.parent / plan["filename"]
        reject_links(config_path)
        config_blob = config_path.read_bytes()
        need(sha(config_blob) == plan["sha256"], "config digest mismatch")
        watched[config_path] = fingerprint(config_blob)
        config = json.loads(config_blob)
        directory = runs_root / ("output_" + plan["run_id"])
        tree = tree_hashes(directory)
        trees[directory] = tree
        meta = json.loads((directory / "run_meta.json").read_bytes())
        need(meta["config"] == config and meta["run_id"] == plan["run_id"], "raw/config identity mismatch")
        need(meta["git_sha"] == SOURCE_SHA and meta["git_dirty"] is False, "inference source mismatch")
        need(meta["status"] == "completed" and meta["aborted"] is False, "this audit requires all four completed received runs")
        need(meta["completed_steps"] == meta["expected_steps"] == plan["duration"] and meta["observed_agents"] == meta["expected_agents"] == 24,
             "terminal coverage mismatch")
        need(meta["logical_llm_calls"] == meta["http_attempts"] == plan["expected_logical_llm_calls"], "terminal calls mismatch")
        need(all(meta[k] == 0 for k in ("generation_retries", "transport_failures", "syntax_parse_attempt_failures", "syntax_parse_failures", "schema_validation_failures")), "failure counter nonzero")
        need(meta["raw_manifest_status"] == "available" and meta["raw_manifest"]["algorithm"] == "sha256", "raw manifest unavailable")
        for filename, expected in meta["raw_manifest"]["files"].items():
            need(filename in tree and tree[filename] == expected, "raw manifest member mismatch")
        records = {name: read_records(directory / name, plan["run_id"]) for name in INPUT_NAMES}
        observed = calculate(config, records, [t for t in (60, 120) if t <= plan["duration"]])
        need([r["position"] for r in observed["initial_signature"]] == reconstructed_initial(config), "raw starts do not match fixed world RNG")
        official = [r["value"] for r in records["warning_events.jsonl"] if r["value"]["event_type"] == "warning_exposure" and r["value"]["source_type"] == "official"]
        need(Counter((e["step"], e["recipient_id"]) for e in official) == Counter((10, a) for a in (1, 5, 9, 13, 17, 21)), "official recipient schedule mismatch")
        runs.append({"run_id": plan["run_id"], "layout": plan["layout"], "duration": plan["duration"], "seed": 6301,
            "source_sha": SOURCE_SHA, "config_sha256": plan["sha256"], "raw_file_hashes": tree,
            "observed": observed, "records": records})
    need(len(runs) == 4 and all(r["observed"]["initial_signature"] == runs[0]["observed"]["initial_signature"] for r in runs),
         "all four initial worlds/model assignments must agree")
    return runs, watched, trees


def comparisons(runs):
    by_condition = {(r["layout"], r["duration"]): r for r in runs}
    pairs, extensions, independent = [], [], []
    for duration in (60, 120):
        edge, inset = [by_condition[layout, duration] for layout in ("edge", "inset")]
        pairs.append({"seed": 6301, "duration": duration, "eligible": True,
            "initial_positions_and_assignment_equal": True, "run_ids": [edge["run_id"], inset["run_id"]],
            "comparisons": [{"checkpoint": e["checkpoint"], "edge": e, "inset": i,
                "inset_minus_edge": {key: i[key] - e[key] for key in ("arrived_agent_count", "final_refuge_occupancy", "hazard_agent_steps", "mean_final_distance")}}
                for e, i in zip(edge["observed"]["checkpoints"], inset["observed"]["checkpoints"])]})
    for layout in ("edge", "inset"):
        short, long = [by_condition[layout, duration] for duration in (60, 120)]
        difference = prefix_difference(short["records"], long["records"])
        independent.append({"seed": 6301, "layout": layout, "run_ids": [short["run_id"], long["run_id"]],
            "prefix60_equal": difference is None, "first_prefix_difference": difference})
        agents = [a for a in long["observed"]["agent_checkpoints"] if a["checkpoint"] == 120]
        extensions.append({"run_id": long["run_id"], "seed": 6301, "layout": layout,
            "checkpoint60": long["observed"]["checkpoints"][0], "checkpoint120": long["observed"]["checkpoints"][1],
            "first_arrivals_steps61_to120": sum(a["first_arrival_step"] is not None and 61 <= a["first_arrival_step"] <= 120 for a in agents)})
    return {"layout_pairs": pairs, "independent_horizon_pairs": independent, "within_120_extensions": extensions}


def compare_to_analyzer(runs, mechanical_comparisons, directory, watched):
    reject_links(directory)
    summary_path, agents_path = directory / "summary.json", directory / "agent_checkpoints.jsonl"
    for path in (summary_path, agents_path, directory / "derived_manifest.json"):
        reject_links(path)
        watched[path] = fingerprint(path.read_bytes())
    artifact_manifest = json.loads((directory / "derived_manifest.json").read_bytes())
    for path in (summary_path, agents_path):
        need(artifact_manifest["files"][path.name] == watched[path], "analyzer artifact manifest mismatch")
    summary = json.loads(summary_path.read_bytes())
    need(summary["batch_id"] == BATCH_ID and summary["metric_version"] == "refuge-layout-study-metric-v1.0.0", "wrong analyzer output identity")
    mismatches, assertions = [], 0
    def compare(expected, actual, location):
        nonlocal assertions
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                assertions += 1
                mismatches.append({"field": location, "reason": "expected object"})
                return
            for key, value in expected.items():
                compare(value, actual.get(key, {"missing": True}), location + "." + str(key))
        elif isinstance(expected, list):
            assertions += 1
            if not isinstance(actual, list) or len(expected) != len(actual):
                mismatches.append({"field": location, "reason": "list length/type mismatch"})
                return
            for i, (a, b) in enumerate(zip(expected, actual)):
                compare(a, b, location + "." + str(i))
        else:
            assertions += 1
            if expected != actual or isinstance(expected, bool) != isinstance(actual, bool):
                mismatches.append({"field": location, "independent": expected, "analyzer": actual})
    compare(4, summary.get("planned_run_count"), "planned_run_count")
    compare(4, summary.get("comparison_eligible_run_count"), "comparison_eligible_run_count")
    compare(4, len(summary["runs"]), "summary_run_row_count")
    indexed_runs = {r["run_id"]: r for r in summary["runs"]}
    compare(sorted(r["run_id"] for r in runs), sorted(indexed_runs), "run_set")
    for run in runs:
        row = indexed_runs.get(run["run_id"], {})
        compare(True, row.get("eligible"), run["run_id"] + ".eligible")
        compare(run["observed"]["checkpoints"], row.get("checkpoints"), run["run_id"] + ".checkpoints")
    compare(mechanical_comparisons, summary, "comparisons")
    actual_agents = [json.loads(line) for line in agents_path.read_bytes().splitlines()]
    actual_index = {(a["run_id"], a["checkpoint"], a["agent_id"]): a for a in actual_agents}
    expected_agents = [{**a, "layout": r["layout"], "duration": r["duration"], "seed": r["seed"]}
                       for r in runs for a in r["observed"]["agent_checkpoints"]]
    compare(len(expected_agents), len(actual_agents), "agent_checkpoint_row_count")
    compare(len(actual_agents), len(actual_index), "agent_checkpoint_unique_count")
    for agent in expected_agents:
        key = (agent["run_id"], agent["checkpoint"], agent["agent_id"])
        compare(agent, actual_index.get(key, {}), "agent_checkpoint." + ".".join(map(str, key)))
    return {"performed": True, "assertions": assertions, "mismatch_count": len(mismatches), "mismatches": mismatches,
        "runs_compared": len(runs), "run_checkpoints_compared": sum(len(r["observed"]["checkpoints"]) for r in runs),
        "agent_checkpoints_compared": len(expected_agents), "analyzer_files": {p.name: watched[p] for p in (summary_path, agents_path)},
        "scope": "all emitted run-checkpoint fields; all agent-checkpoint fields and raw references; layout pairs; independent prefix equality; within-120 extension",
        "not_independently_recomputed": ["warning surface-fact classification", "terminal evacuation-suffix v2", "runtime attestations", "cryptographic authenticity"]}


def audit(manifest_path, runs_root, output_dir, analyzer_dir=None):
    output = safe_output(output_dir)
    inspect_manifest_inputs(manifest_path)
    runs, watched, trees = load_runs(manifest_path, runs_root)
    for relative in BOUNDARY_IMPLEMENTATIONS:
        path = REPO_ROOT / relative
        watched[path] = fingerprint(inspect_public_file(path))
    source_state = audit_source_state()
    mechanical = comparisons(runs)
    # Analysis files are deliberately opened only below this point.
    if analyzer_dir:
        for name in ("summary.json", "agent_checkpoints.jsonl", "derived_manifest.json"):
            inspect_public_file(analyzer_dir / name)
    comparison = compare_to_analyzer(runs, mechanical, analyzer_dir, watched) if analyzer_dir else {"performed": False}
    report = {"audit_version": AUDIT_VERSION, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "audit_class": "post-execution-independent-verification", "audit_source_state": source_state,
        "numerical_implementation": "stdlib-only; no engine, builder, production analyzer or metric-core imports",
        "boundary_implementations": {relative: watched[REPO_ROOT / relative] for relative in BOUNDARY_IMPLEMENTATIONS},
        "batch_id": BATCH_ID, "inference_source_sha": SOURCE_SHA, "frozen_manifest_sha256": MANIFEST_SHA256,
        "independent_script_sha256": sha(Path(__file__).read_bytes()), "raw_modified": False,
        "all_four_initial_worlds_equal": True, "all_raw_starts_match_world_rng": True,
        "runs": [{k: v for k, v in r.items() if k != "records"} for r in runs],
        **mechanical, "analyzer_comparison": comparison}
    payload = json_bytes(report)
    artifact_payload = json_bytes({"audit_version": AUDIT_VERSION, "files": {"observations.json": fingerprint(payload)},
        "independent_script_sha256": sha(Path(__file__).read_bytes())})
    inspect_public_payload("observations.json", payload)
    inspect_public_payload("audit_manifest.json", artifact_payload)
    need(all(fingerprint(path.read_bytes()) == before for path, before in watched.items()), "watched input changed during audit")
    need(all(tree_hashes(path) == before for path, before in trees.items()), "raw tree changed during audit")
    for path in watched:
        inspect_public_file(path)
    need(audit_source_state() == source_state, "audit source state changed during verification")
    safe_output(output)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "observations.json").open("xb") as handle:
        handle.write(payload)
    with (output / "audit_manifest.json").open("xb") as handle:
        handle.write(artifact_payload)
    return output, comparison


def synthetic_records(config, positions, action_rows, messages, warnings, text_by_key):
    records = {name: [] for name in INPUT_NAMES}
    labels = model_labels(config)
    for step, step_positions in enumerate(positions):
        for agent, point in enumerate(step_positions):
            records["positions.jsonl"].append({"step": step, "agent_id": agent, **labels[agent], "position": point,
                "shortest_refuge_distance": distance(point, config["scenario"]["refuges"]),
                "refuge_id": refuge_at(point, config["scenario"]["refuges"]),
                "hazardous": hazard_at(step, point, config["scenario"]["hazard"]["stages"])})
            if step:
                action, direction = action_rows[step, agent]
                records["memory_reasoning.jsonl"].append({"step": step, "agent_id": agent, **labels[agent],
                    "position": positions[step - 1][agent], "action": action, "direction": direction, "memory": "", "reasoning": ""})
                records["phase1_raw.jsonl"].append({"step": step, "agent_id": agent, **labels[agent],
                    "parsed": {"message": text_by_key.get((step, agent), ""), "reasoning": ""}})
    records["messages.jsonl"], records["warning_events.jsonl"] = messages, warnings
    return {name: [{"value": row, "reference": {"run_id": "synthetic", "file": name, "line_number": i,
        "line_sha256": sha(json_bytes(row)), "line_bytes": len(json_bytes(row))}} for i, row in enumerate(rows, 1)] for name, rows in records.items()}


def self_test():
    identifier = "warning-inundation-1"
    for text, expected in ((identifier, True), (identifier + ".", True), ("(" + identifier + ")", True),
                           ("x" + identifier, False), (identifier + "-extra", False), (identifier + ".v2", False),
                           (identifier.upper(), False), ("x" + identifier + " " + identifier, True)):
        need(exact_identifier(text, identifier) is expected, "identifier boundary self-test failed")
    config = {"simulation": {"duration": 3, "run_id": "synthetic", "half_space_size": 5},
        "blocs": [{"name": "q", "model": "Q", "num_agents": 1}, {"name": "l", "model": "L", "num_agents": 1}],
        "agents": {"communication_radius": 12}, "scenario": {"refuges": [{"refuge_id": "r", "rectangle":
            {"x_min": 1, "x_max": 1, "y_min": 0, "y_max": 0}}],
            "hazard": {"stages": [{"start_step": 2, "rectangles": [{"x_min": -5, "x_max": 5, "y_min": 0, "y_max": 0}]}]},
            "official_warning": {"warning_id": identifier}}}
    positions = [[[0, 0], [-5, 0]], [[1, 0], [-5, 0]], [[1, 0], [-5, 0]], [[0, 0], [-5, 0]]]
    actions = {(s, a): ("stay", None) for s in range(1, 4) for a in range(2)}
    actions.update({(1, 0): ("move", "right"), (1, 1): ("move", "left"), (3, 0): ("move", "left")})
    texts = {(1, 0): identifier, (2, 0): identifier + ".", (2, 1): identifier, (3, 1): "x" + identifier}
    messages = [{"step": s, "sender_id": a, "receiver_ids": [b], "message": texts[s, a]}
                for s, a, b in ((1, 0, 1), (2, 0, 1), (2, 1, 0))]
    warnings = [{"event_type": "warning_exposure", "step": 1, "recipient_id": 0, "sender_id": None,
        "source_type": "official", "warning_id": identifier}]
    warnings += [{"event_type": "warning_exposure", "step": s, "recipient_id": b, "sender_id": a,
        "source_type": "agent_relay", "warning_id": identifier} for s, a, b in ((1, 0, 1), (2, 0, 1), (2, 1, 0))]
    records = synthetic_records(config, positions, actions, messages, warnings, texts)
    result = calculate(config, records, (1, 2, 3))
    first, second, third = result["checkpoints"]
    need(first["arrived_agent_count"] == first["final_refuge_occupancy"] == 1 and first["later_reuse_outputs"] == 0,
         "same-step exposure incorrectly counted as reuse")
    need(second["warning_reused_agents"] == 2 and second["later_reuse_outputs"] == 2,
         "later-step reuse self-test failed")
    need(second["official_exposure_events"] == 1 and second["relay_exposure_events"] == 3
         and second["relay_exposed_agents"] == 2 and second["cross_model_exact_id_delivery_edges"] == 3,
         "exposure/delivery self-test failed")
    need(third["arrived_agent_count"] == 1 and third["final_refuge_occupancy"] == 0
         and third["hazard_agent_steps"] == 4 and third["move"] == 3 and third["stay"] == 3
         and third["blocked_moves"] == 1 and third["mean_initial_distance"] == third["mean_final_distance"] == 3.5,
         "geometry/action/censor self-test failed")
    need(prefix_difference(records, records, 3) is None, "equal-prefix self-test failed")
    changed = copy.deepcopy(records)
    next(r for r in changed["phase1_raw.jsonl"] if r["value"]["step"] == 2 and r["value"]["agent_id"] == 1)["value"]["parsed"]["message"] = "changed"
    need(prefix_difference(records, changed, 3) == {"step": 2, "phase": "phase1", "agent_id": 1}, "first-prefix-difference self-test failed")
    need(result["agent_checkpoints"][-1]["first_arrival_step"] is None
         and result["agent_checkpoints"][-1]["arrival_censor_step"] == 3, "null/censor self-test failed")
    print("PASS: synthetic geometry, blocked move, arrival versus occupancy, exposure versus later reuse, exact-ID boundaries, delivery, censoring and prefix tests; no files written")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--analyzer-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        need(args.manifest and args.runs_root and args.output_dir, "--manifest, --runs-root and --output-dir are required")
        output = args.output_dir
        created, comparison = audit(args.manifest, args.runs_root, output, args.analyzer_dir)
        print("WROTE " + str(created / "observations.json"))
        if comparison["performed"]:
            print(f"COMPARISON assertions={comparison['assertions']} mismatches={comparison['mismatch_count']}")
            return 2 if comparison["mismatch_count"] else 0
        print("Independent raw calculations complete; analyzer output was not read")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"FAIL: independent audit rejected ({type(error).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
