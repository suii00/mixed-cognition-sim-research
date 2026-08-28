import json
import os
import tempfile
import unittest
from unittest import mock

from tools import render_report
from tools.viz_common import load_run, per_step_bloc_counts, timestamped_out_dir


class VisualizationToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = os.path.join(self.temp.name, "output-test-run")
        os.makedirs(self.run_dir)
        meta = {
            "run_id": "output-test-run",
            "status": "completed",
            "completed_steps": 1,
            "expected_steps": 1,
            "observed_agents": 2,
            "logical_llm_calls": 4,
            "transport_failures": 0,
            "syntax_parse_failures": 0,
            "schema_validation_failures": 0,
            "git_sha": "a" * 40,
            "start_time_utc": "2026-08-24T00:00:00+00:00",
            "end_time_utc": "2026-08-24T00:01:00+00:00",
            "config": {
                "simulation": {"half_space_size": 5, "seed": 42},
                "places": [],
                "blocs": [
                    {"name": "qwen", "model": "qwen-test"},
                    {"name": "llama", "model": "llama-test"},
                ],
            },
        }
        self._write_json("run_meta.json", meta)
        self._write_jsonl(
            "memory_reasoning.jsonl",
            [
                {
                    "step": 1,
                    "agent_id": 0,
                    "bloc": "qwen",
                    "model": "qwen-test",
                    "position": [0, 0],
                    "action": "move",
                    "direction": "right",
                    "memory": "",
                    "reasoning": "",
                },
                {
                    "step": 1,
                    "agent_id": 1,
                    "bloc": "llama",
                    "model": "llama-test",
                    "position": [1, 1],
                    "action": "stay",
                    "direction": "",
                    "memory": "",
                    "reasoning": "",
                },
            ],
        )
        self._write_jsonl(
            "messages.jsonl",
            [
                {
                    "step": 1,
                    "sender_id": 0,
                    "receiver_ids": [1],
                    "message": "status <safe>",
                    "reasoning": "",
                }
            ],
        )
        self._write_jsonl("parse_errors.jsonl", [])

    def tearDown(self):
        self.temp.cleanup()

    def _write_json(self, name, value):
        with open(os.path.join(self.run_dir, name), "w", encoding="utf-8") as stream:
            json.dump(value, stream)

    def _write_jsonl(self, name, rows):
        with open(os.path.join(self.run_dir, name), "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")

    def test_loader_and_counts_use_only_logged_fields(self):
        run = load_run(self.run_dir)
        counts = per_step_bloc_counts(run)
        self.assertEqual(run["agent_ids"], [0, 1])
        self.assertEqual(counts[1]["qwen"], {
            "agents": 1,
            "sent": 1,
            "move": 1,
            "stay": 0,
        })
        self.assertEqual(counts[1]["llama"], {
            "agents": 1,
            "sent": 0,
            "move": 0,
            "stay": 1,
        })

    def test_default_output_is_outside_raw_run_and_versioned(self):
        out_dir = timestamped_out_dir(self.run_dir, "viz")
        expected_parent = os.path.join(
            self.temp.name,
            "derived",
            "output-test-run",
        )
        self.assertEqual(os.path.dirname(out_dir), expected_parent)
        self.assertTrue(os.path.basename(out_dir).startswith("viz-v1.0.0-"))
        self.assertFalse(os.path.exists(out_dir))

    def test_html_render_refuses_output_collision(self):
        out_dir = os.path.join(self.temp.name, "report-output")
        with mock.patch("sys.argv", [
            "render_report.py",
            self.run_dir,
            "--out",
            out_dir,
        ]):
            render_report.main()
        rendered = os.path.join(out_dir, "output-test-run.html")
        self.assertTrue(os.path.isfile(rendered))
        with mock.patch("sys.argv", [
            "render_report.py",
            self.run_dir,
            "--out",
            out_dir,
        ]):
            with self.assertRaises(FileExistsError):
                render_report.main()


if __name__ == "__main__":
    unittest.main()
