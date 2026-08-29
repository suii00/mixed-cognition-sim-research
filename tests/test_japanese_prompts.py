import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from engine.config import build_effective_config
from engine.disaster import Rectangle, Refuge
from engine.execution_contracts import (
    CURRENT_PROMPT_CONTRACT_VERSION,
    CURRENT_TRANSPORT_BEHAVIOR_VERSION,
    JAPANESE_PROMPT_CONTRACT_VERSION,
    validate_prompt_contract_version,
)
from engine.japanese_prompts_v1 import (
    build_phase1_prompt,
    build_phase3_prompt,
)
from engine.provenance import compute_prompt_hash
from engine.response_contracts import CANONICAL_RESPONSE_CONTRACT_VERSION
from engine.sim import Simulation
from engine.world import Place


REPO_ROOT = Path(__file__).resolve().parents[1]
JAPANESE_PROMPT_SHA256 = (
    "184c6599cdd7895d2ac023c201e7ea164a33343f79bc0ab2f7ceb9a6266d17eb"
)


def japanese_config(run_id: str) -> dict:
    return {
        "simulation": {
            "duration": 1,
            "half_space_size": 2,
            "seed": 2401,
            "run_name": run_id,
            "run_id": run_id,
            "protocol_version": "prospective-japanese-models-v1.0.0",
            "metric_version": "engineering-only-v1.0.0",
            "prompt_contract_version": JAPANESE_PROMPT_CONTRACT_VERSION,
            "response_contract_version": CANONICAL_RESPONSE_CONTRACT_VERSION,
            "research_eligible": False,
        },
        "blocs": [{
            "backend_version": "test-vllm",
            "chat_template": "synthetic-test-template",
            "data_parallel_size": 1,
            "dtype": "bfloat16",
            "name": "condition-a",
            "generation_config": "vllm",
            "max_model_len": 4096,
            "model": "served-model",
            "model_digest": "a" * 40,
            "model_source": "example/served-model",
            "provider": "vllm",
            "quantization": "none",
            "endpoint_id": "logical-endpoint-a",
            "num_agents": 1,
            "tensor_parallel_size": 1,
            "tokenizer_revision": "a" * 40,
        }],
        "agents": {
            "communication_radius": 4,
            "memory_limit": 4,
            "memory_size": 2,
            "message_history_limit": 4,
            "message_context_size": 2,
        },
        "places": [],
        "llm_defaults": {
            "temperature": 0.0,
            "max_tokens": 128,
            "timeout_s": 5,
            "max_concurrency": 1,
        },
    }


class JapanesePromptContractTests(unittest.TestCase):
    def test_contract_is_opt_in_and_hash_bound_to_its_own_source(self):
        self.assertEqual(
            validate_prompt_contract_version(None),
            CURRENT_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            validate_prompt_contract_version(JAPANESE_PROMPT_CONTRACT_VERSION),
            JAPANESE_PROMPT_CONTRACT_VERSION,
        )
        path = REPO_ROOT / "engine" / "japanese_prompts_v1.py"
        self.assertEqual(
            hashlib.sha256(path.read_bytes()).hexdigest(),
            JAPANESE_PROMPT_SHA256,
        )
        self.assertEqual(
            compute_prompt_hash(REPO_ROOT, JAPANESE_PROMPT_CONTRACT_VERSION),
            JAPANESE_PROMPT_SHA256,
        )

    def test_canonical_response_contract_accepts_japanese_prompt_contract(self):
        effective = build_effective_config(japanese_config("japanese-config-test"))
        self.assertEqual(
            effective["simulation"]["prompt_contract_version"],
            JAPANESE_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            effective["simulation"]["response_contract_version"],
            CANONICAL_RESPONSE_CONTRACT_VERSION,
        )

    def test_phase1_translates_observations_without_model_identity(self):
        place = Place("避難所A", 0, 0, 1, 12)
        prompt = build_phase1_prompt(
            agent_id=7,
            x=0,
            y=0,
            half_space_size=5,
            places=[place],
            place=place,
            agent_count=3,
            memories=["北側を確認した"],
            messages=[
                {
                    "source_type": "official_warning",
                    "warning_id": "warning-1",
                    "step": 2,
                    "payload": {"hazard_id": "hazard-1"},
                },
                {"sender_id": 4, "message": "南へ移動する"},
            ],
            step=2,
            hazard_rectangles=(Rectangle(-1, 1, -1, 1),),
            refuges=(Refuge("refuge-a", Rectangle(3, 4, 3, 4)),),
        )
        self.assertIn("あなたは2次元グリッド世界のエージェント7です", prompt)
        self.assertIn("現在セルの危険判定: 危険", prompt)
        self.assertIn('"hazard_id":"hazard-1"', prompt)
        self.assertIn('"message"', prompt)
        self.assertIn('"reasoning"', prompt)
        self.assertIn("自然言語の値は日本語", prompt)
        for identity in ("llm-jp", "swallow", "elyza"):
            self.assertNotIn(identity, prompt.casefold())

    def test_phase3_keeps_machine_values_and_localizes_text_values(self):
        prompt = build_phase3_prompt(
            agent_id=1,
            x=-1,
            y=2,
            half_space_size=5,
            places=[],
            place=None,
            agent_count=0,
            memories=[],
            messages=[],
            response_contract_version=CANONICAL_RESPONSE_CONTRACT_VERSION,
        )
        self.assertIn('`action`が"move"の場合', prompt)
        self.assertIn('`action`が"stay"の場合', prompt)
        self.assertIn('`direction`はnull', prompt)
        for value in ('"move"', '"stay"', '"up"', '"down"', '"left"', '"right"'):
            self.assertIn(value, prompt)
        self.assertIn("`memory`と`reasoning`は日本語", prompt)

    def test_simulation_dispatches_japanese_contract(self):
        simulation = Simulation.__new__(Simulation)
        simulation.agents = [SimpleNamespace(
            agent_id=0,
            model="served-model",
            base_url="http://127.0.0.1:1",
            provider="vllm",
            llm_overrides=None,
            endpoint_id="logical-endpoint-a",
            device_slot="logical-device-a",
        )]
        simulation.half_space_size = 2
        simulation.disaster = None
        simulation.prompt_contract_version = JAPANESE_PROMPT_CONTRACT_VERSION
        simulation.response_contract_version = (
            CANONICAL_RESPONSE_CONTRACT_VERSION
        )
        simulation.transport_behavior_version = CURRENT_TRANSPORT_BEHAVIOR_VERSION
        simulation.temperature = 0.0
        simulation.max_tokens = 128
        simulation.timeout_s = 5
        simulation.strict_response_validation = True
        snapshot = {
            "places": [],
            "agents": {
                0: {
                    "position": (0, 0),
                    "place": None,
                    "agent_count": 0,
                    "memories": [],
                    "messages": [],
                }
            },
        }
        requests = simulation._build_phase_requests(1, "phase1", snapshot)
        self.assertEqual(len(requests), 1)
        self.assertIn("近くのエージェントへ送るメッセージ", requests[0].prompt)
        self.assertNotIn("Decide what message", requests[0].prompt)


if __name__ == "__main__":
    unittest.main()
