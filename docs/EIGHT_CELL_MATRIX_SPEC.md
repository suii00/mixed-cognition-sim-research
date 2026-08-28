# Eight-Cell Matrix Specification

Version: `eight-cell-matrix-v1.1.1`

## 1. Scope

This specification defines the Gate 3 fixed eight-cell experiment bundle,
communication-edge intervention, deterministic planning artifacts, scripted CPU
smoke mode, batch lifecycle, and fail-closed validation boundary. It does not
authorize a pilot or a research run.

## 2. Non-goals

Gate 3 does not freeze production models, a candidate registry, backend
artifacts, pilot seeds, or a run-start approval. It does not test real Ollama,
vLLM, GPU performance, behavioral metrics, topology sweeps, radius sweeps, or
parallel execution of separate runs. It does not change prompts, phase order,
raw-log schema, Metric v2, or Phase-Preserving Parallel Transport semantics.

## 3. Full edge policy

`agents.edge_policy = "full"` preserves the pre-Gate-3 communication rule.
The sender and receiver must be within `communication_radius`. They must both
be outside every place, or both be inside the same named place. Bloc membership
does not affect the boundary. An omitted policy is interpreted as `full` for
backward compatibility.

## 4. Within-bloc-only edge policy

`agents.edge_policy = "within_bloc_only"` first applies the exact same geometry
and place rule as `full`, then requires equal bloc names. It disables cross-bloc
edges while retaining eligible within-bloc edges. It is not a communication-off
condition. Receiver IDs remain unique and in ascending canonical agent-ID order.

## 5. Structural partition

Bloc membership is a structural communication partition only in the
`within_bloc_only` cells. Bloc names, model names, and model identity are never
added to agent prompts. Receiving a message is exposure, not reuse or adoption.

## 6. Fixed cells

Every replicate contains exactly these eight cells:

1. `het-full`
2. `het-within-bloc`
3. `qqq-full`
4. `qqq-within-bloc`
5. `ggg-full`
6. `ggg-within-bloc`
7. `lll-full`
8. `lll-within-bloc`

The cell set and order are normative. HET, QQQ, GGG, and LLL are model
conditions; `full` and `within_bloc_only` are edge policies.

## 7. Canonical order

Replicates execute in plan order. Within each replicate, cells execute in the
order in section 6. Gate 3 has no outer-run parallelism. JSONL planned rows use
the same order, with a zero-based ordinal across the complete matrix.

## 8. Bloc names, order, and counts

The base configuration must contain exactly `alpha`, `beta`, and `neutral`, in
that order, with four agents per bloc. There are twelve agents per run. Bloc
names, order, and counts are paired controls and cannot be plan-controlled.

## 9. Model catalog

The plan contains exactly the slots `qwen`, `gemma`, and `llama`. Each profile
contains `provider`, `model`, and logical `endpoint_id`, and may contain only
`device_slot`, `llm_overrides`, `model_digest`, `quantization`, and
`chat_template` in addition. Provider and logical identities must satisfy the
simulator's public-config validation. Runtime addresses are supplied separately;
matrix construction and Gate 3 tests do not contact them.

## 10. HET rotation

The HET assignment is fixed by zero-based replicate index modulo three:

| Rotation | alpha | beta | neutral |
| ---: | --- | --- | --- |
| 0 | qwen | gemma | llama |
| 1 | gemma | llama | qwen |
| 2 | llama | qwen | gemma |

Index 3 repeats rotation 0. A plan cannot override this mapping.

## 11. Homogeneous assignments

QQQ assigns qwen to all three blocs, GGG assigns gemma to all three, and LLL
assigns llama to all three. The catalog profile is copied into each bloc; model
assignment does not alter bloc names or agent counts.

## 12. Paired world seeds

Each replicate declares one integer `world_seed` (booleans are invalid). All
eight cells in that replicate use it. Replicate IDs are unique. The paired unit
is the set of eight cells for one replicate, not a selection across replicates.

## 13. Plan schema

The plan is UTF-8 JSON with schema
`eight-cell-matrix-plan-v1.1.0`. Duplicate object keys and unknown top-level
fields are rejected. Its exact top-level fields are `schema_version`,
`matrix_id`, `protocol_version`, `metric_version`, `execution_mode`,
`base_config`, `model_catalog`, `replicates`, `candidate_registry`, and
`backend_freeze`. `metric_version` is `metric-v2.0.0`; protocol cannot be blank
or `unversioned`. The required execution mode is a string and is exactly one of
`scripted_smoke` or `reference_ollama`. The base path is relative, cannot
contain `..`, and is pinned by SHA-256. Matrix and replicate IDs must satisfy
the canonical run-ID rules.

Registry and backend records are either `not_frozen` with null evidence, or
`frozen` with respectively a lowercase 64-hex SHA-256 or a non-empty evidence
ID. No production values are committed as part of Gate 3.

## 14. Plan hash

The caller supplies the SHA-256 of the exact plan file bytes. The hash includes
the required execution-mode declaration. Validation stops before batch
publication if it differs. `plan.json` is a canonical copy; the source-byte
hash remains recorded separately in plan and batch manifests.

## 15. Config generation

For each planned run, the generator records matrix, cell, model condition,
replicate ID/index, rotation, execution mode, run ID/name, seed, protocol,
metric, research eligibility, and effective edge policy. Execution-affecting
defaults are explicit. Generated configs are canonical JSON and immutable after
publication.

The plan is the authoritative source of execution mode. Its value is copied to
every planned row, generated config, completed run's saved config snapshot,
`batch_meta.json`, the top level of `batch_manifest.json`, and every batch-
manifest run row. The per-run and batch validation results report only the
unanimous value recovered from that complete evidence chain. An out-of-band
argument cannot override the plan; any retained argument may only assert the
same value. A missing, invalid, or conflicting declaration in a canonical
completed artifact is `FAIL` with exit 3, including after ordinary hashes and
manifests have been recomputed.

## 16. Permitted config differences

Within a replicate, only run ID/name, cell ID, model condition, rotation,
execution mode, edge policy, and the per-bloc provider/model/base URL/overrides/
digest/quantization/chat-template profile may differ. Seed, duration, world,
places, communication and memory settings, sampling, concurrency, thresholds,
protocol, metric, bloc structure, and prompt hash remain paired controls.

## 17. Paired control hash

`paired_control_hash` is SHA-256 over canonical JSON after removing the allowed
cell manipulation fields in section 16 and adding the prompt file-byte hash.
The recomputable `research_eligible` summary is also excluded because it is not
an experimental input; it is checked separately against authoritative evidence.
The hash must be identical across all eight cells of a replicate.

## 18. Initial-state input hash

`initial_state_input_hash` is SHA-256 over canonical world-generation inputs:
world seed, half-space size, places, and ordered bloc names/counts. It must be
identical across the replicate's eight cells. The regression fixture also
constructs simulations and compares actual initial positions.

## 19. Run ID scheme

The exact scheme is `<matrix_id>-<replicate_id>-<cell_id>`. IDs are deterministic,
unique, canonical, and at most 128 characters. There is no truncation, suffix,
or collision recovery.

## 20. Batch layout

The exclusive root is `<output_root>/batch_<matrix_id>`. It contains
`batch_meta.json`, canonical `plan.json`, `planned_runs.jsonl`,
`plan_manifest.json`, `configs/<run_id>.json`,
`runs/output_<run_id>/` raw run artifacts, and `batch_manifest.json`.

## 21. Batch lifecycle

`batch_meta.json` is atomically replaced and has one of `running`, `completed`,
`failed`, or `aborted`. `completed` requires every planned run to complete,
strict validation and smoke validation to pass, the final manifest to exist,
and all pinned hashes to agree. A process interruption records `aborted`; other
execution failures record `failed`.

## 22. No overwrite

The batch root is claimed by exclusive directory creation before any transport
call. An existing path is a collision. Config, plan, planned-row, manifest, run,
and raw files are never replaced or appended by a later batch attempt.

## 23. No resume

Gate 3 implements no resume. Failed, aborted, and not-started evidence is kept.
The same matrix ID cannot be retried; a new experiment requires a new matrix ID.

## 24. Scripted smoke mode

`scripted_smoke` performs no network operation. It is selected by the plan,
not by an overriding CLI value. Each request records one mock
HTTP attempt. Phase 1 emits a non-empty deterministic message derived only from
step and agent ID. Phase 3 stays with empty direction, memory, and reasoning.
The transport does not execute prompt content or include model/bloc names in its
message. Every persisted eligibility summary is false. The smoke profile is
`PASS`; the research profile is `UNVERIFIABLE` because scripted output is never
research eligible.

## 25. Research eligibility boundary

A scripted smoke demonstrates orchestration and artifact integrity only. It is
not research eligible and supplies no behavioral evidence. Missing registry,
backend, model artifact, source-cleanliness, protocol-freeze, plan-freeze, or
run-start-approval evidence remains explicitly unverified.

Research eligibility is independently derived from one validated batch context
before any persisted summary is inspected. Both public run and batch validation
load and bind the same plan, planned rows, generated configs, saved run configs,
batch metadata, batch manifest, and every planned run. They apply the same
plan/spec/base pins, source provenance, registry/backend agreement, execution-
mode chain, per-run validity, and batch-summary checks. A public run result is
research eligible only when both the selected run and its enclosing batch are
independently eligible. Therefore a public run research `PASS` implies a public
batch research `PASS` for the same artifacts.

The derivation requires unanimous non-scripted execution-mode evidence, strict
validity, clean exact source provenance, frozen backend evidence with an ID, a
frozen production registry with a valid hash, complete model artifact fields,
frozen protocol and matrix evidence, run-start approval, complete runs/batch,
no invalid evidence, and no unverified research requirement. Batch eligibility
additionally requires every planned run to be independently eligible. A
scripted, failed, aborted, not-started, invalid, or unverified unselected run
therefore also makes every selected public run in that batch ineligible.

Persisted `research_eligible` values in planned rows, generated configs, saved
run config snapshots, batch metadata, the batch-manifest top level, and batch-
manifest run rows are recomputable summaries only. They are never inputs that
promote or demote the independent derivation. Each required summary is compared
after derivation: matching values are accepted; stale `false` against derived
`true` and unsupported `true` against derived `false` are contradictions and
return `FAIL`/exit 3. Plan declarations and completed batch metadata must also
agree on production-registry and backend freeze state and identity. A missing or
non-Boolean summary in a canonical completed artifact is invalid evidence. A
stale batch-metadata or batch-manifest top-level summary blocks every public run
as well as the batch. Consistent missing or unfrozen research evidence without a
contradiction remains `UNVERIFIABLE`/exit 2 for both public scopes.

A repository-supported synthetic `reference_ollama` fixture exercises the
fully positive eligibility logic with structurally valid fake evidence and zero
network calls. It is only a validator-logic control. It is not backend, model,
GPU, pilot, or research-run evidence; real backend validity remains Gate 4.

## 26. Research validator profiles

The `smoke` profile requires structural, strict-run, pairing, assignment,
communication-boundary, and manifest integrity and permits declared research
evidence to be unfrozen. The `research` profile applies the same checks and also
requires clean exact source provenance, a non-scripted backend, frozen backend
and registry evidence, complete model artifact details, frozen protocol and
plan, complete batch evidence, and a run-start approval reference. Both public
profiles construct the same read-only validated batch context. Persisted
eligibility summaries are checked against the same independently recomputed
evidence under both profiles, while only a successful research-profile result
exposes `research_eligible=true` in validator output. Run output separately
reports selected-run eligibility, batch eligibility, and their conjunction.

## 27. Validator exit codes

Research validation returns 0 for PASS under the selected profile, 2 for
UNVERIFIABLE required research evidence, 3 for contradiction/tampering/strict
failure, and 64 for invocation or validator-configuration errors. Runner exits
0 for completed smoke, 1 for failed/aborted execution, 2 for invalid pinned
input, 3 for batch collision, and 64 for invalid invocation. Normal argparse
help is not an error: `python -m tools.research_validator --help` writes help to
stdout, leaves stderr empty, and exits 0.

## 28. Batch manifest

The final manifest uses schema `eight-cell-batch-manifest-v1.1.0`; this schema
and the `eight-cell-matrix-plan-v1.1.0` schema are unchanged by specification version
`eight-cell-matrix-v1.1.1`. Its top level
records execution mode and the independently derived batch eligibility summary.
It lists every planned row with execution mode, status, config identity, run
directory, run-meta manifest, raw manifest, strict result, original strict
unverifiable list, smoke result, and independently derived run eligibility
summary. It also records counts and the plan/spec/base/prompt pins.
`batch_meta.json` records its file SHA-256. The normal runner derives summaries
from planned and validated run records; callers cannot supply arbitrary summary
values.

## 29. Failure retention

On failure, the failing raw run and metadata remain. Later planned rows remain
`not_started`. All planned rows appear in the batch manifest. No analyzer or
validator deletes, edits, or repairs these artifacts.

## 30. Deferred outer-run parallelism

Runs execute sequentially in canonical order. Only the already-specified
intra-phase LLM transport concurrency is available. Parallel batch or cell
execution requires a later specification and version change.

## 31. Deferred backend smoke

Real Ollama/vLLM API contract, ordering, artifact identity, and resource smoke
are deferred from Gate 3. The staged Gate 4A Ollama reference smoke and the
subsequent Gate 4B vLLM adapter smoke are governed by
`docs/GATE4_BACKEND_SMOKE_SPEC.md`. Gate 3 makes no backend equivalence,
determinism, speed, or GPU claim.

## 32. Deferred production registry

The production candidate registry is not frozen by Gate 3. Candidate selection,
thresholds, and production hashes cannot be inferred from smoke fixtures.

## 33. Version bump rule

Changing the cell set/order, rotation, edge semantics, bloc composition, paired
unit, run-ID scheme, execution-mode evidence chain, plan schema, plan/batch
manifest schema, eligibility derivation or summary-comparison contract, or
validator exit classification requires a matrix-spec version bump and new
regression evidence.

## 34. Regression fixtures

Required CPU fixtures cover edge-policy validation/default/provenance;
full-policy backward compatibility; within-bloc boundary and strict tampering;
plan/hash/base/catalog/replicate/freeze rejection; all fixed cells and rotations;
paired hashes and initial positions; byte-identical static bundles; sequential
and concurrent collision; eight-cell smoke with network guard; success and
failure manifests; failed/aborted/not-started retention; validator exits
0/2/3/64; config/cell/policy/run-ID/seed/manifest/extra/missing/cross-edge
tampering; cross-layer execution-mode agreement; non-authoritative persisted
research-eligibility declarations; missing execution mode at every completed
evidence layer; recomputed-manifest mismatch attacks; a zero-network synthetic
positive eligibility control; stale-false and unsupported-true summary
contradictions; a non-scripted approval-only UNVERIFIABLE control; OS-process
help behavior; run/batch CLI classification agreement; and raw-run byte
immutability.
