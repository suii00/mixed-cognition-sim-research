import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.agent import Agent
from engine.config import build_effective_config
from engine.llm_client import LLMTransportError
from engine.message_selection import (
    MESSAGE_PRESENTATION_VERSION,
    PROMPT_INPUTS_FILE,
    RECENT_MESSAGE_SELECTION_POLICY as RECENT,
    RETAIN_OFFICIAL_WARNING_SELECTION_POLICY as RETAIN,
)
from engine.provenance import RunCollisionError, build_raw_manifest, compute_config_hash
from engine.sim import Simulation, SimulationAbortedError
from tests.test_disaster_simulation import disaster_config, read_jsonl
from tests.test_observability import instrumented_outcome, make_config as observability_config
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_agent(policy=RECENT, context=5, history=5):
    return Agent(
        0, "fixture", "fixture", "http://127.0.0.1:1", (0, 0),
        5, 5, history, context, message_selection_policy=policy,
    )


class MessageSelectionTests(unittest.TestCase):
    def test_recent_matches_legacy_slice_and_default_config_is_not_rewritten(self):
        agent = make_agent()
        agent.add_official_warning("w", {"fact": "fixture"}, 1)
        for index in range(6):
            agent.add_received_message(index + 1, f"peer-{index}", 1)
        self.assertEqual(agent.get_recent_messages(), agent.received_messages[-5:])
        self.assertTrue(all("sender_id" in row for row in agent.get_recent_messages()))
        config = disaster_config("implicit-recent", "free_text")
        original = copy.deepcopy(config)
        self.assertNotIn("message_selection_policy", build_effective_config(config)["agents"])
        self.assertEqual(config, original)

    def test_retention_survives_history_truncation_without_reinserting_receipts(self):
        agent = make_agent(RETAIN)
        payload = {"fact": "fixture"}
        agent.add_official_warning("w", payload, 1)
        payload["fact"] = "changed outside the receiver"
        for index in range(12):
            agent.add_received_message(index + 1, f"peer-{index}", index + 1)
        selected = agent.get_recent_messages()
        self.assertEqual(selected[0], {
            "source_type": "official_warning", "warning_id": "w",
            "payload": {"fact": "fixture"}, "step": 1,
        })
        self.assertEqual([row["sender_id"] for row in selected[1:]], [9, 10, 11, 12])
        self.assertEqual(len(agent.received_messages), 5)
        self.assertTrue(all("sender_id" in row for row in agent.received_messages))
        self.assertEqual(agent.get_recent_messages(), selected)

    def test_nonrecipients_and_peer_warning_claims_do_not_acquire_retention(self):
        recent, retained = make_agent(), make_agent(RETAIN)
        for index in range(8):
            message = "Official environment warning w" if index == 0 else f"peer-{index}"
            for agent in (recent, retained):
                agent.add_received_message(index + 1, message, 1)
        self.assertEqual(recent.get_recent_messages(), retained.get_recent_messages())
        self.assertEqual(recent.received_messages, retained.received_messages)

    def test_one_slot_and_latest_direct_warning_are_explicit(self):
        agent = make_agent(RETAIN, context=1)
        agent.add_official_warning("w1", "first", 1)
        agent.add_received_message(1, "peer", 1)
        agent.add_official_warning("w2", "second", 2)
        selected = agent.get_recent_messages()
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["warning_id"], "w2")
        self.assertEqual(selected[0]["step"], 2)

    def test_invalid_policy_and_retention_limits_fail_before_output_creation(self):
        config = disaster_config("invalid-retention", "free_text")
        for policy in (None, False, 1, {}, "unknown"):
            candidate = copy.deepcopy(config)
            candidate["agents"]["message_selection_policy"] = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "message_selection_policy"):
                build_effective_config(candidate)
        for field in ("message_history_limit", "message_context_size"):
            for value in (0, -1, None, True, 1.5):
                candidate = copy.deepcopy(config)
                candidate["agents"]["message_selection_policy"] = RETAIN
                candidate["agents"][field] = value
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                        Simulation(candidate, output_root=root, transport=lambda *_: None)
                    self.assertEqual(list(root.iterdir()), [])
        a, b = copy.deepcopy(config), copy.deepcopy(config)
        a["agents"]["message_selection_policy"] = RECENT
        b["agents"]["message_selection_policy"] = RETAIN
        self.assertNotEqual(compute_config_hash(build_effective_config(a)), compute_config_hash(build_effective_config(b)))


class MessagePresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for patcher in (
            mock.patch("engine.provenance.collect_git_info", return_value={
                "git_sha": "d" * 40, "git_dirty": True,
                "git_probe_status": "available", "git_probe_errors": [],
            }),
            mock.patch("engine.provenance.collect_gpu_info", return_value={
                "status": "unavailable", "error": "test_disabled",
                "driver_version": None, "cuda_version": None, "devices": [],
            }),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def new_simulation(self, run_id, policy=RECENT, observe=True, transport=None, mode="free_text"):
        config = disaster_config(run_id, mode)
        config["blocs"] = observability_config(run_id, agents=7)["blocs"]
        config["agents"].update({
            "message_selection_policy": policy,
            "message_history_limit": 5, "message_context_size": 5,
        })
        if observe:
            config["simulation"]["input_observability_version"] = MESSAGE_PRESENTATION_VERSION
        self.transcript = []

        def respond(request, telemetry):
            self.transcript.append(copy.deepcopy(request))
            telemetry("http_attempt", 1)
            parsed = (
                {"message": f"peer-{request.agent_id}", "reasoning": ""}
                if request.phase == "phase1" else {
                    "action": "move", "direction": "right",
                    "memory": f"memory-{request.step}-{request.agent_id}", "reasoning": "",
                }
            )
            return instrumented_outcome(request, parsed)

        return Simulation(config, output_root=self.root, repo_root=REPO_ROOT, transport=transport or respond)

    def run_fixture(self, run_id, policy=RECENT, observe=True, mode="free_text"):
        simulation = self.new_simulation(run_id, policy, observe, mode=mode)
        with mock.patch("builtins.print"):
            simulation.run()
        return simulation, Path(simulation.output_dir)

    def rewrite_fixture_manifest(self, output):
        # Only synthetic temporary fixtures are edited to exercise detection.
        meta_path = output / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"] = build_raw_manifest(output, meta["raw_manifest"]["files"])
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def test_ab_exact_inputs_survive_barriers_and_do_not_repeat_exposure(self):
        selections = {}
        exposure_rows = []
        for name, policy in (("a", RECENT), ("b", RETAIN)):
            simulation, output = self.run_fixture(f"presentation-{name}", policy)
            self.assertEqual(validate_run(output, strict=True).errors, [])
            rows = read_jsonl(output / PROMPT_INPUTS_FILE)
            self.assertEqual(len(rows), 28)
            requests = {request.request_id: request for request in self.transcript}
            for row in rows:
                self.assertEqual(row["prompt"], requests[row["request_id"]].prompt)
                self.assertEqual(row["prompt_sha256"], hashlib.sha256(row["prompt"].encode("utf-8")).hexdigest())
            selections[name] = {(row["step"], row["phase"], row["agent_id"]): row["messages"] for row in rows}
            self.assertTrue(selections[name][(1, "phase1", 0)][0].get("warning_id"))
            self.assertTrue(all(not selections[name][(1, "phase1", agent_id)] for agent_id in range(1, 7)))
            exposure_rows.append([
                {key: value for key, value in row.items() if key != "event_id"}
                for row in read_jsonl(output / "warning_events.jsonl")
            ])
            self.assertEqual(simulation.run_lifecycle.meta["input_observability_version"], MESSAGE_PRESENTATION_VERSION)
            self.assertIn(PROMPT_INPUTS_FILE, simulation.run_lifecycle.meta["raw_manifest"]["files"])
        self.assertEqual(exposure_rows[0], exposure_rows[1])
        self.assertEqual(len(exposure_rows[0]), 2)
        for key in ((1, "phase3", 0), (2, "phase1", 0), (2, "phase3", 0)):
            self.assertTrue(all("sender_id" in row for row in selections["a"][key]))
            self.assertEqual(selections["b"][key][0]["warning_id"], "warning-1")
            self.assertEqual(len(selections["b"][key]), 5)
        for key in selections["a"]:
            if key[2] != 0:
                self.assertEqual(selections["a"][key], selections["b"][key])

    def test_disabled_observability_preserves_streams_and_outputs(self):
        _, old = self.run_fixture("presentation-off", observe=False)
        _, new = self.run_fixture("presentation-on")
        self.assertFalse((old / PROMPT_INPUTS_FILE).exists())
        self.assertEqual(validate_run(old, strict=True).errors, [])
        for filename in ("phase1_raw.jsonl", "messages.jsonl", "memory_reasoning.jsonl"):
            self.assertEqual((old / filename).read_bytes(), (new / filename).read_bytes())

    def test_communication_none_never_prepares_phase1_or_retains_undelivered_warning(self):
        _, output = self.run_fixture("presentation-none", RETAIN, mode="communication_none")
        rows = read_jsonl(output / PROMPT_INPUTS_FILE)
        self.assertEqual(len(rows), 14)
        self.assertTrue(all(row["phase"] == "phase3" and row["messages"] == [] for row in rows))
        self.assertEqual(validate_run(output, strict=True).errors, [])

    def test_aborted_prepared_inputs_are_manifested_and_collision_is_read_only(self):
        def fail(_request, _telemetry):
            raise LLMTransportError("fixture")
        simulation = self.new_simulation("presentation-abort", RETAIN, transport=fail)
        with mock.patch("builtins.print"), self.assertRaises(SimulationAbortedError):
            simulation.run()
        output = Path(simulation.output_dir)
        rows = read_jsonl(output / PROMPT_INPUTS_FILE)
        self.assertEqual(len(rows), 7)
        self.assertEqual({row["phase"] for row in rows}, {"phase1"})
        self.assertEqual(simulation.run_lifecycle.meta["status"], "aborted")
        self.assertIn(PROMPT_INPUTS_FILE, simulation.run_lifecycle.meta["raw_manifest"]["files"])
        before = {path.name: path.read_bytes() for path in output.iterdir()}
        with self.assertRaises(RunCollisionError):
            self.new_simulation("presentation-abort", RETAIN)
        self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})

    def test_recomputed_hash_cannot_hide_prompt_or_selection_tampering(self):
        for field in ("prompt", "messages", "missing", "duplicate"):
            with self.subTest(field=field):
                _, output = self.run_fixture(f"presentation-tampered-{field}", RETAIN)
                rows = read_jsonl(output / PROMPT_INPUTS_FILE)
                if field == "prompt":
                    rows[0]["prompt"] += "\nfixture extra directive"
                    rows[0]["prompt_sha256"] = hashlib.sha256(rows[0]["prompt"].encode("utf-8")).hexdigest()
                elif field == "messages":
                    rows[0]["messages"] = []
                elif field == "missing":
                    rows.pop()
                else:
                    rows.append(copy.deepcopy(rows[-1]))
                (output / PROMPT_INPUTS_FILE).write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                self.rewrite_fixture_manifest(output)
                errors = validate_run(output, strict=True).errors
                self.assertTrue(any(PROMPT_INPUTS_FILE in error for error in errors), errors)

    def test_invalid_observability_version_does_not_create_output(self):
        config = disaster_config("invalid-observation", "free_text")
        config["simulation"]["input_observability_version"] = "unknown"
        with self.assertRaisesRegex(ValueError, "input_observability_version"):
            Simulation(config, output_root=self.root, transport=lambda *_: None)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
