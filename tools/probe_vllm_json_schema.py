#!/usr/bin/env python3
"""Paired, nonresearch vLLM JSON-Schema/maxLength compatibility probe."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import (
    load_config,
    load_runtime_bindings,
    required_endpoint_ids,
    validate_runtime_bindings,
)
from engine.provenance import atomic_write_json


PROBE_SCHEMA_VERSION = "vllm-json-schema-compatibility-v1.1.0"
REQUESTED_NOTE = "abcdefghijklmnop"
MAX_NOTE_LENGTH = 8
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def body_evidence(prefix: str, body: bytes) -> Dict[str, Any]:
    return {
        f"{prefix}_body_base64": base64.b64encode(body).decode("ascii"),
        f"{prefix}_bytes": len(body),
        f"{prefix}_sha256": hashlib.sha256(body).hexdigest(),
    }


def response_format(case: str) -> Dict[str, Any]:
    if case == "json_object_control":
        return {"type": "json_object"}
    if case != "json_schema_max_length":
        raise ValueError(f"unsupported compatibility case: {case}")
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "vllm_max_length_probe",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "note"],
                "properties": {
                    "status": {"type": "string", "enum": ["ok"]},
                    "note": {
                        "type": "string",
                        "maxLength": MAX_NOTE_LENGTH,
                    },
                },
            },
        },
    }


def build_payload(model: str, case: str) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": (
                "This is an output-format compatibility check. Return one JSON "
                f"object with status set to ok and note set exactly to {REQUESTED_NOTE}."
            ),
        }],
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
        "response_format": response_format(case),
    }


def parse_content(envelope: Any) -> tuple[Optional[str], Any, Any]:
    if not isinstance(envelope, dict):
        return None, None, None
    try:
        choice = envelope["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice["finish_reason"]
    except (KeyError, IndexError, TypeError):
        return None, None, envelope.get("usage")
    return content, finish_reason, envelope.get("usage")


def validate_generated_object(case: str, parsed: Any) -> Optional[str]:
    if not isinstance(parsed, dict):
        return "content_not_json_object"
    if set(parsed) != {"status", "note"}:
        return "response_fields_mismatch"
    if parsed.get("status") != "ok" or not isinstance(parsed.get("note"), str):
        return "response_values_mismatch"
    note = parsed["note"]
    if case == "json_object_control" and len(note) <= MAX_NOTE_LENGTH:
        return "control_note_not_longer_than_max_length"
    if case == "json_schema_max_length" and len(note) > MAX_NOTE_LENGTH:
        return "max_length_not_enforced"
    return None


def append_record(handle, record: Dict[str, Any]) -> None:
    data = (
        json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    handle.write(data)
    handle.flush()
    os.fsync(handle.fileno())


def run_probe(
    config_path: Path,
    output_dir: Path,
    source_git_sha: str,
    timeout_s: int,
    runtime_bindings: Optional[Dict[str, Dict[str, str]]] = None,
) -> int:
    if not SOURCE_SHA_RE.fullmatch(source_git_sha):
        raise ValueError("source_git_sha must be a lowercase Git object ID")
    config = load_config(str(config_path))
    endpoint_ids = required_endpoint_ids(config)
    if runtime_bindings is None:
        runtime_bindings = {
            endpoint_id: {"base_url": "http://127.0.0.1:1"}
            for endpoint_id in endpoint_ids
        }
    bindings = validate_runtime_bindings(
        {"endpoints": runtime_bindings}, endpoint_ids
    )
    blocs = config.get("blocs")
    if not isinstance(blocs, list) or [bloc.get("name") for bloc in blocs] != [
        "qwen",
        "llama",
        "gemma",
    ]:
        raise ValueError("probe config must contain qwen, llama, gemma in order")
    output_dir.mkdir(mode=0o755, parents=False, exist_ok=False)
    record_path = output_dir / "compatibility_attempts.jsonl"
    meta_path = output_dir / "probe_meta.json"
    start_time = utc_now_iso()
    config_bytes = config_path.read_bytes()
    meta: Dict[str, Any] = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "status": "running",
        "research_eligible": False,
        "source_git_sha": source_git_sha,
        "config_reference": config_path.name,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "start_time_utc": start_time,
        "end_time_utc": None,
        "planned_models": [bloc["name"] for bloc in blocs],
        "planned_cases": ["json_object_control", "json_schema_max_length"],
        "planned_http_attempts": 6,
        "completed_http_attempts": 0,
        "failure_reason": None,
    }
    atomic_write_json(meta_path, meta)
    terminal_failure: Optional[str] = None

    with record_path.open("xb") as record_handle:
        for bloc in blocs:
            for case in meta["planned_cases"]:
                payload = build_payload(bloc["model"], case)
                request_body = canonical_json_bytes(payload)
                record: Dict[str, Any] = {
                    "schema_version": PROBE_SCHEMA_VERSION,
                    "model_name": bloc["name"],
                    "model": bloc["model"],
                    "model_digest": bloc.get("model_digest"),
                    "endpoint_id": bloc.get("endpoint_id"),
                    "device_slot": bloc.get("device_slot"),
                    "case": case,
                    "requested_note": REQUESTED_NOTE,
                    "requested_note_exact_match": None,
                    "note_length": None,
                    "comparison_max_note_length": MAX_NOTE_LENGTH,
                    "note_exceeds_comparison_max_length": None,
                    "max_note_length": (
                        MAX_NOTE_LENGTH
                        if case == "json_schema_max_length"
                        else None
                    ),
                    "request_payload": copy.deepcopy(payload),
                    **body_evidence("request", request_body),
                    "http_status": None,
                    "response_body_base64": None,
                    "response_bytes": None,
                    "response_sha256": None,
                    "envelope": None,
                    "raw_output": None,
                    "parsed_output": None,
                    "finish_reason": None,
                    "usage": None,
                    "result": "running",
                    "failure_reason": None,
                    "started_at_utc": utc_now_iso(),
                    "ended_at_utc": None,
                }
                try:
                    response = requests.post(
                        f"{bindings[bloc['endpoint_id']]['base_url']}"
                        "/v1/chat/completions",
                        data=request_body,
                        headers={"Content-Type": "application/json"},
                        timeout=timeout_s,
                    )
                    response_body = bytes(response.content)
                    record["http_status"] = int(response.status_code)
                    record.update(body_evidence("response", response_body))
                    if not 200 <= response.status_code < 300:
                        terminal_failure = "http_non_2xx"
                    else:
                        try:
                            envelope = json.loads(response_body)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            terminal_failure = "invalid_api_envelope"
                        else:
                            record["envelope"] = envelope
                            content, finish_reason, usage = parse_content(envelope)
                            record["raw_output"] = content
                            record["finish_reason"] = finish_reason
                            record["usage"] = usage
                            if not isinstance(content, str):
                                terminal_failure = "missing_chat_content"
                            elif finish_reason != "stop":
                                terminal_failure = "finish_reason_not_stop"
                            elif not isinstance(usage, dict):
                                terminal_failure = "usage_missing"
                            else:
                                try:
                                    parsed = json.loads(content)
                                except json.JSONDecodeError:
                                    terminal_failure = "content_not_strict_json"
                                else:
                                    record["parsed_output"] = parsed
                                    if (
                                        isinstance(parsed, dict)
                                        and isinstance(parsed.get("note"), str)
                                    ):
                                        note = parsed["note"]
                                        record["requested_note_exact_match"] = (
                                            note == REQUESTED_NOTE
                                        )
                                        record["note_length"] = len(note)
                                        record[
                                            "note_exceeds_comparison_max_length"
                                        ] = len(note) > MAX_NOTE_LENGTH
                                    terminal_failure = validate_generated_object(
                                        case, parsed
                                    )
                except requests.RequestException as error:
                    terminal_failure = "transport_failure"
                    record["transport_error_type"] = type(error).__name__
                record["ended_at_utc"] = utc_now_iso()
                record["failure_reason"] = terminal_failure
                record["result"] = "pass" if terminal_failure is None else "fail"
                append_record(record_handle, record)
                meta["completed_http_attempts"] += 1
                if terminal_failure is not None:
                    break
            if terminal_failure is not None:
                break

    meta["status"] = "completed" if terminal_failure is None else "failed"
    meta["failure_reason"] = terminal_failure
    meta["end_time_utc"] = utc_now_iso()
    attempts_bytes = record_path.read_bytes()
    meta["attempts_sha256"] = hashlib.sha256(attempts_bytes).hexdigest()
    meta["attempts_bytes"] = len(attempts_bytes)
    meta["attempts_lines"] = attempts_bytes.count(b"\n")
    atomic_write_json(meta_path, meta)
    return 0 if terminal_failure is None else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-git-sha", required=True)
    parser.add_argument("--timeout-s", type=int, default=120)
    parser.add_argument("--runtime-bindings", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be positive")
    try:
        config = load_config(str(args.config))
        bindings = load_runtime_bindings(
            args.runtime_bindings, required_endpoint_ids(config)
        )
        return run_probe(
            args.config,
            args.output_dir,
            args.source_git_sha,
            args.timeout_s,
            bindings,
        )
    except (OSError, TypeError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
