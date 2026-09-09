"""Nine-request engineering gate; helpers retained from the audited v3 probe."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping
import requests
from engine.response_contracts import response_format_for_phase, response_schema_sha256, validate_parsed_response
PROBE_HTTP_SESSION = requests.Session()
PROBE_HTTP_SESSION.trust_env = False
PROBE_RAW_SCHEMA_VERSION = "disaster-behavior-schema-probe-attempt-v1.0.0"
RESPONSE_CONTRACT_VERSION = "phase-response-v3.0.0"
MAX_TOKENS = 1024
CASES = (
 {"case":"phase1", "phase":"phase1", "instruction":"Return exactly one JSON object with message set to probe-ok and reasoning set to the empty string. Do not use markdown."},
 {"case":"phase3_move", "phase":"phase3", "instruction":"Return exactly one JSON object with action move, direction right, memory as the empty string, and reasoning as the empty string.", "expected_action":"move", "expected_direction":"right"},
 {"case":"phase3_stay", "phase":"phase3", "instruction":"Return exactly one JSON object with action stay, direction null, memory as the empty string, and reasoning as the empty string.", "expected_action":"stay", "expected_direction":None},
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_payload(model: str, case: Mapping[str, Any]) -> dict:
    response_format = response_format_for_phase(
        RESPONSE_CONTRACT_VERSION,
        str(case["phase"]),
    )
    if response_format is None:
        raise AssertionError("v3 schema probe response format is missing")
    return {
        "model": model,
        "messages": [{"role": "user", "content": case["instruction"]}],
        "temperature": 0.0,
        "max_tokens": MAX_TOKENS,
        "stream": False,
        "response_format": response_format,
    }


def validate_case(case: Mapping[str, Any], parsed: Any) -> str | None:
    if not isinstance(parsed, dict):
        return "content_not_json_object"
    try:
        validate_parsed_response(
            parsed,
            str(case["phase"]),
            RESPONSE_CONTRACT_VERSION,
        )
    except (TypeError, ValueError):
        return "v3_response_contract_failure"
    if case["phase"] == "phase1":
        if parsed["message"] != "probe-ok":
            return "requested_message_not_observed"
        return None
    if parsed["action"] != case["expected_action"]:
        return "requested_action_not_observed"
    if parsed["direction"] != case["expected_direction"]:
        return "requested_direction_not_observed"
    if parsed["memory"] != "":
        return "requested_memory_not_observed"
    return None


def _body_fields(prefix: str, data: bytes) -> dict:
    return {
        f"{prefix}_body_base64": base64.b64encode(data).decode("ascii"),
        f"{prefix}_bytes": len(data),
        f"{prefix}_sha256": hashlib.sha256(data).hexdigest(),
    }


def _parse_envelope(value: Any) -> tuple[str | None, Any, Any]:
    if not isinstance(value, dict):
        return None, None, None
    try:
        choice = value["choices"][0]
        content = choice["message"]["content"]
        finish_reason = choice["finish_reason"]
    except (KeyError, IndexError, TypeError):
        return None, None, value.get("usage")
    return content, finish_reason, value.get("usage")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def strict_json_loads(value: str | bytes) -> Any:
    """Decode RFC JSON while rejecting duplicate keys and non-finite numbers."""
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_object,
    )


def validate_usage(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "usage_missing"
    required = ("prompt_tokens", "completion_tokens", "total_tokens")
    if any(
        not isinstance(value.get(key), int)
        or isinstance(value.get(key), bool)
        or value[key] < 0
        for key in required
    ):
        return "usage_invalid"
    if value["total_tokens"] != value["prompt_tokens"] + value["completion_tokens"]:
        return "usage_total_mismatch"
    return None


def run_one_request(
    *,
    model: Mapping[str, Any],
    case: Mapping[str, Any],
    base_url: str,
    timeout_s: int,
) -> dict:
    payload = build_payload(str(model["model"]), case)
    request_body = canonical_bytes(payload)
    record = {
        "schema_version": PROBE_RAW_SCHEMA_VERSION,
        "model_name": model["name"],
        "model_source": model["model_source"],
        "model_digest": model["model_digest"],
        "case": case["case"],
        "phase": case["phase"],
        "response_contract_version": RESPONSE_CONTRACT_VERSION,
        "response_schema_sha256": response_schema_sha256(
            RESPONSE_CONTRACT_VERSION
        ),
        "request_payload": payload,
        **_body_fields("request", request_body),
        "http_status": None,
        "response_body_base64": None,
        "response_bytes": None,
        "response_sha256": None,
        "envelope": None,
        "raw_output": None,
        "parsed_output": None,
        "finish_reason": None,
        "usage": None,
        "result": "fail",
        "failure_code": None,
        "transport_error_type": None,
        "start_time_utc": utc_now_iso(),
        "end_time_utc": None,
    }
    failure = None
    try:
        response = PROBE_HTTP_SESSION.post(
            f"{base_url}/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json"},
            timeout=timeout_s,
            allow_redirects=False,
        )
        response_body = bytes(response.content)
        record["http_status"] = int(response.status_code)
        record.update(_body_fields("response", response_body))
        if not 200 <= response.status_code < 300:
            failure = "http_non_2xx"
        else:
            try:
                envelope = strict_json_loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                failure = "invalid_api_envelope"
            else:
                record["envelope"] = envelope
                content, finish_reason, usage = _parse_envelope(envelope)
                record["raw_output"] = content
                record["finish_reason"] = finish_reason
                record["usage"] = usage
                if not isinstance(envelope, dict) or envelope.get("model") != model["model"]:
                    failure = "served_model_identity_mismatch"
                elif not isinstance(content, str):
                    failure = "missing_chat_content"
                elif finish_reason != "stop":
                    failure = "finish_reason_not_stop"
                else:
                    failure = validate_usage(usage)
                if failure is None:
                    try:
                        parsed = strict_json_loads(content)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        failure = "content_not_strict_json"
                    else:
                        record["parsed_output"] = parsed
                        failure = validate_case(case, parsed)
    except requests.RequestException as error:
        failure = "transport_failure"
        record["transport_error_type"] = type(error).__name__
    record["failure_code"] = failure
    record["result"] = "pass" if failure is None else "fail"
    record["end_time_utc"] = utc_now_iso()
    return record


def _write_attempt(handle, record: Mapping[str, Any]) -> None:
    handle.write(canonical_bytes(record) + b"\n")
    handle.flush()
    os.fsync(handle.fileno())
