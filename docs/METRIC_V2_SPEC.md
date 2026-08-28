# Metric v2 Normative Specification

## 1. Version, status, and scope

This document normatively defines `metric-v2.0.0`.

- Candidate registry schema: `candidate-registry-v1.0.0`
- Derived schema: `metric-derived-v1.0.0`
- Normalization: `nfkc-casefold-token-sequence-v1`
- Primary matching method: exact normalized token-sequence presence

Metric v2 describes registered-expression events inside one eligible run. It
does not establish linguistic novelty, belief change, adoption, causality,
robustness, or an effect outside the simulation. An `innovation` is only the
first observed self-generation of a registered expression within that run.

Behavioral association is deferred. No causal or behavioral claim is produced
by `metric-v2.0.0`. Statistical tests, confidence intervals, condition
comparisons, semantic embeddings, fuzzy matching, stemming, lemmatization, and
LLM judging are outside this version.

## 2. Eligible runs and input authority

Before analysis, the run must pass
`tools.validate_run.validate_run(run_dir, strict=True)`. In addition:

- `run_meta.status` is `completed`;
- `run_meta.aborted` is `false`;
- `run_meta.metric_version` is `metric-v2.0.0`; and
- the required raw manifest is available.

The strict validator's `UNVERIFIABLE` statements remain unverifiable and are
copied verbatim into `analysis_meta.json`.

The only evidence of an agent's self-generated registered-expression usage is
`phase1_raw.jsonl` → `parsed.message`. A null `parsed`, a missing `message`, or a
non-string `message` contributes no self-use.

The following are excluded from self-use, innovation, and reuse:

- `phase1_raw.jsonl.raw_output` and `parsed.reasoning`;
- a delivered `messages.jsonl.message` attributed to its receiver;
- `messages.jsonl.reasoning`;
- all `memory_reasoning.jsonl` fields and all Phase 3 output;
- prompt text and strings in `run_meta.config`.

Delivery evidence comes only from `messages.jsonl.step`, `sender_id`,
`receiver_ids`, and `message`. Each receiver ID is one exposure. Delivery alone
is not reuse, adoption, or belief change.

`run_meta.json` supplies run identity, source/config/prompt/protocol provenance,
agent-to-bloc/model mapping, the raw manifest, expected steps, and completed
status. It is not a lexical input.

## 3. Fixed candidate registry

Analysis requires both a registry path and the expected lowercase SHA-256 of
the exact registry bytes. A mismatch fails before any derived leaf is claimed.
Metric v2 never discovers candidates from the target run.

The registry is a UTF-8 JSON object with these required top-level fields and no
unknown top-level fields:

```text
schema_version, metric_version, registry_id, normalization,
discovery_provenance, excluded_expressions, expressions
```

`schema_version`, `metric_version`, and `normalization` must equal the versions
in Section 1. `registry_id` is non-empty. `expressions` is a non-empty array of
objects containing non-empty `expression_id` and `text`. Expression IDs are
unique. Normalized token sequences are non-empty and unique.

`excluded_expressions` contains objects with non-empty `text` and `reason`.
Normalized excluded expressions are unique. A candidate equal to an excluded
expression after normalization is invalid.

`discovery_provenance` must contain:

```text
purpose = pilot-only
source_run_ids = non-empty array of non-empty IDs
condition_labels_hidden = true
model_labels_hidden = true
receiver_ids_accessed = false
later_target_outputs_accessed = false
```

These fields fail closed against choosing a candidate with receiver outcomes,
later target outputs, or revealed condition/model labels. Gate 1 does not
create a production registry, and no production registry is frozen here.

Duplicate JSON object keys are invalid. Additional nested descriptive fields
do not affect matching or event identities.

## 4. Normalization and exact matching

For each candidate and message, apply in order:

1. Unicode NFKC;
2. `casefold()`;
3. collapse consecutive Unicode whitespace to one ASCII space;
4. remove leading and trailing whitespace; and
5. tokenize with Python Unicode semantics using
   `[^\W_]+(?:['’\-][^\W_]+)*`.

A candidate matches only when its entire token sequence appears contiguously in
the message token sequence. Substrings, reordered tokens, stems, synonyms,
fuzzy matches, embeddings, and judge decisions do not match. Multiple
occurrences of one expression in one Phase 1 or delivery record count as
presence once. Different registered expressions in one record are independent.
Semantic matching is not used in the primary metric.

## 5. Raw-line provenance

JSONL is read in binary mode, one physical line at a time. Every referenced raw
record stores:

```text
file
line_number
record_sha256
message_sha256
```

Line numbers are one-based. `record_sha256` hashes the exact line bytes,
including the existing newline. `message_sha256` hashes the exact UTF-8 bytes of
the message string. No absolute path is stored.

## 6. Innovation

For each registered expression, find the smallest self-use step in the run.

- One agent at that step: `unique_origin`.
- Multiple agents at that step: `simultaneous_origin`.
- No self-use: absent in the summary and no innovation event.

Simultaneous origins remain derived events but are excluded from all
source-attributed second-hop chains.

## 7. Exposure

A delivery message containing a registered expression produces one exposure
event per receiver. The delivery message must exactly equal the matching
same-step, same-sender `phase1_raw.jsonl.parsed.message`; otherwise analysis
fails closed.

Each exposure includes run/expression identity, step, sender and receiver IDs
and blocs, within/cross-bloc relation, the sender Phase 1 raw reference, the
delivery raw reference, and a deterministic event ID.

## 8. Receiver-expression eligibility and reuse

One status record is generated for every exposed `(expression_id,
receiver_id)` pair. Pairs with no exposure do not enter the exposure-based
denominator.

`first_exposure_step` is the minimum exposure step. All event and sender IDs at
that step are retained. The first-exposure relation is:

- `cross_bloc` if all first senders differ from the receiver's bloc;
- `within_bloc` if all first senders share the receiver's bloc; or
- `mixed_ambiguous` if both relations occur.

Mixed pairs remain in the overall eligible denominator but are excluded from
both relation-specific denominators.

If the receiver self-used the expression at
`self_use_step <= first_exposure_step`, status is
`excluded_prior_or_same_step_use`. Phase 1 precedes delivery, so same-step
self-use is not reuse.

Otherwise, the first receiver self-use at `reuse_step > first_exposure_step`
has status `eligible_reused`. Its latency is
`reuse_step - first_exposure_step`, and at most one reuse event is emitted for
the pair. If no later self-use occurs, status is `eligible_no_reuse` and
`censor_step` is the run's expected final step. Eligible non-reuse remains in
the denominator with numerator zero.

For a reused pair, `exposure_count_before_reuse` counts exposures with step
strictly less than the reuse step. For eligible non-reuse it equals all
exposures through censoring. It is null for a prior/same-step exclusion.

## 9. Second hop

A secondary `second_hop` event is emitted only when all conditions hold:

1. the expression has unique origin agent `S`;
2. relay `R` has a unique first-exposure sender, `S`;
3. `R` reuses at a strictly later step;
4. that reuse message is delivered in the same step from `R` to target `T`;
5. `T` has a unique first-exposure sender, `R`;
6. `T` had neither self-use through that step nor an earlier exposure;
7. `T` reuses at a strictly later step; and
8. `S`, `R`, and `T` are mutually distinct.

A tied first exposure with more than one parent sender is ambiguous and has no
second-hop attribution. Each second-hop event references the innovation,
first-hop reuse, second-hop exposure, and second-hop reuse event IDs. Hops
beyond the second are not implemented.

## 10. Summary, denominators, and censoring

`summary.json` reports registered/present/absent expression counts,
unique/simultaneous origins, exposure and exposed-pair counts, exclusions,
eligible reuse and non-reuse counts, overall and within/cross-bloc reuse rates,
mixed ambiguous pairs, and second-hop chains.

The overall reuse denominator is all statuses `eligible_reused` plus
`eligible_no_reuse`. Relation-specific denominators contain only eligible pairs
with that unambiguous relation. A zero denominator serializes as JSON `null`,
never `0.0`.

## 11. Deterministic event IDs and ordering

An event ID is lowercase SHA-256 over canonical JSON for that event's identity
key. Canonical JSON uses UTF-8, sorted object keys, compact separators, Unicode
characters unescaped where JSON permits, and rejects NaN. Identity keys include
event type, run and expression identity, the relevant actors/steps, and stable
raw line or referenced event identities. They never include a timestamp or
absolute path.

Events sort by event-type order (`innovation`, `exposure`, `reuse`,
`second_hop`), expression ID, step, actor IDs, then event ID. Pair statuses sort
by expression ID and receiver ID. Registry expressions sort by expression ID.

## 12. Derived output schema and immutability

The publication layout is:

```text
<derived_root>/metric-v2.0.0/
  .locks/<run_id>.lock
  .staging/<run_id>-<temporary-id>/
  <run_id>/
```

All input validation and derived byte construction occur before publication.
Publication then uses a non-blocking, per-run operating-system file lock. Lock
ownership belongs to the open process handle and is released by the operating
system when that handle closes or the process terminates; the persistent lock
file is not itself an ownership claim. A busy lock and an existing final leaf
are both explicit collisions. Concurrent publishers therefore have exactly one
owner.

While holding the lock, the owner creates a unique staging leaf under
`.staging` on the same filesystem as the final leaf. It writes all five files,
flushes and `fsync`s every file, rereads the staged bytes, and verifies the
required file set plus every manifest hash, byte count, and newline count. Only
after that verification may it atomically rename the staging directory to:

```text
<derived_root>/metric-v2.0.0/<run_id>/
```

The final leaf is therefore a publication boundary: if it exists, it represents
only a completed, manifest-verified result. Existing final leaves are immutable
collisions; they are never reused, renamed, suffixed, appended to, removed,
replaced, or overwritten.

A staging leaf is private, non-published, and ineligible as a research result,
even if its staged `analysis_meta.json` already contains `"status":"completed"`.
Failure or abrupt termination before atomic rename must leave the final leaf
absent. A residual staging leaf does not block a later attempt, which creates a
new unique staging leaf. Normal analysis neither treats residual staging as a
result nor deletes it; inspection and cleanup are separate administrative
operations so one process cannot remove another process's live staging data.

The derived root may not resolve inside the raw run, including through a
symbolic link. Raw files are read-only inputs and are never changed.

Successful leaves contain:

```text
analysis_meta.json
events.jsonl
receiver_expression_status.jsonl
summary.json
derived_manifest.json
```

`analysis_meta.json` records derived schema/status/version/normalization,
registry and spec hashes, run/source/config/prompt/protocol/metric provenance,
the raw manifest, analysis Git SHA/dirty state, strict validity, and the exact
strict-validator unverifiable list. It contains no timestamp, hostname, or
absolute path.

`derived_manifest.json` records SHA-256, byte count, and newline count for the
other four files and does not include itself. All JSON uses canonical compact
serialization with a final newline. JSONL records use the same serialization;
non-empty JSONL files have a final newline. Identical run, registry, code, spec,
and analysis source state produce byte-identical files in different derived
roots.

## 13. Legacy metric boundary

`tools/vocab_metrics.py` and `output_mvp_demo/vocab_*` are legacy exploratory
artifacts. They are not Metric v2 evidence and must not be used for
confirmatory claims. Metric v2 neither imports nor modifies the legacy module.

## 14. Versioning rule

Any semantic change to matching, eligibility, temporal ordering, event
identity, denominators, censoring, or second-hop attribution requires all of:

1. a metric version bump;
2. a new normative spec hash;
3. a protocol update;
4. updated regression fixtures; and
5. a fresh versioned derived path.

Existing raw or derived artifacts are never retrofitted.

## 15. Required regression fixtures

The normative fixture set covers delivery-only, same-step self-use, prior
self-use, multiple exposures, valid second hop, ambiguous second-hop parent,
simultaneous origin, memory/reasoning exclusion, unregistered future text,
registry hash mismatch, invalid registries, invalid raw runs, sequential and
process-race collisions, raw immutability, deterministic byte equality, exact
raw provenance, zero denominators, untrusted text non-execution, exact token
boundaries, and output-path/symlink rejection. Publication fixtures inject
failure after each of the first four file writes, during manifest writing, and
after manifest verification but before rename. A separate spawned-child fixture
terminates the publication owner while it holds the lock. Every interruption
must leave no final leaf, preserve raw hashes, remain absent from published
result enumeration, and permit a later complete, manifest-valid publication.
