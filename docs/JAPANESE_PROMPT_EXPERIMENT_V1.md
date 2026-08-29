# Japanese Prompt Experiment v1

## Status

This is a prospective experimental prompt condition. No model execution or
research result is claimed by this document.

## Objective

Measure Japanese-capable instruction models under one shared Japanese prompt
condition while preserving the simulator's phase order, world observations,
communication boundary, response schema, and raw-output logging.

Candidate model repositories are:

- `llm-jp/llm-jp-4-8b-instruct`
- `tokyotech-llm/Llama-3.1-Swallow-8B-Instruct-v0.5`
- `elyza/Llama-3-ELYZA-JP-8B`

Model or bloc identity is not inserted into any agent prompt. A future runnable
configuration must pin each model and tokenizer revision and the exact chat
template hash before output creation. This document does not provide unpinned
or placeholder artifact facts.

## Declared intervention and control

The intervention is selection of
`simulation.prompt_contract_version = "japanese-prompts-v1.0.0"`.
The paired language control is `current-prompts-v2.0.0`.

The Japanese contract translates the common Phase 1 and Phase 3 observation and
decision instructions. It requires natural-language response values to be in
Japanese. JSON keys and the machine-interpreted `action` and `direction` enum
values remain unchanged so that `phase-response-v2.0.0` retains the same schema.

The following must remain fixed within a paired comparison unless separately
declared as an experimental factor:

- world and scenario configuration;
- seed and initial-position procedure;
- phase barriers and communication policy;
- response, transport, failure, log, and metric contract versions;
- temperature, token limit, concurrency, and other generation parameters;
- model artifact and chat-template facts within each repeated model condition.

## Observed chain

1. The public config selects the versioned Japanese prompt contract.
2. The simulator copies the common step/phase snapshot before dispatch.
3. `engine/japanese_prompts_v1.py` formats that snapshot without model identity.
4. The request is submitted as one user message through the tokenizer-owned chat
   template pinned by the model condition.
5. The existing phase response schema validates the English JSON keys and enums.
6. Raw responses, parsed fields, attempts, prompt hash, config hash, source commit,
   and terminal metadata are retained by the existing run lifecycle.

Reception of Japanese text remains exposure only. Reuse or adoption still
requires evidence in the receiving agent's own later output.

## Pre-execution decision rules

Before a GPU run is authorized:

1. The exact model and tokenizer commit digests and chat-template hashes must be
   recorded in a new public config.
2. Contract validation and Japanese prompt regression tests must pass.
3. A one-step engineering smoke run must complete with zero transport, syntax,
   and schema failures for each model condition.
4. The smoke run must pass strict run validation, terminal metadata checks,
   publication-boundary scanning, and expected phase coverage.
5. Any null, negative, aborted, or contradictory smoke result must be retained;
   it must not be replaced by a selected favorable run.

A failed condition is reported as observed incompatibility under the pinned
artifact and contract; it is not silently repaired with a model-specific prompt.

## Model-source basis

The three model repositories document tokenizer chat-template usage. ELYZA's
model card additionally demonstrates a Japanese-response instruction. The
shared user-message approach here avoids introducing a different agent prompt
for only one model condition.

- <https://huggingface.co/llm-jp/llm-jp-4-8b-instruct>
- <https://huggingface.co/tokyotech-llm/Llama-3.1-Swallow-8B-Instruct-v0.5>
- <https://huggingface.co/elyza/Llama-3-ELYZA-JP-8B>
