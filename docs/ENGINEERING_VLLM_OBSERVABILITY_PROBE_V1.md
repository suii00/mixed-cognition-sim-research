# vLLM observability compatibility probe v1.0.2

Status: r003 engineering compatibility gate passed on 2026-08-25; not a
research run and not authorization for the disaster120 pilot.

## Purpose and claim boundary

This probe checks that Qwen, Llama, and Gemma can traverse the new log schema
1.3 client path before another 120-step pilot is considered. A separate paired
direct-HTTP subprobe checks whether this exact vLLM build accepts decoder-side
JSON Schema and enforces a string `maxLength`. Both checks concern transport
compatibility and evidence capture only. Their generated behavior is not part
of the disaster study, cannot be pooled with r001 or r002, and cannot support
a behavioral or model-comparison claim.

The simulation probe uses the existing Phase 1 and Phase 3 prompts without
changing `engine/prompts.py`. It retains the r002 execution envelope:
temperature 0, maximum generation 512 tokens, vLLM `json_object`, BF16,
context 4,096, the same model snapshots, and the same chat templates. The
paired direct-HTTP subprobe uses an explicitly engineering-only prompt and
compares `json_object` with `json_schema` using a string `maxLength` of eight.
Neither decoder-side JSON Schema nor `maxLength` is added to the simulation
client allowlist, baseline config, prompt, or proposed r003 run.
The request envelope follows the
[vLLM 0.27.1 structured-output documentation](https://github.com/vllm-project/vllm/blob/v0.27.1/docs/features/structured_outputs.md).

## Observed r001 and r002 results and prospective r003 correction

The authorized r001 operation at source `8549d18` completed and strictly
validated all six simulation calls. Each Qwen, Llama, and Gemma Phase 1/3
attempt retained HTTP 200, `finish_reason="stop"`, usage, response bytes and
hash, request/agent/step/phase identity, endpoint, and GPU UUID. All recorded
transport, syntax, and response-schema checks passed.

The direct structured-output program then failed before making any HTTP
request because executing `tools/probe_vllm_json_schema.py` did not add the
repository root to Python's import path. The complete operation is therefore
`SIMULATION PASS / DIRECT COMPATIBILITY NOT RUN / OVERALL INCOMPLETE`; its
negative evidence is retained at
`evidence/observability-probe-20260825-r001-negative/`. It supplies no evidence
for or against decoder-side JSON Schema or `maxLength` enforcement.

r002 adds the repository root before importing `engine` and adds a subprocess
regression that removes `PYTHONPATH` before invoking the CLI. No prompt,
sampling, token limit, model, snapshot, chat template, GPU binding, request
payload, decoder constraint, or pass criterion changes. r002 uses protocol
`engineering-vllm-observability-probe-v1.0.1`, a new run ID, and fresh scratch
and output roots. It must rerun all twelve requests; r001's six successful
simulation calls are not pooled into an r002 PASS.

The authorized r002 operation at source `e9cde19` completed and strictly
validated all six simulation calls. Its first direct `json_object` control
returned HTTP 200, `finish_reason="stop"`, valid JSON, and a 26-character
lowercase alphabet for `note`. The requested value was the 16-character string
`abcdefghijklmnop`. The r002 program required exact equality, classified this
otherwise-long control as `control_did_not_reproduce_requested_long_value`,
and stopped after one of six direct requests. No `json_schema` request was
sent. The complete operation is therefore
`SIMULATION PASS / DIRECT CONTROL INCOMPLETE / OVERALL INCOMPLETE`; its
evidence is retained at
`evidence/observability-probe-20260825-r002-incomplete/`. It supplies no
evidence for or against decoder-side JSON Schema or `maxLength` enforcement.

r003 changes the engineering control gate from exact requested-string equality
to a valid exact-field JSON object whose `note` is longer than the comparison
limit of eight Unicode code points. Exact requested-string agreement, observed
length, and whether the limit was exceeded remain explicit evidence fields but
only the length condition gates the control. The JSON Schema case remains
unchanged: it must return a valid exact-field object whose `note` is no longer
than eight code points. The direct evidence schema is bumped to
`vllm-json-schema-compatibility-v1.1.0`.

The authorized r003 operation at source `e023b5a` completed all twelve planned
HTTP attempts: six simulation attempts and six paired direct attempts. The
simulation completed one of one steps with zero retries, transport failures,
syntax failures, schema failures, or parse errors, and its strict validator
passed. For Qwen, Llama, and Gemma respectively, the `json_object` control note
lengths were 26, 26, and 16; all exceeded eight. Each corresponding
`json_schema` response had a note length of eight. All six direct responses
recorded HTTP 200, `finish_reason="stop"`, valid exact-field JSON, usage, and a
passing decision. The operation is therefore
`SIMULATION PASS / DIRECT COMPATIBILITY PASS / OVERALL GATE PASS`. Its raw and
operational evidence is retained at
`evidence/observability-probe-20260825-r003-pass/`.

This result establishes only that these six direct requests passed on the
recorded vLLM 0.27.1 process, model snapshots, and environment. It does not
establish general backend conformance or behavioral robustness, and it does
not add decoder-side JSON Schema to the disaster-study baseline.

## Frozen r003 workload

- config: `configs/engineering_vllm_observability_probe_3model_s2300_r003.json`
- run ID: `engineering-vllm-observability-probe-3model-s2300-r003`
- seed: 2300
- one Qwen, one Llama, and one Gemma agent
- one step, both decision phases, six logical simulation calls
- one paired direct check per model: `json_object` control followed by
  `json_schema` with `maxLength`, six additional HTTP calls
- twelve HTTP requests across the complete compatibility operation
- concurrency: three
- GPUs 0, 6, and 7; ports 8100, 8101, and 8102
- workload timeout after endpoint readiness: 10 minutes
- total operational authorization envelope, including model startup: 30 minutes
- scratch: `/tmp/mcs-observability-probe-20260825-r003`
- output root: `/tmp/observability-probe-20260825-r003`

The exact source SHA is filled from the probe-support commit and must match a
clean remote worktree. GPU UUIDs, model snapshot directories, ports, and idle
memory are checked before any endpoint starts.

## Evidence and pass criteria

The simulation run must complete one of one steps with exactly six logical
calls and six HTTP attempts. `llm_attempts.jsonl` must contain six canonically ordered,
non-mixed records. Every record must preserve the exact HTTP body, decoded API
envelope, raw assistant content, nonempty vLLM `finish_reason`, usage object,
request identity, endpoint identity, and GPU UUID. `termination.jsonl` must
contain exactly one completed record, and strict validation must pass.

The paired direct subprobe asks for the same sixteen-character string in both
cases. The `json_object` control must return a valid exact-field object whose
string is longer than eight Unicode code points; exact requested-string
agreement is recorded but does not gate the control. The `json_schema` case
must return a valid exact-field object whose string is no longer than eight
Unicode code points. All six direct attempts preserve the exact request
and response bytes, hashes, envelopes, content, finish reason, usage, endpoint,
GPU UUID, and pass/fail decision in
`compatibility-json-schema-maxlength/compatibility_attempts.jsonl`. Failure of
the control is classified as inconclusive compatibility and fails this gate;
it is not misreported as evidence that `maxLength` worked.

The probe passes only if generation retries, transport failures, syntax parse
failures, response-schema failures, HTTP non-2xx, endpoint deaths, OOM, CUDA
errors, and Xids are all zero. INFO/WARN text is retained but is not terminal
by itself.

## Stopping and cleanup

Stop immediately for endpoint/process death, an identity mismatch, HTTP
non-2xx or transport failure, syntax or response-schema failure, OOM, CUDA
error, Xid, output collision, strict-validator failure, or wall timeout. The
operation script always sends an interrupt to all three endpoint process
groups, escalates to TERM if required, and records the post-stop GPU state.
No GPU process is intentionally left resident.

The immutable run directory, server stdout/stderr including empty files, GPU
telemetry, pre/post `nvidia-smi`, source SHA, config hash, validator output,
and any negative or aborted evidence are retained.

## Execution sequence

Use `tools/ops_vllm_observability_probe_20260825.sh` with the committed source
SHA:

```bash
EXPECTED_SOURCE_SHA=<sha> bash tools/ops_vllm_observability_probe_20260825.sh preflight
EXPECTED_SOURCE_SHA=<sha> bash tools/ops_vllm_observability_probe_20260825.sh start
bash tools/ops_vllm_observability_probe_20260825.sh status
EXPECTED_SOURCE_SHA=<sha> bash tools/ops_vllm_observability_probe_20260825.sh run
```

`status` may be repeated until all endpoints are ready. `run` stops all three
endpoints on either success or failure. `stop` is the manual cleanup command.

This completed probe shows compatibility only. In particular, it does not make
decoder-side schema enforcement part of the study baseline and does not
authorize r003. A fresh complete twelve-cell
120-step r003 pilot must retain seed 2299, all four compositions, all three
communication conditions, 57,600 logical calls, new run IDs, and its own
explicit authorization envelope. No r001/r002 successful subset is reused.
