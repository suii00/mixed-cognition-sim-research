# Tools

## Backend execution

The foundational Ollama path enters through `main.py`; see the
[repository README](../README.md) and
[local execution guide](../docs/SIMPLE_BACKEND_EXECUTION.md). The advanced
vLLM path uses the launcher below.

## Advanced vLLM execution

`run_public_vllm.py` owns the no-log server lifecycle, exact runtime/model
preflight, maximum-six-GPU allocation, health checks, strict validation,
publication scan, and cleanup. It is the standard launcher for vLLM experiments
and for reproducing the repository's current formal artifacts.

```bash
python tools/run_public_vllm.py --preflight-only
python tools/run_public_vllm.py
```

All verification commands are read-only. Generators create new files/directories and refuse
overwrite; no tool creates a transformed public snapshot.

## Integrity and publication boundary

```bash
python tools/validate_run.py runs/output_<run_id> --strict
python tools/verify_repository.py
python tools/scan_publication.py . --git-history
```

For a run produced elsewhere, `ingest_run.py` validates and scans the source, copies it byte for
byte into `runs/`, and verifies every SHA-256 before the final atomic rename.

```bash
python tools/ingest_run.py <source-output-directory>
```

## Experiment builders and runners

- `build_public_disaster_matrix.py`: freezes the research-eligible 4 x 3 x 5
  public matrix at 60 runs and 144,000 no-retry HTTP attempts.
- `run_public_disaster_matrix.py`: owns five no-log vLLM servers on at most six
  GPUs, two fail-fast workers, per-run and aggregate strict validation, scan,
  byte-preserving promotion, and cleanup.
- `public_disaster_matrix_worker.py`: internal one-shot worker used only inside
  ignored staging; it is not a standalone runtime entry point.
- `build_disaster_matrix.py` and `build_disaster_120_matrix.py`: create versioned matrix configs.
- `disaster_matrix_runner.py` and `disaster_120_matrix_runner.py`: execute matrix cells with a
  separately supplied runtime binding.
- `build_vllm_observability_probe_r002.py`, `build_vllm_observability_probe_r003.py`, and
  `build_vllm_phase_contract_probe.py`: generate prospective probe configs.
- `probe_vllm_json_schema.py` and `probe_vllm_phase_contract.py`: execute bounded transport probes.

## Analysis

- `metric_v2.py`: versioned communication/reuse metrics.
- `disaster_metric.py`: disaster-chain metrics.
- `candidate_projection.py`: candidate-discovery projection.
- `phase3_semantic_audit.py`: Phase 3 semantic contract audit.
- `compare_vllm_dual_worker.py` and `compare_vllm_scale.py`: operational comparisons.

## Visualization

`render_video.py` and `render_report.py` write a new timestamped directory beneath `derived/` by
default. They visualize recorded values only; they do not infer intent, coordination, or causality.

```bash
python tools/render_report.py runs/output_<run_id>
python tools/render_video.py runs/output_<run_id>
```
