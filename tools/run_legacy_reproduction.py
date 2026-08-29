#!/usr/bin/env python3
"""Orchestrate the public historical replay matrix with run-level isolation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "configs" / "legacy_reproduction_v1" / "manifest.json"


class LegacyBatchError(RuntimeError):
    pass


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != (
        "legacy-reproduction-manifest-v1.0.0"
    ):
        raise LegacyBatchError("unsupported legacy reproduction manifest")
    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != 10:
        raise LegacyBatchError("legacy reproduction manifest must contain ten rows")
    return value


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or len(value) != 40:
        raise LegacyBatchError("source Git identity is unavailable")
    return value


def _select_rows(
    manifest: Mapping[str, Any],
    provider: str,
    source_run_ids: Sequence[str],
) -> list[dict[str, Any]]:
    requested = set(source_run_ids)
    rows = []
    for row in manifest["rows"]:
        if provider != "all" and row["provider"] != provider:
            continue
        if requested and row["source_run_id"] not in requested:
            continue
        rows.append(dict(row))
    if requested - {row["source_run_id"] for row in rows}:
        raise LegacyBatchError("one or more requested source run IDs are unavailable")
    if not rows:
        raise LegacyBatchError("no legacy reproduction rows were selected")
    return rows


def _gpu_prefix(value: str, required: int) -> str:
    pieces = value.split(",")
    if len(pieces) < required:
        raise LegacyBatchError("the selected GPU list is shorter than a row demand")
    return ",".join(pieces[:required])


def _launcher_command(
    row: Mapping[str, Any],
    args: argparse.Namespace,
    ordinal: int,
) -> list[str]:
    provider = row["provider"]
    script = (
        REPO_ROOT / "tools" / "run_public_vllm.py"
        if provider == "vllm"
        else REPO_ROOT / "tools" / "run_public_ollama.py"
    )
    command = [
        sys.executable,
        str(script),
        "--config",
        str(REPO_ROOT / row["config"]),
        "--allow-legacy-reproduction",
    ]
    if args.contract_only:
        return [*command, "--contract-only"]
    command.extend((
        "--output-root",
        str(args.output_root),
        "--evidence-root",
        str(args.evidence_root),
        "--gpu-indices",
        _gpu_prefix(args.gpu_indices, int(row["required_gpu_count"])),
        "--base-port",
        str(args.base_port + ordinal * 20),
        "--startup-timeout-s",
        str(args.startup_timeout_s),
        "--run-timeout-s",
        str(args.run_timeout_s),
        "--max-initial-memory-mib",
        str(args.max_initial_memory_mib),
    ))
    if provider == "ollama":
        if args.ollama_model_root is None:
            raise LegacyBatchError(
                "--ollama-model-root is required for Ollama runtime operations"
            )
        command.extend(("--model-root", str(args.ollama_model_root)))
    if args.preflight_only:
        command.append("--preflight-only")
    return command


def _directory_names(root: Path, prefix: str) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        path.name
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith(prefix)
    }


def _read_verification(path: Path) -> dict[str, Any]:
    value = json.loads((path / "verification.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LegacyBatchError("verification evidence is not an object")
    return value


def _cleanup_boundary_passed(value: Mapping[str, Any]) -> bool:
    model_root_check = (
        value.get("schema_version") != "public-ollama-verification-v1.0.0"
        or value.get("runtime_model_root_persisted") is False
    )
    return (
        value.get("all_process_groups_stopped") is True
        and value.get("gpu_release_verified") is True
        and value.get("publication_scan_finding_count") == 0
        and value.get("runtime_binding_values_persisted") is False
        and model_root_check
    )


def run_batch(args: argparse.Namespace) -> tuple[int, list[dict[str, Any]]]:
    manifest = _load_manifest(args.manifest.resolve())
    rows = _select_rows(manifest, args.provider, args.source_run_id)
    if not args.contract_only:
        if _git_head() != args.source_git_sha:
            raise LegacyBatchError("approved source Git SHA does not match HEAD")

    results = []
    exit_code = 0
    for ordinal, row in enumerate(rows):
        if (
            not args.contract_only
            and int(row["required_gpu_count"])
            > int(manifest["authorized_gpu_ceiling"])
        ):
            results.append({
                "evidence_directories": [],
                "exit_code": None,
                "outcome": "not_run_requires_separate_gpu_approval",
                "run_directories": [],
                "source_run_id": row["source_run_id"],
            })
            continue

        before_runs = _directory_names(args.output_root, "output_")
        before_evidence = _directory_names(args.evidence_root, "validation-")
        command = _launcher_command(row, args, ordinal)
        completed = subprocess.run(command, cwd=REPO_ROOT, check=False)
        after_runs = _directory_names(args.output_root, "output_")
        after_evidence = _directory_names(args.evidence_root, "validation-")
        new_runs = sorted(after_runs - before_runs)
        new_evidence = sorted(after_evidence - before_evidence)

        outcome = "passed" if completed.returncode == 0 else "failed"
        global_halt = False
        if args.preflight_only or args.contract_only:
            global_halt = completed.returncode != 0
        elif completed.returncode != 0:
            if len(new_runs) != 1 or len(new_evidence) != 1:
                global_halt = True
            else:
                verification = _read_verification(args.evidence_root / new_evidence[0])
                global_halt = not _cleanup_boundary_passed(verification)
                if not global_halt:
                    outcome = "retained_run_level_failure"
        results.append({
            "evidence_directories": new_evidence,
            "exit_code": completed.returncode,
            "outcome": outcome,
            "run_directories": new_runs,
            "source_run_id": row["source_run_id"],
        })
        if global_halt:
            exit_code = completed.returncode or 1
            break
        if completed.returncode != 0:
            exit_code = 1
    return exit_code, results


def write_summary(args: argparse.Namespace, results: Sequence[Mapping[str, Any]]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = args.evidence_root / f"legacy-reproduction-batch-{timestamp}"
    root.mkdir(parents=True, exist_ok=False)
    payload = {
        "approved_source_git_sha": args.source_git_sha,
        "gpu_indices": args.gpu_indices.split(","),
        "results": list(results),
        "run_timeout_s": args.run_timeout_s,
        "schema_version": "legacy-reproduction-batch-result-v1.0.0",
        "startup_timeout_s": args.startup_timeout_s,
    }
    path = root / "summary.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--provider", choices=("all", "vllm", "ollama"), default="all")
    parser.add_argument("--source-run-id", action="append", default=[])
    parser.add_argument("--source-git-sha")
    parser.add_argument("--gpu-indices", default="0,1,2,3,4,5")
    parser.add_argument("--base-port", type=int, default=18400)
    parser.add_argument("--startup-timeout-s", type=float, default=600.0)
    parser.add_argument("--run-timeout-s", type=float, default=7200.0)
    parser.add_argument("--max-initial-memory-mib", type=int, default=512)
    parser.add_argument("--ollama-model-root", type=Path)
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--evidence-root", type=Path, default=REPO_ROOT / "derived")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--contract-only", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.contract_only and not args.source_git_sha:
        print("FAIL: --source-git-sha is required for runtime operations")
        return 2
    try:
        code, results = run_batch(args)
        summary = None if args.contract_only or args.preflight_only else write_summary(args, results)
    except (OSError, TypeError, ValueError, LegacyBatchError, subprocess.SubprocessError) as error:
        print(f"FAIL: {error}")
        return 2
    for result in results:
        print(f"{result['outcome']}: {result['source_run_id']}")
    if summary is not None:
        print(f"Summary: {summary}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
