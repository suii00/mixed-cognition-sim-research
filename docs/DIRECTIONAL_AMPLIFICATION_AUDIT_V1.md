# Directional Amplification Engineering Audit v1

Version: `directional-amplification-audit-v1.0.0`

## 1. Status and claim boundary

This document pre-registers a six-run engineering audit. Every generated config
sets `research_eligible=false`. The audit may diagnose simulator mechanics and
generate hypotheses, but it cannot establish a research result and cannot be
pooled with a later formal matrix.

The source observation is the immutable engineering run
`engineering-ja-qwen25-swallow05-elyza8b-24a60s-s2403-20260830-r001`.
Its raw action log contains a strong rightward pattern. Inspection also shows a
mechanical candidate explanation: messages are delivered in ascending agent-ID
order and each agent exposes only the most recent configured messages to the
next prompt. In the source run, the highest-ID bloc is ELYZA and the context
size is three.

## 2. Question

Does the observed directional pattern materially depend on which model occupies
the highest agent-ID block when only the three most recent messages are visible,
and does that dependence change when every same-step delivery can remain in a
23-message context?

This audit does not test memory removal, communication removal, prompt option
order, or causal adoption. Those require separate versioned interventions.

## 3. Observed chain

1. Phase 1 requests settle from one step-start snapshot.
2. Non-empty messages are delivered in canonical ascending sender-ID order.
3. Receiver history is truncated to `message_history_limit`.
4. Phase 3 requests use the last `message_context_size` retained deliveries.
5. Every Phase 3 decision settles before any movement is applied.
6. Raw messages, actions, positions, attempts, terminal metadata, and hashes are
   preserved unchanged.

The audit metric reconstructs Phase 3 prompt-visible delivery references from
the canonical message order and the exact two configured limits. A delivery is
not treated as reuse or adoption. Model-generated `reasoning` is not analyzed.

## 4. Frozen conditions

All cells use the exact model artifacts, tokenizer revisions, chat-template
hashes, Japanese prompt contract, response contract, transport behavior,
sampling, world seed, world, phase order, communication radius, edge policy,
memory settings, and concurrency from the pinned source config, except for the
declared changes below.

- world seed: `2403`
- duration: `10` steps
- agents: 24, eight per model
- temperature: `0.0`
- prompt contract: `japanese-prompts-v1.0.0`
- response contract: `phase-response-v2.0.0`
- edge policy: `full`
- communication radius: `100`
- memory limit/context: `20/5`

World seed alone is not claimed to make LLM output deterministic.

## 5. Declared factors

### 5.1 Message context

- `c03`: `message_history_limit=10`, `message_context_size=3`. This reproduces
  the source-run visibility policy.
- `c23`: `message_history_limit=23`, `message_context_size=23`. Every message
  delivered in the current step remains eligible for that step's Phase 3
  prompt. When fewer than 23 messages are delivered, older retained messages may
  also be visible and are reported separately.

### 5.2 Model-to-agent-ID rotation

- `r0`: Qwen, Swallow, ELYZA from low to high IDs.
- `r1`: Swallow, ELYZA, Qwen from low to high IDs.
- `r2`: ELYZA, Qwen, Swallow from low to high IDs.

Model and bloc identity are not inserted into prompts. Rotation changes only
which pinned model profile receives each canonical eight-agent ID block and its
paired initial positions.

The cross product produces exactly six cells in this order:
`c03-r0`, `c03-r1`, `c03-r2`, `c23-r0`, `c23-r1`, `c23-r2`.

## 6. Direct observations and mechanical derivations

Per run, the audit reports:

- action counts and right/left rates by model;
- signed horizontal choice index `(right-left)/(right+left)`;
- the same index before a horizontal boundary is reached;
- per-step dominant direction and consensus share;
- first three-step cascade with one direction at or above 75 percent;
- outward choices at world boundaries;
- Phase 3 visible-message slots by sender bloc;
- current-step versus retained older visible slots;
- literal right/left character presence in visible message text;
- one-step-lag alignment of non-high-ID agents with the preceding high-ID
  bloc's unique modal action.

Literal character presence is a mechanical text feature, not semantic belief or
adoption. Boundary action choice and realized displacement are kept separate.

## 7. Pre-registered engineering decision rules

1. **Mechanical sender-order dominance:** for every rotation, the high-ID bloc's
   visible-slot share in `c03` exceeds its paired `c23` share by at least 0.25.
2. **Behavioral context signal:** in at least two rotations, the `c03-c23`
   difference in one-step-lag non-high-ID alignment is at least 0.10 in the same
   direction.
3. **Context-robust right pattern:** the overall right-action rate is at least
   0.75 in all six cells.

Each rule is reported independently. Failure of a rule is retained as a null or
contradictory result. These thresholds are engineering diagnostics, not
confirmatory significance tests.

## 8. Execution and integrity gates

Before execution:

- the plan, generated configs, and generated manifest must match their builder;
- all six run IDs and config hashes must be unique;
- every config must pass the public vLLM contract check;
- focused regression tests must pass;
- execution must use a clean exact source commit.

After execution:

- all six runs must complete 10/10 steps with 24 agents;
- transport, syntax, schema, and parse-error counts must be zero;
- strict validation and publication scans must pass;
- raw runs and validation evidence remain immutable;
- aggregate analysis is written only under a new versioned derived directory.

## 9. Analysis restrictions

- Do not use generated reasoning as access to internal reasoning.
- Do not count delivery alone as reuse, adoption, or belief change.
- Do not treat agent-step rows as independent experimental replicates.
- Do not promote or relabel this audit as research eligible.
- Do not select only rotations or context conditions that support the initial
  interpretation.
