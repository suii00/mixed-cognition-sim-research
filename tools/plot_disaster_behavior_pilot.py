#!/usr/bin/env python3
"""Plot all six strictly validated frozen follow-up runs without changing raw data."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.artifact_boundaries import (  # noqa: E402
    find_immutable_artifact_ancestor, is_allowed_derived_output_root, is_within,
)
from tools.build_disaster_behavior_pilot import (  # noqa: E402
    AGENT_COUNT, CALLS_PER_RUN, DURATION, METRIC_VERSION, MODELS, OUTPUT_DIR,
    PROTOCOL_VERSION, SEEDS, canonical_bytes, load_verified_manifest,
)
from tools.scan_publication import scan_text, scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402

PLOT_VERSION = "disaster-behavior-pilot-trajectories-v1.0.0"
DEFAULT_MANIFEST = OUTPUT_DIR / "manifest.json"
MODEL_LABELS = {"qwen": "Qwen 2.5 7B", "llama": "Llama 3.1 8B", "gemma": "Gemma 2 9B"}
COLORS = ("#2769a6", "#c53a32", "#22805f", "#8860a4")
CAPTION = (
    "All six frozen runs; four agents per panel, 60 steps. Axes and colors are shared.\n"
    "S/E mark initial/final positions; open circles/diamonds mark post-movement steps 10/30.\n"
    "Agent 1 (red) is the designated initial warning recipient at step 10. Hazard shading shows the two declared stages;\n"
    "refuge outlines show fixed geometry. Lines join recorded positions; a move command can yield no displacement at a boundary.\n"
    "Trajectories describe this fixed interface and seeds; they do not establish warning adoption, model intent, or a causal model effect."
)


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def require_output_path(output_dir: Path, runs_root: Path) -> Path:
    output = output_dir.resolve(strict=False)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("figure output directory must be new")
    if not re.search(r"\d{8}T\d{6}Z", output.name):
        raise ValueError("figure output directory name must include a UTC timestamp YYYYMMDDTHHMMSSZ")
    if is_within(output, runs_root.resolve()) or not is_allowed_derived_output_root(output, repo_root=REPO_ROOT):
        raise ValueError("figure output must be outside raw runs and use a derived directory")
    if find_immutable_artifact_ancestor(output) is not None:
        raise ValueError("figure output must not be inside an immutable artifact")
    for part in (output_dir, *output_dir.parents):
        if part.is_symlink():
            raise ValueError("figure output ancestry must not contain a symbolic link")
    return output


def load_runs(runs_root: Path, manifest_path: Path = DEFAULT_MANIFEST) -> tuple[dict, list[dict]]:
    if manifest_path.name != "manifest.json" or manifest_path.is_symlink():
        raise ValueError("expected a regular frozen manifest.json")
    manifest = load_verified_manifest(manifest_path.parent)
    loaded = []
    for row in manifest["rows"]:
        run_dir = runs_root / f"output_{row['run_id']}"
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise ValueError(f"missing or nonregular run: {row['run_id']}")
        meta_bytes = (run_dir / "run_meta.json").read_bytes()
        meta = json.loads(meta_bytes)
        config = json.loads((manifest_path.parent / row["filename"]).read_bytes())
        if (meta.get("status") != "completed" or meta.get("aborted") is not False
                or meta.get("completed_steps") != DURATION or meta.get("config") != config
                or meta.get("git_dirty") is not False):
            raise ValueError(f"run is not a completed frozen clean-source condition: {row['run_id']}")
        if meta.get("logical_llm_calls") != CALLS_PER_RUN or meta.get("http_attempts") != CALLS_PER_RUN:
            raise ValueError(f"unexpected complete-run call coverage: {row['run_id']}")
        for name in ("generation_retries", "transport_failures", "syntax_parse_attempt_failures",
                     "syntax_parse_failures", "schema_validation_failures"):
            if meta.get(name) != 0:
                raise ValueError(f"nonzero or missing failure counter: {row['run_id']}")
        tracked = {"run_meta.json": meta_bytes}
        for filename in meta["raw_manifest"]["files"]:
            path = run_dir / filename
            if path.parent != run_dir or path.is_symlink() or not path.is_file():
                raise ValueError("invalid raw manifest member")
            tracked[filename] = path.read_bytes()
        report = validate_run(run_dir, strict=True)
        if not report.valid or scan_tree(run_dir):
            raise ValueError(f"strict/publication validation failed: {row['run_id']}")
        positions = [json.loads(line) for line in tracked["positions.jsonl"].splitlines()]
        by_agent = {agent_id: {} for agent_id in range(AGENT_COUNT)}
        for position in positions:
            key = position["agent_id"]
            step = position["step"]
            if key not in by_agent or step in by_agent[key]:
                raise ValueError("invalid or duplicate trajectory key")
            by_agent[key][step] = position["position"]
        if any(set(p) != set(range(DURATION + 1)) for p in by_agent.values()):
            raise ValueError("trajectory must cover every initial and post-movement step")
        references = {filename: {"sha256": digest(payload), "bytes": len(payload)}
                      for filename, payload in tracked.items()}
        loaded.append({"row": row, "meta": meta, "config": config, "run_dir": run_dir,
                       "positions": by_agent, "references": references,
                       "strict_unverifiable": report.unverifiable})
    if len({item["meta"]["git_sha"] for item in loaded}) != 1:
        raise ValueError("all six runs must have one frozen source commit")
    for seed in SEEDS:
        starts = [{agent: item["positions"][agent][0] for agent in range(AGENT_COUNT)}
                  for item in loaded if item["row"]["seed"] == seed]
        if len(starts) != 3 or any(value != starts[0] for value in starts):
            raise ValueError("same-seed model conditions have different initial worlds")
    return manifest, loaded


def render_figure(loaded: list[dict]) -> dict[str, bytes]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, Rectangle

    plt.rcParams.update({"font.size": 10, "svg.fonttype": "none", "svg.hashsalt": PLOT_VERSION})
    fig, axes = plt.subplots(2, 3, figsize=(15, 11), sharex=True, sharey=True)
    for item in loaded:
        row = item["row"]
        axis = axes[SEEDS.index(row["seed"]), MODELS.index(row["model_name"]) ]
        scenario = item["config"]["scenario"]
        for index in (1, 0):
            for rect in scenario["hazard"]["stages"][index]["rectangles"]:
                axis.add_patch(Rectangle((rect["x_min"], rect["y_min"]),
                    rect["x_max"]-rect["x_min"], rect["y_max"]-rect["y_min"],
                    color="#ce9850", alpha=0.10 if index == 1 else 0.16, linewidth=0))
        for refuge in scenario["refuges"]:
            rect = refuge["rectangle"]
            axis.add_patch(Rectangle((rect["x_min"], rect["y_min"]),
                rect["x_max"]-rect["x_min"], rect["y_max"]-rect["y_min"],
                fill=False, edgecolor="#286e63", linewidth=1.7))
        for agent_id, positions in item["positions"].items():
            points = [positions[step] for step in range(DURATION+1)]
            color = COLORS[agent_id]
            axis.plot([p[0] for p in points], [p[1] for p in points], color=color,
                      linewidth=2.0 if agent_id == 1 else 1.25, alpha=0.87)
            for step, marker in ((0, "^"), (60, "s"), (10, "o"), (30, "D")):
                x, y = positions[step]
                axis.scatter(x, y, marker=marker, s=45 if step in (10,30) else 28,
                             facecolors="none" if step in (10,30) else color,
                             edgecolors=color, linewidths=1.2, zorder=5, clip_on=False)
            for step, label in ((0, "S"), (60, "E")):
                x, y = positions[step]
                axis.annotate(f"{label}{agent_id}", (x,y),
                    xytext=(-5 if x > 18 else 4, 4+agent_id*4), textcoords="offset points",
                    color=color, fontsize=8, ha="right" if x > 18 else "left")
        axis.set(xlim=(-25,25), ylim=(-25,25), aspect="equal",
                 title=f"{MODEL_LABELS[row['model_name']]} | seed {row['seed']}")
        axis.set_xticks([-25,-10,0,10,25])
        axis.set_yticks([-25,-10,0,10,25])
        axis.grid(alpha=0.18, linewidth=0.6)
        if SEEDS.index(row["seed"]) == 1:
            axis.set_xlabel("X coordinate")
        if MODELS.index(row["model_name"]) == 0:
            axis.set_ylabel("Y coordinate")
    handles = [Line2D([0],[0], color=color, lw=2,
                      label=f"Agent {agent}" + (" (initial recipient)" if agent == 1 else ""))
               for agent,color in enumerate(COLORS)]
    handles += [Line2D([0],[0], color="#555555", lw=0, marker=marker, markerfacecolor="none", label=label)
                for marker,label in (("^","Start"),("s","End"),("o","Step 10"),("D","Step 30"))]
    handles += [Patch(facecolor="#ce9850",alpha=.28,label="Hazard stage 10"),
                Patch(facecolor="#ce9850",alpha=.10,label="Hazard stage 30"),
                Patch(facecolor="none",edgecolor="#286e63",label="Refuge")]
    fig.suptitle("Disaster follow-up: complete recorded trajectories", fontsize=17, y=.975)
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(.5,.94), ncol=6, frameon=False, fontsize=9)
    fig.text(.5,.025,CAPTION,ha="center",va="bottom",fontsize=9,linespacing=1.4)
    fig.subplots_adjust(left=.06,right=.98,bottom=.18,top=.84,wspace=.13,hspace=.24)
    files = {}
    for extension in ("png", "svg"):
        buffer = io.BytesIO()
        metadata = {"Software": PLOT_VERSION} if extension == "png" else {"Creator": PLOT_VERSION,"Date":None}
        fig.savefig(buffer,format=extension,dpi=180,metadata=metadata)
        files[f"trajectories.{extension}"] = buffer.getvalue()
    plt.close(fig)
    return files


def create_plot(runs_root: Path, output_dir: Path, manifest_path: Path = DEFAULT_MANIFEST) -> Path:
    output = require_output_path(output_dir, runs_root)
    manifest, loaded = load_runs(runs_root, manifest_path)
    files = render_figure(loaded)
    files["caption.txt"] = (CAPTION+"\n").encode()
    metadata = {
        "plot_version": PLOT_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "metric_version": METRIC_VERSION,
        "analysis_class": "descriptive-recorded-position-plot",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "batch_id": manifest["batch_id"],
        "source_manifest_sha256": digest(canonical_bytes(manifest)),
        "plot_implementation_sha256": digest(Path(__file__).read_bytes()),
        "strict_validator_sha256": digest((REPO_ROOT/"tools/validate_run.py").read_bytes()),
        "runs": [{"run_id": item["row"]["run_id"], "model_name": item["row"]["model_name"],
                  "seed": item["row"]["seed"], "source_git_sha": item["meta"]["git_sha"],
                  "config_sha256": item["row"]["sha256"], "files": item["references"],
                  "strict_unverifiable": item["strict_unverifiable"]} for item in loaded],
        "artifacts": {name:{"sha256":digest(payload),"bytes":len(payload)} for name,payload in files.items()},
        "caption": CAPTION,
    }
    files["input_manifest.json"] = canonical_bytes(metadata)
    for name,payload in files.items():
        if name.endswith((".json", ".txt", ".svg")) and scan_text(name,payload.decode()):
            raise ValueError("figure metadata/caption failed publication boundary before output creation")
    for item in loaded:
        for name,reference in item["references"].items():
            current = (item["run_dir"]/name).read_bytes()
            if len(current) != reference["bytes"] or digest(current) != reference["sha256"]:
                raise ValueError("raw source changed during plotting; no figure output created")
    if digest(manifest_path.read_bytes()) != metadata["source_manifest_sha256"]:
        raise ValueError("frozen manifest changed during plotting; no figure output created")
    require_output_path(output, runs_root)
    output.mkdir(parents=True,exist_ok=False)
    for name,payload in files.items():
        with (output/name).open("xb") as handle:
            handle.write(payload)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=REPO_ROOT/"runs")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    try:
        output = create_plot(args.runs_root,args.output_dir,args.manifest)
    except (OSError,ValueError,KeyError) as error:
        print(f"FAIL: figure generation rejected ({type(error).__name__})",file=sys.stderr)
        return 1
    print(f"PASS: six-run figure written to {output.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
