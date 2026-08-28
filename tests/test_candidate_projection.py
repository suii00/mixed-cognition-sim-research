import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from engine.provenance import RunLifecycle
from tools.candidate_projection import main as projection_main
from tools.candidate_projection_core import (
    ALL_OUTPUT_FILES,
    PROJECTION_SPEC_PATH,
    PROJECTION_VERSION,
    ProjectionCollisionError,
    ProjectionInputError,
    _read_phase1_messages,
    prepare_projection,
    project_run,
    write_prepared_projection,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_NAME = "output_het12x1-ollama-20260819-r001"
FIXTURE_RUN_ID = "het12x1-ollama-20260819-r001"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_hashes(path: Path) -> dict[str, str]:
    return {
        item.relative_to(path).as_posix(): sha256_bytes(item.read_bytes())
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def create_public_fixture(root: Path) -> Path:
    config = {
        "simulation": {
            "duration": 2,
            "half_space_size": 5,
            "seed": 17,
            "run_name": "candidate-projection-fixture",
            "run_id": FIXTURE_RUN_ID,
            "protocol_version": "candidate-test-protocol-v1",
            "metric_version": "candidate-projection-v1",
        },
        "blocs": [{
            "name": "alpha",
            "model": "mock-model",
            "endpoint_id": "mock-endpoint",
            "num_agents": 2,
        }],
        "agents": {
            "communication_radius": 100,
            "memory_limit": 2,
            "memory_size": 1,
            "message_history_limit": 4,
            "message_context_size": 2,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 32,
            "timeout_s": 1,
        },
    }
    lifecycle = RunLifecycle.create(config, output_root=root, repo_root=REPO_ROOT)
    output = lifecycle.output_dir
    messages = {
        (1, 0): "Blue lantern",
        (2, 0): "Blue lantern",
        (2, 1): "another phrase",
    }
    phase1_rows = []
    delivery_rows = []
    phase3_rows = []
    for step in (1, 2):
        for agent_id in (0, 1):
            message = messages.get((step, agent_id), "")
            parsed = {"message": message, "reasoning": ""}
            phase1_rows.append({
                "step": step,
                "agent_id": agent_id,
                "bloc": "alpha",
                "model": "mock-model",
                "parsed": parsed,
                "raw_output": json.dumps(parsed),
            })
            phase3_rows.append({
                "step": step,
                "agent_id": agent_id,
                "bloc": "alpha",
                "model": "mock-model",
                "position": [agent_id, 0],
                "action": "stay",
                "direction": "",
                "memory": "",
                "reasoning": "",
            })
            if message:
                delivery_rows.append({
                    "step": step,
                    "sender_id": agent_id,
                    "sender_bloc": "alpha",
                    "sender_model": "mock-model",
                    "receiver_ids": [1 - agent_id],
                    "message": message,
                    "reasoning": "",
                })

    def write_rows(name: str, rows: list[dict]) -> None:
        (output / name).write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
            newline="\n",
        )

    write_rows("phase1_raw.jsonl", phase1_rows)
    write_rows("messages.jsonl", delivery_rows)
    write_rows("memory_reasoning.jsonl", phase3_rows)
    write_rows("parse_errors.jsonl", [])
    lifecycle._observed_agent_ids.update({0, 1})
    lifecycle.meta["logical_llm_calls"] = 8
    lifecycle.meta["http_attempts"] = 8
    lifecycle.mark_step_completed(1)
    lifecycle.mark_step_completed(2)
    lifecycle.finalize_completed()
    return output


class CandidateProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        git_info = {
            "git_sha": "c" * 40,
            "git_dirty": False,
            "git_probe_status": "available",
            "git_probe_errors": [],
        }
        self.source_git_patch = mock.patch(
            "engine.provenance.collect_git_info", return_value=git_info
        )
        self.git_patch = mock.patch(
            "tools.candidate_projection_core.collect_git_info",
            return_value=git_info,
        )
        self.source_git_patch.start()
        self.git_patch.start()
        self.addCleanup(self.source_git_patch.stop)
        self.addCleanup(self.git_patch.stop)
        self.run_dir = create_public_fixture(self.root)
        self.derived_root = self.root / "derived"
        self.projection_id = "candidate-view-s1002-r001"
        self.spec_sha256 = sha256_bytes(PROJECTION_SPEC_PATH.read_bytes())

    def project(self) -> Path:
        return project_run(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
            self.derived_root,
        )

    def test_direct_reader_deduplicates_and_blinds_rows(self):
        direct = self.root / "direct"
        direct.mkdir()
        records = [
            {
                "step": 9,
                "agent_id": 44,
                "bloc": "secret-alpha",
                "model": "secret-model",
                "raw_output": "hidden",
                "parsed": {"message": "Blue lantern", "reasoning": "hidden"},
            },
            {
                "step": 1,
                "agent_id": 2,
                "parsed": {"message": "another phrase"},
            },
            {
                "step": 10,
                "agent_id": 45,
                "parsed": {"message": "Blue lantern"},
            },
            {"step": 11, "agent_id": 46, "parsed": None},
        ]
        (direct / "phase1_raw.jsonl").write_bytes(
            b"".join(
                (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
                for record in records
            )
        )

        reviewer_rows, source_rows = _read_phase1_messages(direct)

        self.assertEqual(len(reviewer_rows), 2)
        self.assertTrue(all(set(row) == {"blind_message_id", "message"} for row in reviewer_rows))
        self.assertEqual(
            [row["blind_message_id"] for row in reviewer_rows],
            ["message-000001", "message-000002"],
        )
        expected_messages = sorted(
            {"Blue lantern", "another phrase"},
            key=lambda message: (
                sha256_bytes(message.encode("utf-8")),
                message.encode("utf-8"),
            ),
        )
        self.assertEqual(
            [row["message"] for row in reviewer_rows],
            expected_messages,
        )
        source_by_id = {row["blind_message_id"]: row for row in source_rows}
        blue_id = next(
            row["blind_message_id"]
            for row in reviewer_rows
            if row["message"] == "Blue lantern"
        )
        self.assertEqual(source_by_id[blue_id]["occurrence_count"], 2)
        reviewer_text = json.dumps(reviewer_rows)
        for hidden in ("step", "agent_id", "bloc", "model", "raw_output", "reasoning"):
            self.assertNotIn(hidden, reviewer_text)

    def test_projection_is_deterministic_and_reviewer_bundle_is_bounded(self):
        before = file_hashes(self.run_dir)
        first = prepare_projection(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
        )
        second = prepare_projection(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
        )
        self.assertEqual(first.files, second.files)

        output = self.project()

        self.assertEqual(file_hashes(self.run_dir), before)
        self.assertEqual(
            {
                item.relative_to(output).as_posix()
                for item in output.rglob("*")
                if item.is_file()
            },
            set(ALL_OUTPUT_FILES),
        )
        rows = read_jsonl(output / "reviewer/messages.jsonl")
        self.assertTrue(
            all(set(row) == {"blind_message_id", "message"} for row in rows)
        )
        reviewer_manifest = json.loads(
            (output / "reviewer/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(reviewer_manifest["schema_version"], PROJECTION_VERSION)
        self.assertEqual(
            reviewer_manifest["reviewer_visible_fields"],
            ["blind_message_id", "message"],
        )
        reviewer_bytes = b"".join(
            (output / "reviewer" / name).read_bytes()
            for name in ("messages.jsonl", "manifest.json")
        )
        for forbidden in (
            b"source_run_id",
            b"agent_id",
            b"receiver_id",
            b"condition",
            b"model",
            b"bloc",
            b"step",
            b"line_number",
            b"occurrence_count",
        ):
            self.assertNotIn(forbidden, reviewer_bytes)

        audit_meta = json.loads(
            (output / "audit/projection_meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(audit_meta["source_run_id"], FIXTURE_RUN_ID)
        self.assertTrue(audit_meta["strict_validator_valid"])

    def test_wrong_spec_hash_fails_before_output_creation(self):
        with self.assertRaisesRegex(ProjectionInputError, "specification SHA-256 mismatch"):
            project_run(
                self.run_dir,
                self.projection_id,
                "0" * 64,
                self.derived_root,
            )
        self.assertFalse(self.derived_root.exists())

    def test_invalid_raw_fails_before_output_creation(self):
        with (self.run_dir / "phase1_raw.jsonl").open("ab") as handle:
            handle.write(b"{}\n")
        with self.assertRaisesRegex(ProjectionInputError, "strict run validation failed"):
            self.project()
        self.assertFalse(self.derived_root.exists())

    def test_existing_projection_is_immutable_collision(self):
        output = self.project()
        before = file_hashes(output)
        with self.assertRaises(ProjectionCollisionError):
            self.project()
        self.assertEqual(file_hashes(output), before)

    def test_raw_change_before_publish_fails_and_keeps_final_absent(self):
        prepared = prepare_projection(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
        )

        def mutate(checkpoint: str, _staging: Path) -> None:
            if checkpoint == "before_publish":
                with (self.run_dir / "messages.jsonl").open("ab") as handle:
                    handle.write(b"{}\n")

        with self.assertRaisesRegex(ProjectionInputError, "changed before publication"):
            write_prepared_projection(
                prepared,
                self.run_dir,
                self.derived_root,
                publication_hook=mutate,
            )
        final = (
            self.derived_root
            / PROJECTION_VERSION
            / self.projection_id
        )
        self.assertFalse(final.exists())

    def test_run_meta_change_before_publish_fails(self):
        prepared = prepare_projection(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
        )

        def mutate(checkpoint: str, _staging: Path) -> None:
            if checkpoint == "before_publish":
                with (self.run_dir / "run_meta.json").open("ab") as handle:
                    handle.write(b" ")

        with self.assertRaisesRegex(ProjectionInputError, "changed before publication"):
            write_prepared_projection(
                prepared,
                self.run_dir,
                self.derived_root,
                publication_hook=mutate,
            )

    def test_concurrent_publishers_have_one_owner(self):
        prepared = prepare_projection(
            self.run_dir,
            self.projection_id,
            self.spec_sha256,
        )
        barrier = threading.Barrier(2)

        def publish() -> str:
            try:
                write_prepared_projection(
                    prepared,
                    self.run_dir,
                    self.derived_root,
                    before_claim=lambda: barrier.wait(timeout=5),
                )
            except ProjectionCollisionError:
                return "collision"
            return "success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(publish) for _ in range(2)]
            results = sorted(future.result() for future in futures)
        self.assertEqual(results, ["collision", "success"])

    def test_derived_root_inside_raw_run_is_rejected(self):
        with self.assertRaisesRegex(ProjectionInputError, "inside the raw run"):
            project_run(
                self.run_dir,
                self.projection_id,
                self.spec_sha256,
                self.run_dir / "derived",
            )

    def test_dirty_generator_is_rejected(self):
        with mock.patch(
            "tools.candidate_projection_core.collect_git_info",
            return_value={
                "git_sha": "c" * 40,
                "git_dirty": True,
                "git_probe_status": "available",
                "git_probe_errors": [],
            },
        ):
            with self.assertRaisesRegex(ProjectionInputError, "clean Git provenance"):
                self.project()
        self.assertFalse(self.derived_root.exists())

    def test_cli_reports_collision_without_overwrite(self):
        output = self.project()
        before = file_hashes(output)
        exit_code = projection_main([
            "--run-dir", str(self.run_dir),
            "--projection-id", self.projection_id,
            "--projection-spec-sha256", self.spec_sha256,
            "--derived-root", str(self.derived_root),
        ])
        self.assertEqual(exit_code, 3)
        self.assertEqual(file_hashes(output), before)


if __name__ == "__main__":
    unittest.main()
