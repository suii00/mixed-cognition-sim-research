# Public disaster v3.1 token-limit incident: 2026-08-29

## Classification

これはsimulation結果の解釈ではなく、source
`6ac3f9f3f39e96155897fc2f414506591136d990`で観測したoperational incidentである。
model output本文は表示、実行、または変更判断の意味内容として利用していない。

## Direct observations

- batch ID: `public-disaster-matrix-20260829T010552027444Z-e5b9129cb226`
- public treeへpromoteしたrun: 0
- worker-verified completed runs: 51
- aborted runs: 2
- 未開始runs: 7
- logical calls / HTTP attempts: 127,392 / 127,392
- generation retry: 0
- transport failure: 0
- syntax parse failure: 1
- schema validation failure: 0
- Llama terminal attempt:
  - run ID: `public-disaster-v3p1-llama_only-structured_warning-s3105-wb`
  - step 48、Phase 3、agent 21
  - HTTP status 200、`finish_reason=length`
  - prompt 509 tokens、completion 512 tokens、total 1,021 tokens
  - raw output length 994 characters
  - object開始文字あり、object終了文字なし
  - `LLMSyntaxError`、schema validation未到達
- Qwen cleanup-aborted run:
  - run ID: `public-disaster-v3p1-qwen_only-communication_none-s3105-wa`
  - 51/60 completed steps、1,248 calls / attempts
  - retry、transport、syntax、schema failure 0
- staging publication scan: unapproved match 0
- runtime binding string: 0
- repository `.log` file: 0
- cleanup後: managed vLLM process 0、formal listener 0、全GPU 1 MiB baseline
- tracked public tree: clean

## Mechanical diagnosis and correction

HTTP transportは成功したが、Llama generationが512-token ceilingでJSON objectを閉じる前に停止し、
strict parserがterminal syntax failureとして正しくrunをabortした。もう一方のworkerはmatrix全体の
fail-closed cleanupで停止した。completed 51 runとaborted raw attemptはv3.2 formal resultへ混ぜない。

新protocol v3.2.0では`llm_defaults.max_tokens`だけを512から1,024へ変更し、run IDとconfig
directoryを分離する。有限のtoken ceilingは成功保証ではないため、同じstrict failure条件を維持する。
追加GPU実行は新しいsource SHA、call/time envelope、累積消費量の承認後だけ行う。
