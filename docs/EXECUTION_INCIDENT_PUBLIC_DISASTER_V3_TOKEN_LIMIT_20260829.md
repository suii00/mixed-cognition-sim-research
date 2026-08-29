# Public disaster v3 token-limit incident: 2026-08-29

## Classification

これはsimulation結果の解釈ではなく、source
`4c0cd992d6da24c3a06f608d7a1091a8ca78dd02`で観測したoperational incidentである。
model output本文は表示、実行、判断材料への利用をしていない。

## Direct observations

- batch ID: `public-disaster-matrix-20260829T004345175908Z-5a7cb64aae50`
- public treeへpromoteしたrun: 0
- Llama run:
  - status aborted、5/60 completed steps
  - failure step 6、Phase 3、agent 12
  - 288 logical calls、288 HTTP attempts
  - syntax parse attempt failure 1、terminal syntax failure 1
  - failing HTTP status 200、`finish_reason=length`
  - completion tokens 256、raw output length 818 characters
  - raw outputはobject開始文字を持ち、object終了文字を持たなかった
- Qwen run:
  - cleanupによりstatus aborted、9/60 completed steps
  - 480 logical calls、480 HTTP attempts
  - retry、transport、syntax、schema failure 0
- attempt全体: 768 logical calls、768 HTTP attempts
- staging publication scan: finding 0
- cleanup後: selected GPU memoryは全てbaseline、formal listenerは全てclosed

## Mechanical diagnosis and correction

HTTP transportとschema-guided generationは開始していたが、256-token generation ceilingでJSONが
閉じる前に停止したため、strict parserがterminal syntax failureとして正しくrunをabortした。

新protocol v3.1.0ではmax tokensだけを512へ変更し、run IDとconfig directoryを分離する。
v3.0.0 attemptはv3.1.0 formal resultへ混ぜない。追加GPU実行は変更後のcall/time envelopeの
承認後だけ行う。
