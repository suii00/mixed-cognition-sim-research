"""Render an MP4 of a run: map + trails on the left, per-step bloc stats on the right.

Usage: python tools/render_video.py <run_dir> [--out DIR] [--fps 2] [--trail 6]
Requires matplotlib and ffmpeg on PATH.
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.viz_common import load_run, per_step_bloc_counts, timestamped_out_dir  # noqa: E402


def draw_frame(run, counts, step_idx, trail, out_png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.gridspec import GridSpec

    step_ids = run["step_ids"]
    step = step_ids[step_idx]
    half = run["half"]
    colors = run["colors"]
    blocs = run["bloc_names"]

    fig = plt.figure(figsize=(16, 9), dpi=80)
    gs = GridSpec(3, 2, width_ratios=[1.15, 1], height_ratios=[1, 1, 1],
                  figure=fig, wspace=0.25, hspace=0.45)
    ax_map = fig.add_subplot(gs[:, 0])
    ax_sent = fig.add_subplot(gs[0, 1])
    ax_move = fig.add_subplot(gs[1, 1])
    ax_txt = fig.add_subplot(gs[2, 1])
    ax_txt.axis("off")

    # ---- map ----
    ax_map.set_xlim(-half - 1, half + 1)
    ax_map.set_ylim(-half - 1, half + 1)
    ax_map.set_aspect("equal")
    ax_map.grid(True, alpha=0.15)
    for p in run["places"]:
        hs = p["half_size"]
        ax_map.add_patch(plt.Rectangle((p["center_x"] - hs, p["center_y"] - hs),
                                       2 * hs, 2 * hs, facecolor="#eeeeee",
                                       edgecolor="#888888", alpha=0.5, zorder=1))
        ax_map.text(p["center_x"], p["center_y"] + hs + 0.8, p["name"],
                    ha="center", fontsize=8, color="#666")

    sent_now = run["msgs_by_step"].get(step, {})
    for aid in run["agent_ids"]:
        b = run["agent_bloc"][aid]
        c = colors[b]
        xs, ys = [], []
        for s in step_ids[max(0, step_idx - trail): step_idx + 1]:
            r = run["steps"][s].get(aid)
            if r:
                xs.append(r["position"][0])
                ys.append(r["position"][1])
        if len(xs) > 1:
            ax_map.plot(xs, ys, "-", color=c, alpha=0.35, linewidth=1.5, zorder=2)
        r = run["steps"][step].get(aid)
        if not r:
            continue
        x, y = r["position"]
        stay = r.get("action") == "stay"
        ax_map.plot(x, y, "o", color=c, markersize=8 if stay else 11,
                    markeredgecolor="black", markeredgewidth=0.6,
                    markerfacecolor="white" if stay else c, zorder=4)
        if aid in sent_now:
            ax_map.add_patch(plt.Circle((x, y), 1.6, fill=False, edgecolor=c,
                                        linewidth=2, zorder=3))
        ax_map.text(x + 0.7, y + 0.7, str(aid), fontsize=7, zorder=5)

    handles = [mpatches.Patch(color=colors[b], label=f"{b} ({run['bloc_models'][b]})")
               for b in blocs]
    handles.append(plt.Line2D([], [], marker="o", color="k", markerfacecolor="white",
                              linestyle="", label="action=stay (hollow)"))
    handles.append(plt.Line2D([], [], marker="o", color="k", markerfacecolor="none",
                              markersize=12, linestyle="", label="sent message (ring)"))
    ax_map.legend(handles=handles, loc="upper left", fontsize=8, framealpha=0.9)
    ax_map.set_title(f"{run['run_id']}   step {step}/{step_ids[-1]}", fontsize=12)

    # ---- per-step stats ----
    xs = step_ids
    for ax, key, title in ((ax_sent, "sent", "agents that sent a message (per step, by bloc)"),
                           (ax_move, "move", "agents with action=move (per step, by bloc)")):
        bottom = [0] * len(xs)
        for b in blocs:
            vals = [counts[s][b][key] for s in xs]
            ax.bar(xs, vals, bottom=bottom, color=colors[b], width=0.9, label=b)
            bottom = [bt + v for bt, v in zip(bottom, vals)]
        ax.axvline(step, color="black", linewidth=1.2)
        ax.set_xlim(xs[0] - 0.5, xs[-1] + 0.5)
        ax.set_ylim(0, len(run["agent_ids"]))
        ax.set_title(title, fontsize=10)
        ax.tick_params(labelsize=8)

    # ---- text panel ----
    meta = run["meta"]
    cum = {b: {"sent": 0, "stay": 0, "agents": 0} for b in blocs}
    for s in xs[: step_idx + 1]:
        for b in blocs:
            for k in cum[b]:
                cum[b][k] += counts[s][b][k]
    lines = [f"cumulative through step {step}:"]
    for b in blocs:
        n = cum[b]["agents"] or 1
        lines.append(f"  {b:<7} sent {cum[b]['sent']:4d}/{n:<4d}  "
                     f"stay {cum[b]['stay']:4d}/{n:<4d}")
    lines.append("")
    lines.append(f"status={meta.get('status')}  steps={meta.get('completed_steps')}/"
                 f"{meta.get('expected_steps')}  agents={meta.get('observed_agents')}")
    lines.append(f"transport_failures={meta.get('transport_failures')}  "
                 f"syntax_parse_failures={meta.get('syntax_parse_failures')}  "
                 f"schema_validation_failures={meta.get('schema_validation_failures')}")
    lines.append(f"git_sha={str(meta.get('git_sha'))[:12]}  "
                 f"seed={meta['config']['simulation'].get('seed')}")
    ax_txt.text(0, 1, "\n".join(lines), family="monospace", fontsize=9.5,
                va="top", ha="left", transform=ax_txt.transAxes)

    fig.savefig(out_png)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument(
        "--out",
        default=None,
        help=(
            "output dir (default: <run-parent>/derived/<run-name>/"
            "viz-v1.0.0-<timestamp>)"
        ),
    )
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--trail", type=int, default=6)
    ap.add_argument("--keep-frames", action="store_true")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH")

    run = load_run(args.run_dir)
    if not run["step_ids"]:
        sys.exit("no steps in memory_reasoning.jsonl")
    counts = per_step_bloc_counts(run)
    out_dir = args.out or timestamped_out_dir(args.run_dir, "viz")
    os.makedirs(out_dir, exist_ok=False)
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    for i in range(len(run["step_ids"])):
        draw_frame(run, counts, i, args.trail, os.path.join(frames_dir, f"f{i:04d}.png"))
        if (i + 1) % 10 == 0:
            print(f"frames {i + 1}/{len(run['step_ids'])}")

    mp4 = os.path.join(out_dir, f"{run['run_id']}.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(args.fps),
           "-i", os.path.join(frames_dir, "f%04d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", mp4]
    subprocess.run(cmd, check=True)
    if not args.keep_frames:
        shutil.rmtree(frames_dir)
    print(f"Saved {mp4}")


if __name__ == "__main__":
    main()
