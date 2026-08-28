# Experiment protocol template v2.0.0

各本実験について、run 開始前にこの template を複製して `docs/protocols/` へ保存する。
探索・pilot・formal の区分を明記し、結果を見た後に formal 判定規則を変更しない。

## 1. Identity

- Protocol version:
- Metric version:
- Log schema version: `2.0.0`
- Response contract version:
- Planned run IDs:
- Source commit policy: clean / explicitly recorded dirty pilot

## 2. Research question

- 観測対象となる現象:
- 二つ以上の対象領域:
- 時系列で追う一本の chain:
- 操作可能な intervention point:
- control condition:
- null/negative result の扱い:

## 3. Operational definitions

- `exposure`:
- `reuse`:
- `adoption` を使用する場合の追加条件:
- chain event の natural key:
- observation window:
- 事前固定する vocabulary / threshold:

受信だけを reuse/adoption と数えない。未来の run や後 step から選んだ語彙を過去の
event 判定へ使わない。

## 4. Conditions held constant

- world geometry and seed policy:
- prompts and response contract:
- sampling parameters:
- communication radius and edge policy:
- phase ordering and barriers:
- model condition 以外で揃える項目:

Prompt に bloc 名、モデル名、自他のモデル identity、望ましい結果を含めない。

## 5. Execution plan

- configs:
- seeds / repetitions:
- expected logical LLM calls:
- concurrency:
- timeout and stop conditions:
- runtime environment class:
- output root: `runs`

長時間、有料、remote accelerator 実験は開始前に generation 数、時間上限、停止条件を
明示する。

## 6. Acceptance and abort rules

- required raw files:
- expected steps and agents:
- transport/syntax/schema failure thresholds:
- incomplete/aborted run handling:
- collision handling:

Process exit codeだけで成功判定しない。`run_meta.json`、terminal status、completed steps、
manifest、failure counters を確認する。

## 7. Analysis plan

- primary metric and version:
- control comparison:
- multiplicity handling:
- excluded analyses:
- visualization role:

Derived data は `derived/<run>/<metric-version>-<timestamp>/` へ新規作成し、raw を変更しない。

## 8. Claim discipline

報告を direct observation、mechanical derivation、interpretation/inference、hypothesis/proposal
に分ける。各 empirical claim を run ID、config、source commit、raw JSONL、metric version
へ追跡可能にする。単一 run を robust evidence と呼ばない。
