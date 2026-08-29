#!/usr/bin/env python3
"""Run legacy Ollama replays without persisting operational server output."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engine.config import endpoint_rows, load_config  # noqa: E402
from engine.execution_contracts import (  # noqa: E402
    LEGACY_PROMPT_CONTRACT_VERSION,
    LEGACY_TRANSPORT_BEHAVIOR_VERSION,
    RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
)
from engine.provenance import generate_run_id  # noqa: E402
from tools.run_public_vllm import (  # noqa: E402
    GpuGuard,
    PublicVllmError,
    _load_json_object,
    _tree_digest,
    check_installed_runtime,
    create_gpu_guard,
    parse_gpu_indices,
    ports_are_free,
    query_gpu_rows,
    require_git_head,
    runtime_binding_values_absent,
    stop_process_groups,
    validate_runtime_lock,
    wait_for_gpu_release,
    write_runtime_inputs,
)
from tools.scan_publication import scan_text, scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


DEFAULT_LOCK = REPO_ROOT / "runtime" / "vllm-runtime-lock.json"
DEFAULT_BASE_PORT = 18340
LOOPBACK_HOST = "127.0.0.1"
OLLAMA_VERSION = "0.32.13"
OLLAMA_BINARY = Path("/usr/local/bin/ollama")
MODEL_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
CHAT_TEMPLATE_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
EVIDENCE_SCHEMA = "public-ollama-verification-v1.0.0"


class PublicOllamaError(RuntimeError):
    """Publication-safe Ollama execution failure."""


@dataclass(frozen=True)
class OllamaEndpointSpec:
    endpoint_id: str
    model: str
    model_digest: str
    chat_template_sha256: str
    quantization: str
    port: int
    gpu_index: int

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"


def validate_ollama_config(
    config: Mapping[str, Any],
    *,
    allow_legacy_reproduction: bool,
) -> None:
    blocs = config.get("blocs")
    if not isinstance(blocs, list) or not blocs:
        raise PublicOllamaError("Ollama execution needs at least one bloc")
    for bloc in blocs:
        if not isinstance(bloc, dict) or bloc.get("provider") != "ollama":
            raise PublicOllamaError("the public Ollama launcher accepts only Ollama blocs")
        if bloc.get("backend_version") != OLLAMA_VERSION:
            raise PublicOllamaError("config Ollama version differs from the runtime lock")
        if not MODEL_DIGEST_RE.fullmatch(str(bloc.get("model_digest", ""))):
            raise PublicOllamaError("Ollama model digest must be a SHA-256 value")
        match = CHAT_TEMPLATE_RE.fullmatch(str(bloc.get("chat_template", "")))
        if match is None:
            raise PublicOllamaError("Ollama chat template must be hash-bound")
        if bloc.get("quantization") != "F16":
            raise PublicOllamaError("legacy Ollama reproduction requires F16 models")
        if len(endpoint_rows(bloc)) != 1:
            raise PublicOllamaError("legacy Ollama blocs require one endpoint each")

    simulation = config.get("simulation")
    expected = {
        "log_schema_version": "2.0.0",
        "response_contract_version": "phase-response-v1.0.0",
        "prompt_contract_version": LEGACY_PROMPT_CONTRACT_VERSION,
        "transport_behavior_version": LEGACY_TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
        "research_eligible": False,
    }
    if (
        not allow_legacy_reproduction
        or not isinstance(simulation, dict)
        or any(simulation.get(key) != value for key, value in expected.items())
    ):
        raise PublicOllamaError("explicit legacy-reproduction contract is required")


def build_endpoint_specs(
    config: Mapping[str, Any],
    gpu_indices: tuple[int, ...],
    base_port: int,
) -> list[OllamaEndpointSpec]:
    if base_port < 1024:
        raise PublicOllamaError("base port must be unprivileged")
    if len(config["blocs"]) != len(gpu_indices):
        raise PublicOllamaError("one distinct GPU is required for each Ollama bloc")
    specs = []
    for ordinal, (bloc, gpu_index) in enumerate(zip(config["blocs"], gpu_indices)):
        endpoint = endpoint_rows(bloc)[0]
        template_match = CHAT_TEMPLATE_RE.fullmatch(bloc["chat_template"])
        assert template_match is not None
        specs.append(OllamaEndpointSpec(
            endpoint_id=endpoint["endpoint_id"],
            model=bloc["model"],
            model_digest=bloc["model_digest"],
            chat_template_sha256=template_match.group(1),
            quantization=bloc["quantization"],
            port=base_port + ordinal,
            gpu_index=gpu_index,
        ))
    return specs


def check_ollama_binary() -> None:
    if not OLLAMA_BINARY.is_file():
        raise PublicOllamaError("the pinned Ollama binary is unavailable")
    try:
        result = subprocess.run(
            [str(OLLAMA_BINARY), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicOllamaError("the Ollama version probe failed") from error
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or OLLAMA_VERSION not in combined:
        raise PublicOllamaError("the installed Ollama version differs from the lock")
    sudo = subprocess.run(
        ["sudo", "-n", "-u", "ollama", "true"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    if sudo.returncode != 0:
        raise PublicOllamaError("passwordless execution as the Ollama service user is unavailable")


def build_server_command(spec: OllamaEndpointSpec) -> list[str]:
    return [
        "sudo",
        "-n",
        "-H",
        "-u",
        "ollama",
        "env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        f"CUDA_VISIBLE_DEVICES={spec.gpu_index}",
        "OLLAMA_VULKAN=0",
        f"OLLAMA_HOST={LOOPBACK_HOST}:{spec.port}",
        "OLLAMA_NO_CLOUD=1",
        "OLLAMA_NUM_PARALLEL=1",
        "OLLAMA_MAX_LOADED_MODELS=1",
        "OLLAMA_CONTEXT_LENGTH=4096",
        "OLLAMA_KEEP_ALIVE=-1",
        str(OLLAMA_BINARY),
        "serve",
    ]


def start_server(spec: OllamaEndpointSpec) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        build_server_command(spec),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _http_json(url: str, timeout_s: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return json.loads(response.read())


def _http_post_json(url: str, value: Mapping[str, Any], timeout_s: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(value).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read())


def endpoint_ready(spec: OllamaEndpointSpec) -> bool:
    try:
        value = _http_json(f"{spec.base_url}/api/version", 2.0)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return value == {"version": OLLAMA_VERSION}


def wait_for_servers(
    specs: Sequence[OllamaEndpointSpec],
    processes: Sequence[subprocess.Popen[bytes]],
    guard: GpuGuard,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        guard.observe(query_gpu_rows())
        if any(process.poll() is not None for process in processes):
            raise PublicOllamaError("an Ollama server stopped before readiness")
        if all(endpoint_ready(spec) for spec in specs):
            return
        time.sleep(1.0)
    raise PublicOllamaError("Ollama server startup timed out")


def verify_models(specs: Sequence[OllamaEndpointSpec]) -> None:
    for spec in specs:
        try:
            tags = _http_json(f"{spec.base_url}/api/tags", 15.0)
            models = tags["models"]
            row = next(item for item in models if item.get("name") == spec.model)
            shown = _http_post_json(
                f"{spec.base_url}/api/show",
                {"model": spec.model},
                30.0,
            )
        except (KeyError, StopIteration, TypeError, OSError, ValueError, urllib.error.URLError) as error:
            raise PublicOllamaError("an exact Ollama model is unavailable") from error
        if row.get("digest") != spec.model_digest:
            raise PublicOllamaError("an Ollama model digest differs from the public config")
        details = shown.get("details")
        if not isinstance(details, dict) or details.get("quantization_level") != spec.quantization:
            raise PublicOllamaError("an Ollama quantization differs from the public config")
        template = shown.get("template")
        if not isinstance(template, str) or hashlib.sha256(template.encode("utf-8")).hexdigest() != spec.chat_template_sha256:
            raise PublicOllamaError("an Ollama chat template differs from the public config")


def simulator_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "HOME",
        "LD_LIBRARY_PATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "LANG",
        "LC_ALL",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def start_simulator(
    config_path: Path,
    bindings_path: Path,
    stage_root: Path,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "main.py"),
            "--config",
            str(config_path),
            "--runtime-bindings",
            str(bindings_path),
            "--output-root",
            str(stage_root),
        ],
        cwd=REPO_ROOT,
        env=simulator_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def wait_for_simulator(
    simulation: subprocess.Popen[bytes],
    servers: Sequence[subprocess.Popen[bytes]],
    guard: GpuGuard,
    timeout_s: float,
) -> int:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        guard.observe(query_gpu_rows())
        if any(server.poll() is not None for server in servers):
            stop_process_groups([simulation])
            raise PublicOllamaError("an Ollama server stopped during simulation")
        result = simulation.poll()
        if result is not None:
            return result
        time.sleep(1.0)
    stop_process_groups([simulation])
    raise PublicOllamaError("simulation time limit exceeded")


def verify_completed_run(
    run_dir: Path,
    config: Mapping[str, Any],
    specs: Sequence[OllamaEndpointSpec],
) -> tuple[bool, int]:
    report = validate_run(run_dir, strict=True)
    if not report.valid:
        return False, len(report.unverifiable)
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    expected = {
        "status": "completed",
        "aborted": False,
        "expected_steps": int(config["simulation"]["duration"]),
        "completed_steps": int(config["simulation"]["duration"]),
        "expected_agents": sum(int(bloc["num_agents"]) for bloc in config["blocs"]),
        "observed_agents": sum(int(bloc["num_agents"]) for bloc in config["blocs"]),
        "raw_manifest_status": "available",
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        return False, len(report.unverifiable)
    if not runtime_binding_values_absent(run_dir, specs):
        return False, len(report.unverifiable)
    return True, len(report.unverifiable)


def write_evidence(
    evidence_root: Path,
    run_dir: Path,
    config: Mapping[str, Any],
    guard: GpuGuard,
    strict_passed: bool,
    strict_unverifiable_count: int,
    publication_findings: int,
    gpu_release_verified: bool,
    processes_stopped: bool,
) -> Path:
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    run_id = str(meta["run_id"])
    evidence_dir = evidence_root / f"validation-ollama-{run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    payload = {
        "all_process_groups_stopped": processes_stopped,
        "gpu_release_verified": gpu_release_verified,
        "maximum_observed_active_gpu_count": guard.max_observed_active_gpu_count,
        "ollama_server_log_files_created": False,
        "ollama_version": OLLAMA_VERSION,
        "protocol_version": config["simulation"]["protocol_version"],
        "publication_scan_finding_count": publication_findings,
        "run_id": run_id,
        "run_tree_sha256": _tree_digest(run_dir),
        "runtime_binding_values_persisted": False,
        "schema_version": EVIDENCE_SCHEMA,
        "selected_gpu_count": len(guard.selected),
        "source_git_sha": meta.get("git_sha"),
        "strict_unverifiable_count": strict_unverifiable_count,
        "strict_validation_passed": strict_passed,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if scan_text("verification.json", text):
        shutil.rmtree(evidence_dir)
        raise PublicOllamaError("verification evidence failed its publication scan")
    report_path = evidence_dir / "verification.json"
    report_path.write_text(text, encoding="utf-8")
    return report_path


def run_public_ollama(args: argparse.Namespace) -> tuple[str, Path]:
    lock = _load_json_object(args.runtime_lock.resolve())
    try:
        validate_runtime_lock(lock)
        config = load_config(str(args.config.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError, PublicVllmError) as error:
        raise PublicOllamaError("public Ollama contract was rejected") from error
    validate_ollama_config(
        config,
        allow_legacy_reproduction=args.allow_legacy_reproduction,
    )
    if args.contract_only:
        return "contract-only", args.config
    if os.name != "posix":
        raise PublicOllamaError("GPU Ollama execution requires a POSIX host")

    require_git_head()
    check_installed_runtime(lock)
    check_ollama_binary()
    gpu_limit = int(lock["execution_contract"]["max_gpu_count"])
    indices = parse_gpu_indices(args.gpu_indices, len(config["blocs"]), gpu_limit)
    specs = build_endpoint_specs(config, indices, args.base_port)
    if not ports_are_free(specs):
        raise PublicOllamaError("one or more loopback ports are unavailable")
    guard = create_gpu_guard(indices, gpu_limit, args.max_initial_memory_mib)

    servers: list[subprocess.Popen[bytes]] = []
    processes_stopped = False
    gpu_release_verified = False
    try:
        servers = [start_server(spec) for spec in specs]
        wait_for_servers(specs, servers, guard, args.startup_timeout_s)
        verify_models(specs)
        if args.preflight_only:
            return "preflight-only", args.config

        run_config = copy.deepcopy(config)
        simulation_config = run_config["simulation"]
        run_id = simulation_config.get("run_id")
        if run_id is None:
            run_id = f"public-ollama-{generate_run_id()}"
            simulation_config["run_id"] = run_id
        output_root = args.output_root.resolve()
        evidence_root = args.evidence_root.resolve()
        output_root.mkdir(parents=True, exist_ok=True)
        evidence_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / f"output_{run_id}"
        if destination.exists() or destination.is_symlink():
            raise PublicOllamaError("run output collision detected")
        stage_root = output_root / ".tmp" / f"{run_id}-stage"
        stage_root.mkdir(parents=True, exist_ok=False)

        execution_error: PublicOllamaError | None = None
        simulation_code: int | None = None
        with tempfile.TemporaryDirectory(prefix="public-ollama-runtime-") as temporary:
            runtime_root = Path(temporary)
            config_path, bindings_path = write_runtime_inputs(
                runtime_root,
                run_config,
                specs,
            )
            simulation = start_simulator(config_path, bindings_path, stage_root)
            try:
                simulation_code = wait_for_simulator(
                    simulation,
                    servers,
                    guard,
                    args.run_timeout_s,
                )
            except PublicOllamaError as error:
                execution_error = error
            finally:
                stop_process_groups([simulation])

        staged_run = stage_root / f"output_{run_id}"
        if not staged_run.is_dir():
            shutil.rmtree(stage_root, ignore_errors=True)
            if execution_error is not None:
                raise execution_error
            raise PublicOllamaError("simulation produced no run directory")
        findings = scan_tree(staged_run)
        if findings:
            shutil.rmtree(stage_root)
            raise PublicOllamaError("generated run failed the publication boundary")
        if not runtime_binding_values_absent(staged_run, specs):
            shutil.rmtree(stage_root)
            raise PublicOllamaError("runtime binding values entered the generated run")
        strict_passed = False
        unverifiable_count = 0
        if execution_error is None and simulation_code == 0:
            strict_passed, unverifiable_count = verify_completed_run(
                staged_run,
                run_config,
                specs,
            )
        os.replace(staged_run, destination)
        shutil.rmtree(stage_root)
    finally:
        processes_stopped = stop_process_groups(servers)
        gpu_release_verified = wait_for_gpu_release(guard)

    evidence_path = write_evidence(
        evidence_root,
        destination,
        run_config,
        guard,
        strict_passed,
        unverifiable_count,
        len(findings),
        gpu_release_verified,
        processes_stopped,
    )
    if execution_error is not None:
        raise execution_error
    if simulation_code != 0:
        raise PublicOllamaError("simulation returned a non-completed status")
    if not strict_passed:
        raise PublicOllamaError("strict run validation failed")
    if not processes_stopped:
        raise PublicOllamaError("one or more process groups remained active")
    if not gpu_release_verified:
        raise PublicOllamaError("GPU release could not be verified")
    return str(run_id), evidence_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--evidence-root", type=Path, default=REPO_ROOT / "derived")
    parser.add_argument("--gpu-indices")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument("--startup-timeout-s", type=float, default=600.0)
    parser.add_argument("--run-timeout-s", type=float, default=7200.0)
    parser.add_argument("--max-initial-memory-mib", type=int, default=512)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--contract-only", action="store_true")
    parser.add_argument("--allow-legacy-reproduction", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.contract_only and args.preflight_only:
        print("FAIL: choose only one validation-only mode")
        return 2
    if (
        args.startup_timeout_s <= 0
        or args.run_timeout_s <= 0
        or args.max_initial_memory_mib < 0
    ):
        print("FAIL: timeout and memory bounds must be positive")
        return 2
    try:
        result, evidence_path = run_public_ollama(args)
    except (PublicOllamaError, PublicVllmError) as error:
        print(f"FAIL: {error}")
        return 1
    except KeyboardInterrupt:
        print("FAIL: public Ollama execution was interrupted")
        return 130
    except BaseException as error:
        print(f"FAIL: unexpected launcher error ({type(error).__name__})")
        return 2
    if result == "contract-only":
        print("PASS: public Ollama contract is internally consistent")
    elif result == "preflight-only":
        print("PASS: public Ollama runtime preflight completed and cleaned up")
    else:
        print(f"PASS: public Ollama run {result} completed")
        print(f"Evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
