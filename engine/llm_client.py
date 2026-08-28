import base64
import copy
import hashlib
import json
import requests
from typing import Any, Callable, Dict, Optional, Tuple

from engine.response_contracts import validate_phase_response_format


TelemetryCallback = Callable[[str, int], None]
ResponseObserver = Callable[[Dict[str, Any]], None]
HttpResponseObserver = Callable[[int, bytes], None]
AttemptObserver = Callable[[Dict[str, Any]], None]

VLLM_ALLOWED_OVERRIDES = frozenset({
    "frequency_penalty",
    "ignore_eos",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
    "response_format",
    "seed",
    "stop",
    "top_k",
    "top_p",
})


def validate_ollama_overrides(llm_overrides: Optional[Dict]) -> Dict[str, Any]:
    """Return owned Ollama options plus the supported top-level format switch."""
    if llm_overrides is None:
        return {}
    if not isinstance(llm_overrides, dict):
        raise ValueError("Ollama llm_overrides must be a mapping")
    overrides = copy.deepcopy(llm_overrides)
    response_format = overrides.get("format")
    if response_format is not None and response_format != "json":
        raise ValueError("Ollama llm_overrides.format must be exactly 'json'")
    return overrides


class LLMTransportError(RuntimeError):
    """A terminal HTTP/transport failure that must abort the run."""


def extract_json(text: str) -> Optional[Dict]:
    """Parse exactly one complete JSON object, allowing whitespace only."""
    if not isinstance(text, str):
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _emit(telemetry: Optional[TelemetryCallback], event: str) -> None:
    if telemetry is not None:
        telemetry(event, 1)


def _post_once(
    url: str,
    payload: Dict,
    timeout_s: int,
    telemetry: Optional[TelemetryCallback],
    http_response_observer: Optional[HttpResponseObserver] = None,
    backend_name: str = "LLM backend",
):
    _emit(telemetry, "http_attempt")
    try:
        response = requests.post(url, json=payload, timeout=timeout_s)
        if http_response_observer is not None:
            http_response_observer(
                int(response.status_code),
                bytes(response.content),
            )
        response.raise_for_status()
        return response
    except (requests.ConnectionError, requests.Timeout) as error:
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} transport failed"
        ) from error
    except requests.HTTPError as error:
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} returned an HTTP error"
        ) from error
    except requests.RequestException as error:
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(f"{backend_name} request failed") from error


def _decode_response(
    response,
    observer: Optional[ResponseObserver],
    telemetry: Optional[TelemetryCallback],
    backend_name: str,
) -> Dict[str, Any]:
    try:
        envelope = response.json()
    except (TypeError, ValueError) as error:
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} returned an invalid JSON envelope"
        ) from error
    if not isinstance(envelope, dict):
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} returned a non-object JSON envelope"
        )
    if observer is not None:
        observer(copy.deepcopy(envelope))
    return envelope


def _require_content(
    envelope: Dict[str, Any],
    path: Tuple[Any, ...],
    telemetry: Optional[TelemetryCallback],
    backend_name: str,
) -> str:
    value: Any = envelope
    try:
        for component in path:
            value = value[component]
    except (KeyError, IndexError, TypeError) as error:
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} response does not match its chat contract"
        ) from error
    if not isinstance(value, str):
        _emit(telemetry, "transport_failure")
        raise LLMTransportError(
            f"{backend_name} chat content must be a string"
        )
    return value


def build_ollama_chat_payload(
    prompt: str,
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    llm_overrides: Optional[Dict] = None,
    keep_alive: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build the exact native ``/api/chat`` payload used by the client."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    overrides = validate_ollama_overrides(llm_overrides)
    response_format = overrides.pop("format", None)
    payload["options"].update(overrides)
    if response_format is not None:
        payload["format"] = response_format
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    return payload


def validate_vllm_overrides(llm_overrides: Optional[Dict]) -> Dict[str, Any]:
    """Return an owned allowlisted OpenAI-compatible sampling override map."""
    if llm_overrides is None:
        return {}
    if not isinstance(llm_overrides, dict):
        raise ValueError("vLLM llm_overrides must be a mapping")
    unknown = set(llm_overrides) - VLLM_ALLOWED_OVERRIDES
    if unknown:
        raise ValueError(
            "unsupported vLLM llm_overrides keys: "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    response_format = llm_overrides.get("response_format")
    if response_format is not None and response_format != {"type": "json_object"}:
        raise ValueError(
            "vLLM llm_overrides.response_format must be exactly "
            "{'type': 'json_object'}"
        )
    return copy.deepcopy(llm_overrides)


def build_vllm_chat_payload(
    prompt: str,
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    llm_overrides: Optional[Dict] = None,
    phase_response_format: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the fixed OpenAI-compatible ``/v1/chat/completions`` payload."""
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    overrides = validate_vllm_overrides(llm_overrides)
    response_format = validate_phase_response_format(phase_response_format)
    if response_format is not None and "response_format" in overrides:
        raise ValueError(
            "phase-owned response_format cannot be combined with an "
            "llm_overrides response_format"
        )
    payload.update(overrides)
    if response_format is not None:
        payload["response_format"] = response_format
    return payload


def _call_chat_once(
    *,
    url: str,
    payload: Dict[str, Any],
    content_path: Tuple[Any, ...],
    timeout_s: int,
    telemetry: Optional[TelemetryCallback],
    response_observer: Optional[ResponseObserver],
    http_response_observer: Optional[HttpResponseObserver],
    attempt_observer: Optional[AttemptObserver],
    backend_name: str,
) -> Tuple[Optional[Dict], str]:
    attempt: Dict[str, Any] = {
        "generation_attempt": 1,
        "http_attempt": 1,
        "http_status": None,
        "http_response_body_base64": None,
        "http_response_bytes": None,
        "http_response_sha256": None,
        "envelope": None,
        "raw_output": None,
        "finish_reason": None,
        "usage": None,
        "transport_status": "not_completed",
        "parse_status": "not_reached",
        "schema_status": "not_checked",
        "failure_kind": None,
        "error_type": None,
    }

    def observe_http(status: int, body: bytes) -> None:
        attempt["http_status"] = status
        attempt["http_response_body_base64"] = base64.b64encode(body).decode(
            "ascii"
        )
        attempt["http_response_bytes"] = len(body)
        attempt["http_response_sha256"] = hashlib.sha256(body).hexdigest()
        if http_response_observer is not None:
            http_response_observer(status, bytes(body))

    def observe_envelope(envelope: Dict[str, Any]) -> None:
        attempt["envelope"] = copy.deepcopy(envelope)
        if response_observer is not None:
            response_observer(copy.deepcopy(envelope))

    try:
        response = _post_once(
            url,
            payload,
            timeout_s,
            telemetry,
            observe_http,
            backend_name,
        )
        envelope = _decode_response(
            response,
            observe_envelope,
            telemetry,
            backend_name,
        )
        raw_text = _require_content(
            envelope,
            content_path,
            telemetry,
            backend_name,
        )
        attempt["transport_status"] = "ok"
        attempt["raw_output"] = raw_text
        if backend_name == "vLLM":
            try:
                attempt["finish_reason"] = envelope["choices"][0].get(
                    "finish_reason"
                )
            except (KeyError, IndexError, TypeError):
                attempt["finish_reason"] = None
            usage = envelope.get("usage")
            attempt["usage"] = (
                copy.deepcopy(usage) if isinstance(usage, dict) else None
            )
        else:
            attempt["finish_reason"] = envelope.get("done_reason")
            usage_keys = (
                "prompt_eval_count",
                "eval_count",
                "total_duration",
                "load_duration",
                "prompt_eval_duration",
                "eval_duration",
            )
            usage = {
                key: copy.deepcopy(envelope[key])
                for key in usage_keys
                if key in envelope
            }
            attempt["usage"] = usage or None

        parsed = extract_json(raw_text)
        attempt["parse_status"] = "valid" if parsed is not None else "invalid"
        if parsed is None:
            attempt["failure_kind"] = "syntax"
            _emit(telemetry, "syntax_parse_attempt_failure")
        return parsed, raw_text
    except LLMTransportError as error:
        attempt["transport_status"] = "error"
        attempt["failure_kind"] = "transport"
        attempt["error_type"] = type(error).__name__
        raise
    except BaseException as error:
        attempt["failure_kind"] = "unexpected"
        attempt["error_type"] = type(error).__name__
        raise
    finally:
        if attempt_observer is not None:
            attempt_observer(copy.deepcopy(attempt))


def call_ollama(prompt: str, model: str, base_url: str,
                temperature: float = 0.2, max_tokens: int = 1024,
                timeout_s: int = 120, llm_overrides: Optional[Dict] = None,
                telemetry: Optional[TelemetryCallback] = None,
                keep_alive: Optional[Any] = None,
                response_observer: Optional[ResponseObserver] = None,
                http_response_observer: Optional[HttpResponseObserver] = None,
                attempt_observer: Optional[AttemptObserver] = None,
                ) -> Tuple[Optional[Dict], str]:
    url = f"{base_url}/api/chat"
    payload = build_ollama_chat_payload(
        prompt=prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        llm_overrides=llm_overrides,
        keep_alive=keep_alive,
    )

    return _call_chat_once(
        url=url,
        payload=payload,
        content_path=("message", "content"),
        timeout_s=timeout_s,
        telemetry=telemetry,
        response_observer=response_observer,
        http_response_observer=http_response_observer,
        attempt_observer=attempt_observer,
        backend_name="Ollama",
    )


def call_vllm(prompt: str, model: str, base_url: str,
              temperature: float = 0.2, max_tokens: int = 1024,
              timeout_s: int = 120, llm_overrides: Optional[Dict] = None,
              telemetry: Optional[TelemetryCallback] = None,
              response_observer: Optional[ResponseObserver] = None,
              http_response_observer: Optional[HttpResponseObserver] = None,
              attempt_observer: Optional[AttemptObserver] = None,
              phase_response_format: Optional[Dict[str, Any]] = None,
              ) -> Tuple[Optional[Dict], str]:
    """Call a vLLM OpenAI-compatible chat endpoint exactly once."""
    url = f"{base_url}/v1/chat/completions"
    payload = build_vllm_chat_payload(
        prompt=prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        llm_overrides=llm_overrides,
        phase_response_format=phase_response_format,
    )
    return _call_chat_once(
        url=url,
        payload=payload,
        content_path=("choices", 0, "message", "content"),
        timeout_s=timeout_s,
        telemetry=telemetry,
        response_observer=response_observer,
        http_response_observer=http_response_observer,
        attempt_observer=attempt_observer,
        backend_name="vLLM",
    )
