"""Replay semantics and read-only publication boundaries, without model calls."""

import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import render_disaster_run as renderer


def fixture(agent_count=3, duration=60, completed=31):
    meta = {"run_id": "replay-fixture", "status": "completed" if completed == duration else "aborted",
        "completed_steps": completed, "git_sha": "a" * 40, "config": {
            "simulation": {"duration": duration, "seed": 1, "half_space_size": 5},
            "agents": {"communication_radius": 12},
            "scenario": {"type": "disaster_v1", "official_warning": {"warning_id": "warning-test"},
                "refuges": [{"refuge_id": "refuge-test", "rectangle": {"x_min": -5, "x_max": -4, "y_min": 4, "y_max": 5}}]}}}
    records = {name: [] for name in renderer.FILES}

    def add(name, row):
        records[name].append({"row": row, "reference": {
            "file": name, "line": len(records[name]) + 1, "sha256": "b" * 64}})

    for step in range(completed + 1):
        for aid in range(agent_count):
            add("positions.jsonl", {"step": step, "phase": "post_movement" if step else "initial",
                "agent_id": aid, "model": ["qwen", "llama", "gemma"][aid % 3], "position": [5, -5], "refuge_id": None})
            if step:
                text = "warning-test" if aid == 1 and step in (10, 11, 13) else "warning-test-extra" if step == 12 else ""
                add("phase1_raw.jsonl", {"step": step, "agent_id": aid, "parsed": {"message": text}})
                add("memory_reasoning.jsonl", {"step": step, "agent_id": aid, "action": "move", "direction": "right",
                    "memory": "warning-test", "reasoning": ""})
        if step:
            add("world_events.jsonl", {"event_type": "hazard_state", "step": step,
                "rectangles": [] if step < 10 else [{"x_min": -5, "x_max": 5, "y_min": -5, "y_max": -2 if step < 30 else 0}]})
    add("warning_events.jsonl", {"event_type": "warning_issued", "step": 10, "warning_id": "warning-test", "recipient_ids": [1], "payload": "Official warning warning-test"})
    for aid, step in ((1, 10), (2, 11)):
        add("warning_events.jsonl", {"event_type": "warning_exposure", "step": step,
            "warning_id": "warning-test", "recipient_id": aid})
    add("messages.jsonl", {"step": 11, "sender_id": 1, "receiver_ids": [2], "message": "warning-test"})
    add("messages.jsonl", {"step": 12, "sender_id": 1, "receiver_ids": [], "message": "warning-test"})
    return meta, records


def frame(replay, step, phase="end"):
    return next(f for f in replay["frames"] if f["step"] == step and f["phase"] == phase)


class ReplaySemanticsTests(unittest.TestCase):
    def setUp(self):
        self.meta, self.records = fixture()
        self.replay = renderer.build_replay(self.meta, self.records)

    def test_phase_positions_remaining_and_margin_are_not_off_by_one(self):
        pre = frame(self.replay, 10, "communication")
        end = frame(self.replay, 10)
        self.assertEqual((pre["position_step"], end["position_step"]), (9, 10))
        self.assertEqual((pre["agents"][0]["remaining"], end["agents"][0]["remaining"]), (51, 50))
        self.assertEqual(pre["agents"][0]["distance"], 18)
        self.assertEqual(pre["agents"][0]["margin"], 33)
        self.assertEqual(pre["agents"][0]["position_reference"]["line"], 28)
        self.assertEqual(end["agents"][0]["position_reference"]["line"], 31)
        meta = copy.deepcopy(self.meta)
        meta["config"]["simulation"]["duration"] = 31
        self.assertEqual(frame(renderer.build_replay(meta, self.records), 31)["agents"][0]["margin"], -18)

    def test_hazard_comes_from_same_step_and_warning_not_reissued_at_30(self):
        self.assertEqual(frame(self.replay, 9)["hazard"], [])
        self.assertEqual(frame(self.replay, 10, "communication")["hazard"][0]["y_max"], -2)
        self.assertEqual(frame(self.replay, 29)["hazard"][0]["y_max"], -2)
        self.assertEqual(frame(self.replay, 30)["hazard"][0]["y_max"], 0)
        self.assertEqual(len(frame(self.replay, 10)["official_now"]), 1)
        self.assertEqual(frame(self.replay, 30)["official_now"], [])
        self.assertEqual(len(frame(self.replay, 30)["official_history"]), 1)

    def test_exposure_and_later_exact_message_reuse_are_distinct(self):
        self.assertIsNone(frame(self.replay, 9)["agents"][1]["exposure_step"])
        same = frame(self.replay, 10, "communication")["agents"][1]
        self.assertEqual(same["exposure_step"], 10)
        self.assertFalse(same["reuse_now"])
        self.assertTrue(frame(self.replay, 11, "communication")["agents"][1]["reuse_now"])
        self.assertFalse(frame(self.replay, 12)["agents"][1]["reuse_now"])
        self.assertEqual(frame(self.replay, 31)["agents"][1]["reuse_history"], [11, 13])
        self.assertEqual(frame(self.replay, 31)["agents"][2]["reuse_history"], [])
        # Memory exact-ID mentions never become Phase 1 message reuse.
        self.assertEqual(frame(self.replay, 31)["agents"][0]["reuse_history"], [])

    def test_real_receivers_only_and_arrows_only_at_communication_positions(self):
        pre = frame(self.replay, 11, "communication")
        end = frame(self.replay, 11)
        self.assertTrue(pre["draw_delivery_arrows"])
        self.assertFalse(end["draw_delivery_arrows"])
        self.assertEqual(pre["deliveries"], end["deliveries"])
        self.assertEqual(pre["deliveries"][0]["receivers"], [2])
        self.assertTrue(pre["deliveries"][0]["warning_carrier"])
        self.assertEqual(frame(self.replay, 12, "communication")["deliveries"], [])

    def test_move_command_does_not_invent_displacement_or_arrival(self):
        agent = frame(self.replay, 10)["agents"][0]
        self.assertEqual(agent["action"]["row"]["action"], "move")
        self.assertEqual(agent["displacement"], [0, 0])
        self.assertEqual(frame(self.replay, 31)["arrival_count"], 0)
        self.assertIsNone(frame(self.replay, 10, "communication")["agents"][0]["action"])

    def test_24_mixed_agents_and_120_step_horizon(self):
        meta, records = fixture(agent_count=24, duration=120, completed=120)
        replay = renderer.build_replay(meta, records)
        self.assertEqual(len(replay["frames"]), 241)
        self.assertEqual(len(frame(replay, 120)["agents"]), 24)
        self.assertEqual(len(replay["model_colors"]), 3)
        self.assertEqual(frame(replay, 120)["agents"][0]["remaining"], 0)

    def test_aborted_step_does_not_invent_a_complete_frame(self):
        self.records["phase1_raw.jsonl"].append({"row": {"step": 32, "agent_id": 0, "parsed": None}, "reference": {}})
        replay = renderer.build_replay(self.meta, self.records)
        self.assertEqual(replay["status"], "aborted")
        self.assertEqual(replay["frames"][-1]["step"], 31)
        meta, records = fixture(completed=0)
        self.assertEqual(len(renderer.build_replay(meta, records)["frames"]), 1)

    def test_missing_hazard_or_position_fails_closed(self):
        for name in ("positions.jsonl", "world_events.jsonl"):
            records = copy.deepcopy(self.records)
            records[name].pop()
            with self.assertRaises(ValueError):
                renderer.build_replay(self.meta, records)

    def test_json_script_cannot_close_data_element_and_no_raw_inner_html(self):
        malicious = "</script><script>throw Error('injected')</script>\u2028\u2029"
        value = {"message": malicious}
        encoded = renderer.script_json(value)
        self.assertNotIn("<", encoded)
        self.assertNotIn("\u2028", encoded)
        self.assertEqual(json.loads(encoded), value)
        output = renderer.render_html({**self.replay, "untrusted": malicious}).decode()
        self.assertNotIn(malicious, output)
        self.assertNotIn("innerHTML", output)
        self.assertIn("connect-src 'none'", output)


class ReplayBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="disaster-renderer-")
        self.root = Path(self.temp.name)
        self.run = self.root / "runs" / "output_fixture"
        self.run.mkdir(parents=True)
        self.output = self.root / "derived" / f"{renderer.VERSION}_20260910T010000Z"

    def tearDown(self):
        self.temp.cleanup()

    def test_collision_raw_ancestry_version_and_timestamp_rejected(self):
        self.output.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            renderer.require_output_path(self.output, self.run)
        with self.assertRaises(ValueError):
            renderer.require_output_path(self.run / "derived" / self.output.name, self.run)
        with self.assertRaises(ValueError):
            renderer.require_output_path(self.root / "derived" / "unversioned", self.run)
        other = self.root / "immutable"
        other.mkdir()
        (other / "run_meta.json").write_text("{}")
        with self.assertRaises(ValueError):
            renderer.require_output_path(other / "derived" / self.output.name, self.run)

    def test_symlink_ancestry_is_rejected(self):
        link = self.root / "linked"
        try:
            link.symlink_to(self.run, target_is_directory=True)
        except OSError:
            self.skipTest("host does not permit unprivileged symlink creation")
        with self.assertRaises(ValueError):
            renderer.snapshot_tree(link)
        with self.assertRaises(ValueError):
            renderer.require_output_path(link / "derived" / self.output.name, self.run)

    def test_reparse_ancestor_is_rejected_without_host_symlink_privilege(self):
        original = renderer.is_link
        def reparse(path):
            return path == self.run.parent or original(path)
        with mock.patch.object(renderer, "is_link", side_effect=reparse):
            with self.assertRaises(ValueError):
                renderer.snapshot_tree(self.run)

    def test_unsafe_decoded_response_body_is_rejected_before_output(self):
        # Construct the fixture marker without placing a private address in source.
        marker = ".".join(map(str, (10, 98, 76, 54)))
        encoded = base64.b64encode(json.dumps({"message": marker}).encode()).decode()
        (self.run / "attempts.jsonl").write_text(json.dumps({"http_response_body_base64": encoded}) + "\n")
        with mock.patch.object(renderer, "validate_run") as validation:
            with self.assertRaisesRegex(ValueError, "publication boundary"):
                renderer.create_replay(self.run, self.output)
            validation.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_changed_raw_during_rendering_prevents_output_creation(self):
        meta, records = fixture()
        (self.run / "run_meta.json").write_text(json.dumps(meta))

        def mutate(_):
            (self.run / "new.json").write_text("{}")
            return b"png"

        with mock.patch.object(renderer, "public_tree_safe", return_value=True), \
                mock.patch.object(renderer, "validate_run", return_value=mock.Mock(valid=True, unverifiable=[])), \
                mock.patch.object(renderer, "read_records", return_value=records), \
                mock.patch.object(renderer, "render_png", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "run changed"):
                renderer.create_replay(self.run, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
