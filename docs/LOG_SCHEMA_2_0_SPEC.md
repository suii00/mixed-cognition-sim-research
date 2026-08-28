# Log schema 2.0.0

## Run metadata

`run_meta.json` records immutable run ID, lifecycle status, public effective config and its
canonical SHA-256, source Git state, prompt/response-schema hashes, protocol/metric versions,
logical model/endpoint identities, dependency versions, non-unique GPU environment facts,
failure counters, completed steps, and a SHA-256 manifest of raw files.

The public config snapshot is exact. No transformed or filtered derivative is hashed.

## Raw files

Every run contains:

- `phase1_raw.jsonl`: sender output before delivery
- `messages.jsonl`: delivered exposure records
- `memory_reasoning.jsonl`: Phase 3 action, memory, and model-generated explanation
- `parse_errors.jsonl`: parse failures, normally empty for a completed strict run
- `llm_attempts.jsonl`: request/attempt evidence with logical endpoint identity
- `termination.jsonl`: exactly one terminal lifecycle event

Disaster scenarios additionally contain `world_events.jsonl`, `positions.jsonl`, and
`warning_events.jsonl`.

## Ordering and identity

Primary records use deterministic `(step, phase, agent_id)` commit order. Concurrent worker
completion order is not semantic. Attempt and terminal records carry stable event/request IDs.
Logical endpoint identity uses `endpoint_id` and optional `device_slot`.

## Phase 3 direction

`action=move` requires one cardinal string. `action=stay` permits null; the canonical
phase-response-v2.0.0 contract requires null for stay.

## Integrity

Raw files are created once, never appended across runs, and never edited after terminal
manifest creation. `tools/validate_run.py --strict` recomputes hashes, natural-key coverage,
cross-file message reconstruction, phase/schema contracts, counters, and terminal state.
