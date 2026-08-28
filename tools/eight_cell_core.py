"""Deterministic plan and artifact core for the fixed Gate 3 matrix."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from engine.config import build_effective_config, load_config
from engine.provenance import (
    canonical_json_bytes,
    compute_config_hash,
    compute_prompt_hash,
    file_manifest,
    normalize_run_id,
    sha256_bytes,
    validate_provider,
)


PLAN_SCHEMA_VERSION = "eight-cell-matrix-plan-v1.1.0"
MATRIX_SPEC_VERSION = "eight-cell-matrix-v1.1.1"
PLAN_MANIFEST_VERSION = "eight-cell-plan-manifest-v1.0.0"
BATCH_MANIFEST_VERSION = "eight-cell-batch-manifest-v1.1.0"
METRIC_VERSION = "metric-v2.0.0"
EXECUTION_MODES = frozenset({"scripted_smoke", "reference_ollama"})
CANONICAL_BLOCS = ("alpha", "beta", "neutral")
MODEL_SLOTS = ("qwen", "gemma", "llama")
MODEL_PROFILE_FIELDS = frozenset({
    "provider",
    "model",
    "endpoint_id",
    "device_slot",
    "llm_overrides",
    "model_digest",
    "quantization",
    "chat_template",
})
PLAN_FIELDS = frozenset({
    "schema_version",
    "matrix_id",
    "protocol_version",
    "metric_version",
    "execution_mode",
    "base_config",
    "model_catalog",
    "replicates",
    "candidate_registry",
    "backend_freeze",
})
CELL_DEFINITIONS: Tuple[Tuple[str, str, str], ...] = (
    ("het-full", "HET", "full"),
    ("het-within-bloc", "HET", "within_bloc_only"),
    ("qqq-full", "QQQ", "full"),
    ("qqq-within-bloc", "QQQ", "within_bloc_only"),
    ("ggg-full", "GGG", "full"),
    ("ggg-within-bloc", "GGG", "within_bloc_only"),
    ("lll-full", "LLL", "full"),
    ("lll-within-bloc", "LLL", "within_bloc_only"),
)
CELL_BY_ID = {
    cell_id: (condition, edge_policy)
    for cell_id, condition, edge_policy in CELL_DEFINITIONS
}
PLANNED_ROW_FIELDS = frozenset({
    "ordinal",
    "matrix_id",
    "replicate_id",
    "replicate_index",
    "world_seed",
    "cell_index",
    "cell_id",
    "model_condition",
    "edge_policy",
    "rotation_index",
    "execution_mode",
    "research_eligible",
    "run_id",
    "config_path",
    "config_sha256",
    "paired_control_hash",
    "initial_state_input_hash",
    "model_slots_by_bloc",
    "prompt_sha256",
})
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class PlanValidationError(ValueError):
    """The matrix plan, base config, or pinned hash is invalid."""


class DuplicateJsonKeyError(PlanValidationError):
    """A JSON object contains a duplicate member name."""


@dataclass(frozen=True)
class LoadedPlan:
    path: Path
    data: Dict[str, Any]
    sha256: str
    base_config_path: Path
    base_config: Dict[str, Any]
    base_config_sha256: str


@dataclass(frozen=True)
class MatrixBundle:
    plan: Dict[str, Any]
    plan_sha256: str
    matrix_spec_sha256: str
    prompt_sha256: str
    rows: Tuple[Dict[str, Any], ...]
    configs: Mapping[str, Dict[str, Any]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_hex64(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HEX64_RE.fullmatch(value):
        raise PlanValidationError(f"{label} must be a lowercase 64-hex SHA-256")
    return value


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json_unique(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except UnicodeDecodeError as error:
        raise PlanValidationError("plan must be UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise PlanValidationError(f"invalid JSON: {error.msg}") from error
    if not isinstance(value, dict):
        raise PlanValidationError("plan must be a JSON object")
    return value


def _validate_safe_id(value: Any, label: str) -> str:
    try:
        return normalize_run_id(value)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(f"invalid {label}: {error}") from error


def _validate_freeze_record(
    value: Any,
    *,
    label: str,
    evidence_key: str,
    frozen_validator,
) -> None:
    if not isinstance(value, dict):
        raise PlanValidationError(f"{label} must be an object")
    expected = {"status", evidence_key}
    if set(value) != expected:
        raise PlanValidationError(
            f"{label} fields must be exactly: {', '.join(sorted(expected))}"
        )
    status = value.get("status")
    evidence = value.get(evidence_key)
    if status not in {"not_frozen", "frozen"}:
        raise PlanValidationError(f"{label}.status is invalid")
    if status == "not_frozen":
        if evidence is not None:
            raise PlanValidationError(
                f"{label}.{evidence_key} must be null when not_frozen"
            )
    elif not frozen_validator(evidence):
        raise PlanValidationError(
            f"{label}.{evidence_key} is required when frozen"
        )


def _validate_model_profile(slot: str, profile: Any) -> None:
    if not isinstance(profile, dict):
        raise PlanValidationError(f"model_catalog.{slot} must be an object")
    unknown = set(profile) - MODEL_PROFILE_FIELDS
    if unknown:
        raise PlanValidationError(
            f"model_catalog.{slot} has unknown fields: "
            + ", ".join(sorted(unknown))
        )
    required = {"provider", "model", "endpoint_id"}
    missing = required - set(profile)
    if missing:
        raise PlanValidationError(
            f"model_catalog.{slot} missing: " + ", ".join(sorted(missing))
        )
    try:
        validate_provider(profile["provider"])
    except ValueError as error:
        raise PlanValidationError(f"model_catalog.{slot}: {error}") from error
    if not isinstance(profile["model"], str) or not profile["model"]:
        raise PlanValidationError(f"model_catalog.{slot}.model must be non-empty")
    if not isinstance(profile["endpoint_id"], str) or not profile["endpoint_id"]:
        raise PlanValidationError(
            f"model_catalog.{slot}.endpoint_id must be non-empty"
        )
    if "device_slot" in profile and (
        not isinstance(profile["device_slot"], str)
        or not profile["device_slot"]
    ):
        raise PlanValidationError(
            f"model_catalog.{slot}.device_slot must be non-empty when present"
        )
    if "llm_overrides" in profile and not isinstance(
        profile["llm_overrides"], dict
    ):
        raise PlanValidationError(
            f"model_catalog.{slot}.llm_overrides must be an object"
        )
    for key in ("model_digest", "quantization", "chat_template"):
        if key in profile and (
            not isinstance(profile[key], str) or not profile[key]
        ):
            raise PlanValidationError(
                f"model_catalog.{slot}.{key} must be a non-empty string"
            )


def _load_base_config(path: Path) -> Dict[str, Any]:
    try:
        effective = load_config(str(path))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as error:
        raise PlanValidationError(f"cannot load base config: {type(error).__name__}") from error
    required_top = {"simulation", "blocs", "agents", "places", "llm_defaults"}
    if not required_top.issubset(effective):
        missing = required_top - set(effective)
        raise PlanValidationError(
            "base config missing sections: " + ", ".join(sorted(missing))
        )
    simulation = effective["simulation"]
    if not isinstance(simulation, dict):
        raise PlanValidationError("base config simulation must be a mapping")
    for key in ("duration", "half_space_size", "seed", "run_name"):
        if key not in simulation:
            raise PlanValidationError(f"base config missing simulation.{key}")
    if (
        not isinstance(simulation["duration"], int)
        or isinstance(simulation["duration"], bool)
        or simulation["duration"] <= 0
    ):
        raise PlanValidationError("base config duration must be positive")
    blocs = effective["blocs"]
    if not isinstance(blocs, list) or len(blocs) != 3:
        raise PlanValidationError("base config must contain exactly three blocs")
    observed = []
    for bloc in blocs:
        if not isinstance(bloc, dict):
            raise PlanValidationError("base config bloc must be a mapping")
        observed.append((bloc.get("name"), bloc.get("num_agents")))
    expected = [(name, 4) for name in CANONICAL_BLOCS]
    if observed != expected:
        raise PlanValidationError(
            "base config blocs must be alpha/beta/neutral in order with 4 agents each"
        )
    if not isinstance(effective["places"], list):
        raise PlanValidationError("base config places must be an array")
    return effective


def validate_plan_data(data: Dict[str, Any]) -> None:
    if set(data) != PLAN_FIELDS:
        unknown = set(data) - PLAN_FIELDS
        missing = PLAN_FIELDS - set(data)
        details = []
        if unknown:
            details.append("unknown=" + ",".join(sorted(unknown)))
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        raise PlanValidationError("plan top-level fields invalid: " + "; ".join(details))
    if data["schema_version"] != PLAN_SCHEMA_VERSION:
        raise PlanValidationError("wrong plan schema_version")
    _validate_safe_id(data["matrix_id"], "matrix_id")
    protocol = data["protocol_version"]
    if not isinstance(protocol, str) or not protocol or protocol == "unversioned":
        raise PlanValidationError("protocol_version must be non-empty and versioned")
    if data["metric_version"] != METRIC_VERSION:
        raise PlanValidationError(f"metric_version must be {METRIC_VERSION}")
    execution_mode = data["execution_mode"]
    if not isinstance(execution_mode, str) or execution_mode not in EXECUTION_MODES:
        raise PlanValidationError("execution_mode is invalid")

    base = data["base_config"]
    if not isinstance(base, dict) or set(base) != {"path", "sha256"}:
        raise PlanValidationError("base_config must contain exactly path and sha256")
    relative = base["path"]
    if not isinstance(relative, str) or not relative:
        raise PlanValidationError("base_config.path must be non-empty")
    normalized = relative.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or pure.parts in {(), (".",)}:
        raise PlanValidationError("base_config.path must be a safe relative path")
    _require_hex64(base["sha256"], "base_config.sha256")

    catalog = data["model_catalog"]
    if not isinstance(catalog, dict) or tuple(sorted(catalog)) != tuple(sorted(MODEL_SLOTS)):
        raise PlanValidationError("model_catalog slots must be exactly qwen, gemma, llama")
    for slot in MODEL_SLOTS:
        _validate_model_profile(slot, catalog[slot])

    replicates = data["replicates"]
    if not isinstance(replicates, list) or not replicates:
        raise PlanValidationError("replicates must be a non-empty array")
    seen = set()
    for index, replicate in enumerate(replicates):
        if not isinstance(replicate, dict) or set(replicate) != {
            "replicate_id", "world_seed"
        }:
            raise PlanValidationError(
                f"replicates[{index}] fields must be replicate_id and world_seed"
            )
        replicate_id = _validate_safe_id(
            replicate["replicate_id"], f"replicates[{index}].replicate_id"
        )
        if replicate_id in seen:
            raise PlanValidationError(f"duplicate replicate_id: {replicate_id}")
        seen.add(replicate_id)
        seed = replicate["world_seed"]
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise PlanValidationError(
                f"replicates[{index}].world_seed must be an integer"
            )

    _validate_freeze_record(
        data["candidate_registry"],
        label="candidate_registry",
        evidence_key="sha256",
        frozen_validator=lambda value: isinstance(value, str)
        and HEX64_RE.fullmatch(value) is not None,
    )
    _validate_freeze_record(
        data["backend_freeze"],
        label="backend_freeze",
        evidence_key="evidence_id",
        frozen_validator=lambda value: isinstance(value, str) and bool(value.strip()),
    )


def load_plan(path: Path | str, expected_sha256: str) -> LoadedPlan:
    plan_path = Path(path).resolve()
    expected = _require_hex64(expected_sha256, "expected plan SHA-256")
    actual = sha256_file(plan_path)
    if actual != expected:
        raise PlanValidationError(
            f"plan SHA-256 mismatch: expected {expected}, got {actual}"
        )
    data = load_json_unique(plan_path)
    validate_plan_data(data)
    relative = Path(data["base_config"]["path"])
    base_path = (plan_path.parent / relative).resolve()
    try:
        base_path.relative_to(plan_path.parent.resolve())
    except ValueError as error:
        raise PlanValidationError("base_config.path escapes the plan directory") from error
    if not base_path.is_file() or base_path.is_symlink():
        raise PlanValidationError("base config must be a regular file")
    base_sha = sha256_file(base_path)
    if base_sha != data["base_config"]["sha256"]:
        raise PlanValidationError(
            "base config SHA-256 mismatch: "
            f"expected {data['base_config']['sha256']}, got {base_sha}"
        )
    return LoadedPlan(
        path=plan_path,
        data=copy.deepcopy(data),
        sha256=actual,
        base_config_path=base_path,
        base_config=_load_base_config(base_path),
        base_config_sha256=base_sha,
    )


def expected_model_slots(condition: str, replicate_index: int) -> Dict[str, str]:
    if condition == "HET":
        rotations = (
            {"alpha": "qwen", "beta": "gemma", "neutral": "llama"},
            {"alpha": "gemma", "beta": "llama", "neutral": "qwen"},
            {"alpha": "llama", "beta": "qwen", "neutral": "gemma"},
        )
        return dict(rotations[replicate_index % len(rotations)])
    homogeneous = {"QQQ": "qwen", "GGG": "gemma", "LLL": "llama"}
    try:
        slot = homogeneous[condition]
    except KeyError as error:
        raise PlanValidationError(f"unknown model condition: {condition}") from error
    return {bloc: slot for bloc in CANONICAL_BLOCS}


def _apply_model_profile(bloc: Dict[str, Any], profile: Dict[str, Any]) -> None:
    for field in MODEL_PROFILE_FIELDS:
        bloc.pop(field, None)
    bloc.update(copy.deepcopy(profile))


def _paired_control_payload(config: Dict[str, Any], prompt_sha256: str) -> Dict[str, Any]:
    value = copy.deepcopy(config)
    simulation = value["simulation"]
    for key in (
        "run_id",
        "run_name",
        "cell_id",
        "model_condition",
        "rotation_index",
        "execution_mode",
        "research_eligible",
    ):
        simulation.pop(key, None)
    value["agents"].pop("edge_policy", None)
    for bloc in value["blocs"]:
        for key in MODEL_PROFILE_FIELDS:
            bloc.pop(key, None)
    return {"config": value, "prompt_sha256": prompt_sha256}


def paired_control_hash(config: Dict[str, Any], prompt_sha256: str) -> str:
    return sha256_bytes(
        canonical_json_bytes(_paired_control_payload(config, prompt_sha256))
    )


def initial_state_input_hash(config: Dict[str, Any]) -> str:
    payload = {
        "seed": config["simulation"]["seed"],
        "half_space_size": config["simulation"]["half_space_size"],
        "places": config["places"],
        "blocs": [
            {"name": bloc["name"], "num_agents": bloc["num_agents"]}
            for bloc in config["blocs"]
        ],
    }
    return sha256_bytes(canonical_json_bytes(payload))


def build_bundle(
    loaded: LoadedPlan,
    matrix_spec_sha256: str,
    *,
    repo_root: Optional[Path] = None,
) -> MatrixBundle:
    spec_sha = _require_hex64(matrix_spec_sha256, "matrix spec SHA-256")
    execution_mode = loaded.data["execution_mode"]
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    prompt_sha = compute_prompt_hash(root)
    rows: List[Dict[str, Any]] = []
    configs: Dict[str, Dict[str, Any]] = {}
    seen_run_ids = set()
    for replicate_index, replicate in enumerate(loaded.data["replicates"]):
        for cell_index, (cell_id, condition, edge_policy) in enumerate(
            CELL_DEFINITIONS
        ):
            run_id = (
                f"{loaded.data['matrix_id']}-{replicate['replicate_id']}-{cell_id}"
            )
            _validate_safe_id(run_id, "generated run_id")
            if run_id in seen_run_ids:
                raise PlanValidationError(f"duplicate generated run_id: {run_id}")
            seen_run_ids.add(run_id)

            config = copy.deepcopy(loaded.base_config)
            simulation = config["simulation"]
            simulation.update({
                "matrix_id": loaded.data["matrix_id"],
                "cell_id": cell_id,
                "model_condition": condition,
                "replicate_id": replicate["replicate_id"],
                "replicate_index": replicate_index,
                "rotation_index": replicate_index % 3,
                "execution_mode": execution_mode,
                "research_eligible": False,
                "run_id": run_id,
                "run_name": run_id,
                "seed": replicate["world_seed"],
                "protocol_version": loaded.data["protocol_version"],
                "metric_version": loaded.data["metric_version"],
            })
            config["agents"]["edge_policy"] = edge_policy
            slots = expected_model_slots(condition, replicate_index)
            for bloc in config["blocs"]:
                _apply_model_profile(
                    bloc,
                    loaded.data["model_catalog"][slots[bloc["name"]]],
                )
            config = build_effective_config(config)
            config_sha = compute_config_hash(config)
            row = {
                "ordinal": len(rows),
                "matrix_id": loaded.data["matrix_id"],
                "replicate_id": replicate["replicate_id"],
                "replicate_index": replicate_index,
                "world_seed": replicate["world_seed"],
                "cell_index": cell_index,
                "cell_id": cell_id,
                "model_condition": condition,
                "edge_policy": edge_policy,
                "rotation_index": replicate_index % 3,
                "execution_mode": execution_mode,
                "research_eligible": False,
                "run_id": run_id,
                "config_path": f"configs/{run_id}.json",
                "config_sha256": config_sha,
                "paired_control_hash": paired_control_hash(config, prompt_sha),
                "initial_state_input_hash": initial_state_input_hash(config),
                "model_slots_by_bloc": slots,
                "prompt_sha256": prompt_sha,
            }
            rows.append(row)
            configs[run_id] = config

    for replicate in loaded.data["replicates"]:
        grouped = [
            row for row in rows if row["replicate_id"] == replicate["replicate_id"]
        ]
        if len({row["paired_control_hash"] for row in grouped}) != 1:
            raise PlanValidationError("paired_control_hash differs within replicate")
        if len({row["initial_state_input_hash"] for row in grouped}) != 1:
            raise PlanValidationError("initial_state_input_hash differs within replicate")

    return MatrixBundle(
        plan=copy.deepcopy(loaded.data),
        plan_sha256=loaded.sha256,
        matrix_spec_sha256=spec_sha,
        prompt_sha256=prompt_sha,
        rows=tuple(rows),
        configs=configs,
    )


def canonical_json_file_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def planned_rows_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(row) + b"\n" for row in rows)


def write_exclusive_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_static_bundle(batch_dir: Path, bundle: MatrixBundle) -> Dict[str, Any]:
    configs_dir = batch_dir / "configs"
    configs_dir.mkdir(mode=0o755, parents=False, exist_ok=False)
    plan_path = batch_dir / "plan.json"
    rows_path = batch_dir / "planned_runs.jsonl"
    write_exclusive_bytes(plan_path, canonical_json_file_bytes(bundle.plan))
    write_exclusive_bytes(rows_path, planned_rows_bytes(bundle.rows))
    for row in bundle.rows:
        write_exclusive_bytes(
            batch_dir / row["config_path"],
            canonical_json_file_bytes(bundle.configs[row["run_id"]]),
        )
    static_paths = ["plan.json", "planned_runs.jsonl"] + [
        row["config_path"] for row in bundle.rows
    ]
    manifest = {
        "schema_version": PLAN_MANIFEST_VERSION,
        "matrix_spec_version": MATRIX_SPEC_VERSION,
        "matrix_spec_sha256": bundle.matrix_spec_sha256,
        "matrix_id": bundle.plan["matrix_id"],
        "source_plan_sha256": bundle.plan_sha256,
        "base_config_sha256": bundle.plan["base_config"]["sha256"],
        "prompt_sha256": bundle.prompt_sha256,
        "files": {
            relative: file_manifest(batch_dir / relative)
            for relative in static_paths
        },
    }
    write_exclusive_bytes(
        batch_dir / "plan_manifest.json",
        canonical_json_file_bytes(manifest),
    )
    return manifest


def read_jsonl_objects(path: Path) -> List[Dict[str, Any]]:
    rows = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line:
                raise PlanValidationError(
                    f"{path.name}:{line_number} is blank"
                )
            value = json.loads(line, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise PlanValidationError(
                    f"{path.name}:{line_number} must be an object"
                )
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlanValidationError(
            f"invalid JSONL in {path.name}: {type(error).__name__}"
        ) from error
    return rows
