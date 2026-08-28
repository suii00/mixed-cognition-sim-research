# Disaster 120-step horizon protocol v1.0.0

Status: engineering pilot r003 failed; formal 120-step execution remains
`NO-GO`.

## Purpose and claim boundary

This study extends the observation horizon of disaster protocol v1 from 60 to
120 steps. It observes the same predeclared chain: official warning event,
initial exposure, later generated exact-identifier reuse, relay exposure,
later movement response, and final refuge outcome. Information transmission
and spatial movement remain the two domains. Warning representation remains
the intervention and `communication_none` remains the control.

The 120-step runs are new trajectories. Temperature zero and a shared world
seed do not prove deterministic LLM output, so they are not described as
literal continuations of completed 60-step runs. Primary comparisons are
within the frozen 120-step matrix. Comparisons with the 60-step study are
horizon-sensitivity analyses, not evidence that duration alone caused a
difference in a particular realized trajectory.

## Single intended horizon change

The scenario, prompt semantics, model snapshots, sampling, phase barriers,
communication conditions, agent count, model proportions, spatial geometry,
warning facts, recipients, and metric detector are unchanged from
`docs/EXPERIMENT_PROTOCOL_DISASTER_V1.md`.

Only `simulation.duration` changes from 60 to 120. In particular:

- the official warning is still issued at step 10;
- the first hazard rectangle is active from step 10 through step 29;
- the expanded hazard rectangle begins at step 30 and remains active through
  step 120;
- refuges, initial eligible cells, communication radius, and model assignment
  rules remain unchanged;
- the prompt does not reveal future hazard stages, model identity, desired
  behavior, or an optimization target.

This fixed schedule isolates a longer post-expansion observation period.
Moving the warning or hazard stages proportionally would be a different
intervention and is excluded from this protocol.

## Operational definitions at 120 steps

`disaster-metric-v1.0.0` is retained because its implementation derives the
horizon from the immutable `expected_steps` field rather than assuming 60.
The definitions are unchanged except for their prospective censor endpoint:

- dangerous-area residence is the count of hazardous post-movement snapshots
  through step 120;
- evacuation success is refuge occupancy at the final step-120 snapshot;
- evacuation completion is the first step of the uninterrupted refuge suffix
  ending at step 120, otherwise null;
- warning exposure remains receipt and is not called reuse or adoption;
- warning reuse remains an exposed agent's own later Phase 1 output containing
  exact ID `warning-inundation-1`;
- eligible non-reuse is right-censored at step 120;
- movement response remains the first later step that strictly reduces
  nearest-refuge distance and is not an internal-state claim.

All null, negative, aborted, and contradictory observations are retained.
No result-dependent vocabulary, threshold, warning payload, or metric change
is permitted.

## Engineering pilot

Pilot seed `2299` is fixed before execution and is excluded from formal
research estimates. The pilot covers all twelve cells:

- compositions: Qwen-only (QQQ), Llama-only (LLL), Gemma-only (GGG), and
  8/8/8 mixed;
- communication: `free_text`, `structured_warning`, and
  `communication_none`;
- 12 runs and 57,600 logical calls total;
- two concurrent workers, six GPUs, and GPUs 1 and 5 left unused as spares;
- upper wall timeout: three hours; expected safe operating interval:
  two to two-and-a-half hours.

The pilot is limited to runtime, batching, log volume, completeness, metric
execution, and strict-validator behavior. Its behavioral results cannot be
used to revise the formal hypotheses, payload, metrics, seeds, or thresholds.
Formal execution requires all 12 runs complete 120/120 steps, all strict
validators pass, expected logical-call counts match, and every retry,
transport, final-parse, schema, OOM, CUDA, and Xid count is zero.

## Formal matrix and staged envelope

Formal seeds are prospectively fixed as `2201,2202,2203,2204,2205` and do not
overlap the pilot.

1. Seeds 2201--2203: 36 runs, 172,800 logical calls, six-hour wall timeout.
2. Seeds 2204--2205: 24 runs, 115,200 logical calls, four-hour wall timeout.
3. Full formal matrix: 60 runs and 288,000 logical calls.

Each seed contains four compositions by three communication modes. A
communication-enabled run makes 5,760 logical decisions; a
`communication_none` run makes 2,880. Each worker receives six runs and 28,800
logical calls per seed. Formal seeds 2204--2205 may start only after the first
three seeds pass the same completeness, failure-counter, log-capacity, and
strict-validation audit used for the pilot gate.

## Runtime topology, evidence, and stopping

Worker A uses Qwen/Llama/Gemma on GPUs 0/6/7 and ports 8100/8101/8102. Worker B
uses GPUs 2/3/4 and ports 8103/8104/8105. GPUs 1 and 5 remain spare. All model
digests, BF16 settings, context 4,096, maximum generation 256, JSON-object
transport constraint, and Gemma memory setting 0.92 match the accepted
60-step formal execution.

Before each workload, verify exact source SHA and a clean worktree, run
`nvidia-smi`, verify GPU UUID/model/port identity, and refuse existing scratch
or output roots. Stop both workers for endpoint/process death, OOM, CUDA error,
Xid, HTTP non-2xx or transport failure, any generation retry, final parse or
schema failure, strict-validation failure, output collision, or wall timeout.
WARN/INFO text is retained but is not independently terminal. Always stop the
six endpoints and record post-stop GPU state.

The immutable configs, manifests, raw JSONL, `run_meta.json`, worker metadata,
server stdout/stderr (including empty files), GPU telemetry, source SHA,
validator output, and any aborted evidence are retained. Every empirical claim
must cite these artifacts and the relevant run IDs.

## Authorization boundary

Preparing, testing, committing, bundling, and transferring the execution code
does not authorize GPU use. Each of the pilot, first-three-seed formal stage,
and last-two-seed formal stage requires explicit authorization that names its
run count, logical-call count, wall timeout, stopping conditions, source SHA,
and output root.

## Observed engineering pilot r001 result

The authorized r001 workload ran on 2026-08-24 at source `2426286` and stopped
on the first registered parse terminal condition. Nine homogeneous runs
completed 120/120 steps and passed strict validation. The mixed free-text run
recorded one generation retry and one final syntax-parse failure at step 69,
Phase 3, agent 8. The paired mixed structured-warning run was interrupted at
57 completed steps, and mixed communication-none had not started. All six
endpoints were stopped and all eight GPUs returned to 1 MiB.

The result is `FAILED / FORMAL NO-GO / CLEANUP PASS`. Full raw, aborted,
server, telemetry, metric, and validation evidence is retained at
`evidence/disaster120-pilot-20260824-r001-negative/`. No formal 120-step run is
authorized by the nine completed cells. Any changed generation envelope
requires a prospectively versioned complete 12-cell pilot with fresh run IDs.

## Prospective engineering pilot r002 amendment (v1.1.0)

Pilot r002 is a complete replacement engineering gate for r001; it does not
combine the nine completed r001 cells with newly generated mixed cells. The
only execution-envelope change from r001 is `llm_defaults.max_tokens` from
256 to 512. Protocol version and run IDs are changed so that no r001 output
can be overwritten or mistaken for r002 evidence.

Seed 2299, all twelve composition-by-communication cells, the six-GPU
topology, model snapshots, BF16, context 4,096, temperature zero, prompt
semantics, world, warning and hazard schedule, JSON-object transport
constraint, retry policy, strict validator, and terminal conditions remain
unchanged. The pilot remains nonresearch and contains 57,600 logical calls.
Its upper wall timeout remains three hours. The higher generation bound does
not change the prompt instruction or target answer length, and it does not
alter any output parser or metric; it provides capacity for a syntactically
complete response before parsing.

The same formal gate applies to all twelve fresh r002 runs: 120/120 steps,
strict validation, exact logical-call counts, and zero retries, transport
failures, final parse failures, schema failures, OOM, CUDA errors, and Xids.
The previous failed and aborted r001 evidence remains immutable. This
prospective amendment does not itself authorize remote GPU use.

## Observed engineering pilot r002 result

The authorized r002 workload ran on 2026-08-24 at source `9cf27ca` and stopped
on the first registered parse terminal condition. Nine runs completed 120/120
steps and passed strict validation. The mixed structured-warning run recorded
one generation retry, two syntax-parse attempt failures, and one final
syntax-parse failure at step 61, Phase 1, Qwen agent 7. Its failed output was
an unclosed JSON object containing a long enumeration of agent identifiers.

The paired mixed free-text run had reached 98 completed steps when its worker
was stopped. Its immutable metadata remains `running` because worker B exited
137 before lifecycle finalization, so strict validation fails. Mixed
communication-none did not start. The aggregate across eleven started run
directories is 50,856 logical calls, 50,857 HTTP attempts, one retry, two
parse-attempt failures, one final parse failure, and zero transport or schema
failures. Nine completed runs produced metric-v1 artifacts. No OOM, CUDA
error, or Xid was found, and cleanup returned all eight GPUs to 1 MiB.

The failed text is consistent with truncation at the 512-token ceiling, but
the retained event has no API finish reason, so that is an inference rather
than a direct observation. The result is again
`FAILED / FORMAL NO-GO / CLEANUP PASS`. Full evidence is retained at
`evidence/disaster120-r002-negative/`. No formal 120-step run
is authorized, and no completed r001 or r002 subset may be combined to pass
the complete twelve-cell engineering gate.

## Prospective engineering pilot r003 amendment (v1.2.0)

Pilot r003 is a fresh complete replacement engineering gate. It does not reuse
or pool any completed r001 or r002 cell. It remains nonresearch and retains
pilot seed 2299, all four compositions, all three communication modes, twelve
runs, 57,600 logical simulation calls, the six-GPU topology, and the three-hour
wall timeout.

The world, warning, hazard schedule, agent count and assignments, communication
conditions, prompt bytes and semantics, model and tokenizer snapshots, chat
templates, BF16, context 4,096, temperature zero, maximum generation 512,
vLLM `json_object`, metric version, and failure thresholds are unchanged from
r002. Decoder-side JSON Schema, `maxLength`, repetition penalty, stop strings,
and any repair or fallback output are not adopted by r003. Compatibility-probe
results cannot alter this frozen baseline without a later prospective protocol.

The engineering measurement path changes as follows and is identified by
protocol `engineering-disaster-120-pilot-v1.2.0` and log schema 1.3.0:

- each logical decision has exactly one generation/HTTP attempt; blind retry
  is removed;
- exact response bytes, HTTP status, API envelope, assistant content,
  `finish_reason`, usage, request ID, endpoint ID, GPU UUID, step, phase, and
  agent are retained in immutable `llm_attempts.jsonl`;
- assistant content must be exactly one whole JSON object, apart from surrounding
  whitespace;
- client-side Phase 1 and Phase 3 response schemas are checked before state
  application;
- syntax or response-schema failure aborts the run before Phase 2 delivery or
  Phase 4 movement for the failing phase;
- completion, SIGINT, SIGTERM, and controlled failure create exactly one
  immutable `termination.jsonl` record and finalize `run_meta.json` when the
  process receives a catchable signal.

These changes prevent an unobserved model-generation failure from being
recorded as a simulated `stay` or no-message action. They change measurement
and missingness handling, not the modeled disaster intervention. Consequently,
r003 is a new trajectory set and cannot be compared with r001/r002 as though
only stochastic replication differed.

r003 may start only after the separately versioned three-model compatibility
probe passes its Phase 1/3 envelope checks and its engineering-only structured
output checks. The structured-output subprobe is not a research cell and its
decoder constraints remain absent from r003.

The r003 gate requires all twelve fresh runs to complete 120/120 steps, exact
logical-call and attempt counts, exactly one terminal record per run, all
strict validators passing, and zero retry, transport, HTTP non-2xx, syntax,
schema, endpoint, OOM, CUDA, and Xid failures. INFO/WARN text remains evidence
but is not independently terminal. Any failed, aborted, null, or negative r003
evidence is retained. This amendment prepares no GPU authorization: execution
still requires an exact source SHA, output root, stop conditions, run and call
counts, and explicit approval after the compatibility probe result is known.

## Observed engineering pilot r003 result

The authorized r003 workload ran on 2026-08-25 at source `f314546`. Linux
preflight passed 387 tests and verified the complete twelve-run, 57,600-call
matrix. Six endpoints became ready on GPUs 0/6/7 and 2/3/4 before two workers
started the fresh output root
`/tmp/disaster120-pilot-r003-20260825-r001`.

Worker B's first condition, Qwen-only structured warning, aborted at step 14
Phase 3 after completing 13 steps. Agents 5 and 18 each returned HTTP 200 with
`finish_reason="stop"` and a whole JSON object containing `action="stay"` and
`direction="none"`. The client schema requires a stay direction to be empty or
cardinal, so both attempts were retained as schema failures and state was not
applied for that phase. The run recorded 672 logical calls and HTTP attempts,
zero retries, transport failures, or syntax failures, and two schema failures.

The terminal condition stopped both workers. Worker A's Qwen-only free-text
condition was interrupted at step 14 after 13 complete steps and 648 logical
calls/HTTP attempts with zero recorded generation or response failures. Its
interrupt is a coordinated stop consequence, not the initiating failure.
No run completed; the remaining ten cells never started. The operation is
therefore `FAILED / FORMAL NO-GO / CLEANUP PASS`. It must not be repaired,
pooled, or treated as a shortened research matrix.

No OOM, CUDA error, Xid, endpoint death, or transport failure was observed.
Cleanup stopped all six endpoints and returned all eight GPUs to 1 MiB and 0%
utilization. Full negative and aborted evidence is retained at
`evidence/disaster120-r003-negative/`.

## Prospective response-contract remediation (v1.3.0)

On 2026-08-25 Asia/Tokyo, Su explicitly approved local implementation of a new
prospective engineering contract based on source `98d4bb0`. This authorization
does not reinterpret, repair, pool, or replace r003 evidence and does not
authorize a GPU operation or an r004 simulation run.

Protocol `engineering-disaster-120-pilot-v1.3.0` selects response contract
`phase-response-v2.0.0` and vLLM transport contract
`vllm-openai-compatible-transport-v1.2.0`. Phase 1 requires exactly string
fields `message` and `reasoning`. Phase 3 uses the following canonical pairs:

- `action="move"` requires `direction` to be one of `up`, `down`, `left`, or
  `right`;
- `action="stay"` requires `direction` to be JSON `null`.

The Phase 3 prompt states this serialization contract. It does not add a
preferred action, destination, warning response, or evacuation objective. The
same Phase 3 decoder schema applies to all twelve cells. The same Phase 1 schema
applies to every Phase 1 call in the eight communication-enabled cells;
`communication_none` continues to omit Phase 1 by design.

The client selects repository-owned schemas by immutable request phase. Configs
cannot supply an arbitrary schema or combine a bloc-level `response_format`
with this contract. Raw response bytes and assistant text remain unmodified.
There is no repair retry, coercion, fallback action, or string replacement.
Runtime and offline validation dispatch from the recorded response-contract
version. `run_meta.json` records that version, the transport-contract version,
and the canonical phase-schema bundle hash. Raw log file shapes remain schema
1.3.0; this protocol change does not relabel old logs or make r003 eligible.

No `maxLength` is adopted for simulation fields by v1.3.0. The earlier
eight-character compatibility probe establishes only that the backend enforced
that keyword in three direct requests. Any future bound on Phase 1 message,
Phase 3 memory, or reasoning is a separate prospective inference condition
because it may change communication or later decisions.

The next gate is a nonresearch compatibility probe that exercises both Phase 3
`oneOf` branches for Qwen, Llama, and Gemma and preserves request bodies,
response envelopes, finish reasons, usage, and exact raw output. Local tests or
the earlier maxLength probe cannot establish this compatibility. No simulation
smoke or formal run may start until that probe passes under a separately fixed
request count, GPU assignment, timeout, output root, source SHA, and explicit
authorization. Its prospective engineering scope and pass boundary are frozen
in `docs/ENGINEERING_VLLM_PHASE_CONTRACT_PROBE_V1.md`; adding that document and
its local tooling does not itself authorize execution.

The fresh Phase 3 compatibility probe r002 subsequently passed six of six
direct requests at source `a26e88e`: Qwen, Llama, and Gemma each generated both
`move/right` and `stay/null` with HTTP 200, `finish_reason="stop"`, usage, and
strict contract validation. This clears only the decoder-compatibility gate.
It does not retroactively repair r003 and does not authorize a simulation
smoke or r004; those remain prospective, separately approved operations.

## Prospective pilot r004 authorization

On 2026-08-25 Asia/Tokyo, after the Phase 3 compatibility gate passed, Su
explicitly approved starting disaster120 r004. The source commit containing
this prospective amendment and its generated matrix must be recorded at
runtime; a dirty worktree is prohibited.

r004 retains the frozen r003 envelope: pilot seed 2299, 24 agents, 120 steps,
four compositions, three communication modes, twelve nonresearch runs, 57,600
logical calls, two worker slices, six endpoint GPUs (0/6/7 and 2/3/4), and a
three-hour wall limit. Its fresh output root is
`/tmp/disaster120-pilot-r004-20260825-r001`.

The prospective protocol version is
`engineering-disaster-120-pilot-v1.3.0`. Relative to r003, r004 changes only
the previously approved response-contract boundary: phase-aware repository
JSON Schemas, `move` with cardinal direction, `stay` with JSON null, and
transport contract v1.2.0. World, seed, composition, communication mode,
sampling temperature, 512-token limit, agent count, phase barriers, and metric
version remain fixed. No field uses `maxLength`; no repair retry, coercion, or
raw-output modification is allowed.

The operation stops both workers and all endpoints for process death, OOM,
CUDA error, Xid, HTTP/transport failure, retry, syntax or schema failure,
aborted/failed run metadata, strict-validator failure, output collision, source
or binding mismatch, or wall timeout. Failed, aborted, negative, empty, and
server-log evidence must be retained. r004 remains a nonresearch pilot and
does not make the prior r003 runs eligible or comparable as stochastic
replicates.

The first r004 Linux preflight attempt at source `0db55a2` stopped before
scratch creation or GPU startup because ten retired Ollama Prompt6 tests still
guarded only the historical whole-file hash of `engine/prompts.py`. The other
403 discovered tests passed. This was a preflight compatibility defect, not a
model, decoder, or simulation observation. The prospective fix keeps both the
historical and approved current prompt-source hashes explicit and continues to
test Prompt6 through the legacy response-contract dispatch; it does not alter
r004 prompts, configs, or sampling. A fresh source commit and a fresh preflight
are required before any r004 endpoint starts.
