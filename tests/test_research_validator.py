import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.provenance import (
    build_raw_manifest,
    compute_config_hash,
    file_manifest,
)
from tests.gate3_fixtures import (
    REPO_ROOT,
    make_synthetic_research_batch,
    patched_gate3_environment,
    tree_hashes,
    write_plan_fixture,
)
from tools.eight_cell_core import (
    canonical_json_file_bytes,
    paired_control_hash,
    sha256_file,
)
from tools.eight_cell_runner import run_smoke_batch
from tools.research_validator import (
    validate_batch_profile,
    validate_run_profile,
)


class ResearchValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared_temp = tempfile.TemporaryDirectory()
        cls.shared_root = Path(cls.shared_temp.name)
        _, _, _, cls.bundle = write_plan_fixture(
            cls.shared_root, matrix_id="gate3-validator"
        )
        with patched_gate3_environment():
            cls.batch_dir = run_smoke_batch(
                cls.bundle,
                cls.shared_root / "batches",
                repo_root=REPO_ROOT,
            )

    @classmethod
    def tearDownClass(cls):
        cls.shared_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def copy_batch(self, name: str) -> Path:
        target = self.root / name / self.batch_dir.name
        target.parent.mkdir(parents=True)
        shutil.copytree(self.batch_dir, target)
        return target

    @staticmethod
    def read_json(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_json(path: Path, value: dict) -> None:
        path.write_bytes(canonical_json_file_bytes(value))

    @staticmethod
    def read_rows(batch: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in (batch / "planned_runs.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    @staticmethod
    def write_rows(batch: Path, rows: list[dict]) -> None:
        (batch / "planned_runs.jsonl").write_bytes(
            b"".join(canonical_json_file_bytes(row) for row in rows)
        )

    def first_run_paths(self, batch: Path) -> tuple[dict, Path, Path]:
        row = self.read_rows(batch)[0]
        config_path = batch / row["config_path"]
        run_dir = batch / "runs" / f"output_{row['run_id']}"
        return row, config_path, run_dir

    def refresh_integrity_evidence(self, batch: Path) -> None:
        plan_path = batch / "plan.json"
        rows_path = batch / "planned_runs.jsonl"
        meta_path = batch / "batch_meta.json"
        plan_manifest_path = batch / "plan_manifest.json"
        batch_manifest_path = batch / "batch_manifest.json"
        rows = self.read_rows(batch)
        for row in rows:
            config = self.read_json(batch / row["config_path"])
            row["config_sha256"] = compute_config_hash(config)
            row["paired_control_hash"] = paired_control_hash(
                config, row["prompt_sha256"]
            )
        self.write_rows(batch, rows)

        meta = self.read_json(meta_path)
        meta["plan_sha256"] = sha256_file(plan_path)
        plan_manifest = self.read_json(plan_manifest_path)
        plan_manifest["source_plan_sha256"] = meta["plan_sha256"]
        for relative in list(plan_manifest["files"]):
            plan_manifest["files"][relative] = file_manifest(batch / relative)
        self.write_json(plan_manifest_path, plan_manifest)
        meta["plan_manifest_sha256"] = sha256_file(plan_manifest_path)

        manifest = self.read_json(batch_manifest_path)
        manifest["plan_sha256"] = meta["plan_sha256"]
        manifest["plan_manifest_sha256"] = meta["plan_manifest_sha256"]
        row_by_id = {row["run_id"]: row for row in rows}
        for manifest_row in manifest["runs"]:
            run_id = manifest_row["run_id"]
            row = row_by_id[run_id]
            manifest_row["config_sha256"] = row["config_sha256"]
            run_meta_path = batch / manifest_row["run_directory"] / "run_meta.json"
            if run_meta_path.is_file():
                run_meta = self.read_json(run_meta_path)
                run_meta["config_hash"] = compute_config_hash(run_meta["config"])
                self.write_json(run_meta_path, run_meta)
                manifest_row["run_meta_manifest"] = file_manifest(run_meta_path)
                manifest_row["raw_manifest"] = run_meta.get("raw_manifest")
        self.write_json(batch_manifest_path, manifest)
        meta["batch_manifest_sha256"] = sha256_file(batch_manifest_path)
        self.write_json(meta_path, meta)

    def read_only_result(self, batch: Path, operation):
        before = tree_hashes(batch)
        result = operation()
        self.assertEqual(before, tree_hashes(batch))
        return result

    def validate_every_run_research(self, batch: Path):
        batch_result = self.read_only_result(
            batch, lambda: validate_batch_profile(batch, "research")
        )
        run_results = {}
        for row in self.read_rows(batch):
            run_dir = batch / "runs" / f"output_{row['run_id']}"
            run_result = self.read_only_result(
                batch,
                lambda row=row, run_dir=run_dir: validate_run_profile(
                    run_dir, batch, row, "research"
                ),
            )
            run_results[row["run_id"]] = run_result
            if run_result.exit_code == 0:
                self.assertEqual(batch_result.exit_code, 0, batch_result.errors)
            if batch_result.exit_code == 3:
                self.assertNotEqual(run_result.exit_code, 0, run_result.errors)
        return batch_result, run_results

    def validate_all_public_profiles_read_only(self, batch: Path):
        row = self.read_rows(batch)[0]
        run_dir = batch / "runs" / f"output_{row['run_id']}"
        return {
            "batch_smoke": self.read_only_result(
                batch, lambda: validate_batch_profile(batch, "smoke")
            ),
            "batch_research": self.read_only_result(
                batch, lambda: validate_batch_profile(batch, "research")
            ),
            "run_smoke": self.read_only_result(
                batch,
                lambda: validate_run_profile(run_dir, batch, row, "smoke"),
            ),
            "run_research": self.read_only_result(
                batch,
                lambda: validate_run_profile(run_dir, batch, row, "research"),
            ),
        }

    def set_all_persisted_eligibility(
        self,
        batch: Path,
        value: bool,
        *,
        stored_unverified: list[str] | None = None,
    ) -> None:
        rows = self.read_rows(batch)
        for row in rows:
            row["research_eligible"] = value
            config_path = batch / row["config_path"]
            config = self.read_json(config_path)
            config["simulation"]["research_eligible"] = value
            self.write_json(config_path, config)
            run_meta_path = (
                batch / "runs" / f"output_{row['run_id']}" / "run_meta.json"
            )
            run_meta = self.read_json(run_meta_path)
            run_meta["config"]["simulation"]["research_eligible"] = value
            self.write_json(run_meta_path, run_meta)
        self.write_rows(batch, rows)

        meta_path = batch / "batch_meta.json"
        meta = self.read_json(meta_path)
        meta["research_eligible"] = value
        self.write_json(meta_path, meta)
        manifest_path = batch / "batch_manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["research_eligible"] = value
        for manifest_row in manifest["runs"]:
            manifest_row["research_eligible"] = value
            if stored_unverified is not None:
                manifest_row["smoke_unverified_research_requirements"] = list(
                    stored_unverified
                )
        self.write_json(manifest_path, manifest)
        self.refresh_integrity_evidence(batch)

    def test_smoke_pass_research_unverifiable_and_read_only(self):
        before = tree_hashes(self.batch_dir)
        smoke = validate_batch_profile(self.batch_dir, "smoke")
        after = tree_hashes(self.batch_dir)
        self.assertEqual(smoke.exit_code, 0, smoke.errors)
        self.assertTrue(smoke.to_dict()["smoke_valid"])
        self.assertFalse(smoke.to_dict()["research_eligible"])
        self.assertEqual(smoke.details["execution_mode"], "scripted_smoke")
        self.assertTrue(smoke.unverified_research_requirements)
        self.assertTrue(smoke.strict_unverifiable)
        self.assertEqual(before, after)

        research = validate_batch_profile(self.batch_dir, "research")
        self.assertEqual(research.exit_code, 2, research.errors)
        self.assertEqual(research.classification, "UNVERIFIABLE")
        self.assertFalse(research.to_dict()["research_eligible"])
        self.assertEqual(research.details["execution_mode"], "scripted_smoke")

    def test_single_run_profiles_bind_to_planned_evidence(self):
        batch_research = validate_batch_profile(self.batch_dir, "research")
        for row in self.bundle.rows:
            with self.subTest(run_id=row["run_id"]):
                run_dir = self.batch_dir / "runs" / f"output_{row['run_id']}"
                smoke = self.read_only_result(
                    self.batch_dir,
                    lambda row=row, run_dir=run_dir: validate_run_profile(
                        run_dir, self.batch_dir, dict(row), "smoke"
                    ),
                )
                research = self.read_only_result(
                    self.batch_dir,
                    lambda row=row, run_dir=run_dir: validate_run_profile(
                        run_dir, self.batch_dir, dict(row), "research"
                    ),
                )
                self.assertEqual(smoke.exit_code, 0, smoke.errors)
                self.assertEqual(research.exit_code, 2, research.errors)
                self.assertEqual(
                    smoke.details["execution_mode"], "scripted_smoke"
                )
                self.assertEqual(
                    research.details["execution_mode"], "scripted_smoke"
                )
                self.assertFalse(
                    research.details["selected_run_research_eligible"]
                )
                self.assertFalse(research.details["batch_research_eligible"])
                if research.exit_code == 0:
                    self.assertEqual(batch_research.exit_code, 0)

    def test_execution_mode_conflicts_fail_across_every_evidence_layer(self):
        for layer in (
            "plan",
            "row",
            "config",
            "run",
            "batch",
            "manifest-top",
            "manifest-row",
        ):
            with self.subTest(layer=layer):
                batch = self.copy_batch(f"mode-{layer}")
                row, config_path, run_dir = self.first_run_paths(batch)
                if layer == "plan":
                    path = batch / "plan.json"
                    value = self.read_json(path)
                    value["execution_mode"] = "reference_ollama"
                    self.write_json(path, value)
                elif layer == "batch":
                    meta_path = batch / "batch_meta.json"
                    value = self.read_json(meta_path)
                    value["execution_mode"] = "reference_ollama"
                    self.write_json(meta_path, value)
                elif layer == "row":
                    rows = self.read_rows(batch)
                    rows[0]["execution_mode"] = "reference_ollama"
                    self.write_rows(batch, rows)
                elif layer == "config":
                    value = self.read_json(config_path)
                    value["simulation"]["execution_mode"] = "reference_ollama"
                    self.write_json(config_path, value)
                elif layer == "run":
                    meta_path = run_dir / "run_meta.json"
                    value = self.read_json(meta_path)
                    value["config"]["simulation"][
                        "execution_mode"
                    ] = "reference_ollama"
                    self.write_json(meta_path, value)
                else:
                    path = batch / "batch_manifest.json"
                    value = self.read_json(path)
                    if layer == "manifest-top":
                        value["execution_mode"] = "reference_ollama"
                    else:
                        value["runs"][0]["execution_mode"] = "reference_ollama"
                    self.write_json(path, value)

                self.refresh_integrity_evidence(batch)

                before = tree_hashes(batch)
                batch_report = validate_batch_profile(batch, "research")
                run_report = validate_run_profile(
                    run_dir, batch, self.read_rows(batch)[0], "research"
                )
                self.assertEqual(batch_report.exit_code, 3, batch_report.errors)
                self.assertEqual(run_report.exit_code, 3, run_report.errors)
                self.assertFalse(batch_report.to_dict()["research_eligible"])
                self.assertFalse(run_report.to_dict()["research_eligible"])
                self.assertIsNone(batch_report.details["execution_mode"])
                self.assertIsNone(run_report.details["execution_mode"])
                self.assertTrue(
                    any(
                        "execution_mode conflict" in error
                        for error in batch_report.errors + run_report.errors
                    ),
                    batch_report.errors + run_report.errors,
                )
                self.assertEqual(before, tree_hashes(batch))

    def test_missing_execution_mode_in_each_completed_layer_fails(self):
        for layer in (
            "plan",
            "row",
            "config",
            "run",
            "batch",
            "manifest-top",
            "manifest-row",
        ):
            with self.subTest(layer=layer):
                batch = self.copy_batch(f"missing-mode-{layer}")
                row, config_path, run_dir = self.first_run_paths(batch)
                if layer == "plan":
                    path = batch / "plan.json"
                    value = self.read_json(path)
                    value.pop("execution_mode")
                    self.write_json(path, value)
                elif layer == "row":
                    rows = self.read_rows(batch)
                    rows[0].pop("execution_mode")
                    self.write_rows(batch, rows)
                elif layer == "config":
                    value = self.read_json(config_path)
                    value["simulation"].pop("execution_mode")
                    self.write_json(config_path, value)
                elif layer == "run":
                    path = run_dir / "run_meta.json"
                    value = self.read_json(path)
                    value["config"]["simulation"].pop("execution_mode")
                    self.write_json(path, value)
                elif layer == "batch":
                    path = batch / "batch_meta.json"
                    value = self.read_json(path)
                    value.pop("execution_mode")
                    self.write_json(path, value)
                else:
                    path = batch / "batch_manifest.json"
                    value = self.read_json(path)
                    if layer == "manifest-top":
                        value.pop("execution_mode")
                    else:
                        value["runs"][0].pop("execution_mode")
                    self.write_json(path, value)
                self.refresh_integrity_evidence(batch)
                selected = self.read_rows(batch)[0]
                before = tree_hashes(batch)
                report = validate_run_profile(
                    run_dir, batch, selected, "research"
                )
                self.assertEqual(report.exit_code, 3, report.errors)
                self.assertFalse(report.to_dict()["research_eligible"])
                self.assertTrue(
                    any("execution_mode" in error for error in report.errors),
                    report.errors,
                )
                self.assertEqual(before, tree_hashes(batch))

    def test_recomputed_manifest_cannot_conceal_batch_mode_conflict(self):
        batch = self.copy_batch("mode-recomputed")
        meta_path = batch / "batch_meta.json"
        manifest_path = batch / "batch_manifest.json"
        meta = self.read_json(meta_path)
        meta["execution_mode"] = "reference_ollama"
        manifest = self.read_json(manifest_path)
        self.write_json(manifest_path, manifest)
        meta["batch_manifest_sha256"] = sha256_file(manifest_path)
        self.write_json(meta_path, meta)

        row = self.read_rows(batch)[0]
        run_dir = batch / "runs" / f"output_{row['run_id']}"
        before = tree_hashes(batch)
        batch_result = validate_batch_profile(batch, "research")
        run_result = validate_run_profile(
            run_dir, batch, row, "research"
        )
        self.assertEqual(batch_result.exit_code, 3, batch_result.errors)
        self.assertEqual(run_result.exit_code, 3, run_result.errors)
        self.assertIsNone(batch_result.details["execution_mode"])
        self.assertIsNone(run_result.details["execution_mode"])
        self.assertTrue(
            any(
                "execution_mode conflict" in error
                for error in batch_result.errors + run_result.errors
            )
        )
        self.assertEqual(before, tree_hashes(batch))

        batch_cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.research_validator",
                "batch",
                "--profile",
                "research",
                "--batch-dir",
                str(batch),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        run_cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.research_validator",
                "run",
                "--profile",
                "research",
                "--batch-dir",
                str(batch),
                "--run-id",
                row["run_id"],
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(batch_cli.returncode, 3, batch_cli.stderr + batch_cli.stdout)
        self.assertEqual(run_cli.returncode, 3, run_cli.stderr + run_cli.stdout)
        self.assertEqual(
            json.loads(batch_cli.stdout)["classification"],
            json.loads(run_cli.stdout)["classification"],
        )
        self.assertEqual(before, tree_hashes(batch))

    def test_persisted_research_eligible_cannot_override_derived_result(self):
        for layer in (
            "batch",
            "row",
            "config",
            "run",
            "manifest-top",
            "manifest-row",
        ):
            with self.subTest(layer=layer):
                batch = self.copy_batch(f"eligible-{layer}")
                row, config_path, run_dir = self.first_run_paths(batch)
                if layer == "batch":
                    path = batch / "batch_meta.json"
                    value = self.read_json(path)
                    value["research_eligible"] = True
                    self.write_json(path, value)
                elif layer == "row":
                    rows = self.read_rows(batch)
                    rows[0]["research_eligible"] = True
                    self.write_rows(batch, rows)
                elif layer == "config":
                    value = self.read_json(config_path)
                    value["simulation"]["research_eligible"] = True
                    self.write_json(config_path, value)
                elif layer == "run":
                    path = run_dir / "run_meta.json"
                    value = self.read_json(path)
                    value["config"]["simulation"]["research_eligible"] = True
                    self.write_json(path, value)
                else:
                    path = batch / "batch_manifest.json"
                    value = self.read_json(path)
                    if layer == "manifest-top":
                        value["research_eligible"] = True
                    else:
                        value["runs"][0]["research_eligible"] = True
                    self.write_json(path, value)

                self.refresh_integrity_evidence(batch)
                before = tree_hashes(batch)
                report = validate_batch_profile(batch, "research")
                self.assertEqual(report.exit_code, 3, report.errors)
                self.assertFalse(report.to_dict()["research_eligible"])
                self.assertFalse(report.details["derived_research_eligible"])
                self.assertTrue(
                    any(
                        "research eligible" in error
                        or "research_eligible" in error
                        for error in report.errors
                    ),
                    report.errors,
                )
                self.assertEqual(before, tree_hashes(batch))

    def test_synthetic_positive_eligibility_is_derived_and_matches_summaries(self):
        batch = self.copy_batch("synthetic-positive")
        make_synthetic_research_batch(batch)
        before = tree_hashes(batch)
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("validator logic must not use the network"),
        ) as post:
            profiles = self.validate_all_public_profiles_read_only(batch)
            research, run_results = self.validate_every_run_research(batch)
        self.assertEqual(post.call_count, 0)
        smoke = profiles["batch_smoke"]
        self.assertEqual(smoke.exit_code, 0, smoke.errors)
        self.assertFalse(smoke.to_dict()["research_eligible"])
        self.assertTrue(smoke.details["derived_research_eligible"])
        self.assertEqual(research.exit_code, 0, research.errors)
        self.assertEqual(research.classification, "PASS")
        self.assertTrue(research.to_dict()["research_eligible"])
        self.assertTrue(research.details["derived_research_eligible"])
        self.assertEqual(research.details["execution_mode"], "reference_ollama")
        for run_research in run_results.values():
            self.assertEqual(run_research.exit_code, 0, run_research.errors)
            self.assertTrue(run_research.to_dict()["research_eligible"])
            self.assertTrue(
                run_research.details["selected_run_research_eligible"]
            )
            self.assertTrue(run_research.details["batch_research_eligible"])
            self.assertEqual(
                set(run_research.details["persisted_research_eligibility"]),
                {
                    "batch_metadata",
                    "batch_manifest",
                    "selected_planned_row",
                    "selected_generated_config",
                    "selected_saved_run_config",
                    "selected_manifest_run",
                },
            )
            self.assertTrue(
                all(
                    run_research.details["persisted_research_eligibility"].values()
                )
            )
        self.assertEqual(before, tree_hashes(batch))

    def test_plan_freeze_conflict_blocks_batch_and_every_public_run(self):
        batch = self.copy_batch("plan-freeze-conflict")
        make_synthetic_research_batch(batch)
        plan_path = batch / "plan.json"
        plan = self.read_json(plan_path)
        plan["candidate_registry"] = {"status": "not_frozen", "sha256": None}
        plan["backend_freeze"] = {"status": "not_frozen", "evidence_id": None}
        self.write_json(plan_path, plan)
        self.refresh_integrity_evidence(batch)

        before = tree_hashes(batch)
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("validator logic must not use the network"),
        ) as post:
            profiles = self.validate_all_public_profiles_read_only(batch)
            batch_result, run_results = self.validate_every_run_research(batch)
        self.assertEqual(post.call_count, 0)
        for report in profiles.values():
            self.assertEqual(report.exit_code, 3, report.errors)
            self.assertFalse(report.to_dict()["research_eligible"])
        self.assertEqual(batch_result.exit_code, 3, batch_result.errors)
        self.assertTrue(
            any("candidate_registry differs" in error for error in batch_result.errors),
            batch_result.errors,
        )
        self.assertTrue(
            any("backend_freeze differs" in error for error in batch_result.errors),
            batch_result.errors,
        )
        for report in run_results.values():
            self.assertEqual(report.exit_code, 3, report.errors)
            self.assertFalse(report.to_dict()["research_eligible"])
            self.assertTrue(
                report.details["selected_run_research_eligible"], report.errors
            )
            self.assertFalse(report.details["batch_research_eligible"])
        self.assertEqual(before, tree_hashes(batch))

    def test_consistently_not_frozen_is_unverifiable_for_batch_and_every_run(self):
        batch = self.copy_batch("consistently-not-frozen")
        make_synthetic_research_batch(batch, persisted_research_eligible=False)
        plan_path = batch / "plan.json"
        plan = self.read_json(plan_path)
        plan["candidate_registry"] = {"status": "not_frozen", "sha256": None}
        plan["backend_freeze"] = {"status": "not_frozen", "evidence_id": None}
        self.write_json(plan_path, plan)
        meta_path = batch / "batch_meta.json"
        meta = self.read_json(meta_path)
        meta["candidate_registry"] = dict(plan["candidate_registry"])
        meta["backend_freeze"] = dict(plan["backend_freeze"])
        self.write_json(meta_path, meta)
        expected_unverified = [
            "backend artifacts are not frozen",
            "production candidate registry is not frozen",
        ]
        self.set_all_persisted_eligibility(
            batch,
            False,
            stored_unverified=expected_unverified,
        )

        before = tree_hashes(batch)
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("validator logic must not use the network"),
        ) as post:
            profiles = self.validate_all_public_profiles_read_only(batch)
            batch_result, run_results = self.validate_every_run_research(batch)
        self.assertEqual(post.call_count, 0)
        self.assertEqual(profiles["batch_smoke"].exit_code, 0)
        self.assertEqual(profiles["run_smoke"].exit_code, 0)
        for report in (batch_result, *run_results.values()):
            self.assertEqual(report.exit_code, 2, report.errors)
            self.assertEqual(report.classification, "UNVERIFIABLE")
            self.assertFalse(report.to_dict()["research_eligible"])
            self.assertEqual(
                report.unverified_research_requirements,
                expected_unverified,
            )
        self.assertEqual(before, tree_hashes(batch))

    def test_each_stale_batch_summary_blocks_every_public_run(self):
        for layer in ("batch-metadata", "batch-manifest"):
            with self.subTest(layer=layer):
                batch = self.copy_batch(f"stale-{layer}")
                make_synthetic_research_batch(batch)
                meta_path = batch / "batch_meta.json"
                manifest_path = batch / "batch_manifest.json"
                meta = self.read_json(meta_path)
                manifest = self.read_json(manifest_path)
                if layer == "batch-metadata":
                    meta["research_eligible"] = False
                else:
                    manifest["research_eligible"] = False
                    self.write_json(manifest_path, manifest)
                    meta["batch_manifest_sha256"] = sha256_file(manifest_path)
                self.write_json(meta_path, meta)

                before = tree_hashes(batch)
                with mock.patch(
                    "engine.llm_client.requests.post",
                    side_effect=AssertionError(
                        "validator logic must not use the network"
                    ),
                ) as post:
                    profiles = self.validate_all_public_profiles_read_only(batch)
                    batch_result, run_results = self.validate_every_run_research(
                        batch
                    )
                self.assertEqual(post.call_count, 0)
                for report in profiles.values():
                    self.assertEqual(report.exit_code, 3, report.errors)
                    self.assertFalse(report.to_dict()["research_eligible"])
                self.assertEqual(batch_result.exit_code, 3, batch_result.errors)
                self.assertTrue(
                    any(
                        "stale false summary" in error
                        for error in batch_result.errors
                    ),
                    batch_result.errors,
                )
                for report in run_results.values():
                    self.assertEqual(report.exit_code, 3, report.errors)
                    self.assertFalse(report.to_dict()["research_eligible"])
                    self.assertTrue(
                        report.details[
                            "derived_selected_run_research_eligible"
                        ]
                    )
                    self.assertTrue(
                        report.details["derived_batch_research_eligible"]
                    )
                    self.assertFalse(report.details["batch_research_eligible"])
                self.assertEqual(before, tree_hashes(batch))

    def test_unselected_invalid_run_blocks_selected_run_research_pass(self):
        batch = self.copy_batch("unselected-invalid")
        make_synthetic_research_batch(batch)
        rows = self.read_rows(batch)
        invalid_row = rows[1]
        invalid_meta_path = (
            batch
            / "runs"
            / f"output_{invalid_row['run_id']}"
            / "run_meta.json"
        )
        invalid_meta = self.read_json(invalid_meta_path)
        invalid_meta["status"] = "failed"
        self.write_json(invalid_meta_path, invalid_meta)
        self.refresh_integrity_evidence(batch)

        before = tree_hashes(batch)
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("validator logic must not use the network"),
        ) as post:
            profiles = self.validate_all_public_profiles_read_only(batch)
            batch_result, run_results = self.validate_every_run_research(batch)
        self.assertEqual(post.call_count, 0)
        for report in profiles.values():
            self.assertEqual(report.exit_code, 3, report.errors)
            self.assertFalse(report.to_dict()["research_eligible"])
        self.assertEqual(batch_result.exit_code, 3, batch_result.errors)
        selected = run_results[rows[0]["run_id"]]
        self.assertEqual(selected.exit_code, 3, selected.errors)
        self.assertTrue(selected.details["selected_run_research_eligible"])
        self.assertFalse(selected.details["batch_research_eligible"])
        for report in run_results.values():
            self.assertNotEqual(report.exit_code, 0, report.errors)
            self.assertFalse(report.to_dict()["research_eligible"])
        self.assertEqual(before, tree_hashes(batch))

    def test_stale_false_and_unsupported_true_summaries_fail(self):
        stale = self.copy_batch("stale-false")
        make_synthetic_research_batch(stale)
        stale_meta_path = stale / "batch_meta.json"
        stale_manifest_path = stale / "batch_manifest.json"
        stale_meta = self.read_json(stale_meta_path)
        stale_manifest = self.read_json(stale_manifest_path)
        stale_meta["research_eligible"] = False
        stale_manifest["research_eligible"] = False
        self.write_json(stale_manifest_path, stale_manifest)
        stale_meta["batch_manifest_sha256"] = sha256_file(stale_manifest_path)
        self.write_json(stale_meta_path, stale_meta)
        stale_result = validate_batch_profile(stale, "research")
        self.assertEqual(stale_result.exit_code, 3, stale_result.errors)
        self.assertTrue(stale_result.details["derived_research_eligible"])
        self.assertFalse(stale_result.to_dict()["research_eligible"])
        self.assertTrue(
            any("stale false summary" in error for error in stale_result.errors),
            stale_result.errors,
        )

        unsupported = self.copy_batch("unsupported-true")
        unsupported_meta_path = unsupported / "batch_meta.json"
        unsupported_manifest_path = unsupported / "batch_manifest.json"
        unsupported_meta = self.read_json(unsupported_meta_path)
        unsupported_manifest = self.read_json(unsupported_manifest_path)
        unsupported_meta["research_eligible"] = True
        unsupported_manifest["research_eligible"] = True
        self.write_json(unsupported_manifest_path, unsupported_manifest)
        unsupported_meta["batch_manifest_sha256"] = sha256_file(
            unsupported_manifest_path
        )
        self.write_json(unsupported_meta_path, unsupported_meta)
        unsupported_result = validate_batch_profile(unsupported, "research")
        self.assertEqual(unsupported_result.exit_code, 3, unsupported_result.errors)
        self.assertFalse(unsupported_result.details["derived_research_eligible"])
        self.assertFalse(unsupported_result.to_dict()["research_eligible"])
        self.assertTrue(
            any(
                "unsupported true summary" in error
                for error in unsupported_result.errors
            ),
            unsupported_result.errors,
        )

    def test_per_run_stale_false_summaries_do_not_demote_derivation(self):
        batch = self.copy_batch("per-run-stale-false")
        make_synthetic_research_batch(batch)
        rows = self.read_rows(batch)
        row = rows[0]
        row["research_eligible"] = False
        self.write_rows(batch, rows)

        config_path = batch / row["config_path"]
        config = self.read_json(config_path)
        config["simulation"]["research_eligible"] = False
        self.write_json(config_path, config)

        run_dir = batch / "runs" / f"output_{row['run_id']}"
        run_meta_path = run_dir / "run_meta.json"
        run_meta = self.read_json(run_meta_path)
        run_meta["config"]["simulation"]["research_eligible"] = False
        self.write_json(run_meta_path, run_meta)

        manifest_path = batch / "batch_manifest.json"
        manifest = self.read_json(manifest_path)
        manifest["runs"][0]["research_eligible"] = False
        self.write_json(manifest_path, manifest)
        self.refresh_integrity_evidence(batch)

        selected_row = self.read_rows(batch)[0]
        before = tree_hashes(batch)
        batch_result = validate_batch_profile(batch, "research")
        run_result = validate_run_profile(
            run_dir, batch, selected_row, "research"
        )
        for report in (batch_result, run_result):
            self.assertEqual(report.exit_code, 3, report.errors)
            self.assertTrue(
                report.details["derived_research_eligible"], report.errors
            )
            self.assertFalse(report.to_dict()["research_eligible"])
            self.assertTrue(
                any("stale false summary" in error for error in report.errors),
                report.errors,
            )
        self.assertEqual(before, tree_hashes(batch))

    def test_consistent_non_scripted_missing_only_approval_is_unverifiable(self):
        batch = self.copy_batch("missing-approval")
        make_synthetic_research_batch(batch, approval_present=False)
        before = tree_hashes(batch)
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=AssertionError("validator logic must not use the network"),
        ) as post:
            self.validate_all_public_profiles_read_only(batch)
            batch_result, run_results = self.validate_every_run_research(batch)
        self.assertEqual(post.call_count, 0)
        for report in (batch_result, *run_results.values()):
            self.assertEqual(report.exit_code, 2, report.errors)
            self.assertEqual(report.classification, "UNVERIFIABLE")
            self.assertFalse(report.details["derived_research_eligible"])
            self.assertFalse(report.to_dict()["research_eligible"])
            self.assertEqual(
                report.unverified_research_requirements,
                ["run-start approval reference is absent"],
            )
        self.assertEqual(before, tree_hashes(batch))

    def test_config_cell_policy_seed_and_planned_run_tampering_fail(self):
        mutations = (
            ("config-cell", "config", lambda value: value["simulation"].update({"cell_id": "qqq-full"})),
            ("config-policy", "config", lambda value: value["agents"].update({"edge_policy": "within_bloc_only"})),
            ("config-seed", "config", lambda value: value["simulation"].update({"seed": 9999})),
            ("planned-run", "planned", lambda value: value[0].update({"run_id": "tampered-run"})),
        )
        for name, target, mutate in mutations:
            with self.subTest(name=name):
                batch = self.copy_batch(name)
                if target == "config":
                    config_path = batch / self.bundle.rows[0]["config_path"]
                    value = json.loads(config_path.read_text(encoding="utf-8"))
                    mutate(value)
                    config_path.write_bytes(canonical_json_file_bytes(value))
                else:
                    rows_path = batch / "planned_runs.jsonl"
                    value = [
                        json.loads(line)
                        for line in rows_path.read_text(encoding="utf-8").splitlines()
                    ]
                    mutate(value)
                    rows_path.write_text(
                        "".join(
                            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                            for row in value
                        ),
                        encoding="utf-8",
                        newline="\n",
                    )
                report = validate_batch_profile(batch, "smoke")
                self.assertEqual(report.exit_code, 3, report.errors)

    def test_manifest_extra_and_missing_run_tampering_fail(self):
        manifest_batch = self.copy_batch("manifest")
        meta_path = manifest_batch / "batch_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["batch_manifest_sha256"] = "0" * 64
        meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
        self.assertEqual(
            validate_batch_profile(manifest_batch, "smoke").exit_code, 3
        )

        extra_batch = self.copy_batch("extra")
        (extra_batch / "runs" / "output_extra-run").mkdir()
        self.assertEqual(validate_batch_profile(extra_batch, "smoke").exit_code, 3)

        missing_batch = self.copy_batch("missing")
        missing = missing_batch / "runs" / f"output_{self.bundle.rows[0]['run_id']}"
        shutil.rmtree(missing)
        self.assertEqual(validate_batch_profile(missing_batch, "smoke").exit_code, 3)

    def test_cross_bloc_delivery_tamper_fails_even_with_recomputed_manifests(self):
        batch = self.copy_batch("cross-edge")
        row = next(
            item for item in self.bundle.rows
            if item["edge_policy"] == "within_bloc_only"
        )
        run_dir = batch / "runs" / f"output_{row['run_id']}"
        messages_path = run_dir / "messages.jsonl"
        messages = [
            json.loads(line)
            for line in messages_path.read_text(encoding="utf-8").splitlines()
        ]
        messages[0]["receiver_ids"].append(4)
        messages[0]["receiver_ids"].sort()
        messages_path.write_text(
            "".join(json.dumps(item) + "\n" for item in messages),
            encoding="utf-8",
            newline="\n",
        )
        run_meta_path = run_dir / "run_meta.json"
        run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
        run_meta["raw_manifest"] = build_raw_manifest(run_dir)
        run_meta_path.write_text(
            json.dumps(run_meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        batch_manifest_path = batch / "batch_manifest.json"
        batch_manifest = json.loads(
            batch_manifest_path.read_text(encoding="utf-8")
        )
        manifest_row = next(
            item for item in batch_manifest["runs"] if item["run_id"] == row["run_id"]
        )
        manifest_row["run_meta_manifest"] = file_manifest(run_meta_path)
        manifest_row["raw_manifest"] = run_meta["raw_manifest"]
        batch_manifest_path.write_bytes(canonical_json_file_bytes(batch_manifest))
        batch_meta_path = batch / "batch_meta.json"
        batch_meta = json.loads(batch_meta_path.read_text(encoding="utf-8"))
        batch_meta["batch_manifest_sha256"] = sha256_file(batch_manifest_path)
        batch_meta_path.write_text(
            json.dumps(batch_meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report = validate_batch_profile(batch, "smoke")
        self.assertEqual(report.exit_code, 3, report.errors)
        self.assertTrue(
            any("communication boundary" in error for error in report.errors),
            report.errors,
        )

    def test_cli_process_exit_codes_zero_two_three_and_sixty_four(self):
        base = [sys.executable, "-m", "tools.research_validator", "batch"]
        smoke = subprocess.run(
            base + ["--profile", "smoke", "--batch-dir", str(self.batch_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        research = subprocess.run(
            base + ["--profile", "research", "--batch-dir", str(self.batch_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        run_id = self.bundle.rows[0]["run_id"]
        run_base = [
            sys.executable,
            "-m",
            "tools.research_validator",
            "run",
            "--batch-dir",
            str(self.batch_dir),
            "--run-id",
            run_id,
        ]
        run_smoke = subprocess.run(
            run_base + ["--profile", "smoke"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        run_research = subprocess.run(
            run_base + ["--profile", "research"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        tampered = self.copy_batch("cli-tampered")
        (tampered / "batch_manifest.json").write_text("{}\n", encoding="utf-8")
        failed = subprocess.run(
            base + ["--profile", "smoke", "--batch-dir", str(tampered)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        conflict = self.copy_batch("cli-plan-freeze-conflict")
        make_synthetic_research_batch(conflict)
        conflict_plan_path = conflict / "plan.json"
        conflict_plan = self.read_json(conflict_plan_path)
        conflict_plan["candidate_registry"] = {
            "status": "not_frozen",
            "sha256": None,
        }
        conflict_plan["backend_freeze"] = {
            "status": "not_frozen",
            "evidence_id": None,
        }
        self.write_json(conflict_plan_path, conflict_plan)
        self.refresh_integrity_evidence(conflict)
        conflict_before = tree_hashes(conflict)
        conflict_batch = subprocess.run(
            base + ["--profile", "research", "--batch-dir", str(conflict)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        conflict_run_id = self.read_rows(conflict)[0]["run_id"]
        conflict_run = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.research_validator",
                "run",
                "--profile",
                "research",
                "--batch-dir",
                str(conflict),
                "--run-id",
                conflict_run_id,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        invocation = subprocess.run(
            [sys.executable, "-m", "tools.research_validator"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        help_result = subprocess.run(
            [sys.executable, "-m", "tools.research_validator", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr + smoke.stdout)
        self.assertEqual(research.returncode, 2, research.stderr + research.stdout)
        self.assertEqual(run_smoke.returncode, 0, run_smoke.stderr + run_smoke.stdout)
        self.assertEqual(
            run_research.returncode,
            2,
            run_research.stderr + run_research.stdout,
        )
        self.assertEqual(
            json.loads(research.stdout)["classification"],
            json.loads(run_research.stdout)["classification"],
        )
        self.assertEqual(failed.returncode, 3, failed.stderr + failed.stdout)
        self.assertEqual(
            conflict_batch.returncode,
            3,
            conflict_batch.stderr + conflict_batch.stdout,
        )
        self.assertEqual(
            conflict_run.returncode,
            3,
            conflict_run.stderr + conflict_run.stdout,
        )
        self.assertFalse(json.loads(conflict_batch.stdout)["research_eligible"])
        self.assertFalse(json.loads(conflict_run.stdout)["research_eligible"])
        self.assertEqual(conflict_before, tree_hashes(conflict))
        self.assertEqual(invocation.returncode, 64, invocation.stderr + invocation.stdout)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("usage:", help_result.stdout)
        self.assertEqual(help_result.stderr, "")


if __name__ == "__main__":
    unittest.main()
