import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from engine.llm_client import LLMTransportError
from engine.sim import Simulation
from tests.gate3_fixtures import (
    REPO_ROOT,
    patched_gate3_environment,
    read_jsonl,
    tree_hashes,
    write_plan_fixture,
)
from tools.eight_cell_core import (
    CELL_DEFINITIONS,
    PlanValidationError,
    load_plan,
    sha256_file,
    write_static_bundle,
)
from tools.eight_cell_runner import (
    BatchCollisionError,
    BatchExecutionError,
    ScriptedSmokeTransport,
    run_smoke_batch,
)
from tools.research_validator import validate_batch_profile

HAS_GIT_HEAD = subprocess.run(
    ["git", "rev-parse", "--verify", "HEAD"],
    cwd=REPO_ROOT,
    check=False,
    capture_output=True,
    timeout=10,
).returncode == 0


class FailingSecondCellTransport:
    def __init__(self):
        self.delegate = ScriptedSmokeTransport()
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, request, telemetry):
        with self.lock:
            self.calls += 1
            call = self.calls
        if call == 25:
            telemetry("http_attempt", 1)
            telemetry("transport_failure", 1)
            raise LLMTransportError("injected Gate 3 failure")
        return self.delegate(request, telemetry)


class EightCellRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def rewrite_plan(self, plan_path: Path, mutator):
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        mutator(plan)
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return sha256_file(plan_path)

    def assert_plan_invalid(self, mutator, pattern=None):
        case_root = self.root / f"invalid-{len(list(self.root.iterdir()))}"
        case_root.mkdir()
        plan_path, _, _, _ = write_plan_fixture(case_root)
        plan_sha = self.rewrite_plan(plan_path, mutator)
        with self.assertRaises(PlanValidationError) as raised:
            load_plan(plan_path, plan_sha)
        if pattern:
            self.assertIn(pattern, str(raised.exception))

    def test_plan_rejects_hash_duplicate_unknown_and_schema_errors(self):
        plan_path, plan_sha, _, _ = write_plan_fixture(self.root)
        with self.assertRaisesRegex(PlanValidationError, "plan SHA-256 mismatch"):
            load_plan(plan_path, "0" * 64)
        duplicate_path = self.root / "duplicate.json"
        text = plan_path.read_text(encoding="utf-8")
        duplicate_path.write_text(
            text.replace(
                '"schema_version": "eight-cell-matrix-plan-v1.1.0",',
                '"schema_version": "eight-cell-matrix-plan-v1.1.0",\n'
                '  "schema_version": "eight-cell-matrix-plan-v1.1.0",',
                1,
            ),
            encoding="utf-8",
            newline="\n",
        )
        with self.assertRaisesRegex(PlanValidationError, "duplicate JSON key"):
            load_plan(duplicate_path, sha256_file(duplicate_path))
        self.assert_plan_invalid(lambda plan: plan.update({"extra": True}), "unknown")
        self.assert_plan_invalid(
            lambda plan: plan.update({"schema_version": "wrong"}), "schema"
        )
        self.assert_plan_invalid(
            lambda plan: plan.update({"protocol_version": "unversioned"}),
            "protocol",
        )
        self.assert_plan_invalid(
            lambda plan: plan.update({"metric_version": "metric-v1"}), "metric"
        )

    def test_plan_execution_mode_is_required_validated_hashed_and_authoritative(self):
        scripted_root = self.root / "scripted"
        reference_root = self.root / "reference"
        scripted_root.mkdir()
        reference_root.mkdir()
        _, scripted_sha, _, scripted = write_plan_fixture(scripted_root)
        _, reference_sha, _, reference = write_plan_fixture(
            reference_root, execution_mode="reference_ollama"
        )
        self.assertNotEqual(scripted_sha, reference_sha)
        self.assertEqual(scripted.plan["execution_mode"], "scripted_smoke")
        self.assertEqual(reference.plan["execution_mode"], "reference_ollama")
        self.assertTrue(
            all(row["execution_mode"] == "reference_ollama" for row in reference.rows)
        )
        self.assertTrue(
            all(
                config["simulation"]["execution_mode"] == "reference_ollama"
                for config in reference.configs.values()
            )
        )
        self.assert_plan_invalid(
            lambda plan: plan.pop("execution_mode"), "missing=execution_mode"
        )
        self.assert_plan_invalid(
            lambda plan: plan.update({"execution_mode": "unknown"}),
            "execution_mode",
        )
        self.assert_plan_invalid(
            lambda plan: plan.update({"execution_mode": 1}),
            "execution_mode",
        )

        transport = ScriptedSmokeTransport()
        output = self.root / "reference-output"
        with self.assertRaisesRegex(PlanValidationError, "requires plan execution_mode"):
            run_smoke_batch(
                reference,
                output,
                repo_root=REPO_ROOT,
                transport=transport,
            )
        self.assertEqual(transport.call_count, 0)
        self.assertFalse(output.exists())

    def test_plan_rejects_base_catalog_replicate_and_freeze_errors(self):
        self.assert_plan_invalid(
            lambda plan: plan["base_config"].update({"sha256": "0" * 64}),
            "base config SHA-256 mismatch",
        )
        self.assert_plan_invalid(
            lambda plan: plan["model_catalog"].pop("qwen"), "model_catalog"
        )
        self.assert_plan_invalid(
            lambda plan: plan["model_catalog"].update(
                {"extra": copy.deepcopy(plan["model_catalog"]["qwen"])}
            ),
            "model_catalog",
        )
        self.assert_plan_invalid(
            lambda plan: plan["replicates"].append(
                copy.deepcopy(plan["replicates"][0])
            ),
            "duplicate replicate_id",
        )
        self.assert_plan_invalid(
            lambda plan: plan["replicates"][0].update({"world_seed": True}),
            "world_seed",
        )
        self.assert_plan_invalid(
            lambda plan: plan.update({"matrix_id": "../unsafe"}), "matrix_id"
        )
        self.assert_plan_invalid(
            lambda plan: plan["candidate_registry"].update(
                {"status": "frozen", "sha256": None}
            ),
            "candidate_registry",
        )
        self.assert_plan_invalid(
            lambda plan: plan["backend_freeze"].update(
                {"status": "frozen", "evidence_id": None}
            ),
            "backend_freeze",
        )

    def test_base_bloc_structure_is_fixed(self):
        plan_path, plan_sha, _, _ = write_plan_fixture(self.root)
        base_path = self.root / "base_config.json"
        base = json.loads(base_path.read_text(encoding="utf-8"))
        base["blocs"][0]["num_agents"] = 3
        base_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
        base_sha = sha256_file(base_path)
        plan_sha = self.rewrite_plan(
            plan_path, lambda plan: plan["base_config"].update({"sha256": base_sha})
        )
        with self.assertRaisesRegex(PlanValidationError, "4 agents each"):
            load_plan(plan_path, plan_sha)

    def test_fixed_cells_rotations_homogeneous_and_paired_hashes(self):
        replicates = [
            {"replicate_id": f"r{index:03d}", "world_seed": 1000 + index}
            for index in range(4)
        ]
        _, _, _, bundle = write_plan_fixture(
            self.root, replicates=replicates
        )
        self.assertEqual(len(bundle.rows), 32)
        for replicate_index in range(4):
            rows = bundle.rows[replicate_index * 8:(replicate_index + 1) * 8]
            self.assertEqual(
                [(row["cell_id"], row["model_condition"], row["edge_policy"])
                 for row in rows],
                list(CELL_DEFINITIONS),
            )
            self.assertEqual(len({row["paired_control_hash"] for row in rows}), 1)
            self.assertEqual(
                len({row["initial_state_input_hash"] for row in rows}), 1
            )
            het_slots = rows[0]["model_slots_by_bloc"]
            expected_rotations = (
                {"alpha": "qwen", "beta": "gemma", "neutral": "llama"},
                {"alpha": "gemma", "beta": "llama", "neutral": "qwen"},
                {"alpha": "llama", "beta": "qwen", "neutral": "gemma"},
            )
            self.assertEqual(het_slots, expected_rotations[replicate_index % 3])
            for row, slot in ((rows[2], "qwen"), (rows[4], "gemma"), (rows[6], "llama")):
                self.assertEqual(set(row["model_slots_by_bloc"].values()), {slot})

    def test_paired_configs_produce_identical_initial_positions(self):
        _, _, _, bundle = write_plan_fixture(self.root)
        output = self.root / "positions"
        output.mkdir()
        positions = []
        with patched_gate3_environment():
            for row in bundle.rows:
                simulation = Simulation(
                    copy.deepcopy(bundle.configs[row["run_id"]]),
                    output_root=output,
                    repo_root=REPO_ROOT,
                    transport=ScriptedSmokeTransport(),
                )
                positions.append([agent.position for agent in simulation.agents])
        self.assertTrue(all(value == positions[0] for value in positions))

    def test_static_bundle_is_byte_identical_across_roots(self):
        _, _, _, bundle = write_plan_fixture(self.root)
        first = self.root / "static-one"
        second = self.root / "static-two"
        first.mkdir()
        second.mkdir()
        write_static_bundle(first, bundle)
        write_static_bundle(second, bundle)
        self.assertEqual(tree_hashes(first), tree_hashes(second))

    def test_eight_cell_smoke_manifest_policies_and_sequential_collision(self):
        _, _, _, bundle = write_plan_fixture(self.root)
        output = self.root / "batches"
        transport = ScriptedSmokeTransport()
        with patched_gate3_environment():
            batch_dir = run_smoke_batch(
                bundle,
                output,
                repo_root=REPO_ROOT,
                transport=transport,
            )
        self.assertEqual(transport.call_count, 8 * 12 * 2)
        report = validate_batch_profile(batch_dir, "smoke")
        self.assertEqual(report.exit_code, 0, report.errors)
        manifest = json.loads(
            (batch_dir / "batch_manifest.json").read_text(encoding="utf-8")
        )
        plan = json.loads((batch_dir / "plan.json").read_text(encoding="utf-8"))
        meta = json.loads(
            (batch_dir / "batch_meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["planned_runs"], 8)
        self.assertEqual(manifest["completed_runs"], 8)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(
            plan["schema_version"], "eight-cell-matrix-plan-v1.1.0"
        )
        self.assertEqual(
            manifest["schema_version"],
            "eight-cell-batch-manifest-v1.1.0",
        )
        self.assertEqual(
            manifest["matrix_spec_version"], "eight-cell-matrix-v1.1.1"
        )
        self.assertEqual(plan["execution_mode"], "scripted_smoke")
        self.assertEqual(meta["execution_mode"], "scripted_smoke")
        self.assertFalse(meta["research_eligible"])
        self.assertEqual(manifest["execution_mode"], "scripted_smoke")
        self.assertFalse(manifest["research_eligible"])
        self.assertTrue(
            all(row["execution_mode"] == "scripted_smoke" for row in manifest["runs"])
        )
        self.assertTrue(
            all(row["research_eligible"] is False for row in manifest["runs"])
        )
        labels = {index: ("alpha", "beta", "neutral")[index // 4] for index in range(12)}
        for row in bundle.rows:
            config = json.loads(
                (batch_dir / row["config_path"]).read_text(encoding="utf-8")
            )
            run_meta = json.loads(
                (
                    batch_dir
                    / "runs"
                    / f"output_{row['run_id']}"
                    / "run_meta.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                config["simulation"]["execution_mode"], "scripted_smoke"
            )
            self.assertEqual(
                run_meta["config"]["simulation"]["execution_mode"],
                "scripted_smoke",
            )
            messages = read_jsonl(
                batch_dir / "runs" / f"output_{row['run_id']}" / "messages.jsonl"
            )
            self.assertTrue(messages)
            cross = [
                (message["sender_id"], receiver)
                for message in messages
                for receiver in message["receiver_ids"]
                if labels[message["sender_id"]] != labels[receiver]
            ]
            if row["edge_policy"] == "full":
                self.assertTrue(cross)
            else:
                self.assertFalse(cross)
            self.assertTrue(
                all(message["receiver_ids"] == sorted(message["receiver_ids"])
                    for message in messages)
            )
        before = tree_hashes(batch_dir)
        loser = ScriptedSmokeTransport()
        with self.assertRaises(BatchCollisionError):
            run_smoke_batch(bundle, output, repo_root=REPO_ROOT, transport=loser)
        self.assertEqual(loser.call_count, 0)
        self.assertEqual(tree_hashes(batch_dir), before)

    def test_controlled_failure_retains_all_rows_and_blocks_retry(self):
        _, _, _, bundle = write_plan_fixture(
            self.root, matrix_id="gate3-failure"
        )
        output = self.root / "failed-batches"
        with patched_gate3_environment():
            with self.assertRaises(BatchExecutionError):
                run_smoke_batch(
                    bundle,
                    output,
                    repo_root=REPO_ROOT,
                    transport=FailingSecondCellTransport(),
                )
        batch_dir = output / "batch_gate3-failure"
        manifest = json.loads(
            (batch_dir / "batch_manifest.json").read_text(encoding="utf-8")
        )
        statuses = [row["status"] for row in manifest["runs"]]
        self.assertEqual(statuses[0], "completed")
        self.assertEqual(statuses[1], "aborted")
        self.assertEqual(statuses[2:], ["not_started"] * 6)
        self.assertEqual(manifest["status"], "aborted")
        before = tree_hashes(batch_dir)
        with self.assertRaises(BatchCollisionError):
            run_smoke_batch(bundle, output, repo_root=REPO_ROOT)
        self.assertEqual(tree_hashes(batch_dir), before)

    @unittest.skipUnless(HAS_GIT_HEAD, "requires a committed repository in child processes")
    def test_concurrent_cli_claim_has_one_owner_and_one_collision(self):
        plan_path, plan_sha, spec_sha, _ = write_plan_fixture(
            self.root, matrix_id="gate3-concurrent"
        )
        output = self.root / "concurrent-batches"
        command = [
            sys.executable,
            "-m",
            "tools.eight_cell_runner",
            "smoke",
            "--plan",
            str(plan_path),
            "--plan-sha256",
            plan_sha,
            "--matrix-spec-sha256",
            spec_sha,
            "--output-root",
            str(output),
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=60) for process in processes]
        codes = sorted(process.returncode for process in processes)
        self.assertEqual(codes, [0, 3], results)

    @unittest.skipUnless(HAS_GIT_HEAD, "requires a committed repository in child processes")
    def test_runner_cli_exit_codes_one_two_and_sixty_four(self):
        plan_path, plan_sha, spec_sha, _ = write_plan_fixture(
            self.root, matrix_id="gate3-cli"
        )
        base_command = [
            sys.executable,
            "-m",
            "tools.eight_cell_runner",
            "smoke",
            "--plan",
            str(plan_path),
            "--plan-sha256",
            plan_sha,
            "--matrix-spec-sha256",
            spec_sha,
            "--output-root",
            str(self.root / "cli-batches"),
        ]
        invalid_plan = base_command.copy()
        invalid_plan[invalid_plan.index(plan_sha)] = "0" * 64
        invalid_result = subprocess.run(
            invalid_plan,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        invocation_result = subprocess.run(
            [sys.executable, "-m", "tools.eight_cell_runner"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(invalid_result.returncode, 2, invalid_result.stderr)
        self.assertEqual(invocation_result.returncode, 64, invocation_result.stderr)

        base_path = self.root / "base_config.json"
        base = json.loads(base_path.read_text(encoding="utf-8"))
        base["agents"]["communication_radius"] = 0
        base_path.write_text(json.dumps(base) + "\n", encoding="utf-8")
        base_sha = sha256_file(base_path)
        plan_sha = self.rewrite_plan(
            plan_path,
            lambda plan: plan["base_config"].update({"sha256": base_sha}),
        )
        failed_command = base_command.copy()
        failed_command[failed_command.index("--plan-sha256") + 1] = plan_sha
        failed_result = subprocess.run(
            failed_command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(failed_result.returncode, 1, failed_result.stderr + failed_result.stdout)


if __name__ == "__main__":
    unittest.main()
