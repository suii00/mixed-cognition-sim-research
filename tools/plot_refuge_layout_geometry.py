#!/usr/bin/env python3
"""Plot configuration-derived geometric bounds; never read or modify run data."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import stat
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.disaster import parse_disaster_scenario  # noqa: E402
from engine.provenance import collect_git_info  # noqa: E402
from engine.world import World  # noqa: E402
from tools.artifact_boundaries import find_immutable_artifact_ancestor  # noqa: E402
from tools.build_refuge_layout_study import OUTPUT_DIR, canonical_bytes, load_verified_manifest  # noqa: E402
from tools.scan_publication import scan_text  # noqa: E402

PLOT_VERSION = "refuge-layout-study-geometry-v1.0.0"
DEFAULT_MANIFEST = OUTPUT_DIR / "manifest.json"
IMPLEMENTATIONS = (
    "tools/plot_refuge_layout_geometry.py", "tools/build_refuge_layout_study.py",
    "tools/build_disaster_matrix.py", "engine/world.py", "engine/disaster.py",
    "engine/config.py", "engine/provenance.py", "tools/artifact_boundaries.py",
    "tools/scan_publication.py", "docs/EXPERIMENT_PROTOCOL_REFUGE_LAYOUT_STUDY_V1.md",
    "docs/REFUGE_LAYOUT_STUDY_METRIC_V1_SPEC.md", "docs/DISASTER_METRIC_V2_SPEC.md",
)
CAPTION = (
    "Geometric bounds only: d is the shortest Manhattan distance to a refuge cell; one cardinal move costs one step.\n"
    "Counts use all 2,121 common eligible cells and assume shortest-path choices. Hazard does not block movement.\n"
    "Markers reconstruct the 24 initial positions from the public config and seed 6301; compare them with raw starts after execution.\n"
    "No LLM outputs or trajectories are used. This is not an observed arrival rate, understanding score, or evacuation guarantee."
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def fingerprint(payload):
    return {"sha256": digest(payload), "bytes": len(payload)}


def reject_links(paths):
    for path in paths:
        require(not path.is_symlink() and not (path.exists() and
            getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)),
            "symlink or reparse point rejected")


def destination_path(path):
    require(not os.path.lexists(path), "geometry output collision")
    require(re.fullmatch(re.escape(PLOT_VERSION) + r"_\d{8}T\d{6}(?:\d{6})?Z", path.name),
            "new versioned UTC timestamp directory required")
    reject_links((path, *path.parents))
    output = path.resolve()
    require(output.parent == (REPO_ROOT / "derived").resolve(), "output must be directly below repository derived")
    require(find_immutable_artifact_ancestor(output) is None, "output inside immutable artifact")
    return output


def geometry_from_configs(configs):
    """Pure geometry and the engine's initial-position reconstruction."""
    require(set(configs) == {"edge", "inset"}, "both fixed layouts are required")
    panels = []
    common_cells, common_initial = None, None
    for layout in ("edge", "inset"):
        config = configs[layout]
        count = sum(b["num_agents"] for b in config["blocs"])
        simulation = config["simulation"]
        require(count == 24 and simulation["seed"] == 6301 and simulation["half_space_size"] == 25,
                "geometry must use the fixed population, seed and world")
        scenario = parse_disaster_scenario(config["scenario"], half_space_size=25,
            duration=simulation["duration"], total_agents=count)
        cells = scenario.eligible_initial_cells()
        require(len(cells) == 2121, "unexpected eligible cell count")
        positions = World(25, config["places"], disaster=scenario).generate_initial_positions(
            count, random.Random(simulation["seed"]))
        labels = [bloc["name"] for bloc in config["blocs"] for _ in range(bloc["num_agents"])]
        initial = [{"agent_id": a, "model_name": labels[a], "position": list(p)}
                   for a, p in enumerate(positions)]
        if common_cells is None:
            common_cells, common_initial = cells, initial
        require(cells == common_cells and initial == common_initial, "layouts do not share the initial world")
        rows = [{"position": [x, y], "distance": scenario.shortest_refuge_distance(x, y)} for x, y in cells]
        maximum = max(row["distance"] for row in rows)
        panels.append({"layout": layout, "eligible_cells": rows, "eligible_cell_count": len(rows),
            "maximum_distance": maximum,
            "maximum_distance_cells": [row["position"] for row in rows if row["distance"] == maximum],
            "reachable_cell_counts": {str(t): sum(row["distance"] <= t for row in rows) for t in (60, 120)},
            "initial_agents": [{**row, "distance": scenario.shortest_refuge_distance(*row["position"])} for row in initial],
            "refuges": config["scenario"]["refuges"]})
    return {"plot_version": PLOT_VERSION, "seed": 6301, "agent_count": 24,
        "world_half_size": 25, "distance_basis": "minimum-Manhattan-distance-to-inclusive-refuge-cell",
        "initial_position_basis": "public-config-and-engine-world-rng-reconstruction",
        "llm_observations": False, "raw_inputs_read": False,
        "shared_color_limits": [0, max(panel["maximum_distance"] for panel in panels)],
        "panels": panels}


def load_inputs(manifest_path):
    require(manifest_path.name == "manifest.json", "fixed manifest.json required")
    reject_links((manifest_path, *manifest_path.parents))
    manifest = load_verified_manifest(manifest_path.parent)
    paths = [manifest_path, *(manifest_path.parent / row["filename"] for row in manifest["rows"]),
             *(REPO_ROOT / relative for relative in IMPLEMENTATIONS)]
    watched = {}
    for path in paths:
        reject_links((path, *path.parents))
        require(path.is_file() and not path.is_symlink(), "input must be a regular file")
        payload = path.read_bytes()
        require(not scan_text(path.name, payload.decode("utf-8")), "unsafe geometry input")
        watched[path] = payload
    configs = {row["layout"]: json.loads(watched[manifest_path.parent / row["filename"]])
               for row in manifest["rows"] if row["duration"] == 60}
    return manifest, geometry_from_configs(configs), watched


def render_figure(geometry):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, Rectangle

    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none", "svg.hashsalt": PLOT_VERSION})
    fig, axes = plt.subplots(1, 2, figsize=(14, 9), sharex=True, sharey=True)
    markers = {"qwen": "o", "llama": "^", "gemma": "s"}
    cmap = plt.get_cmap("cividis").copy()
    cmap.set_bad("#eeeeee")
    for axis, panel in zip(axes, geometry["panels"]):
        grid = [[float("nan")] * 51 for _ in range(51)]
        for row in panel["eligible_cells"]:
            x, y = row["position"]
            grid[y + 25][x + 25] = row["distance"]
        heat = axis.imshow(grid, origin="lower", extent=(-25.5, 25.5, -25.5, 25.5),
            cmap=cmap, vmin=geometry["shared_color_limits"][0], vmax=geometry["shared_color_limits"][1],
            interpolation="nearest", zorder=1)
        for refuge in panel["refuges"]:
            r = refuge["rectangle"]
            axis.add_patch(Rectangle((r["x_min"] - .5, r["y_min"] - .5),
                r["x_max"] - r["x_min"] + 1, r["y_max"] - r["y_min"] + 1,
                facecolor="white", edgecolor="#168267", linewidth=2, zorder=3))
        for row in panel["eligible_cells"]:
            if row["distance"] > 60:
                x, y = row["position"]
                axis.add_patch(Rectangle((x - .5, y - .5), 1, 1, fill=False,
                    edgecolor="#db3131", linewidth=1.7, zorder=4))
        for agent in panel["initial_agents"]:
            x, y = agent["position"]
            axis.scatter(x, y, marker=markers[agent["model_name"]], s=48,
                facecolor="white", edgecolor="#111111", linewidth=1.0, zorder=5)
            axis.annotate(str(agent["agent_id"]), (x, y), xytext=(4 if x < 22 else -4, 4),
                textcoords="offset points", ha="left" if x < 22 else "right", fontsize=7,
                color="#111111", bbox={"facecolor": "white", "alpha": .75, "edgecolor": "none", "pad": .2}, zorder=6)
        counts = panel["reachable_cell_counts"]
        axis.set(title=(f"{panel['layout'].upper()} | maximum d = {panel['maximum_distance']}\n"
            f"d <= 60: {counts['60']:,}/2,121 cells   |   d <= 120: {counts['120']:,}/2,121 cells"),
            xlim=(-25.5, 25.5), ylim=(-25.5, 25.5), aspect="equal", xlabel="X coordinate")
        axis.set_xticks([-25, -10, 0, 10, 25])
        axis.set_yticks([-25, -10, 0, 10, 25])
        axis.grid(alpha=.12, linewidth=.5)
    axes[0].set_ylabel("Y coordinate")
    handles = [Line2D([0], [0], linestyle="none", marker=markers[model], markerfacecolor="white",
        markeredgecolor="#111111", label=label) for model, label in (
            ("qwen", "Qwen initial IDs 0-7"), ("llama", "Llama initial IDs 8-15"), ("gemma", "Gemma initial IDs 16-23"))]
    handles += [Patch(facecolor="white", edgecolor="#168267", label="Refuge cells"),
                Patch(facecolor="#eeeeee", label="Outside common eligible set"),
                Patch(facecolor="none", edgecolor="#db3131", label="d > 60")]
    fig.suptitle("Geometric reachability from the shared starting cells", fontsize=17, y=.975)
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(.5, .933), ncol=3, frameon=False, fontsize=9)
    color_axis = fig.add_axes([.92, .25, .015, .52])
    fig.colorbar(heat, cax=color_axis, label="Shortest distance d (cardinal moves)")
    fig.text(.5, .035, CAPTION, ha="center", va="bottom", fontsize=9, linespacing=1.45)
    fig.subplots_adjust(left=.06, right=.9, top=.79, bottom=.23, wspace=.13)
    files = {}
    for extension in ("png", "svg"):
        buffer = io.BytesIO()
        metadata = {"Software": PLOT_VERSION} if extension == "png" else {"Creator": PLOT_VERSION, "Date": None}
        fig.savefig(buffer, format=extension, dpi=180, metadata=metadata)
        files[f"geometry.{extension}"] = buffer.getvalue()
    plt.close(fig)
    return files


def create_plot(output_dir, manifest_path=DEFAULT_MANIFEST):
    output = destination_path(output_dir)
    manifest, geometry, watched = load_inputs(manifest_path)
    source = collect_git_info(REPO_ROOT)
    require(source.get("git_sha") and source.get("git_dirty") is False, "geometry requires clean committed source")
    files = render_figure(geometry)
    files["geometry.json"] = canonical_bytes(geometry)
    files["caption.txt"] = (CAPTION + "\n").encode("utf-8")
    files["input_manifest.json"] = canonical_bytes({"plot_version": PLOT_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "batch_id": manifest["batch_id"],
        "protocol_version": manifest["protocol_version"], "source_state": source,
        "source_manifest_sha256": digest(watched[manifest_path]), "raw_inputs_read": False,
        "raw_modified": False, "llm_observations": False,
        "configs": {row["filename"]: fingerprint(watched[manifest_path.parent / row["filename"]]) for row in manifest["rows"]},
        "implementations": {relative: fingerprint(watched[REPO_ROOT / relative]) for relative in IMPLEMENTATIONS}})
    files["artifact_manifest.json"] = canonical_bytes({"plot_version": PLOT_VERSION, "algorithm": "sha256",
        "files": {name: fingerprint(payload) for name, payload in files.items()}})
    for name, payload in files.items():
        if name.endswith((".svg", ".json", ".txt")):
            require(not scan_text(name, payload.decode("utf-8")), "unsafe geometry output")
    for path in watched:
        reject_links((path, *path.parents))
    require(all(path.read_bytes() == before for path, before in watched.items()), "geometry input changed before output creation")
    require(collect_git_info(REPO_ROOT) == source, "source state changed before output creation")
    destination_path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        with (output / name).open("xb") as handle:
            handle.write(payload)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        output = create_plot(args.output_dir, args.manifest)
    except (ValueError, OSError, KeyError) as error:
        print(f"FAIL: geometry plot rejected ({type(error).__name__})", file=sys.stderr)
        return 1
    print(f"PASS: configuration-derived geometry written to {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
