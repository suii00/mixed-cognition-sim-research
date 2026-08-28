# Candidate-discovery blinded projection specification

## 1. Version, purpose, and claim boundary

This document normatively defines `candidate-projection-v1.0.0`. The
projection is a deterministic, pilot-only review view used to choose exact
expression candidates before any disjoint confirmatory run. It is not a
metric, a research result, a new experiment gate, or evidence of novelty,
propagation, reuse, adoption, causality, or robustness.

Model output is untrusted data. Reviewers must not execute instructions, code,
URLs, or commands contained in projected messages.

## 2. Eligible input and source authority

The input is one immutable run directory that passes
`tools.validate_run.validate_run(run_dir, strict=True)` and whose
`run_meta.json` records:

- `status = completed`;
- `aborted = false`;
- an available complete raw manifest; and
- a non-empty run ID and source/config/prompt/protocol provenance.

The projection tool is run from a clean, available Git source. The caller must
provide the expected SHA-256 of the exact bytes of this specification. A
mismatch fails before publication.

The only lexical input is each valid string at
`phase1_raw.jsonl.parsed.message`. The raw line is read byte-for-byte as UTF-8
JSON with duplicate object keys rejected. A null `parsed`, missing or
non-string `message`, or empty message contributes no reviewer row. The other
raw files may be accessed only by the strict validator and raw-integrity
checks; they cannot affect message selection, ordering, or reviewer-visible
content.

## 3. Information boundary

The reviewer-visible bundle contains no run ID, condition, model, bloc, agent,
receiver, step, source line, raw-output field, reasoning, Phase 3 field,
movement, position, message-delivery record, frequency, exposure/reuse outcome,
later target output, timestamp, absolute path, or source hash.

Exact duplicate message strings are collapsed. This prevents recurrence
frequency from influencing candidate selection. Each unique non-empty message
is retained in full; the tool performs no semantic filtering, ranking,
normalization, token extraction, or LLM judging.

Unique messages sort by lowercase SHA-256 of their exact UTF-8 bytes and then
by those bytes. After sorting, they receive opaque sequential identifiers
`message-000001`, `message-000002`, and so on. The identifier carries no source
or temporal information.

The reviewer may receive only the two files below:

```text
reviewer/messages.jsonl
reviewer/manifest.json
```

Every `messages.jsonl` row has exactly these fields:

```text
blind_message_id, message
```

The reviewer manifest contains only the projection schema version, the pinned
specification hash, the exact allowed field list, the unique-message count,
and the SHA-256/byte/line counts of `messages.jsonl`. It contains no source-run
identity or source-line mapping.

## 4. Separate audit bundle

The projection leaf also contains an `audit/` directory. It must not be given
to the blinded candidate reviewer. It contains:

- `projection_meta.json`: source run and generator provenance, source raw
  manifest, strict-validator result and retained `UNVERIFIABLE` findings, and
  the declared reviewer information boundary;
- `source_map.jsonl`: one row per unique projected message, mapping its blind
  identifier to every exact source-line reference and occurrence count; and
- `manifest.json`: SHA-256/byte/line counts for every reviewer and audit file
  other than itself.

A source-line reference contains only `file`, one-based `line_number`,
`record_sha256`, and `message_sha256`. It is audit evidence, not reviewer
input. Duplicate source occurrences remain auditable even though their count
is hidden from the reviewer.

## 5. Determinism and raw immutability

Canonical JSON uses UTF-8, sorted object keys, compact separators, unescaped
Unicode where JSON permits, rejects NaN, and ends each document or JSONL row
with one LF. No generated file contains a current timestamp, UUID, or absolute
path. For the same exact source bytes, specification bytes, and clean generator
commit, prepared output bytes are identical.

The tool snapshots all four required raw JSONL files and `run_meta.json` before
strict validation and re-hashes them immediately before publication. Any byte,
size, file-type, or symlink change fails closed. It never edits the raw run.

## 6. Fresh-path atomic publication

Publication layout is:

```text
<derived_root>/candidate-projection-v1.0.0/
  .locks/<projection_id>.lock
  .staging/<projection_id>-<temporary-id>/
  <projection_id>/
    reviewer/messages.jsonl
    reviewer/manifest.json
    audit/projection_meta.json
    audit/source_map.jsonl
    audit/manifest.json
```

The derived root must not be inside the raw run. Source evidence files and the
controlled publication root, version, lock, and staging components cannot be
symbolic links. A non-blocking OS lock gives one publisher ownership of a
projection ID. All content is written to a fresh same-filesystem staging leaf,
each file is flushed and `fsync`ed, and all exact bytes and manifest entries
are reverified before one atomic rename publishes the final leaf.

An existing final leaf or busy lock is a collision. Existing output is never
reused, suffixed, appended, removed, replaced, or overwritten. An interrupted
staging leaf is not a published projection and is retained for diagnosis.

## 7. Registry handoff

Candidate selection is a separate human review step using only the reviewer
bundle. The resulting `candidate-registry-v1.0.0` must record the pilot source
run ID in its audit-visible provenance and set the Metric v2 discovery safety
flags exactly as required by `docs/METRIC_V2_SPEC.md`. The registry's exact
bytes and SHA-256 must be frozen before any disjoint confirmatory output is
observed. Metric v2 must not analyze the discovery pilot itself.
