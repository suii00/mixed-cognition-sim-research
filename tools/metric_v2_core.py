"""Deterministic exact-expression Metric v2 analysis core."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Sequence

from engine.provenance import collect_git_info
from tools.validate_run import validate_run


METRIC_VERSION = "metric-v2.0.0"
REGISTRY_SCHEMA_VERSION = "candidate-registry-v1.0.0"
DERIVED_SCHEMA_VERSION = "metric-derived-v1.0.0"
NORMALIZATION_ID = "nfkc-casefold-token-sequence-v1"
METRIC_SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "METRIC_V2_SPEC.md"

TOKEN_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_TYPE_ORDER = {
    "innovation": 0,
    "exposure": 1,
    "reuse": 2,
    "second_hop": 3,
}
REQUIRED_DERIVED_FILES = (
    "analysis_meta.json",
    "events.jsonl",
    "receiver_expression_status.jsonl",
    "summary.json",
)
DERIVED_MANIFEST_FILE = "derived_manifest.json"
ALL_DERIVED_FILES = (*REQUIRED_DERIVED_FILES, DERIVED_MANIFEST_FILE)

PublicationHook = Callable[[str, Path], None]


class MetricV2Error(RuntimeError):
    """Base class for expected Metric v2 failures."""


class InputValidationError(MetricV2Error):
    """The run, registry, spec, or output path is not eligible."""


class RegistryValidationError(InputValidationError):
    """The fixed candidate registry is invalid or has the wrong digest."""


class RunEligibilityError(InputValidationError):
    """The raw run does not meet Metric v2 eligibility requirements."""


class DerivedCollisionError(MetricV2Error):
    """The final leaf exists or another process owns its publication."""


class DerivedPublicationError(MetricV2Error):
    """A fully staged derived result could not be verified or published."""


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class RegistryExpression:
    expression_id: str
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRegistry:
    registry_id: str
    sha256: str
    expressions: tuple[RegistryExpression, ...]


@dataclass(frozen=True)
class RawRecord:
    filename: str
    line_number: int
    line_bytes: bytes
    value: Dict[str, Any]

    def reference(self, message: str) -> Dict[str, Any]:
        return {
            "file": self.filename,
            "line_number": self.line_number,
            "record_sha256": sha256_bytes(self.line_bytes),
            "message_sha256": sha256_bytes(message.encode("utf-8")),
        }


@dataclass(frozen=True)
class PreparedAnalysis:
    run_id: str
    files: Dict[str, bytes]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def json_document_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def jsonl_bytes(values: Iterable[Dict[str, Any]]) -> bytes:
    return b"".join(json_document_bytes(value) for value in values)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise InputValidationError(
            f"{field_name} must be a lowercase 64-character SHA-256 digest"
        )
    return value


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.split())


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(TOKEN_RE.findall(normalize_text(text)))


def contains_token_sequence(
    message_tokens: Sequence[str], candidate_tokens: Sequence[str]
) -> bool:
    width = len(candidate_tokens)
    if width == 0 or width > len(message_tokens):
        return False
    return any(
        tuple(message_tokens[index:index + width]) == tuple(candidate_tokens)
        for index in range(len(message_tokens) - width + 1)
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _load_json_object(raw: bytes, label: str) -> Dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputValidationError(f"{label} is not UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateJsonKey, json.JSONDecodeError) as error:
        raise InputValidationError(f"{label} is not valid unambiguous JSON") from error
    if not isinstance(value, dict):
        raise InputValidationError(f"{label} root must be a JSON object")
    return value


def _nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError(f"{field_name} must be a non-empty string")
    return value


def load_candidate_registry(
    registry_path: Path | str,
    expected_sha256: str,
) -> CandidateRegistry:
    expected = _require_sha256(expected_sha256, "registry SHA-256")
    path = Path(registry_path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RegistryValidationError(
            f"candidate registry cannot be read: {type(error).__name__}"
        ) from error
    actual = sha256_bytes(raw)
    if actual != expected:
        raise RegistryValidationError("candidate registry SHA-256 mismatch")
    try:
        value = _load_json_object(raw, "candidate registry")
    except InputValidationError as error:
        raise RegistryValidationError(str(error)) from error

    allowed_top_level = {
        "schema_version",
        "metric_version",
        "registry_id",
        "normalization",
        "discovery_provenance",
        "excluded_expressions",
        "expressions",
    }
    unknown = sorted(set(value) - allowed_top_level)
    if unknown:
        raise RegistryValidationError(
            "candidate registry has unknown top-level fields: "
            + ", ".join(unknown)
        )
    missing = sorted(allowed_top_level - set(value))
    if missing:
        raise RegistryValidationError(
            "candidate registry is missing fields: " + ", ".join(missing)
        )
    if value["schema_version"] != REGISTRY_SCHEMA_VERSION:
        raise RegistryValidationError("candidate registry schema version mismatch")
    if value["metric_version"] != METRIC_VERSION:
        raise RegistryValidationError("candidate registry metric version mismatch")
    if value["normalization"] != NORMALIZATION_ID:
        raise RegistryValidationError("candidate registry normalization mismatch")
    registry_id = _nonempty_string(value["registry_id"], "registry_id")

    provenance = value["discovery_provenance"]
    required_provenance = {
        "purpose",
        "source_run_ids",
        "condition_labels_hidden",
        "model_labels_hidden",
        "receiver_ids_accessed",
        "later_target_outputs_accessed",
    }
    if not isinstance(provenance, dict):
        raise RegistryValidationError("discovery_provenance must be an object")
    missing_provenance = sorted(required_provenance - set(provenance))
    if missing_provenance:
        raise RegistryValidationError(
            "discovery_provenance is missing fields: "
            + ", ".join(missing_provenance)
        )
    if provenance.get("purpose") != "pilot-only":
        raise RegistryValidationError("discovery purpose must be pilot-only")
    source_run_ids = provenance.get("source_run_ids")
    if (
        not isinstance(source_run_ids, list)
        or not source_run_ids
        or any(not isinstance(item, str) or not item.strip() for item in source_run_ids)
    ):
        raise RegistryValidationError(
            "discovery source_run_ids must contain non-empty strings"
        )
    expected_flags = {
        "condition_labels_hidden": True,
        "model_labels_hidden": True,
        "receiver_ids_accessed": False,
        "later_target_outputs_accessed": False,
    }
    for field_name, expected_value in expected_flags.items():
        if provenance.get(field_name) is not expected_value:
            raise RegistryValidationError(
                f"unsafe discovery provenance flag: {field_name}"
            )

    excluded_values = value["excluded_expressions"]
    if not isinstance(excluded_values, list):
        raise RegistryValidationError("excluded_expressions must be an array")
    excluded_tokens: set[tuple[str, ...]] = set()
    for index, entry in enumerate(excluded_values):
        if not isinstance(entry, dict):
            raise RegistryValidationError(
                f"excluded_expressions[{index}] must be an object"
            )
        text = _nonempty_string(
            entry.get("text"), f"excluded_expressions[{index}].text"
        )
        _nonempty_string(
            entry.get("reason"), f"excluded_expressions[{index}].reason"
        )
        tokens = tokenize(text)
        if not tokens:
            raise RegistryValidationError(
                f"excluded_expressions[{index}] has no normalized tokens"
            )
        if tokens in excluded_tokens:
            raise RegistryValidationError("duplicate normalized excluded expression")
        excluded_tokens.add(tokens)

    expression_values = value["expressions"]
    if not isinstance(expression_values, list) or not expression_values:
        raise RegistryValidationError("expressions must be a non-empty array")
    expression_ids: set[str] = set()
    normalized_expressions: set[tuple[str, ...]] = set()
    expressions = []
    for index, entry in enumerate(expression_values):
        if not isinstance(entry, dict):
            raise RegistryValidationError(f"expressions[{index}] must be an object")
        expression_id = _nonempty_string(
            entry.get("expression_id"), f"expressions[{index}].expression_id"
        )
        text = _nonempty_string(entry.get("text"), f"expressions[{index}].text")
        if expression_id in expression_ids:
            raise RegistryValidationError("duplicate expression_id")
        tokens = tokenize(text)
        if not tokens:
            raise RegistryValidationError(
                f"expressions[{index}] has no normalized tokens"
            )
        if tokens in normalized_expressions:
            raise RegistryValidationError("duplicate normalized expression")
        if tokens in excluded_tokens:
            raise RegistryValidationError(
                "candidate expression conflicts with excluded expression"
            )
        expression_ids.add(expression_id)
        normalized_expressions.add(tokens)
        expressions.append(RegistryExpression(expression_id, text, tokens))

    expressions.sort(key=lambda item: item.expression_id)
    return CandidateRegistry(registry_id, actual, tuple(expressions))


def _read_raw_jsonl(path: Path) -> list[RawRecord]:
    records = []
    try:
        with path.open("rb") as handle:
            for line_number, line_bytes in enumerate(handle, start=1):
                if not line_bytes.endswith(b"\n"):
                    raise RunEligibilityError(
                        f"{path.name}:{line_number} lacks a JSONL newline"
                    )
                try:
                    value = json.loads(line_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RunEligibilityError(
                        f"{path.name}:{line_number} is invalid UTF-8 JSON"
                    ) from error
                if not isinstance(value, dict):
                    raise RunEligibilityError(
                        f"{path.name}:{line_number} is not a JSON object"
                    )
                records.append(
                    RawRecord(path.name, line_number, line_bytes, value)
                )
    except OSError as error:
        raise RunEligibilityError(
            f"raw file cannot be read: {path.name}: {type(error).__name__}"
        ) from error
    return records


def _load_run_meta(run_dir: Path) -> Dict[str, Any]:
    try:
        raw = (run_dir / "run_meta.json").read_bytes()
    except OSError as error:
        raise RunEligibilityError("run_meta.json cannot be read") from error
    try:
        return _load_json_object(raw, "run_meta.json")
    except InputValidationError as error:
        raise RunEligibilityError(str(error)) from error


def _agent_labels(meta: Dict[str, Any]) -> Dict[int, Dict[str, str]]:
    config = meta.get("config")
    blocs = config.get("blocs") if isinstance(config, dict) else None
    if not isinstance(blocs, list):
        raise RunEligibilityError("run config lacks bloc mapping")
    labels: Dict[int, Dict[str, str]] = {}
    agent_id = 0
    for bloc in blocs:
        if not isinstance(bloc, dict):
            raise RunEligibilityError("run config has invalid bloc mapping")
        name = bloc.get("name")
        model = bloc.get("model")
        count = bloc.get("num_agents")
        if (
            not isinstance(name, str)
            or not isinstance(model, str)
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
        ):
            raise RunEligibilityError("run config has invalid agent labels")
        for _ in range(count):
            labels[agent_id] = {"bloc": name, "model": model}
            agent_id += 1
    if agent_id != meta.get("expected_agents"):
        raise RunEligibilityError("run agent label count mismatch")
    return labels


def _matching_expression_ids(
    message: str, registry: CandidateRegistry
) -> list[str]:
    message_tokens = tokenize(message)
    return [
        expression.expression_id
        for expression in registry.expressions
        if contains_token_sequence(message_tokens, expression.tokens)
    ]


def _event_id(identity: Dict[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(identity))


def _add_event_id(
    event: Dict[str, Any], identity: Dict[str, Any]
) -> Dict[str, Any]:
    result = dict(event)
    result["event_id"] = _event_id(identity)
    return result


def _event_sort_key(event: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        EVENT_TYPE_ORDER[event["event_type"]],
        event["expression_id"],
        event.get("step", -1),
        event.get("sender_id", event.get("origin_agent_id", -1)),
        event.get("receiver_id", event.get("agent_id", -1)),
        event.get("event_id", ""),
    )


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _build_metric_content(
    run_dir: Path,
    meta: Dict[str, Any],
    registry: CandidateRegistry,
    metric_spec_sha256: str,
    strict_unverifiable: Sequence[str],
    analysis_source: Dict[str, Any],
) -> Dict[str, bytes]:
    run_id = meta["run_id"]
    labels = _agent_labels(meta)
    phase1_records = _read_raw_jsonl(run_dir / "phase1_raw.jsonl")
    message_records = _read_raw_jsonl(run_dir / "messages.jsonl")

    phase1_by_key: Dict[tuple[int, int], RawRecord] = {}
    self_uses: Dict[str, list[Dict[str, Any]]] = {
        expression.expression_id: [] for expression in registry.expressions
    }
    for raw_record in phase1_records:
        record = raw_record.value
        step = record.get("step")
        agent_id = record.get("agent_id")
        if not isinstance(step, int) or not isinstance(agent_id, int):
            raise RunEligibilityError("Phase 1 record lacks integer identity")
        key = (step, agent_id)
        if key in phase1_by_key:
            raise RunEligibilityError("duplicate Phase 1 identity")
        phase1_by_key[key] = raw_record
        parsed = record.get("parsed")
        message = parsed.get("message") if isinstance(parsed, dict) else None
        if not isinstance(message, str):
            continue
        for expression_id in _matching_expression_ids(message, registry):
            self_uses[expression_id].append({
                "step": step,
                "agent_id": agent_id,
                "agent_bloc": labels[agent_id]["bloc"],
                "raw_reference": raw_record.reference(message),
            })

    innovation_events = []
    innovation_by_expression: Dict[str, Dict[str, Any]] = {}
    unique_origin_by_expression: Dict[str, int] = {}
    for expression in registry.expressions:
        uses = sorted(
            self_uses[expression.expression_id],
            key=lambda item: (
                item["step"],
                item["agent_id"],
                item["raw_reference"]["line_number"],
            ),
        )
        self_uses[expression.expression_id] = uses
        if not uses:
            continue
        first_step = uses[0]["step"]
        origin_uses = [item for item in uses if item["step"] == first_step]
        origin_agent_ids = sorted({item["agent_id"] for item in origin_uses})
        origin_type = (
            "unique_origin" if len(origin_agent_ids) == 1
            else "simultaneous_origin"
        )
        event = _add_event_id(
            {
                "event_type": "innovation",
                "run_id": run_id,
                "expression_id": expression.expression_id,
                "step": first_step,
                "origin_type": origin_type,
                "origin_agent_ids": origin_agent_ids,
                "origin_agent_blocs": [labels[item]["bloc"] for item in origin_agent_ids],
                "origin_self_uses": origin_uses,
            },
            {
                "event_type": "innovation",
                "run_id": run_id,
                "expression_id": expression.expression_id,
                "step": first_step,
                "origin_agent_ids": origin_agent_ids,
                "origin_lines": [
                    item["raw_reference"]["line_number"] for item in origin_uses
                ],
            },
        )
        innovation_events.append(event)
        innovation_by_expression[expression.expression_id] = event
        if origin_type == "unique_origin":
            unique_origin_by_expression[expression.expression_id] = origin_agent_ids[0]

    exposure_events = []
    for delivery_record in message_records:
        delivery = delivery_record.value
        step = delivery.get("step")
        sender_id = delivery.get("sender_id")
        message = delivery.get("message")
        receiver_ids = delivery.get("receiver_ids")
        if (
            not isinstance(step, int)
            or not isinstance(sender_id, int)
            or not isinstance(message, str)
            or not isinstance(receiver_ids, list)
        ):
            raise RunEligibilityError("delivery record has invalid metric fields")
        phase1_record = phase1_by_key.get((step, sender_id))
        parsed = phase1_record.value.get("parsed") if phase1_record else None
        phase1_message = parsed.get("message") if isinstance(parsed, dict) else None
        if not isinstance(phase1_message, str) or phase1_message != message:
            raise RunEligibilityError(
                "delivery message has no exact matching Phase 1 message"
            )
        expression_ids = _matching_expression_ids(message, registry)
        for expression_id in expression_ids:
            for receiver_id in receiver_ids:
                if not isinstance(receiver_id, int) or receiver_id not in labels:
                    raise RunEligibilityError("delivery has invalid receiver ID")
                relation = (
                    "within_bloc"
                    if labels[sender_id]["bloc"] == labels[receiver_id]["bloc"]
                    else "cross_bloc"
                )
                exposure_events.append(_add_event_id(
                    {
                        "event_type": "exposure",
                        "run_id": run_id,
                        "expression_id": expression_id,
                        "step": step,
                        "sender_id": sender_id,
                        "sender_bloc": labels[sender_id]["bloc"],
                        "receiver_id": receiver_id,
                        "receiver_bloc": labels[receiver_id]["bloc"],
                        "within_or_cross_bloc": relation,
                        "sender_phase1_raw_reference": phase1_record.reference(message),
                        "delivery_raw_reference": delivery_record.reference(message),
                    },
                    {
                        "event_type": "exposure",
                        "run_id": run_id,
                        "expression_id": expression_id,
                        "step": step,
                        "sender_id": sender_id,
                        "receiver_id": receiver_id,
                        "delivery_line": delivery_record.line_number,
                    },
                ))

    exposure_events.sort(key=_event_sort_key)
    exposures_by_pair: Dict[tuple[str, int], list[Dict[str, Any]]] = {}
    for event in exposure_events:
        key = (event["expression_id"], event["receiver_id"])
        exposures_by_pair.setdefault(key, []).append(event)

    status_records = []
    reuse_events = []
    reuse_event_by_pair: Dict[tuple[str, int], Dict[str, Any]] = {}
    for (expression_id, receiver_id), pair_exposures in sorted(
        exposures_by_pair.items()
    ):
        pair_exposures.sort(key=_event_sort_key)
        first_exposure_step = pair_exposures[0]["step"]
        first_exposures = [
            item for item in pair_exposures
            if item["step"] == first_exposure_step
        ]
        first_sender_ids = sorted({item["sender_id"] for item in first_exposures})
        relation_values = {
            item["within_or_cross_bloc"] for item in first_exposures
        }
        first_relation = (
            next(iter(relation_values))
            if len(relation_values) == 1 else "mixed_ambiguous"
        )
        receiver_uses = [
            item for item in self_uses[expression_id]
            if item["agent_id"] == receiver_id
        ]
        prior_uses = [
            item for item in receiver_uses
            if item["step"] <= first_exposure_step
        ]
        prior_step = min(
            (item["step"] for item in prior_uses),
            default=None,
        )
        later_uses = [
            item for item in receiver_uses
            if item["step"] > first_exposure_step
        ]
        reuse_use = min(
            later_uses,
            key=lambda item: (
                item["step"], item["raw_reference"]["line_number"]
            ),
            default=None,
        )
        if prior_step is not None:
            status = "excluded_prior_or_same_step_use"
            reuse_step = None
            latency_steps = None
            censor_step = None
            exposure_count_before_reuse = None
            reuse_event_id = None
        elif reuse_use is not None:
            status = "eligible_reused"
            reuse_step = reuse_use["step"]
            latency_steps = reuse_step - first_exposure_step
            censor_step = None
            exposure_count_before_reuse = sum(
                item["step"] < reuse_step for item in pair_exposures
            )
            reuse_event = _add_event_id(
                {
                    "event_type": "reuse",
                    "run_id": run_id,
                    "expression_id": expression_id,
                    "step": reuse_step,
                    "agent_id": receiver_id,
                    "agent_bloc": labels[receiver_id]["bloc"],
                    "first_exposure_step": first_exposure_step,
                    "first_exposure_event_ids": sorted(
                        item["event_id"] for item in first_exposures
                    ),
                    "self_use_raw_reference": reuse_use["raw_reference"],
                },
                {
                    "event_type": "reuse",
                    "run_id": run_id,
                    "expression_id": expression_id,
                    "agent_id": receiver_id,
                    "step": reuse_step,
                    "self_use_line": reuse_use["raw_reference"]["line_number"],
                },
            )
            reuse_events.append(reuse_event)
            reuse_event_by_pair[(expression_id, receiver_id)] = reuse_event
            reuse_event_id = reuse_event["event_id"]
        else:
            status = "eligible_no_reuse"
            reuse_step = None
            latency_steps = None
            censor_step = meta["expected_steps"]
            exposure_count_before_reuse = len(pair_exposures)
            reuse_event_id = None

        status_records.append({
            "run_id": run_id,
            "expression_id": expression_id,
            "receiver_id": receiver_id,
            "receiver_bloc": labels[receiver_id]["bloc"],
            "first_exposure_step": first_exposure_step,
            "first_exposure_event_ids": sorted(
                item["event_id"] for item in first_exposures
            ),
            "first_exposure_sender_ids": first_sender_ids,
            "first_exposure_relation": first_relation,
            "total_exposure_count": len(pair_exposures),
            "exposure_count_before_reuse": exposure_count_before_reuse,
            "prior_self_use_step": prior_step,
            "reuse_step": reuse_step,
            "reuse_event_id": reuse_event_id,
            "latency_steps": latency_steps,
            "censor_step": censor_step,
            "status": status,
        })

    status_by_pair = {
        (item["expression_id"], item["receiver_id"]): item
        for item in status_records
    }
    exposure_by_id = {item["event_id"]: item for item in exposure_events}
    second_hop_events = []
    for first_pair, first_status in sorted(status_by_pair.items()):
        expression_id, relay_id = first_pair
        source_id = unique_origin_by_expression.get(expression_id)
        if (
            source_id is None
            or first_status["status"] != "eligible_reused"
            or first_status["first_exposure_sender_ids"] != [source_id]
        ):
            continue
        first_reuse = reuse_event_by_pair[first_pair]
        for second_pair, second_status in sorted(status_by_pair.items()):
            second_expression_id, target_id = second_pair
            if second_expression_id != expression_id:
                continue
            if len({source_id, relay_id, target_id}) != 3:
                continue
            if (
                second_status["status"] != "eligible_reused"
                or second_status["first_exposure_sender_ids"] != [relay_id]
                or second_status["first_exposure_step"] != first_reuse["step"]
            ):
                continue
            parent_exposures = [
                exposure_by_id[event_id]
                for event_id in second_status["first_exposure_event_ids"]
            ]
            if (
                len(parent_exposures) != 1
                or parent_exposures[0]["sender_id"] != relay_id
                or parent_exposures[0]["receiver_id"] != target_id
            ):
                continue
            second_reuse = reuse_event_by_pair[second_pair]
            innovation = innovation_by_expression[expression_id]
            second_exposure = parent_exposures[0]
            references = {
                "innovation_event_id": innovation["event_id"],
                "first_hop_reuse_event_id": first_reuse["event_id"],
                "second_hop_exposure_event_id": second_exposure["event_id"],
                "second_hop_reuse_event_id": second_reuse["event_id"],
            }
            second_hop_events.append(_add_event_id(
                {
                    "event_type": "second_hop",
                    "run_id": run_id,
                    "expression_id": expression_id,
                    "step": second_reuse["step"],
                    "source_agent_id": source_id,
                    "relay_agent_id": relay_id,
                    "target_agent_id": target_id,
                    "first_hop_reuse_step": first_reuse["step"],
                    "second_hop_exposure_step": second_exposure["step"],
                    "second_hop_reuse_step": second_reuse["step"],
                    "referenced_event_ids": references,
                },
                {
                    "event_type": "second_hop",
                    "run_id": run_id,
                    "expression_id": expression_id,
                    "source_agent_id": source_id,
                    "relay_agent_id": relay_id,
                    "target_agent_id": target_id,
                    "referenced_event_ids": references,
                },
            ))

    events = sorted(
        innovation_events + exposure_events + reuse_events + second_hop_events,
        key=_event_sort_key,
    )
    status_records.sort(
        key=lambda item: (item["expression_id"], item["receiver_id"])
    )

    present_count = len(innovation_events)
    eligible = [
        item for item in status_records
        if item["status"] in {"eligible_reused", "eligible_no_reuse"}
    ]
    reused = [item for item in eligible if item["status"] == "eligible_reused"]
    cross_eligible = [
        item for item in eligible
        if item["first_exposure_relation"] == "cross_bloc"
    ]
    cross_reused = [
        item for item in cross_eligible if item["status"] == "eligible_reused"
    ]
    within_eligible = [
        item for item in eligible
        if item["first_exposure_relation"] == "within_bloc"
    ]
    within_reused = [
        item for item in within_eligible if item["status"] == "eligible_reused"
    ]
    summary = {
        "metric_version": METRIC_VERSION,
        "run_id": run_id,
        "registered_expression_count": len(registry.expressions),
        "expression_present_count": present_count,
        "expression_absent_count": len(registry.expressions) - present_count,
        "unique_origin_count": sum(
            item["origin_type"] == "unique_origin" for item in innovation_events
        ),
        "simultaneous_origin_count": sum(
            item["origin_type"] == "simultaneous_origin"
            for item in innovation_events
        ),
        "exposure_event_count": len(exposure_events),
        "unique_exposed_pair_count": len(status_records),
        "excluded_prior_or_same_step_use_count": sum(
            item["status"] == "excluded_prior_or_same_step_use"
            for item in status_records
        ),
        "eligible_pair_count": len(eligible),
        "eligible_reused_pair_count": len(reused),
        "eligible_no_reuse_pair_count": len(eligible) - len(reused),
        "overall_reuse_rate": _rate(len(reused), len(eligible)),
        "cross_bloc_eligible_pair_count": len(cross_eligible),
        "cross_bloc_reused_pair_count": len(cross_reused),
        "cross_bloc_reuse_rate": _rate(len(cross_reused), len(cross_eligible)),
        "within_bloc_eligible_pair_count": len(within_eligible),
        "within_bloc_reused_pair_count": len(within_reused),
        "within_bloc_reuse_rate": _rate(len(within_reused), len(within_eligible)),
        "mixed_ambiguous_pair_count": sum(
            item["first_exposure_relation"] == "mixed_ambiguous"
            for item in status_records
        ),
        "second_hop_chain_count": len(second_hop_events),
    }

    analysis_meta = {
        "schema_version": DERIVED_SCHEMA_VERSION,
        "status": "completed",
        "metric_version": METRIC_VERSION,
        "normalization": NORMALIZATION_ID,
        "metric_spec_sha256": metric_spec_sha256,
        "registry_id": registry.registry_id,
        "registry_sha256": registry.sha256,
        "run_id": run_id,
        "run_source_git_sha": meta.get("git_sha"),
        "run_config_hash": meta.get("config_hash"),
        "run_prompt_hash": meta.get("prompt_hash"),
        "run_protocol_version": meta.get("protocol_version"),
        "run_metric_version": meta.get("metric_version"),
        "run_raw_manifest": meta.get("raw_manifest"),
        "analysis_source_git_sha": analysis_source["git_sha"],
        "analysis_source_dirty": analysis_source["git_dirty"],
        "strict_validator_valid": True,
        "strict_validator_unverifiable": list(strict_unverifiable),
    }

    return {
        "analysis_meta.json": json_document_bytes(analysis_meta),
        "events.jsonl": jsonl_bytes(events),
        "receiver_expression_status.jsonl": jsonl_bytes(status_records),
        "summary.json": json_document_bytes(summary),
    }


def prepare_analysis(
    run_dir: Path | str,
    registry_path: Path | str,
    registry_sha256: str,
    metric_spec_sha256: str,
) -> PreparedAnalysis:
    expected_spec = _require_sha256(metric_spec_sha256, "metric spec SHA-256")
    try:
        actual_spec = sha256_bytes(METRIC_SPEC_PATH.read_bytes())
    except OSError as error:
        raise InputValidationError("Metric v2 specification cannot be read") from error
    if actual_spec != expected_spec:
        raise InputValidationError("Metric v2 specification SHA-256 mismatch")

    registry = load_candidate_registry(registry_path, registry_sha256)
    path = Path(run_dir)
    report = validate_run(path, strict=True)
    if not report.valid:
        details = "; ".join(report.errors[:3])
        raise RunEligibilityError(f"strict run validation failed: {details}")
    meta = _load_run_meta(path)
    if (
        meta.get("status") != "completed"
        or meta.get("aborted") is not False
        or meta.get("metric_version") != METRIC_VERSION
        or meta.get("raw_manifest_status") != "available"
        or not isinstance(meta.get("raw_manifest"), dict)
    ):
        raise RunEligibilityError("run is not eligible for metric-v2.0.0")

    analysis_source = collect_git_info(Path(__file__).resolve().parents[1])
    if (
        analysis_source.get("git_probe_status") != "available"
        or not isinstance(analysis_source.get("git_sha"), str)
        or not isinstance(analysis_source.get("git_dirty"), bool)
    ):
        raise InputValidationError("analysis source Git provenance is unavailable")

    files = _build_metric_content(
        path,
        meta,
        registry,
        actual_spec,
        report.unverifiable,
        analysis_source,
    )
    manifest = {
        "algorithm": "sha256",
        "files": {
            filename: {
                "sha256": sha256_bytes(files[filename]),
                "bytes": len(files[filename]),
                "lines": files[filename].count(b"\n"),
            }
            for filename in REQUIRED_DERIVED_FILES
        },
        "schema_version": DERIVED_SCHEMA_VERSION,
    }
    files["derived_manifest.json"] = json_document_bytes(manifest)
    return PreparedAnalysis(meta["run_id"], files)


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _create_publication_directory(path: Path, label: str) -> None:
    if _path_lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise InputValidationError(f"{label} must be a real directory")
    try:
        path.mkdir(exist_ok=True)
    except OSError as error:
        raise InputValidationError(
            f"{label} cannot be created: {type(error).__name__}"
        ) from error
    if path.is_symlink() or not path.is_dir():
        raise InputValidationError(f"{label} must be a real directory")


def _prepare_publication_layout(
    run_dir: Path | str,
    derived_root: Path | str,
    run_id: str,
) -> tuple[Path, Path, Path, Path]:
    raw_path = Path(run_dir).resolve(strict=True)
    root = Path(derived_root)
    resolved_root = root.resolve(strict=False)
    if _is_within(resolved_root, raw_path):
        raise InputValidationError("derived root may not be inside the raw run")
    if root.exists() and root.is_symlink():
        raise InputValidationError("derived root may not be a symbolic link")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise InputValidationError(
            f"derived root cannot be created: {type(error).__name__}"
        ) from error
    if root.is_symlink() or _is_within(root.resolve(), raw_path):
        raise InputValidationError("derived root resolves inside the raw run")

    version_directory = root / METRIC_VERSION
    if version_directory.exists() and version_directory.is_symlink():
        raise InputValidationError("metric version directory may not be a symlink")
    try:
        version_directory.mkdir(exist_ok=True)
    except OSError as error:
        raise InputValidationError(
            f"metric version directory cannot be created: {type(error).__name__}"
        ) from error
    if version_directory.is_symlink():
        raise InputValidationError("metric version directory may not be a symlink")

    resolved_version = version_directory.resolve(strict=True)
    if resolved_version != root.resolve(strict=True) / METRIC_VERSION:
        raise InputValidationError(
            "metric version directory must remain inside the derived root"
        )

    lock_directory = version_directory / ".locks"
    staging_directory = version_directory / ".staging"
    _create_publication_directory(lock_directory, "publication lock directory")
    _create_publication_directory(staging_directory, "staging directory")
    if lock_directory.resolve(strict=True).parent != resolved_version:
        raise InputValidationError(
            "publication lock directory must remain inside the metric directory"
        )
    if staging_directory.resolve(strict=True).parent != resolved_version:
        raise InputValidationError(
            "staging directory must remain inside the metric directory"
        )
    if os.stat(staging_directory).st_dev != os.stat(version_directory).st_dev:
        raise InputValidationError(
            "staging and final derived outputs must use the same filesystem"
        )

    final_leaf = version_directory / run_id
    lock_path = lock_directory / f"{run_id}.lock"
    return version_directory, staging_directory, lock_path, final_leaf


def _open_lock_file(lock_path: Path):
    if _path_lexists(lock_path) and lock_path.is_symlink():
        raise InputValidationError("publication lock file may not be a symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise InputValidationError(
            f"publication lock file cannot be opened: {type(error).__name__}"
        ) from error
    return os.fdopen(descriptor, "r+b", buffering=0)


@contextmanager
def _publication_lock(lock_path: Path, run_id: str) -> Iterator[None]:
    handle = _open_lock_file(lock_path)
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise DerivedCollisionError(
                    f"derived publication is already in progress for run ID {run_id!r}"
                ) from error
            raise InputValidationError(
                f"publication lock cannot be acquired: {type(error).__name__}"
            ) from error
        locked = True
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor still releases an OS-owned lock.
                pass
        handle.close()


def _create_staging_leaf(staging_directory: Path, run_id: str) -> Path:
    for _ in range(16):
        staging_leaf = staging_directory / f"{run_id}-{uuid.uuid4().hex}"
        try:
            staging_leaf.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        except OSError as error:
            raise DerivedPublicationError(
                f"staging directory cannot be created: {type(error).__name__}"
            ) from error
        return staging_leaf
    raise DerivedPublicationError("a unique staging directory could not be created")


def _write_fsynced(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_manifest_with_checkpoint(
    path: Path,
    content: bytes,
    staging_leaf: Path,
    publication_hook: Optional[PublicationHook],
) -> None:
    midpoint = max(1, len(content) // 2)
    with path.open("xb") as handle:
        handle.write(content[:midpoint])
        handle.flush()
        os.fsync(handle.fileno())
        if publication_hook is not None:
            publication_hook("during_manifest_write", staging_leaf)
        handle.write(content[midpoint:])
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_staging_leaf(staging_leaf: Path, prepared: PreparedAnalysis) -> None:
    actual_names = {item.name for item in staging_leaf.iterdir()}
    if actual_names != set(ALL_DERIVED_FILES):
        raise DerivedPublicationError("staging directory has an invalid file set")
    for filename in ALL_DERIVED_FILES:
        path = staging_leaf / filename
        if path.is_symlink() or not path.is_file():
            raise DerivedPublicationError(
                f"staged derived artifact is not a regular file: {filename}"
            )
        if path.read_bytes() != prepared.files[filename]:
            raise DerivedPublicationError(
                f"staged derived artifact differs from prepared bytes: {filename}"
            )

    try:
        manifest = json.loads(
            (staging_leaf / DERIVED_MANIFEST_FILE).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DerivedPublicationError("staged manifest cannot be decoded") from error
    if manifest.get("algorithm") != "sha256":
        raise DerivedPublicationError("staged manifest algorithm is invalid")
    if manifest.get("schema_version") != DERIVED_SCHEMA_VERSION:
        raise DerivedPublicationError("staged manifest schema version is invalid")
    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != set(REQUIRED_DERIVED_FILES):
        raise DerivedPublicationError("staged manifest file set is invalid")
    for filename in REQUIRED_DERIVED_FILES:
        content = (staging_leaf / filename).read_bytes()
        expected = {
            "sha256": sha256_bytes(content),
            "bytes": len(content),
            "lines": content.count(b"\n"),
        }
        if entries[filename] != expected:
            raise DerivedPublicationError(
                f"staged manifest entry is invalid: {filename}"
            )


def _raise_final_collision(run_id: str) -> None:
    raise DerivedCollisionError(
        f"derived output already exists for run ID {run_id!r}"
    )


def write_prepared_analysis(
    prepared: PreparedAnalysis,
    run_dir: Path | str,
    derived_root: Path | str,
    before_claim: Optional[Callable[[], None]] = None,
    publication_hook: Optional[PublicationHook] = None,
) -> Path:
    (
        version_directory,
        staging_directory,
        lock_path,
        final_leaf,
    ) = _prepare_publication_layout(run_dir, derived_root, prepared.run_id)
    if before_claim is not None:
        before_claim()
    with _publication_lock(lock_path, prepared.run_id):
        if _path_lexists(final_leaf):
            _raise_final_collision(prepared.run_id)
        staging_leaf = _create_staging_leaf(staging_directory, prepared.run_id)
        checkpoints = {
            "analysis_meta.json": "after_analysis_meta_write",
            "events.jsonl": "after_events_write",
            "receiver_expression_status.jsonl": "after_receiver_status_write",
            "summary.json": "after_summary_write",
        }
        for filename in REQUIRED_DERIVED_FILES:
            _write_fsynced(staging_leaf / filename, prepared.files[filename])
            if publication_hook is not None:
                publication_hook(checkpoints[filename], staging_leaf)
        _write_manifest_with_checkpoint(
            staging_leaf / DERIVED_MANIFEST_FILE,
            prepared.files[DERIVED_MANIFEST_FILE],
            staging_leaf,
            publication_hook,
        )
        _verify_staging_leaf(staging_leaf, prepared)
        _fsync_directory(staging_leaf)
        if publication_hook is not None:
            publication_hook(
                "after_manifest_verification_before_publish",
                staging_leaf,
            )
        if _path_lexists(final_leaf):
            _raise_final_collision(prepared.run_id)
        try:
            os.rename(staging_leaf, final_leaf)
        except OSError as error:
            if _path_lexists(final_leaf):
                raise DerivedCollisionError(
                    f"derived output already exists for run ID {prepared.run_id!r}"
                ) from error
            raise DerivedPublicationError(
                f"staged result cannot be published: {type(error).__name__}"
            ) from error
    return final_leaf


def analyze_run(
    run_dir: Path | str,
    registry_path: Path | str,
    registry_sha256: str,
    metric_spec_sha256: str,
    derived_root: Path | str,
    before_claim: Optional[Callable[[], None]] = None,
    publication_hook: Optional[PublicationHook] = None,
) -> Path:
    prepared = prepare_analysis(
        run_dir,
        registry_path,
        registry_sha256,
        metric_spec_sha256,
    )
    return write_prepared_analysis(
        prepared,
        run_dir,
        derived_root,
        before_claim=before_claim,
        publication_hook=publication_hook,
    )
