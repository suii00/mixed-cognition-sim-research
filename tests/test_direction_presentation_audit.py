import copy
import json
import tempfile
import unittest
from pathlib import Path

from engine.config import build_effective_config, validate_public_config_boundary
from engine.provenance import compute_config_hash, compute_prompt_hash
from engine.response_contracts import response_schema_sha256
from tools.direction_presentation_core import (
    DirectionPresentationAuditError,
    analyze_audit,
    analyze_run,
    build_audit_bundle,
    load_and_validate_plan,
    load_json_unique,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = REPO_ROOT / "configs" / "direction_presentation_audit_v2" / "plan.json"
MANIFEST_PATH = PLAN_PATH.parent / "manifest.json"


def labels_for_config(config: dict) -> dict[int, tuple[str, str]]:
    labels = {}
    agent_id = 0
    for bloc in config["blocs"]:
        for _ in range(bloc["num_agents"]):
            labels[agent_id] = (bloc["name"], bloc["model"])
            agent_id += 1
    return labels


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def write_synthetic_run(
    root: Path,
    config: dict,
    manifest_row: dict,
    right_fraction: float,
) -> Path:
    run_id = manifest_row["run_id"]
    run_dir = root / f"output_{run_id}"
    run_dir.mkdir()
    effective = build_effective_config(copy.deepcopy(config))
    labels = labels_for_config(effective)
    phase1 = []
    actions = []
    messages = []
    sequence = 0
    for step in range(1, manifest_row["expected_steps"] + 1):
        for agent_id in range(manifest_row["expected_agents"]):
            bloc, model = labels[agent_id]
            phase1.append({
                "step": step,
                "agent_id": agent_id,
                "bloc": bloc,
                "model": model,
                "parsed": {"message": "周囲を確認する", "reasoning": ""},
                "raw_output": "synthetic",
            })
            direction = (
                "right"
                if sequence < int(
                    manifest_row["expected_steps"]
                    * manifest_row["expected_agents"]
                    * right_fraction
                )
                else "left"
            )
            actions.append({
                "step": step,
                "agent_id": agent_id,
                "bloc": bloc,
                "model": model,
                "action": "move",
                "direction": direction,
                "memory": "位置を記録する",
                "reasoning": "",
            })
            if manifest_row["message_delivery"]:
                messages.append({
                    "step": step,
                    "sender_id": agent_id,
                    "sender_bloc": bloc,
                    "sender_model": model,
                    "receiver_ids": [
                        value
                        for value in range(manifest_row["expected_agents"])
                        if value != agent_id
                    ],
                    "message": "周囲を確認する",
                    "reasoning": "",
                })
            sequence += 1
    write_jsonl(run_dir / "phase1_raw.jsonl", phase1)
    write_jsonl(run_dir / "memory_reasoning.jsonl", actions)
    write_jsonl(run_dir / "messages.jsonl", messages)
    write_jsonl(run_dir / "parse_errors.jsonl", [])
    write_jsonl(
        run_dir / "llm_attempts.jsonl",
        [{} for _ in range(manifest_row["expected_llm_calls"])],
    )
    meta = {
        "run_id": run_id,
        "status": "completed",
        "aborted": False,
        "completed_steps": manifest_row["expected_steps"],
        "expected_agents": manifest_row["expected_agents"],
        "logical_llm_calls": manifest_row["expected_llm_calls"],
        "git_sha": "b" * 40,
        "git_dirty": False,
        "config": effective,
        "config_hash": compute_config_hash(effective),
        "prompt_hash": compute_prompt_hash(
            REPO_ROOT, manifest_row["prompt_contract_version"]
        ),
        "response_schema_sha256": response_schema_sha256(
            manifest_row["response_contract_version"]
        ),
        "transport_failures": 0,
        "syntax_parse_failures": 0,
        "schema_validation_failures": 0,
    }
    (run_dir / "run_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return run_dir


class DirectionPresentationAuditTests(unittest.TestCase):
    def test_plan_and_bundle_freeze_twelve_readable_cells(self):
        plan = load_and_validate_plan(PLAN_PATH)
        manifest, configs = build_audit_bundle(PLAN_PATH)
        self.assertFalse(plan["research_eligible"])
        self.assertEqual(manifest["expected_total_runs"], 12)
        self.assertEqual(manifest["expected_total_llm_calls"], 5760)
        self.assertEqual(len(configs), 12)
        self.assertEqual(
            [row["cell_id"] for row in manifest["rows"]],
            [
                f"{presentation}-{delivery}-r{rotation}"
                for presentation in ("lr", "rl")
                for delivery in ("com", "iso")
                for rotation in range(3)
            ],
        )
        self.assertEqual(
            len({row["paired_control_sha256"] for row in manifest["rows"]}),
            1,
        )
        for row in manifest["rows"]:
            self.assertIn(f"-{row['cell_id']}-s2403-", row["run_id"])
            config = json.loads(configs[row["config_path"]])
            validate_public_config_boundary(config)
            self.assertFalse(config["simulation"]["research_eligible"])
            self.assertEqual(config["agents"]["edge_policy"], row["edge_policy"])

    def test_comm_and_iso_differ_only_by_declared_delivery_policy(self):
        manifest, configs = build_audit_bundle(PLAN_PATH)
        by_cell = {row["cell_id"]: row for row in manifest["rows"]}
        communicated = json.loads(configs[by_cell["lr-com-r0"]["config_path"]])
        isolated = json.loads(configs[by_cell["lr-iso-r0"]["config_path"]])
        for config in (communicated, isolated):
            config["simulation"].pop("run_id")
            config["simulation"].pop("run_name")
            config["agents"].pop("edge_policy")
        self.assertEqual(communicated, isolated)

    def test_analyze_run_separates_authorship_from_delivery(self):
        plan = load_and_validate_plan(PLAN_PATH)
        manifest = load_json_unique(MANIFEST_PATH)
        rows = {row["cell_id"]: row for row in manifest["rows"]}
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            for cell_id in ("lr-com-r0", "lr-iso-r0"):
                row = rows[cell_id]
                config = load_json_unique(MANIFEST_PATH.parent / row["config_path"])
                run_dir = write_synthetic_run(root, config, row, 1.0)
                result = analyze_run(run_dir, config, row, plan["decision_rules"])
                direct = result["direct_observation"]
                self.assertEqual(direct["phase1_rows"], 240)
                self.assertEqual(direct["authored_nonempty_message_rows"], 240)
                if row["message_delivery"]:
                    self.assertEqual(direct["delivered_message_rows"], 240)
                    self.assertGreater(
                        result["mechanical_derivation"][
                            "visible_context_slot_count"
                        ],
                        0,
                    )
                else:
                    self.assertEqual(direct["delivered_message_rows"], 0)
                    self.assertEqual(
                        result["mechanical_derivation"][
                            "visible_context_slot_count"
                        ],
                        0,
                    )

    def test_analyzer_computes_pre_registered_pairing(self):
        manifest = load_json_unique(MANIFEST_PATH)
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            for row in manifest["rows"]:
                config = load_json_unique(MANIFEST_PATH.parent / row["config_path"])
                fraction = {
                    ("lr", "com"): 1.0,
                    ("rl", "com"): 0.0,
                    ("lr", "iso"): 0.5,
                    ("rl", "iso"): 0.0,
                }[(row["presentation_id"], row["delivery_id"])]
                write_synthetic_run(root, config, row, fraction)
            result = analyze_audit(MANIFEST_PATH, root)
        decision = result["engineering_decision"]
        self.assertEqual(
            decision["presentation_sensitivity_supporting_rotations_by_delivery"],
            {"com": 3, "iso": 3},
        )
        self.assertEqual(
            decision[
                "communication_sensitivity_supporting_rotations_by_presentation"
            ],
            {"lr": 3, "rl": 0},
        )
        self.assertTrue(decision["presentation_communication_interaction_rule"])
        self.assertTrue(decision["zero_contract_failure_rule"])

    def test_analyzer_rejects_nonempty_reasoning(self):
        plan = load_and_validate_plan(PLAN_PATH)
        manifest = load_json_unique(MANIFEST_PATH)
        row = manifest["rows"][0]
        config = load_json_unique(MANIFEST_PATH.parent / row["config_path"])
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            run_dir = write_synthetic_run(Path(temp_dir), config, row, 1.0)
            actions = [
                json.loads(line)
                for line in (run_dir / "memory_reasoning.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            actions[0]["reasoning"] = "説明"
            write_jsonl(run_dir / "memory_reasoning.jsonl", actions)
            with self.assertRaisesRegex(
                DirectionPresentationAuditError, "reasoning is not empty"
            ):
                analyze_run(run_dir, config, row, plan["decision_rules"])


if __name__ == "__main__":
    unittest.main()
