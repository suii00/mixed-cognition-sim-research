import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from engine.config import load_config
from tools import run_public_vllm as launcher


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "configs" / "public_vllm_smoke_3model.json"
LOCK_PATH = REPO_ROOT / "runtime" / "vllm-runtime-lock.json"


def gpu_row(index: int, used: int = 1) -> launcher.GpuRow:
    return launcher.GpuRow(
        index=index,
        name="NVIDIA Test GPU",
        memory_used_mib=used,
        memory_total_mib=24564,
    )


class PublicVllmContractTests(unittest.TestCase):
    def load_contract(self):
        lock = launcher._load_json_object(LOCK_PATH)
        config = load_config(str(CONFIG_PATH))
        launcher.validate_runtime_lock(lock)
        launcher.validate_vllm_config(config, lock)
        return config, lock

    def test_representative_config_matches_artifact_model_context_and_contract(self):
        config, _lock = self.load_contract()
        self.assertEqual(
            {bloc["model_source"] for bloc in config["blocs"]},
            {
                "Qwen/Qwen2.5-7B-Instruct",
                "meta-llama/Llama-3.1-8B-Instruct",
                "google/gemma-2-9b-it",
            },
        )
        self.assertEqual(
            {bloc["max_model_len"] for bloc in config["blocs"]},
            {4096},
        )
        self.assertEqual(
            config["simulation"]["response_contract_version"],
            "phase-response-v2.0.0",
        )
        self.assertEqual(launcher.required_gpu_count(config), 4)
        self.assertNotIn("run_id", config["simulation"])

    def test_runtime_lock_records_exact_flashinfer_disabled_profile(self):
        _config, lock = self.load_contract()
        self.assertEqual(lock["python"]["version"], "3.10.12")
        self.assertEqual(lock["packages"]["vllm"], "0.27.1")
        self.assertEqual(
            lock["packages"]["flashinfer-python"],
            "0.6.16.post3",
        )
        self.assertEqual(
            lock["execution_contract"]["flashinfer_mode"],
            "installed-but-disabled-before-import",
        )
        self.assertEqual(lock["execution_contract"]["max_gpu_count"], 6)

    def test_runtime_check_rejects_any_version_drift(self):
        _config, lock = self.load_contract()
        versions = dict(lock["packages"])
        versions["vllm"] = "different"
        with mock.patch.object(
            launcher.platform,
            "python_implementation",
            return_value="CPython",
        ), mock.patch.object(
            launcher.platform,
            "python_version",
            return_value="3.10.12",
        ), mock.patch.object(
            launcher.importlib.metadata,
            "version",
            side_effect=lambda name: versions[name],
        ):
            with self.assertRaisesRegex(launcher.PublicVllmError, "exact lock"):
                launcher.check_installed_runtime(lock)

    def test_contract_only_does_not_probe_runtime(self):
        output = io.StringIO()
        with mock.patch.object(
            launcher,
            "require_git_head",
            side_effect=AssertionError("contract-only probed Git state"),
        ) as require_git_head, mock.patch.object(
            launcher,
            "check_installed_runtime",
            side_effect=AssertionError("contract-only probed the installed runtime"),
        ) as check_installed_runtime, mock.patch.object(
            launcher,
            "query_gpu_rows",
            side_effect=AssertionError("contract-only probed GPU state"),
        ) as query_gpu_rows, redirect_stdout(output):
            result = launcher.main([
                "--config",
                str(CONFIG_PATH),
                "--runtime-lock",
                str(LOCK_PATH),
                "--contract-only",
            ])
        self.assertEqual(result, 0)
        self.assertIn("internally consistent", output.getvalue())
        require_git_head.assert_not_called()
        check_installed_runtime.assert_not_called()
        query_gpu_rows.assert_not_called()


class PublicVllmAllocationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(str(CONFIG_PATH))

    def test_default_allocation_is_exact_and_tensor_parallel_aware(self):
        indices = launcher.parse_gpu_indices(None, 4, 6)
        specs = launcher.build_endpoint_specs(self.config, indices, 18100)
        self.assertEqual([spec.gpu_indices for spec in specs], [(0,), (1,), (2, 3)])
        self.assertEqual([spec.port for spec in specs], [18100, 18101, 18102])

    def test_allocation_rejects_more_than_six_gpus(self):
        with self.assertRaisesRegex(launcher.PublicVllmError, "ceiling"):
            launcher.parse_gpu_indices("0,1,2,3,4,5,6", 7, 6)

    def test_allocation_rejects_duplicates_and_noncanonical_values(self):
        with self.assertRaisesRegex(launcher.PublicVllmError, "distinct"):
            launcher.parse_gpu_indices("0,1,1,2", 4, 6)
        for value in ("00,1,2,3", "0, 1,2,3", "-1,0,1,2"):
            with self.subTest(value=value), self.assertRaises(
                launcher.PublicVllmError
            ):
                launcher.parse_gpu_indices(value, 4, 6)

    def test_gpu_guard_fails_closed_on_unselected_activation(self):
        baseline = {index: gpu_row(index) for index in range(8)}
        guard = launcher.GpuGuard(baseline, frozenset({0, 1, 2, 3}), 6)
        selected_active = dict(baseline)
        for index in range(4):
            selected_active[index] = gpu_row(index, 16000)
        guard.observe(selected_active)
        self.assertEqual(guard.max_observed_active_gpu_count, 4)
        escaped = dict(selected_active)
        escaped[7] = gpu_row(7, 16000)
        with self.assertRaisesRegex(launcher.PublicVllmError, "escaped"):
            guard.observe(escaped)

    def test_gpu_guard_verifies_release_against_baseline(self):
        baseline = {index: gpu_row(index) for index in range(4)}
        guard = launcher.GpuGuard(baseline, frozenset(range(4)), 6)
        self.assertTrue(guard.released(baseline))
        retained = dict(baseline)
        retained[2] = gpu_row(2, launcher.GPU_RELEASE_DELTA_MIB + 2)
        self.assertFalse(guard.released(retained))


class PublicVllmProcessBoundaryTests(unittest.TestCase):
    def make_spec(self, snapshot: Path) -> launcher.EndpointSpec:
        return launcher.EndpointSpec(
            endpoint_id="logical-endpoint",
            served_model_name="public-model",
            model_source="example/model",
            model_digest="a" * 40,
            dtype="bfloat16",
            max_model_len=4096,
            tensor_parallel_size=1,
            gpu_memory_utilization=0.9,
            port=18100,
            gpu_indices=(0,),
            snapshot=snapshot,
        )

    def test_server_command_uses_dot_model_loopback_and_no_request_logging(self):
        spec = self.make_spec(Path("model-cache") / "snapshot")
        command = launcher.build_server_command(spec)
        self.assertEqual(command[command.index("--model") + 1], ".")
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertIn("--no-enable-log-requests", command)
        self.assertNotIn(str(spec.snapshot), command)

    def test_child_environment_is_allowlisted_offline_and_ephemeral(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shadow = root / "shadow"
            shadow.mkdir()
            source_environment = {
                "PATH": "safe-path",
                "HOME": "safe-home",
                "HF_TOKEN": "must-not-pass",
                "SSH_AUTH_SOCK": "must-not-pass",
            }
            with mock.patch.dict(os.environ, source_environment, clear=True):
                environment = launcher.build_child_environment(
                    root,
                    shadow,
                    (2, 3),
                )
            self.assertEqual(environment["PATH"], "safe-path")
            self.assertEqual(environment["HOME"], "safe-home")
            self.assertNotIn("HF_TOKEN", environment)
            self.assertNotIn("SSH_AUTH_SOCK", environment)
            self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
            self.assertEqual(environment["CUDA_VISIBLE_DEVICES"], "2,3")
            self.assertTrue(Path(environment["VLLM_CACHE_ROOT"]).is_relative_to(root))

    def test_server_output_is_connected_directly_to_devnull(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            shadow = root / "shadow"
            shadow.mkdir()
            process = mock.Mock()
            with mock.patch.object(
                launcher,
                "build_child_environment",
                return_value={"PATH": "safe"},
            ), mock.patch.object(
                launcher.subprocess,
                "Popen",
                return_value=process,
            ) as popen:
                self.assertIs(
                    launcher.start_server(
                        self.make_spec(snapshot),
                        root,
                        shadow,
                    ),
                    process,
                )
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["cwd"], snapshot)

    def test_flashinfer_shadow_disables_import_without_persisted_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shadow = launcher.write_flashinfer_shadow(root)
            content = (shadow / "flashinfer" / "__init__.py").read_text(
                encoding="utf-8"
            )
            self.assertIn("raise ImportError", content)
            self.assertEqual(list(root.rglob("*.log")), [])

    def test_runtime_binding_values_are_rejected_if_they_enter_run_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = self.make_spec(root / "snapshot")
            (root / "safe.json").write_text('{"endpoint_id":"logical-endpoint"}\n')
            self.assertTrue(launcher.runtime_binding_values_absent(root, [spec]))
            (root / "unsafe.txt").write_text(spec.base_url, encoding="utf-8")
            self.assertFalse(launcher.runtime_binding_values_absent(root, [spec]))


if __name__ == "__main__":
    unittest.main()
