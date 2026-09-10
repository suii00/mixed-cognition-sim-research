# Message presentation evidence, version 1.0.0

`simulation.input_observability_version: message-presentation-v1.0.0` opts a
log-schema-2.0.0 run into the additional raw stream `prompt_inputs.jsonl`.
The opt-in version is recorded in the public config and `run_meta.json`; the
stream participates in exclusive creation and the terminal raw manifest. Runs
without this opt-in keep their existing raw-file set and effective config keys.
The base response/attempt schemas and all prompt text contracts remain unchanged.

## Raw record

One record is written for each complete prepared Phase 1 or Phase 3 request,
before that phase's batch is dispatched, in `(step, phase, agent_id)` order.
Every row has exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | `message-presentation-v1.0.0` |
| `event_id` | `<run_id>:prompt_input:<request_id>` |
| `run_id` | The unique immutable run ID |
| `request_id` | `step-NNNNNN:phase1:agent-NNNNNN` or `phase3` equivalent |
| `step` | One-based step |
| `phase` | `phase1` or `phase3` |
| `agent_id` | Zero-based agent ID |
| `message_selection_policy` | The effective versioned selection policy |
| `messages` | Deep copy of the actual selected messages in the frozen phase snapshot |
| `prompt` | The exact complete `LLMRequest.prompt` string passed toward transport |
| `prompt_sha256` | Lowercase SHA-256 of that string's UTF-8 bytes, without normalization |

Peer message values retain `sender_id`, `message`, and original `step`. An
official value retains `source_type: official_warning`, `warning_id`, `payload`,
and original `step`. The existing formatter is used; the observation code adds
no model-facing text. The saved prompt excludes runtime bindings and transport
addresses, which are not prompt inputs.

## What can be claimed

The row directly observes request preparation. In a completed run, matching
`llm_attempts.jsonl` keys establish the engine's association with a transport
attempt. A row by itself does not prove that a remote model processed the request:
a failed/aborted phase may retain prepared rows that were never submitted, and
these rows must remain in the terminal manifest without being relabeled as
successful model exposure. No backend attention or internal reasoning is measured.

The complete prompt makes it possible to distinguish a warning in selected
received messages from warning content in agent-written memory or another prompt
section. A warning identifier in a prompt is not evidence of later reuse; the
receiver's own later output must be analyzed separately. Existing runs without
this stream permit mechanical reconstruction from their source/config/raw
receipts, which is a different evidence class from recorded prepared input.

## Read-only verification

`tools/validate_run.py --strict` checks exact fields, version, identities, complete
phase/agent coverage and order, uniqueness, matching attempt request keys, UTF-8
prompt hashes, raw-manifest hashes and counts. Independently of `Agent` selection
methods it replays official and peer receipts, history truncation and policy
selection, then compares the selected message values. It reconstructs the full
prompt with the named prompt builder, public world/config, pre-movement positions
and prior-step agent memory outputs. Extra or altered prompt content is rejected
even if its prompt digest and the fixture's manifest were recomputed.

Completed strict validation is not an approval to discard incomplete runs. The
validator's existing completed-run requirements intentionally fail for an aborted
run; its terminal metadata, complete or partial raw streams and failure counters
remain evidence. Analysis of an aborted request must state whether it was merely
prepared, associated with an attempt, or returned a response.

All records remain ordinary immutable raw evidence. Validation performs no
rewriting, redaction, copying or normalization. Negative, null and aborted results
are preserved under their original run IDs.
