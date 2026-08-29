# Legacy Gemma startup incident, 2026-08-29

## Scope

This note records an engineering startup failure observed before any simulation
request was sent. It does not report model behavior and is not research
evidence.

## Direct observations

- Source commit `8dbf7a99eb1f920c293398f3ac6e0a01f85ea6dc` passed unit,
  contract, repository, and publication-history checks before remote execution.
- Qwen and Llama endpoints reached their ready states sequentially. The Gemma
  endpoint exited before a run directory was created. All selected GPUs then
  returned to the one-MiB idle baseline and no compute process remained.
- Kernel OOM and NVIDIA Xid event counts were zero. Host RAM and filesystem
  capacity were not exhausted.
- With FlashInfer visible under CPython 3.10.12, Gemma initialization raised a
  Python import-time `TypeError` inside FlashInfer communication code.
- With the launcher's FlashInfer import boundary active, weights loaded, but
  `gpu_memory_utilization=0.92` left 0.68 GiB for KV cache. vLLM reported that
  context 4096 required 1.32 GiB and rejected startup.
- With the same model digest, dtype, context, and fresh cache at utilization
  `0.95`, vLLM allocated 1.38 GiB of KV cache and 4,303 tokens of capacity.
- Historical r004 evidence also records utilization `0.95`, 1.38 GiB of KV
  cache, and 4,303 tokens. Historical dual-worker evidence at `0.92` directly
  records reuse of an AOT compile artifact, reducing the measured activation
  peak from 1.73 GiB to 0.26 GiB.

## Mechanical correction

- Pin the public vLLM runtime to the historical CPython 3.12.14 environment and
  its exact installed package versions.
- Keep FlashInfer unavailable before import and keep server output connected to
  the null device in the normal public launcher.
- Pin Gemma TP1/context-4096 legacy replay configs to utilization `0.95` because
  the public launcher deliberately uses a fresh ephemeral compile cache.

No model, model digest, context length, prompt bytes, sampling value, response
contract, world seed, phase ordering, or communication condition changes.
