#!/usr/bin/env python3
"""Validate one schema 1.2 run and write a versioned derived metric artifact."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.provenance import atomic_write_json, file_manifest  # noqa: E402
from tools.disaster_metric_core import (  # noqa: E402
    DISASTER_METRIC_VERSION,
    derive_disaster_metrics,
)
from tools.validate_run import validate_run  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    report = validate_run(run_dir, strict=True)
    if not report.valid:
        for error in report.errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    meta_path = run_dir / "run_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("log_schema_version") != "1.2.0":
        print("FAIL: disaster metrics require log schema 1.2.0", file=sys.stderr)
        return 1
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = args.output_dir or run_dir / f"derived_{DISASTER_METRIC_VERSION}_{timestamp}"
    output.mkdir(parents=False, exist_ok=False)
    result = derive_disaster_metrics(
        run_meta=meta,
        positions=read_jsonl(run_dir / "positions.jsonl"),
        phase1=read_jsonl(run_dir / "phase1_raw.jsonl"),
        warning_events=read_jsonl(run_dir / "warning_events.jsonl"),
    )
    result["source_run_meta_manifest"] = file_manifest(meta_path)
    result["source_raw_manifest"] = meta["raw_manifest"]
    atomic_write_json(output / "disaster_metrics.json", result)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
