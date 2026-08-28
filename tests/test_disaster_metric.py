import unittest

from tools.disaster_metric_core import derive_disaster_metrics


class DisasterMetricTests(unittest.TestCase):
    def test_suffix_residence_reuse_censoring_and_response(self):
        meta = {
            "run_id": "metric-fixture",
            "log_schema_version": "1.2.0",
            "expected_steps": 4,
            "expected_agents": 2,
            "config": {"scenario": {
                "communication_mode": "free_text",
                "hazard": {"stages": [{"start_step": 2}]},
                "official_warning": {"warning_id": "warn-1", "issue_step": 1},
            }},
        }
        positions = []
        for agent_id in range(2):
            positions.append({
                "step": 0, "phase": "initial", "agent_id": agent_id,
                "bloc": "a", "model": "m", "refuge_id": None,
                "hazardous": False, "shortest_refuge_distance": 4,
            })
        distances = {0: [4, 3, 0, 0], 1: [4, 4, 4, 4]}
        refuges = {0: [None, None, "r", "r"], 1: [None] * 4}
        hazards = {0: [False, True, False, False], 1: [False, True, True, True]}
        for agent_id in range(2):
            for step in range(1, 5):
                positions.append({
                    "step": step, "phase": "post_movement", "agent_id": agent_id,
                    "bloc": "a", "model": "m",
                    "refuge_id": refuges[agent_id][step - 1],
                    "hazardous": hazards[agent_id][step - 1],
                    "shortest_refuge_distance": distances[agent_id][step - 1],
                })
        phase1 = [
            {"step": 2, "agent_id": 0, "parsed": {"message": "warn-1"}},
            {"step": 2, "agent_id": 1, "parsed": {"message": "none"}},
        ]
        warning_events = [
            {"event_type": "warning_exposure", "recipient_id": agent_id,
             "step": 1, "source_type": "official"}
            for agent_id in range(2)
        ]
        result = derive_disaster_metrics(
            run_meta=meta,
            positions=positions,
            phase1=phase1,
            warning_events=warning_events,
        )
        first, second = result["agents"]
        self.assertEqual(first["dangerous_area_residence_steps"], 1)
        self.assertTrue(first["evacuation_success"])
        self.assertEqual(first["evacuation_completion_step"], 3)
        self.assertEqual(first["warning_reuse_delay_steps"], 1)
        self.assertEqual(first["movement_response_delay_steps"], 1)
        self.assertTrue(second["warning_reuse_right_censored"])
        self.assertEqual(second["warning_reuse_censor_step"], 4)
        self.assertIsNone(second["evacuation_completion_step"])


if __name__ == "__main__":
    unittest.main()
