import base64
import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from engine.parallel_transport import LLMRequest, TransportOutcome
from engine.sim import Simulation
from tests.test_disaster_scenario import scenario_config
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def disaster_config(run_id: str, mode: str) -> dict:
    scenario = scenario_config(mode)
    scenario["hazard"]["stages"] = [
        {
            "start_step": 1,
            "rectangles": [
                {"x_min": -4, "x_max": 1, "y_min": -4, "y_max": 1}
            ],
        },
        {
            "start_step": 2,
            "rectangles": [
                {"x_min": -4, "x_max": 2, "y_min": -4, "y_max": 2}
            ],
        },
    ]
    scenario["official_warning"] = {
        "warning_id": "warning-1",
        "issue_step": 1,
        "initial_recipient_ids": [0],
    }
    return {
        "simulation": {
            "duration": 2,
            "half_space_size": 5,
            "seed": 2101,
            "run_name": run_id,
            "run_id": run_id,
            "protocol_version": "disaster-protocol-v1.0.0",
            "metric_version": "disaster-metric-v1.0.0",
        },
        "blocs": [{
            "name": "alpha",
            "model": "scripted-model",
            "endpoint_id": "scripted-endpoint",
            "num_agents": 3,
        }],
        "agents": {
            "communication_radius": 100,
            "memory_limit": 8,
            "memory_size": 8,
            "message_history_limit": 20,
            "message_context_size": 20,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 64,
            "timeout_s": 1,
            "max_concurrency": 3,
        },
        "scenario": scenario,
    }


class WarningRelayTransport:
    def __init__(self):
        self.transcript = []
        self.lock = threading.Lock()

    def __call__(self, request: LLMRequest, telemetry):
        with self.lock:
            self.transcript.append(copy.deepcopy(request))
        telemetry("http_attempt", 1)
        if request.phase == "phase1":
            message = "warning-1" if request.agent_id == 0 else ""
            parsed = {"message": message, "reasoning": "fixture"}
        else:
            parsed = {
                "action": "stay",
                "direction": None,
                "memory": "",
                "reasoning": "fixture",
            }
        raw_output = json.dumps(parsed, sort_keys=True)
        envelope = {
            "message": {"content": raw_output},
            "done_reason": "stop",
            "prompt_eval_count": 1,
            "eval_count": 1,
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        return TransportOutcome(
            parsed=parsed,
            raw_output=raw_output,
            attempts=({
                "generation_attempt": 1,
                "http_attempt": 1,
                "http_status": 200,
                "http_response_body_base64": base64.b64encode(body).decode("ascii"),
                "http_response_bytes": len(body),
                "http_response_sha256": hashlib.sha256(body).hexdigest(),
                "envelope": envelope,
                "raw_output": raw_output,
                "finish_reason": "stop",
                "usage": {"prompt_eval_count": 1, "eval_count": 1},
                "transport_status": "ok",
                "parse_status": "valid",
                "schema_status": "not_checked",
                "failure_kind": None,
                "error_type": None,
            },),
        )


class DisasterSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.output_root = Path(self.temp.name)
        patches = (
            mock.patch("engine.provenance.collect_git_info", return_value={
                "git_sha": "d" * 40,
                "git_dirty": True,
                "git_probe_status": "available",
                "git_probe_errors": [],
            }),
            mock.patch("engine.provenance.collect_gpu_info", return_value={
                "status": "unavailable",
                "error": "test_disabled",
                "driver_version": None,
                "cuda_version": None,
                "devices": [],
            }),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_mode(self, mode: str):
        transport = WarningRelayTransport()
        simulation = Simulation(
            disaster_config(f"disaster-{mode}", mode),
            output_root=self.output_root,
            repo_root=REPO_ROOT,
            transport=transport,
        )
        with mock.patch("builtins.print"):
            simulation.run()
        return simulation, transport, Path(simulation.output_dir)

    def test_communication_none_skips_phase1_and_preserves_world_observation(self):
        simulation, transport, output = self.run_mode("communication_none")
        self.assertEqual({request.phase for request in transport.transcript}, {"phase3"})
        self.assertEqual(len(transport.transcript), 6)
        self.assertEqual(read_jsonl(output / "phase1_raw.jsonl"), [])
        self.assertEqual(read_jsonl(output / "messages.jsonl"), [])
        warning_events = read_jsonl(output / "warning_events.jsonl")
        self.assertEqual([row["event_type"] for row in warning_events], ["warning_issued"])
        self.assertIsNone(warning_events[0]["payload"])
        self.assertEqual(len(read_jsonl(output / "world_events.jsonl")), 3)
        self.assertEqual(len(read_jsonl(output / "positions.jsonl")), 9)
        self.assertEqual(simulation.total_llm_calls, 6)
        validation = validate_run(output, strict=True)
        self.assertEqual(validation.errors, [])

    def test_warning_exposure_relay_and_time_local_prompts_are_raw_events(self):
        simulation, transport, output = self.run_mode("free_text")
        self.assertEqual(len(transport.transcript), 12)
        warning_events = read_jsonl(output / "warning_events.jsonl")
        issue = [row for row in warning_events if row["event_type"] == "warning_issued"]
        exposures = [row for row in warning_events if row["event_type"] == "warning_exposure"]
        self.assertEqual(len(issue), 1)
        self.assertEqual(
            [(row["source_type"], row["sender_id"], row["recipient_id"]) for row in exposures],
            [
                ("official", None, 0),
                ("agent_relay", 0, 1),
                ("agent_relay", 0, 2),
                ("agent_relay", 0, 1),
                ("agent_relay", 0, 2),
            ],
        )
        ids = [
            row["event_id"]
            for filename in ("world_events.jsonl", "positions.jsonl", "warning_events.jsonl")
            for row in read_jsonl(output / filename)
        ]
        self.assertEqual(len(ids), len(set(ids)))
        step1_prompts = [request.prompt for request in transport.transcript if request.step == 1]
        self.assertTrue(all("Current cell hazard classification:" in prompt for prompt in step1_prompts))
        self.assertTrue(all("x=-4..2, y=-4..2" not in prompt for prompt in step1_prompts))
        unexposed_phase1_prompt = next(
            request.prompt
            for request in transport.transcript
            if request.step == 1 and request.phase == "phase1" and request.agent_id == 1
        )
        self.assertNotIn("x=-4..1, y=-4..1", unexposed_phase1_prompt)
        recipient_prompt = next(
            request.prompt
            for request in transport.transcript
            if request.step == 1 and request.phase == "phase1" and request.agent_id == 0
        )
        self.assertIn("Official environment warning warning-1", recipient_prompt)
        self.assertEqual(simulation.total_llm_calls, 12)
        validation = validate_run(output, strict=True)
        self.assertEqual(validation.errors, [])

    def test_structured_warning_uses_canonical_facts_and_strictly_validates(self):
        _, transport, output = self.run_mode("structured_warning")
        warning_events = read_jsonl(output / "warning_events.jsonl")
        issue = warning_events[0]
        self.assertIsInstance(issue["payload"], dict)
        self.assertEqual(issue["payload"], issue["facts"])
        recipient_prompt = next(
            request.prompt
            for request in transport.transcript
            if request.step == 1 and request.phase == "phase1" and request.agent_id == 0
        )
        self.assertIn('"warning_id":"warning-1"', recipient_prompt)
        self.assertEqual(validate_run(output, strict=True).errors, [])


if __name__ == "__main__":
    unittest.main()
