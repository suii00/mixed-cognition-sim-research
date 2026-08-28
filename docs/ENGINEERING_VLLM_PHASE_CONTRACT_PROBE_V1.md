# Engineering vLLM Phase 3 Contract Probe v1

## Status and scope

This document freezes a nonresearch compatibility probe for response contract
`phase-response-v2.0.0` and transport contract
`vllm-openai-compatible-transport-v1.2.0`. Preparing this probe does not
authorize a GPU operation, a disaster simulation smoke, or r004.

The probe asks whether the recorded vLLM 0.27.1 environment can compile and
execute the repository-owned Phase 3 `oneOf` schema on the fixed Qwen, Llama,
and Gemma snapshots. It does not estimate agent behavior and its output is not
eligible for research analysis or pooling with a simulation run.

## Frozen request matrix

The active config is
`configs/engineering_vllm_phase_contract_probe_3model_20260825_r002.json`.
For each model, the probe sends these two direct requests in order:

1. request `action="move"` with `direction="right"`;
2. request `action="stay"` with `direction=null`.

This is six HTTP requests in total. Both requests use the exact Phase 3
response format returned by `engine.response_contracts.response_format_for_phase`
for `phase-response-v2.0.0`. The schema contains no `maxLength`. Each logical
request has one HTTP attempt, no retry, repair prompt, coercion, fallback, or
raw-text modification. Temperature is zero, `max_tokens` is 512, and each HTTP
timeout is 120 seconds.

The prompts select branches only for this engineering compatibility check.
They are not simulation prompts and do not modify `engine/prompts.py`.

## Proposed execution envelope

- Qwen on physical GPU 0 at port 8100;
- Llama on physical GPU 6 at port 8101;
- Gemma on physical GPU 7 at port 8102;
- workload limit: 10 minutes after endpoint readiness;
- complete operation limit: 30 minutes, including startup and cleanup;
- scratch: `/tmp/mcs-phase-contract-probe-20260825-r002`;
- output: `/tmp/phase-contract-probe-20260825-r002/phase3-oneof`;
- maximum direct requests: six.

An execution request must name the committed source SHA and bundle and must
receive separate explicit approval. The wrapper
`tools/ops_vllm_phase_contract_probe_20260825.sh` checks a clean worktree,
source identity, config validity, schema hash, model snapshot directories, GPU
UUID bindings, idle memory, free ports, and `nvidia-smi` before starting any
endpoint. Its `all` operation enforces the 30-minute outer limit.

## Evidence and pass criteria

`tools/probe_vllm_phase_contract.py` creates a new output directory and fails
on collision. It preserves, for every attempted request:

- model digest, endpoint ID, and GPU UUID from the frozen config;
- the exact request object, request bytes as base64, size, and SHA-256;
- HTTP status, exact response bytes as base64, size, and SHA-256;
- the decoded API envelope and unmodified assistant content;
- parsed content, `finish_reason`, usage, timestamps, and the gate result.

The output also contains `probe_meta.json` and an exclusively created
`termination.json`. A pass requires all six requests, HTTP 2xx, a valid API
envelope, `finish_reason="stop"`, a usage object, strict whole-content JSON,
the prospective runtime contract, and exact agreement with the requested
action/direction pair. The attempt order must be Qwen move/stay, Llama
move/stay, then Gemma move/stay.

Any HTTP non-2xx, transport failure, invalid envelope, non-stop finish reason,
missing usage, parse failure, cross-field contract failure, wrong branch,
endpoint death, OOM, CUDA error, Xid, collision, or timeout fails the gate.
The first failed attempt is preserved and no further request is sent. INFO and
WARN text is retained but does not independently stop the probe.

## Interpretation boundary

A six-of-six pass establishes only that both valid `oneOf` branches were
generated through this exact backend, model-snapshot, schema, and hardware
combination. It does not establish general JSON Schema conformance, reject-path
coverage, future-version compatibility, behavioral validity, or formal-run
readiness. A failure is negative engineering evidence and must be retained; it
must not be repaired or converted into a simulation action.

Only after this probe passes may a separately versioned, separately approved
one-step simulation smoke be proposed. A successful probe does not itself
authorize that smoke or disaster120 r004.

## Startup-only r001 attempt

On 2026-08-25, Linux preflight at source `c2952b1` passed 67 tests and the
three endpoints began loading on GPUs 0, 6, and 7. Before any compatibility
request or output directory was created, status monitoring exposed an
operational wrapper defect: the successful no-match result from the final
fatal-pattern `grep` became the implicit return status of `terminal_safe`.
This falsely classified healthy startup as terminal.

All endpoints were stopped immediately. Post-stop `nvidia-smi` recorded every
GPU at 1 MiB and zero utilization. The r001 scratch logs are retained as
startup-only engineering evidence. They contain no probe response and cannot
support a compatibility conclusion. Prospective r002 adds an explicit
successful return after the clean scan and uses fresh scratch, output, config,
and run identifiers. Its scientific response schema and six-request matrix are
unchanged.

## Observed r002 result

The fresh r002 operation ran at source `a26e88e` on 2026-08-25. Linux
preflight passed 68 tests and verified the source, config, schema hash, model
snapshots, GPU UUIDs, idle memory, and ports. All three endpoints became ready
on GPUs 0, 6, and 7.

The six direct requests completed in about eight seconds. Qwen, Llama, and
Gemma each returned the requested `move/right` and `stay/null` branch. Every
request received HTTP 200, `finish_reason="stop"`, a usage object, strict
whole-content JSON, and a runtime-valid prospective Phase 3 object. No request
used `maxLength`; no retry or repair request occurred. `probe_meta.json` and
`termination.json` both record `completed`, and the attempt JSONL contains six
lines with SHA-256
`b5da81c12f011a61249fad4adea226a46067c910fc49383b4507240d901331b0`.

Cleanup stopped all endpoint processes and returned every GPU to 1 MiB and 0%
utilization. No OOM, CUDA error, Xid, HTTP failure, transport failure, parse
failure, or schema failure was observed. The gate result is `PASS`, limited to
the compatibility boundary above. Evidence is retained under
`evidence/phase-contract-probe-20260825-r002-pass/`; r001 remains separately
retained as startup-only negative evidence.
