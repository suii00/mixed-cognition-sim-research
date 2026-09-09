# Disaster behavior pilot descriptive metric v1

Metric version: `disaster-behavior-pilot-metric-v1.0.0`.
Protocol: `disaster-llm-behavior-pilot-v1.0.0`.
This specification is frozen before this pilot's model requests. It contains
no results from this pilot. Its mechanical definitions retain an existing
descriptive method under a distinct identifier for this new experiment.
Prior completed runs informed engineering choices and are preserved in local
custody; they are not observations in this pilot. This is a prospective
descriptive study, not a blind confirmatory study.

## Population and eligibility

The fixed population is six runs: three homogeneous model conditions, world
seeds 6201 and 6202, four agents, 60 steps, free-text communication. The public
manifest at `configs/disaster_behavior_pilot_v1/manifest.json` fixes the model
artifacts, config hashes and run IDs for batch
`disaster-llm-behavior-pilot-v1-20260909T120500Z`. No replacement or
outcome-dependent extra run is included. This is a descriptive comparison;
agents and steps are not independent experimental replicates.

Every planned run appears in the status table, including missing, interrupted,
aborted, failed and invalid runs. Counts from incomplete runs are operational
coverage only. Behavioral summaries and examples require completed status,
`aborted: false`, strict raw validation, exact manifest/config agreement,
60 steps and four agents, 480 logical calls and HTTP attempts, and zero retries
and response/transport failures.
Comparison-eligible runs must have clean source state and the same recorded
source commit across the completed set. A missing or incomplete model condition
makes its seed ineligible for the three-model example. No partial run is
promoted to a completed observation. Missing runs are called
`not_started_or_unavailable`; absence alone cannot establish that no remote
execution occurred. Descriptive comparison eligibility does not confer research
or formal eligibility: `research_eligible=false` and `formal_eligible=false`.

## Mechanical observations

Actions come from `memory_reasoning.jsonl`, one Phase 3 choice per agent/step.
Count `stay` and `move`; count `up`, `down`, `left`, `right` only for `move`.
The two windows are steps 1–60 and steps 10–60 inclusive (240 and 204 choices
per complete run). A move choice can be blocked at the world boundary; these
counts describe choices, not displacement. Every count retains its contributing
raw-line references. No rate is formed with incomplete coverage.

For a position `(x,y)` and inclusive refuge rectangle
`[x_min,x_max] × [y_min,y_max]`, Manhattan distance is
`max(x_min-x,0,x-x_max) + max(y_min-y,0,y-y_max)`.
Nearest-refuge distance is the minimum across all refuge rectangles, matching
`engine.disaster.Rectangle.manhattan_distance`. It is not toroidal or Euclidean
distance. Report each agent's initial and final nearest-refuge distances, and
the minimum over initial plus all post-movement positions, with all tied minima
and raw references. Initial state is step 0; subsequent states are post-movement.

Hazard residence counts post-movement positions marked hazardous in each window.
First refuge arrival is the earliest initial/post-movement position inside any
refuge; absent arrival is null with right-censoring at step 60. Final refuge
occupancy and final continuous refuge-occupancy completion use unchanged
`disaster-metric-v2.0.0`; arrival does not imply remaining in refuge.

Warning exposure, phase-aware first eligible output, strictly later-step exact
warning-ID reuse, right-censoring, and frozen surface-fact classifications use
the unchanged `docs/DISASTER_METRIC_V2_SPEC.md` and its implementation. Its
SHA-256 is `cd0ca4fec67d945a2b878fe5c0c7e49391e9bacf10a3f7aa0a142f7962cbf711`.
Exposure is not reuse or adoption. Same-step output is not later-step reuse.
Generated explanations are output fields, not access to internal reasoning.
Temporal succession does not establish that warning exposure caused movement.
Zero exact-ID reuse does not establish rejection, forgetting, or absence of
semantic understanding. Preserve all null, negative and contradictory results.

## Pre-specified example

Order eligible seeds numerically ascending, then steps 10–60 ascending, then
agent IDs ascending. Select the first tuple for which the set of the three
models' `(action,direction)` pairs has size greater than one. Present all three
models, irrespective of apparent success. If none exists, preserve a null
example and its explicit reason. Do not search other seeds or criteria.

For each of the three rows include run/config/source provenance, Phase 1 raw
output, Phase 3 action row and HTTP-attempt row, pre- and post-movement position
rows, and all warning exposures through that step. Each embedded raw observation
has original filename, one-based physical JSONL line, byte length, line SHA-256,
and run ID. These selected quotations are evidence in an analysis artifact;
no rewritten raw dataset is created. Raw files remain untouched.

Check initial-position equality across completed model conditions for each seed.
Record whether positions immediately before the selected action agree and the
earliest prior action or Phase 1/Phase 3 output disagreement. Even matching
positions do not establish identical prompts or histories. All examples carry
the statement that trajectories and input histories can already differ; no
same-input or causal model-effect claim is made after divergence. Selection at
or after step 10 does not imply that the selected agent received a warning;
report its recorded exposure, including an empty exposure history, explicitly.

## Integrity, boundaries and derivation

Before output creation, validate the manifest, its public config file hashes,
effective config equivalence and metric specification hashes; scan the manifest,
configs and all available run files read-only for publication-boundary findings.
Reject symbolic links and unsafe data. Strict validation is run for each available
run; invalid or incomplete runs remain in the status inventory but supply no
behavioral comparison. Analysis files are prepared and scanned in memory before
the immutable destination is created. No sanitizer or transformed public copy
is used. Scan findings stop derivation rather than changing source content.

Record metric/spec and implementation hashes, analyzer source commit/dirty flag,
manifest hash, each config file hash/effective config hash, source run commit,
protocol/prompt/response/log versions, raw file manifests and validation limits.
Recheck input byte hashes before publication. Outputs go only outside raw and
other immutable artifacts, to a new leaf named
`disaster-behavior-pilot-metric-v1.0.0_<YYYYMMDDTHHMMSS[ffffff]Z>`.
An existing leaf is an error before any writes. A derived manifest hashes every
produced file except itself. Failed creation is preserved, never overwritten.

The report separates direct observations, deterministic derivation, interpretation,
and proposals. It makes no statistical superiority, internal cognition, mixed
population, communication intervention, or real-disaster effectiveness claim.
The aim is to make information transfer and subsequent behavior auditable as a
prerequisite for social coordination; successful execution does not establish
improved safety or an end-to-end exposure-to-reuse-to-evacuation chain.
