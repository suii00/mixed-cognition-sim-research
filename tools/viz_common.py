"""Shared loader for run visualisations (video / HTML report).

Only fields that exist in the run logs are exposed; nothing is inferred.
"""
import json
import os
from collections import defaultdict
from datetime import datetime

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#7f7f7f"]
DERIVED_LAYOUT_VERSION = "1.0.0"


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_run(run_dir):
    with open(os.path.join(run_dir, "run_meta.json"), "r", encoding="utf-8") as f:
        meta = json.load(f)
    cfg = meta["config"]
    blocs = cfg["blocs"]
    bloc_names = [b["name"] for b in blocs]
    colors = {b: PALETTE[i % len(PALETTE)] for i, b in enumerate(bloc_names)}

    mr = _read_jsonl(os.path.join(run_dir, "memory_reasoning.jsonl"))
    msgs = _read_jsonl(os.path.join(run_dir, "messages.jsonl"))
    parse_errors = _read_jsonl(os.path.join(run_dir, "parse_errors.jsonl"))

    agent_bloc, agent_model = {}, {}
    steps = defaultdict(dict)          # step -> aid -> record
    for r in mr:
        agent_bloc[r["agent_id"]] = r["bloc"]
        agent_model[r["agent_id"]] = r["model"]
        steps[r["step"]][r["agent_id"]] = r
    msgs_by_step = defaultdict(dict)   # step -> sender -> record
    for m in msgs:
        msgs_by_step[m["step"]][m["sender_id"]] = m

    return {
        "meta": meta,
        "run_id": meta.get("run_id", os.path.basename(os.path.normpath(run_dir))),
        "half": cfg["simulation"]["half_space_size"],
        "places": cfg.get("places", []),
        "bloc_names": bloc_names,
        "bloc_models": {b["name"]: b["model"] for b in blocs},
        "colors": colors,
        "agent_bloc": agent_bloc,
        "agent_model": agent_model,
        "agent_ids": sorted(agent_bloc),
        "step_ids": sorted(steps),
        "steps": steps,
        "msgs_by_step": msgs_by_step,
        "parse_errors": parse_errors,
    }


def per_step_bloc_counts(run):
    """Return {step: {bloc: {"agents", "sent", "move", "stay"}}} from logs."""
    out = {}
    for s in run["step_ids"]:
        row = {b: {"agents": 0, "sent": 0, "move": 0, "stay": 0}
               for b in run["bloc_names"]}
        for aid, r in run["steps"][s].items():
            b = r["bloc"]
            row[b]["agents"] += 1
            if aid in run["msgs_by_step"].get(s, {}):
                row[b]["sent"] += 1
            if r.get("action") == "stay":
                row[b]["stay"] += 1
            elif r.get("action") == "move":
                row[b]["move"] += 1
        out[s] = row
    return out


def timestamped_out_dir(run_dir, prefix="viz"):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_path = os.path.abspath(run_dir)
    run_parent = os.path.dirname(run_path)
    run_name = os.path.basename(os.path.normpath(run_path))
    return os.path.join(
        run_parent,
        "derived",
        run_name,
        f"{prefix}-v{DERIVED_LAYOUT_VERSION}-{ts}",
    )
