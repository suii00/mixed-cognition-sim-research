"""Run vLLM experiments through a public-by-construction execution boundary.

The launcher never creates vLLM server log files. It resolves exact local model
snapshots offline, binds only to loopback, enforces a six-GPU ceiling, monitors
GPU scope, stops process groups on every exit path, and promotes run bytes only
after strict validation and a zero-finding publication scan.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import signal
import socket
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
from tools.scan_publication import scan_text, scan_tree  # noqa: E402
from tools.validate_run import validate_run  # noqa: E402


DEFAULT_CONFIG = REPO_ROOT / "configs" / "public_vllm_smoke_3model.json"
DEFAULT_LOCK = REPO_ROOT / "runtime" / "vllm-runtime-lock.json"
LOCK_SCHEMA = "public-vllm-runtime-lock-v1.0.0"
EVIDENCE_SCHEMA = "public-vllm-verification-v1.0.0"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_BASE_PORT = 18100
GPU_ACTIVATION_DELTA_MIB = 256
GPU_RELEASE_DELTA_MIB = 128
MODEL_DIGEST_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MODEL_SOURCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)
PASSTHROUGH_ENVIRONMENT = (
    "PATH",
    "HOME",
    "LD_LIBRARY_PATH",
    "LIBRARY_PATH",
    "CUDA_HOME",
    "CUDA_PATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "LANG",
    "LC_ALL",
)


class PublicVllmError(RuntimeError):
    """A deliberately coarse, publication-safe execution failure."""


@dataclass(frozen=True)
class EndpointSpec:
    endpoint_id: str
    served_model_name: str
    model_source: str
    model_digest: str
    dtype: str
    max_model_len: int
    tensor_parallel_size: int
    gpu_memory_utilization: float
    port: int
    gpu_indices: tuple[int, ...]
    snapshot: Path | None = None

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}"


@dataclass(frozen=True)
class GpuRow:
    index: int
    name: str
    memory_used_mib: int
    memory_total_mib: int


@dataclass
class GpuGuard:
    baseline: dict[int, GpuRow]
    selected: frozenset[int]
    max_gpu_count: int
    max_observed_active_gpu_count: int = 0

    def observe(self, rows: Mapping[int, GpuRow]) -> None:
        if set(rows) != set(self.baseline):
            raise PublicVllmError("GPU inventory changed during execution")
        active = {
            index
            for index, row in rows.items()
            if row.memory_used_mib
            - self.baseline[index].memory_used_mib
            >= GPU_ACTIVATION_DELTA_MIB
        }
        self.max_observed_active_gpu_count = max(
            self.max_observed_active_gpu_count,
            len(active),
        )
        if len(active) > self.max_gpu_count:
            raise PublicVllmError("GPU ceiling exceeded during execution")
        if active - self.selected:
            raise PublicVllmError("GPU allocation escaped the selected scope")

    def released(self, rows: Mapping[int, GpuRow]) -> bool:
        if set(rows) != set(self.baseline):
            return False
        return all(
            rows[index].memory_used_mib
            <= self.baseline[index].memory_used_mib + GPU_RELEASE_DELTA_MIB
            for index in self.selected
        )


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PublicVllmError("runtime lock must be a JSON object")
    return value


def validate_runtime_lock(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != LOCK_SCHEMA:
        raise PublicVllmError("unsupported vLLM runtime lock schema")
    python_lock = value.get("python")
    packages = value.get("packages")
    contract = value.get("execution_contract")
    if not isinstance(python_lock, dict) or set(python_lock) != {
        "implementation",
        "version",
    }:
        raise PublicVllmError("runtime lock has an invalid Python section")
    if not all(
        isinstance(python_lock.get(key), str) and python_lock[key]
        for key in ("implementation", "version")
    ):
        raise PublicVllmError("runtime lock Python values must be non-empty")
    if not isinstance(packages, dict) or not packages or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        for name, version in packages.items()
    ):
        raise PublicVllmError("runtime lock package versions are invalid")
    expected_contract = {
        "compile_cache": "ephemeral-per-run",
        "flashinfer_mode": "installed-but-disabled-before-import",
        "loopback_only": True,
        "max_gpu_count": 6,
        "model_resolution": "offline-exact-snapshot",
        "server_startup": "sequential-under-shared-deadline",
        "server_output": "discarded-without-file-creation",
    }
    if contract != expected_contract:
        raise PublicVllmError("runtime lock execution contract is invalid")


def check_installed_runtime(value: Mapping[str, Any]) -> dict[str, str]:
    python_lock = value["python"]
    if platform.python_implementation() != python_lock["implementation"]:
        raise PublicVllmError("Python implementation does not match runtime lock")
    if platform.python_version() != python_lock["version"]:
        raise PublicVllmError("Python version does not match runtime lock")
    actual: dict[str, str] = {}
    for distribution, expected in value["packages"].items():
        try:
            observed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise PublicVllmError(
                "a required vLLM runtime package is unavailable"
            ) from error
        if observed != expected:
            raise PublicVllmError(
                "a vLLM runtime package does not match the exact lock"
            )
        actual[distribution] = observed
    return actual


def validate_vllm_config(
    config: Mapping[str, Any],
    lock: Mapping[str, Any],
    *,
    allow_legacy_reproduction: bool = False,
) -> None:
    blocs = config.get("blocs")
    if not isinstance(blocs, list) or not blocs:
        raise PublicVllmError("vLLM execution needs at least one bloc")
    backend_version = lock["packages"].get("vllm")
    for bloc in blocs:
        if not isinstance(bloc, dict) or bloc.get("provider") != "vllm":
            raise PublicVllmError("the public vLLM launcher accepts only vLLM blocs")
        if bloc.get("backend_version") != backend_version:
            raise PublicVllmError("config backend version differs from runtime lock")
        if bloc.get("data_parallel_size") != 1:
            raise PublicVllmError("data parallel launch is not in the public contract")
        if bloc.get("flashinfer_mode") != "disabled":
            raise PublicVllmError("config must explicitly disable FlashInfer")
        if not MODEL_SOURCE_RE.fullmatch(str(bloc.get("model_source", ""))):
            raise PublicVllmError("model source is not a canonical repository ID")
        if not MODEL_DIGEST_RE.fullmatch(str(bloc.get("model_digest", ""))):
            raise PublicVllmError("model digest must be an exact commit digest")
        if bloc.get("tokenizer_revision") != bloc.get("model_digest"):
            raise PublicVllmError("tokenizer and model revisions must be identical")
        utilization = bloc.get("gpu_memory_utilization", 0.9)
        if (
            not isinstance(utilization, (int, float))
            or isinstance(utilization, bool)
            or not 0.1 <= float(utilization) <= 0.95
        ):
            raise PublicVllmError("GPU memory utilization is outside the safe range")
    simulation = config.get("simulation")
    if not isinstance(simulation, dict):
        raise PublicVllmError("simulation config is missing")
    if simulation.get("log_schema_version") != "2.0.0":
        raise PublicVllmError("public vLLM execution requires log schema 2.0.0")
    response_contract = simulation.get("response_contract_version")
    if response_contract == "phase-response-v2.0.0":
        return
    expected_legacy = {
        "response_contract_version": "phase-response-v1.0.0",
        "prompt_contract_version": LEGACY_PROMPT_CONTRACT_VERSION,
        "transport_behavior_version": LEGACY_TRANSPORT_BEHAVIOR_VERSION,
        "response_failure_policy": RECORD_AND_CONTINUE_RESPONSE_FAILURE_POLICY,
        "research_eligible": False,
    }
    if not allow_legacy_reproduction or any(
        simulation.get(key) != value for key, value in expected_legacy.items()
    ):
        raise PublicVllmError(
            "public vLLM execution requires the phase-aware response contract "
            "unless the explicit legacy-reproduction contract is selected"
        )


def parse_gpu_indices(value: str | None, required_count: int, limit: int) -> tuple[int, ...]:
    if value is None:
        indices = tuple(range(required_count))
    else:
        pieces = value.split(",")
        if not pieces or any(not re.fullmatch(r"0|[1-9][0-9]*", piece) for piece in pieces):
            raise PublicVllmError("GPU indices must be canonical comma-separated integers")
        indices = tuple(int(piece) for piece in pieces)
    if len(indices) != required_count:
        raise PublicVllmError("GPU index count does not match tensor-parallel demand")
    if len(indices) > limit:
        raise PublicVllmError("requested GPU count exceeds the public ceiling")
    if len(set(indices)) != len(indices):
        raise PublicVllmError("GPU indices must be distinct")
    return indices


def build_endpoint_specs(
    config: Mapping[str, Any],
    gpu_indices: tuple[int, ...],
    base_port: int,
) -> list[EndpointSpec]:
    if base_port < 1024:
        raise PublicVllmError("base port must be unprivileged")
    endpoints: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for bloc in config["blocs"]:
        endpoints.extend((bloc, row) for row in endpoint_rows(bloc))
    if base_port + len(endpoints) - 1 > 65535:
        raise PublicVllmError("endpoint port range is invalid")
    cursor = 0
    specs: list[EndpointSpec] = []
    for ordinal, (bloc, endpoint) in enumerate(endpoints):
        tensor_parallel_size = int(bloc["tensor_parallel_size"])
        assigned = gpu_indices[cursor:cursor + tensor_parallel_size]
        if len(assigned) != tensor_parallel_size:
            raise PublicVllmError("GPU allocation is incomplete")
        cursor += tensor_parallel_size
        specs.append(EndpointSpec(
            endpoint_id=str(endpoint["endpoint_id"]),
            served_model_name=str(bloc["model"]),
            model_source=str(bloc["model_source"]),
            model_digest=str(bloc["model_digest"]),
            dtype=str(bloc["dtype"]),
            max_model_len=int(bloc["max_model_len"]),
            tensor_parallel_size=tensor_parallel_size,
            gpu_memory_utilization=float(bloc.get("gpu_memory_utilization", 0.9)),
            port=base_port + ordinal,
            gpu_indices=assigned,
        ))
    if cursor != len(gpu_indices):
        raise PublicVllmError("GPU allocation contains unused indices")
    return specs


def required_gpu_count(config: Mapping[str, Any]) -> int:
    return sum(
        int(bloc["tensor_parallel_size"]) * len(endpoint_rows(bloc))
        for bloc in config["blocs"]
    )


def _huggingface_cache_root() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        return Path(hf_home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def resolve_exact_snapshot(model_source: str, model_digest: str) -> Path:
    owner, repository = model_source.split("/", 1)
    snapshot = (
        _huggingface_cache_root()
        / f"models--{owner}--{repository}"
        / "snapshots"
        / model_digest
    )
    if not snapshot.is_dir():
        raise PublicVllmError("an exact offline model snapshot is unavailable")
    if not (snapshot / "config.json").is_file():
        raise PublicVllmError("offline model snapshot has no model config")
    if not (snapshot / "tokenizer_config.json").is_file():
        raise PublicVllmError("offline model snapshot has no tokenizer config")
    return snapshot


def attach_snapshots(specs: Sequence[EndpointSpec]) -> list[EndpointSpec]:
    resolved: dict[tuple[str, str], Path] = {}
    result = []
    for spec in specs:
        key = (spec.model_source, spec.model_digest)
        if key not in resolved:
            resolved[key] = resolve_exact_snapshot(*key)
        result.append(EndpointSpec(**{
            **spec.__dict__,
            "snapshot": resolved[key],
        }))
    return result


def query_gpu_rows() -> dict[int, GpuRow]:
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicVllmError("GPU inventory probe is unavailable") from error
    if completed.returncode != 0:
        raise PublicVllmError("GPU inventory probe failed")
    rows: dict[int, GpuRow] = {}
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            raise PublicVllmError("GPU inventory output is malformed")
        try:
            index = int(parts[0])
            used = int(parts[2])
            total = int(parts[3])
        except ValueError as error:
            raise PublicVllmError("GPU inventory output is malformed") from error
        if index in rows or not parts[1] or used < 0 or total <= 0 or used > total:
            raise PublicVllmError("GPU inventory output is malformed")
        rows[index] = GpuRow(index, parts[1], used, total)
    if not rows:
        raise PublicVllmError("no NVIDIA GPU is available")
    return rows


def create_gpu_guard(
    indices: tuple[int, ...],
    limit: int,
    max_initial_memory_mib: int,
) -> GpuGuard:
    baseline = query_gpu_rows()
    if any(index not in baseline for index in indices):
        raise PublicVllmError("a selected GPU index is unavailable")
    if any(
        baseline[index].memory_used_mib > max_initial_memory_mib
        for index in indices
    ):
        raise PublicVllmError("a selected GPU is already busy")
    return GpuGuard(baseline, frozenset(indices), limit)


def ports_are_free(specs: Sequence[EndpointSpec]) -> bool:
    sockets: list[socket.socket] = []
    try:
        for spec in specs:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((LOOPBACK_HOST, spec.port))
            sockets.append(listener)
        return True
    except OSError:
        return False
    finally:
        for listener in sockets:
            listener.close()


def write_flashinfer_shadow(root: Path) -> Path:
    shadow_root = root / "compat"
    package = shadow_root / "flashinfer"
    package.mkdir(parents=True, exist_ok=False)
    (package / "__init__.py").write_text(
        "raise ImportError('disabled by the public vLLM compatibility profile')\n",
        encoding="utf-8",
    )
    return shadow_root


def build_child_environment(
    runtime_root: Path,
    shadow_root: Path,
    gpu_indices: Sequence[int] | None,
) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in PASSTHROUGH_ENVIRONMENT
        if key in os.environ and os.environ[key]
    }
    cache_root = runtime_root / "cache"
    directories = {
        "HF_HOME": cache_root / "huggingface",
        "TORCH_HOME": cache_root / "torch",
        "TORCHINDUCTOR_CACHE_DIR": cache_root / "torchinductor",
        "TRITON_CACHE_DIR": cache_root / "triton",
        "VLLM_CACHE_ROOT": cache_root / "vllm",
        "VLLM_ASSETS_CACHE": cache_root / "vllm-assets",
        "FLASHINFER_WORKSPACE_BASE": cache_root / "flashinfer",
        "XDG_CACHE_HOME": cache_root / "xdg",
        "TMPDIR": runtime_root / "tmp",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    environment.update({key: str(path) for key, path in directories.items()})
    environment.update({
        "PYTHONPATH": str(shadow_root),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_DISABLE_PROGRESS_BARS": "1",
        "DO_NOT_TRACK": "1",
        "VLLM_NO_USAGE_STATS": "1",
        "VLLM_CONFIGURE_LOGGING": "0",
        "VLLM_LOGGING_LEVEL": "CRITICAL",
        "VLLM_ALLREDUCE_USE_FLASHINFER": "0",
        "VLLM_USE_FLASHINFER_SAMPLER": "0",
        "TOKENIZERS_PARALLELISM": "false",
        "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
    })
    if gpu_indices is not None:
        environment["CUDA_VISIBLE_DEVICES"] = ",".join(
            str(index) for index in gpu_indices
        )
    return environment


def build_server_command(spec: EndpointSpec) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        ".",
        "--served-model-name",
        spec.served_model_name,
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(spec.port),
        "--dtype",
        spec.dtype,
        "--max-model-len",
        str(spec.max_model_len),
        "--tensor-parallel-size",
        str(spec.tensor_parallel_size),
        "--gpu-memory-utilization",
        str(spec.gpu_memory_utilization),
        "--generation-config",
        "vllm",
        "--disable-custom-all-reduce",
        "--no-enable-log-requests",
    ]


def start_server(
    spec: EndpointSpec,
    runtime_root: Path,
    shadow_root: Path,
) -> subprocess.Popen[bytes]:
    if spec.snapshot is None:
        raise PublicVllmError("model snapshot was not resolved")
    return subprocess.Popen(
        build_server_command(spec),
        cwd=spec.snapshot,
        env=build_child_environment(runtime_root, shadow_root, spec.gpu_indices),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _send_group_signal(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signum)
    except ProcessLookupError:
        return


def stop_process_groups(processes: Sequence[subprocess.Popen[bytes]]) -> bool:
    stages = (
        (signal.SIGINT, 15.0),
        (signal.SIGTERM, 10.0),
        (signal.SIGKILL, 5.0),
    )
    for signum, grace_s in stages:
        active = [process for process in processes if process.poll() is None]
        if not active:
            return True
        for process in active:
            _send_group_signal(process, signum)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if all(process.poll() is not None for process in processes):
                return True
            time.sleep(0.2)
    return all(process.poll() is not None for process in processes)


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, timeout_s: float) -> Any:
    request = urllib.request.Request(url, method="GET")
    with _NO_PROXY_OPENER.open(request, timeout=timeout_s) as response:
        if response.status != 200:
            raise PublicVllmError("vLLM health endpoint returned a failure")
        return json.loads(response.read())


def endpoint_ready(spec: EndpointSpec) -> bool:
    try:
        request = urllib.request.Request(f"{spec.base_url}/health", method="GET")
        with _NO_PROXY_OPENER.open(request, timeout=2.0) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_servers(
    specs: Sequence[EndpointSpec],
    processes: Sequence[subprocess.Popen[bytes]],
    guard: GpuGuard,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    ready: set[int] = set()
    while time.monotonic() < deadline:
        guard.observe(query_gpu_rows())
        for ordinal, (spec, process) in enumerate(zip(specs, processes)):
            return_code = process.poll()
            if return_code is not None:
                raise PublicVllmError(
                    "vLLM server "
                    f"{ordinal} stopped during startup with exit code {return_code}"
                )
            if ordinal not in ready and endpoint_ready(spec):
                ready.add(ordinal)
        if len(ready) == len(specs):
            for spec in specs:
                try:
                    payload = _http_json(f"{spec.base_url}/v1/models", 5.0)
                    identifiers = {
                        row.get("id")
                        for row in payload.get("data", [])
                        if isinstance(row, dict)
                    }
                except (AttributeError, json.JSONDecodeError, OSError, urllib.error.URLError) as error:
                    raise PublicVllmError("vLLM model health contract failed") from error
                if spec.served_model_name not in identifiers:
                    raise PublicVllmError("vLLM served-model identity differs from config")
            return
        time.sleep(1.0)
    raise PublicVllmError("vLLM startup health check timed out")


def start_servers_sequentially(
    specs: Sequence[EndpointSpec],
    processes: list[subprocess.Popen[bytes]],
    runtime_root: Path,
    shadow_root: Path,
    guard: GpuGuard,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    for spec in specs:
        processes.append(start_server(spec, runtime_root, shadow_root))
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            raise PublicVllmError("vLLM startup health check timed out")
        count = len(processes)
        wait_for_servers(
            specs[:count],
            processes,
            guard,
            remaining_s,
        )


def wait_for_gpu_release(guard: GpuGuard, timeout_s: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = query_gpu_rows()
        if guard.released(rows):
            return True
        time.sleep(1.0)
    return False


def write_runtime_inputs(
    runtime_root: Path,
    config: Mapping[str, Any],
    specs: Sequence[EndpointSpec],
) -> tuple[Path, Path]:
    config_path = runtime_root / "public-config.json"
    bindings_path = runtime_root / "runtime-bindings.yaml"
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    bindings_path.write_text(
        yaml.safe_dump(
            {
                "endpoints": {
                    spec.endpoint_id: {"base_url": spec.base_url}
                    for spec in specs
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config_path, bindings_path


def start_simulator(
    runtime_root: Path,
    shadow_root: Path,
    config_path: Path,
    bindings_path: Path,
    stage_root: Path,
) -> subprocess.Popen[bytes]:
    environment = build_child_environment(runtime_root, shadow_root, None)
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
        env=environment,
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
            raise PublicVllmError("a vLLM server stopped during simulation")
        result = simulation.poll()
        if result is not None:
            return result
        time.sleep(1.0)
    stop_process_groups([simulation])
    raise PublicVllmError("simulation time limit exceeded")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        content_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(content_digest)
    return digest.hexdigest()


def runtime_binding_values_absent(run_dir: Path, specs: Sequence[EndpointSpec]) -> bool:
    forbidden = [spec.base_url.encode("utf-8") for spec in specs]
    for path in sorted(item for item in run_dir.rglob("*") if item.is_file()):
        content = path.read_bytes()
        if any(value in content for value in forbidden):
            return False
    return True


def verify_completed_run(
    run_dir: Path,
    config: Mapping[str, Any],
    specs: Sequence[EndpointSpec],
) -> tuple[bool, int]:
    report = validate_run(run_dir, strict=True)
    if not report.valid:
        return False, len(report.unverifiable)
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    expected_steps = int(config["simulation"]["duration"])
    expected_agents = sum(int(bloc["num_agents"]) for bloc in config["blocs"])
    expected = {
        "status": "completed",
        "aborted": False,
        "expected_steps": expected_steps,
        "completed_steps": expected_steps,
        "expected_agents": expected_agents,
        "observed_agents": expected_agents,
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
    lock: Mapping[str, Any],
    guard: GpuGuard,
    strict_passed: bool,
    strict_unverifiable_count: int,
    publication_findings: int,
    gpu_release_verified: bool,
    processes_stopped: bool,
) -> Path:
    meta = json.loads((run_dir / "run_meta.json").read_text(encoding="utf-8"))
    run_id = str(meta["run_id"])
    evidence_dir = evidence_root / f"validation-vllm-{run_id}"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    lock_bytes = json.dumps(lock, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload = {
        "schema_version": EVIDENCE_SCHEMA,
        "run_id": run_id,
        "source_git_sha": meta.get("git_sha"),
        "protocol_version": config["simulation"]["protocol_version"],
        "metric_version": config["simulation"]["metric_version"],
        "response_contract_version": config["simulation"]["response_contract_version"],
        "model_sources": sorted({bloc["model_source"] for bloc in config["blocs"]}),
        "max_model_len_values": sorted({bloc["max_model_len"] for bloc in config["blocs"]}),
        "runtime_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
        "run_tree_sha256": _tree_digest(run_dir),
        "strict_validation_passed": strict_passed,
        "strict_unverifiable_count": strict_unverifiable_count,
        "publication_scan_finding_count": publication_findings,
        "runtime_binding_values_persisted": False,
        "server_startup": lock["execution_contract"]["server_startup"],
        "vllm_server_log_files_created": False,
        "selected_gpu_count": len(guard.selected),
        "maximum_observed_active_gpu_count": guard.max_observed_active_gpu_count,
        "gpu_release_verified": gpu_release_verified,
        "all_process_groups_stopped": processes_stopped,
        "flashinfer_mode": lock["execution_contract"]["flashinfer_mode"],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if scan_text("verification.json", text):
        shutil.rmtree(evidence_dir)
        raise PublicVllmError("verification evidence failed its publication scan")
    report_path = evidence_dir / "verification.json"
    report_path.write_text(text, encoding="utf-8")
    return report_path


def require_git_head() -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicVllmError("source Git revision cannot be verified") from error
    if result.returncode != 0:
        raise PublicVllmError("source Git revision cannot be verified")


def run_public_vllm(args: argparse.Namespace) -> tuple[str, Path]:
    lock = _load_json_object(args.runtime_lock.resolve())
    validate_runtime_lock(lock)
    try:
        config = load_config(str(args.config.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise PublicVllmError("public experiment config was rejected") from error
    validate_vllm_config(
        config,
        lock,
        allow_legacy_reproduction=args.allow_legacy_reproduction,
    )
    if args.contract_only:
        return "contract-only", args.config
    if os.name != "posix":
        raise PublicVllmError("GPU vLLM execution requires a POSIX host")

    require_git_head()
    check_installed_runtime(lock)
    gpu_limit = int(lock["execution_contract"]["max_gpu_count"])
    gpu_count = required_gpu_count(config)
    indices = parse_gpu_indices(args.gpu_indices, gpu_count, gpu_limit)
    specs = build_endpoint_specs(config, indices, args.base_port)
    specs = attach_snapshots(specs)
    if not ports_are_free(specs):
        raise PublicVllmError("one or more loopback ports are unavailable")
    guard = create_gpu_guard(indices, gpu_limit, args.max_initial_memory_mib)
    if args.preflight_only:
        return "preflight-only", args.config

    run_config = copy.deepcopy(config)
    simulation_config = run_config["simulation"]
    run_id = simulation_config.get("run_id")
    if run_id is None:
        run_id = f"public-vllm-{generate_run_id()}"
        simulation_config["run_id"] = run_id
    output_root = args.output_root.resolve()
    evidence_root = args.evidence_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"output_{run_id}"
    if destination.exists() or destination.is_symlink():
        raise PublicVllmError("run output collision detected")
    stage_root = output_root / ".tmp" / f"{run_id}-stage"
    stage_root.mkdir(parents=True, exist_ok=False)

    servers: list[subprocess.Popen[bytes]] = []
    simulation_process: subprocess.Popen[bytes] | None = None
    execution_error: PublicVllmError | None = None
    simulation_code: int | None = None
    processes_stopped = False
    gpu_release_verified = False
    try:
        with tempfile.TemporaryDirectory(prefix="public-vllm-runtime-") as temporary:
            runtime_root = Path(temporary)
            shadow_root = write_flashinfer_shadow(runtime_root)
            config_path, bindings_path = write_runtime_inputs(
                runtime_root,
                run_config,
                specs,
            )
            try:
                start_servers_sequentially(
                    specs,
                    servers,
                    runtime_root,
                    shadow_root,
                    guard,
                    args.startup_timeout_s,
                )
                simulation_process = start_simulator(
                    runtime_root,
                    shadow_root,
                    config_path,
                    bindings_path,
                    stage_root,
                )
                simulation_code = wait_for_simulator(
                    simulation_process,
                    servers,
                    guard,
                    args.run_timeout_s,
                )
            except PublicVllmError as error:
                execution_error = error
            except (OSError, subprocess.SubprocessError) as error:
                execution_error = PublicVllmError(
                    "a managed child process could not be executed"
                )
            finally:
                processes = [
                    process
                    for process in [simulation_process, *servers]
                    if process is not None
                ]
                processes_stopped = stop_process_groups(processes)
        gpu_release_verified = wait_for_gpu_release(guard)
    finally:
        staged_run = stage_root / f"output_{run_id}"
        if not staged_run.is_dir():
            shutil.rmtree(stage_root, ignore_errors=True)

    staged_run = stage_root / f"output_{run_id}"
    if not staged_run.is_dir():
        if execution_error is not None:
            raise execution_error
        raise PublicVllmError("simulation produced no run directory")

    findings = scan_tree(staged_run)
    if findings:
        shutil.rmtree(stage_root)
        raise PublicVllmError("generated run failed the publication boundary")
    if not runtime_binding_values_absent(staged_run, specs):
        shutil.rmtree(stage_root)
        raise PublicVllmError("runtime binding values entered the generated run")

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
    evidence_path = write_evidence(
        evidence_root,
        destination,
        run_config,
        lock,
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
        raise PublicVllmError("simulation returned a non-completed status")
    if not strict_passed:
        raise PublicVllmError("strict run validation failed")
    if not processes_stopped:
        raise PublicVllmError("one or more process groups remained active")
    if not gpu_release_verified:
        raise PublicVllmError("GPU release could not be verified")
    return str(run_id), evidence_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--runtime-lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--output-root", type=Path, default=REPO_ROOT / "runs")
    parser.add_argument("--evidence-root", type=Path, default=REPO_ROOT / "derived")
    parser.add_argument("--gpu-indices")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    parser.add_argument("--startup-timeout-s", type=float, default=600.0)
    parser.add_argument("--run-timeout-s", type=float, default=900.0)
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
        result, evidence_path = run_public_vllm(args)
    except PublicVllmError as error:
        print(f"FAIL: {error}")
        return 1
    except KeyboardInterrupt:
        print("FAIL: public vLLM execution was interrupted")
        return 130
    except BaseException as error:
        print(f"FAIL: unexpected launcher error ({type(error).__name__})")
        return 2
    if result == "contract-only":
        print("PASS: public vLLM config and runtime-lock contract are internally consistent")
        return 0
    if result == "preflight-only":
        print("PASS: exact runtime, offline models, loopback ports, and GPU scope are ready")
        return 0
    try:
        relative_evidence = evidence_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        relative_evidence = evidence_path.name
    print("PASS: public vLLM run completed without server log creation")
    print(f"run_id={result}")
    print("strict_validation=pass publication_findings=0 runtime_binding_values=absent")
    print(f"evidence={relative_evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
