import json
import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from tools.phase3_semantic_audit import audit_runs, classify_row, discover_runs


@contextmanager
def workspace_tempdir():
    base = Path.cwd() / ".tmp"
    base.mkdir(exist_ok=True)
    path = base / f"publication-test-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(path)


class Phase3SemanticAuditTests(unittest.TestCase):
    def write_run(self, root: Path, run_id: str, rows: list[dict]) -> Path:
        run = root / f"output_{run_id}"
        run.mkdir()
        (run / "run_meta.json").write_text(
            json.dumps({"run_id": run_id}), encoding="utf-8"
        )
        (run / "memory_reasoning.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        return run

    def test_classification_contract(self):
        self.assertIsNone(classify_row({"action": "move", "direction": "left"}))
        self.assertIsNone(classify_row({"action": "stay", "direction": None}))
        self.assertIsNone(classify_row({"action": "stay", "direction": ""}))
        self.assertIsNone(classify_row({"action": "stay", "direction": "right"}))
        self.assertEqual(
            classify_row({"action": "left", "direction": "left"}), "invalid_action"
        )
        self.assertEqual(
            classify_row({"action": "move", "direction": "west"}),
            "move_invalid_direction",
        )
        self.assertEqual(
            classify_row({"action": "stay", "direction": "none"}),
            "stay_invalid_direction",
        )

    def test_audit_counts_rows_runs_and_critical_union(self):
        with workspace_tempdir() as temp:
            root = Path(temp)
            self.write_run(
                root,
                "a",
                [
                    {"step": 1, "agent_id": 1, "action": "left", "direction": "left"},
                    {"step": 1, "agent_id": 2, "action": "stay", "direction": None},
                ],
            )
            self.write_run(
                root,
                "b",
                [
                    {"step": 1, "agent_id": 1, "action": "move", "direction": "west"},
                    {"step": 1, "agent_id": 2, "action": "stay", "direction": "none"},
                ],
            )
            runs = discover_runs([root])
            summary, violations = audit_runs(runs)
            self.assertEqual(summary["run_count"], 2)
            self.assertEqual(summary["phase3_row_count"], 4)
            self.assertEqual(summary["counts"]["invalid_action_rows"], 1)
            self.assertEqual(summary["counts"]["move_invalid_direction_rows"], 1)
            self.assertEqual(summary["counts"]["stay_invalid_direction_rows"], 1)
            self.assertEqual(summary["counts"]["critical_union_runs"], 2)
            self.assertEqual(len(violations), 3)


if __name__ == "__main__":
    unittest.main()
