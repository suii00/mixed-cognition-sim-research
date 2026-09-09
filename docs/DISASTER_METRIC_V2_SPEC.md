# Disaster metric v2.0.0

## 1. Status and scope

This document normatively defines `disaster-metric-v2.0.0`. The metric is a
mechanical derivation over a completed disaster run using log schema `2.0.0`.
It does not modify raw files, repair model output, use an LLM judge, infer
semantic equivalence, infer psychological adoption, or select a causal parent
message.

Metric v1 remains frozen. A v2 derivation of a run that declared another
metric version or a different metric-spec SHA-256 is an explicitly post-hoc
derivation; it is not prospectively eligible merely because the tool completed.
Matrix analysis should pass `--require-declared-metric`, which requires both
`disaster-metric-v2.0.0` and the exact current specification digest in the
source simulation config.

## 2. Required raw inputs

The source run must pass `tools/validate_run.py --strict`, be terminally
`completed`, have `aborted=false`, and record log schema `2.0.0`. The only
metric inputs are:

- `run_meta.json`;
- `phase1_raw.jsonl`;
- `messages.jsonl`;
- `warning_events.jsonl`;
- `positions.jsonl`.

Every input file is hashed. References to JSONL evidence record the raw
filename, one-based line number, SHA-256 of the exact raw line bytes, and byte
count. Model-generated text is not copied into the derived artifact; its UTF-8
SHA-256 and character count provide the join back to immutable raw evidence.
The canonical warning issue row, each first post-exposure distance-decrease
pair, final position, and refuge-completion position retain direct raw-line
references.

## 3. Event clock and eligibility

The event clock is the lexicographic pair `(step, phase_order)`:

| Event | `phase_order` |
|---|---:|
| Official warning exposure before Phase 1 | 0 |
| Phase 1 generated output | 1 |
| Agent-relay exposure during Phase 2 | 2 |
| Phase 3 decision and post-movement observation | 3 |

An exposure is prior to an output or decision only when its event clock is
strictly smaller. Consequently:

- an official recipient's same-step Phase 1 output is eligible;
- a relay recipient's same-step Phase 1 output is not eligible;
- both official and relayed exposure precede same-step Phase 3 movement.

The first eligible output is the earliest Phase 1 output after any exposure,
whether empty, warning-bearing, or unrelated. A same-step official-recipient
output is a phase-ordered post-exposure carrier observation, but is not called
`reuse`. The first exact-ID reuse is the earliest carrier output in a strictly
later numeric step than at least one prior exposure. This preserves the project
rule that reuse requires the receiver's own output in a later step while still
recording same-step surface fidelity independently.

Eligible non-reuse is right-censored at the terminal step. Exact-ID reuse delay
is at least one step. The first post-exposure refuge-distance decrease may have
delay zero because both official and relayed exposure precede same-step Phase 3
movement.

## 4. Canonical fact slots

Expected values are derived only from the public scenario fields used by
`DisasterScenario.warning_facts()`:

### Warning-specific slots

- `warning_id`;
- `issue_step`;
- `hazard_id`;
- `hazard_geometry_at_issue`, using the complete latest hazard stage active at
  the issue step and treating rectangle order as non-semantic.

### Shared-context slots

Each `refuge:<refuge_id>` slot contains the refuge identifier and rectangle.
Refuges are always labelled `shared_context`: disaster prompts independently
show refuge rectangles at every step, so their occurrence is not evidence that
the warning caused transmission.

## 5. Frozen surface recognizer

The recognizer is `frozen-surface-claims-v1.0.0`. It recognizes only:

1. exact warning and hazard identifiers under the repository token-boundary
   rule;
2. repository-form prose clauses (`Official warning`, `At step`, `hazard
   classification ... covers`, `Refuge areas`) and integer `x=a..b, y=c..d`
   rectangles;
3. syntactically valid JSON objects or arrays containing the canonical keys
   `warning_id`, `issue_step`, `hazard_id`, `hazard_rectangles`, `refuges`,
   `refuge_id`, and `rectangle` with correctly typed values.

It does not repair malformed JSON, resolve paraphrases, process negation,
translate coordinates, apply embeddings, or inspect `reasoning`. Therefore the
result is surface fidelity, not truth, intent, or semantic fidelity.

Each slot has exactly one status:

- `match`: at least one recognized value equals the expected value and no
  recognized value differs;
- `recognized_conflict`: at least one value is recognized and all recognized
  values differ;
- `mixed`: both matching and differing recognized values occur;
- `unrecognized`: no value for that slot is recognized.

`unrecognized` does not mean false. `recognized_conflict` does not establish a
model belief.

## 6. Exposure and relay observations

For every eligible Phase 1 output, the metric records all prior exposure event
IDs and available raw references. It always records
`causal_parent_inferred=false`; when multiple exposures precede one output no
source is selected as the parent.

Exact-ID carrier output has one relay status:

- `generated_and_delivered`: `messages.jsonl` records delivery to one or more
  receivers;
- `generated_not_delivered`: the output was generated but has no delivered
  message record;
- `not_exact_id_carrier`: the eligible output did not retain the exact ID.

Delivered message count and receiver exposure-edge count remain distinct. One
generated message may expose multiple receivers.

Every output also records `later_step_reuse_eligible`. Aggregate output keeps
same-step post-exposure exact-ID carriers separate from later-step exact-ID
reuse outputs.

An exact-ID carrier produced without any prior exposure is retained as
`unattributed_exact_id_carrier`, not discarded and not labelled reuse. This
captures spontaneous or otherwise unattributed sources that can still create
relay exposure edges for receivers. Aggregate output reports all exact-ID relay
edges separately from the subset originating in post-exposure outputs.

## 7. Movement and spatial observations

`first_post_exposure_refuge_distance_decrease_step` is the first Phase 3 step
after any prior exposure whose post-movement shortest-refuge distance is lower
than the immediately preceding position snapshot. It is an observable geometric
change only; the metric does not call it a causal response. Refuge rectangles
are independently present in every prompt, and the movement may continue a
pre-existing trajectory.

Dangerous-area residence, final refuge occupancy, uninterrupted terminal
refuge-suffix completion step, and explicit null/censoring follow the existing
mechanical disaster definitions.

## 8. Derived artifact and matrix compatibility

The CLI publication layout is:

```text
<derived_root>/disaster-metric-v2.0.0/
  .staging/<run_id>-<temporary-id>/
  <run_id>/
    analysis_meta.json
    agents.jsonl
    warning_outputs.jsonl
    summary.json
    derived_manifest.json
```

The derived root must resolve outside every directory that is recognizable as
an immutable `output_<run_id>` raw run, not merely outside the run currently
being analyzed. All bytes are prepared before publication, written exclusively
to same-filesystem staging, flushed, re-read, and atomically renamed. An
existing final run leaf is an immutable collision and is never overwritten,
suffixed, appended, or resumed. A staging leaf is not a result.

`analysis_meta.json` records the source run/protocol/log/declared-metric
versions, declared and actual metric-spec hashes, config hash, source Git state,
source raw manifest, exact input manifests, communication mode, seed, and bloc
composition. It also records the analyzer Git SHA/dirty/probe state and exact
manifests for the metric core, CLI, and direct warning-identifier dependency.
`source_declared_metric_matches_analysis` is true only when both the declared
metric version and declared spec digest match, distinguishing prospective
matrix use from post-hoc derivation.

The command is:

```text
python tools/disaster_metric_v2.py \
  --run-dir <raw-output-directory> \
  --derived-root <derived-root> \
  --metric-spec-sha256 <sha256-of-this-file> \
  --require-declared-metric
```

Matrix aggregation must require the exact prospective run set. Null, negative,
aborted, contradictory, and non-reuse observations must not be filtered based
on outcome. Aborted runs cannot be passed to this per-completed-run metric and
remain separate raw incident evidence.
