import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from engine.config import build_effective_config
from tools import run_public_disaster_matrix as launcher
from tools import public_disaster_matrix_worker as worker
from tools.build_public_disaster_matrix import (
    COMPOSITIONS,
    LOG_SCHEMA_VERSION,
    MODES,
    OUTPUT_DIR,
    PRIOR_OUTPUT_DIR,
    PROTOCOL_VERSION,
    RESPONSE_CONTRACT_VERSION,
    SEEDS,
    SERVER_LAYOUT,
    build_files,
)
from tools.public_disaster_matrix_worker import (
    runtime_values_absent,
    select_worker_rows,
)
from tools.run_public_vllm import (
    PublicVllmError,
    _load_json_object,
    _tree_digest,
    validate_runtime_lock,
)
from tools.scan_publication import scan_tree
from tools.verify_repository import _public_config_paths


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "runtime" / "vllm-runtime-lock.json"


class PublicDisasterMatrixContractTests(unittest.TestCase):
    def setUp(self):
        self.files = build_files()
        self.manifest = json.loads(self.files["manifest.json"])

    def test_matrix_is_exactly_four_by_three_by_five(self):
        rows = self.manifest["rows"]
        self.assertEqual(SEEDS, (3101, 3102, 3103, 3104, 3105))
        self.assertEqual(self.manifest["planned_runs"], 60)
        self.assertEqual(self.manifest["planned_logical_llm_calls"], 144000)
        self.assertEqual(self.manifest["planned_http_attempts"], 144000)
        self.assertEqual(
            self.manifest["validation_gate_version"],
            "public-strict-gate-v1.1.0",
        )
        self.assertEqual(
            self.manifest["contingency_ceiling_logical_llm_calls"],
            144000,
        )
        self.assertEqual(
            self.manifest["contingency_ceiling_http_attempts"],
            144000,
        )
        self.assertEqual(
            {
                (row["composition"], row["communication_mode"], row["seed"])
                for row in rows
            },
            {
                (composition, mode, seed)
                for composition in COMPOSITIONS
                for mode in MODES
                for seed in SEEDS
            },
        )
        self.assertEqual(len({row["run_id"] for row in rows}), 60)

    def test_every_config_is_canonical_research_eligible_and_hash_bound(self):
        for row in self.manifest["rows"]:
            payload = self.files[row["filename"]]
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])
            raw_config = json.loads(payload)
            config = build_effective_config(raw_config)
            simulation = config["simulation"]
            self.assertEqual(simulation["protocol_version"], PROTOCOL_VERSION)
            self.assertEqual(
                simulation["response_contract_version"],
                RESPONSE_CONTRACT_VERSION,
            )
            self.assertEqual(simulation["log_schema_version"], LOG_SCHEMA_VERSION)
            self.assertTrue(simulation["research_eligible"])
            self.assertEqual(simulation["duration"], 60)
            self.assertEqual(config["llm_defaults"]["max_tokens"], 512)
            self.assertEqual(sum(bloc["num_agents"] for bloc in config["blocs"]), 24)
            self.assertTrue(all(bloc["flashinfer_mode"] == "disabled" for bloc in config["blocs"]))
            self.assertTrue(
                all("llm_overrides" not in bloc for bloc in raw_config["blocs"])
            )
            expected = 1440 if row["communication_mode"] == "communication_none" else 2880
            self.assertEqual(row["expected_logical_llm_calls"], expected)
            self.assertEqual(row["expected_http_attempts"], expected)

    def test_six_gpu_layout_has_shared_gemma_tp2_only(self):
        self.assertEqual(self.manifest["maximum_gpu_count"], 6)
        self.assertEqual(len(SERVER_LAYOUT), 5)
        self.assertEqual(
            {ordinal for server in SERVER_LAYOUT for ordinal in server["gpu_ordinals"]},
            set(range(6)),
        )
        gemma = next(server for server in SERVER_LAYOUT if server["model_name"] == "gemma")
        self.assertEqual(gemma["tensor_parallel_size"], 2)
        self.assertEqual(
            set(gemma["logical_endpoint_ids"]),
            {"worker-a-gemma", "worker-b-gemma"},
        )
        for row in self.manifest["rows"]:
            config = json.loads(self.files[row["filename"]])
            for bloc in config["blocs"]:
                self.assertEqual(
                    bloc["tensor_parallel_size"],
                    2 if bloc["name"] == "gemma" else 1,
                )

    def test_worker_slices_are_disjoint_exact_halves(self):
        a = select_worker_rows(self.manifest, "a")
        b = select_worker_rows(self.manifest, "b")
        self.assertEqual(len(a), 30)
        self.assertEqual(len(b), 30)
        self.assertEqual(sum(row["expected_logical_llm_calls"] for row in a), 72000)
        self.assertEqual(sum(row["expected_logical_llm_calls"] for row in b), 72000)
        self.assertFalse({row["run_id"] for row in a} & {row["run_id"] for row in b})

    def test_paired_communication_conditions_use_one_replica_per_seed(self):
        rows = self.manifest["rows"]
        for seed in SEEDS:
            for composition in COMPOSITIONS:
                selected = [
                    row
                    for row in rows
                    if row["seed"] == seed and row["composition"] == composition
                ]
                self.assertEqual(len(selected), 3)
                self.assertEqual(len({row["worker_slot"] for row in selected}), 1)
                scenarios = []
                for row in selected:
                    scenario = dict(
                        json.loads(self.files[row["filename"]])["scenario"]
                    )
                    scenario.pop("communication_mode")
                    scenarios.append(json.dumps(scenario, sort_keys=True))
                self.assertEqual(len(set(scenarios)), 1)

    def test_generated_public_configs_are_scanner_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in self.files.items():
                (root / name).write_bytes(payload)
            self.assertEqual(scan_tree(root), [])

    def test_repository_verifier_discovers_all_nested_matrix_configs(self):
        paths = _public_config_paths(REPO_ROOT)
        matrix_paths = [path for path in paths if path.parent == OUTPUT_DIR]
        self.assertEqual(len(matrix_paths), 60)
        self.assertNotIn(OUTPUT_DIR / "manifest.json", paths)

    def test_prior_v3_matrix_remains_hash_complete_and_immutable(self):
        manifest_path = PRIOR_OUTPUT_DIR / "manifest.json"
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(prior["protocol_version"], "formal-public-disaster-protocol-v3.0.0")
        self.assertEqual(prior["planned_runs"], 60)
        self.assertEqual(prior["planned_logical_llm_calls"], 144000)
        for row in prior["rows"]:
            payload = (PRIOR_OUTPUT_DIR / row["filename"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), row["sha256"])

    def test_v3_1_configs_change_only_declared_amendment_fields(self):
        for row in self.manifest["rows"]:
            current = json.loads(self.files[row["filename"]])
            prior_filename = row["filename"].replace(
                "public-disaster-v3p1-",
                "public-disaster-v3-",
                1,
            )
            prior = json.loads(
                (PRIOR_OUTPUT_DIR / prior_filename).read_text(encoding="utf-8")
            )

            current["llm_defaults"]["max_tokens"] = prior["llm_defaults"]["max_tokens"]
            for field in ("protocol_version", "run_id", "run_name"):
                current["simulation"][field] = prior["simulation"][field]

            self.assertEqual(current, prior)


class PublicDisasterLauncherTests(unittest.TestCase):
    def load_contract(self):
        manifest = json.loads(build_files()["manifest.json"])
        lock = _load_json_object(LOCK_PATH)
        validate_runtime_lock(lock)
        configs, blocs = launcher.load_matrix_configs(manifest, lock)
        return manifest, configs, blocs

    def test_server_specs_use_exact_arbitrary_six_gpu_scope(self):
        _manifest, _configs, blocs = self.load_contract()
        specs = launcher.build_server_specs(blocs, (7, 6, 5, 4, 3, 2), 19000)
        self.assertEqual(len(specs), 5)
        self.assertEqual([spec.port for spec in specs], list(range(19000, 19005)))
        self.assertEqual(
            [spec.gpu_indices for spec in specs],
            [(7,), (6,), (5,), (4,), (3, 2)],
        )

    def test_runtime_bindings_share_only_gemma_server(self):
        _manifest, _configs, blocs = self.load_contract()
        specs = launcher.build_server_specs(blocs, tuple(range(6)), 19000)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bindings.yaml"
            launcher.write_matrix_bindings(path, specs)
            endpoints = yaml.safe_load(path.read_text(encoding="utf-8"))["endpoints"]
        self.assertEqual(
            endpoints["worker-a-gemma"]["base_url"],
            endpoints["worker-b-gemma"]["base_url"],
        )
        self.assertNotEqual(
            endpoints["worker-a-qwen"]["base_url"],
            endpoints["worker-b-qwen"]["base_url"],
        )
        self.assertNotEqual(
            endpoints["worker-a-llama"]["base_url"],
            endpoints["worker-b-llama"]["base_url"],
        )

    def test_runtime_binding_byte_check_is_recursive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.json").write_text('{"value":"safe"}\n', encoding="utf-8")
            self.assertTrue(runtime_values_absent(root, (b"http://127.0.0.1:19000",)))
            nested = root / "nested"
            nested.mkdir()
            (nested / "raw.jsonl").write_text(
                '{"value":"http://127.0.0.1:19000"}\n', encoding="utf-8"
            )
            self.assertFalse(runtime_values_absent(root, (b"http://127.0.0.1:19000",)))

    def test_strict_unverifiable_is_recorded_without_becoming_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "output_run-a"
            run_dir.mkdir()
            source_sha = "a" * 40
            config = {
                "simulation": {"duration": 60, "research_eligible": True}
            }
            meta = {
                "run_id": "run-a",
                "status": "completed",
                "aborted": False,
                "expected_steps": 60,
                "completed_steps": 60,
                "expected_agents": 24,
                "observed_agents": 24,
                "logical_llm_calls": 1440,
                "http_attempts": 1440,
                "git_sha": source_sha,
                "git_dirty": False,
                "raw_manifest_status": "available",
                "response_contract_version": "phase-response-v2.0.0",
                "log_schema_version": "2.0.0",
                "generation_retries": 0,
                "transport_failures": 0,
                "syntax_parse_attempt_failures": 0,
                "syntax_parse_failures": 0,
                "schema_validation_failures": 0,
                "config": config,
            }
            (run_dir / "run_meta.json").write_text(
                json.dumps(meta) + "\n",
                encoding="utf-8",
            )
            row = {
                "run_id": "run-a",
                "sha256": "b" * 64,
                "expected_logical_llm_calls": 1440,
                "expected_http_attempts": 1440,
            }
            with mock.patch.object(
                worker,
                "validate_run",
                return_value=SimpleNamespace(
                    valid=True,
                    unverifiable=["known epistemic limitation"],
                ),
            ), mock.patch.object(worker, "scan_tree", return_value=[]):
                result = worker.verify_one_run(
                    run_dir,
                    row,
                    config,
                    source_sha,
                    (b"http://127.0.0.1:19000",),
                )
            self.assertTrue(result["strict_validation_passed"])
            self.assertEqual(result["strict_unverifiable_count"], 1)
            self.assertEqual(len(result["strict_unverifiable_sha256"]), 64)

    def make_staged_runs(self, root: Path):
        stage = root / "stage"
        for slot, run_id in (("a", "run-a"), ("b", "run-b")):
            run_dir = stage / f"worker-{slot}" / "runs" / f"output_{run_id}"
            run_dir.mkdir(parents=True)
            (run_dir / "raw.jsonl").write_text(
                json.dumps({"run_id": run_id}) + "\n",
                encoding="utf-8",
            )
        verified = [
            {
                "run_id": run_id,
                "run_tree_sha256": _tree_digest(
                    stage / f"worker-{slot}" / "runs" / f"output_{run_id}"
                ),
            }
            for slot, run_id in (("a", "run-a"), ("b", "run-b"))
        ]
        return stage, verified

    def test_promotion_moves_verified_bytes_only_after_all_collision_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, verified = self.make_staged_runs(root)
            output = root / "runs"
            output.mkdir()
            collision = output / "output_run-b"
            collision.mkdir()
            with self.assertRaisesRegex(PublicVllmError, "collision"):
                launcher.promote_runs(stage, output, verified)
            self.assertTrue(
                (stage / "worker-a" / "runs" / "output_run-a").is_dir()
            )
            self.assertFalse((output / "output_run-a").exists())

    def test_promotion_rolls_back_if_byte_digest_does_not_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage, verified = self.make_staged_runs(root)
            verified[0]["run_tree_sha256"] = "0" * 64
            output = root / "runs"
            output.mkdir()
            with self.assertRaisesRegex(PublicVllmError, "bytes changed"):
                launcher.promote_runs(stage, output, verified)
            for slot, run_id in (("a", "run-a"), ("b", "run-b")):
                self.assertTrue(
                    (stage / f"worker-{slot}" / "runs" / f"output_{run_id}").is_dir()
                )
                self.assertFalse((output / f"output_{run_id}").exists())

    def test_contract_only_cli_does_not_touch_gpu_runtime(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = launcher.main([
                "--source-git-sha",
                "0" * 40,
                "--contract-only",
            ])
        self.assertEqual(code, 0)
        self.assertIn("60-run matrix contract", output.getvalue())

    def test_cli_rejects_wall_limit_above_eight_hours(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = launcher.main([
                "--source-git-sha",
                "0" * 40,
                "--wall-timeout-s",
                str(launcher.MAXIMUM_WALL_TIMEOUT_S + 1),
                "--contract-only",
            ])
        self.assertEqual(code, 2)
        self.assertIn("timeout", output.getvalue())


if __name__ == "__main__":
    unittest.main()
