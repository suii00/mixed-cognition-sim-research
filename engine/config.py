import copy
from pathlib import Path
from typing import Any, Dict, Iterable

import yaml

from engine.llm_client import validate_ollama_overrides, validate_vllm_overrides
from engine.disaster import parse_disaster_scenario
from engine.execution_contracts import (
    ABORT_RUN_RESPONSE_FAILURE_POLICY,
    CURRENT_PROMPT_CONTRACT_VERSION,
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    JAPANESE_PROMPT_CONTRACT_VERSION,
    LEGACY_PROMPT_CONTRACT_VERSION,
    LEGACY_TRANSPORT_BEHAVIOR_VERSION,
    RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
    validate_prompt_contract_version,
    validate_response_failure_policy,
    validate_transport_behavior_version,
)
from engine.response_contracts import (
    CANONICAL_RESPONSE_CONTRACT_VERSION,
    LEGACY_RESPONSE_CONTRACT_VERSION,
    validate_response_contract_version,
)

from engine.provenance import (
    OBSERVABILITY_LOG_SCHEMA_VERSION,
    normalize_run_id,
    validate_credential_free_config,
    validate_bloc_backend,
    validate_base_url,
)


DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_EDGE_POLICY = "full"
EDGE_POLICIES = frozenset({"full", "within_bloc_only"})
ENDPOINT_ASSIGNMENT_POLICY = "round_robin_by_bloc_ordinal_v1"
PUBLIC_ENDPOINT_KEYS = frozenset({"endpoint_id", "device_slot"})
RUNTIME_BINDING_KEYS = frozenset({"base_url"})
FORBIDDEN_PUBLIC_KEYS = frozenset({
    "base_url",
    "gpu_uuid",
    "hostname",
    "cuda_visible_devices",
})


def _walk_public_config(value: Any, path: str = "config") -> None:
    """Reject operational identifiers before they can enter run provenance."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            if key.casefold() in FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(
                    f"{path}.{key} is runtime-only and forbidden in public config"
                )
            _walk_public_config(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_public_config(child, f"{path}[{index}]")


def validate_public_config_boundary(config: Dict[str, Any]) -> None:
    """Fail closed; public config is persisted byte-for-value as provided."""
    _walk_public_config(config)
    validate_credential_free_config(config)


def endpoint_rows(bloc: Dict[str, Any]) -> list[Dict[str, Any]]:
    pool = bloc.get("endpoint_pool")
    if isinstance(pool, list):
        return pool
    row = {"endpoint_id": bloc.get("endpoint_id")}
    if "device_slot" in bloc:
        row["device_slot"] = bloc.get("device_slot")
    return [row]


def required_endpoint_ids(config: Dict[str, Any]) -> tuple[str, ...]:
    values = {
        str(endpoint["endpoint_id"])
        for bloc in config.get("blocs", [])
        for endpoint in endpoint_rows(bloc)
    }
    return tuple(sorted(values))


def validate_endpoint_pool(bloc: Dict[str, Any], bloc_index: int) -> None:
    """Validate public logical endpoints without operational addresses."""
    if "endpoint_pool" not in bloc:
        endpoint_id = bloc.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError(
                f"blocs[{bloc_index}].endpoint_id must be a non-empty string"
            )
        device_slot = bloc.get("device_slot")
        if device_slot is not None and (
            not isinstance(device_slot, str) or not device_slot
        ):
            raise ValueError(
                f"blocs[{bloc_index}].device_slot must be a non-empty string or absent"
            )
        return
    pool = bloc["endpoint_pool"]
    if not isinstance(pool, list) or not pool:
        raise ValueError(
            f"blocs[{bloc_index}].endpoint_pool must be a non-empty array"
        )
    if bloc.get("endpoint_assignment_policy") != ENDPOINT_ASSIGNMENT_POLICY:
        raise ValueError(
            f"blocs[{bloc_index}].endpoint_assignment_policy must be exactly "
            f"'{ENDPOINT_ASSIGNMENT_POLICY}'"
        )

    endpoint_ids = set()
    device_slots = set()
    for endpoint_index, endpoint in enumerate(pool):
        label = f"blocs[{bloc_index}].endpoint_pool[{endpoint_index}]"
        if not isinstance(endpoint, dict):
            raise ValueError(f"{label} must be a mapping")
        unknown = set(endpoint) - PUBLIC_ENDPOINT_KEYS
        if unknown:
            raise ValueError(
                f"{label} has unknown fields: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        endpoint_id = endpoint.get("endpoint_id")
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError(f"{label}.endpoint_id must be a non-empty string")
        device_slot = endpoint.get("device_slot")
        if device_slot is not None and (
            not isinstance(device_slot, str) or not device_slot
        ):
            raise ValueError(
                f"{label}.device_slot must be a non-empty string or absent"
            )
        if endpoint["endpoint_id"] in endpoint_ids:
            raise ValueError(f"{label}.endpoint_id must be unique within the pool")
        if device_slot is not None and device_slot in device_slots:
            raise ValueError(f"{label}.device_slot must be unique within the pool")
        endpoint_ids.add(endpoint["endpoint_id"])
        if device_slot is not None:
            device_slots.add(device_slot)

    if "endpoint_id" in bloc or "device_slot" in bloc:
        raise ValueError(
            f"blocs[{bloc_index}] must not duplicate endpoint identity outside endpoint_pool"
        )


def validate_runtime_bindings(
    value: Any,
    required_ids: Iterable[str] = (),
) -> Dict[str, Dict[str, str]]:
    """Validate operational endpoint addresses without returning them to config."""
    if not isinstance(value, dict) or set(value) != {"endpoints"}:
        raise ValueError("runtime bindings must contain exactly an 'endpoints' mapping")
    endpoints = value["endpoints"]
    if not isinstance(endpoints, dict) or not endpoints:
        raise ValueError("runtime bindings endpoints must be a non-empty mapping")
    normalized: Dict[str, Dict[str, str]] = {}
    for endpoint_id, binding in endpoints.items():
        if not isinstance(endpoint_id, str) or not endpoint_id:
            raise ValueError("runtime endpoint IDs must be non-empty strings")
        if not isinstance(binding, dict) or set(binding) != RUNTIME_BINDING_KEYS:
            raise ValueError(
                f"runtime endpoint {endpoint_id!r} must contain exactly base_url"
            )
        base_url = binding.get("base_url")
        validate_base_url(base_url)
        normalized[endpoint_id] = {"base_url": base_url}
    missing = sorted(set(required_ids) - set(normalized))
    if missing:
        raise ValueError(
            "runtime bindings are missing endpoint IDs: " + ", ".join(missing)
        )
    return normalized


def load_runtime_bindings(
    path: str | Path,
    required_ids: Iterable[str] = (),
) -> Dict[str, Dict[str, str]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return validate_runtime_bindings(value, required_ids)


def build_effective_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return an owned config with all execution-affecting defaults explicit."""
    if not isinstance(config, dict):
        raise ValueError("config must be a mapping")
    effective = copy.deepcopy(config)
    validate_public_config_boundary(effective)
    llm_defaults = effective.get("llm_defaults")
    if not isinstance(llm_defaults, dict):
        raise ValueError("llm_defaults must be a mapping")
    max_concurrency = llm_defaults.get(
        "max_concurrency",
        DEFAULT_MAX_CONCURRENCY,
    )
    if (
        not isinstance(max_concurrency, int)
        or isinstance(max_concurrency, bool)
        or max_concurrency <= 0
    ):
        raise ValueError("llm_defaults.max_concurrency must be a positive integer")
    llm_defaults["max_concurrency"] = max_concurrency

    agents = effective.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("agents must be a mapping")
    edge_policy = agents.get("edge_policy", DEFAULT_EDGE_POLICY)
    if not isinstance(edge_policy, str) or edge_policy not in EDGE_POLICIES:
        raise ValueError(
            "agents.edge_policy must be one of: "
            + ", ".join(sorted(EDGE_POLICIES))
        )
    agents["edge_policy"] = edge_policy

    simulation = effective.get("simulation")
    if isinstance(simulation, dict):
        simulation.setdefault(
            "log_schema_version", OBSERVABILITY_LOG_SCHEMA_VERSION
        )
    response_contract_version = validate_response_contract_version(
        simulation.get("response_contract_version")
        if isinstance(simulation, dict)
        else None
    )
    prompt_contract_version = validate_prompt_contract_version(
        simulation.get("prompt_contract_version")
        if isinstance(simulation, dict)
        else None
    )
    transport_behavior_version = validate_transport_behavior_version(
        simulation.get("transport_behavior_version")
        if isinstance(simulation, dict)
        else None
    )
    response_failure_policy = validate_response_failure_policy(
        simulation.get("response_failure_policy")
        if isinstance(simulation, dict)
        else None
    )
    if isinstance(simulation, dict):
        simulation["prompt_contract_version"] = prompt_contract_version
        simulation["transport_behavior_version"] = transport_behavior_version
        simulation["response_failure_policy"] = response_failure_policy

    legacy_reproduction_requested = any((
        prompt_contract_version == LEGACY_PROMPT_CONTRACT_VERSION,
        transport_behavior_version == LEGACY_TRANSPORT_BEHAVIOR_VERSION,
        response_failure_policy == RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
    ))
    if legacy_reproduction_requested:
        if not isinstance(simulation, dict):
            raise ValueError("legacy reproduction requires simulation configuration")
        if response_contract_version != LEGACY_RESPONSE_CONTRACT_VERSION:
            raise ValueError(
                "legacy reproduction requires phase-response-v1.0.0"
            )
        if prompt_contract_version != LEGACY_PROMPT_CONTRACT_VERSION:
            raise ValueError(
                "legacy reproduction requires legacy-prompts-v1.0.0"
            )
        if transport_behavior_version != LEGACY_TRANSPORT_BEHAVIOR_VERSION:
            raise ValueError(
                "legacy reproduction requires the versioned legacy transport behavior"
            )
        if response_failure_policy != RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY:
            raise ValueError(
                "legacy reproduction requires record_and_continue response failures"
            )
        if simulation.get("research_eligible") is not False:
            raise ValueError("legacy reproduction must set research_eligible to false")
        protocol_version = simulation.get("protocol_version")
        if not isinstance(protocol_version, str) or not protocol_version.startswith(
            "legacy-reproduction-v1"
        ):
            raise ValueError(
                "legacy reproduction requires a legacy-reproduction-v1 protocol"
            )
        if "scenario" in effective:
            raise ValueError("legacy prompt reproduction does not support scenarios")
    if response_contract_version == CANONICAL_RESPONSE_CONTRACT_VERSION:
        if not isinstance(simulation, dict):
            raise ValueError(
                "phase-response-v2.0.0 requires simulation configuration"
            )
        protocol_version = simulation.get("protocol_version")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise ValueError(
                "phase-response-v2.0.0 requires an explicit protocol_version"
            )
        if simulation.get("log_schema_version") != OBSERVABILITY_LOG_SCHEMA_VERSION:
            raise ValueError(
                "phase-response-v2.0.0 requires log_schema_version "
                f"'{OBSERVABILITY_LOG_SCHEMA_VERSION}'"
            )
        if prompt_contract_version not in {
            CURRENT_PROMPT_CONTRACT_VERSION,
            JAPANESE_PROMPT_CONTRACT_VERSION,
        }:
            raise ValueError(
                "phase-response-v2.0.0 requires the current or Japanese "
                "prompt contract"
            )
        if transport_behavior_version != CURRENT_TRANSPORT_BEHAVIOR_VERSION:
            raise ValueError(
                "phase-response-v2.0.0 requires the current transport behavior"
            )
        if response_failure_policy != ABORT_RUN_RESPONSE_FAILURE_POLICY:
            raise ValueError(
                "phase-response-v2.0.0 requires abort_run response failures"
            )

    blocs = effective.get("blocs")
    if isinstance(blocs, list):
        for bloc_index, bloc in enumerate(blocs):
            if not isinstance(bloc, dict):
                continue
            provider = validate_bloc_backend(bloc)
            bloc["provider"] = provider
            validate_endpoint_pool(bloc, bloc_index)
            if provider == "ollama" and "llm_overrides" in bloc:
                bloc["llm_overrides"] = validate_ollama_overrides(
                    bloc.get("llm_overrides")
                )
            if provider == "vllm":
                bloc["llm_overrides"] = validate_vllm_overrides(
                    bloc.get("llm_overrides")
                )
            if response_contract_version == CANONICAL_RESPONSE_CONTRACT_VERSION:
                if provider != "vllm":
                    raise ValueError(
                        "phase-response-v2.0.0 requires provider 'vllm' "
                        f"for blocs[{bloc_index}]"
                    )
                if "response_format" in bloc.get("llm_overrides", {}):
                    raise ValueError(
                        "phase-response-v2.0.0 owns phase-specific "
                        f"response_format for blocs[{bloc_index}]"
                    )
    if "scenario" in effective:
        simulation = effective.get("simulation")
        if not isinstance(simulation, dict):
            raise ValueError("simulation must be a mapping when scenario is present")
        if not isinstance(blocs, list) or not blocs:
            raise ValueError("blocs must be a non-empty array when scenario is present")
        parse_disaster_scenario(
            effective["scenario"],
            half_space_size=simulation.get("half_space_size"),
            duration=simulation.get("duration"),
            total_agents=sum(
                bloc.get("num_agents", 0)
                for bloc in blocs
                if isinstance(bloc, dict)
            ),
        )
    return effective


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ValueError("config must be a mapping")
    required_top = ["simulation", "blocs", "agents", "places", "llm_defaults"]
    for key in required_top:
        if key not in cfg:
            raise ValueError(f"Missing required config section: {key}")

    cfg = build_effective_config(cfg)
    sim = cfg["simulation"]
    for key in [
        "duration",
        "half_space_size",
        "seed",
        "run_name",
        "protocol_version",
        "metric_version",
    ]:
        if key not in sim:
            raise ValueError(f"Missing simulation.{key}")

    if not isinstance(sim["run_name"], str) or not sim["run_name"]:
        raise ValueError("simulation.run_name must be a non-empty string")
    if (
        not isinstance(sim["duration"], int)
        or isinstance(sim["duration"], bool)
        or sim["duration"] <= 0
    ):
        raise ValueError("simulation.duration must be a positive integer")
    if "run_id" in sim:
        normalize_run_id(sim["run_id"])
    for version_key in ["protocol_version", "metric_version"]:
        if not isinstance(sim[version_key], str) or not sim[version_key]:
            raise ValueError(f"simulation.{version_key} must be a non-empty string")
    if (
        "log_schema_version" in sim
        and sim["log_schema_version"] != OBSERVABILITY_LOG_SCHEMA_VERSION
    ):
        raise ValueError(
            "simulation.log_schema_version must be exactly "
            f"'{OBSERVABILITY_LOG_SCHEMA_VERSION}' when specified"
        )
    thresholds = sim.get("failure_thresholds", {})
    if not isinstance(thresholds, dict):
        raise ValueError("simulation.failure_thresholds must be a mapping")
    threshold_keys = {
        "transport_failures",
        "syntax_parse_failures",
        "schema_validation_failures",
    }
    unknown_thresholds = set(thresholds) - threshold_keys
    if unknown_thresholds:
        raise ValueError(
            "Unknown simulation.failure_thresholds keys: "
            + ", ".join(sorted(str(key) for key in unknown_thresholds))
        )
    for key in threshold_keys:
        if key in thresholds and (
            not isinstance(thresholds[key], int)
            or isinstance(thresholds[key], bool)
            or thresholds[key] < 0
        ):
            raise ValueError(
                f"simulation.failure_thresholds.{key} must be a non-negative integer"
            )

    if not isinstance(cfg["blocs"], list) or not cfg["blocs"]:
        raise ValueError("blocs must contain at least one bloc")
    for i, bloc in enumerate(cfg["blocs"]):
        if not isinstance(bloc, dict):
            raise ValueError(f"blocs[{i}] must be a mapping")
        for key in ["name", "model", "num_agents"]:
            if key not in bloc:
                raise ValueError(f"blocs[{i}] missing '{key}'")
        for key in ["name", "model"]:
            if not isinstance(bloc[key], str) or not bloc[key]:
                raise ValueError(f"blocs[{i}].{key} must be a non-empty string")
        validate_bloc_backend(bloc)
        for key in ["model_digest", "quantization", "chat_template"]:
            if key in bloc and (
                not isinstance(bloc[key], str) or not bloc[key]
            ):
                raise ValueError(
                    f"blocs[{i}].{key} must be a non-empty string"
                )
        if (
            not isinstance(bloc["num_agents"], int)
            or isinstance(bloc["num_agents"], bool)
            or bloc["num_agents"] <= 0
        ):
            raise ValueError(f"blocs[{i}].num_agents must be a positive integer")
        validate_endpoint_pool(bloc, i)

    agents = cfg["agents"]
    for key in ["communication_radius", "memory_limit", "memory_size",
                "message_history_limit", "message_context_size"]:
        if key not in agents:
            raise ValueError(f"agents.{key} missing")

    for i, place in enumerate(cfg["places"]):
        for key in ["name", "center_x", "center_y", "half_size", "capacity"]:
            if key not in place:
                raise ValueError(f"places[{i}] missing '{key}'")

    return cfg
