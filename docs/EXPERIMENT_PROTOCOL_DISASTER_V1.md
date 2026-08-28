# Disaster experiment protocol v1.0.0

Status: prospectively frozen implementation candidate; no formal run is
authorized by this document.

## Observation target and chain

The study observes where one fixed official warning identifier is exposed,
later reused in an exposed agent's own generated message, relayed to another
agent, followed by a later movement reducing refuge distance, and associated
with the final spatial outcome. The two domains are information transmission
and spatial movement. The intervention is warning representation or its
non-delivery. The control is `communication_none`.

Missing links are retained as null or right-censored observations. Receipt is
called exposure. Generated exact-ID occurrence is called reuse. Neither is
called psychological adoption.

## Fixed world

- Grid: `[-25,25] x [-25,25]`; 24 unique initial cells sampled by world seed
  from `x=-25..25, y=-25..17`, excluding refuges.
- Hazard ID: `inundation-zone-1`.
- Steps 10-29: `x=-25..25, y=-25..-8` is hazard-classified.
- Steps 30-60: `x=-25..25, y=-25..0` is hazard-classified.
- West refuge: `x=-23..-18, y=18..23`.
- East refuge: `x=18..23, y=18..23`.
- Official warning `warning-inundation-1` is issued at step 10 to agent IDs
  `1,5,9,13,17,21` in communication-enabled conditions. IDs are balanced over
  the sequential thirds used by the mixed composition and fixed before model
  output is observed.

At each step all agents receive only the time-local classification of their
current cell and the fixed refuge rectangles. The prompt does not reveal the
global hazard geometry or a future hazard stage. The official warning carries
the current global hazard geometry and the same refuge facts in either a
natural-language or structured representation. It contains no urgency,
evaluation, reward, route, action instruction, or desired outcome.

## Interventions and invariants

- `free_text`: fixed factual prose payload, then ordinary Phase 1 generation
  and geometric delivery.
- `structured_warning`: the same canonical facts as a JSON object, then the
  same Phase 1 and delivery path.
- `communication_none`: the issue event remains raw evidence, no official
  payload is delivered, Phase 1 is omitted, and no agent message is delivered.
- Phase 1 completes for every agent before any enabled delivery. Phase 3
  completes for every agent before any movement is applied.
- Model/bloc identity is absent from agent prompts. World, sampling,
  communication radius, recipients, and initial state are paired by seed.

## Operational metrics

- Dangerous-area residence: count of post-movement snapshots classified as
  hazardous.
- Evacuation success: refuge occupancy at the final post-movement snapshot.
- Completion step: first step of the uninterrupted suffix of refuge occupancy
  ending at step 60; otherwise null.
- Warning reuse: first Phase 1 output strictly after first exposure containing
  exact ID `warning-inundation-1` under `disaster-metric-v1.0.0`.
- Reuse delay: reuse step minus first exposure; eligible non-reuse is
  right-censored at step 60.
- Movement response: first step strictly after exposure whose post-movement
  nearest-refuge distance is lower than the preceding snapshot; this is not an
  internal-state claim.

## Frozen matrix and execution gate

The repository contains 60 hashed JSON configs: four compositions (Qwen only,
Llama only, Gemma only, and 8/8/8 mixed), three communication modes, and seeds
`2101,2102,2103,2104,2105`. The total is 144,000 logical calls: 115,200 in
communication-enabled cells and 28,800 in communication-none cells. The
contingency ceiling is 165,000.

Execution uses the observed six-GPU, two-worker topology with two GPUs left
free. Seeds 2101-2103 are a separate first authorization envelope. Seeds
2104-2105 require a later audit and authorization. Every worker invocation
requires an approval reference and exact source Git SHA, refuses a dirty
worktree, runs `nvidia-smi`, stops on the first failed run or strict validation,
and never resumes or overwrites a run directory.

Before any formal GPU run: run the full Linux test suite, execute a bounded
three-mode CPU smoke, then request explicit approval stating run count,
logical-call count, wall timeout, stop conditions, source SHA, and output root.
