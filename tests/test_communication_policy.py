import copy
import json
import tempfile
import unittest
from pathlib import Path

from engine.config import build_effective_config
from engine.provenance import (
    RAW_JSONL_FILES,
    build_raw_manifest,
    compute_config_hash,
)
from engine.sim import Simulation
from tests.gate3_fixtures import (
    REPO_ROOT,
    patched_gate3_environment,
    read_jsonl,
)
from tools.eight_cell_runner import ScriptedSmokeTransport
from tools.validate_run import validate_run


def communication_config(run_id: str, policy=...):
    config = {
        "simulation": {
            "duration": 1,
            "half_space_size": 3,
            "seed": 99,
            "run_name": run_id,
            "run_id": run_id,
            "protocol_version": "gate3-edge-test-v1",
            "metric_version": "metric-v2.0.0",
        },
        "blocs": [
            {
                "name": name,
                "model": "scripted",
                "endpoint_id": "scripted-endpoint",
                "num_agents": 2,
            }
            for name in ("alpha", "beta", "neutral")
        ],
        "agents": {
            "communication_radius": 100,
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 20,
            "message_context_size": 20,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 32,
            "timeout_s": 1,
            "max_concurrency": 3,
        },
    }
    if policy is not ...:
        config["agents"]["edge_policy"] = policy
    return config


class CommunicationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patched_gate3_environment()
        self.environment.__enter__()
        self.addCleanup(self.environment.__exit__, None, None, None)

    def run_fixture(self, run_id: str, policy=...):
        simulation = Simulation(
            communication_config(run_id, policy),
            output_root=self.root,
            repo_root=REPO_ROOT,
            transport=ScriptedSmokeTransport(),
        )
        fixed = [(-2, -2), (-1, -1), (0, 0), (1, 1), (2, 2), (0, 2)]
        for agent, position in zip(simulation.agents, fixed):
            agent.position = position
        simulation.run()
        return simulation

    def test_edge_policy_default_validation_ownership_and_hash(self):
        omitted = communication_config("omitted")
        original = copy.deepcopy(omitted)
        self.assertEqual(
            build_effective_config(omitted)["agents"]["edge_policy"],
            "full",
        )
        self.assertEqual(omitted, original)
        for policy in ("full", "none", "within_bloc_only"):
            effective = build_effective_config(
                communication_config(f"explicit-{policy}", policy)
            )
            self.assertEqual(effective["agents"]["edge_policy"], policy)
        for invalid in ("unknown", True, False, None, 1):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "edge_policy"):
                    build_effective_config(communication_config("bad", invalid))
        full = build_effective_config(communication_config("same", "full"))
        within = build_effective_config(
            communication_config("same", "within_bloc_only")
        )
        self.assertNotEqual(compute_config_hash(full), compute_config_hash(within))
        simulation = self.run_fixture("snapshot-default")
        self.assertEqual(
            simulation.run_lifecycle.meta["config"]["agents"]["edge_policy"],
            "full",
        )

    def test_omitted_and_explicit_full_are_scientifically_identical(self):
        omitted = self.run_fixture("full-omitted")
        explicit = self.run_fixture("full-explicit", "full")
        for filename in RAW_JSONL_FILES:
            self.assertEqual(
                (Path(omitted.output_dir) / filename).read_bytes(),
                (Path(explicit.output_dir) / filename).read_bytes(),
            )
        omitted_state = [
            (agent.position, agent.memories, agent.received_messages)
            for agent in omitted.agents
        ]
        explicit_state = [
            (agent.position, agent.memories, agent.received_messages)
            for agent in explicit.agents
        ]
        self.assertEqual(omitted_state, explicit_state)

    def test_within_bloc_retains_same_bloc_and_removes_cross_bloc_edges(self):
        full = self.run_fixture("policy-full", "full")
        within = self.run_fixture("policy-within", "within_bloc_only")
        full_rows = read_jsonl(Path(full.output_dir) / "messages.jsonl")
        within_rows = read_jsonl(Path(within.output_dir) / "messages.jsonl")
        labels = {agent.agent_id: agent.bloc for agent in within.agents}
        self.assertTrue(
            any(
                labels[row["sender_id"]] != labels[receiver]
                for row in full_rows
                for receiver in row["receiver_ids"]
            )
        )
        self.assertTrue(within_rows)
        self.assertTrue(
            all(
                labels[row["sender_id"]] == labels[receiver]
                for row in within_rows
                for receiver in row["receiver_ids"]
            )
        )
        self.assertTrue(
            all(row["receiver_ids"] == sorted(row["receiver_ids"]) for row in within_rows)
        )
        self.assertTrue(validate_run(full.output_dir, strict=True).valid)
        self.assertTrue(validate_run(within.output_dir, strict=True).valid)

    def test_none_keeps_phase1_generation_but_suppresses_every_delivery(self):
        suppressed = self.run_fixture("policy-none", "none")
        run_dir = Path(suppressed.output_dir)
        phase1_rows = read_jsonl(run_dir / "phase1_raw.jsonl")
        message_rows = read_jsonl(run_dir / "messages.jsonl")
        self.assertEqual(len(phase1_rows), 6)
        self.assertEqual(message_rows, [])
        self.assertEqual(suppressed.total_llm_calls, 12)
        self.assertTrue(
            all(agent.received_messages == [] for agent in suppressed.agents)
        )
        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)

    @staticmethod
    def rewrite_messages(run_dir: Path, rows: list[dict]) -> None:
        (run_dir / "messages.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
            newline="\n",
        )
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["raw_manifest"] = build_raw_manifest(run_dir)
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def test_strict_validator_detects_cross_bloc_injection_and_same_bloc_omission(self):
        injected = self.run_fixture("within-injected", "within_bloc_only")
        injected_dir = Path(injected.output_dir)
        rows = read_jsonl(injected_dir / "messages.jsonl")
        rows[0]["receiver_ids"].append(2)
        rows[0]["receiver_ids"].sort()
        self.rewrite_messages(injected_dir, rows)
        self.assertFalse(validate_run(injected_dir, strict=True).valid)

        omitted = self.run_fixture("within-omission", "within_bloc_only")
        omitted_dir = Path(omitted.output_dir)
        rows = read_jsonl(omitted_dir / "messages.jsonl")
        rows.pop(0)
        self.rewrite_messages(omitted_dir, rows)
        self.assertFalse(validate_run(omitted_dir, strict=True).valid)

    def test_legacy_config_without_policy_is_validated_as_full(self):
        simulation = self.run_fixture("legacy-full", "full")
        run_dir = Path(simulation.output_dir)
        meta_path = run_dir / "run_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["config"]["agents"].pop("edge_policy")
        meta["config_hash"] = compute_config_hash(meta["config"])
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report = validate_run(run_dir, strict=True)
        self.assertTrue(report.valid, report.errors)


if __name__ == "__main__":
    unittest.main()
