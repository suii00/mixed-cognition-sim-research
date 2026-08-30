#!/usr/bin/env python3
"""Read-only integrity validation for a simulation run directory.

The validator deliberately limits its PASS claim to facts represented by the
current log schema. Current raw records do not have event IDs, so natural keys
can detect some duplicates but cannot prove global event identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# Running "python tools/validate_run.py" puts tools/, not the repository root,
# first on sys.path. Use the runtime's run-ID and hash helpers directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.provenance import (  # noqa: E402
    DEPENDENCY_DISTRIBUTIONS,
    DISASTER_LOG_SCHEMA_VERSION,
    LEGACY_LOG_SCHEMA_VERSION,
    LOG_SCHEMA_VERSION,
    OBSERVABILITY_LOG_SCHEMA_VERSION,
    RAW_JSONL_FILES,
    SUPPORTED_LOG_SCHEMA_VERSIONS,
    InvalidRunIdError,
    collect_bloc_models,
    compute_config_hash,
    file_manifest,
    normalize_run_id,
    raw_jsonl_files_for_schema,
)
from engine.disaster import (  # noqa: E402
    contains_warning_identifier,
    parse_disaster_scenario,
)
from engine.config import validate_public_config_boundary  # noqa: E402
from engine.execution_contracts import (  # noqa: E402
    LEGACY_TRANSPORT_BEHAVIOR_VERSION,
)
from engine.world import World  # noqa: E402
from engine.response_contracts import (  # noqa: E402
    LEGACY_RESPONSE_CONTRACT_VERSION,
    response_schema_sha256,
    validate_parsed_response,
    validate_response_contract_version,
    vllm_transport_contract_version,
)


HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FAILURE_COUNTERS = (
    "transport_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
)
ALL_COUNTERS = (
    "logical_llm_calls",
    "http_attempts",
    "generation_retries",
    "transport_failures",
    "syntax_parse_attempt_failures",
    "syntax_parse_failures",
    "schema_validation_failures",
)


@dataclass
class ValidationReport:
    """Structured result; callers need not parse CLI output."""

    run_dir: Path
    strict: bool
    errors: List[str] = field(default_factory=list)
    unverifiable: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def cannot_verify(self, message: str) -> None:
        if message not in self.unverifiable:
            self.unverifiable.append(message)


Record = Tuple[int, Dict[str, Any]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _read_meta(path: Path, report: ValidationReport) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report.error("run_meta.json is missing")
        return None
    except (OSError, UnicodeError) as error:
        report.error(
            f"run_meta.json cannot be read as UTF-8: {type(error).__name__}"
        )
        return None

    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        report.error(
            f"run_meta.json is not valid JSON: "
            f"line {error.lineno}, column {error.colno}"
        )
        return None
    if not isinstance(value, dict):
        report.error("run_meta.json root must be a JSON object")
        return None
    return value


def _parse_utc(
    value: Any, field_name: str, report: ValidationReport
) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        report.error(f"{field_name} must be a non-empty UTC timestamp")
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        report.error(f"{field_name} is not an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        report.error(f"{field_name} must explicitly use UTC")
        return None
    return parsed


def _check_required_meta(meta: Dict[str, Any], report: ValidationReport) -> None:
    required = (
        "run_id",
        "run_name",
        "protocol_version",
        "log_schema_version",
        "metric_version",
        "start_time_utc",
        "end_time_utc",
        "status",
        "aborted",
        "abort_reason",
        "failure_step",
        "failure_phase",
        "failure_agent_id",
        "expected_steps",
        "completed_steps",
        "expected_agents",
        "observed_agents",
        *ALL_COUNTERS,
        "schema_validation_supported",
        "git_sha",
        "git_dirty",
        "config",
        "config_hash",
        "prompt_hash",
        "execution_identity_policy",
        "os",
        "platform",
        "python_version",
        "dependencies",
        "dependencies_probe_status",
        "dependencies_probe_errors",
        "gpu_info",
        "models",
        "raw_manifest",
        "raw_manifest_status",
        "raw_manifest_error",
        "failure_thresholds",
        "output_directory",
    )
    for key in required:
        if key not in meta:
            report.error(f"run_meta.json is missing required field: {key}")
    if meta.get("log_schema_version") == OBSERVABILITY_LOG_SCHEMA_VERSION:
        if meta.get("terminal_record_schema_version") != "1.0.0":
            report.error(
                "terminal_record_schema_version must be '1.0.0' under schema 2.0"
            )
        if meta.get("schema_validation_supported") is not True:
            report.error(
                "schema_validation_supported must be true under schema 2.0"
            )

    for key in (
        "run_name",
        "protocol_version",
        "log_schema_version",
        "metric_version",
    ):
        if key in meta and not _is_nonempty_string(meta[key]):
            report.error(f"{key} must be a non-empty string")

    if meta.get("log_schema_version") not in SUPPORTED_LOG_SCHEMA_VERSIONS:
        supported = ", ".join(
            repr(version)
            for version in sorted(SUPPORTED_LOG_SCHEMA_VERSIONS)
        )
        report.error(
            "unsupported log_schema_version: "
            f"expected one of {supported}, "
            f"got {meta.get('log_schema_version')!r}"
        )

    for key in ALL_COUNTERS + (
        "expected_steps",
        "completed_steps",
        "expected_agents",
        "observed_agents",
    ):
        if key in meta and not _is_nonnegative_int(meta[key]):
            report.error(f"{key} must be a non-negative integer")

    if "aborted" in meta and not isinstance(meta["aborted"], bool):
        report.error("aborted must be a boolean")
    if (
        "schema_validation_supported" in meta
        and not isinstance(meta["schema_validation_supported"], bool)
    ):
        report.error("schema_validation_supported must be a boolean")

    start = _parse_utc(
        meta.get("start_time_utc"), "start_time_utc", report
    )
    end = _parse_utc(meta.get("end_time_utc"), "end_time_utc", report)
    if start is not None and end is not None and end < start:
        report.error("end_time_utc precedes start_time_utc")

    if meta.get("status") != "completed":
        report.error(f"status is not completed: {meta.get('status')!r}")
    if meta.get("aborted") is not False:
        report.error("aborted is not false")
    if meta.get("abort_reason") is not None:
        report.error("completed run has a non-null abort_reason")
    for key in ("failure_step", "failure_phase", "failure_agent_id"):
        if meta.get(key) is not None:
            report.error(f"completed run has a non-null {key}")

    expected_steps = meta.get("expected_steps")
    completed_steps = meta.get("completed_steps")
    if _is_nonnegative_int(expected_steps) and _is_nonnegative_int(
        completed_steps
    ):
        if expected_steps != completed_steps:
            report.error(
                f"completed_steps mismatch: expected {expected_steps}, "
                f"got {completed_steps}"
            )
        if expected_steps <= 0:
            report.error("expected_steps must be positive")

    expected_agents = meta.get("expected_agents")
    observed_agents = meta.get("observed_agents")
    if _is_nonnegative_int(expected_agents) and _is_nonnegative_int(
        observed_agents
    ):
        if expected_agents != observed_agents:
            report.error(
                f"observed_agents mismatch: expected {expected_agents}, "
                f"got {observed_agents}"
            )
        if expected_agents <= 0:
            report.error("expected_agents must be positive")


def _check_run_identity(
    run_dir: Path, meta: Dict[str, Any], report: ValidationReport
) -> None:
    run_id = meta.get("run_id")
    try:
        normalized = normalize_run_id(run_id)
    except (InvalidRunIdError, TypeError) as error:
        report.error(f"invalid run_id in run_meta.json: {error}")
        return

    expected_name = f"output_{normalized}"
    if run_dir.name != expected_name:
        report.error(
            f"run ID/directory mismatch: expected directory {expected_name!r}, "
            f"got {run_dir.name!r}"
        )
    if meta.get("output_directory") != expected_name:
        report.error(
            "output_directory metadata mismatch: "
            f"expected {expected_name!r}, "
            f"got {meta.get('output_directory')!r}"
        )


def _check_config(
    meta: Dict[str, Any], report: ValidationReport
) -> Optional[Dict[str, Any]]:
    config = meta.get("config")
    if not isinstance(config, dict):
        report.error("config must be a JSON object")
        return None

    try:
        validate_public_config_boundary(config)
        actual_hash = compute_config_hash(config)
    except (TypeError, ValueError) as error:
        report.error(
            "config snapshot cannot be canonically hashed: "
            f"{type(error).__name__}"
        )
        return config

    stored_hash = meta.get("config_hash")
    if (
        not isinstance(stored_hash, str)
        or not HEX_SHA256_RE.fullmatch(stored_hash)
    ):
        report.error("config_hash must be a lowercase SHA-256 hex digest")
    elif actual_hash != stored_hash:
        report.error("config_hash does not match the saved config snapshot")
    if (
        meta.get("config_hash_algorithm")
        != "sha256-canonical-json-v1"
    ):
        report.error("unsupported or missing config_hash_algorithm")

    simulation = config.get("simulation")
    blocs = config.get("blocs")
    if not isinstance(simulation, dict):
        report.error("config.simulation must be an object")
        return config
    if not isinstance(blocs, list):
        report.error("config.blocs must be an array")
        return config

    try:
        response_contract_version = validate_response_contract_version(
            simulation.get("response_contract_version")
        )
    except ValueError as error:
        report.error(str(error))
        response_contract_version = LEGACY_RESPONSE_CONTRACT_VERSION

    recorded_contract_version = meta.get("response_contract_version")
    if recorded_contract_version is None:
        if response_contract_version != LEGACY_RESPONSE_CONTRACT_VERSION:
            report.error("run_meta.json is missing response_contract_version")
    elif recorded_contract_version != response_contract_version:
        report.error("response_contract_version differs from config snapshot")

    expected_schema_hash = response_schema_sha256(response_contract_version)
    has_vllm = any(
        isinstance(bloc, dict) and bloc.get("provider", "ollama") == "vllm"
        for bloc in blocs
    )
    if response_contract_version != LEGACY_RESPONSE_CONTRACT_VERSION:
        if not _is_nonempty_string(simulation.get("protocol_version")):
            report.error(
                "phase-response-v2.0.0 requires an explicit protocol_version"
            )
        if simulation.get("log_schema_version") != OBSERVABILITY_LOG_SCHEMA_VERSION:
            report.error(
                "phase-response-v2.0.0 config must select log schema 2.0.0"
            )
        if meta.get("log_schema_version") != OBSERVABILITY_LOG_SCHEMA_VERSION:
            report.error(
                "phase-response-v2.0.0 metadata must record log schema 2.0.0"
            )
        if meta.get("response_schema_sha256") != expected_schema_hash:
            report.error(
                "response_schema_sha256 does not match the versioned contract"
            )
        if meta.get("response_schema_hash_algorithm") != "sha256-canonical-json-v1":
            report.error("unsupported or missing response_schema_hash_algorithm")
        if (
            meta.get("vllm_transport_contract_version")
            != vllm_transport_contract_version(response_contract_version)
        ):
            report.error(
                "vllm_transport_contract_version differs from the response contract"
            )
    elif recorded_contract_version is not None:
        if meta.get("response_schema_sha256") is not None:
            report.error("legacy response contract must not record a schema hash")
        if meta.get("response_schema_hash_algorithm") is not None:
            report.error("legacy response contract must not record a schema hash algorithm")
        expected_transport_version = (
            vllm_transport_contract_version(response_contract_version)
            if has_vllm
            else None
        )
        if meta.get("vllm_transport_contract_version") != expected_transport_version:
            report.error("legacy vLLM transport contract version is inconsistent")

    if simulation.get("run_name") != meta.get("run_name"):
        report.error("run_name differs between metadata and config snapshot")
    if (
        "run_id" in simulation
        and simulation.get("run_id") != meta.get("run_id")
    ):
        report.error("explicit config run_id differs from metadata run_id")
    if simulation.get("duration") != meta.get("expected_steps"):
        report.error(
            "expected_steps differs from config.simulation.duration"
        )
    for config_key, meta_key in (
        ("protocol_version", "protocol_version"),
        ("metric_version", "metric_version"),
    ):
        if (
            config_key in simulation
            and simulation[config_key] != meta.get(meta_key)
        ):
            report.error(f"{meta_key} differs from config snapshot")

    derived_agents = 0
    for index, bloc in enumerate(blocs):
        if not isinstance(bloc, dict):
            report.error(f"config.blocs[{index}] must be an object")
            continue
        count = bloc.get("num_agents")
        if not _is_nonnegative_int(count) or count <= 0:
            report.error(
                f"config.blocs[{index}].num_agents must be positive"
            )
            continue
        derived_agents += count
        if response_contract_version != LEGACY_RESPONSE_CONTRACT_VERSION:
            if bloc.get("provider") != "vllm":
                report.error(
                    f"config.blocs[{index}] must use vllm under a structured contract"
                )
            overrides = bloc.get("llm_overrides", {})
            if isinstance(overrides, dict) and "response_format" in overrides:
                report.error(
                    f"config.blocs[{index}] response_format must be phase-owned"
                )
    if derived_agents != meta.get("expected_agents"):
        report.error(
            "expected_agents differs from sum(config.blocs[].num_agents): "
            f"{meta.get('expected_agents')!r} != {derived_agents}"
        )

    try:
        expected_models = collect_bloc_models(config)
    except Exception:
        report.error("models cannot be derived from the config snapshot")
    else:
        if meta.get("models") != expected_models:
            report.error(
                "models metadata differs from the saved config snapshot"
            )
    return config


def _check_provenance(
    meta: Dict[str, Any], report: ValidationReport
) -> None:
    prompt_hash = meta.get("prompt_hash")
    if (
        not isinstance(prompt_hash, str)
        or not HEX_SHA256_RE.fullmatch(prompt_hash)
    ):
        report.error("prompt_hash must be a lowercase SHA-256 hex digest")
    if meta.get("prompt_hash_algorithm") != "sha256-file-bytes":
        report.error("unsupported or missing prompt_hash_algorithm")

    git_status = meta.get("git_probe_status")
    if git_status == "available":
        git_sha = meta.get("git_sha")
        if (
            not isinstance(git_sha, str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", git_sha)
        ):
            report.error(
                "git_sha is invalid although git_probe_status is available"
            )
        if not isinstance(meta.get("git_dirty"), bool):
            report.error(
                "git_dirty is not boolean although git_probe_status is available"
            )
        elif meta.get("git_dirty") is True:
            report.cannot_verify(
                "exact source state: the Git worktree was dirty and its patch "
                "is not embedded in run metadata"
            )
    elif git_status == "unavailable":
        if not isinstance(meta.get("git_probe_errors"), list):
            report.error(
                "unavailable git provenance lacks git_probe_errors"
            )
        report.cannot_verify(
            "source Git SHA/dirty state: Git probe was unavailable"
        )
    else:
        report.error(
            "git_probe_status must be 'available' or 'unavailable'"
        )

    if meta.get("execution_identity_policy") != "logical-endpoints-only-v1":
        report.error("execution_identity_policy is invalid or missing")
    for forbidden in ("hostname", "cuda_visible_devices"):
        if forbidden in meta:
            report.error(f"public run metadata contains forbidden field: {forbidden}")
    for key in ("os", "platform", "python_version"):
        if not _is_nonempty_string(meta.get(key)):
            report.error(f"{key} must be a non-empty string")
    dependencies = meta.get("dependencies")
    if not isinstance(dependencies, dict):
        report.error("dependencies must be an object")
    elif any(
        not isinstance(name, str)
        or (
            version is not None
            and (
                not isinstance(version, str)
                or not version.strip()
            )
        )
        for name, version in dependencies.items()
    ):
        report.error(
            "dependencies must map names to non-empty version strings or null"
        )
    else:
        expected_dependency_names = set(DEPENDENCY_DISTRIBUTIONS)
        if set(dependencies) != expected_dependency_names:
            report.error(
                "dependencies keys do not match the required distributions"
            )
        missing_dependencies = sorted(
            name for name, version in dependencies.items() if version is None
        )
        if missing_dependencies:
            report.cannot_verify(
                "dependency versions unavailable: "
                + ", ".join(missing_dependencies)
            )
        dependency_status = meta.get("dependencies_probe_status")
        if dependency_status not in {"available", "partial", "unavailable"}:
            report.error(
                "dependencies_probe_status must be available, partial, or "
                "unavailable"
            )
        expected_status = (
            "available"
            if not missing_dependencies
            else (
                "unavailable"
                if len(missing_dependencies) == len(DEPENDENCY_DISTRIBUTIONS)
                else "partial"
            )
        )
        if dependency_status != expected_status:
            report.error(
                "dependencies_probe_status is inconsistent with versions"
            )
        dependency_errors = meta.get("dependencies_probe_errors")
        expected_dependency_errors = [
            f"{name}:version_unavailable"
            for name in DEPENDENCY_DISTRIBUTIONS
            if dependencies.get(name) is None
        ]
        if dependency_errors != expected_dependency_errors:
            report.error(
                "dependencies_probe_errors is inconsistent with versions"
            )
        if dependency_status in {"partial", "unavailable"}:
            report.cannot_verify(
                "complete dependency environment: one or more package "
                "versions were unavailable"
            )
    gpu_info = meta.get("gpu_info")
    if not isinstance(gpu_info, dict):
        report.error("gpu_info must be an object")
    elif gpu_info.get("status") not in {
        "available",
        "partial",
        "unavailable",
    }:
        report.error(
            "gpu_info.status must be available, partial, or unavailable"
        )
    elif gpu_info.get("status") == "unavailable":
        report.cannot_verify(
            "GPU/driver/CUDA details: GPU probe was unavailable"
        )
    else:
        devices = gpu_info.get("devices")
        if not isinstance(devices, list) or not devices:
            report.error(
                "available/partial gpu_info.devices must be a non-empty array"
            )
        elif any(
            not isinstance(device, dict)
            or any(
                not _is_nonempty_string(device.get(field_name))
                for field_name in (
                    "index",
                    "name",
                    "memory_total_mib",
                )
            )
            for device in devices
        ):
            report.error("available/partial GPU device entries are incomplete")
        elif any("uuid" in device for device in devices):
            report.error("public GPU inventory must not contain device UUIDs")
        if not _is_nonempty_string(gpu_info.get("driver_version")):
            report.error(
                "available/partial gpu_info.driver_version must be a non-empty string"
            )
        if gpu_info.get("status") == "partial":
            report.cannot_verify(
                "complete GPU inventory: one or more device rows were malformed"
            )

    if isinstance(gpu_info, dict):
        malformed_device_rows = gpu_info.get("malformed_device_rows")
        if not _is_nonnegative_int(malformed_device_rows):
            report.error(
                "gpu_info.malformed_device_rows must be a non-negative integer"
            )
        elif gpu_info.get("status") == "partial":
            if malformed_device_rows == 0:
                report.error(
                    "partial GPU probe must report malformed_device_rows"
                )
            if gpu_info.get("error") != "malformed_device_rows":
                report.error(
                    "partial GPU probe must report malformed_device_rows error"
                )
        elif (
            gpu_info.get("status") == "available"
            and malformed_device_rows != 0
        ):
            report.error(
                "available GPU probe cannot contain malformed device rows"
            )

        cuda_probe_status = gpu_info.get("cuda_probe_status")
        if cuda_probe_status not in {"available", "unavailable"}:
            report.error(
                "gpu_info.cuda_probe_status must be available or unavailable"
            )
        elif cuda_probe_status == "available":
            if not _is_nonempty_string(gpu_info.get("cuda_version")):
                report.error(
                    "available CUDA probe must include cuda_version"
                )
            if gpu_info.get("cuda_probe_error") is not None:
                report.error(
                    "available CUDA probe has a non-null error"
                )
        else:
            if not _is_nonempty_string(gpu_info.get("cuda_probe_error")):
                report.error(
                    "unavailable CUDA probe must include an error code"
                )
            report.cannot_verify(
                "CUDA version: the CUDA-version probe was unavailable"
            )

    models = meta.get("models")
    if not isinstance(models, list):
        report.error("models must be an array")
    elif any(not isinstance(model, dict) for model in models):
        report.error("models entries must be objects")
    else:
        model_detail_fields = (
            "model_digest",
            "quantization",
            "chat_template_hash",
        )
        missing_model_fields = sorted({
            field_name
            for model in models
            for field_name in model_detail_fields
            if model.get(field_name) is None
        })
        if missing_model_fields:
            report.cannot_verify(
                "model artifact details unavailable for one or more blocs: "
                + ", ".join(missing_model_fields)
            )
        for model in models:
            for field_name in ("model_digest", "quantization"):
                value = model.get(field_name)
                if value is not None and not _is_nonempty_string(value):
                    report.error(
                        f"model {field_name} must be a non-empty string or null"
                    )
            chat_template_hash = model.get("chat_template_hash")
            if (
                chat_template_hash is not None
                and (
                    not isinstance(chat_template_hash, str)
                    or not HEX_SHA256_RE.fullmatch(chat_template_hash)
                )
            ):
                report.error(
                    "model chat_template_hash must be a SHA-256 digest or null"
                )
            present_count = sum(
                model.get(field_name) is not None
                for field_name in model_detail_fields
            )
            expected_detail_source = (
                "config"
                if present_count == len(model_detail_fields)
                else ("partial" if present_count else "unavailable")
            )
            if model.get("detail_source") != expected_detail_source:
                report.error(
                    "model detail_source is inconsistent with saved details"
                )


def _check_manifest(
    run_dir: Path, meta: Dict[str, Any], report: ValidationReport
) -> None:
    if meta.get("raw_manifest_status") != "available":
        report.error(
            "completed run raw_manifest_status must be 'available'"
        )
    if meta.get("raw_manifest_error") is not None:
        report.error("completed run has a non-null raw_manifest_error")
    manifest = meta.get("raw_manifest")
    if not isinstance(manifest, dict):
        report.error(
            "raw_manifest must be an object in a completed run"
        )
        return
    if manifest.get("algorithm") != "sha256":
        report.error("raw_manifest.algorithm must be 'sha256'")
    files = manifest.get("files")
    if not isinstance(files, dict):
        report.error("raw_manifest.files must be an object")
        return

    config = meta.get("config")
    required_files = raw_jsonl_files_for_schema(
        meta.get("log_schema_version"),
        has_disaster=isinstance(config, dict) and "scenario" in config,
    )
    required = set(required_files)
    recorded = set(files)
    if recorded != required:
        missing = sorted(required - recorded)
        extra = sorted(recorded - required)
        if missing:
            report.error(
                "raw_manifest is missing required files: "
                + ", ".join(missing)
            )
        if extra:
            report.error(
                "raw_manifest contains unexpected files: "
                + ", ".join(extra)
            )

    try:
        actual_jsonl = {
            path.name
            for path in run_dir.iterdir()
            if path.suffix == ".jsonl"
        }
    except OSError as error:
        report.error(
            f"cannot list run directory: {type(error).__name__}"
        )
        return
    if actual_jsonl != required:
        missing = sorted(required - actual_jsonl)
        extra = sorted(actual_jsonl - required)
        if missing:
            report.error(
                "required raw files are missing: " + ", ".join(missing)
            )
        if extra:
            report.error(
                "unmanifested JSONL files are present: "
                + ", ".join(extra)
            )

    for filename in required_files:
        path = run_dir / filename
        entry = files.get(filename)
        if not isinstance(entry, dict):
            if filename in files:
                report.error(
                    f"raw_manifest entry for {filename} must be an object"
                )
            continue
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not HEX_SHA256_RE.fullmatch(digest):
            report.error(
                f"raw_manifest entry for {filename} has an invalid sha256"
            )
        for count_field in ("bytes", "lines"):
            if not _is_nonnegative_int(entry.get(count_field)):
                report.error(
                    f"raw_manifest entry for {filename}.{count_field} "
                    "must be a non-negative integer"
                )
        if path.is_symlink():
            report.error(
                f"raw file must not be a symbolic link: {filename}"
            )
            continue
        if not path.is_file():
            continue
        try:
            actual = file_manifest(path)
        except OSError as error:
            report.error(
                f"cannot hash {filename}: {type(error).__name__}"
            )
            continue
        for attribute in ("sha256", "bytes", "lines"):
            if entry.get(attribute) != actual[attribute]:
                report.error(
                    f"raw manifest mismatch for "
                    f"{filename}.{attribute}: "
                    f"recorded {entry.get(attribute)!r}, "
                    f"actual {actual[attribute]!r}"
                )


def _read_jsonl(path: Path, report: ValidationReport) -> List[Record]:
    records: List[Record] = []
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return records
    except OSError as error:
        report.error(
            f"cannot read {path.name}: {type(error).__name__}"
        )
        return records
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        report.error(
            f"{path.name} is not valid UTF-8 at byte {error.start}"
        )
        return records
    if raw and not raw.endswith(b"\n"):
        report.error(f"{path.name} does not end with a JSONL newline")

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            report.error(f"{path.name}:{line_number} is blank")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            report.error(
                f"{path.name}:{line_number} is invalid JSON "
                f"at column {error.colno}"
            )
            continue
        if not isinstance(value, dict):
            report.error(
                f"{path.name}:{line_number} must contain a JSON object"
            )
            continue
        records.append((line_number, value))
    return records


def _missing_fields(
    record: Dict[str, Any], fields: Iterable[str]
) -> List[str]:
    return [field for field in fields if field not in record]


def _valid_step_agent(
    record: Dict[str, Any], expected_steps: int, expected_agents: int
) -> bool:
    step = record.get("step")
    agent_id = record.get("agent_id")
    return (
        _is_int(step)
        and 1 <= step <= expected_steps
        and _is_int(agent_id)
        and 0 <= agent_id < expected_agents
    )


def _check_natural_key_duplicates(
    filename: str,
    records: Sequence[Record],
    key_builder: Callable[
        [Dict[str, Any]], Optional[Tuple[Any, ...]]
    ],
    report: ValidationReport,
) -> None:
    first_lines: Dict[Tuple[Any, ...], int] = {}
    for line_number, record in records:
        key = key_builder(record)
        if key is None:
            continue
        previous = first_lines.get(key)
        if previous is None:
            first_lines[key] = line_number
        else:
            report.error(
                f"{filename}:{line_number} duplicates natural key "
                f"{key!r} from line {previous}"
            )


def _derive_agent_labels(
    config: Dict[str, Any],
) -> Dict[int, Tuple[str, str]]:
    labels: Dict[int, Tuple[str, str]] = {}
    agent_id = 0
    blocs = config.get("blocs", [])
    if not isinstance(blocs, list):
        return labels
    for bloc in blocs:
        if (
            not isinstance(bloc, dict)
            or not _is_nonnegative_int(bloc.get("num_agents"))
        ):
            continue
        for _ in range(bloc["num_agents"]):
            labels[agent_id] = (
                bloc.get("name"),
                bloc.get("model"),
            )
            agent_id += 1
    return labels


def _check_primary_records(
    filename: str,
    records: Sequence[Record],
    expected_steps: int,
    expected_agents: int,
    labels: Dict[int, Tuple[str, str]],
    log_schema_version: Any,
    response_contract_version: str,
    report: ValidationReport,
    expect_complete: bool = True,
) -> set[Tuple[int, int]]:
    is_phase1 = filename == "phase1_raw.jsonl"
    required = (
        ("step", "agent_id", "bloc", "model", "parsed", "raw_output")
        if is_phase1
        else (
            "step",
            "agent_id",
            "bloc",
            "model",
            "position",
            "action",
            "direction",
            "memory",
            "reasoning",
        )
    )
    keys: set[Tuple[int, int]] = set()
    for line_number, record in records:
        missing = _missing_fields(record, required)
        if missing:
            report.error(
                f"{filename}:{line_number} missing fields: "
                + ", ".join(missing)
            )
        if not _valid_step_agent(
            record, expected_steps, expected_agents
        ):
            report.error(
                f"{filename}:{line_number} has invalid step or agent_id"
            )
            continue
        step = record["step"]
        agent_id = record["agent_id"]
        keys.add((step, agent_id))
        expected_label = labels.get(agent_id)
        if (
            expected_label is not None
            and (record.get("bloc"), record.get("model"))
            != expected_label
        ):
            report.error(
                f"{filename}:{line_number} bloc/model "
                "does not match config"
            )
        if (
            not isinstance(record.get("bloc"), str)
            or not isinstance(record.get("model"), str)
        ):
            report.error(
                f"{filename}:{line_number} bloc/model must be strings"
            )
        if is_phase1:
            if (
                record.get("parsed") is not None
                and not isinstance(record.get("parsed"), dict)
            ):
                report.error(
                    f"{filename}:{line_number} parsed "
                    "must be an object or null"
                )
            if not isinstance(record.get("raw_output"), str):
                report.error(
                    f"{filename}:{line_number} "
                    "raw_output must be a string"
                )
            if (
                response_contract_version
                != LEGACY_RESPONSE_CONTRACT_VERSION
                and isinstance(record.get("parsed"), dict)
            ):
                try:
                    validate_parsed_response(
                        record["parsed"], "phase1", response_contract_version
                    )
                except ValueError as error:
                    report.error(
                        f"{filename}:{line_number} violates "
                        f"{response_contract_version}: {error}"
                    )
        else:
            position = record.get("position")
            if not (
                isinstance(position, list)
                and len(position) == 2
                and all(_is_int(value) for value in position)
            ):
                report.error(
                    f"{filename}:{line_number} "
                    "position must be two integers"
                )
            for key in ("action", "memory", "reasoning"):
                if not isinstance(record.get(key), str):
                    report.error(
                        f"{filename}:{line_number} "
                        f"{key} must be a string"
                    )
            direction = record.get("direction")
            null_stay_is_valid = (
                log_schema_version in {
                    LOG_SCHEMA_VERSION,
                    DISASTER_LOG_SCHEMA_VERSION,
                    OBSERVABILITY_LOG_SCHEMA_VERSION,
                }
                and record.get("action") == "stay"
                and direction is None
            )
            if not isinstance(direction, str) and not null_stay_is_valid:
                if log_schema_version == LEGACY_LOG_SCHEMA_VERSION:
                    requirement = "must be a string under log schema 1.0.0"
                elif log_schema_version in {
                    LOG_SCHEMA_VERSION,
                    DISASTER_LOG_SCHEMA_VERSION,
                    OBSERVABILITY_LOG_SCHEMA_VERSION,
                }:
                    requirement = (
                        "must be a string, or null only when action is "
                        "'stay', under log schema 1.1.0, 1.2.0, or 2.0.0"
                    )
                else:
                    requirement = (
                        "must be a string when the recorded log schema "
                        "is unsupported"
                    )
                report.error(
                    f"{filename}:{line_number} direction {requirement}"
                )
            if (
                response_contract_version
                != LEGACY_RESPONSE_CONTRACT_VERSION
            ):
                parsed = {
                    key: record.get(key)
                    for key in ("action", "direction", "memory", "reasoning")
                }
                try:
                    validate_parsed_response(
                        parsed, "phase3", response_contract_version
                    )
                except ValueError as error:
                    report.error(
                        f"{filename}:{line_number} violates "
                        f"{response_contract_version}: {error}"
                    )

    _check_natural_key_duplicates(
        filename,
        records,
        lambda record: (
            (record.get("step"), record.get("agent_id"))
            if _valid_step_agent(
                record, expected_steps, expected_agents
            )
            else None
        ),
        report,
    )
    expected_count = expected_steps * expected_agents if expect_complete else 0
    if len(records) != expected_count:
        report.error(
            f"{filename} row count mismatch: "
            f"expected {expected_count}, got {len(records)}"
        )
    expected_keys = (
        {
            (step, agent_id)
            for step in range(1, expected_steps + 1)
            for agent_id in range(expected_agents)
        }
        if expect_complete
        else set()
    )
    if keys != expected_keys:
        missing_keys = sorted(expected_keys - keys)
        extra_keys = sorted(keys - expected_keys)
        if missing_keys:
            report.error(
                f"{filename} lacks {len(missing_keys)} "
                "expected natural keys; "
                f"examples: {missing_keys[:5]!r}"
            )
        if extra_keys:
            report.error(
                f"{filename} has {len(extra_keys)} "
                "unexpected natural keys; "
                f"examples: {extra_keys[:5]!r}"
            )
    return keys


def _check_messages(
    records: Sequence[Record],
    phase1_records: Sequence[Record],
    memory_records: Sequence[Record],
    expected_steps: int,
    expected_agents: int,
    labels: Dict[int, Tuple[str, str]],
    config: Dict[str, Any],
    report: ValidationReport,
) -> None:
    filename = "messages.jsonl"
    scenario_config = config.get("scenario")
    if (
        isinstance(scenario_config, dict)
        and scenario_config.get("communication_mode") == "communication_none"
    ):
        if records:
            report.error("messages.jsonl must be empty for communication_none")
        return
    phase1_by_key = {
        (record.get("step"), record.get("agent_id")): record
        for _, record in phase1_records
        if _valid_step_agent(record, expected_steps, expected_agents)
    }
    positions_by_key = {
        (record.get("step"), record.get("agent_id")): record.get("position")
        for _, record in memory_records
        if _valid_step_agent(record, expected_steps, expected_agents)
        and isinstance(record.get("position"), list)
        and len(record["position"]) == 2
        and all(_is_int(value) for value in record["position"])
    }
    expected_primary_keys = {
        (step, agent_id)
        for step in range(1, expected_steps + 1)
        for agent_id in range(expected_agents)
    }
    expected_receivers: Optional[Dict[Tuple[int, int], List[int]]] = None
    if (
        set(phase1_by_key) != expected_primary_keys
        or set(positions_by_key) != expected_primary_keys
    ):
        report.error(
            "cannot reconstruct expected messages from incomplete "
            "Phase 1 decisions or positions"
        )
    else:
        simulation = config.get("simulation")
        agents_config = config.get("agents")
        places = config.get("places")
        if (
            not isinstance(simulation, dict)
            or not isinstance(agents_config, dict)
            or not isinstance(places, list)
            or "half_space_size" not in simulation
            or "communication_radius" not in agents_config
        ):
            report.error(
                "cannot reconstruct expected messages from invalid world or "
                "communication config"
            )
        else:
            try:
                world = World(simulation["half_space_size"], places)
                communication_radius = agents_config["communication_radius"]
                edge_policy = agents_config.get("edge_policy", "full")
                if edge_policy not in {"full", "none", "within_bloc_only"}:
                    raise ValueError("invalid agents.edge_policy")
                reconstructed: Dict[Tuple[int, int], List[int]] = {}
                for step in range(1, expected_steps + 1):
                    for sender_id in range(expected_agents):
                        if edge_policy == "none":
                            continue
                        parsed = phase1_by_key[(step, sender_id)].get("parsed")
                        if not isinstance(parsed, dict):
                            continue
                        if not parsed.get("message", ""):
                            continue
                        sender_position = positions_by_key[(step, sender_id)]
                        sender_place = world.get_place_for(*sender_position)
                        receiver_ids = []
                        for receiver_id in range(expected_agents):
                            if receiver_id == sender_id:
                                continue
                            if (
                                edge_policy == "within_bloc_only"
                                and labels.get(sender_id, (None, None))[0]
                                != labels.get(receiver_id, (None, None))[0]
                            ):
                                continue
                            receiver_position = positions_by_key[
                                (step, receiver_id)
                            ]
                            distance = world.euclidean_distance(
                                sender_position[0],
                                sender_position[1],
                                receiver_position[0],
                                receiver_position[1],
                            )
                            if distance > communication_radius:
                                continue
                            receiver_place = world.get_place_for(
                                *receiver_position
                            )
                            if sender_place is None and receiver_place is None:
                                receiver_ids.append(receiver_id)
                            elif (
                                sender_place is not None
                                and receiver_place is not None
                                and sender_place.name == receiver_place.name
                            ):
                                receiver_ids.append(receiver_id)
                        if receiver_ids:
                            reconstructed[(step, sender_id)] = receiver_ids
                expected_receivers = reconstructed
            except Exception as error:
                report.error(
                    "cannot reconstruct expected messages: "
                    f"{type(error).__name__}"
                )

    required = (
        "step",
        "sender_id",
        "sender_bloc",
        "sender_model",
        "receiver_ids",
        "message",
        "reasoning",
    )
    for line_number, record in records:
        missing = _missing_fields(record, required)
        if missing:
            report.error(
                f"{filename}:{line_number} missing fields: "
                + ", ".join(missing)
            )
        step = record.get("step")
        sender_id = record.get("sender_id")
        if not _is_int(step) or not 1 <= step <= expected_steps:
            report.error(f"{filename}:{line_number} has invalid step")
        if (
            not _is_int(sender_id)
            or not 0 <= sender_id < expected_agents
        ):
            report.error(
                f"{filename}:{line_number} has invalid sender_id"
            )
        elif labels.get(sender_id) != (
            record.get("sender_bloc"),
            record.get("sender_model"),
        ):
            report.error(
                f"{filename}:{line_number} sender bloc/model "
                "does not match config"
            )
        if (
            _is_int(step)
            and 1 <= step <= expected_steps
            and _is_int(sender_id)
            and 0 <= sender_id < expected_agents
        ):
            phase1_record = phase1_by_key.get((step, sender_id))
            parsed = (
                phase1_record.get("parsed")
                if isinstance(phase1_record, dict)
                else None
            )
            if not isinstance(parsed, dict):
                report.error(
                    f"{filename}:{line_number} has no matching parsed "
                    "Phase 1 message decision"
                )
            elif (
                record.get("message") != parsed.get("message", "")
                or record.get("reasoning") != parsed.get("reasoning", "")
            ):
                report.error(
                    f"{filename}:{line_number} message/reasoning differs "
                    "from the matching Phase 1 output"
                )

        receiver_ids = record.get("receiver_ids")
        if not isinstance(receiver_ids, list) or not receiver_ids:
            report.error(
                f"{filename}:{line_number} receiver_ids "
                "must be a non-empty array"
            )
        elif not all(
            _is_int(receiver_id) for receiver_id in receiver_ids
        ):
            report.error(
                f"{filename}:{line_number} receiver_ids "
                "must contain integers"
            )
        else:
            if len(set(receiver_ids)) != len(receiver_ids):
                report.error(
                    f"{filename}:{line_number} "
                    "contains duplicate receiver_ids"
                )
            if any(
                not 0 <= receiver_id < expected_agents
                for receiver_id in receiver_ids
            ):
                report.error(
                    f"{filename}:{line_number} "
                    "contains an invalid receiver_id"
                )
            if sender_id in receiver_ids:
                report.error(
                    f"{filename}:{line_number} contains a self-delivery"
                )
            if (
                expected_receivers is not None
                and _is_int(step)
                and _is_int(sender_id)
                and (step, sender_id) in expected_receivers
                and receiver_ids != expected_receivers[(step, sender_id)]
            ):
                report.error(
                    f"{filename}:{line_number} receiver_ids differ from "
                    "the reconstructed communication boundary: "
                    f"expected {expected_receivers[(step, sender_id)]!r}, "
                    f"got {receiver_ids!r}"
                )
        for key in (
            "sender_bloc",
            "sender_model",
            "message",
            "reasoning",
        ):
            if not isinstance(record.get(key), str):
                report.error(
                    f"{filename}:{line_number} {key} must be a string"
                )
        if record.get("message") == "":
            report.error(
                f"{filename}:{line_number} message must be non-empty"
            )

    _check_natural_key_duplicates(
        filename,
        records,
        lambda record: (
            (record.get("step"), record.get("sender_id"))
            if _is_int(record.get("step"))
            and _is_int(record.get("sender_id"))
            else None
        ),
        report,
    )
    if expected_receivers is not None:
        actual_keys = {
            (record.get("step"), record.get("sender_id"))
            for _, record in records
            if _is_int(record.get("step"))
            and _is_int(record.get("sender_id"))
        }
        expected_keys = set(expected_receivers)
        missing_keys = sorted(expected_keys - actual_keys)
        extra_keys = sorted(actual_keys - expected_keys)
        if missing_keys:
            report.error(
                f"{filename} lacks {len(missing_keys)} expected message "
                f"natural keys; examples: {missing_keys[:5]!r}"
            )
        if extra_keys:
            report.error(
                f"{filename} has {len(extra_keys)} unexpected message "
                f"natural keys; examples: {extra_keys[:5]!r}"
            )


def _check_parse_errors(
    records: Sequence[Record],
    expected_steps: int,
    expected_agents: int,
    phase1_records: Sequence[Record],
    memory_keys: set[Tuple[int, int]],
    meta: Dict[str, Any],
    report: ValidationReport,
) -> None:
    filename = "parse_errors.jsonl"
    phase1_null_keys = {
        (record.get("step"), record.get("agent_id"))
        for _, record in phase1_records
        if _valid_step_agent(
            record, expected_steps, expected_agents
        )
        and record.get("parsed") is None
    }
    logged_phase1_keys: set[Tuple[int, int]] = set()
    for line_number, record in records:
        missing = _missing_fields(
            record,
            ("step", "agent_id", "phase", "raw_output"),
        )
        if missing:
            report.error(
                f"{filename}:{line_number} missing fields: "
                + ", ".join(missing)
            )
        if not _valid_step_agent(
            record, expected_steps, expected_agents
        ):
            report.error(
                f"{filename}:{line_number} "
                "has invalid step or agent_id"
            )
            continue
        phase = record.get("phase")
        if phase not in {1, 3}:
            report.error(
                f"{filename}:{line_number} phase must be 1 or 3"
            )
            continue
        if not isinstance(record.get("raw_output"), str):
            report.error(
                f"{filename}:{line_number} "
                "raw_output must be a string"
            )
        key = (record["step"], record["agent_id"])
        if phase == 1:
            logged_phase1_keys.add(key)
        elif key not in memory_keys:
            report.error(
                f"{filename}:{line_number} "
                "has no matching Phase 3 record"
            )

    _check_natural_key_duplicates(
        filename,
        records,
        lambda record: (
            (
                record.get("step"),
                record.get("agent_id"),
                record.get("phase"),
            )
            if _valid_step_agent(
                record, expected_steps, expected_agents
            )
            and record.get("phase") in {1, 3}
            else None
        ),
        report,
    )
    if phase1_null_keys != logged_phase1_keys:
        report.error(
            "Phase 1 null parsed outputs do not match "
            "Phase 1 parse-error records"
        )
    syntax_failures = meta.get("syntax_parse_failures")
    if (
        _is_nonnegative_int(syntax_failures)
        and len(records) != syntax_failures
    ):
        report.error(
            f"parse_errors.jsonl row count mismatch: "
            f"expected {syntax_failures}, got {len(records)}"
        )


def _check_attempt_records(
    records: Sequence[Record],
    expected_steps: int,
    expected_agents: int,
    labels: Dict[int, Tuple[str, str]],
    config: Dict[str, Any],
    meta: Dict[str, Any],
    report: ValidationReport,
) -> None:
    filename = "llm_attempts.jsonl"
    required = {
        "schema_version",
        "event_id",
        "run_id",
        "generation_attempt",
        "http_attempt",
        "http_status",
        "http_response_body_base64",
        "http_response_bytes",
        "http_response_sha256",
        "envelope",
        "raw_output",
        "finish_reason",
        "usage",
        "transport_status",
        "parse_status",
        "schema_status",
        "failure_kind",
        "error_type",
        "request_id",
        "step",
        "phase",
        "agent_id",
        "model",
        "provider",
        "endpoint_id",
        "device_slot",
    }
    legacy_transport = (
        config.get("simulation", {}).get("transport_behavior_version")
        == LEGACY_TRANSPORT_BEHAVIOR_VERSION
    )
    observed_keys: List[Tuple[int, int, int]] = []
    attempts_by_key: Dict[Tuple[int, int, int], List[Record]] = {}
    event_ids = set()
    phase_order = {"phase1": 1, "phase3": 3}
    for line_number, record in records:
        if set(record) != required:
            report.error(
                f"{filename}:{line_number} fields differ from schema 2.0"
            )
        if not _valid_step_agent(record, expected_steps, expected_agents):
            report.error(
                f"{filename}:{line_number} has invalid step or agent_id"
            )
            continue
        phase = record.get("phase")
        if phase not in phase_order:
            report.error(f"{filename}:{line_number} has invalid phase")
            continue
        step = record["step"]
        agent_id = record["agent_id"]
        request_key = (step, phase_order[phase], agent_id)
        if request_key not in attempts_by_key:
            observed_keys.append(request_key)
            attempts_by_key[request_key] = []
        attempts_by_key[request_key].append((line_number, record))
        expected_request_id = (
            f"step-{step:06d}:{phase}:agent-{agent_id:06d}"
        )
        if record.get("request_id") != expected_request_id:
            report.error(f"{filename}:{line_number} request_id mismatch")
        generation_attempt = record.get("generation_attempt")
        expected_event_id = (
            f"{meta.get('run_id')}:llm_attempt:{expected_request_id}:"
            f"generation-{generation_attempt}"
        )
        if record.get("event_id") != expected_event_id:
            report.error(f"{filename}:{line_number} event_id mismatch")
        event_id = record.get("event_id")
        if event_id in event_ids:
            report.error(f"{filename}:{line_number} duplicates event_id")
        event_ids.add(event_id)
        if record.get("run_id") != meta.get("run_id"):
            report.error(f"{filename}:{line_number} run_id mismatch")
        if record.get("schema_version") != "1.0.0":
            report.error(f"{filename}:{line_number} schema_version mismatch")
        allowed_generations = {1, 2} if legacy_transport else {1}
        if generation_attempt not in allowed_generations:
            report.error(f"{filename}:{line_number} generation_attempt is invalid")
        if record.get("http_attempt") != 1:
            report.error(f"{filename}:{line_number} http_attempt must be 1")
        if record.get("transport_status") != "ok":
            report.error(f"{filename}:{line_number} transport_status is not ok")
        recovered_legacy_attempt = (
            legacy_transport
            and generation_attempt == 1
            and record.get("parse_status") == "invalid"
            and record.get("schema_status") == "not_checked"
            and record.get("failure_kind") == "syntax"
            and record.get("error_type") is None
        )
        if not recovered_legacy_attempt:
            if record.get("parse_status") != "valid":
                report.error(f"{filename}:{line_number} parse_status is not valid")
            if record.get("schema_status") != "valid":
                report.error(f"{filename}:{line_number} schema_status is not valid")
            if record.get("failure_kind") is not None:
                report.error(f"{filename}:{line_number} failure_kind is not null")
            if record.get("error_type") is not None:
                report.error(f"{filename}:{line_number} error_type is not null")
        status = record.get("http_status")
        if not _is_int(status) or not 200 <= status < 300:
            report.error(f"{filename}:{line_number} HTTP status is not 2xx")

        encoded = record.get("http_response_body_base64")
        body: Optional[bytes] = None
        if not isinstance(encoded, str):
            report.error(f"{filename}:{line_number} response body is not base64 text")
        else:
            try:
                body = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                report.error(f"{filename}:{line_number} response body base64 is invalid")
        if body is not None:
            if record.get("http_response_bytes") != len(body):
                report.error(f"{filename}:{line_number} response byte count mismatch")
            if record.get("http_response_sha256") != hashlib.sha256(body).hexdigest():
                report.error(f"{filename}:{line_number} response SHA-256 mismatch")
            try:
                decoded_envelope = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                report.error(f"{filename}:{line_number} response body is not JSON")
            else:
                if decoded_envelope != record.get("envelope"):
                    report.error(f"{filename}:{line_number} envelope differs from body")

        envelope = record.get("envelope")
        raw_output = record.get("raw_output")
        if not isinstance(envelope, dict):
            report.error(f"{filename}:{line_number} envelope must be an object")
        if not isinstance(raw_output, str):
            report.error(f"{filename}:{line_number} raw_output must be a string")
        provider = record.get("provider")
        if provider == "vllm" and isinstance(envelope, dict):
            try:
                content = envelope["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content = None
            if content != raw_output:
                report.error(f"{filename}:{line_number} vLLM content mismatch")
            if not isinstance(record.get("finish_reason"), str):
                report.error(f"{filename}:{line_number} vLLM finish_reason missing")
            if not isinstance(record.get("usage"), dict):
                report.error(f"{filename}:{line_number} vLLM usage missing")
        elif provider == "ollama" and isinstance(envelope, dict):
            try:
                content = envelope["message"]["content"]
            except (KeyError, TypeError):
                content = None
            if content != raw_output:
                report.error(f"{filename}:{line_number} Ollama content mismatch")
        elif provider not in {"vllm", "ollama"}:
            report.error(f"{filename}:{line_number} provider is invalid")
        if record.get("usage") is not None and not isinstance(
            record.get("usage"), dict
        ):
            report.error(f"{filename}:{line_number} usage must be object or null")
        expected_label = labels.get(agent_id)
        if expected_label is not None and record.get("model") != expected_label[1]:
            report.error(f"{filename}:{line_number} model differs from config")
        for optional_identity in ("endpoint_id", "device_slot"):
            if record.get(optional_identity) is not None and not isinstance(
                record.get(optional_identity), str
            ):
                report.error(
                    f"{filename}:{line_number} {optional_identity} must be string or null"
                )

    recovered_retry_count = 0
    for request_key, request_attempts in attempts_by_key.items():
        generations = [record.get("generation_attempt") for _, record in request_attempts]
        expected_generations = [1]
        if legacy_transport and generations == [1, 2]:
            expected_generations = [1, 2]
            recovered_retry_count += 1
            first = request_attempts[0][1]
            second = request_attempts[1][1]
            if first.get("parse_status") != "invalid":
                report.error(f"{filename} legacy retry did not follow an invalid parse")
            if second.get("parse_status") != "valid":
                report.error(f"{filename} legacy retry did not recover")
        if generations != expected_generations:
            report.error(
                f"{filename} generation sequence mismatch for request {request_key}"
            )
    if legacy_transport and recovered_retry_count != meta.get("generation_retries"):
        report.error(f"{filename} retry rows differ from generation_retries")

    scenario = config.get("scenario")
    communication_none = (
        isinstance(scenario, dict)
        and scenario.get("communication_mode") == "communication_none"
    )
    expected_keys = [
        (step, phase_order[phase], agent_id)
        for step in range(1, expected_steps + 1)
        for phase in (("phase3",) if communication_none else ("phase1", "phase3"))
        for agent_id in range(expected_agents)
    ]
    if observed_keys != expected_keys:
        report.error(
            f"{filename} request coverage/order mismatch: "
            f"expected {len(expected_keys)}, got {len(observed_keys)}"
        )
    if len(records) != meta.get("http_attempts"):
        report.error(
            f"{filename} row count differs from http_attempts"
        )


def _check_termination_record(
    records: Sequence[Record],
    meta: Dict[str, Any],
    report: ValidationReport,
) -> None:
    filename = "termination.jsonl"
    if len(records) != 1:
        report.error(f"{filename} must contain exactly one record")
        return
    line_number, record = records[0]
    required = {
        "schema_version",
        "event_id",
        "run_id",
        "status",
        "aborted",
        "reason",
        "exception_type",
        "failure_step",
        "failure_phase",
        "failure_agent_id",
        "completed_steps",
        "end_time_utc",
    }
    if set(record) != required:
        report.error(f"{filename}:{line_number} fields differ from schema 2.0")
    expected = {
        "schema_version": "1.0.0",
        "event_id": f"{meta.get('run_id')}:termination",
        "run_id": meta.get("run_id"),
        "status": meta.get("status"),
        "aborted": meta.get("aborted"),
        "reason": meta.get("abort_reason"),
        "exception_type": meta.get("failure_exception_type"),
        "failure_step": meta.get("failure_step"),
        "failure_phase": meta.get("failure_phase"),
        "failure_agent_id": meta.get("failure_agent_id"),
        "completed_steps": meta.get("completed_steps"),
        "end_time_utc": meta.get("end_time_utc"),
    }
    if record != expected:
        report.error(f"{filename}:{line_number} differs from terminal metadata")


def _check_counters_and_thresholds(
    meta: Dict[str, Any],
    config: Dict[str, Any],
    report: ValidationReport,
) -> None:
    thresholds = meta.get("failure_thresholds")
    if not isinstance(thresholds, dict):
        report.error("failure_thresholds must be an object")
        return
    for counter in FAILURE_COUNTERS:
        threshold = thresholds.get(counter)
        if not _is_nonnegative_int(threshold):
            report.error(
                f"failure_thresholds.{counter} "
                "must be a non-negative integer"
            )
            continue
        value = meta.get(counter)
        if _is_nonnegative_int(value) and value > threshold:
            report.error(
                f"{counter} exceeds threshold: {value} > {threshold}"
            )

    simulation = config.get("simulation", {})
    configured = (
        simulation.get("failure_thresholds", {})
        if isinstance(simulation, dict)
        else {}
    )
    if not isinstance(configured, dict):
        report.error(
            "config.simulation.failure_thresholds must be an object"
        )
        configured = {}
    unknown_thresholds = set(configured) - set(FAILURE_COUNTERS)
    if unknown_thresholds:
        report.error(
            "config.simulation.failure_thresholds has unknown keys: "
            + ", ".join(sorted(str(key) for key in unknown_thresholds))
        )
    expected_thresholds = {
        counter: configured.get(counter, 0)
        for counter in FAILURE_COUNTERS
    }
    if thresholds != expected_thresholds:
        report.error(
            "failure_thresholds differ from the saved "
            "pre-run config/defaults"
        )

    logical = meta.get("logical_llm_calls")
    retries = meta.get("generation_retries")
    transport = meta.get("transport_failures")
    attempts = meta.get("http_attempts")
    syntax_attempts = meta.get("syntax_parse_attempt_failures")
    syntax_failures = meta.get("syntax_parse_failures")
    legacy_transport = (
        config.get("simulation", {}).get("transport_behavior_version")
        == LEGACY_TRANSPORT_BEHAVIOR_VERSION
    )
    if all(
        _is_nonnegative_int(value)
        for value in (logical, retries, transport, attempts)
    ):
        if meta.get("log_schema_version") == OBSERVABILITY_LOG_SCHEMA_VERSION:
            expected_attempts = logical + (retries if legacy_transport else 0)
        else:
            expected_attempts = logical + retries + transport
        if attempts != expected_attempts:
            report.error(
                "HTTP counter inconsistency for the current Ollama client: "
                f"http_attempts={attempts}, expected {expected_attempts}"
            )
    if all(
        _is_nonnegative_int(value)
        for value in (syntax_attempts, retries, syntax_failures)
    ):
        expected_syntax_attempts = (
            syntax_failures
            if (
                meta.get("log_schema_version") == OBSERVABILITY_LOG_SCHEMA_VERSION
                and not legacy_transport
            )
            else retries + syntax_failures
        )
        if syntax_attempts != expected_syntax_attempts:
            report.error(
                "syntax-attempt counter inconsistency: "
                f"expected {expected_syntax_attempts}, got {syntax_attempts}"
            )

    expected_steps = meta.get("expected_steps")
    expected_agents = meta.get("expected_agents")
    if (
        _is_nonnegative_int(logical)
        and _is_nonnegative_int(expected_steps)
        and _is_nonnegative_int(expected_agents)
    ):
        scenario = config.get("scenario")
        communication_none = (
            isinstance(scenario, dict)
            and scenario.get("communication_mode") == "communication_none"
        )
        expected_calls = (
            (1 if communication_none else 2)
            * expected_steps
            * expected_agents
        )
        if logical != expected_calls:
            report.error(
                f"logical_llm_calls mismatch: "
                f"expected {expected_calls}, got {logical}"
            )

    if meta.get("total_llm_calls") != logical:
        report.error(
            "total_llm_calls alias differs from logical_llm_calls"
        )
    if meta.get("parse_errors") != syntax_failures:
        report.error(
            "parse_errors alias differs from syntax_parse_failures"
        )
    if (
        _is_nonnegative_int(logical)
        and _is_nonnegative_int(syntax_failures)
    ):
        expected_rate = syntax_failures / logical if logical else 0.0
        rate = meta.get("parse_error_rate")
        if (
            not isinstance(rate, (int, float))
            or isinstance(rate, bool)
            or not math.isfinite(rate)
        ):
            report.error("parse_error_rate must be a finite number")
        elif not math.isclose(
            float(rate),
            expected_rate,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            report.error(
                "parse_error_rate does not match "
                "syntax_parse_failures/logical_llm_calls"
            )


def _check_disaster_records(
    records: Dict[str, Sequence[Record]],
    meta: Dict[str, Any],
    config: Dict[str, Any],
    labels: Dict[int, Tuple[str, str]],
    report: ValidationReport,
) -> None:
    """Validate schema 1.2 world state and warning events mechanically."""
    simulation = config.get("simulation", {})
    try:
        scenario = parse_disaster_scenario(
            config.get("scenario"),
            half_space_size=simulation.get("half_space_size"),
            duration=meta["expected_steps"],
            total_agents=meta["expected_agents"],
        )
    except Exception as error:
        report.error(
            "config.scenario cannot be validated: "
            f"{type(error).__name__}: {error}"
        )
        return

    run_id = meta.get("run_id")
    expected_world = [{
        "event_id": f"{run_id}:scenario_initialized:000000",
        "event_type": "scenario_initialized",
        "step": 0,
        "scenario_type": "disaster_v1",
        "hazard_id": scenario.hazard_id,
        "communication_mode": scenario.communication_mode,
    }]
    expected_world.extend({
        "event_id": f"{run_id}:hazard_state:{step:06d}",
        "event_type": "hazard_state",
        "step": step,
        "hazard_id": scenario.hazard_id,
        "rectangles": [
            rectangle.as_dict()
            for rectangle in scenario.active_hazard_rectangles(step)
        ],
    } for step in range(1, meta["expected_steps"] + 1))
    actual_world = [record for _, record in records["world_events.jsonl"]]
    if actual_world != expected_world:
        report.error(
            "world_events.jsonl differs from the prospective scenario schedule"
        )

    position_rows = records["positions.jsonl"]
    expected_position_keys = {
        (0, "initial", agent_id)
        for agent_id in range(meta["expected_agents"])
    } | {
        (step, "post_movement", agent_id)
        for step in range(1, meta["expected_steps"] + 1)
        for agent_id in range(meta["expected_agents"])
    }
    actual_position_keys = set()
    position_by_key: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
    position_fields = {
        "event_id", "step", "phase", "agent_id", "bloc", "model",
        "position", "hazardous", "refuge_id", "shortest_refuge_distance",
    }
    for line_number, row in position_rows:
        if set(row) != position_fields:
            report.error(
                f"positions.jsonl:{line_number} fields differ from schema 1.2"
            )
        step, phase, agent_id = row.get("step"), row.get("phase"), row.get("agent_id")
        if not (
            _is_int(step)
            and phase in {"initial", "post_movement"}
            and _is_int(agent_id)
        ):
            report.error(
                f"positions.jsonl:{line_number} has invalid step, phase, or agent_id"
            )
            continue
        key = (step, phase, agent_id)
        actual_position_keys.add(key)
        if key in position_by_key:
            report.error(f"positions.jsonl:{line_number} duplicates key {key!r}")
        position_by_key[key] = row
        if key not in expected_position_keys:
            report.error(f"positions.jsonl:{line_number} has invalid key {key!r}")
            continue
        expected_event_id = (
            f"{run_id}:position:{step:06d}:{phase}:agent-{agent_id:06d}"
        )
        if row.get("event_id") != expected_event_id:
            report.error(f"positions.jsonl:{line_number} has invalid event_id")
        if (row.get("bloc"), row.get("model")) != labels.get(agent_id):
            report.error(f"positions.jsonl:{line_number} bloc/model mismatch")
        position = row.get("position")
        if not (
            isinstance(position, list) and len(position) == 2
            and all(_is_int(value) for value in position)
        ):
            report.error(f"positions.jsonl:{line_number} has invalid position")
            continue
        x, y = position
        half_size = simulation.get("half_space_size")
        if not (_is_int(half_size) and -half_size <= x <= half_size and -half_size <= y <= half_size):
            report.error(f"positions.jsonl:{line_number} is outside the world")
        refuge = scenario.refuge_for(x, y)
        expected_refuge_id = refuge.refuge_id if refuge else None
        if row.get("hazardous") != scenario.is_hazardous(step, x, y):
            report.error(f"positions.jsonl:{line_number} hazardous flag mismatch")
        if row.get("refuge_id") != expected_refuge_id:
            report.error(f"positions.jsonl:{line_number} refuge_id mismatch")
        if row.get("shortest_refuge_distance") != scenario.shortest_refuge_distance(x, y):
            report.error(
                f"positions.jsonl:{line_number} shortest refuge distance mismatch"
            )
    if actual_position_keys != expected_position_keys:
        report.error("positions.jsonl does not cover every initial/post-movement key")
    if len(position_rows) != len(expected_position_keys):
        report.error(
            "positions.jsonl row count mismatch: "
            f"expected {len(expected_position_keys)}, got {len(position_rows)}"
        )

    memory_by_key = {
        (row.get("step"), row.get("agent_id")): row
        for _, row in records["memory_reasoning.jsonl"]
    }
    half_size = simulation.get("half_space_size")
    for step in range(1, meta["expected_steps"] + 1):
        for agent_id in range(meta["expected_agents"]):
            before = position_by_key.get(
                (0, "initial", agent_id)
                if step == 1
                else (step - 1, "post_movement", agent_id)
            )
            after = position_by_key.get((step, "post_movement", agent_id))
            decision = memory_by_key.get((step, agent_id))
            if not all(isinstance(item, dict) for item in (before, after, decision)):
                continue
            if decision.get("position") != before.get("position"):
                report.error(
                    f"step {step} agent {agent_id} Phase 3 position breaks position chain"
                )
                continue
            x, y = before["position"]
            if decision.get("action") == "move" and decision.get("direction"):
                direction = decision["direction"]
                x += (direction == "right") - (direction == "left")
                y += (direction == "up") - (direction == "down")
            expected_after = [
                max(-half_size, min(half_size, x)),
                max(-half_size, min(half_size, y)),
            ]
            if expected_after != after.get("position"):
                report.error(
                    f"step {step} agent {agent_id} post-movement position mismatch"
                )

    warning = scenario.official_warning
    expected_warning = [{
        "event_id": f"{run_id}:warning_issued:{warning.issue_step:06d}",
        "event_type": "warning_issued",
        "step": warning.issue_step,
        "warning_id": warning.warning_id,
        "source_type": "official",
        "recipient_ids": list(warning.initial_recipient_ids),
        "payload": scenario.warning_payload(),
        "facts": scenario.warning_facts(),
    }]
    if scenario.communication_mode != "communication_none":
        expected_warning.extend({
            "event_id": (
                f"{run_id}:warning_exposure:{warning.issue_step:06d}:"
                f"official:{recipient_id:06d}"
            ),
            "event_type": "warning_exposure",
            "step": warning.issue_step,
            "warning_id": warning.warning_id,
            "recipient_id": recipient_id,
            "source_type": "official",
            "sender_id": None,
        } for recipient_id in warning.initial_recipient_ids)
        for _, message in records["messages.jsonl"]:
            step = message.get("step")
            sender_id = message.get("sender_id")
            receiver_ids = message.get("receiver_ids")
            if (
                contains_warning_identifier(message.get("message"), warning.warning_id)
                and _is_int(step)
                and _is_int(sender_id)
                and isinstance(receiver_ids, list)
                and all(_is_int(recipient_id) for recipient_id in receiver_ids)
            ):
                expected_warning.extend({
                    "event_id": (
                        f"{run_id}:warning_exposure:{step:06d}:relay:"
                        f"{sender_id:06d}:{recipient_id:06d}"
                    ),
                    "event_type": "warning_exposure",
                    "step": step,
                    "warning_id": warning.warning_id,
                    "recipient_id": recipient_id,
                    "source_type": "agent_relay",
                    "sender_id": sender_id,
                } for recipient_id in receiver_ids)
    actual_warning = [row for _, row in records["warning_events.jsonl"]]
    if actual_warning != expected_warning:
        report.error(
            "warning_events.jsonl differs from official issue/exposure and exact-ID relays"
        )

    event_ids = []
    for filename in ("world_events.jsonl", "positions.jsonl", "warning_events.jsonl"):
        for line_number, row in records[filename]:
            event_id = row.get("event_id")
            if not _is_nonempty_string(event_id):
                report.error(f"{filename}:{line_number} event_id must be non-empty")
            else:
                event_ids.append(event_id)
    if len(event_ids) != len(set(event_ids)):
        report.error("schema 1.2 event_id values are not globally unique")


def _check_jsonl_and_counts(
    run_dir: Path,
    meta: Dict[str, Any],
    config: Dict[str, Any],
    report: ValidationReport,
) -> None:
    expected_steps = meta.get("expected_steps")
    expected_agents = meta.get("expected_agents")
    if (
        not _is_nonnegative_int(expected_steps)
        or not _is_nonnegative_int(expected_agents)
    ):
        report.error(
            "cannot validate JSONL counts without valid "
            "expected_steps/expected_agents"
        )
        return

    required_files = raw_jsonl_files_for_schema(
        meta.get("log_schema_version"),
        has_disaster="scenario" in config,
    )
    records = {
        filename: _read_jsonl(run_dir / filename, report)
        for filename in required_files
    }
    labels = _derive_agent_labels(config)
    scenario_config = config.get("scenario")
    communication_none = (
        isinstance(scenario_config, dict)
        and scenario_config.get("communication_mode") == "communication_none"
    )
    try:
        response_contract_version = validate_response_contract_version(
            config.get("simulation", {}).get("response_contract_version")
        )
    except ValueError:
        response_contract_version = LEGACY_RESPONSE_CONTRACT_VERSION
    phase1_keys = _check_primary_records(
        "phase1_raw.jsonl",
        records["phase1_raw.jsonl"],
        expected_steps,
        expected_agents,
        labels,
        meta.get("log_schema_version"),
        response_contract_version,
        report,
        expect_complete=not communication_none,
    )
    memory_keys = _check_primary_records(
        "memory_reasoning.jsonl",
        records["memory_reasoning.jsonl"],
        expected_steps,
        expected_agents,
        labels,
        meta.get("log_schema_version"),
        response_contract_version,
        report,
    )
    _check_messages(
        records["messages.jsonl"],
        records["phase1_raw.jsonl"],
        records["memory_reasoning.jsonl"],
        expected_steps,
        expected_agents,
        labels,
        config,
        report,
    )
    _check_parse_errors(
        records["parse_errors.jsonl"],
        expected_steps,
        expected_agents,
        records["phase1_raw.jsonl"],
        memory_keys,
        meta,
        report,
    )
    if meta.get("log_schema_version") == OBSERVABILITY_LOG_SCHEMA_VERSION:
        _check_attempt_records(
            records["llm_attempts.jsonl"],
            expected_steps,
            expected_agents,
            labels,
            config,
            meta,
            report,
        )
        _check_termination_record(
            records["termination.jsonl"],
            meta,
            report,
        )
    if isinstance(scenario_config, dict):
        _check_disaster_records(records, meta, config, labels, report)

    observed_ids = {
        agent_id for _, agent_id in phase1_keys | memory_keys
    }
    if len(observed_ids) != meta.get("observed_agents"):
        report.error(
            "observed_agents does not match distinct agents "
            "in primary raw logs: "
            f"{meta.get('observed_agents')!r} != {len(observed_ids)}"
        )


def validate_run(
    run_dir: Path | str, strict: bool = False
) -> ValidationReport:
    """Validate run_dir without creating, modifying, or deleting files."""
    path = Path(run_dir)
    report = ValidationReport(run_dir=path, strict=strict)
    if not path.exists():
        report.error(f"run directory does not exist: {path}")
        return report
    if not path.is_dir():
        report.error(f"run path is not a directory: {path}")
        return report
    if path.is_symlink():
        report.error("run directory must not be a symbolic link")

    meta_path = path / "run_meta.json"
    if meta_path.is_symlink():
        report.error("run_meta.json must not be a symbolic link")
    meta = _read_meta(meta_path, report)
    if meta is None:
        return report

    _check_required_meta(meta, report)
    _check_run_identity(path, meta, report)
    config = _check_config(meta, report)
    _check_manifest(path, meta, report)

    if strict and config is not None:
        _check_provenance(meta, report)
        _check_jsonl_and_counts(path, meta, config, report)
        _check_counters_and_thresholds(meta, config, report)
        if meta.get("log_schema_version") == OBSERVABILITY_LOG_SCHEMA_VERSION:
            report.cannot_verify(
                "global event identity for primary logs: schema 2.0 "
                "event IDs cover attempt, termination, and disaster events, "
                "but not every primary Phase/message row"
            )
        else:
            report.cannot_verify(
                "global event identity/duplicate detection: legacy primary "
                "records do not all have event_id"
            )
        if meta.get("log_schema_version") != OBSERVABILITY_LOG_SCHEMA_VERSION:
            report.cannot_verify(
                "HTTP-attempt and transport-failure truth: "
                "legacy schema has counters but no attempt-event log"
            )
            if meta.get("schema_validation_supported") is not True:
                report.cannot_verify(
                    "model-response semantic validity: "
                    "response schema validation is not implemented"
                )
            report.cannot_verify(
                "Phase 3 parse-error completeness: the legacy Phase 3 raw "
                "record does not retain parsed/null status"
            )
        report.cannot_verify(
            "cryptographic authenticity: "
            "raw_manifest hashes are not externally signed"
        )
        report.cannot_verify(
            "operational endpoint address identity: runtime bindings are "
            "deliberately excluded from public artifacts"
        )
    return report


def _print_report(report: ValidationReport) -> None:
    for message in report.errors:
        print(f"FAIL: {message}")
    for message in report.unverifiable:
        print(f"UNVERIFIABLE: {message}")
    if report.valid:
        qualification = (
            "strict checks available under the current schema"
            if report.strict
            else "basic checks"
        )
        print(
            f"PASS: all {qualification} passed for {report.run_dir}"
        )
    else:
        print(
            "FAIL: run integrity validation failed with "
            f"{len(report.errors)} error(s)"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a simulation run directory without modifying it"
        )
    )
    parser.add_argument("run_dir", help="Path to output_<run_id>")
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "validate record schemas, complete coverage, "
            "natural keys, and thresholds"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_run(args.run_dir, strict=args.strict)
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            raise
        print(
            f"ERROR: validator failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 2
    _print_report(report)
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
