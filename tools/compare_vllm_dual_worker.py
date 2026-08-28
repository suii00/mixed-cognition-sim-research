#!/usr/bin/env python3
"""Compare one three-GPU worker used sequentially with two concurrent workers."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__:
    from tools.compare_vllm_scale import (
        FAILURE_COUNTERS,
        load_run,
        parse_throughput_samples,
        parse_timestamp,
        summarize,
    )
else:
    from compare_vllm_scale import (  # type: ignore[no-redef]
        FAILURE_COUNTERS,
        load_run,
        parse_throughput_samples,
        parse_timestamp,
        summarize,
    )


def _runtime_seconds(meta: dict[str, Any]) -> float:
    start = datetime.fromisoformat(meta["start_time_utc"])
    end = datetime.fromisoformat(meta["end_time_utc"])
    return (end - start).total_seconds()


def load_dual_worker(
    output_a: Path,
    output_b: Path,
    evidence_dir: Path,
) -> dict[str, Any]:
    metas = [
        json.loads((path / "run_meta.json").read_text(encoding="utf-8"))
        for path in (output_a, output_b)
    ]
    start = parse_timestamp((evidence_dir / "workload-start.txt").read_text())
    end = parse_timestamp((evidence_dir / "workload-end.txt").read_text())
    wall_seconds = int(
        (evidence_dir / "aggregate-wall-ns.txt").read_text().strip()
    ) / 1_000_000_000

    endpoints = {}
    for log_path in sorted(evidence_dir.glob("[ab]-*.stdout.log")):
        endpoint = log_path.name.removesuffix(".stdout.log")
        count_path = evidence_dir / f"{endpoint}.http-200-count.txt"
        if not count_path.exists():
            continue
        samples = parse_throughput_samples(
            log_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start,
            end,
        )
        if not samples:
            raise ValueError(f"no workload throughput samples for {endpoint}")
        endpoints[endpoint] = {
            "worker": endpoint.split("-", 1)[0].upper(),
            "http_200_count": int(count_path.read_text().strip()),
            "prompt_tokens_per_second": summarize([
                row["prompt_tokens_per_second"] for row in samples
            ]),
            "generation_tokens_per_second": summarize([
                row["generation_tokens_per_second"] for row in samples
            ]),
        }
    if len(endpoints) != 6:
        raise ValueError(f"expected six endpoint logs, found {len(endpoints)}")

    logical_calls = sum(int(meta["logical_llm_calls"]) for meta in metas)
    failure_events = sum(
        int(meta.get(key, 0)) for meta in metas for key in FAILURE_COUNTERS
    )
    return {
        "run_ids": [meta["run_id"] for meta in metas],
        "git_shas": [meta["git_sha"] for meta in metas],
        "statuses": [meta["status"] for meta in metas],
        "worker_wall_seconds": [_runtime_seconds(meta) for meta in metas],
        "wall_seconds": wall_seconds,
        "logical_llm_calls": logical_calls,
        "http_attempts": sum(int(meta["http_attempts"]) for meta in metas),
        "failure_events": failure_events,
        "failure_events_per_logical_call": failure_events / logical_calls,
        "validator_exit_codes": [
            int((evidence_dir / f"worker-{label}-validator-exit-code.txt").read_text())
            for label in ("a", "b")
        ],
        "logical_calls_per_second": logical_calls / wall_seconds,
        "sampled_aggregate_prompt_tokens_per_second": sum(
            row["prompt_tokens_per_second"]["mean"] for row in endpoints.values()
        ),
        "sampled_aggregate_generation_tokens_per_second": sum(
            row["generation_tokens_per_second"]["mean"]
            for row in endpoints.values()
        ),
        "concurrent_gpu_count": 6,
        "gpu_seconds": 6 * wall_seconds,
        "logical_calls_per_gpu_second": logical_calls / (6 * wall_seconds),
        "endpoints": endpoints,
    }


def compare_schedules(
    single_worker: dict[str, Any],
    dual_worker: dict[str, Any],
    planned_runs: int = 60,
) -> dict[str, Any]:
    sequential_wall = 2 * single_worker["wall_seconds"]
    sequential_calls = 2 * single_worker["logical_llm_calls"]
    sequential_gpu_seconds = 3 * sequential_wall
    sequential_failures = 2 * single_worker["failure_events"]
    sequential = {
        "source_run_id": single_worker["run_id"],
        "schedule": "two sequential uses of one three-GPU worker",
        "wall_seconds": sequential_wall,
        "logical_llm_calls": sequential_calls,
        "logical_calls_per_second": sequential_calls / sequential_wall,
        "sampled_aggregate_prompt_tokens_per_second": single_worker[
            "sampled_aggregate_prompt_tokens_per_second"
        ],
        "sampled_aggregate_generation_tokens_per_second": single_worker[
            "sampled_aggregate_generation_tokens_per_second"
        ],
        "failure_events": sequential_failures,
        "failure_events_per_logical_call": sequential_failures / sequential_calls,
        "concurrent_gpu_count": 3,
        "gpu_seconds": sequential_gpu_seconds,
        "logical_calls_per_gpu_second": sequential_calls / sequential_gpu_seconds,
    }
    comparison = {
        "wall_speedup": sequential_wall / dual_worker["wall_seconds"],
        "wall_reduction_seconds": sequential_wall - dual_worker["wall_seconds"],
        "wall_reduction_percent": (
            sequential_wall - dual_worker["wall_seconds"]
        ) / sequential_wall * 100,
        "dual_wall_over_single_worker_percent": (
            dual_worker["wall_seconds"] / single_worker["wall_seconds"] - 1
        ) * 100,
        "logical_throughput_ratio": (
            dual_worker["logical_calls_per_second"]
            / sequential["logical_calls_per_second"]
        ),
        "sampled_prompt_throughput_ratio": (
            dual_worker["sampled_aggregate_prompt_tokens_per_second"]
            / sequential["sampled_aggregate_prompt_tokens_per_second"]
        ),
        "sampled_generation_throughput_ratio": (
            dual_worker["sampled_aggregate_generation_tokens_per_second"]
            / sequential["sampled_aggregate_generation_tokens_per_second"]
        ),
        "gpu_seconds_ratio": dual_worker["gpu_seconds"] / sequential_gpu_seconds,
        "gpu_efficiency_ratio": (
            dual_worker["logical_calls_per_gpu_second"]
            / sequential["logical_calls_per_gpu_second"]
        ),
        "failure_rate_delta": (
            dual_worker["failure_events_per_logical_call"]
            - sequential["failure_events_per_logical_call"]
        ),
    }
    projection = {
        "label": (
            "mechanical same-workload projection, not a disaster-matrix forecast"
        ),
        "planned_runs": planned_runs,
        "one_worker_sequential_hours": (
            planned_runs * single_worker["wall_seconds"] / 3600
        ),
        "two_worker_wave_count": (planned_runs + 1) // 2,
        "two_worker_hours": (
            ((planned_runs + 1) // 2) * dual_worker["wall_seconds"] / 3600
        ),
    }
    return {
        "method": {
            "wall_time": "monotonic workload intervals retained by both runs",
            "tokens_per_second": (
                "sum of endpoint means from vLLM 10-second throughput reports "
                "inside the workload interval"
            ),
            "failure_rate": (
                "sum(generation_retries, transport_failures, syntax_parse_failures, "
                "schema_validation_failures) / logical calls"
            ),
            "limitation": (
                "token throughput is sampled; raw schema 1.1 has no exact "
                "per-response usage-event log"
            ),
        },
        "single_worker_observation": single_worker,
        "two_run_sequential_baseline": sequential,
        "dual_worker_observation": dual_worker,
        "comparison": comparison,
        "projection": projection,
    }


def format_markdown(report: dict[str, Any]) -> str:
    sequential = report["two_run_sequential_baseline"]
    dual = report["dual_worker_observation"]
    delta = report["comparison"]
    projection = report["projection"]
    lines = [
        "# Six-GPU dual-worker smoke comparison",
        "",
        "| Metric | One 3-GPU worker, two sequential runs | Two concurrent 3-GPU workers |",
        "|---|---:|---:|",
        f"| Wall time (s) | {sequential['wall_seconds']:.6f} | {dual['wall_seconds']:.6f} |",
        f"| Logical calls | {sequential['logical_llm_calls']} | {dual['logical_llm_calls']} |",
        f"| Logical calls/s | {sequential['logical_calls_per_second']:.6f} | {dual['logical_calls_per_second']:.6f} |",
        f"| Sampled prompt tokens/s | {sequential['sampled_aggregate_prompt_tokens_per_second']:.6f} | {dual['sampled_aggregate_prompt_tokens_per_second']:.6f} |",
        f"| Sampled generation tokens/s | {sequential['sampled_aggregate_generation_tokens_per_second']:.6f} | {dual['sampled_aggregate_generation_tokens_per_second']:.6f} |",
        f"| Recorded failure events/call | {sequential['failure_events_per_logical_call']:.6f} | {dual['failure_events_per_logical_call']:.6f} |",
        f"| GPU-seconds | {sequential['gpu_seconds']:.6f} | {dual['gpu_seconds']:.6f} |",
        f"| Calls/GPU-second | {sequential['logical_calls_per_gpu_second']:.6f} | {dual['logical_calls_per_gpu_second']:.6f} |",
        "",
        f"Observed schedule speedup: **{delta['wall_speedup']:.6f}x**; wall reduction: "
        f"**{delta['wall_reduction_percent']:.3f}%** "
        f"({delta['wall_reduction_seconds']:.6f} s).",
        "",
        f"The dual-worker outer wall was {delta['dual_wall_over_single_worker_percent']:.3f}% "
        "longer than one accepted three-GPU run while completing twice its calls.",
        "",
        "Token throughput is sampled from vLLM's 10-second reports, not an exact "
        "per-response token total.",
        "",
        "## Worker observations",
        "",
        f"- Run IDs: `{dual['run_ids'][0]}`, `{dual['run_ids'][1]}`",
        f"- Worker wall times from run metadata: {dual['worker_wall_seconds'][0]:.6f} s, "
        f"{dual['worker_wall_seconds'][1]:.6f} s",
        f"- Validator exit codes: {dual['validator_exit_codes']}",
        f"- Recorded failure events: {dual['failure_events']}",
        "",
        "## Same-workload projection",
        "",
        f"For {projection['planned_runs']} runs, one worker projects to "
        f"{projection['one_worker_sequential_hours']:.3f} h; "
        f"{projection['two_worker_wave_count']} dual-worker waves project to "
        f"{projection['two_worker_hours']:.3f} h.",
        "",
        "This is a mechanical projection of the engineering workload, not a "
        "forecast for the not-yet-implemented disaster matrix.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_output", type=Path)
    parser.add_argument("baseline_evidence", type=Path)
    parser.add_argument("worker_a_output", type=Path)
    parser.add_argument("worker_b_output", type=Path)
    parser.add_argument("dual_evidence", type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--markdown-out", required=True, type=Path)
    args = parser.parse_args()
    for target in (args.json_out, args.markdown_out):
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing report: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
    report = compare_schedules(
        load_run(args.baseline_output, args.baseline_evidence),
        load_dual_worker(
            args.worker_a_output,
            args.worker_b_output,
            args.dual_evidence,
        ),
    )
    args.json_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    args.markdown_out.write_text(
        format_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
