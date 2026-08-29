# Historical Run Reproduction Protocol v1.0.0

## Status and purpose

This protocol is fixed before remote execution. It defines an engineering
reproduction audit for ten historical run conditions. It is not a new causal
experiment and every generated config sets `research_eligible` to `false`.

The audit asks whether this standalone public repository can execute the same
declared world seed, agent population, model snapshot, prompt bytes, sampling
settings, response format, and endpoint topology without creating private
operational artifacts. It does not ask whether an LLM reproduces identical
text. The simulation seed was not sent to the historical model backends, so
world-state replay must not be described as deterministic LLM replay.

## Complete source set

All ten named source attempts are included. Eight source attempts completed;
two were aborted. No source attempt is excluded because of its outcome.
Lineage fields in every generated config bind the source run ID, source commit,
source config SHA-256, source prompt SHA-256, protocol, log schema, terminal
status, and completed step count.

Nine conditions use seed `42`. The aborted Ollama pilot uses seed `1002`.
The complete planned set contains 19,106 logical model calls. Under the current
authorization of at most six GPUs, eight conditions containing 13,346 logical
calls are runnable. The two historical seven-GPU conditions contain 5,760
logical calls and remain `not_run` unless a separate seven-GPU authorization is
given.

## Fixed compatibility contracts

- protocol: `legacy-reproduction-v1.0.0`
- prompt contract: `legacy-prompts-v1.0.0`
- prompt file SHA-256:
  `f414ab30a963636d80239644c2d3770672c77d5b8bdde027de2eb15a0d08bc3d`
- response contract: `phase-response-v1.0.0`
- transport behavior:
  `legacy-subobject-generation-retry-v1.0.0`
- response failure policy: `record_and_continue`
- output log schema: `2.0.0`
- metric version: `metric-v2.0.0`

The historical balanced-object JSON parser and one regeneration after a syntax
parse miss are retained only in this explicit compatibility path. The formal
`phase-response-v2.0.0` path remains unchanged and aborts its own run on a
response-contract failure.

## Public-by-construction changes

The following differences from the source executions are deliberate and must
not be hidden:

- New immutable run IDs are generated; source run IDs are lineage only.
- Runtime URLs and device-unique IDs are replaced by public `endpoint_id` and
  `device_slot` values. Runtime bindings remain ephemeral.
- Log schema 2.0 records raw HTTP envelopes and model output for independent
  validation. Historical raw logs are never edited or overwritten.
- vLLM and Ollama server stdout/stderr are connected directly to the null
  device. No server-log file is created and no sanitizer is run.
- Managed Ollama servers run as the invoking user with a clean environment,
  loopback-only listeners, and per-server ephemeral home/cache directories.
  The read-only model root is supplied only at launch and its literal value is
  mechanically checked to be absent from the run tree and verification record.
- The public vLLM runtime disables FlashInfer before import. This differs from
  any historical endpoint for which sampler-kernel selection was not recorded.
- Multiple vLLM servers start sequentially under one shared startup deadline,
  avoiding simultaneous model-load peaks. Simulation begins only after every
  declared endpoint passes its model-identity health check.
- The public runtime uses CPython 3.10.12. Historical vLLM simulator metadata
  recorded CPython 3.12.14; the Ollama source attempts recorded CPython 3.10.12.
- Dual-worker A and B may be executed sequentially on one host under the
  six-GPU guard. Such runs do not reproduce simultaneous two-worker host load.

These changes make byte-identical output and timing reproduction unverifiable.
They do not change the declared world seed or legacy prompt bytes.

## GPU and topology boundary

Before each launch, the launcher must establish a baseline with `nvidia-smi`.
A selected GPU must be below the configured initial-memory ceiling. Activation
outside the selected set, inventory changes, more than six active GPUs, or
failure to return to baseline stops the batch.

The seven-GPU source topology is two Qwen endpoints, two Llama endpoints, and
three Gemma endpoints with deterministic round-robin assignment. It must not be
silently collapsed to six GPUs. Its public configs remain executable only after
the launcher ceiling and authorization are explicitly revised.

## Run-level and batch-level stopping rules

Each condition has a two-hour simulation ceiling and a ten-minute server-start
ceiling. A model syntax or schema failure is recorded against that run. The run
is retained and the next condition may proceed. A completed run that exceeds a
declared zero failure threshold fails strict validation; its raw bytes remain
available as a negative result.

The whole batch stops on any of the following:

- publication-boundary finding or runtime-binding persistence;
- source commit mismatch or dirty source state;
- unexpected GPU activation, GPU inventory change, or GPU release failure;
- model snapshot/digest mismatch;
- missing terminal metadata or inability to preserve a generated run;
- managed-process cleanup failure.

## Output and decision rules

Each launcher writes first to an ignored staging directory. It scans the raw
tree, confirms that runtime binding values are absent, and performs read-only
strict validation. The unmodified run directory is then moved to `runs/` and a
timestamped validation record is written below `derived/`.

A replay is reported as operationally completed only when all of the following
are directly observed:

- terminal status `completed`, `aborted=false`, and expected step/agent coverage;
- raw manifest available and internally consistent;
- strict validation PASS;
- publication finding count zero;
- runtime binding persistence zero;
- all managed process groups stopped;
- selected GPUs returned to baseline.

Any comparison with the source output is a separate mechanical derivation. No
single replay is treated as evidence of robustness, emergence, adoption, or
causality.
