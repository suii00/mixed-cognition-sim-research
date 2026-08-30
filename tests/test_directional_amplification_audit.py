import json
import unittest
from pathlib import Path

from engine.config import build_effective_config, validate_public_config_boundary
from tools.directional_amplification_core import (
    _cascade_summary,
    _reconstruct_phase3_visible,
    build_audit_bundle,
    json_bytes,
    load_and_validate_plan,
)
from tools.directional_amplification_incomplete_metric import (
    _all_threshold_outcome,
    _minimum_support_outcome,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_ROOT = REPO_ROOT / "configs" / "directional_amplification_audit_v1"
PLAN_PATH = AUDIT_ROOT / "plan.json"


class DirectionalAmplificationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = load_and_validate_plan(PLAN_PATH)
        cls.manifest, cls.config_bytes = build_audit_bundle(PLAN_PATH)

    def test_builder_freezes_exact_six_cell_cross_product(self):
        rows = self.manifest["rows"]
        self.assertEqual(
            [row["cell_id"] for row in rows],
            [
                "c03-r0",
                "c03-r1",
                "c03-r2",
                "c23-r0",
                "c23-r1",
                "c23-r2",
            ],
        )
        self.assertEqual(
            [row["high_agent_id_bloc"] for row in rows],
            ["elyza", "qwen", "swallow", "elyza", "qwen", "swallow"],
        )
        self.assertEqual(len({row["run_id"] for row in rows}), 6)
        self.assertEqual(len({row["config_sha256"] for row in rows}), 6)
        self.assertEqual(len({row["paired_control_sha256"] for row in rows}), 1)
        self.assertEqual(self.manifest["expected_total_llm_calls"], 2880)
        self.assertIs(self.manifest["research_eligible"], False)

    def test_generated_artifacts_match_builder_and_public_boundary(self):
        manifest_path = AUDIT_ROOT / "manifest.json"
        self.assertEqual(manifest_path.read_bytes(), json_bytes(self.manifest))
        observed = {
            str(path.relative_to(AUDIT_ROOT).as_posix())
            for path in (AUDIT_ROOT / "configs").glob("*.json")
        }
        self.assertEqual(observed, set(self.config_bytes))
        for relative, expected in self.config_bytes.items():
            path = AUDIT_ROOT / relative
            self.assertEqual(path.read_bytes(), expected)
            config = json.loads(expected)
            effective = build_effective_config(config)
            validate_public_config_boundary(effective)
            self.assertEqual(effective["simulation"]["duration"], 10)
            self.assertEqual(effective["simulation"]["seed"], 2403)
            self.assertIs(effective["simulation"]["research_eligible"], False)
            self.assertEqual(sum(row["num_agents"] for row in effective["blocs"]), 24)

    def test_context_intervention_and_rotations_are_exact(self):
        configs = {
            row["cell_id"]: json.loads(self.config_bytes[row["config_path"]])
            for row in self.manifest["rows"]
        }
        for rotation_id in ("r0", "r1", "r2"):
            c03 = configs[f"c03-{rotation_id}"]
            c23 = configs[f"c23-{rotation_id}"]
            self.assertEqual(c03["agents"]["message_history_limit"], 10)
            self.assertEqual(c03["agents"]["message_context_size"], 3)
            self.assertEqual(c23["agents"]["message_history_limit"], 23)
            self.assertEqual(c23["agents"]["message_context_size"], 23)
            c03["simulation"].pop("run_id")
            c03["simulation"].pop("run_name")
            c23["simulation"].pop("run_id")
            c23["simulation"].pop("run_name")
            c03["agents"].pop("message_history_limit")
            c03["agents"].pop("message_context_size")
            c23["agents"].pop("message_history_limit")
            c23["agents"].pop("message_context_size")
            self.assertEqual(c03, c23)

        expected_orders = {
            "r0": ["qwen", "swallow", "elyza"],
            "r1": ["swallow", "elyza", "qwen"],
            "r2": ["elyza", "qwen", "swallow"],
        }
        for rotation_id, expected_order in expected_orders.items():
            config = configs[f"c03-{rotation_id}"]
            self.assertEqual([row["name"] for row in config["blocs"]], expected_order)

    def test_visibility_reconstruction_exposes_last_bloc_under_context_three(self):
        agent_ids = list(range(24))
        messages = []
        for sender_id in agent_ids:
            bloc = (
                "qwen"
                if sender_id < 8
                else "swallow"
                if sender_id < 16
                else "elyza"
            )
            messages.append({
                "step": 1,
                "sender_id": sender_id,
                "sender_bloc": bloc,
                "receiver_ids": [value for value in agent_ids if value != sender_id],
                "message": f"message-{sender_id}",
            })
        c03 = _reconstruct_phase3_visible(messages, agent_ids, 1, 10, 3)
        c23 = _reconstruct_phase3_visible(messages, agent_ids, 1, 23, 23)
        self.assertTrue(
            all(
                len(rows) == 3
                and {row["sender_bloc"] for row in rows} == {"elyza"}
                for rows in c03.values()
            )
        )
        self.assertTrue(all(len(rows) == 23 for rows in c23.values()))
        self.assertTrue(
            all(
                {row["sender_bloc"] for row in rows}
                == {"qwen", "swallow", "elyza"}
                for rows in c23.values()
            )
        )

    def test_cascade_requires_same_direction_for_three_consecutive_steps(self):
        rows = {
            1: [{"action": "move", "direction": "right"}] * 18
            + [{"action": "move", "direction": "left"}] * 6,
            2: [{"action": "move", "direction": "right"}] * 19
            + [{"action": "move", "direction": "left"}] * 5,
            3: [{"action": "move", "direction": "left"}] * 18
            + [{"action": "move", "direction": "right"}] * 6,
            4: [{"action": "move", "direction": "right"}] * 20
            + [{"action": "move", "direction": "left"}] * 4,
            5: [{"action": "move", "direction": "right"}] * 21
            + [{"action": "move", "direction": "left"}] * 3,
            6: [{"action": "move", "direction": "right"}] * 22
            + [{"action": "move", "direction": "left"}] * 2,
        }
        _steps, onset = _cascade_summary(rows, 6, 0.75, 3)
        self.assertEqual(
            onset,
            {"direction": "right", "start_step": 4, "confirmed_step": 6},
        )

    def test_incomplete_all_cell_rule_can_fail_but_not_pass_early(self):
        self.assertEqual(_all_threshold_outcome([0.8, 0.9], 3, 0.75), "indeterminate")
        self.assertEqual(_all_threshold_outcome([0.8, 0.7], 3, 0.75), "failed")
        self.assertEqual(_all_threshold_outcome([0.8, 0.9, 1.0], 3, 0.75), "passed")

    def test_incomplete_minimum_support_rule_uses_remaining_upper_bound(self):
        self.assertEqual(_minimum_support_outcome(0, 2, 3, 2), "failed")
        self.assertEqual(_minimum_support_outcome(1, 2, 3, 2), "indeterminate")
        self.assertEqual(_minimum_support_outcome(2, 2, 3, 2), "passed")


if __name__ == "__main__":
    unittest.main()
