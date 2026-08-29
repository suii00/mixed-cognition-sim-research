import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from engine.config import load_config
from engine.execution_contracts import LEGACY_TRANSPORT_BEHAVIOR_VERSION
from engine.llm_client import call_vllm, extract_json, extract_legacy_json
from engine.provenance import compute_prompt_hash
from tools import build_legacy_reproduction_matrix as builder
from tools import run_legacy_reproduction as batch_launcher
from tools import run_public_ollama as ollama_launcher
from tools import run_public_vllm as launcher


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "configs" / "legacy_reproduction_v1" / "manifest.json"
LOCK_PATH = REPO_ROOT / "runtime" / "vllm-runtime-lock.json"


class FakeResponse:
    status_code = 200

    def __init__(self, content):
        self.envelope = {
            "choices": [{
                "message": {"content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        self.content = json.dumps(self.envelope).encode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return self.envelope


class LegacyReproductionTests(unittest.TestCase):
    def test_historical_prompt_bytes_are_exactly_hash_bound(self):
        path = REPO_ROOT / "engine" / "legacy_prompts_v1.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), builder.PROMPT_SHA256)
        self.assertEqual(
            compute_prompt_hash(REPO_ROOT, builder.PROMPT_CONTRACT_VERSION),
            builder.PROMPT_SHA256,
        )

    def test_matrix_is_deterministic_and_preserves_seed_scope(self):
        builder.write_or_check(builder.build_outputs(), check=True)
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["planned_attempts"], 10)
        self.assertEqual(manifest["planned_logical_llm_calls"], 19106)
        self.assertEqual(manifest["runnable_attempts_under_current_approval"], 8)
        self.assertEqual(
            manifest["runnable_logical_llm_calls_under_current_approval"],
            13346,
        )
        self.assertEqual(
            [row["seed"] for row in manifest["rows"]].count(42),
            9,
        )
        self.assertEqual(
            [row["seed"] for row in manifest["rows"]].count(1002),
            1,
        )
        self.assertEqual(
            [row["required_gpu_count"] for row in manifest["rows"]].count(7),
            2,
        )
        for row in manifest["rows"]:
            config = load_config(str(REPO_ROOT / row["config"]))
            simulation = config["simulation"]
            self.assertFalse(simulation["research_eligible"])
            self.assertEqual(
                simulation["prompt_contract_version"],
                builder.PROMPT_CONTRACT_VERSION,
            )
            self.assertEqual(
                simulation["transport_behavior_version"],
                LEGACY_TRANSPORT_BEHAVIOR_VERSION,
            )

    def test_public_vllm_launcher_requires_explicit_legacy_opt_in(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        row = next(item for item in manifest["rows"] if item["provider"] == "vllm")
        config = load_config(str(REPO_ROOT / row["config"]))
        lock = launcher._load_json_object(LOCK_PATH)
        with self.assertRaisesRegex(launcher.PublicVllmError, "explicit legacy"):
            launcher.validate_vllm_config(config, lock)
        launcher.validate_vllm_config(
            config,
            lock,
            allow_legacy_reproduction=True,
        )

    def test_public_ollama_launcher_is_explicit_and_discards_server_output(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        row = next(item for item in manifest["rows"] if item["provider"] == "ollama")
        config = load_config(str(REPO_ROOT / row["config"]))
        with self.assertRaisesRegex(ollama_launcher.PublicOllamaError, "explicit"):
            ollama_launcher.validate_ollama_config(
                config,
                allow_legacy_reproduction=False,
            )
        ollama_launcher.validate_ollama_config(
            config,
            allow_legacy_reproduction=True,
        )
        specs = ollama_launcher.build_endpoint_specs(config, (0, 1, 2), 18340)
        model_root = Path("/runtime-only/models")
        server_home = Path("/runtime-only/server-0")
        command = ollama_launcher.build_server_command(
            specs[0],
            model_root,
            server_home,
        )
        self.assertIn("/usr/bin/env", command)
        self.assertIn("-i", command)
        self.assertIn(f"HOME={server_home}", command)
        self.assertIn(f"OLLAMA_MODELS={model_root}", command)
        self.assertIn("OLLAMA_NO_CLOUD=1", command)
        self.assertIn("OLLAMA_CONTEXT_LENGTH=4096", command)
        self.assertNotIn("sudo", command)
        self.assertNotIn("HF_TOKEN", " ".join(command))
        process = mock.Mock()
        with mock.patch.object(Path, "mkdir"):
            with mock.patch.object(
                ollama_launcher.subprocess,
                "Popen",
                return_value=process,
            ) as popen:
                self.assertIs(
                    ollama_launcher.start_server(
                        specs[0],
                        model_root,
                        server_home,
                    ),
                    process,
                )
        self.assertIs(popen.call_args.kwargs["stdout"], ollama_launcher.subprocess.DEVNULL)
        self.assertIs(popen.call_args.kwargs["stderr"], ollama_launcher.subprocess.DEVNULL)

    def test_legacy_parser_and_generation_retry_are_version_scoped(self):
        wrapped = 'prefix {"message":"ok","reasoning":"r"} suffix'
        self.assertIsNone(extract_json(wrapped))
        self.assertEqual(
            extract_legacy_json(wrapped),
            {"message": "ok", "reasoning": "r"},
        )
        attempts = []
        events = []
        with mock.patch(
            "engine.llm_client.requests.post",
            side_effect=[
                FakeResponse("not json"),
                FakeResponse(wrapped),
            ],
        ) as post:
            parsed, raw = call_vllm(
                prompt="prompt",
                model="model",
                base_url="http://127.0.0.1:8001",
                transport_behavior_version=LEGACY_TRANSPORT_BEHAVIOR_VERSION,
                attempt_observer=attempts.append,
                telemetry=lambda event, amount: events.extend([event] * amount),
            )
        self.assertEqual(parsed, {"message": "ok", "reasoning": "r"})
        self.assertEqual(raw, wrapped)
        self.assertEqual(post.call_count, 2)
        self.assertEqual([item["generation_attempt"] for item in attempts], [1, 2])
        self.assertEqual(events.count("generation_retry"), 1)
        self.assertEqual(events.count("syntax_parse_attempt_failure"), 1)

    def test_ollama_cleanup_boundary_requires_model_root_absence_evidence(self):
        evidence = {
            "all_process_groups_stopped": True,
            "gpu_release_verified": True,
            "publication_scan_finding_count": 0,
            "runtime_binding_values_persisted": False,
            "schema_version": "public-ollama-verification-v1.0.0",
        }
        self.assertFalse(batch_launcher._cleanup_boundary_passed(evidence))
        evidence["runtime_model_root_persisted"] = False
        self.assertTrue(batch_launcher._cleanup_boundary_passed(evidence))


if __name__ == "__main__":
    unittest.main()
