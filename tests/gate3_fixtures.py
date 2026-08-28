"""Shared temporary-only fixtures for Gate 3 regression tests."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
from unittest import mock

from engine.provenance import (
    collect_bloc_models,
    compute_config_hash,
    file_manifest,
)
from tools.eight_cell_core import (
    build_bundle,
    canonical_json_file_bytes,
    load_plan,
    paired_control_hash,
    planned_rows_bytes,
    sha256_file,
)
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "docs" / "EIGHT_CELL_MATRIX_SPEC.md"


def base_config() -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 0,
            "run_name": "gate3-base",
            "protocol_version": "gate3-test-protocol-v1",
            "metric_version": "metric-v2.0.0",
            "failure_thresholds": {
                "transport_failures": 0,
                "syntax_parse_failures": 0,
                "schema_validation_failures": 0,
            },
        },
        "blocs": [
            {
                "name": name,
                "model": "base-placeholder",
                "endpoint_id": "base-placeholder-endpoint",
                "num_agents": 4,
            }
            for name in ("alpha", "beta", "neutral")
        ],
        "agents": {
            "communication_radius": 100,
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 20,
            "message_context_size": 20,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 32,
            "timeout_s": 1,
            "max_concurrency": 3,
        },
    }


def matrix_plan(
    base_sha256: str,
    *,
    matrix_id: str = "gate3-smoke",
    replicates: list[dict] | None = None,
    execution_mode: str = "scripted_smoke",
) -> dict:
    return {
        "schema_version": "eight-cell-matrix-plan-v1.1.0",
        "matrix_id": matrix_id,
        "protocol_version": "gate3-test-protocol-v1",
        "metric_version": "metric-v2.0.0",
        "execution_mode": execution_mode,
        "base_config": {
            "path": "base_config.json",
            "sha256": base_sha256,
        },
        "model_catalog": {
            "qwen": {
                "provider": "ollama",
                "model": "qwen-placeholder",
                "endpoint_id": "qwen-endpoint",
            },
            "gemma": {
                "provider": "ollama",
                "model": "gemma-placeholder",
                "endpoint_id": "gemma-endpoint",
            },
            "llama": {
                "provider": "ollama",
                "model": "llama-placeholder",
                "endpoint_id": "llama-endpoint",
            },
        },
        "replicates": replicates or [
            {"replicate_id": "r000", "world_seed": 1001}
        ],
        "candidate_registry": {"status": "not_frozen", "sha256": None},
        "backend_freeze": {"status": "not_frozen", "evidence_id": None},
    }


def write_plan_fixture(
    root: Path,
    *,
    matrix_id: str = "gate3-smoke",
    replicates: list[dict] | None = None,
    execution_mode: str = "scripted_smoke",
):
    base_path = root / "base_config.json"
    base_path.write_text(
        json.dumps(base_config(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    base_sha = sha256_file(base_path)
    plan = matrix_plan(
        base_sha,
        matrix_id=matrix_id,
        replicates=replicates,
        execution_mode=execution_mode,
    )
    plan_path = root / "matrix_plan.json"
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    plan_sha = sha256_file(plan_path)
    spec_sha = sha256_file(SPEC_PATH)
    loaded = load_plan(plan_path, plan_sha)
    bundle = build_bundle(loaded, spec_sha, repo_root=REPO_ROOT)
    return plan_path, plan_sha, spec_sha, bundle


def gate3_patchers():
    git_info = {
        "git_sha": "3" * 40,
        "git_dirty": True,
        "git_probe_status": "available",
        "git_probe_errors": [],
    }
    gpu_info = {
        "status": "unavailable",
        "error": "test_disabled",
        "driver_version": None,
        "cuda_version": None,
        "devices": [],
    }
    dependencies = {
        "requests": "test",
        "PyYAML": "test",
        "matplotlib": "test",
        "Pillow": "test",
    }
    return (
        mock.patch("engine.provenance.collect_git_info", return_value=git_info),
        mock.patch("engine.provenance.collect_gpu_info", return_value=gpu_info),
        mock.patch(
            "engine.provenance.collect_dependency_versions",
            return_value=dependencies,
        ),
        mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("real network is forbidden in Gate 3 tests"),
        ),
    )


@contextlib.contextmanager
def patched_gate3_environment():
    patchers = gate3_patchers()
    for patcher in patchers:
        patcher.start()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict) -> None:
    path.write_bytes(canonical_json_file_bytes(value))


def make_synthetic_research_batch(
    batch_dir: Path,
    *,
    approval_present: bool = True,
    persisted_research_eligible: bool | None = None,
) -> None:
    """Transform a copied smoke batch into a no-network validator-logic fixture."""
    expected_eligible = approval_present
    persisted = (
        expected_eligible
        if persisted_research_eligible is None
        else persisted_research_eligible
    )
    mode = "reference_ollama"
    plan_path = batch_dir / "plan.json"
    rows_path = batch_dir / "planned_runs.jsonl"
    meta_path = batch_dir / "batch_meta.json"
    plan_manifest_path = batch_dir / "plan_manifest.json"
    batch_manifest_path = batch_dir / "batch_manifest.json"

    plan = _read_json(plan_path)
    plan["execution_mode"] = mode
    plan["candidate_registry"] = {"status": "frozen", "sha256": "a" * 64}
    plan["backend_freeze"] = {
        "status": "frozen",
        "evidence_id": "synthetic-validator-logic-backend",
    }
    for slot, profile in plan["model_catalog"].items():
        profile.update({
            "model_digest": f"synthetic-digest-{slot}",
            "quantization": "synthetic-quantization",
            "chat_template": f"synthetic-template-{slot}",
        })
    _write_json(plan_path, plan)

    rows = read_jsonl(rows_path)
    catalog_by_model = {
        profile["model"]: profile for profile in plan["model_catalog"].values()
    }
    configs: dict[str, dict] = {}
    for row in rows:
        config_path = batch_dir / row["config_path"]
        config = _read_json(config_path)
        config["simulation"]["execution_mode"] = mode
        config["simulation"]["research_eligible"] = persisted
        for bloc in config["blocs"]:
            profile = catalog_by_model[bloc["model"]]
            for key in ("model_digest", "quantization", "chat_template"):
                bloc[key] = profile[key]
        row["execution_mode"] = mode
        row["research_eligible"] = persisted
        row["config_sha256"] = compute_config_hash(config)
        row["paired_control_hash"] = paired_control_hash(
            config, row["prompt_sha256"]
        )
        configs[row["run_id"]] = config
        _write_json(config_path, config)
    rows_path.write_bytes(planned_rows_bytes(rows))

    meta = _read_json(meta_path)
    meta.update({
        "execution_mode": mode,
        "research_eligible": persisted,
        "candidate_registry": copy.deepcopy(plan["candidate_registry"]),
        "backend_freeze": copy.deepcopy(plan["backend_freeze"]),
        "source_git_dirty": False,
        "source_git_probe_status": "available",
        "source_git_probe_errors": [],
        "protocol_frozen": True,
        "matrix_plan_frozen": True,
        "run_start_approval_reference": (
            "synthetic-validator-logic-approval" if approval_present else None
        ),
        "plan_sha256": sha256_file(plan_path),
    })

    run_metas: dict[str, dict] = {}
    strict_results = {}
    for row in rows:
        run_dir = batch_dir / "runs" / f"output_{row['run_id']}"
        run_meta_path = run_dir / "run_meta.json"
        run_meta = _read_json(run_meta_path)
        saved_config = copy.deepcopy(configs[row["run_id"]])
        run_meta["config"] = saved_config
        run_meta["config_hash"] = compute_config_hash(saved_config)
        run_meta["models"] = collect_bloc_models(saved_config)
        run_meta["git_sha"] = meta["source_git_sha"]
        run_meta["git_dirty"] = False
        run_meta["git_probe_status"] = "available"
        run_meta["git_probe_errors"] = []
        _write_json(run_meta_path, run_meta)
        run_metas[row["run_id"]] = run_meta
        strict_results[row["run_id"]] = validate_run(run_dir, strict=True)

    plan_manifest = _read_json(plan_manifest_path)
    plan_manifest.update({
        "matrix_spec_version": meta["matrix_spec_version"],
        "matrix_spec_sha256": meta["matrix_spec_sha256"],
        "source_plan_sha256": meta["plan_sha256"],
    })
    for relative in list(plan_manifest["files"]):
        plan_manifest["files"][relative] = file_manifest(batch_dir / relative)
    _write_json(plan_manifest_path, plan_manifest)
    meta["plan_manifest_sha256"] = sha256_file(plan_manifest_path)

    batch_manifest = _read_json(batch_manifest_path)
    batch_manifest.update({
        "execution_mode": mode,
        "research_eligible": persisted,
        "plan_sha256": meta["plan_sha256"],
        "plan_manifest_sha256": meta["plan_manifest_sha256"],
    })
    rows_by_id = {row["run_id"]: row for row in rows}
    for manifest_row in batch_manifest["runs"]:
        run_id = manifest_row["run_id"]
        row = rows_by_id[run_id]
        run_dir = batch_dir / "runs" / f"output_{run_id}"
        strict = strict_results[run_id]
        manifest_row.update({
            "execution_mode": mode,
            "research_eligible": persisted,
            "config_sha256": row["config_sha256"],
            "run_meta_manifest": file_manifest(run_dir / "run_meta.json"),
            "raw_manifest": copy.deepcopy(run_metas[run_id]["raw_manifest"]),
            "strict_valid": strict.valid,
            "strict_errors": list(strict.errors),
            "strict_unverifiable": list(strict.unverifiable),
            "smoke_valid": True,
            "smoke_errors": [],
            "smoke_unverified_research_requirements": (
                []
                if approval_present
                else ["run-start approval reference is absent"]
            ),
        })
    _write_json(batch_manifest_path, batch_manifest)
    meta["batch_manifest_sha256"] = sha256_file(batch_manifest_path)
    _write_json(meta_path, meta)
