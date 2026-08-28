"""Create a versioned mechanical audit of saved Phase 3 action semantics.

This audit does not reinterpret or repair raw rows.  It classifies the recorded
``action`` and ``direction`` fields against the schema contract and records the
hash of every input file used.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


AUDIT_VERSION = "phase3-semantic-audit-v1.0.0"
PUBLISHED_RUN_PREFIX = "../../runs"
ALLOWED_ACTIONS = {"move", "stay"}
ALLOWED_MOVE_DIRECTIONS = {"up", "down", "left", "right"}
ALLOWED_LEGACY_STAY_DIRECTIONS = {None, "", *ALLOWED_MOVE_DIRECTIONS}


class AuditError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def discover_runs(roots: Iterable[Path]) -> list[Path]:
    by_run_id: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            raise AuditError(f"input root is not a directory: {root}")
        for raw_path in sorted(root.rglob("output_*/memory_reasoning.jsonl")):
            run_dir = raw_path.parent
            meta_path = run_dir / "run_meta.json"
            if not meta_path.is_file():
                raise AuditError(f"run has no run_meta.json: {run_dir}")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            run_id = meta.get("run_id")
            if not isinstance(run_id, str) or run_dir.name != f"output_{run_id}":
                raise AuditError(f"run identity mismatch: {run_dir}")
            if run_id in by_run_id:
                raise AuditError(f"duplicate run_id across audit roots: {run_id}")
            by_run_id[run_id] = run_dir
    return [by_run_id[run_id] for run_id in sorted(by_run_id)]


def classify_row(row: Mapping[str, Any]) -> str | None:
    action = row.get("action")
    direction = row.get("direction")
    if action not in ALLOWED_ACTIONS:
        return "invalid_action"
    if action == "move" and direction not in ALLOWED_MOVE_DIRECTIONS:
        return "move_invalid_direction"
    if action == "stay" and direction not in ALLOWED_LEGACY_STAY_DIRECTIONS:
        return "stay_invalid_direction"
    return None


def audit_runs(runs: Sequence[Path]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counts: Counter[str] = Counter()
    affected: dict[str, set[str]] = {
        "invalid_action": set(),
        "move_invalid_direction": set(),
        "stay_invalid_direction": set(),
    }
    observed_values: dict[str, Counter[str]] = {
        category: Counter() for category in affected
    }
    violations: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    total_rows = 0

    for run_dir in runs:
        meta_path = run_dir / "run_meta.json"
        raw_path = run_dir / "memory_reasoning.jsonl"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        run_id = str(meta["run_id"])
        inputs.append(
            {
                "run_id": run_id,
                "memory_reasoning_path": (
                    f"{PUBLISHED_RUN_PREFIX}/{run_dir.name}/memory_reasoning.jsonl"
                ),
                "memory_reasoning_sha256": sha256_file(raw_path),
            }
        )
        with raw_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    raise AuditError(f"blank JSONL row: {raw_path}:{line_number}")
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as error:
                    raise AuditError(f"invalid JSONL row: {raw_path}:{line_number}") from error
                if not isinstance(row, dict):
                    raise AuditError(f"non-object JSONL row: {raw_path}:{line_number}")
                total_rows += 1
                category = classify_row(row)
                if category is None:
                    continue
                counts[category] += 1
                affected[category].add(run_id)
                observed = row.get("action") if category == "invalid_action" else row.get("direction")
                observed_values[category][json.dumps(observed, ensure_ascii=False)] += 1
                violations.append(
                    {
                        "run_id": run_id,
                        "source_file": (
                            f"{PUBLISHED_RUN_PREFIX}/{run_dir.name}/memory_reasoning.jsonl"
                        ),
                        "line_number": line_number,
                        "step": row.get("step"),
                        "agent_id": row.get("agent_id"),
                        "category": category,
                        "observed_action": row.get("action"),
                        "observed_direction": row.get("direction"),
                    }
                )

    critical_union = affected["invalid_action"] | affected["move_invalid_direction"]
    input_set_hash = hashlib.sha256(canonical_json_bytes(inputs)).hexdigest()
    summary = {
        "schema_version": "phase3-semantic-audit-summary-v1.0.0",
        "audit_version": AUDIT_VERSION,
        "classification_contract": {
            "valid_actions": sorted(ALLOWED_ACTIONS),
            "valid_move_directions": sorted(ALLOWED_MOVE_DIRECTIONS),
            "valid_legacy_stay_directions": [None, "", *sorted(ALLOWED_MOVE_DIRECTIONS)],
            "raw_rows_modified": False,
        },
        "run_count": len(runs),
        "phase3_row_count": total_rows,
        "counts": {
            "invalid_action_rows": counts["invalid_action"],
            "invalid_action_runs": len(affected["invalid_action"]),
            "move_invalid_direction_rows": counts["move_invalid_direction"],
            "move_invalid_direction_runs": len(affected["move_invalid_direction"]),
            "stay_invalid_direction_rows": counts["stay_invalid_direction"],
            "stay_invalid_direction_runs": len(affected["stay_invalid_direction"]),
            "critical_union_runs": len(critical_union),
        },
        "observed_value_counts": {
            category: dict(sorted(counter.items()))
            for category, counter in observed_values.items()
        },
        "input_set_sha256": input_set_hash,
        "inputs": inputs,
    }
    return summary, violations


def _assert_expected(summary: Mapping[str, Any], args: argparse.Namespace) -> None:
    checks = {
        "run_count": args.expect_runs,
        "phase3_row_count": args.expect_rows,
        "invalid_action_rows": args.expect_invalid_action_rows,
        "invalid_action_runs": args.expect_invalid_action_runs,
        "move_invalid_direction_rows": args.expect_move_invalid_direction_rows,
        "move_invalid_direction_runs": args.expect_move_invalid_direction_runs,
        "critical_union_runs": args.expect_critical_union_runs,
    }
    counts = summary["counts"]
    for name, expected in checks.items():
        if expected is None:
            continue
        actual = summary[name] if name in summary else counts[name]
        if actual != expected:
            raise AuditError(f"{name}: expected {expected}, got {actual}")


def write_audit(output: Path, summary: Mapping[str, Any], violations: Sequence[Mapping[str, Any]]) -> None:
    if output.exists() or output.is_symlink():
        raise AuditError(f"output directory already exists: {output}")
    output.mkdir(parents=True, exist_ok=False)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    fields = [
        "run_id", "source_file", "line_number", "step", "agent_id", "category",
        "observed_action", "observed_direction",
    ]
    with (output / "violations.csv").open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(violations)
    manifest_rows = []
    for filename in ("summary.json", "violations.csv"):
        manifest_rows.append(f"{sha256_file(output / filename)}  {filename}\n")
    (output / "SHA256SUMS").write_text("".join(manifest_rows), encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", action="append", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expect-runs", type=int)
    parser.add_argument("--expect-rows", type=int)
    parser.add_argument("--expect-invalid-action-rows", type=int)
    parser.add_argument("--expect-invalid-action-runs", type=int)
    parser.add_argument("--expect-move-invalid-direction-rows", type=int)
    parser.add_argument("--expect-move-invalid-direction-runs", type=int)
    parser.add_argument("--expect-critical-union-runs", type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runs = discover_runs(path.resolve() for path in args.runs_root)
        summary, violations = audit_runs(runs)
        _assert_expected(summary, args)
        write_audit(args.output_dir.resolve(), summary, violations)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, AuditError) as error:
        print(f"FAIL: {type(error).__name__}: {error}")
        return 1
    print(
        "PASS: phase3 semantic audit "
        f"{summary['run_count']} runs / {summary['phase3_row_count']} rows / "
        f"critical union {summary['counts']['critical_union_runs']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
