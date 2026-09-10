# Warning retention study metric v1

Version: `warning-retention-study-metric-v1.0.0`.
Frozen before the six planned model runs. This document specifies descriptive
mechanical outcomes, not a test of cognition, semantic adoption, or a population
model effect. The unit of comparison is a paired run at seed 7301, 7302 or 7303.
Report all three B minus A differences separately; agents are not independent
replicates and no pooled significance test is planned.

## Intervention and evidence chain

A uses `recent-v1.0.0`: the five most recent received items. B uses
`retain-official-warning-v1.0.0`: the directly received official warning followed
by at most four most recent peer items. Agents without direct official reception
use A. B is an input-selection intervention and replaces one peer slot; it does
not isolate memory capacity, equalize tokens, or create new reception events.

Keep distinct: official or peer reception; selection of the official item;
exact warning identifier anywhere in the actual request prompt; own Phase 1
message generation; actual receiver edges; receiver own output in a later step;
movement and refuge geometry. A peer can repeat the official text without that
text becoming a directly received official item. Memory can carry an identifier
after the official item disappears. These situations must remain distinguishable.

`prompt_inputs.jsonl` under `message-presentation-v1.0.0` records the actual
selected messages and complete request prompt before dispatch. Check exact
coverage, unique step/phase/agent keys, SHA-256 of the UTF-8 prompt, and equality
of selected items against an independent chronological replay. Replay official
reception before Phase 1, all deliveries in ascending sender order after every
Phase 1, then Phase 3; bound raw history after each reception. B retains a
separate official item even after that item leaves bounded history. An observed
request is an engine dispatch input, not proof that a model attended to it.

For the four previous refuge-layout runs, prompts were not logged. Replay the
same raw event chronology to derive selected items, label results
`mechanical_reconstruction`, and never call these direct prompt observations.
Record config, source SHA and raw line references for this retrospective audit.

## Primary outcomes

For each completed run and each phase, count official-selected agent-phases for
the six direct recipients over steps 10 through 60 inclusive (denominator 306).
Also count exact-ID-present prompts in that population, loss between Phase 1 and
Phase 3 at issue step, and the first absent phase after official reception for
each initial recipient, retaining null if no absence occurs. Preserve all 24
agents and both phases, including all false and null observations.

Use `disaster-metric-v2.0.0` unchanged for exact-ID output carriers, actual
delivery edges, and later-step reuse. Generated speech without a receiver is
generation only. Same-step Phase 1 output after official delivery is possible
but never later-step reuse. Peer reception in Phase 2 cannot explain same-step
Phase 1 output. Report unique noninitial recipients of exact-ID peer deliveries
out of 18, exact-ID delivery edge counts to these recipients, and their own
later-step exact-ID outputs and reused-agent count separately. Retain the v2
surface-fact statuses as fixed mechanical classifications; unrecognized is not
equivalent to false. Do not infer reuse from reception alone.

## Secondary outcomes and fixed auxiliary text review

Use configured refuge rectangles and Manhattan distance to their nearest cell.
At every snapshot retain distance, refuge membership, hazard classification,
action, displacement and blocked moves. Per agent and run report first arrival
(null/right-censored at step 60 if never), final occupancy, mean final distance,
and hazardous agent-steps over steps 1 through 60. Geometry measures behavior;
the environment always shows refuge geometry, so arrival alone is not evidence
that the warning caused it.

For every Phase 1 message, including empty messages, retain exact-ID status and
fixed case-insensitive lexical flags: hazard words `warning`, `hazard`,
`hazardous`, `inundation`, `flood`, `flooding`; refuge words `refuge`, `refuges`,
`shelter`, `shelters`, `evacuate`, `evacuation`. Match whole ASCII words with
nonletter boundaries. Report non-ID messages with either flag as auxiliary
review candidates, not paraphrase reuse, factual fidelity or semantic adoption.
The HTML exposes all messages and raw references, so no favorable examples are
selected and arbitrary other-language paraphrases are an acknowledged omission.
Never execute model text or URLs. `reasoning` is an explanation field only.

## Integrity, comparison and artifacts

Require strict raw validation, completed terminal status, exact expected agent
and phase coverage, frozen source/config/spec bytes, 2,880 logical calls and
HTTP attempts per run, and zero failure/retry counters for paired comparison.
Preserve missing, aborted, failed and otherwise ineligible planned rows without
replacing them. A pair with either run ineligible has null differences.
Require identical initial positions and model assignments within every pair.

Write only a new `derived/warning-retention-study-metric-v1.0.0_<UTC>` directory
(or `warning-retention-history-v1.0.0_<UTC>` for the prior-run replay). Reject
collision, immutable-tree output, symlinks/reparse points and unsafe publication
inputs before creating any output. Validation/scanning is read-only. Verify raw
tree hashes and watched implementation/config/spec hashes again before writing.
Provenance includes run ID, public config hash, source commit, raw file manifests,
raw line hashes, metric/spec/implementation hashes and analysis source state.

Produce summary JSON/Markdown, agent-phase JSONL, per-agent endpoints and geometry
JSONL, v2 warning outputs, and a self-contained HTML comparison with seed, agent
and step selectors. The view shows A and B side by side: reception, selected
inputs, speech, destinations, action and map; keep all agents and nulls. Model
strings are displayed as inert text. The report is descriptive and exploratory.
