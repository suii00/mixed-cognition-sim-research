import contextlib
import hashlib
import io
import json
import multiprocessing
import os
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from engine.provenance import RunLifecycle
from tools.metric_v2 import main as metric_main
from tools.metric_v2_core import (
    ALL_DERIVED_FILES,
    DERIVED_SCHEMA_VERSION,
    METRIC_SPEC_PATH,
    METRIC_VERSION,
    NORMALIZATION_ID,
    REGISTRY_SCHEMA_VERSION,
    DerivedCollisionError,
    InputValidationError,
    RegistryValidationError,
    RunEligibilityError,
    analyze_run,
    contains_token_sequence,
    load_candidate_registry,
    tokenize,
)
from tools.validate_run import validate_run


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPRESSION_ID = "expr-0001"
EXPRESSION_TEXT = "blue lantern"
HAS_GIT_HEAD = subprocess.run(
    ["git", "rev-parse", "--verify", "HEAD"],
    cwd=REPO_ROOT,
    check=False,
    capture_output=True,
    timeout=10,
).returncode == 0


class _InjectedPublicationFailure(RuntimeError):
    pass


def canonical_document(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def directory_hashes(path: Path) -> dict[str, str]:
    return {
        str(item.relative_to(path)): sha256_bytes(item.read_bytes())
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _analyze_in_process(
    run_dir: str,
    registry_path: str,
    registry_sha256: str,
    metric_spec_sha256: str,
    derived_root: str,
    barrier,
    queue,
) -> None:
    try:
        output = analyze_run(
            run_dir,
            registry_path,
            registry_sha256,
            metric_spec_sha256,
            derived_root,
            before_claim=lambda: barrier.wait(timeout=10),
        )
    except DerivedCollisionError:
        queue.put(("collision", None))
    except BaseException as error:
        queue.put(("error", type(error).__name__))
    else:
        queue.put(("success", output.name))


def _analyze_and_block_in_process(
    run_dir: str,
    registry_path: str,
    registry_sha256: str,
    metric_spec_sha256: str,
    derived_root: str,
    entered,
) -> None:
    def block_after_first_write(checkpoint: str, _staging_leaf: Path) -> None:
        if checkpoint == "after_analysis_meta_write":
            entered.set()
            while True:
                time.sleep(1)

    analyze_run(
        run_dir,
        registry_path,
        registry_sha256,
        metric_spec_sha256,
        derived_root,
        publication_hook=block_after_first_write,
    )


class MetricV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.root = Path(self.temp_directory.name)
        self.output_root = self.root / "runs"
        self.derived_root = self.root / "derived"
        self.registry_path = self.root / "registry.json"
        self.spec_sha256 = sha256_bytes(METRIC_SPEC_PATH.read_bytes())

        git_info = {
            "git_sha": "c" * 40,
            "git_dirty": False,
            "git_probe_status": "available",
            "git_probe_errors": [],
        }
        self.git_patch = mock.patch(
            "engine.provenance.collect_git_info", return_value=git_info
        )
        self.metric_git_patch = mock.patch(
            "tools.metric_v2_core.collect_git_info", return_value=git_info
        )
        self.gpu_patch = mock.patch(
            "engine.provenance.collect_gpu_info",
            return_value={
                "status": "unavailable",
                "error": "test_disabled",
                "driver_version": None,
                "cuda_version": None,
                "devices": [],
            },
        )
        self.git_patch.start()
        self.metric_git_patch.start()
        self.gpu_patch.start()
        self.addCleanup(self.git_patch.stop)
        self.addCleanup(self.metric_git_patch.stop)
        self.addCleanup(self.gpu_patch.stop)

        self.registry = self.make_registry()
        self.registry_sha256 = self.write_registry(self.registry)

    @staticmethod
    def make_registry(expressions=None, excluded=None) -> dict:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "metric_version": METRIC_VERSION,
            "registry_id": "gate1-test-registry",
            "normalization": NORMALIZATION_ID,
            "discovery_provenance": {
                "purpose": "pilot-only",
                "source_run_ids": ["pilot-fixture-only"],
                "condition_labels_hidden": True,
                "model_labels_hidden": True,
                "receiver_ids_accessed": False,
                "later_target_outputs_accessed": False,
            },
            "excluded_expressions": excluded or [],
            "expressions": expressions or [{
                "expression_id": EXPRESSION_ID,
                "text": EXPRESSION_TEXT,
            }],
        }

    def write_registry(self, value: dict, name: str = "registry.json") -> str:
        path = self.root / name
        raw = canonical_document(value)
        path.write_bytes(raw)
        if name == "registry.json":
            self.registry_path = path
        return sha256_bytes(raw)

    @staticmethod
    def _bloc_configs(agent_blocs: tuple[str, ...]) -> list[dict]:
        result = []
        for bloc_name in agent_blocs:
            if result and result[-1]["name"] == bloc_name:
                result[-1]["num_agents"] += 1
            else:
                result.append({
                    "name": bloc_name,
                    "model": f"mock-{bloc_name}",
                    "endpoint_id": f"endpoint-{bloc_name}",
                    "num_agents": 1,
                })
        return result

    def create_run(
        self,
        run_id: str,
        *,
        steps: int,
        agent_blocs: tuple[str, ...],
        messages: dict[tuple[int, int], str] | None = None,
        positions: dict[tuple[int, int], tuple[int, int]] | None = None,
        phase1_reasoning: dict[tuple[int, int], str] | None = None,
        memories: dict[tuple[int, int], str] | None = None,
        memory_reasoning: dict[tuple[int, int], str] | None = None,
        output_root: Path | None = None,
    ) -> Path:
        messages = messages or {}
        positions = positions or {}
        phase1_reasoning = phase1_reasoning or {}
        memories = memories or {}
        memory_reasoning = memory_reasoning or {}
        root = output_root or self.output_root
        root.mkdir(parents=True, exist_ok=True)
        config = {
            "simulation": {
                "duration": steps,
                "half_space_size": 100,
                "seed": 17,
                "run_name": "metric_v2_fixture",
                "run_id": run_id,
                "protocol_version": "gate1-test-protocol-v1",
                "metric_version": METRIC_VERSION,
            },
            "blocs": self._bloc_configs(agent_blocs),
            "agents": {
                "communication_radius": 1.1,
                "memory_limit": 2,
                "memory_size": 1,
                "message_history_limit": 2,
                "message_context_size": 1,
            },
            "places": [],
            "llm_defaults": {
                "temperature": 0.0,
                "max_tokens": 32,
                "timeout_s": 1,
            },
        }
        lifecycle = RunLifecycle.create(
            config,
            output_root=root,
            repo_root=REPO_ROOT,
        )
        output = lifecycle.output_dir
        agent_count = len(agent_blocs)

        def position(step: int, agent_id: int) -> tuple[int, int]:
            return positions.get((step, agent_id), (agent_id * 10, step * 10))

        phase1_records = []
        memory_records = []
        delivery_records = []
        for step in range(1, steps + 1):
            for agent_id, bloc in enumerate(agent_blocs):
                key = (step, agent_id)
                message = messages.get(key, "")
                reasoning = phase1_reasoning.get(key, "")
                parsed = {"message": message, "reasoning": reasoning}
                phase1_records.append({
                    "step": step,
                    "agent_id": agent_id,
                    "bloc": bloc,
                    "model": f"mock-{bloc}",
                    "parsed": parsed,
                    "raw_output": json.dumps(parsed, ensure_ascii=False),
                })
                memory_records.append({
                    "step": step,
                    "agent_id": agent_id,
                    "bloc": bloc,
                    "model": f"mock-{bloc}",
                    "position": list(position(step, agent_id)),
                    "action": "stay",
                    "direction": "",
                    "memory": memories.get(key, ""),
                    "reasoning": memory_reasoning.get(key, ""),
                })

            for sender_id, sender_bloc in enumerate(agent_blocs):
                key = (step, sender_id)
                message = messages.get(key, "")
                if not message:
                    continue
                sender_position = position(step, sender_id)
                receiver_ids = []
                for receiver_id in range(agent_count):
                    if receiver_id == sender_id:
                        continue
                    receiver_position = position(step, receiver_id)
                    distance_squared = (
                        (sender_position[0] - receiver_position[0]) ** 2
                        + (sender_position[1] - receiver_position[1]) ** 2
                    )
                    if distance_squared <= 1.1 ** 2:
                        receiver_ids.append(receiver_id)
                if receiver_ids:
                    delivery_records.append({
                        "step": step,
                        "sender_id": sender_id,
                        "sender_bloc": sender_bloc,
                        "sender_model": f"mock-{sender_bloc}",
                        "receiver_ids": receiver_ids,
                        "message": message,
                        "reasoning": phase1_reasoning.get(key, ""),
                    })

        def write_jsonl(filename: str, records: list[dict]) -> None:
            (output / filename).write_bytes(
                b"".join(canonical_document(record) for record in records)
            )

        write_jsonl("phase1_raw.jsonl", phase1_records)
        write_jsonl("messages.jsonl", delivery_records)
        write_jsonl("memory_reasoning.jsonl", memory_records)
        write_jsonl("parse_errors.jsonl", [])

        lifecycle._observed_agent_ids.update(range(agent_count))
        logical_calls = 2 * steps * agent_count
        lifecycle.meta["logical_llm_calls"] = logical_calls
        lifecycle.meta["http_attempts"] = logical_calls
        for step in range(1, steps + 1):
            lifecycle.mark_step_completed(step)
        lifecycle.finalize_completed()

        report = validate_run(output, strict=True)
        self.assertTrue(report.valid, report.errors)
        return output

    def analyze(
        self,
        run_dir: Path,
        *,
        derived_root: Path | None = None,
        registry_path: Path | None = None,
        registry_sha256: str | None = None,
        publication_hook=None,
    ) -> Path:
        return analyze_run(
            run_dir,
            registry_path or self.registry_path,
            registry_sha256 or self.registry_sha256,
            self.spec_sha256,
            derived_root or self.derived_root,
            publication_hook=publication_hook,
        )

    @staticmethod
    def pair_status(leaf: Path, receiver_id: int) -> dict:
        return next(
            item
            for item in read_jsonl(leaf / "receiver_expression_status.jsonl")
            if item["expression_id"] == EXPRESSION_ID
            and item["receiver_id"] == receiver_id
        )

    @staticmethod
    def published_run_ids(derived_root: Path) -> list[str]:
        version_directory = derived_root / METRIC_VERSION
        if not version_directory.exists():
            return []
        return sorted(
            item.name
            for item in version_directory.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        )

    def assert_valid_derived_leaf(self, leaf: Path) -> None:
        self.assertEqual(
            {item.name for item in leaf.iterdir()},
            set(ALL_DERIVED_FILES),
        )
        manifest = json.loads(
            (leaf / "derived_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(manifest["files"]), set(ALL_DERIVED_FILES[:-1]))
        for filename, entry in manifest["files"].items():
            raw = (leaf / filename).read_bytes()
            self.assertEqual(entry["sha256"], sha256_bytes(raw))
            self.assertEqual(entry["bytes"], len(raw))
            self.assertEqual(entry["lines"], raw.count(b"\n"))

    def assert_interrupted_publication_is_retryable(self, checkpoint: str) -> None:
        run_id = f"interrupted-{checkpoint.replace('_', '-')}"
        run = self.create_run(
            run_id,
            steps=1,
            agent_blocs=("alpha",),
        )
        raw_before = directory_hashes(run)

        def inject_failure(actual: str, _staging_leaf: Path) -> None:
            if actual == checkpoint:
                raise _InjectedPublicationFailure(checkpoint)

        with self.assertRaises(_InjectedPublicationFailure):
            self.analyze(run, publication_hook=inject_failure)

        version_directory = self.derived_root / METRIC_VERSION
        final_leaf = version_directory / run_id
        self.assertFalse(os.path.lexists(final_leaf))
        self.assertNotIn(run_id, self.published_run_ids(self.derived_root))
        self.assertEqual(directory_hashes(run), raw_before)
        stale_staging = sorted(
            (version_directory / ".staging").glob(f"{run_id}-*")
        )
        self.assertEqual(len(stale_staging), 1)

        leaf = self.analyze(run)
        self.assertEqual(leaf, final_leaf)
        self.assertEqual(directory_hashes(run), raw_before)
        self.assertTrue(stale_staging[0].exists())
        self.assert_valid_derived_leaf(leaf)

    def test_exact_token_sequence_normalization_and_boundaries(self):
        candidate = tokenize("  BLUE\u3000Lantern ")
        self.assertEqual(candidate, ("blue", "lantern"))
        self.assertTrue(contains_token_sequence(tokenize("a blue, lantern!"), candidate))
        self.assertFalse(contains_token_sequence(tokenize("blue lanterns"), candidate))
        self.assertFalse(contains_token_sequence(tokenize("lantern blue"), candidate))

    def test_delivery_only_is_exposure_not_reuse(self):
        run = self.create_run(
            "delivery-only",
            steps=2,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): EXPRESSION_TEXT},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        leaf = self.analyze(run)
        events = read_jsonl(leaf / "events.jsonl")
        self.assertEqual(sum(item["event_type"] == "exposure" for item in events), 1)
        self.assertEqual(sum(item["event_type"] == "reuse" for item in events), 0)
        status = self.pair_status(leaf, 1)
        self.assertEqual(status["status"], "eligible_no_reuse")
        summary = json.loads((leaf / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["eligible_pair_count"], 1)
        self.assertEqual(summary["eligible_reused_pair_count"], 0)
        self.assertEqual(summary["overall_reuse_rate"], 0.0)

    def test_same_step_self_use_is_excluded_not_reuse(self):
        run = self.create_run(
            "same-step-use",
            steps=1,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): EXPRESSION_TEXT, (1, 1): EXPRESSION_TEXT},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        leaf = self.analyze(run)
        status = self.pair_status(leaf, 1)
        self.assertEqual(status["status"], "excluded_prior_or_same_step_use")
        self.assertEqual(status["prior_self_use_step"], 1)
        self.assertIsNone(status["reuse_step"])

    def test_prior_self_use_is_excluded_not_reuse(self):
        run = self.create_run(
            "prior-use",
            steps=2,
            agent_blocs=("alpha", "beta"),
            messages={(1, 1): EXPRESSION_TEXT, (2, 0): EXPRESSION_TEXT},
            positions={
                (1, 0): (0, 0), (1, 1): (10, 0),
                (2, 0): (0, 0), (2, 1): (1, 0),
            },
        )
        status = self.pair_status(self.analyze(run), 1)
        self.assertEqual(status["status"], "excluded_prior_or_same_step_use")
        self.assertEqual(status["prior_self_use_step"], 1)

    def test_multiple_exposures_produce_one_reuse(self):
        run = self.create_run(
            "multiple-exposures",
            steps=4,
            agent_blocs=("alpha", "beta"),
            messages={
                (1, 0): EXPRESSION_TEXT,
                (2, 0): f"again {EXPRESSION_TEXT}",
                (4, 1): f"I use {EXPRESSION_TEXT}",
            },
            positions={
                (1, 0): (0, 0), (1, 1): (1, 0),
                (2, 0): (0, 0), (2, 1): (1, 0),
            },
        )
        leaf = self.analyze(run)
        status = self.pair_status(leaf, 1)
        self.assertEqual(status["first_exposure_step"], 1)
        self.assertEqual(status["total_exposure_count"], 2)
        self.assertEqual(status["exposure_count_before_reuse"], 2)
        self.assertEqual(status["reuse_step"], 4)
        self.assertEqual(status["latency_steps"], 3)
        reuse_events = [
            item for item in read_jsonl(leaf / "events.jsonl")
            if item["event_type"] == "reuse" and item["agent_id"] == 1
        ]
        self.assertEqual(len(reuse_events), 1)

    def test_valid_second_hop_references_existing_events(self):
        run = self.create_run(
            "valid-second-hop",
            steps=3,
            agent_blocs=("alpha", "beta", "beta"),
            messages={
                (1, 0): EXPRESSION_TEXT,
                (2, 1): f"relay {EXPRESSION_TEXT}",
                (3, 2): f"target {EXPRESSION_TEXT}",
            },
            positions={
                (1, 0): (0, 0), (1, 1): (1, 0), (1, 2): (10, 0),
                (2, 0): (10, 0), (2, 1): (0, 0), (2, 2): (1, 0),
            },
        )
        leaf = self.analyze(run)
        events = read_jsonl(leaf / "events.jsonl")
        chains = [item for item in events if item["event_type"] == "second_hop"]
        self.assertEqual(len(chains), 1)
        chain = chains[0]
        self.assertEqual(
            {chain["source_agent_id"], chain["relay_agent_id"], chain["target_agent_id"]},
            {0, 1, 2},
        )
        event_ids = {item["event_id"] for item in events}
        self.assertTrue(set(chain["referenced_event_ids"].values()) <= event_ids)

    def test_ambiguous_second_hop_parent_is_not_attributed(self):
        run = self.create_run(
            "ambiguous-parent",
            steps=3,
            agent_blocs=("alpha", "beta", "beta", "beta"),
            messages={
                (1, 0): EXPRESSION_TEXT,
                (2, 1): f"relay {EXPRESSION_TEXT}",
                (2, 2): f"other {EXPRESSION_TEXT}",
                (3, 3): f"target {EXPRESSION_TEXT}",
            },
            positions={
                (1, 0): (0, 0), (1, 1): (1, 0),
                (1, 2): (10, 0), (1, 3): (20, 0),
                (2, 0): (20, 0), (2, 1): (0, 0),
                (2, 2): (0, 0), (2, 3): (1, 0),
            },
        )
        leaf = self.analyze(run)
        target = self.pair_status(leaf, 3)
        self.assertEqual(target["status"], "eligible_reused")
        self.assertEqual(target["first_exposure_sender_ids"], [1, 2])
        self.assertFalse(any(
            item["event_type"] == "second_hop"
            for item in read_jsonl(leaf / "events.jsonl")
        ))

    def test_simultaneous_origin_excludes_source_attributed_chain(self):
        run = self.create_run(
            "simultaneous-origin",
            steps=3,
            agent_blocs=("alpha", "alpha", "beta", "beta"),
            messages={
                (1, 0): EXPRESSION_TEXT,
                (1, 1): f"also {EXPRESSION_TEXT}",
                (2, 2): f"relay {EXPRESSION_TEXT}",
                (3, 3): f"target {EXPRESSION_TEXT}",
            },
            positions={
                (1, 0): (0, 0), (1, 1): (20, 0),
                (1, 2): (1, 0), (1, 3): (30, 0),
                (2, 0): (20, 0), (2, 1): (30, 0),
                (2, 2): (0, 0), (2, 3): (1, 0),
            },
        )
        events = read_jsonl(self.analyze(run) / "events.jsonl")
        innovation = next(item for item in events if item["event_type"] == "innovation")
        self.assertEqual(innovation["origin_type"], "simultaneous_origin")
        self.assertEqual(innovation["origin_agent_ids"], [0, 1])
        self.assertFalse(any(item["event_type"] == "second_hop" for item in events))

    def test_memory_and_reasoning_are_not_self_use(self):
        run = self.create_run(
            "excluded-fields",
            steps=1,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): "ordinary delivery"},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
            phase1_reasoning={(1, 0): EXPRESSION_TEXT},
            memories={(1, 1): EXPRESSION_TEXT},
            memory_reasoning={(1, 1): f"reasoning {EXPRESSION_TEXT}"},
        )
        leaf = self.analyze(run)
        summary = json.loads((leaf / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["expression_present_count"], 0)
        self.assertEqual(summary["exposure_event_count"], 0)
        self.assertEqual(summary["eligible_pair_count"], 0)

    def test_unregistered_future_text_does_not_change_registered_events(self):
        common = {
            "steps": 3,
            "agent_blocs": ("alpha", "beta"),
            "positions": {
                (1, 0): (0, 0), (1, 1): (1, 0),
                (2, 0): (10, 0), (2, 1): (20, 0),
                (3, 0): (10, 0), (3, 1): (20, 0),
            },
        }
        base = self.create_run(
            "future-stability",
            output_root=self.root / "base-runs",
            messages={(1, 0): EXPRESSION_TEXT, (2, 1): EXPRESSION_TEXT},
            **common,
        )
        future = self.create_run(
            "future-stability",
            output_root=self.root / "future-runs",
            messages={
                (1, 0): EXPRESSION_TEXT,
                (2, 1): EXPRESSION_TEXT,
                (3, 0): "ultraviolet quasar ultraviolet quasar ultraviolet quasar",
            },
            **common,
        )
        base_leaf = self.analyze(base, derived_root=self.root / "base-derived")
        future_leaf = self.analyze(future, derived_root=self.root / "future-derived")
        self.assertEqual(
            (base_leaf / "events.jsonl").read_bytes(),
            (future_leaf / "events.jsonl").read_bytes(),
        )
        self.assertEqual(
            (base_leaf / "receiver_expression_status.jsonl").read_bytes(),
            (future_leaf / "receiver_expression_status.jsonl").read_bytes(),
        )

    def test_registry_hash_mismatch_creates_no_leaf_and_preserves_raw(self):
        run = self.create_run(
            "registry-hash-mismatch",
            steps=1,
            agent_blocs=("alpha",),
        )
        before = directory_hashes(run)
        with self.assertRaisesRegex(RegistryValidationError, "SHA-256 mismatch"):
            self.analyze(run, registry_sha256="0" * 64)
        self.assertFalse(
            (self.derived_root / METRIC_VERSION / run.name.removeprefix("output_")).exists()
        )
        self.assertEqual(directory_hashes(run), before)

    def assert_invalid_registry(self, value: dict, message: str) -> None:
        name = f"invalid-{sha256_bytes(canonical_document(value))[:12]}.json"
        digest = self.write_registry(value, name=name)
        with self.assertRaisesRegex(RegistryValidationError, message):
            load_candidate_registry(self.root / name, digest)

    def test_registry_duplicate_expression_id_is_rejected(self):
        value = self.make_registry(expressions=[
            {"expression_id": "same", "text": "blue lantern"},
            {"expression_id": "same", "text": "red lantern"},
        ])
        self.assert_invalid_registry(value, "duplicate expression_id")

    def test_registry_duplicate_normalized_expression_is_rejected(self):
        value = self.make_registry(expressions=[
            {"expression_id": "one", "text": "BLUE  lantern"},
            {"expression_id": "two", "text": "blue\u3000lantern"},
        ])
        self.assert_invalid_registry(value, "duplicate normalized expression")

    def test_registry_empty_normalized_tokens_are_rejected(self):
        value = self.make_registry(expressions=[
            {"expression_id": "empty", "text": "___"},
        ])
        self.assert_invalid_registry(value, "no normalized tokens")

    def test_registry_candidate_exclusion_conflict_is_rejected(self):
        value = self.make_registry(excluded=[{
            "text": "BLUE\u3000LANTERN",
            "reason": "environment term",
        }])
        self.assert_invalid_registry(value, "conflicts with excluded")

    def test_registry_wrong_metric_version_is_rejected(self):
        value = self.make_registry()
        value["metric_version"] = "metric-v1"
        self.assert_invalid_registry(value, "metric version mismatch")

    def test_registry_unsafe_future_discovery_flag_is_rejected(self):
        value = self.make_registry()
        value["discovery_provenance"]["later_target_outputs_accessed"] = True
        self.assert_invalid_registry(value, "unsafe discovery provenance flag")

    def test_registry_unknown_top_level_field_is_rejected(self):
        value = self.make_registry()
        value["dynamic_candidates"] = []
        self.assert_invalid_registry(value, "unknown top-level fields")

    def test_metric_spec_hash_mismatch_has_cli_exit_two_and_no_leaf(self):
        run = self.create_run(
            "spec-hash-mismatch",
            steps=1,
            agent_blocs=("alpha",),
        )
        raw_before = directory_hashes(run)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = metric_main([
                "analyze",
                "--run-dir", str(run),
                "--registry", str(self.registry_path),
                "--registry-sha256", self.registry_sha256,
                "--metric-spec-sha256", "0" * 64,
                "--derived-root", str(self.derived_root),
            ])
        self.assertEqual(exit_code, 2, stderr.getvalue())
        self.assertFalse(
            (self.derived_root / METRIC_VERSION / "spec-hash-mismatch").exists()
        )
        self.assertEqual(directory_hashes(run), raw_before)

    def test_cli_success_exit_zero(self):
        run = self.create_run(
            "cli-success",
            steps=1,
            agent_blocs=("alpha",),
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = metric_main([
                "analyze",
                "--run-dir", str(run),
                "--registry", str(self.registry_path),
                "--registry-sha256", self.registry_sha256,
                "--metric-spec-sha256", self.spec_sha256,
                "--derived-root", str(self.derived_root),
            ])
        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assert_valid_derived_leaf(
            self.derived_root / METRIC_VERSION / "cli-success"
        )

    def test_cli_unexpected_failure_exit_one(self):
        stderr = io.StringIO()
        with (
            mock.patch(
                "tools.metric_v2.analyze_run",
                side_effect=OSError("injected publication failure"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = metric_main([
                "analyze",
                "--run-dir", str(self.root / "unused-run"),
                "--registry", str(self.registry_path),
                "--registry-sha256", self.registry_sha256,
                "--metric-spec-sha256", self.spec_sha256,
                "--derived-root", str(self.derived_root),
            ])
        self.assertEqual(exit_code, 1, stderr.getvalue())
        self.assertIn("OSError", stderr.getvalue())

    def test_invalid_raw_run_creates_no_leaf_and_gets_no_further_mutation(self):
        run = self.create_run(
            "invalid-raw",
            steps=1,
            agent_blocs=("alpha",),
        )
        with (run / "phase1_raw.jsonl").open("ab") as handle:
            handle.write(b'{"tampered":true}\n')
        before_analysis = directory_hashes(run)
        with self.assertRaises(RunEligibilityError):
            self.analyze(run)
        self.assertEqual(directory_hashes(run), before_analysis)
        self.assertFalse((self.derived_root / METRIC_VERSION / "invalid-raw").exists())

    def test_sequential_collision_has_exit_three_and_preserves_everything(self):
        run = self.create_run(
            "sequential-derived-collision",
            steps=1,
            agent_blocs=("alpha",),
        )
        leaf = self.analyze(run)
        raw_before = directory_hashes(run)
        derived_before = directory_hashes(leaf)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = metric_main([
                "analyze",
                "--run-dir", str(run),
                "--registry", str(self.registry_path),
                "--registry-sha256", self.registry_sha256,
                "--metric-spec-sha256", self.spec_sha256,
                "--derived-root", str(self.derived_root),
            ])
        self.assertEqual(exit_code, 3, stderr.getvalue())
        self.assertEqual(directory_hashes(run), raw_before)
        self.assertEqual(directory_hashes(leaf), derived_before)

    @unittest.skipUnless(HAS_GIT_HEAD, "requires a committed repository for child provenance")
    def test_concurrent_process_claim_has_one_success_and_one_collision(self):
        run = self.create_run(
            "concurrent-derived-collision",
            steps=1,
            agent_blocs=("alpha",),
        )
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(3)
        queue = context.Queue()
        args = (
            str(run),
            str(self.registry_path),
            self.registry_sha256,
            self.spec_sha256,
            str(self.derived_root),
            barrier,
            queue,
        )
        processes = [
            context.Process(target=_analyze_in_process, args=args)
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            barrier.wait(timeout=15)
            for process in processes:
                process.join(timeout=15)
            self.assertTrue(all(not item.is_alive() for item in processes))
            self.assertTrue(all(item.exitcode == 0 for item in processes))
            outcomes = [queue.get(timeout=5) for _ in processes]
            self.assertCountEqual(
                outcomes,
                [("success", "concurrent-derived-collision"), ("collision", None)],
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_failure_after_analysis_meta_write_is_retryable(self):
        self.assert_interrupted_publication_is_retryable(
            "after_analysis_meta_write"
        )

    def test_failure_after_events_write_is_retryable(self):
        self.assert_interrupted_publication_is_retryable("after_events_write")

    def test_failure_after_receiver_status_write_is_retryable(self):
        self.assert_interrupted_publication_is_retryable(
            "after_receiver_status_write"
        )

    def test_failure_after_summary_write_is_retryable(self):
        self.assert_interrupted_publication_is_retryable("after_summary_write")

    def test_failure_during_manifest_write_is_retryable(self):
        self.assert_interrupted_publication_is_retryable("during_manifest_write")

    def test_failure_after_manifest_verification_is_retryable(self):
        self.assert_interrupted_publication_is_retryable(
            "after_manifest_verification_before_publish"
        )

    @unittest.skipUnless(HAS_GIT_HEAD, "requires a committed repository for child provenance")
    def test_abrupt_child_termination_releases_lock_and_allows_retry(self):
        run_id = "abrupt-publication-termination"
        run = self.create_run(
            run_id,
            steps=1,
            agent_blocs=("alpha",),
        )
        raw_before = directory_hashes(run)
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        process = context.Process(
            target=_analyze_and_block_in_process,
            args=(
                str(run),
                str(self.registry_path),
                self.registry_sha256,
                self.spec_sha256,
                str(self.derived_root),
                entered,
            ),
        )
        try:
            process.start()
            self.assertTrue(
                entered.wait(timeout=20),
                f"child did not reach staging checkpoint; exit={process.exitcode}",
            )
            self.assertTrue(process.is_alive())
            final_leaf = self.derived_root / METRIC_VERSION / run_id
            self.assertFalse(os.path.lexists(final_leaf))
            self.assertNotIn(run_id, self.published_run_ids(self.derived_root))

            process.terminate()
            process.join(timeout=20)
            self.assertFalse(process.is_alive())
            self.assertNotEqual(process.exitcode, 0)
            self.assertFalse(os.path.lexists(final_leaf))
            self.assertEqual(directory_hashes(run), raw_before)

            leaf = self.analyze(run)
            self.assertEqual(leaf, final_leaf)
            self.assertEqual(directory_hashes(run), raw_before)
            self.assert_valid_derived_leaf(leaf)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    def test_analysis_keeps_entire_raw_directory_byte_identical(self):
        run = self.create_run(
            "raw-immutability",
            steps=2,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): EXPRESSION_TEXT},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        before = directory_hashes(run)
        self.analyze(run)
        self.assertEqual(directory_hashes(run), before)

    def test_different_derived_roots_are_byte_identical(self):
        run = self.create_run(
            "deterministic-output",
            steps=2,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): EXPRESSION_TEXT, (2, 1): EXPRESSION_TEXT},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        first = self.analyze(run, derived_root=self.root / "derived-a")
        second = self.analyze(run, derived_root=self.root / "derived-b")
        self.assertEqual(directory_hashes(first), directory_hashes(second))
        self.assertEqual(
            sorted(path.name for path in first.iterdir()),
            [
                "analysis_meta.json",
                "derived_manifest.json",
                "events.jsonl",
                "receiver_expression_status.jsonl",
                "summary.json",
            ],
        )

    def test_event_raw_provenance_matches_exact_source_bytes(self):
        run = self.create_run(
            "raw-provenance",
            steps=1,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): f"prefix {EXPRESSION_TEXT} suffix"},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        exposure = next(
            item for item in read_jsonl(self.analyze(run) / "events.jsonl")
            if item["event_type"] == "exposure"
        )
        for reference_name in (
            "sender_phase1_raw_reference",
            "delivery_raw_reference",
        ):
            reference = exposure[reference_name]
            raw_line = (run / reference["file"]).read_bytes().splitlines(keepends=True)[
                reference["line_number"] - 1
            ]
            self.assertEqual(reference["record_sha256"], sha256_bytes(raw_line))
            raw_record = json.loads(raw_line)
            message = (
                raw_record["parsed"]["message"]
                if reference["file"] == "phase1_raw.jsonl"
                else raw_record["message"]
            )
            self.assertEqual(
                reference["message_sha256"],
                sha256_bytes(message.encode("utf-8")),
            )

    def test_zero_denominator_rates_are_null(self):
        run = self.create_run(
            "zero-denominator",
            steps=1,
            agent_blocs=("alpha",),
        )
        summary = json.loads(
            (self.analyze(run) / "summary.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary["eligible_pair_count"], 0)
        self.assertIsNone(summary["overall_reuse_rate"])
        self.assertIsNone(summary["cross_bloc_reuse_rate"])
        self.assertIsNone(summary["within_bloc_reuse_rate"])

    def test_untrusted_message_is_only_text_and_executes_nothing(self):
        sentinel = self.root / "must-not-exist.txt"
        untrusted = (
            f"{EXPRESSION_TEXT}; touch {sentinel}; "
            "python -c 'raise SystemExit()'; https://invalid.example/test"
        )
        run = self.create_run(
            "untrusted-text",
            steps=1,
            agent_blocs=("alpha", "beta"),
            messages={(1, 0): untrusted},
            positions={(1, 0): (0, 0), (1, 1): (1, 0)},
        )
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=AssertionError("network access attempted"),
        ) as urlopen:
            leaf = self.analyze(run)
        urlopen.assert_not_called()
        self.assertFalse(sentinel.exists())
        self.assertTrue(any(
            item["event_type"] == "exposure"
            for item in read_jsonl(leaf / "events.jsonl")
        ))

    def test_derived_root_inside_raw_run_is_rejected_before_leaf(self):
        run = self.create_run(
            "unsafe-derived-root",
            steps=1,
            agent_blocs=("alpha",),
        )
        raw_before = directory_hashes(run)
        with self.assertRaisesRegex(InputValidationError, "inside the raw run"):
            self.analyze(run, derived_root=run / "derived")
        self.assertEqual(directory_hashes(run), raw_before)
        self.assertFalse((run / "derived").exists())

    def test_symlink_path_into_raw_run_is_rejected(self):
        run = self.create_run(
            "symlink-derived-root",
            steps=1,
            agent_blocs=("alpha",),
        )
        link = self.root / "raw-link"
        try:
            link.symlink_to(run, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(
                    f"directory symlinks unavailable: {type(error).__name__}"
                )
            junction = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link), str(run)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if junction.returncode != 0:
                self.skipTest("directory symlink and junction creation unavailable")
        raw_before = directory_hashes(run)
        with self.assertRaisesRegex(InputValidationError, "inside the raw run"):
            self.analyze(run, derived_root=link / "derived")
        self.assertEqual(directory_hashes(run), raw_before)
        self.assertFalse((run / "derived").exists())

    def test_analysis_metadata_and_manifest_are_complete(self):
        run = self.create_run(
            "metadata-manifest",
            steps=1,
            agent_blocs=("alpha",),
        )
        leaf = self.analyze(run)
        meta = json.loads((leaf / "analysis_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["schema_version"], DERIVED_SCHEMA_VERSION)
        self.assertEqual(meta["metric_version"], METRIC_VERSION)
        self.assertEqual(meta["metric_spec_sha256"], self.spec_sha256)
        self.assertEqual(meta["registry_sha256"], self.registry_sha256)
        self.assertTrue(meta["strict_validator_valid"])
        self.assertTrue(meta["strict_validator_unverifiable"])
        rendered = (leaf / "analysis_meta.json").read_text(encoding="utf-8")
        self.assertNotIn(str(run.resolve()), rendered)
        self.assertNotIn("start_time", rendered)
        manifest = json.loads(
            (leaf / "derived_manifest.json").read_text(encoding="utf-8")
        )
        for filename, entry in manifest["files"].items():
            raw = (leaf / filename).read_bytes()
            self.assertEqual(entry["sha256"], sha256_bytes(raw))
            self.assertEqual(entry["bytes"], len(raw))
            self.assertEqual(entry["lines"], raw.count(b"\n"))


if __name__ == "__main__":
    unittest.main()
