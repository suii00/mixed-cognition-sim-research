# Refuge layout study metric v1

Metric version: `refuge-layout-study-metric-v1.0.0`.
This specification is fixed before the four new model executions. It describes
one world seed, a fixed mixed population, two refuge layouts, and independent
60/120-step runs. All attempted and missing conditions remain in the status table.
Only complete, strictly validated, manifest-matching runs enter comparisons.
No model ranking, significance test, internal-cognition claim, or real-disaster
effectiveness claim is made. Agent/step rows are not independent replications.

## Inputs and identity

Read the fixed manifest/configs, run_meta.json, positions.jsonl,
memory_reasoning.jsonl, phase1_raw.jsonl, messages.jsonl, warning_events.jsonl,
and world_events.jsonl. Verify source identity, clean-source status, expected
agent/step/call coverage, failure counters, strict validation, raw manifests and
publication boundaries. Never edit raw inputs. Emit source/config/spec/code/raw
hashes, reference exact JSONL lines by SHA-256 including the newline, and check
all input hashes again before creating a new derived directory.

## Time and geometry

Initial positions are step 0. Each completed step's position is the Phase 4
post-movement observation, after every Phase 3 action has been decided. Message
delivery at step s uses positions at the end of step s-1. Official exposure is
before Phase 1; relay exposure is in Phase 2. The analysis clock in disaster
metric v2 groups decision/post-movement for eligibility; it does not change the
execution phase order.

Refuge rectangles include their boundaries. The shortest-refuge distance d is
the minimum Manhattan rectangle distance used by engine.disaster.Rectangle.
Hazard classification follows the active geometry at the observed step. Check
both values and the refuge ID against each raw position row. There are no
obstacles or movement penalties in this world, and one cardinal move costs one
step. The bound is geometric; it does not assume agents choose a shortest path.

For horizon T and an end-of-step snapshot s, remaining moves are T-s and the
reachability margin is T-s-d(s). A negative margin means that, from this
snapshot, a refuge cannot be reached by T even by a shortest path. It does not
mean the agent has never visited a refuge or that the agent lacks information.
At the start of step s, the corresponding budget is T-s+1 and the position is
the snapshot at s-1. Refuge locations are already supplied to every agent in
each prompt; no unknown-location discovery ability is inferred.

## Per-agent and per-run observations

Emit one derived agent-step row for every initial/post-movement snapshot,
including position/model, distance, current hazard/refuge status, action label,
actual displacement, and reachability margins for each applicable horizon.
Attach raw position/action references. A move with zero displacement is counted
as a move choice and separately as a blocked move; it is not reclassified as stay.

Use checkpoints T=60 for every complete run, and T=120 for 120-step runs.
For each agent at T report initial/final/minimum distance, first refuge arrival
including step 0, final refuge occupancy, hazardous post-movement steps 1..T,
move/stay/direction counts, blocked moves, and first snapshot with negative
reachability margin for that T. Absent first arrival is null and right-censored
at T. First arrival and final occupancy are distinct measurements.

At each checkpoint report official/relay exposure events and exposed agents,
later exact-ID reuse agents/outputs, actual message-delivery edges, cross-model
delivery edges, exact-ID delivery edges, and cross-model exact-ID delivery edges.
An edge counts one logged sender/receiver delivery, not mere proximity, one
generated message, or inferred adoption. Model assignment comes from run config
and agent metadata, not model-generated text. Prefix summaries include only
events/outputs at steps <= T.

## Warning semantics

Apply unchanged `disaster-metric-v2.0.0` and its frozen specification to each
complete run for warning exposure, later-step exact-ID reuse, surface-fact
classification, and terminal evacuation-suffix definitions. Preserve its full
agent/output records separately. Exact-ID reuse requires the receiver's own
Phase 1 output in a later numeric step than at least one exposure. Same-step
official-recipient outputs may carry the ID but are not later-step reuse.
Receiving a message is exposure; ID reuse is not evidence of meaning, belief,
adoption, or a causal effect on movement. Missing ID reuse does not rule out
paraphrases. The official warning retains facts at issue; step 30 hazard
expansion does not issue another warning or update the stored payload.

## Comparisons and selection rules

For each duration, pair edge and inset layouts by the fixed seed and agent IDs.
Require identical initial positions and model assignment before comparing.
Report both checkpoint values and inset-minus-edge differences in arrival,
occupancy, hazard residence, and mean final distance. Distances refer to each
condition's refuge geometry. Layout changes also change the corresponding
refuge facts in prompts and the warning; this is part of the declared layout
intervention. Later trajectories and input histories can already differ.

For each layout, retain independent 60- and 120-step run identities and compare
their first 60 steps' parsed Phase 1 outputs, Phase 3 action/memory outputs, and
positions. Report prefix equality and the first differing step/phase/agent in
step, phase, agent order; do not assume equality from seed or temperature zero.
The principal extension description uses the 120-step run's own checkpoints
60 and 120, including first arrivals in steps 61..120 and final-occupancy change.
Do not append execution to an existing 60-step run or combine separate runs
into a fictitious continuous trajectory.

For a concrete mixed-communication example, use duration 60, then manifest run
order, then step >=10, sender ID, receiver ID. Select the first actual
cross-model delivery whose generated message contains the exact warning ID.
If none exists, record null; do not substitute an unrelated message. Include
sender/receiver models, original Phase 1/message rows, matching receiver
exposure rows, raw line hashes, and whether the sender had a prior exposure.
This example demonstrates delivery, not the receiver's subsequent reuse.

## Output discipline

Write only to a new `derived/refuge-layout-study-metric-v1.0.0_<UTC timestamp>`
directory, outside all raw/immutable artifacts and with no symlink ancestry.
Include status/checkpoint/comparison tables, agent_steps.jsonl,
agent_checkpoints.jsonl, warning_agents.jsonl, warning_outputs.jsonl, example.json,
analysis_meta.json and a derived artifact manifest. Scan all public text before
creating output. Null, negative, contradictory and incomplete conditions are
retained; completed-only calculations are explicitly identified.
