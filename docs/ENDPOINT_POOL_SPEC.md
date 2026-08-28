# Logical endpoint pool specification v2.0.0

## Purpose

複数 worker への割当を研究条件として再現しつつ、network address や device 固有 ID を
公開 artifact に含めない。

## Public config

単一 endpoint の bloc は `endpoint_id` と任意の `device_slot` を持つ。pool の場合は
bloc 直下の identity を置かず、次の形式を使う。

```yaml
endpoint_assignment_policy: round_robin_by_bloc_ordinal_v1
endpoint_pool:
  - endpoint_id: alpha-a
    device_slot: accelerator-0
  - endpoint_id: alpha-b
    device_slot: accelerator-1
```

Pool は空であってはならず、`endpoint_id` と存在する `device_slot` は pool 内で一意。
unknown field は拒否する。agent は bloc 内 ordinal による deterministic round-robin で
割り当てられる。

## Runtime resolution

各 `endpoint_id` は runtime-binding file の `endpoints` mapping に存在しなければならない。
余分な binding は許容するが、public config へ merge しない。agent の transport request
は address と logical identity の両方を process memory 内で持ち、raw attempt log には
logical identity だけを書く。

## Provenance

`run_meta.json.models` は bloc/model/provider、endpoint ID、device slot、assignment policy、
model artifact fields を記録する。address と device 固有 ID は記録しない。
