import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.ingest_run import ingest_run
from tools.scan_publication import Finding


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class RepositoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "output_public-run-001"
        self.source.mkdir()
        (self.source / "run_meta.json").write_text(
            json.dumps({"run_id": "public-run-001"}) + "\n",
            encoding="utf-8",
        )
        (self.source / "raw.bin").write_bytes(bytes(range(256)))

    def ingest(self) -> Path:
        with mock.patch(
            "tools.ingest_run.validate_run",
            return_value=SimpleNamespace(valid=True, errors=[]),
        ), mock.patch("tools.ingest_run.scan_tree", return_value=[]):
            return ingest_run(self.source, self.root / "repository" / "runs")

    def test_ingest_is_byte_preserving_and_collision_safe(self):
        before = tree_hashes(self.source)
        destination = self.ingest()
        self.assertEqual(tree_hashes(self.source), before)
        self.assertEqual(tree_hashes(destination), before)
        with self.assertRaises(FileExistsError):
            self.ingest()
        self.assertEqual(tree_hashes(destination), before)

    def test_ingest_rejects_scan_findings_without_creating_destination(self):
        runs_root = self.root / "repository" / "runs"
        finding = Finding("raw.bin", "credential_assignment", 1)
        with mock.patch(
            "tools.ingest_run.validate_run",
            return_value=SimpleNamespace(valid=True, errors=[]),
        ), mock.patch("tools.ingest_run.scan_tree", return_value=[finding]):
            with self.assertRaisesRegex(ValueError, "publication scan failed"):
                ingest_run(self.source, runs_root)
        self.assertFalse(runs_root.exists())

    def test_ingest_rejects_destination_inside_source(self):
        with self.assertRaisesRegex(ValueError, "must not be inside"):
            ingest_run(self.source, self.source / "runs")

    def test_run_outputs_are_trackable_while_root_scratch_outputs_are_ignored(self):
        repository = Path(__file__).resolve().parents[1]
        tracked_run = subprocess.run(
            [
                "git",
                "check-ignore",
                "--no-index",
                "runs/output_public-example/run_meta.json",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        root_scratch = subprocess.run(
            [
                "git",
                "check-ignore",
                "--no-index",
                "output_scratch/run_meta.json",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tracked_run.returncode, 1)
        self.assertEqual(root_scratch.returncode, 0)


if __name__ == "__main__":
    unittest.main()
