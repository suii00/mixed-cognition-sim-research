import copy
import hashlib
import random
import unittest

from engine.config import build_effective_config
from engine.disaster import contains_warning_identifier, parse_disaster_scenario
from engine.world import World
from engine.prompts import build_phase1_prompt, build_phase3_prompt


def scenario_config(mode: str = "free_text") -> dict:
    return {
        "schema_version": "disaster-scenario-v1.0.0",
        "type": "disaster_v1",
        "communication_mode": mode,
        "hazard": {
            "hazard_id": "hazard-1",
            "stages": [
                {
                    "start_step": 3,
                    "rectangles": [
                        {"x_min": -4, "x_max": 1, "y_min": -4, "y_max": 1}
                    ],
                },
                {
                    "start_step": 5,
                    "rectangles": [
                        {"x_min": -4, "x_max": 2, "y_min": -4, "y_max": 2}
                    ],
                },
            ],
        },
        "refuges": [
            {
                "refuge_id": "refuge-1",
                "rectangle": {"x_min": 4, "x_max": 5, "y_min": 4, "y_max": 5},
            }
        ],
        "official_warning": {
            "warning_id": "warning-1",
            "issue_step": 3,
            "initial_recipient_ids": [0, 2],
        },
        "initial_eligible_rectangles": [
            {"x_min": -5, "x_max": 3, "y_min": -5, "y_max": 3}
        ],
    }


class DisasterScenarioTests(unittest.TestCase):
    def parse(self, config: dict | None = None):
        return parse_disaster_scenario(
            config or scenario_config(),
            half_space_size=5,
            duration=6,
            total_agents=4,
        )

    def test_hazard_state_uses_only_latest_started_stage(self):
        scenario = self.parse()
        self.assertFalse(scenario.is_hazardous(2, 0, 0))
        self.assertTrue(scenario.is_hazardous(3, 0, 0))
        self.assertFalse(scenario.is_hazardous(3, 2, 2))
        self.assertTrue(scenario.is_hazardous(5, 2, 2))

    def test_refuge_and_distance_are_mechanical(self):
        scenario = self.parse()
        self.assertEqual(scenario.refuge_for(4, 4).refuge_id, "refuge-1")
        self.assertIsNone(scenario.refuge_for(3, 3))
        self.assertEqual(scenario.shortest_refuge_distance(3, 3), 2)
        self.assertEqual(scenario.shortest_refuge_distance(4, 4), 0)

    def test_free_and_structured_warning_share_canonical_facts(self):
        free = self.parse(scenario_config("free_text"))
        structured = self.parse(scenario_config("structured_warning"))
        self.assertEqual(free.warning_facts(), structured.warning_facts())
        self.assertIsInstance(free.warning_payload(), str)
        self.assertEqual(structured.warning_payload(), structured.warning_facts())
        self.assertIn("warning-1", free.warning_payload())

    def test_communication_none_has_no_agent_payload_but_keeps_facts(self):
        scenario = self.parse(scenario_config("communication_none"))
        self.assertIsNone(scenario.warning_payload())
        self.assertEqual(scenario.warning_facts()["warning_id"], "warning-1")

    def test_unknown_field_fails_closed(self):
        config = scenario_config()
        config["desired_outcome"] = "evacuate"
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            self.parse(config)

    def test_future_or_duplicate_hazard_stages_fail(self):
        config = scenario_config()
        config["hazard"]["stages"][1]["start_step"] = 3
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            self.parse(config)

    def test_refuge_overlapping_issue_hazard_fails(self):
        config = scenario_config()
        config["refuges"][0]["rectangle"] = {
            "x_min": 0,
            "x_max": 1,
            "y_min": 0,
            "y_max": 1,
        }
        with self.assertRaisesRegex(ValueError, "disjoint"):
            self.parse(config)

    def test_recipient_outside_agent_count_fails(self):
        config = scenario_config()
        config["official_warning"]["initial_recipient_ids"] = [4]
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.parse(config)

    def test_parsing_does_not_mutate_input(self):
        config = scenario_config()
        before = copy.deepcopy(config)
        self.parse(config)
        self.assertEqual(config, before)

    def test_initial_positions_are_paired_across_communication_modes(self):
        free = self.parse(scenario_config("free_text"))
        structured = self.parse(scenario_config("structured_warning"))
        none = self.parse(scenario_config("communication_none"))
        positions = [
            World(5, [], scenario).generate_initial_positions(4, random.Random(123))
            for scenario in (free, structured, none)
        ]
        self.assertEqual(positions[0], positions[1])
        self.assertEqual(positions[0], positions[2])
        self.assertTrue(all(free.refuge_for(*position) is None for position in positions[0]))

    def test_effective_config_validates_scenario_without_rewriting_it(self):
        scenario = scenario_config()
        config = {
            "simulation": {"half_space_size": 5, "duration": 6},
            "llm_defaults": {},
            "agents": {},
            "blocs": [
                {
                    "name": "a",
                    "model": "model",
                    "endpoint_id": "scenario-endpoint",
                    "num_agents": 4,
                }
            ],
            "scenario": scenario,
        }
        effective = build_effective_config(config)
        self.assertEqual(effective["scenario"], scenario)

    def test_warning_identifier_match_is_exact_and_prospective(self):
        warning_id = "warning-1"
        self.assertTrue(contains_warning_identifier("saw warning-1.", warning_id))
        self.assertTrue(contains_warning_identifier("(warning-1)", warning_id))
        self.assertFalse(contains_warning_identifier("warning-10", warning_id))
        self.assertFalse(contains_warning_identifier("xwarning-1", warning_id))
        self.assertFalse(contains_warning_identifier("warning-1_extra", warning_id))
        self.assertFalse(contains_warning_identifier("unrelated", warning_id))

    def test_non_disaster_prompt_bytes_remain_schema_1_1_compatible(self):
        kwargs = {
            "agent_id": 2, "x": 1, "y": -1, "half_space_size": 5,
            "places": [], "place": None, "agent_count": 0,
            "memories": ["m"],
            "messages": [{"sender_id": 1, "message": "x", "step": 1}],
        }
        expected = {
            build_phase1_prompt: "e74e52add2cb5c0ba6aa793a8e9c893083e994730235bff45883903110ecc9fc",
            build_phase3_prompt: "4612ba629e016ff779f7d5dc7badac128aca559430c98793abc1960c9318a5db",
        }
        for builder, digest in expected.items():
            self.assertEqual(
                hashlib.sha256(builder(**kwargs).encode("utf-8")).hexdigest(),
                digest,
            )


if __name__ == "__main__":
    unittest.main()
