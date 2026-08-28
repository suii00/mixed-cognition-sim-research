#!/usr/bin/env python3
"""Compare two retained vLLM scale runs without rewriting primary evidence."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


THROUGHPUT_RE = re.compile(
    r"INFO (?P<month>\d{2})-(?P<day>\d{2}) "
    r"(?P<clock>\d{2}:\d{2}:\d{2}).*?"
    r"Avg prompt throughput: (?P<prompt>\d+(?:\.\d+)?) tokens/s, "
    r"Avg generation throughput: (?P<generation>\d+(?:\.\d+)?) tokens/s"
)
FAILURE_COUNTERS = (
    "generation_retries",
    "transport_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
)


def parse_timestamp(value: str) -> datetime:
    value = value.strip()
    value = re.sub(r"(?<=\d),(?=\d{1,9}[+-]\d{2}:\d{2}$)", ".", value)
    return datetime.fromisoformat(value)


def parse_throughput_samples(
    lines: Iterable[str],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    samples = []
    for line in lines:
        match = THROUGHPUT_RE.search(line)
        if match is None:
            continue
        timestamp = datetime(
            start.year,
            int(match.group("month")),
            int(match.group("day")),
            *map(int, match.group("clock").split(":")),
            tzinfo=start.tzinfo,
        )
        if timestamp < start or timestamp > end:
            continue
        samples.append({
            "timestamp": timestamp.isoformat(),
            "prompt_tokens_per_second": float(match.group("prompt")),
            "generation_tokens_per_second": float(match.group("generation")),
        })
    return samples


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        raise ValueError("no throughput samples fall inside the workload interval")
    return {
        "sample_count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "minimum": min(values),
        "maximum": max(values),
    }


def load_run(output_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    meta = json.loads((output_dir / "run_meta.json").read_text(encoding="utf-8"))
    start = parse_timestamp((evidence_dir / "workload-start.txt").read_text())
    end = parse_timestamp((evidence_dir / "workload-end.txt").read_text())
    wall_seconds = int(
        (evidence_dir / "workload-wall-ns.txt").read_text().strip()
    ) / 1_000_000_000

    endpoints = {}
    for log_path in sorted(evidence_dir.glob("*.stdout.log")):
        endpoint = log_path.name.removesuffix(".stdout.log")
        samples = parse_throughput_samples(
            log_path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start,
            end,
        )
        if not samples:
            continue
        count_path = evidence_dir / f"{endpoint}.http-200-count.txt"
        endpoints[endpoint] = {
            "http_200_count": int(count_path.read_text().strip()),
            "prompt_tokens_per_second": summarize([
                item["prompt_tokens_per_second"] for item in samples
            ]),
            "generation_tokens_per_second": summarize([
                item["generation_tokens_per_second"] for item in samples
            ]),
        }
    if not endpoints:
        raise ValueError(f"no endpoint throughput samples found under {evidence_dir}")

    logical_calls = int(meta["logical_llm_calls"])
    failure_events = sum(int(meta.get(key, 0)) for key in FAILURE_COUNTERS)
    aggregate_prompt = sum(
        item["prompt_tokens_per_second"]["mean"] for item in endpoints.values()
    )
    aggregate_generation = sum(
        item["generation_tokens_per_second"]["mean"]
        for item in endpoints.values()
    )
    return {
        "run_id": meta["run_id"],
        "git_sha": meta["git_sha"],
        "status": meta["status"],
        "aborted": meta["aborted"],
        "completed_steps": meta["completed_steps"],
        "logical_llm_calls": logical_calls,
        "http_attempts": meta["http_attempts"],
        "failure_counters": {key: int(meta.get(key, 0)) for key in FAILURE_COUNTERS},
        "failure_events": failure_events,
        "failure_events_per_logical_call": (
            failure_events / logical_calls if logical_calls else None
        ),
        "validator_exit_code": int(
            (evidence_dir / "validator-exit-code.txt").read_text().strip()
        ),
        "wall_seconds": wall_seconds,
        "logical_calls_per_second": logical_calls / wall_seconds,
        "sampled_aggregate_prompt_tokens_per_second": aggregate_prompt,
        "sampled_aggregate_generation_tokens_per_second": aggregate_generation,
        "endpoints": endpoints,
    }


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_wall = baseline["wall_seconds"]
    candidate_wall = candidate["wall_seconds"]
    base_generation = baseline["sampled_aggregate_generation_tokens_per_second"]
    candidate_generation = candidate[
        "sampled_aggregate_generation_tokens_per_second"
    ]
    base_prompt = baseline["sampled_aggregate_prompt_tokens_per_second"]
    candidate_prompt = candidate["sampled_aggregate_prompt_tokens_per_second"]
    return {
        "method": {
            "wall_time": "workload-wall-ns.txt monotonic interval",
            "tokens_per_second": (
                "sum of endpoint means from vLLM 10-second throughput reports "
                "whose timestamps fall inside the workload interval"
            ),
            "failure_rate": (
                "sum(generation_retries, transport_failures, "
                "syntax_parse_failures, schema_validation_failures) / logical calls"
            ),
            "limitation": (
                "server-log token throughput is sampled; exact per-response usage "
                "events were not retained by raw schema 1.1"
            ),
        },
        "baseline": baseline,
        "candidate": candidate,
        "comparison": {
            "wall_speedup": base_wall / candidate_wall,
            "wall_reduction_seconds": base_wall - candidate_wall,
            "wall_reduction_percent": (base_wall - candidate_wall) / base_wall * 100,
            "logical_throughput_ratio": (
                candidate["logical_calls_per_second"]
                / baseline["logical_calls_per_second"]
            ),
            "sampled_prompt_throughput_ratio": candidate_prompt / base_prompt,
            "sampled_generation_throughput_ratio": (
                candidate_generation / base_generation
            ),
            "failure_rate_delta": (
                candidate["failure_events_per_logical_call"]
                - baseline["failure_events_per_logical_call"]
            ),
        },
    }


def format_markdown(report: dict[str, Any]) -> str:
    base = report["baseline"]
    candidate = report["candidate"]
    delta = report["comparison"]
    rows = [
        ("GPU endpoints", len(base["endpoints"]), len(candidate["endpoints"])),
        ("Wall time (s)", base["wall_seconds"], candidate["wall_seconds"]),
        (
            "Logical calls/s",
            base["logical_calls_per_second"],
            candidate["logical_calls_per_second"],
        ),
        (
            "Sampled prompt tokens/s (aggregate)",
            base["sampled_aggregate_prompt_tokens_per_second"],
            candidate["sampled_aggregate_prompt_tokens_per_second"],
        ),
        (
            "Sampled generation tokens/s (aggregate)",
            base["sampled_aggregate_generation_tokens_per_second"],
            candidate["sampled_aggregate_generation_tokens_per_second"],
        ),
        (
            "Recorded failure events/call",
            base["failure_events_per_logical_call"],
            candidate["failure_events_per_logical_call"],
        ),
    ]

    def cell(value: Any) -> str:
        return f"{value:.6f}" if isinstance(value, float) else str(value)

    lines = [
        "# Paired vLLM scale comparison",
        "",
        f"Baseline: `{base['run_id']}`  ",
        f"Candidate: `{candidate['run_id']}`",
        "",
        "| Metric | 3 GPU | 7 GPU |",
        "|---|---:|---:|",
    ]
    lines.extend(f"| {name} | {cell(left)} | {cell(right)} |" for name, left, right in rows)
    lines.extend([
        "",
        f"Wall speedup: **{delta['wall_speedup']:.6f}x**; wall reduction: "
        f"**{delta['wall_reduction_percent']:.3f}%** "
        f"({delta['wall_reduction_seconds']:.6f} s).",
        "",
        "Token throughput is the sum of endpoint means from vLLM's sampled "
        "10-second reports within each workload interval. It is not an exact "
        "per-response token total because schema 1.1 retains no usage-event log.",
        "",
        "## Endpoint sampled means",
        "",
        "| Run | Endpoint | HTTP 200 | Prompt tokens/s | Generation tokens/s | Samples |",
        "|---|---|---:|---:|---:|---:|",
    ])
    for run_label, run in (("3 GPU", base), ("7 GPU", candidate)):
        for endpoint, item in sorted(run["endpoints"].items()):
            prompt = item["prompt_tokens_per_second"]
            generation = item["generation_tokens_per_second"]
            lines.append(
                f"| {run_label} | {endpoint} | {item['http_200_count']} | "
                f"{prompt['mean']:.6f} | {generation['mean']:.6f} | "
                f"{generation['sample_count']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline_output", type=Path)
    parser.add_argument("baseline_evidence", type=Path)
    parser.add_argument("candidate_output", type=Path)
    parser.add_argument("candidate_evidence", type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--markdown-out", required=True, type=Path)
    args = parser.parse_args()
    for target in (args.json_out, args.markdown_out):
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing report: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)

    report = compare(
        load_run(args.baseline_output, args.baseline_evidence),
        load_run(args.candidate_output, args.candidate_evidence),
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
