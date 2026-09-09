# Disaster LLM behavior pilot: descriptive observations

Metric: `disaster-behavior-pilot-metric-v1.0.0`; warning metric: `disaster-metric-v2.0.0`.

## Direct run status

All six pre-specified runs are retained. Missing data are not completed observations.

| Model | Seed | Status | Eligible | Calls | HTTP attempts |
| --- | --- | --- | --- | --- | --- |
| qwen | 6201 | completed | True | 480 | 480 |
| llama | 6201 | completed | True | 480 | 480 |
| gemma | 6201 | completed | True | 480 | 480 |
| gemma | 6202 | completed | True | 480 | 480 |
| llama | 6202 | completed | True | 480 | 480 |
| qwen | 6202 | completed | True | 480 | 480 |

Run IDs, config hashes, source commits, complete failure counters and raw references are in `summary.json` and `analysis_meta.json`.

## Mechanical derivation

| Model | Seed | Window | Stay | Move | Up | Down | Left | Right |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| qwen | 6201 | all_steps | 1 | 239 | 0 | 0 | 157 | 82 |
| qwen | 6201 | steps_ge_10 | 0 | 204 | 0 | 0 | 140 | 64 |
| llama | 6201 | all_steps | 0 | 240 | 0 | 0 | 15 | 225 |
| llama | 6201 | steps_ge_10 | 0 | 204 | 0 | 0 | 12 | 192 |
| gemma | 6201 | all_steps | 119 | 121 | 0 | 1 | 36 | 84 |
| gemma | 6201 | steps_ge_10 | 117 | 87 | 0 | 1 | 35 | 51 |
| gemma | 6202 | all_steps | 116 | 124 | 0 | 0 | 6 | 118 |
| gemma | 6202 | steps_ge_10 | 116 | 88 | 0 | 0 | 6 | 82 |
| llama | 6202 | all_steps | 0 | 240 | 0 | 0 | 0 | 240 |
| llama | 6202 | steps_ge_10 | 0 | 204 | 0 | 0 | 0 | 204 |
| qwen | 6202 | all_steps | 10 | 230 | 35 | 57 | 88 | 50 |
| qwen | 6202 | steps_ge_10 | 8 | 196 | 35 | 57 | 70 | 34 |

Per-agent distances, hazard residence, refuge arrival, exposure and later exact-ID reuse are in `agents.jsonl` and `warning_agents.jsonl`; all warning outputs are in `warning_outputs.jsonl`.

## Pre-specified example

Seed 6201, step 10, agent 0.

| Model | Action | Direction | Position before | Position after | Action raw line |
| --- | --- | --- | --- | --- | --- |
| qwen | move | left | [-3, 8] | [-4, 8] | `disaster-llm-behavior-pilot-v1-20260909T120500Z-qwen-s6201/memory_reasoning.jsonl:37` |
| llama | move | right | [14, 8] | [15, 8] | `disaster-llm-behavior-pilot-v1-20260909T120500Z-llama-s6201/memory_reasoning.jsonl:37` |
| gemma | move | right | [14, 8] | [15, 8] | `disaster-llm-behavior-pilot-v1-20260909T120500Z-gemma-s6201/memory_reasoning.jsonl:37` |

All three original Phase 1, Phase 3 and position rows, raw-line hashes and warning exposures are quoted in `example.json`.

## Interpretation

Trajectories, messages, memory and input histories may already differ. Matching positions do not establish identical model inputs; no causal model effect or internal cognition is inferred.

Two world seeds describe examples only. No significance test, model ranking, internal-cognition claim or real-disaster effectiveness claim is made. Exposure is not reuse or adoption. A move choice need not change position.

## Proposal

Use the completed observations and the fixed example, including a null example, to explain what this instrument measures. Any further experiment requires a new prospective protocol.
