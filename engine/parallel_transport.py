"""Threaded, phase-preserving execution for blocking LLM transports."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from engine.execution_contracts import (
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    validate_transport_behavior_version,
)
from engine.llm_client import LLMTransportError, TelemetryCallback
from engine.response_contracts import (
    LEGACY_RESPONSE_CONTRACT_VERSION,
    validate_parsed_response,
    validate_response_contract_version,
)


THREAD_NAME_PREFIX = "gate2-llm"
PHASE_ORDER = {"phase1": 1, "phase3": 3}


@dataclass(frozen=True)
class LLMRequest:
    """One transport request containing no mutable simulation object."""

    request_id: str
    step: int
    phase: str
    agent_id: int
    model: str
    base_url: str
    prompt: str
    temperature: float
    max_tokens: int
    timeout_s: int
    provider: str = "ollama"
    llm_overrides: Dict[str, Any] = field(default_factory=dict)
    endpoint_id: Optional[str] = None
    device_slot: Optional[str] = None
    strict_response_validation: bool = False
    response_contract_version: str = LEGACY_RESPONSE_CONTRACT_VERSION
    transport_behavior_version: str = CURRENT_TRANSPORT_BEHAVIOR_VERSION

    def __post_init__(self) -> None:
        if self.phase not in PHASE_ORDER:
            raise ValueError(f"unsupported LLM request phase: {self.phase!r}")
        if self.provider not in {"ollama", "vllm"}:
            raise ValueError(f"unsupported LLM request provider: {self.provider!r}")
        validate_response_contract_version(self.response_contract_version)
        validate_transport_behavior_version(self.transport_behavior_version)
        object.__setattr__(self, "llm_overrides", copy.deepcopy(self.llm_overrides))


@dataclass(frozen=True)
class WorkerTelemetry:
    http_attempts: int = 0
    generation_retries: int = 0
    transport_failures: int = 0
    syntax_parse_attempt_failures: int = 0


class LocalTelemetry:
    """A worker-owned telemetry collector with no shared-state callbacks."""

    _EVENT_FIELDS = {
        "http_attempt": "http_attempts",
        "generation_retry": "generation_retries",
        "transport_failure": "transport_failures",
        "syntax_parse_attempt_failure": "syntax_parse_attempt_failures",
    }

    def __init__(self) -> None:
        self._counts = {field_name: 0 for field_name in self._EVENT_FIELDS.values()}

    def record(self, event: str, amount: int = 1) -> None:
        field_name = self._EVENT_FIELDS.get(event)
        if field_name is None:
            raise ValueError(f"unknown worker telemetry event: {event}")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("worker telemetry amount must be a non-negative integer")
        self._counts[field_name] += amount

    def snapshot(self) -> WorkerTelemetry:
        return WorkerTelemetry(**self._counts)


@dataclass(frozen=True)
class LLMResult:
    request_id: str
    step: int
    phase: str
    agent_id: int
    parsed: Optional[Dict[str, Any]]
    raw_output: str
    telemetry: WorkerTelemetry
    attempts: Tuple[Dict[str, Any], ...] = ()
    error_kind: Optional[str] = None
    error: Optional[BaseException] = None


@dataclass(frozen=True)
class TransportOutcome:
    """Owned result from an instrumented transport invocation."""

    parsed: Optional[Dict[str, Any]]
    raw_output: str
    attempts: Tuple[Dict[str, Any], ...] = ()
    error_kind: Optional[str] = None
    error: Optional[BaseException] = None

    def __iter__(self):
        yield self.parsed
        yield self.raw_output

    def __getitem__(self, index: int):
        return (self.parsed, self.raw_output)[index]


class LLMSyntaxError(RuntimeError):
    """A complete generation attempt did not contain one strict JSON object."""


class LLMResponseSchemaError(RuntimeError):
    """A parsed model response did not satisfy the phase response contract."""


TransportInvocation = Callable[
    [LLMRequest, TelemetryCallback],
    Tuple[Optional[Dict[str, Any]], str] | TransportOutcome,
]
SettledCallback = Callable[[list[LLMResult]], None]


def request_sort_key(request: LLMRequest) -> tuple[int, int, int, str]:
    return (
        request.step,
        PHASE_ORDER[request.phase],
        request.agent_id,
        request.request_id,
    )


def _validate_response_schema(
    parsed: Dict[str, Any],
    phase: str,
    response_contract_version: str = LEGACY_RESPONSE_CONTRACT_VERSION,
) -> None:
    try:
        validate_parsed_response(parsed, phase, response_contract_version)
    except ValueError as error:
        raise LLMResponseSchemaError(str(error)) from error


def _enrich_attempts(
    attempts: Tuple[Dict[str, Any], ...],
    request: LLMRequest,
) -> Tuple[Dict[str, Any], ...]:
    enriched = []
    for value in attempts:
        row = copy.deepcopy(value)
        row.update({
            "request_id": request.request_id,
            "step": request.step,
            "phase": request.phase,
            "agent_id": request.agent_id,
            "model": request.model,
            "provider": request.provider,
            "endpoint_id": request.endpoint_id,
            "device_slot": request.device_slot,
        })
        enriched.append(row)
    return tuple(enriched)


def _execute_one(
    request: LLMRequest,
    invoke_transport: TransportInvocation,
) -> LLMResult:
    telemetry = LocalTelemetry()
    try:
        invocation = invoke_transport(request, telemetry.record)
    except LLMTransportError as error:
        return LLMResult(
            request_id=request.request_id,
            step=request.step,
            phase=request.phase,
            agent_id=request.agent_id,
            parsed=None,
            raw_output="",
            telemetry=telemetry.snapshot(),
            error_kind="transport",
            error=error,
        )
    except BaseException as error:
        return LLMResult(
            request_id=request.request_id,
            step=request.step,
            phase=request.phase,
            agent_id=request.agent_id,
            parsed=None,
            raw_output="",
            telemetry=telemetry.snapshot(),
            error_kind="unexpected",
            error=error,
        )

    if isinstance(invocation, TransportOutcome):
        parsed = copy.deepcopy(invocation.parsed)
        raw_output = invocation.raw_output
        attempts = _enrich_attempts(invocation.attempts, request)
        if invocation.error_kind is not None:
            return LLMResult(
                request_id=request.request_id,
                step=request.step,
                phase=request.phase,
                agent_id=request.agent_id,
                parsed=parsed,
                raw_output=raw_output,
                telemetry=telemetry.snapshot(),
                attempts=attempts,
                error_kind=invocation.error_kind,
                error=invocation.error,
            )
    else:
        parsed, raw_output = invocation
        parsed = copy.deepcopy(parsed)
        attempts = ()

    error_kind: Optional[str] = None
    error: Optional[BaseException] = None
    if parsed is None and request.strict_response_validation:
        error_kind = "syntax"
        error = LLMSyntaxError("model response was not one complete JSON object")
    elif parsed is not None and request.strict_response_validation:
        try:
            _validate_response_schema(
                parsed,
                request.phase,
                request.response_contract_version,
            )
        except LLMResponseSchemaError as schema_error:
            error_kind = "schema"
            error = schema_error

    if attempts:
        mutable_attempts = [copy.deepcopy(value) for value in attempts]
        terminal_attempt = mutable_attempts[-1]
        if error_kind == "schema":
            terminal_attempt["schema_status"] = "invalid"
            terminal_attempt["failure_kind"] = "schema"
            terminal_attempt["error_type"] = type(error).__name__
        elif error_kind == "syntax":
            terminal_attempt["schema_status"] = "not_reached"
            terminal_attempt["failure_kind"] = "syntax"
            terminal_attempt["error_type"] = type(error).__name__
        elif request.strict_response_validation:
            terminal_attempt["schema_status"] = "valid"
        attempts = tuple(mutable_attempts)

    return LLMResult(
        request_id=request.request_id,
        step=request.step,
        phase=request.phase,
        agent_id=request.agent_id,
        parsed=copy.deepcopy(parsed),
        raw_output=raw_output,
        telemetry=telemetry.snapshot(),
        attempts=attempts,
        error_kind=error_kind,
        error=error,
    )


def execute_llm_batch(
    requests: Iterable[LLMRequest],
    max_concurrency: int,
    invoke_transport: TransportInvocation,
    settled_callback: Optional[SettledCallback] = None,
) -> list[LLMResult]:
    """Settle submitted requests and return results in canonical order.

    If the coordinator is interrupted while waiting, executor shutdown still
    settles every request that was submitted. The optional callback receives
    those canonical results exactly once before the original interruption is
    re-raised, allowing coordinator-owned telemetry to remain complete.
    """
    if (
        not isinstance(max_concurrency, int)
        or isinstance(max_concurrency, bool)
        or max_concurrency <= 0
    ):
        raise ValueError("max_concurrency must be a positive integer")

    ordered = sorted(list(requests), key=request_sort_key)
    request_ids = [request.request_id for request in ordered]
    agent_ids = [request.agent_id for request in ordered]
    if len(set(request_ids)) != len(request_ids):
        raise ValueError("duplicate LLM request_id in phase batch")
    if len(set(agent_ids)) != len(agent_ids):
        raise ValueError("duplicate agent_id in phase batch")
    if not ordered:
        if settled_callback is not None:
            settled_callback([])
        return []

    worker_count = min(max_concurrency, len(ordered))
    future_pairs = []
    results = []
    coordinator_error: Optional[BaseException] = None
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=THREAD_NAME_PREFIX,
    ) as executor:
        try:
            for request in ordered:
                future_pairs.append((
                    request,
                    executor.submit(
                        _execute_one,
                        request,
                        invoke_transport,
                    ),
                ))
            results = [future.result() for _, future in future_pairs]
        except BaseException as error:
            coordinator_error = error

    if coordinator_error is not None:
        # Executor shutdown has settled every successfully submitted future.
        results = [future.result() for _, future in future_pairs]

    if settled_callback is not None:
        settled_callback(results)
    if coordinator_error is not None:
        raise coordinator_error
    return results
