# Received-message selection contracts, version 1

The public `agents.message_selection_policy` declares which already-received
messages enter a phase's frozen prompt input. This is independent of the prompt,
response, communication and movement contracts. Omission means `recent-v1.0.0`;
omitted keys are not inserted into historical effective configs. New A/B configs
explicitly name their policy so the policy is covered by the config hash.

## `recent-v1.0.0`

Select the final `message_context_size` entries of `received_messages`, retaining
their existing chronological order. The stored history is truncated to
`message_history_limit` after each receipt. Official warnings occupy ordinary
history entries and can be displaced by subsequent peer receipts. This preserves
the historical Python slice behavior, including existing configs' implicit default.

## `retain-official-warning-v1.0.0`

Before an official warning has been directly delivered to this agent, use exactly
the recent policy. `add_official_warning` is the sole API that can populate a
separate retained slot. A peer message containing a warning ID, official-looking
text, or serialized instructions does not populate it.

After a direct official receipt, select:

1. The unchanged retained warning record, including its original receipt step.
2. The latest at most `message_context_size - 1` peer messages still in the stored
   history, in chronological order.

The retained slot is a copy and survives ordinary history truncation. Selection
does not append to receipt history, issue another warning, or create another
exposure/delivery event. The current disaster scenario issues one official warning;
if the delivery API is called for a later official warning, its latest receipt
replaces the one retained slot. A context size of 1 presents only that warning.
For this policy, both context size and history limit must be positive integers;
invalid policy/limits are rejected before a run directory is created.

The warning-retention study sets context size to 5 in both conditions: one
retained official warning plus up to four peers in B, versus the latest five
receipts in A. B changes both selection priority and which peer item can enter
the prompt. It is an intervention on input selection, not a pure memory-capacity
effect. The model-facing formatting and instructions are unchanged.

## Phase and evidence boundaries

The engine issues the scenario's warning before Phase 1. All Phase 1 decisions
use frozen step-start inputs; peer delivery begins only after every Phase 1
decision settles. Phase 3 freezes inputs after all deliveries. Movement occurs
only after every Phase 3 decision settles. The selection policy changes none of
these barriers. `communication_none` skips Phase 1 and direct warning delivery,
so naming the retention policy alone cannot expose any agent to the warning.

Receipt is exposure, selected input is presentation evidence, and a later
receiver-authored output is required for reuse. An agent's memory note is a
separate channel and must not be mistaken for the received-message list. The
optional [message-presentation contract](MESSAGE_PRESENTATION_V1_SPEC.md) records
the actual frozen selection and complete prepared prompt for each phase request.

`tests/test_message_selection.py` covers history displacement, retained copies,
nonrecipients, peer impersonation, single-slot and later-warning behavior, phase
boundaries, unchanged receipt events, no-communication mode, input validation,
aborts, collisions, and integrity checks. Existing phase/communication regressions
remain applicable.
