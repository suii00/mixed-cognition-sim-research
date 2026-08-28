import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from engine.config import (
    build_effective_config,
    load_config,
    load_runtime_bindings,
    required_endpoint_ids,
    validate_public_config_boundary,
)
from engine.provenance import (
    InvalidRunIdError,
    RunLifecycle,
    collect_bloc_models,
    collect_gpu_info,
    compute_config_hash,
    normalize_run_id,
    validate_base_url,
)


def public_config(run_id: str = "public-provenance-test") -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 7,
            "run_name": run_id,
            "run_id": run_id,
            "protocol_version": "public-provenance-test-v2.0.0",
            "log_schema_version": "2.0.0",
            "metric_version": "test-metric-v1.0.0",
        },
        "blocs": [{
            "name": "alpha",
            "provider": "ollama",
            "model": "model-a",
            "endpoint_id": "ollama-local",
            "device_slot": "logical-device-0",
            "num_agents": 1,
        }],
        "agents": {
            "communication_radius": 8,
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 4,
            "message_context_size": 2,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 32,
            "timeout_s": 5,
            "max_concurrency": 1,
        },
    }


def contains_key(value, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(contains_key(child, key) for child in value)
    return False


class PublicConfigBoundaryTests(unittest.TestCase):
    def test_public_config_has_only_logical_endpoint_identity(self):
        config = build_effective_config(public_config())
        self.assertEqual(required_endpoint_ids(config), ("ollama-local",))
        validate_public_config_boundary(config)
        self.assertFalse(contains_key(config, "base_url"))
        self.assertFalse(contains_key(config, "gpu_uuid"))

    def test_runtime_values_are_loaded_separately(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.yaml"
            bindings_path = root / "bindings.yaml"
            config_path.write_text(
                yaml.safe_dump(public_config(), sort_keys=False), encoding="utf-8"
            )
            bindings_path.write_text(
                yaml.safe_dump({
                    "endpoints": {
                        "ollama-local": {"base_url": "http://127.0.0.1:11434"}
                    }
                }),
                encoding="utf-8",
            )
            config = load_config(str(config_path))
            bindings = load_runtime_bindings(
                bindings_path, required_endpoint_ids(config)
            )
        self.assertEqual(
            bindings["ollama-local"]["base_url"], "http://127.0.0.1:11434"
        )
        self.assertFalse(contains_key(config, "base_url"))

    def test_runtime_fields_and_credentials_fail_before_output(self):
        forbidden = (
            ("base_url", "http://127.0.0.1:11434"),
            ("gpu_uuid", "device-identity"),
            ("hostname", "internal-host"),
            ("api_key", "example-not-a-real-secret"),
        )
        for key, value in forbidden:
            with self.subTest(key=key):
                config = public_config(f"reject-{key.replace('_', '-')}")
                config["blocs"][0][key] = value
                with self.assertRaises(ValueError):
                    build_effective_config(config)

    def test_runtime_url_rejects_credentials(self):
        with self.assertRaises(ValueError):
            validate_base_url(
                "https://user:" + "password@" + "example." + "test"
            )

    def test_unsafe_run_ids_fail_closed(self):
        for value in ("", "../escape", "C:\\escape", "CON", "a" * 129):
            with self.subTest(value=value), self.assertRaises(InvalidRunIdError):
                normalize_run_id(value)


class PublicProvenanceTests(unittest.TestCase):
    def test_gpu_probe_never_requests_or_records_uuid(self):
        calls = []

        def command(argv, _cwd, timeout_s):
            calls.append((argv, timeout_s))
            if argv[0] == "nvidia-smi" and len(argv) > 1:
                return True, "0, Test GPU, 24576, 999.0", None
            return True, "CUDA Version: 13.0", None

        with mock.patch("engine.provenance._run_command", side_effect=command):
            info = collect_gpu_info()
        self.assertEqual(info["status"], "available")
        self.assertNotIn("uuid", info["devices"][0])
        self.assertNotIn("uuid", " ".join(calls[0][0]).lower())

    def test_model_summary_contains_logical_identity_only(self):
        models = collect_bloc_models(build_effective_config(public_config()))
        self.assertEqual(models[0]["endpoint_id"], "ollama-local")
        self.assertEqual(models[0]["device_slot"], "logical-device-0")
        self.assertNotIn("base_url_host", models[0])
        self.assertNotIn("gpu_uuid", models[0])

    def test_run_meta_is_public_native_and_config_hash_is_exact(self):
        config = build_effective_config(public_config())
        gpu_info = {
            "status": "available",
            "error": None,
            "driver_version": "999.0",
            "cuda_version": "13.0",
            "cuda_probe_status": "available",
            "cuda_probe_error": None,
            "malformed_device_rows": 0,
            "devices": [{
                "index": "0",
                "name": "Test GPU",
                "memory_total_mib": "24576",
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "engine.provenance.collect_gpu_info", return_value=gpu_info
        ), mock.patch(
            "engine.provenance.collect_git_info",
            return_value={
                "git_sha": "a" * 40,
                "git_dirty": False,
                "git_probe_status": "available",
                "git_probe_errors": [],
            },
        ):
            lifecycle = RunLifecycle.create(config, output_root=Path(temp_dir))
            meta = json.loads(
                (lifecycle.output_dir / "run_meta.json").read_text(encoding="utf-8")
            )
        self.assertEqual(meta["config"], config)
        self.assertEqual(meta["config_hash"], compute_config_hash(config))
        self.assertEqual(meta["config_hash_algorithm"], "sha256-canonical-json-v1")
        self.assertEqual(
            meta["execution_identity_policy"], "logical-endpoints-only-v1"
        )
        for key in ("hostname", "cuda_visible_devices", "gpu_uuid", "base_url"):
            self.assertFalse(contains_key(meta, key), key)


if __name__ == "__main__":
    unittest.main()
