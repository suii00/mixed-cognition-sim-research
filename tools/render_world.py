"""World snapshot renderer: PNG per step + animated GIF."""
import argparse
import json
import os
from collections import defaultdict
from typing import Dict, List, Tuple


def load_data(run_dir: str):
    meta_path = os.path.join(run_dir, "run_meta.json")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    config = meta["config"]
    half_space_size = config["simulation"]["half_space_size"]
    places = config["places"]
    blocs = config["blocs"]

    bloc_names = [b["name"] for b in blocs]
    bloc_models = {b["name"]: b["model"] for b in blocs}

    agent_bloc: Dict[int, str] = {}
    positions_by_step: Dict[int, Dict[int, Tuple[int, int]]] = defaultdict(dict)

    mr_path = os.path.join(run_dir, "memory_reasoning.jsonl")
    with open(mr_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            step = record["step"]
            aid = record["agent_id"]
            agent_bloc[aid] = record["bloc"]
            pos = record["position"]
            positions_by_step[step][aid] = (pos[0], pos[1])

    return half_space_size, places, bloc_names, bloc_models, agent_bloc, positions_by_step


def render_step(half_space_size, places, bloc_names, bloc_models,
                agent_bloc, positions, step, colors):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    fig, ax = plt.subplots(figsize=(8, 8))
    s = half_space_size

    ax.set_xlim(-s - 1, s + 1)
    ax.set_ylim(-s - 1, s + 1)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.15)

    for place in places:
        cx, cy = place["center_x"], place["center_y"]
        hs = place["half_size"]
        rect = plt.Rectangle(
            (cx - hs, cy - hs), 2 * hs, 2 * hs,
            fill=True, facecolor="#eeeeee", edgecolor="#888888",
            linewidth=1.5, alpha=0.5, zorder=1,
        )
        ax.add_patch(rect)
        ax.text(cx, cy + hs + 0.8, place["name"],
                ha="center", va="bottom", fontsize=8, color="#666666")

    for aid, (x, y) in sorted(positions.items()):
        bloc = agent_bloc.get(aid, "?")
        color = colors.get(bloc, "gray")
        ax.plot(x, y, "o", color=color, markersize=10,
                markeredgecolor="black", markeredgewidth=0.5, zorder=3)
        ax.text(x + 0.6, y + 0.6, str(aid), fontsize=7, zorder=4)

    legend_handles = []
    for bloc in bloc_names:
        model = bloc_models.get(bloc, "?")
        handle = mpatches.Patch(color=colors.get(bloc, "gray"),
                                label=f"{bloc} ({model})")
        legend_handles.append(handle)
    ax.legend(handles=legend_handles, loc="upper left", fontsize=8)

    ax.set_title(f"Step {step}", fontsize=12)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    plt.tight_layout()
    return fig


def main():
    parser = argparse.ArgumentParser(
        description="Render world snapshots from simulation output"
    )
    parser.add_argument("run_dir", help="Path to simulation output directory")
    parser.add_argument("--steps", nargs="*", type=int, default=None,
                        help="Steps to render (default: first, middle, last)")
    args = parser.parse_args()

    if not os.path.isdir(args.run_dir):
        print(f"Error: {args.run_dir} is not a directory")
        return

    (half_space_size, places, bloc_names, bloc_models,
     agent_bloc, positions_by_step) = load_data(args.run_dir)

    all_steps = sorted(positions_by_step.keys())
    if not all_steps:
        print("No position data found")
        return

    palette = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
               "#8c564b", "#e377c2", "#7f7f7f"]
    colors = {}
    for i, bloc in enumerate(bloc_names):
        colors[bloc] = palette[i % len(palette)]

    if args.steps is not None:
        target_steps = [s for s in args.steps if s in positions_by_step]
    else:
        first = all_steps[0]
        last = all_steps[-1]
        mid = all_steps[len(all_steps) // 2]
        target_steps = sorted(set([first, mid, last]))

    for step in target_steps:
        fig = render_step(half_space_size, places, bloc_names, bloc_models,
                          agent_bloc, positions_by_step[step], step, colors)
        png_path = os.path.join(args.run_dir, f"world_step_{step:03d}.png")
        fig.savefig(png_path, dpi=150)
        import matplotlib.pyplot as plt
        plt.close(fig)
        print(f"Saved {png_path}")

    # GIF from all steps
    from PIL import Image
    frames = []
    for step in all_steps:
        fig = render_step(half_space_size, places, bloc_names, bloc_models,
                          agent_bloc, positions_by_step[step], step, colors)
        tmp_path = os.path.join(args.run_dir, "_tmp_frame.png")
        fig.savefig(tmp_path, dpi=100)
        import matplotlib.pyplot as plt
        plt.close(fig)
        frames.append(Image.open(tmp_path).copy())

    if os.path.exists(os.path.join(args.run_dir, "_tmp_frame.png")):
        os.remove(os.path.join(args.run_dir, "_tmp_frame.png"))

    if frames:
        gif_path = os.path.join(args.run_dir, "world.gif")
        frames[0].save(
            gif_path, save_all=True, append_images=frames[1:],
            duration=500, loop=0,
        )
        print(f"Saved {gif_path}")


if __name__ == "__main__":
    main()
